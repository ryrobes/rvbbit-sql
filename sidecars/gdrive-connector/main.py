"""Google Drive connector sidecar — lists configured folders or single files,
maps Drive sharing to allowed emails, stages changed files, and returns a
manifest the rvbbit brain reconciles. The pluggable "file source connector" for
the document brain (Gdrive first; S3/NFS/local connectors implement the same
/sync contract).

Auth (two modes, auto-detected from the credential JSON's "type"; env, never from rvbbit):
  GDRIVE_SA_KEY = path to the credential JSON (or the JSON itself), either:
    • "service_account"  → acts as the SA; admin shares the target folders/docs with the SA email.
                           Best for org-wide ingest + reading Drive sharing (permissions.list).
    • "authorized_user"  → acts as YOU (3-legged OAuth refresh token); no admin/SA needed. Generate with:
          gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/drive.readonly
                           Sees what your account sees (no folder-sharing-with-SA). Caveats: a "Testing"
                           consent screen expires the refresh token after ~7 days; and permissions.list on
                           entries you don't own may be partial → the Drive→emails ACL can be incomplete
                           (fall back to setting source roles manually).
Scopes: drive.readonly + drive.metadata.readonly (read files + their permissions).

Contract (rvbbit POSTs this):
  POST /sync
  { "source_id": 4,
    "folders": ["<driveFolderId-or-url>", "<driveFileId-or-url>", ...],
                                                 # configured Drive entries (ACL domains)
    "cursor": null,                           # reserved for Drive changes API (incremental)
    "known": { "<fileId>": "<contentHash>" }  # so we only download CHANGED files
  }
  →
  { "files": [ { "uri","title","rel_path","folder_id","mime","modified_at",
                 "content_hash","permissions":[email,...],"staged_path","body"? }, ... ],
    "pending_grants": [ {"folder_id","grant_kind":"group|domain|anyone","grant_value"}, ... ],
    "cursor": null }

ACL model (STRICT, matches the brain's default-deny): a CONFIGURED Drive entry
(folder or single file) is one ACL domain. Every file under a configured folder
inherits that folder's INDIVIDUAL-user shares as `permissions`; a configured
single file uses that file's own shares when Drive exposes them. Group /
domain-wide / anyone-with-link shares are NOT auto-granted; they surface in
`pending_grants` for admin approval. We return ALL current files (so rvbbit can
tombstone vanished ones) but stage bytes only for new/changed files; unchanged
files come back metadata-only.
"""
from __future__ import annotations

import io
import json
import os
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

EXPECTED_TOKEN = os.environ.get("CONNECTOR_TOKEN", "")
STAGING_DIR = os.environ.get("STAGING_DIR", "/staging")
SA_KEY = os.environ.get("GDRIVE_SA_KEY", "")


def _env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


MAX_STAGE_BYTES = _env_int("GDRIVE_MAX_STAGE_BYTES", 64 * 1024 * 1024)
PAGE_SIZE = _env_int("GDRIVE_PAGE_SIZE", 200, maximum=1000)
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

# Google-native types have no bytes/md5 — export them to a concrete format first.
GOOGLE_EXPORT = {
    "application/vnd.google-apps.document": ("text/markdown", ".md"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}
_TEXT_MIMES = ("text/markdown", "text/plain")
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_FILE_FIELDS = "id,name,mimeType,modifiedTime,md5Checksum,version,size,trashed"

_svc = None


def _service():
    """Build the Drive client (lazy). Auto-detects service-account vs user-OAuth
    credentials by the JSON's "type" field, so the same env var takes either."""
    global _svc
    if _svc is not None:
        return _svc
    from googleapiclient.discovery import build

    if not SA_KEY:
        raise RuntimeError("GDRIVE_SA_KEY is not set")
    if SA_KEY.lstrip().startswith("{"):
        info = json.loads(SA_KEY)
    else:
        with open(SA_KEY, encoding="utf-8") as fh:
            info = json.load(fh)
    ctype = (info.get("type") or "").strip()
    if not ctype:
        # Some files lack an explicit "type" (e.g. google-auth's creds.to_json()).
        # Infer: SA keys carry private_key+client_email; user-OAuth carries refresh_token+client_id.
        if info.get("private_key") and info.get("client_email"):
            ctype = "service_account"
        elif info.get("refresh_token") and info.get("client_id"):
            ctype = "authorized_user"
    if ctype == "service_account":
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif ctype == "authorized_user":
        # gcloud ADC / 3-legged OAuth refresh token — acts as the consenting user.
        from google.oauth2.credentials import Credentials as UserCredentials
        creds = UserCredentials.from_authorized_user_info(info, scopes=SCOPES)
    else:
        raise RuntimeError(
            f"unsupported credential type '{ctype}' "
            "(expected 'service_account' or 'authorized_user')"
        )
    _svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _svc


app = FastAPI()


class SyncRequest(BaseModel):
    source_id: int | None = None
    folders: list[str] = Field(default_factory=list)
    cursor: str | None = None
    known: dict[str, str] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "creds_configured": bool(SA_KEY)}


