"""Sidecar-level tests for Google Drive connector local safety helpers."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_gdrive(monkeypatch, tmp_path, max_stage_bytes: str | None = "10"):
    module_path = (
        Path(__file__).resolve().parents[1] / "sidecars" / "gdrive-connector" / "main.py"
    )
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    if max_stage_bytes is not None:
        monkeypatch.setenv("GDRIVE_MAX_STAGE_BYTES", max_stage_bytes)

    module_name = f"rvbbit_gdrive_test_{os.getpid()}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_gdrive_sync_request_defaults_are_isolated(monkeypatch, tmp_path):
    sidecar = _load_gdrive(monkeypatch, tmp_path)

    first = sidecar.SyncRequest()
    second = sidecar.SyncRequest()
    first.folders.append("folder-a")
    first.known["file-a"] = "hash-a"

    assert second.folders == []
    assert second.known == {}


def test_gdrive_stage_rejects_declared_oversize_before_download(monkeypatch, tmp_path):
    sidecar = _load_gdrive(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="larger than the staging byte limit"):
        sidecar._stage(
            object(),
            {
                "id": "file-1",
                "name": "large.txt",
                "mimeType": "text/plain",
                "size": "11",
            },
            str(tmp_path / "staging"),
        )


def test_gdrive_safe_stage_name_removes_path_characters(monkeypatch, tmp_path):
    sidecar = _load_gdrive(monkeypatch, tmp_path)

    assert sidecar._safe_stage_name("../odd/id") == ".._odd_id"


def test_gdrive_invalid_numeric_env_uses_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv("GDRIVE_MAX_STAGE_BYTES", "not-an-int")
    monkeypatch.setenv("GDRIVE_PAGE_SIZE", "9001")
    sidecar = _load_gdrive(monkeypatch, tmp_path, max_stage_bytes=None)

    assert sidecar.MAX_STAGE_BYTES == 64 * 1024 * 1024
    assert sidecar.PAGE_SIZE == 1000


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ("folder-plain-id", "folder-plain-id"),
        ("https://drive.google.com/drive/folders/folder-123?usp=sharing", "folder-123"),
        ("https://docs.google.com/document/d/doc-123/edit", "doc-123"),
        ("https://docs.google.com/document/u/0/d/doc-456/edit?tab=t.0", "doc-456"),
        ("https://drive.google.com/file/d/file-123/view?usp=drive_link", "file-123"),
        ("https://drive.google.com/open?id=open-123", "open-123"),
    ],
)
def test_gdrive_locator_accepts_ids_and_common_urls(monkeypatch, tmp_path, locator, expected):
    sidecar = _load_gdrive(monkeypatch, tmp_path)

    assert sidecar._drive_id_from_locator(locator) == expected


class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeFiles:
    def __init__(self, items, children=None):
        self.items = items
        self.children = children or {}

    def get(self, *, fileId, **_kwargs):
        return _FakeRequest(self.items[fileId])

    def list(self, *, q, **_kwargs):
        folder_id = q.split("'")[1]
        return _FakeRequest({"files": self.children.get(folder_id, [])})


class _FakePermissions:
    def __init__(self, permissions):
        self.permissions = permissions

    def list(self, *, fileId, **_kwargs):
        return _FakeRequest({"permissions": self.permissions.get(fileId, [])})


class _FakeDriveService:
    def __init__(self, items, children=None, permissions=None):
        self._files = _FakeFiles(items, children)
        self._permissions = _FakePermissions(permissions or {})

    def files(self):
        return self._files

    def permissions(self):
        return self._permissions


def test_gdrive_sync_can_manifest_single_google_doc_url(monkeypatch, tmp_path):
    sidecar = _load_gdrive(monkeypatch, tmp_path)
    modified = "2026-06-30T12:00:00Z"
    service = _FakeDriveService({
        "doc-123": {
            "id": "doc-123",
            "name": "Product Plan",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": modified,
            "version": "7",
            "trashed": False,
        }
    }, permissions={
        "doc-123": [
            {"type": "user", "emailAddress": "USER@EXAMPLE.COM"},
            {"type": "group", "emailAddress": "team@example.com"},
            {"type": "domain", "domain": "example.com"},
            {"type": "anyone"},
        ]
    })
    monkeypatch.setattr(sidecar, "_service", lambda: service)

    res = sidecar.sync(
        sidecar.SyncRequest(
            source_id=42,
            folders=["https://docs.google.com/document/d/doc-123/edit"],
            known={"doc-123": f"{modified}:7"},
        )
    )

    assert res["files"] == [{
        "uri": "doc-123",
        "title": "Product Plan",
        "rel_path": "/",
        "folder_id": "doc-123",
        "mime": "application/vnd.google-apps.document",
        "modified_at": modified,
        "content_hash": f"{modified}:7",
        "permissions": ["user@example.com"],
    }]
    assert res["pending_grants"] == [
        {"folder_id": "doc-123", "grant_kind": "group", "grant_value": "team@example.com"},
        {"folder_id": "doc-123", "grant_kind": "domain", "grant_value": "example.com"},
        {"folder_id": "doc-123", "grant_kind": "anyone", "grant_value": "anyone"},
    ]


def test_gdrive_sync_still_manifests_folder_children(monkeypatch, tmp_path):
    sidecar = _load_gdrive(monkeypatch, tmp_path)
    modified = "2026-06-30T13:00:00Z"
    service = _FakeDriveService(
        {
            "folder-123": {
                "id": "folder-123",
                "name": "Policies",
                "mimeType": "application/vnd.google-apps.folder",
                "trashed": False,
            }
        },
        children={
            "folder-123": [{
                "id": "file-123",
                "name": "Policy.md",
                "mimeType": "text/markdown",
                "modifiedTime": modified,
                "md5Checksum": "hash-123",
                "size": "128",
            }]
        },
        permissions={
            "folder-123": [{"type": "user", "emailAddress": "folder-user@example.com"}]
        },
    )
    monkeypatch.setattr(sidecar, "_service", lambda: service)

    res = sidecar.sync(
        sidecar.SyncRequest(
            source_id=42,
            folders=["https://drive.google.com/drive/folders/folder-123"],
            known={"file-123": "hash-123"},
        )
    )

    assert res["files"] == [{
        "uri": "file-123",
        "title": "Policy.md",
        "rel_path": "/folder-123",
        "folder_id": "folder-123",
        "mime": "text/markdown",
        "modified_at": modified,
        "content_hash": "hash-123",
        "permissions": ["folder-user@example.com"],
    }]
    assert res["pending_grants"] == []