def _drive_id_from_locator(locator: str) -> str:
    """Accept a raw Drive ID or common Google Drive/Docs URL and return the file ID."""
    value = (locator or "").strip().strip("<>\"'")
    if not value:
        return ""

    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        params = parse_qs(parsed.query)
        for key in ("id", "folderId"):
            for candidate in params.get(key, []):
                candidate = candidate.strip()
                if candidate:
                    return candidate

        parts = [unquote(p) for p in parsed.path.split("/") if p]
        for marker in ("folders", "d"):
            if marker in parts:
                idx = parts.index(marker)
                if idx + 1 < len(parts):
                    return parts[idx + 1]
        if parts:
            return parts[-1]

    return value.rstrip("/")


def _get_drive_item(svc, item_id: str) -> dict[str, Any]:
    return svc.files().get(
        fileId=item_id,
        supportsAllDrives=True,
        fields=DRIVE_FILE_FIELDS,
    ).execute()


def _item_acl(svc, item_id: str) -> tuple[list[str], list[dict[str, str]]]:
    """A Drive entry's sharing → (individual emails, pending non-individual grants)."""
    emails: list[str] = []
    pending: list[dict[str, str]] = []
    page = None
    while True:
        resp = svc.permissions().list(
            fileId=item_id, pageToken=page, pageSize=100, supportsAllDrives=True,
            fields="permissions(id,type,emailAddress,domain,role),nextPageToken",
        ).execute()
        for p in resp.get("permissions", []):
            ptype = p.get("type")
            if ptype == "user" and p.get("emailAddress"):
                emails.append(p["emailAddress"].lower())
            elif ptype == "group":
                pending.append({"folder_id": item_id, "grant_kind": "group",
                                "grant_value": p.get("emailAddress", "")})
            elif ptype == "domain":
                pending.append({"folder_id": item_id, "grant_kind": "domain",
                                "grant_value": p.get("domain", "")})
            elif ptype == "anyone":
                pending.append({"folder_id": item_id, "grant_kind": "anyone", "grant_value": "anyone"})
        page = resp.get("nextPageToken")
        if not page:
            break
    return sorted(set(emails)), pending


def _list_folder(svc, folder_id: str, rel_path: str):
    """Yield (file_metadata, rel_path) for every non-folder file under folder_id, recursively."""
    page = None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageToken=page, pageSize=PAGE_SIZE, supportsAllDrives=True, includeItemsFromAllDrives=True,
            fields="files(id,name,mimeType,modifiedTime,md5Checksum,version,size),nextPageToken",
        ).execute()
        for f in resp.get("files", []):
            if f.get("mimeType") == FOLDER_MIME:
                yield from _list_folder(svc, f["id"], f"{rel_path.rstrip('/')}/{f['name']}")
            else:
                yield f, rel_path
        page = resp.get("nextPageToken")
        if not page:
            break


def _content_hash(f: dict[str, Any]) -> str:
    # Binary files have an md5; Google-native docs don't → use modifiedTime+version.
    return f.get("md5Checksum") or f"{f.get('modifiedTime','')}:{f.get('version','')}"


def _declared_size(f: dict[str, Any]) -> int:
    try:
        return int(f.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _safe_stage_name(file_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", file_id or "file")


def _stage(svc, f: dict[str, Any], dest_dir: str) -> tuple[str, str, str | None]:
    """Download/export a file to staging. Returns (staged_path, effective_mime, inline_body|None)."""
    if _declared_size(f) > MAX_STAGE_BYTES:
        raise RuntimeError("file is larger than the staging byte limit")

    from googleapiclient.http import MediaIoBaseDownload

    os.makedirs(dest_dir, exist_ok=True)
    mime = f["mimeType"]
    export = GOOGLE_EXPORT.get(mime)
    if export:
        eff_mime, ext = export
        request = svc.files().export_media(fileId=f["id"], mimeType=eff_mime)
    else:
        eff_mime, ext = mime, os.path.splitext(f.get("name", ""))[1] or ""
        request = svc.files().get_media(fileId=f["id"], supportsAllDrives=True)

    path = os.path.join(dest_dir, _safe_stage_name(f["id"]) + ext)
    with io.FileIO(path, "wb") as buf:
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    if os.path.getsize(path) > MAX_STAGE_BYTES:
        try:
            os.remove(path)
        finally:
            raise RuntimeError("downloaded file is larger than the staging byte limit")

    # Inline text so rvbbit can ingest without an extraction round-trip.
    inline = None
    if eff_mime in _TEXT_MIMES:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                inline = fh.read()
        except Exception:
            inline = None
    return path, eff_mime, inline


def _manifest_row(
    svc,
    f: dict[str, Any],
    *,
    rel_path: str,
    acl_id: str,
    permissions: list[str],
    known: dict[str, str],
    dest_dir: str,
) -> dict[str, Any]:
    chash = _content_hash(f)
    row: dict[str, Any] = {
        "uri": f["id"],
        "title": f.get("name") or f["id"],
        "rel_path": rel_path,
        "folder_id": acl_id,          # the configured Drive entry = the ACL domain
        "mime": f.get("mimeType"),
        "modified_at": f.get("modifiedTime"),
        "content_hash": chash,
        "permissions": permissions,
    }
    # Only stage bytes for new/changed files; unchanged → metadata only.
    if known.get(f["id"]) != chash:
        try:
            staged_path, eff_mime, inline = _stage(svc, f, dest_dir)
            row["staged_path"] = staged_path
            row["mime"] = eff_mime
            if inline is not None:
                row["body"] = inline
        except Exception:
            # Couldn't fetch this file; still report it (metadata) so it isn't tombstoned.
            pass
    return row


@app.post("/sync")
def sync(req: SyncRequest, authorization: str = Header(default="")) -> dict[str, Any]:
    if EXPECTED_TOKEN and authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")
    try:
        svc = _service()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"drive auth failed: {e}")

    dest_dir = os.path.join(STAGING_DIR, str(req.source_id or "0"))
    files_out: list[dict[str, Any]] = []
    pending_out: list[dict[str, str]] = []
    seen_pending: set[tuple] = set()

    for locator in req.folders:
        item_id = _drive_id_from_locator(locator)
        if not item_id:
            continue
        try:
            item = _get_drive_item(svc, item_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"metadata for Drive entry {locator}: {e}")
        if item.get("trashed"):
            continue

        try:
            emails, pending = _item_acl(svc, item_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"permissions for Drive entry {item_id}: {e}")
        for pg in pending:
            key = (pg["folder_id"], pg["grant_kind"], pg["grant_value"])
            if key not in seen_pending:
                seen_pending.add(key)
                pending_out.append(pg)

        if item.get("mimeType") == FOLDER_MIME:
            for f, rel_path in _list_folder(svc, item_id, "/" + item_id):
                files_out.append(_manifest_row(
                    svc,
                    f,
                    rel_path=rel_path,
                    acl_id=item_id,
                    permissions=emails,
                    known=req.known,
                    dest_dir=dest_dir,
                ))
        else:
            files_out.append(_manifest_row(
                svc,
                item,
                rel_path="/",
                acl_id=item_id,
                permissions=emails,
                known=req.known,
                dest_dir=dest_dir,
            ))

    return {"files": files_out, "pending_grants": pending_out, "cursor": None}
