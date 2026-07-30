"""Calliope: Hermes-backed, user-indexed living artifact notebooks.

Hermes owns the agent conversation and shared company memory. This module owns
the authenticated Hub projection: a private-by-email session rail, mirrored
turn prose, image attachments, and an append-only surface ledger derived from
actual RVBBIT MCP tool results.

The feature is startup-opt-in. Both WAREHOUSE_HERMES_URL and
WAREHOUSE_HERMES_API_KEY must be set; otherwise no routes are registered and
the gallery renders no Calliope affordance.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

import httpx


_HERE = Path(__file__).resolve().parent
_ASSET_DIR = _HERE / "calliope"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
_DATA_IMAGE_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\r\n]+)$",
    re.I,
)
_MARKDOWN_DATA_IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]{0,240}\]\(\s*"
    r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\r\n]+\s*\)",
    re.I,
)
_INLINE_DATA_IMAGE_RE = re.compile(
    r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\r\n]+",
    re.I,
)
_MAX_ASSISTANT_CHARS = 40_000
_KNOWN_TOOLS = {
    "run_sql",
    "run_sql_multi",
    "metric",
    "publish_dashboard",
    "update_dashboard",
    "create_live_app",
    "update_live_app",
    "capture_live_app",
    "render_pdf",
}
_ARTIFACT_TOOLS = {
    "publish_dashboard",
    "update_dashboard",
    "create_live_app",
    "update_live_app",
}
_VISUAL_FEEDBACK_BUDGET = 2


@dataclass(frozen=True)
class CalliopeConfig:
    hermes_url: str
    hermes_api_key: str
    memory_key: str
    file_root: Path
    max_image_bytes: int

    @property
    def enabled(self) -> bool:
        return bool(self.hermes_url and self.hermes_api_key)

    @classmethod
    def from_env(cls) -> "CalliopeConfig":
        try:
            max_image = int(os.environ.get("WAREHOUSE_CALLIOPE_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))
        except (TypeError, ValueError):
            max_image = 8 * 1024 * 1024
        return cls(
            hermes_url=os.environ.get("WAREHOUSE_HERMES_URL", "").strip().rstrip("/"),
            hermes_api_key=os.environ.get("WAREHOUSE_HERMES_API_KEY", "").strip(),
            memory_key=os.environ.get("WAREHOUSE_HERMES_MEMORY_KEY", "").strip(),
            file_root=Path(os.environ.get("WAREHOUSE_CALLIOPE_DIR", "/app/data/calliope")),
            max_image_bytes=max(256 * 1024, min(max_image, 25 * 1024 * 1024)),
        )


def is_enabled() -> bool:
    return CalliopeConfig.from_env().enabled


# Shape-identical to migration 0222. Fresh extension installs get the migration;
# service-only upgrades self-heal here before the first Calliope request.
_DDL = """
CREATE TABLE IF NOT EXISTS rvbbit.calliope_sessions (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    hermes_session_id text UNIQUE NOT NULL,
    title text NOT NULL,
    archived boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS calliope_sessions_owner_updated_idx
    ON rvbbit.calliope_sessions (owner_email, archived, updated_at DESC);
CREATE TABLE IF NOT EXISTS rvbbit.calliope_turns (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    user_message text NOT NULL,
    assistant_message text,
    attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
    selected_surface_id uuid,
    hermes_message_id text,
    status text NOT NULL DEFAULT 'running',
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (session_id, ordinal)
);
CREATE INDEX IF NOT EXISTS calliope_turns_session_created_idx
    ON rvbbit.calliope_turns (session_id, created_at);
CREATE TABLE IF NOT EXISTS rvbbit.calliope_surfaces (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    turn_id uuid NOT NULL REFERENCES rvbbit.calliope_turns(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    tool_name text NOT NULL,
    tool_call_id text NOT NULL,
    lineage_key text NOT NULL,
    parent_surface_id uuid REFERENCES rvbbit.calliope_surfaces(id) ON DELETE SET NULL,
    artifact_slug text,
    artifact_version integer,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    presentation jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, tool_call_id, lineage_key)
);
CREATE INDEX IF NOT EXISTS calliope_surfaces_session_created_idx
    ON rvbbit.calliope_surfaces (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_surfaces_lineage_idx
    ON rvbbit.calliope_surfaces (session_id, lineage_key, created_at DESC);
CREATE TABLE IF NOT EXISTS rvbbit.calliope_attachments (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    turn_id uuid NOT NULL REFERENCES rvbbit.calliope_turns(id) ON DELETE CASCADE,
    original_name text,
    mime_type text NOT NULL,
    storage_path text NOT NULL,
    bytes integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS calliope_attachments_session_idx
    ON rvbbit.calliope_attachments (session_id, created_at);
"""


def ensure_tables(conn_factory: Callable[..., Any]) -> None:
    with conn_factory() as conn:
        conn.execute(_DDL)
        # A server restart cannot preserve an in-flight SSE/agent task. Clear
        # those abandoned leases now so the per-session concurrency guard does
        # not strand a notebook forever after a crash or deploy.
        conn.execute(
            "UPDATE rvbbit.calliope_turns "
            "SET status='interrupted',error='warehouse service restarted',"
            "completed_at=coalesce(completed_at,now()) WHERE status='running'"
        )


def _uuid(value: Any) -> str | None:
    value = str(value or "").strip().lower()
    return value if _UUID_RE.fullmatch(value) else None


def _now_iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row_json(row: Any) -> dict[str, Any]:
    return {k: _now_iso(v) for k, v in dict(row or {}).items()}


def _canonical_owner(request: Any) -> tuple[str | None, dict[str, Any] | None]:
    import auth

    session = auth.read_session_full(request)
    if not session:
        return None, None
    identity = str(session.get("identity") or "").strip().lower()
    return (identity or None), session


def _hermes_headers(config: CalliopeConfig) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.hermes_api_key}",
        "Content-Type": "application/json",
    }
    # One optional COMPANY scope. Never derive this from owner email: the
    # intended pilot behavior is shared memory across Calliope + GChat.
    if config.memory_key:
        headers["X-Hermes-Session-Key"] = config.memory_key
    return headers


def _hermes_session_url(config: CalliopeConfig, hermes_id: str, suffix: str = "") -> str:
    return f"{config.hermes_url}/api/sessions/{quote(hermes_id, safe='')}{suffix}"


async def _hermes_json(
    config: CalliopeConfig,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timeout = httpx.Timeout(45.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method,
            f"{config.hermes_url}{path}",
            headers=_hermes_headers(config),
            json=body,
        )
    if response.status_code >= 400:
        detail = response.text[:800]
        raise RuntimeError(f"Hermes {method} {path} failed ({response.status_code}): {detail}")
    try:
        value = response.json()
    except ValueError:
        value = {"text": response.text}
    return value if isinstance(value, dict) else {"result": value}


def _sandbox_bridge_shim(slug: str) -> str:
    """Bridge a sandboxed historical artifact through its owning Calliope page.

    The iframe intentionally omits allow-same-origin, so direct cookie-bearing
    fetches are unavailable and the artifact cannot reach into the notebook
    DOM. Query calls and document-height reports cross the same narrow
    postMessage bridge; the parent validates event.source against a rendered
    artifact frame before acting on either.
    """
    return (
        "<script>\n"
        "(()=>{let n=0,heightTimer=0,lastHeight=0,lastWidth=innerWidth;"
        "const waiting=new Map();"
        "addEventListener('message',e=>{const d=e.data||{};"
        "if(d.type==='calliope.artifact.measure'){lastHeight=0;reportHeight();return;}"
        "if(d.type!=='calliope.query.result'||!waiting.has(d.id))return;"
        "const p=waiting.get(d.id);waiting.delete(d.id);"
        "d.error?p.reject(new Error(d.error)):p.resolve(d.result);});"
        "function documentHeight(){const d=document.documentElement,b=document.body;"
        "return Math.ceil(Math.max(d?d.scrollHeight:0,d?d.offsetHeight:0,"
        "b?b.scrollHeight:0,b?b.offsetHeight:0));}"
        "function reportHeight(){clearTimeout(heightTimer);heightTimer=setTimeout(()=>{"
        "requestAnimationFrame(()=>{const height=documentHeight();"
        "if(height>0&&Math.abs(height-lastHeight)>1){lastHeight=height;"
        "parent.postMessage({type:'calliope.artifact.resize',height},'*');}});},60);}"
        "addEventListener('DOMContentLoaded',reportHeight);"
        "addEventListener('load',reportHeight);"
        "addEventListener('resize',()=>{if(Math.abs(innerWidth-lastWidth)>1){"
        "lastWidth=innerWidth;lastHeight=0;reportHeight();}});"
        "new MutationObserver(reportHeight).observe(document.documentElement,"
        "{childList:true,subtree:true,characterData:true});"
        "if(document.fonts&&document.fonts.ready)document.fonts.ready.then(reportHeight);"
        "[0,300,1000,3000].forEach(delay=>setTimeout(reportHeight,delay));"
        "function relay(kind,payload){return new Promise((resolve,reject)=>{"
        "const id='cq_'+Date.now().toString(36)+'_'+(++n).toString(36);"
        "waiting.set(id,{resolve,reject});"
        "parent.postMessage(Object.assign({type:'calliope.query',id,kind},payload),'*');"
        "setTimeout(()=>{if(waiting.delete(id))reject(new Error('Calliope data bridge timed out'));},60000);"
        "});}"
        f"window.RVBBIT_DASHBOARD={{slug:{json.dumps(slug)},historical:true}};"
        "window.rvbbitQuery=(sql,opts)=>relay('single',{sql,opts:opts||{}});"
        "window.cowork=window.cowork||{};"
        "window.cowork.callMcpTool=async(tool,args)=>{args=args||{};"
        "if(String(tool).endsWith('run_sql_multi')){"
        "const data=await relay('multi',{queries:args.queries||{},opts:{as_of:args.as_of||null}});"
        "return{structuredContent:data};}"
        "if(String(tool).endsWith('run_sql')){"
        "const data=await relay('single',{sql:args.sql||'',opts:{as_of:args.as_of||null}});"
        "return{structuredContent:data};}"
        "throw new Error('Unsupported artifact bridge tool: '+tool);};"
        "})();\n"
        "</script>\n"
    )


def _session_for_owner(
    conn_factory: Callable[..., Any],
    local_id: str,
    owner: str,
) -> dict[str, Any] | None:
    sid = _uuid(local_id)
    if not sid:
        return None
    with conn_factory() as conn:
        row = conn.execute(
            "SELECT * FROM rvbbit.calliope_sessions WHERE id=%s::uuid AND owner_email=%s",
            (sid, owner),
        ).fetchone()
    return dict(row) if row else None


def _compact_surface_context(
    conn_factory: Callable[..., Any],
    session_id: str,
    selected_surface_id: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    with conn_factory() as conn:
        rows = conn.execute(
            "SELECT id, kind, title, artifact_slug, artifact_version, lineage_key, "
            "source, created_at FROM rvbbit.calliope_surfaces "
            "WHERE session_id=%s::uuid ORDER BY created_at DESC LIMIT 24",
            (session_id,),
        ).fetchall()
    compact = []
    selected = None
    for row in rows:
        source = row.get("source") or {}
        item = {
            "surface_id": str(row["id"]),
            "kind": row["kind"],
            "title": row["title"],
            "artifact": (
                f"{row['artifact_slug']}@v{row['artifact_version']}"
                if row.get("artifact_slug") and row.get("artifact_version")
                else row.get("artifact_slug")
            ),
            "sql": str(source.get("sql") or "")[:900] or None,
            "created_at": _now_iso(row.get("created_at")),
        }
        compact.append({k: v for k, v in item.items() if v is not None})
        if selected_surface_id and str(row["id"]) == selected_surface_id:
            selected = compact[-1]
    return compact, selected


def _instructions(surfaces: list[dict[str, Any]], selected: dict[str, Any] | None) -> str:
    state = json.dumps(
        {"selected_surface": selected, "recent_surfaces": surfaces},
        separators=(",", ":"),
        default=str,
    )
    return (
        "You are Calliope, the company warehouse's visual business collaborator. "
        "You are running inside the Calliope notebook, not a terminal chat. Use the configured "
        "RVBBIT warehouse MCP tools whenever data, metrics, queries, dashboards, apps, decks, "
        "documents, or captures would make the answer tangible. Actual tool results are rendered "
        "as separate surfaces automatically, so keep the prose concise: explain the decision, "
        "what you placed on the stage, and the most useful next move. Never paste full rowsets or "
        "whole HTML documents into the reply. Never include base64 data, data URLs, or Markdown "
        "images in the reply; captures already appear as image surfaces on the stage. Prefer "
        "governed run_sql/run_sql_multi for data; use "
        "create_live_app/update_live_app or the compatible publish/update dashboard tools for "
        "durable composed work. VISUAL SELF-CHECK: after creating or restyling a visual app, "
        "dashboard, or deck, end that build pass with capture_live_app(width=1200,height=800,"
        "full_page=false,return_image=false). Calliope will automatically send the saved screenshot "
        "back as the next private image turn. Do not call vision_analyze on the saved path and do not "
        "request return_image=true in that same pass; wait for the notebook's image turn. Inspect the "
        "actual rendering; if it needs adjustment, "
        "make one focused fix and capture again. If it is good, simply finish. At most two screenshots "
        "are fed back per user request, so do not keep iterating or ask the user to relay the image. "
        "When the user refers to 'this', 'that', or an older version, use "
        "the selected surface first, then the recent surface ledger. An update must preserve prior "
        "history: create a new artifact version rather than claiming the old surface changed. "
        "Your shared Hermes memory is the company brain; the surface ledger below is fresh UI state "
        "for this turn only.\n\nCALLIOPE_SURFACE_STATE=" + state
    )


def _sanitize_assistant_text(value: Any) -> str:
    """Keep binary/image payloads out of prose history and the browser DOM."""
    text = str(value or "")
    text = _MARKDOWN_DATA_IMAGE_RE.sub("[Image placed on the stage.]", text)
    text = _INLINE_DATA_IMAGE_RE.sub("[Image placed on the stage.]", text)
    if len(text) > _MAX_ASSISTANT_CHARS:
        text = text[:_MAX_ASSISTANT_CHARS].rstrip() + "\n\n[Response shortened for the notebook.]"
    return text


def _extract_json(value: Any) -> Any:
    """Unwrap Hermes/FastMCP text and structured-content wrappers."""
    current = value
    for _ in range(7):
        if isinstance(current, str):
            stripped = current.strip()
            if not stripped:
                return ""
            # Hermes deliberately fences external MCP output so the model
            # treats it as data. The projector is not executing that content;
            # it only recovers the JSON payload beneath the fixed warning.
            if stripped.startswith("<untrusted_tool_result"):
                opening_end = stripped.find(">")
                closing_start = stripped.rfind("</untrusted_tool_result>")
                if opening_end >= 0 and closing_start > opening_end:
                    inner = stripped[opening_end + 1:closing_start].strip()
                    # Current Hermes separates the warning and payload with a
                    # blank line. Keep a fallback for older one-block output.
                    current = inner.split("\n\n", 1)[-1].strip()
                    continue
            try:
                current = json.loads(stripped)
                continue
            except Exception:
                return current
        if isinstance(current, list):
            text_parts = [
                part.get("text")
                for part in current
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            if text_parts:
                current = "\n".join(text_parts)
                continue
            return current
        if not isinstance(current, dict):
            return current
        structured = current.get("structuredContent")
        if isinstance(structured, (dict, list)) and structured:
            current = structured
            continue
        # Hermes's MCP adapter commonly returns {"result": <tool result>}.
        if "result" in current and (
            len(current) == 1
            or set(current).issubset({"result", "content", "structuredContent", "isError"})
        ):
            current = current["result"]
            continue
        return current
    return current


def _canonical_tool(name: Any) -> str | None:
    value = str(name or "")
    for tool in sorted(_KNOWN_TOOLS, key=len, reverse=True):
        if value == tool or value.endswith("_" + tool):
            return tool
    return None


def _parse_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _sql_title(sql: str, fallback: str = "Query result") -> str:
    compact = re.sub(r"\s+", " ", str(sql or "")).strip()
    match = re.search(r"\bfrom\s+([a-zA-Z0-9_.\"]+)", compact, re.I)
    if match:
        return f"Query · {match.group(1).replace(chr(34), '')}"[:120]
    return compact[:70] + ("…" if len(compact) > 70 else "") if compact else fallback


_METADATA_RELATION_RE = re.compile(
    r"""
    \b(?:from|join)\s+
    (?:(?:"?pg_catalog"?)\s*\.\s*)?
    "?pg_(?:
        aggregate|am|amop|amproc|attrdef|attribute|auth_members|available_extension_versions|
        available_extensions|cast|class|collation|constraint|conversion|database|depend|
        description|enum|event_trigger|extension|foreign_data_wrapper|foreign_server|
        foreign_table|index|indexes|inherit|language|locks|matviews|namespace|opclass|
        operator|opfamily|partitioned_table|policies|policy|proc|publication|range|
        replication_slots|rewrite|roles|rules|seclabel|sequence|sequences|settings|
        shadow|shdepend|shdescription|stat(?:istic|_.*)?|tables|tablespace|transform|
        trigger|ts_.*|type|user|user_mapping|views
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_METADATA_FUNCTION_RE = re.compile(
    r"\b(?:to_reg(?:class|namespace|operator|proc|procedure|type)|"
    r"has_(?:any_column|column|database|foreign_data_wrapper|function|language|"
    r"parameter|schema|sequence|server|table|tablespace|type)_privilege)\s*\(",
    re.IGNORECASE,
)


def _is_metadata_sql(sql: str) -> bool:
    """Classify obvious catalog introspection without hiding business queries.

    The browser keeps these calls in the temporal ledger for provenance, but
    presents them as collapsed metadata strips instead of primary artifacts.
    """
    compact = re.sub(r"\s+", " ", str(sql or "")).strip()
    if not compact:
        return False
    return bool(
        re.search(r'\b(?:"?information_schema"?)\s*\.', compact, re.IGNORECASE)
        or re.search(r'\b(?:"?pg_catalog"?)\s*\.', compact, re.IGNORECASE)
        or _METADATA_RELATION_RE.search(compact)
        or _METADATA_FUNCTION_RE.search(compact)
    )


def _hash_key(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _query_surface(
    result: Any,
    args: dict[str, Any],
    tool_call_id: str,
    title_hint: str | None = None,
    suffix: str = "",
) -> dict[str, Any] | None:
    result = _extract_json(result)
    if not isinstance(result, dict) or result.get("error"):
        return None
    rows = result.get("rows")
    columns = result.get("columns")
    if not isinstance(rows, list) or not isinstance(columns, list):
        return None
    sql = str(args.get("sql") or result.get("sql") or "")
    title = title_hint or _sql_title(sql)
    normalized_sql = re.sub(r"\s+", " ", sql).strip().lower()
    metadata_query = _is_metadata_sql(sql)
    # A named run_sql_multi result is its own lineage even when two names use
    # identical SQL. The suffix also keeps the idempotency key from collapsing
    # those sibling surfaces into one row.
    lineage = _hash_key("query", (normalized_sql + suffix) or tool_call_id + suffix)
    return {
        "kind": "query",
        "title": title,
        "lineage_key": lineage,
        "payload": {
            "columns": columns,
            "rows": rows,
            "row_count": result.get("row_count", len(rows)),
            "truncated": bool(result.get("truncated")),
            "engine": result.get("engine"),
            "elapsed_ms": result.get("elapsed_ms"),
            "as_of_applied": result.get("as_of_applied"),
            "metadata_query": metadata_query,
        },
        "source": {"sql": sql, "args": args},
    }


def _project_tool_result(
    tool: str,
    result: Any,
    args: dict[str, Any],
    tool_call_id: str,
) -> list[dict[str, Any]]:
    value = _extract_json(result)
    if isinstance(value, dict) and value.get("error"):
        return []
    if tool == "run_sql":
        surface = _query_surface(value, args, tool_call_id)
        return [surface] if surface else []
    if tool == "run_sql_multi":
        if not isinstance(value, dict):
            return []
        results = value.get("results")
        queries = args.get("queries") if isinstance(args.get("queries"), dict) else {}
        out = []
        if isinstance(results, dict):
            for index, (name, query_result) in enumerate(results.items()):
                surface = _query_surface(
                    query_result,
                    {"sql": queries.get(name, ""), "batch": args},
                    tool_call_id,
                    str(name).replace("_", " ").strip().title() or f"Query {index + 1}",
                    f":{name}",
                )
                if surface:
                    out.append(surface)
        return out
    if tool == "metric" and isinstance(value, dict):
        name = str(value.get("name") or args.get("name") or "Metric")
        return [{
            "kind": "metric",
            "title": name.replace("_", " ").title(),
            "lineage_key": f"metric:{name.lower()}",
            "payload": value,
            "source": {"args": args},
        }]
    if tool in _ARTIFACT_TOOLS and isinstance(value, dict) and value.get("slug"):
        slug = str(value["slug"])
        version = value.get("version")
        app_kind = str(
            value.get("app_kind")
            or ("dashboard" if "dashboard" in tool else args.get("app_kind") or "app")
        )
        name = str(args.get("name") or slug).replace("-", " ")
        return [{
            "kind": "artifact",
            "title": name,
            "lineage_key": f"artifact:{slug}",
            "artifact_slug": slug,
            "artifact_version": int(version) if str(version or "").isdigit() else None,
            "payload": {
                **value,
                "app_kind": app_kind,
                "display_url": (
                    f"/calliope/artifacts/{quote(slug, safe='')}/versions/{int(version)}"
                    if str(version or "").isdigit()
                    else value.get("url")
                ),
            },
            "source": {"args": args},
        }]
    if tool == "capture_live_app" and isinstance(value, dict) and value.get("slug"):
        slug = str(value["slug"])
        version = value.get("version")
        return [{
            "kind": "image",
            "title": f"Capture · {slug}",
            "lineage_key": f"capture:{slug}",
            "artifact_slug": slug,
            "artifact_version": int(version) if str(version or "").isdigit() else None,
            "payload": value,
            "source": {"args": args},
        }]
    if tool == "render_pdf" and isinstance(value, dict) and value.get("path"):
        name = str(value.get("name") or "Document")
        return [{
            "kind": "document",
            "title": name.replace("-", " "),
            "lineage_key": f"document:{name}",
            "payload": value,
            "source": {"args": args},
        }]
    return []


def project_messages(messages: Any) -> list[dict[str, Any]]:
    """Project one Hermes turn transcript into deterministic visual surfaces."""
    if not isinstance(messages, list):
        return []
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            call_id = str(call.get("id") or call.get("call_id") or "")
            if not call_id:
                continue
            name = function.get("name") or call.get("name")
            args = _parse_args(function.get("arguments") or call.get("arguments"))
            # Hermes keeps large/deferred MCP schemas out of the model prompt
            # and invokes them through tool_call(name, arguments). Recover the
            # actual MCP identity so projection is identical to a direct call.
            if name == "tool_call" and isinstance(args.get("arguments"), dict):
                name = args.get("name") or name
                args = args["arguments"]
            calls[call_id] = {
                "name": name,
                "args": args,
            }

    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or message.get("call_id") or "")
        call = calls.get(call_id, {})
        raw_name = message.get("tool_name") or call.get("name")
        tool = _canonical_tool(raw_name)
        if not tool:
            continue
        for surface in _project_tool_result(
            tool,
            message.get("content"),
            call.get("args") or {},
            call_id or f"anonymous-{len(projected)}",
        ):
            surface["tool_name"] = str(raw_name or tool)
            surface["tool_call_id"] = call_id or f"anonymous-{len(projected)}"
            projected.append(surface)
    return projected


def _insert_surfaces(
    conn_factory: Callable[..., Any],
    session_id: str,
    turn_id: str,
    projected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    inserted = []
    with conn_factory() as conn:
        with conn.transaction():
            current = conn.execute(
                "SELECT coalesce(max(ordinal),0)::int AS n "
                "FROM rvbbit.calliope_surfaces WHERE turn_id=%s::uuid",
                (turn_id,),
            ).fetchone()
            first_ordinal = int(current["n"]) + 1
            for ordinal, surface in enumerate(projected, start=first_ordinal):
                lineage = surface["lineage_key"]
                explicit_parent = _uuid(surface.get("parent_surface_id"))
                if explicit_parent:
                    parent = conn.execute(
                        "SELECT id FROM rvbbit.calliope_surfaces "
                        "WHERE id=%s::uuid AND session_id=%s::uuid",
                        (explicit_parent, session_id),
                    ).fetchone()
                else:
                    parent = conn.execute(
                        "SELECT id FROM rvbbit.calliope_surfaces "
                        "WHERE session_id=%s::uuid AND lineage_key=%s "
                        "ORDER BY created_at DESC, ordinal DESC LIMIT 1",
                        (session_id, lineage),
                    ).fetchone()
                sid = str(uuid.uuid4())
                row = conn.execute(
                    "INSERT INTO rvbbit.calliope_surfaces "
                    "(id,session_id,turn_id,ordinal,kind,title,tool_name,tool_call_id,"
                    " lineage_key,parent_surface_id,artifact_slug,artifact_version,payload,source) "
                    "VALUES (%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s::uuid,%s,%s,"
                    " %s::jsonb,%s::jsonb) "
                    "ON CONFLICT (session_id,tool_call_id,lineage_key) DO NOTHING "
                    "RETURNING *",
                    (
                        sid,
                        session_id,
                        turn_id,
                        ordinal,
                        surface["kind"],
                        str(surface["title"])[:240],
                        surface["tool_name"],
                        surface["tool_call_id"],
                        lineage,
                        str(parent["id"]) if parent else None,
                        surface.get("artifact_slug"),
                        surface.get("artifact_version"),
                        json.dumps(surface.get("payload") or {}, default=str),
                        json.dumps(surface.get("source") or {}, default=str),
                    ),
                ).fetchone()
                if row:
                    inserted.append(_surface_json(row))
    return inserted


def _surface_json(row: Any) -> dict[str, Any]:
    item = _row_json(row)
    for key in ("id", "session_id", "turn_id", "parent_surface_id"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    payload = dict(item.get("payload") or {})
    if item.get("kind") == "image":
        # Capture and markup projections can contain server-only paths or
        # attachment row ids. Keep those in the ledger for exact replay, but
        # expose only owner-gated URLs to the browser.
        payload.pop("path", None)
        attachment_id = _uuid(payload.pop("attachment_id", None))
        overlay_id = _uuid(payload.pop("overlay_attachment_id", None))
        source_id = _uuid(payload.get("source_surface_id"))
        payload["image_url"] = (
            f"/api/calliope/surfaces/{quote(str(item['id']), safe='')}/image"
        )
        if attachment_id and source_id:
            payload["base_image_url"] = (
                f"/api/calliope/surfaces/{quote(source_id, safe='')}/image"
            )
        if overlay_id:
            payload["overlay_image_url"] = (
                f"/api/calliope/attachments/{quote(overlay_id, safe='')}"
            )
    item["payload"] = payload
    return item


def _turn_json(row: Any) -> dict[str, Any]:
    item = _row_json(row)
    for key in ("id", "session_id", "selected_surface_id"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    item["attachments"] = item.get("attachments") or []
    return item


def _session_json(row: Any) -> dict[str, Any]:
    item = _row_json(row)
    item["id"] = str(item["id"])
    return item


def _decode_attachments(
    attachments: Any,
    config: CalliopeConfig,
) -> list[dict[str, Any]]:
    if attachments is None:
        return []
    if not isinstance(attachments, list) or len(attachments) > 4:
        raise ValueError("attachments must be a list of at most 4 images")
    decoded = []
    total = 0
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("each attachment must be an object")
        data_url = str(item.get("data_url") or "")
        match = _DATA_IMAGE_RE.fullmatch(data_url)
        if not match:
            raise ValueError("attachments must be PNG, JPEG, WebP, or GIF data URLs")
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise ValueError("attachment contains invalid base64") from exc
        total += len(raw)
        if not raw or len(raw) > config.max_image_bytes or total > config.max_image_bytes:
            raise ValueError(
                f"image attachments exceed the {config.max_image_bytes // (1024 * 1024)} MB limit"
            )
        name = re.sub(r"[\x00-\x1f/\\]+", "-", str(item.get("name") or "image"))[:160]
        decoded.append({
            "name": name,
            "mime": match.group(1).lower(),
            "raw": raw,
            "data_url": data_url,
        })
        annotation = item.get("annotation")
        if annotation is not None:
            if not isinstance(annotation, dict):
                raise ValueError("attachment annotation must be an object")
            source_surface_id = _uuid(annotation.get("source_surface_id"))
            if not source_surface_id:
                raise ValueError("attachment annotation needs a valid source surface")
            overlay_data_url = str(annotation.get("overlay_data_url") or "")
            overlay_match = _DATA_IMAGE_RE.fullmatch(overlay_data_url)
            if not overlay_match or overlay_match.group(1).lower() != "image/png":
                raise ValueError("attachment markup overlay must be a PNG data URL")
            try:
                overlay_raw = base64.b64decode(overlay_match.group(2), validate=True)
            except Exception as exc:
                raise ValueError("attachment markup overlay contains invalid base64") from exc
            total += len(overlay_raw)
            if not overlay_raw or len(overlay_raw) > config.max_image_bytes or total > config.max_image_bytes:
                raise ValueError(
                    f"image attachments exceed the {config.max_image_bytes // (1024 * 1024)} MB limit"
                )
            try:
                width = max(1, min(int(annotation.get("width") or item.get("width") or 1), 8000))
                height = max(1, min(int(annotation.get("height") or item.get("height") or 1), 16000))
            except (TypeError, ValueError):
                raise ValueError("attachment annotation dimensions must be integers")
            decoded[-1]["annotation"] = {
                "source_surface_id": source_surface_id,
                "overlay_mime": "image/png",
                "overlay_raw": overlay_raw,
                "overlay_data_url": overlay_data_url,
                "width": width,
                "height": height,
            }
    return decoded


def _annotation_sources(
    conn_factory: Callable[..., Any],
    session_id: str,
    decoded: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    source_ids = {
        item["annotation"]["source_surface_id"]
        for item in decoded
        if isinstance(item.get("annotation"), dict)
    }
    if not source_ids:
        return {}
    found: dict[str, dict[str, Any]] = {}
    with conn_factory() as conn:
        for source_id in source_ids:
            row = conn.execute(
                "SELECT id,title,lineage_key,artifact_slug,artifact_version "
                "FROM rvbbit.calliope_surfaces "
                "WHERE id=%s::uuid AND session_id=%s::uuid AND kind='image'",
                (source_id, session_id),
            ).fetchone()
            if row:
                found[source_id] = dict(row)
    missing = source_ids - set(found)
    if missing:
        raise ValueError("attachment markup source is not available in this session")
    return found


def _persist_attachments(
    conn_factory: Callable[..., Any],
    config: CalliopeConfig,
    session_id: str,
    turn_id: str,
    decoded: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not decoded:
        return []
    folder = config.file_root / "attachments" / session_id
    folder.mkdir(parents=True, exist_ok=True)
    stored = []
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
    with conn_factory() as conn:
        with conn.transaction():
            for item in decoded:
                aid = str(uuid.uuid4())
                path = folder / f"{aid}{extensions[item['mime']]}"
                path.write_bytes(item["raw"])
                conn.execute(
                    "INSERT INTO rvbbit.calliope_attachments "
                    "(id,session_id,turn_id,original_name,mime_type,storage_path,bytes) "
                    "VALUES (%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s)",
                    (
                        aid,
                        session_id,
                        turn_id,
                        item["name"],
                        item["mime"],
                        str(path),
                        len(item["raw"]),
                    ),
                )
                stored_item = {
                    "id": aid,
                    "name": item["name"],
                    "mime_type": item["mime"],
                    "bytes": len(item["raw"]),
                    "url": f"/api/calliope/attachments/{aid}",
                }
                item["attachment_id"] = aid
                annotation = item.get("annotation")
                if isinstance(annotation, dict):
                    overlay_id = str(uuid.uuid4())
                    overlay_path = folder / f"{overlay_id}.png"
                    overlay_path.write_bytes(annotation["overlay_raw"])
                    conn.execute(
                        "INSERT INTO rvbbit.calliope_attachments "
                        "(id,session_id,turn_id,original_name,mime_type,storage_path,bytes) "
                        "VALUES (%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s)",
                        (
                            overlay_id,
                            session_id,
                            turn_id,
                            f"{item['name']} · overlay.png",
                            "image/png",
                            str(overlay_path),
                            len(annotation["overlay_raw"]),
                        ),
                    )
                    annotation["overlay_attachment_id"] = overlay_id
                    stored_item["annotation"] = {
                        "source_surface_id": annotation["source_surface_id"],
                        "overlay_url": f"/api/calliope/attachments/{overlay_id}",
                        "width": annotation["width"],
                        "height": annotation["height"],
                    }
                stored.append(stored_item)
            conn.execute(
                "UPDATE rvbbit.calliope_turns SET attachments=%s::jsonb WHERE id=%s::uuid",
                (json.dumps(stored), turn_id),
            )
    return stored


def _annotation_surface_projections(
    decoded: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    projected = []
    for item in decoded:
        annotation = item.get("annotation")
        if not isinstance(annotation, dict):
            continue
        source_id = annotation["source_surface_id"]
        source = sources.get(source_id)
        attachment_id = _uuid(item.get("attachment_id"))
        overlay_id = _uuid(annotation.get("overlay_attachment_id"))
        if not source or not attachment_id or not overlay_id:
            continue
        source_title = str(source.get("title") or "Image")
        projected.append({
            "kind": "image",
            "title": f"Markup · {source_title}"[:240],
            "tool_name": "calliope_markup",
            "tool_call_id": f"markup:{attachment_id}",
            "lineage_key": str(source["lineage_key"]),
            "parent_surface_id": source_id,
            "artifact_slug": source.get("artifact_slug"),
            "artifact_version": source.get("artifact_version"),
            "payload": {
                "attachment_id": attachment_id,
                "overlay_attachment_id": overlay_id,
                "source_surface_id": source_id,
                "width": annotation["width"],
                "height": annotation["height"],
                "annotated": True,
            },
            "source": {
                "source_surface_id": source_id,
                "input": "user_markup",
            },
        })
    return projected


def _capture_feedback_message(
    projected: list[dict[str, Any]],
    inserted: list[dict[str, Any]],
    config: CalliopeConfig,
    feedback_number: int,
) -> list[dict[str, Any]] | None:
    inserted_calls = {
        str(surface.get("tool_call_id") or "")
        for surface in inserted
        if surface.get("kind") == "image"
    }
    if not inserted_calls:
        return None
    candidates = [
        surface for surface in projected
        if surface.get("kind") == "image"
        and str(surface.get("tool_call_id") or "") in inserted_calls
    ]
    if not candidates:
        return None
    capture = candidates[-1]
    payload = capture.get("payload") or {}
    path_value = payload.get("path") if isinstance(payload, dict) else None
    if not path_value:
        return None
    path = Path(path_value).resolve()
    capture_root = Path(
        os.environ.get(
            "WAREHOUSE_LIVE_APP_CAPTURE_DIR",
            str(Path(tempfile.gettempdir()) / "rvbbit-live-app-captures"),
        )
    ).resolve()
    try:
        path.relative_to(capture_root)
    except ValueError:
        return None
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(path.suffix.lower())
    if not mime or not path.is_file():
        return None
    raw = path.read_bytes()
    if not raw or len(raw) > config.max_image_bytes:
        return None
    slug = str(payload.get("slug") or capture.get("artifact_slug") or "visual artifact")
    return [
        {
            "type": "text",
            "text": (
                f"[visual self-check {feedback_number}/{_VISUAL_FEEDBACK_BUDGET}] "
                f"The actual rendered screenshot of {slug} is attached. Inspect layout, hierarchy, "
                "spacing, clipping, readability, and obvious data/rendering failures. If a focused "
                "fix is needed, update the artifact and capture it once more. If it is already good, "
                "finish without another capture."
            ),
        },
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(raw).decode()}",
                "detail": "high",
            },
        },
    ]


def _sse(event: str, data: Any) -> bytes:
    payload = json.dumps(data, default=str, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


async def _iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    event = "message"
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    data: Any = json.loads(raw)
                except Exception:
                    data = {"text": raw}
                yield event, data
            event, data_lines = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except Exception:
            data = {"text": raw}
        yield event, data


def _complete_turn(
    conn_factory: Callable[..., Any],
    turn_id: str,
    assistant_message: str,
    hermes_message_id: str | None,
    status: str = "complete",
    error: str | None = None,
) -> None:
    with conn_factory() as conn:
        conn.execute(
            "UPDATE rvbbit.calliope_turns SET assistant_message=%s,hermes_message_id=%s,"
            "status=%s,error=%s,completed_at=now() WHERE id=%s::uuid",
            (assistant_message, hermes_message_id, status, error, turn_id),
        )
        conn.execute(
            "UPDATE rvbbit.calliope_sessions SET updated_at=now() "
            "WHERE id=(SELECT session_id FROM rvbbit.calliope_turns WHERE id=%s::uuid)",
            (turn_id,),
        )


def register_calliope_routes(
    mcp: Any,
    conn_factory: Callable[..., Any],
    rabbit_svg: str,
    artifact_shim: Callable[[str], str],
) -> bool:
    """Register the optional Calliope routes. Returns whether it was enabled."""
    config = CalliopeConfig.from_env()
    if not config.enabled:
        return False

    import auth
    from starlette.responses import (
        FileResponse,
        HTMLResponse,
        RedirectResponse,
        Response,
        StreamingResponse,
    )

    ensure_tables(conn_factory)
    config.file_root.mkdir(parents=True, exist_ok=True)

    def json_response(value: Any, status: int = 200) -> Response:
        return Response(
            json.dumps(value, default=str),
            status_code=status,
            media_type="application/json",
            headers={"cache-control": "no-store"},
        )

    def api_owner(request: Any) -> tuple[str | None, Response | None]:
        owner, session = _canonical_owner(request)
        if not owner:
            return None, json_response({"error": {"code": "UNAUTHORIZED"}}, 401)
        if not session.get("mapped", True):
            return None, json_response({"error": {"code": "ACCESS_PENDING"}}, 403)
        return owner, None

    @mcp.custom_route("/calliope", methods=["GET"])
    async def calliope_page(request):
        owner, session = _canonical_owner(request)
        if not owner:
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=302)
        if not session.get("mapped", True):
            return RedirectResponse("/gallery", status_code=302)
        template = (_ASSET_DIR / "index.html").read_text(encoding="utf-8")
        background = auth.background_layer(
            0.62,
            "radial-gradient(1000px 700px at 58% -15%, rgba(32,67,64,.14), transparent 67%),"
            "linear-gradient(to bottom,rgba(16,13,11,.08),rgba(16,13,11,.46) 86%)",
        )
        html = (
            template.replace("__CALLIOPE_BACKGROUND__", background)
            .replace("__CALLIOPE_RABBIT__", rabbit_svg)
            .replace("__CALLIOPE_VIEWER__", escape(owner))
        )
        return HTMLResponse(
            html,
            headers={
                "cache-control": "no-store",
                "content-security-policy": (
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                    "font-src 'self' https://fonts.gstatic.com; "
                    "img-src 'self' data: blob:; "
                    "connect-src 'self'; frame-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'self'; form-action 'self'"
                ),
                "x-content-type-options": "nosniff",
            },
        )

    @mcp.custom_route("/calliope/calliope.css", methods=["GET"])
    async def calliope_css(request):
        owner, _ = _canonical_owner(request)
        if not owner:
            return Response(status_code=401)
        return FileResponse(_ASSET_DIR / "calliope.css", media_type="text/css")

    @mcp.custom_route("/calliope/calliope.js", methods=["GET"])
    async def calliope_js(request):
        owner, _ = _canonical_owner(request)
        if not owner:
            return Response(status_code=401)
        return FileResponse(_ASSET_DIR / "calliope.js", media_type="text/javascript")

    @mcp.custom_route("/calliope/thinking-orbs.js", methods=["GET"])
    async def calliope_thinking_orbs(request):
        owner, _ = _canonical_owner(request)
        if not owner:
            return Response(status_code=401)
        return FileResponse(_ASSET_DIR / "thinking-orbs.js", media_type="text/javascript")

    @mcp.custom_route("/calliope/callie-avatar-{period}.jpg", methods=["GET"])
    async def calliope_avatar(request):
        owner, _ = _canonical_owner(request)
        if not owner:
            return Response(status_code=401)
        period = request.path_params["period"]
        if period not in {"day", "night"}:
            return Response(status_code=404)
        return FileResponse(
            _ASSET_DIR / f"callie-avatar-{period}.jpg",
            media_type="image/jpeg",
            headers={
                "cache-control": "private, max-age=3600",
                "x-content-type-options": "nosniff",
            },
        )

    @mcp.custom_route("/api/calliope/config", methods=["GET"])
    async def calliope_config(request):
        _, err = api_owner(request)
        if err:
            return err
        healthy = False
        detail = None
        try:
            result = await _hermes_json(config, "GET", "/health")
            healthy = True
            detail = result
        except Exception as exc:
            detail = str(exc)[:240]
        return json_response({
            "enabled": True,
            "name": "Calliope",
            "healthy": healthy,
            "hermes": detail,
            "shared_memory": True,
            "max_image_bytes": config.max_image_bytes,
        })

    @mcp.custom_route("/api/calliope/sessions", methods=["GET"])
    async def list_sessions(request):
        owner, err = api_owner(request)
        if err:
            return err
        with conn_factory() as conn:
            rows = conn.execute(
                "SELECT s.*, count(DISTINCT t.id)::int AS turn_count,"
                " count(DISTINCT f.id)::int AS surface_count,"
                " max(f.created_at) AS last_surface_at "
                "FROM rvbbit.calliope_sessions s "
                "LEFT JOIN rvbbit.calliope_turns t ON t.session_id=s.id "
                "LEFT JOIN rvbbit.calliope_surfaces f ON f.session_id=s.id "
                "WHERE s.owner_email=%s AND NOT s.archived "
                "GROUP BY s.id ORDER BY s.updated_at DESC",
                (owner,),
            ).fetchall()
        return json_response({"sessions": [_session_json(row) for row in rows]})

    @mcp.custom_route("/api/calliope/sessions", methods=["POST"])
    async def create_session(request):
        owner, err = api_owner(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        title = re.sub(r"\s+", " ", str((body or {}).get("title") or "New inquiry")).strip()[:120]
        if not title:
            title = "New inquiry"
        local_id = str(uuid.uuid4())
        hermes_id = f"calliope_{int(time.time())}_{uuid.uuid4().hex[:10]}"
        try:
            await _hermes_json(
                config,
                "POST",
                "/api/sessions",
                {"id": hermes_id, "source": "api_server"},
            )
        except Exception as exc:
            return json_response(
                {"error": {"code": "HERMES_UNAVAILABLE", "message": str(exc)[:600]}},
                502,
            )
        try:
            with conn_factory() as conn:
                row = conn.execute(
                    "INSERT INTO rvbbit.calliope_sessions "
                    "(id,owner_email,hermes_session_id,title) "
                    "VALUES (%s::uuid,%s,%s,%s) RETURNING *",
                    (local_id, owner, hermes_id, title),
                ).fetchone()
        except Exception:
            try:
                await _hermes_json(config, "DELETE", f"/api/sessions/{quote(hermes_id, safe='')}")
            except Exception:
                pass
            raise
        return json_response({"session": _session_json(row)}, 201)

    @mcp.custom_route("/api/calliope/sessions/{session_id}", methods=["GET"])
    async def get_session(request):
        owner, err = api_owner(request)
        if err:
            return err
        session = _session_for_owner(conn_factory, request.path_params["session_id"], owner)
        if not session:
            return json_response({"error": {"code": "NOT_FOUND"}}, 404)
        with conn_factory() as conn:
            turns = conn.execute(
                "SELECT * FROM rvbbit.calliope_turns WHERE session_id=%s::uuid "
                "ORDER BY ordinal",
                (str(session["id"]),),
            ).fetchall()
            surfaces = conn.execute(
                "SELECT * FROM rvbbit.calliope_surfaces WHERE session_id=%s::uuid "
                "ORDER BY created_at DESC, ordinal DESC",
                (str(session["id"]),),
            ).fetchall()
        return json_response({
            "session": _session_json(session),
            "turns": [_turn_json(row) for row in turns],
            "surfaces": [_surface_json(row) for row in surfaces],
        })

    @mcp.custom_route("/api/calliope/sessions/{session_id}", methods=["PATCH"])
    async def patch_session(request):
        owner, err = api_owner(request)
        if err:
            return err
        session = _session_for_owner(conn_factory, request.path_params["session_id"], owner)
        if not session:
            return json_response({"error": {"code": "NOT_FOUND"}}, 404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        updates, values = [], []
        if "title" in body:
            title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:120]
            if not title:
                return json_response({"error": {"code": "INVALID_TITLE"}}, 400)
            updates.append("title=%s")
            values.append(title)
        if "archived" in body:
            updates.append("archived=%s")
            values.append(bool(body.get("archived")))
        if not updates:
            return json_response({"error": {"code": "NO_CHANGES"}}, 400)
        values.extend([str(session["id"]), owner])
        with conn_factory() as conn:
            row = conn.execute(
                f"UPDATE rvbbit.calliope_sessions SET {','.join(updates)},updated_at=now() "
                "WHERE id=%s::uuid AND owner_email=%s RETURNING *",
                values,
            ).fetchone()
        return json_response({"session": _session_json(row)})

    @mcp.custom_route("/api/calliope/attachments/{attachment_id}", methods=["GET"])
    async def get_attachment(request):
        owner, err = api_owner(request)
        if err:
            return err
        aid = _uuid(request.path_params["attachment_id"])
        if not aid:
            return Response(status_code=404)
        with conn_factory() as conn:
            row = conn.execute(
                "SELECT a.* FROM rvbbit.calliope_attachments a "
                "JOIN rvbbit.calliope_sessions s ON s.id=a.session_id "
                "WHERE a.id=%s::uuid AND s.owner_email=%s",
                (aid, owner),
            ).fetchone()
        if not row:
            return Response(status_code=404)
        path = Path(row["storage_path"]).resolve()
        try:
            path.relative_to(config.file_root.resolve())
        except ValueError:
            return Response(status_code=404)
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(
            path,
            media_type=row["mime_type"],
            filename=row.get("original_name") or path.name,
            content_disposition_type="inline",
            headers={
                "cache-control": "private, no-store",
                "x-content-type-options": "nosniff",
            },
        )

    @mcp.custom_route("/api/calliope/surfaces/{surface_id}/image", methods=["GET"])
    async def get_surface_image(request):
        owner, err = api_owner(request)
        if err:
            return err
        surface_id = _uuid(request.path_params["surface_id"])
        if not surface_id:
            return Response(status_code=404)
        with conn_factory() as conn:
            row = conn.execute(
                "SELECT f.session_id,f.payload "
                "FROM rvbbit.calliope_surfaces f "
                "JOIN rvbbit.calliope_sessions s ON s.id=f.session_id "
                "WHERE f.id=%s::uuid AND f.kind='image' AND s.owner_email=%s",
                (surface_id, owner),
            ).fetchone()
        if not row:
            return Response(status_code=404)
        payload = dict(row.get("payload") or {})
        attachment_id = _uuid(payload.get("attachment_id"))
        media_type = None
        allowed_root = None
        path = None
        if attachment_id:
            with conn_factory() as conn:
                attachment = conn.execute(
                    "SELECT storage_path,mime_type FROM rvbbit.calliope_attachments "
                    "WHERE id=%s::uuid AND session_id=%s::uuid",
                    (attachment_id, str(row["session_id"])),
                ).fetchone()
            if attachment:
                path = Path(attachment["storage_path"]).resolve()
                media_type = str(attachment["mime_type"])
                allowed_root = config.file_root.resolve()
        else:
            capture_path = payload.get("path")
            if capture_path:
                path = Path(capture_path).resolve()
                allowed_root = Path(
                    os.environ.get(
                        "WAREHOUSE_LIVE_APP_CAPTURE_DIR",
                        str(Path(tempfile.gettempdir()) / "rvbbit-live-app-captures"),
                    )
                ).resolve()
                media_type = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                    ".gif": "image/gif",
                }.get(path.suffix.lower())
        if not path or not allowed_root or not media_type:
            return Response(status_code=404)
        try:
            path.relative_to(allowed_root)
        except ValueError:
            return Response(status_code=404)
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "cache-control": "private, no-store",
                "x-content-type-options": "nosniff",
            },
        )

    @mcp.custom_route(
        "/calliope/artifacts/{slug}/versions/{version}",
        methods=["GET"],
    )
    async def artifact_version(request):
        owner, session = _canonical_owner(request)
        if not owner:
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=302)
        if not session.get("mapped", True):
            return RedirectResponse("/gallery", status_code=302)
        slug = request.path_params["slug"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", slug, re.I):
            return HTMLResponse("<h1>404</h1>", status_code=404)
        try:
            version = int(request.path_params["version"])
        except (TypeError, ValueError):
            return HTMLResponse("<h1>404</h1>", status_code=404)
        with conn_factory() as conn:
            row = conn.execute(
                "SELECT v.html FROM rvbbit.dashboards d "
                "JOIN rvbbit.dashboard_versions v ON v.dashboard_id=d.id "
                "WHERE d.slug=%s AND v.version=%s",
                (slug, version),
            ).fetchone()
        if not row:
            return HTMLResponse("<h1>404 — no such artifact version</h1>", status_code=404)
        return HTMLResponse(
            artifact_shim(slug) + _sandbox_bridge_shim(slug) + (row["html"] or ""),
            headers={
                "cache-control": "no-store",
                "content-security-policy": (
                    "sandbox allow-scripts allow-forms allow-popups allow-downloads; "
                    "default-src 'self' data: blob: https:; "
                    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
                    "style-src 'self' 'unsafe-inline' https:; "
                    "img-src * data: blob:; connect-src 'self' https:; "
                    "object-src 'none'; base-uri 'none'"
                ),
                "x-content-type-options": "nosniff",
            },
        )

    @mcp.custom_route("/api/calliope/sessions/{session_id}/turn", methods=["POST"])
    async def run_turn(request):
        owner, err = api_owner(request)
        if err:
            return err
        session = _session_for_owner(conn_factory, request.path_params["session_id"], owner)
        if not session:
            return json_response({"error": {"code": "NOT_FOUND"}}, 404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        message = str((body or {}).get("message") or "").strip()
        if len(message) > 40_000:
            return json_response({"error": {"code": "MESSAGE_TOO_LONG"}}, 400)
        try:
            decoded = _decode_attachments((body or {}).get("attachments"), config)
        except ValueError as exc:
            return json_response({"error": {"code": "BAD_ATTACHMENT", "message": str(exc)}}, 400)
        if not message and not decoded:
            return json_response({"error": {"code": "EMPTY_MESSAGE"}}, 400)
        try:
            annotation_sources = _annotation_sources(
                conn_factory,
                str(session["id"]),
                decoded,
            )
        except ValueError as exc:
            return json_response({"error": {"code": "BAD_ATTACHMENT", "message": str(exc)}}, 400)

        selected_id = _uuid((body or {}).get("selected_surface_id"))
        if selected_id:
            with conn_factory() as conn:
                selected_ok = conn.execute(
                    "SELECT 1 FROM rvbbit.calliope_surfaces "
                    "WHERE id=%s::uuid AND session_id=%s::uuid",
                    (selected_id, str(session["id"])),
                ).fetchone()
            if not selected_ok:
                selected_id = None

        turn_id = str(uuid.uuid4())
        with conn_factory() as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT id FROM rvbbit.calliope_sessions WHERE id=%s::uuid FOR UPDATE",
                    (str(session["id"]),),
                )
                active = conn.execute(
                    "SELECT id FROM rvbbit.calliope_turns "
                    "WHERE session_id=%s::uuid AND status='running' LIMIT 1",
                    (str(session["id"]),),
                ).fetchone()
                if active:
                    return json_response(
                        {
                            "error": {
                                "code": "TURN_IN_PROGRESS",
                                "message": "Calliope is already working in this session",
                            }
                        },
                        409,
                    )
                next_ordinal = conn.execute(
                    "SELECT coalesce(max(ordinal),0)+1 AS n FROM rvbbit.calliope_turns "
                    "WHERE session_id=%s::uuid",
                    (str(session["id"]),),
                ).fetchone()["n"]
                conn.execute(
                    "INSERT INTO rvbbit.calliope_turns "
                    "(id,session_id,ordinal,user_message,selected_surface_id) "
                    "VALUES (%s::uuid,%s::uuid,%s,%s,%s::uuid)",
                    (
                        turn_id,
                        str(session["id"]),
                        next_ordinal,
                        message or "[Image]",
                        selected_id,
                    ),
                )
                conn.execute(
                    "UPDATE rvbbit.calliope_sessions SET updated_at=now() WHERE id=%s::uuid",
                    (str(session["id"]),),
                )

        try:
            stored_attachments = _persist_attachments(
                conn_factory,
                config,
                str(session["id"]),
                turn_id,
                decoded,
            )
        except Exception as exc:
            _complete_turn(conn_factory, turn_id, "", None, "failed", str(exc)[:600])
            return json_response({"error": {"code": "ATTACHMENT_STORE_FAILED"}}, 500)

        try:
            annotation_surfaces = _insert_surfaces(
                conn_factory,
                str(session["id"]),
                turn_id,
                _annotation_surface_projections(decoded, annotation_sources),
            )
        except Exception as exc:
            _complete_turn(conn_factory, turn_id, "", None, "failed", str(exc)[:600])
            return json_response({"error": {"code": "MARKUP_STORE_FAILED"}}, 500)

        compact, selected = _compact_surface_context(
            conn_factory,
            str(session["id"]),
            selected_id,
        )
        if decoded:
            hermes_message: Any = []
            if message:
                hermes_message.append({"type": "text", "text": message})
            hermes_message.extend({
                "type": "image_url",
                "image_url": {"url": item["data_url"], "detail": "high"},
            } for item in decoded)
        else:
            hermes_message = message

        async def stream() -> AsyncIterator[bytes]:
            assistant_text = ""
            hermes_message_id = None
            effective_hermes_id = str(session["hermes_session_id"])
            completed = False
            upstream_error = None
            turn_messages: list[dict[str, Any]] = []
            suppress_assistant_deltas = False
            delta_probe = ""
            yield _sse("calliope.turn.started", {
                "turn_id": turn_id,
                "ordinal": next_ordinal,
                "attachments": stored_attachments,
            })
            if annotation_surfaces:
                yield _sse("calliope.surfaces", {
                    "turn_id": turn_id,
                    "surfaces": annotation_surfaces,
                })
            timeout = httpx.Timeout(None, connect=10.0, write=45.0, pool=10.0)
            try:
                next_hermes_message = hermes_message
                feedback_count = 0
                inserted_surface_count = len(annotation_surfaces)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    for hop in range(_VISUAL_FEEDBACK_BUDGET + 1):
                        if hop:
                            assistant_text = ""
                            yield _sse("calliope.visual_check", {
                                "turn_id": turn_id,
                                "number": feedback_count,
                                "budget": _VISUAL_FEEDBACK_BUDGET,
                            })
                        completed = False
                        upstream_error = None
                        turn_messages = []
                        suppress_assistant_deltas = False
                        delta_probe = ""
                        hop_compact, hop_selected = (
                            (compact, selected)
                            if hop == 0
                            else _compact_surface_context(
                                conn_factory,
                                str(session["id"]),
                                selected_id,
                            )
                        )
                        url = _hermes_session_url(
                            config,
                            effective_hermes_id,
                            "/chat/stream",
                        )
                        async with client.stream(
                            "POST",
                            url,
                            headers=_hermes_headers(config),
                            json={
                                "message": next_hermes_message,
                                "instructions": _instructions(hop_compact, hop_selected),
                            },
                        ) as upstream:
                            if upstream.status_code >= 400:
                                raw = (await upstream.aread()).decode(errors="replace")[:900]
                                raise RuntimeError(
                                    f"Hermes turn failed ({upstream.status_code}): {raw}"
                                )
                            async for event, data in _iter_sse(upstream):
                                skip_forward = False
                                if isinstance(data, dict):
                                    if data.get("session_id"):
                                        effective_hermes_id = str(data["session_id"])
                                    if event == "assistant.delta":
                                        delta = str(data.get("delta") or "")
                                        delta_probe = (delta_probe + delta)[-96:]
                                        if (
                                            "data:image/" in delta_probe.lower()
                                            or re.search(r"!\[[^\]]{0,60}\]\($", delta_probe)
                                        ):
                                            suppress_assistant_deltas = True
                                        if not suppress_assistant_deltas:
                                            assistant_text = _sanitize_assistant_text(
                                                assistant_text + delta
                                            )
                                        else:
                                            skip_forward = True
                                    elif event == "assistant.completed":
                                        assistant_text = _sanitize_assistant_text(
                                            data.get("content") or assistant_text
                                        )
                                        data = {**data, "content": assistant_text}
                                        hermes_message_id = (
                                            data.get("message_id") or hermes_message_id
                                        )
                                    elif event == "run.completed":
                                        completed = True
                                        hermes_message_id = (
                                            data.get("message_id") or hermes_message_id
                                        )
                                        if isinstance(data.get("messages"), list):
                                            turn_messages = data["messages"]
                                        # The interleaved transcript can contain
                                        # rowsets, HTML, paths, and image bytes. It
                                        # is server-side projection input, never a
                                        # browser/chat event.
                                        skip_forward = True
                                    elif event == "tool.started":
                                        data = {
                                            "tool_name": str(
                                                data.get("tool_name") or "warehouse tool"
                                            ),
                                            "preview": str(data.get("preview") or "")[:240],
                                        }
                                    elif event == "tool.completed":
                                        data = {
                                            "tool_name": str(
                                                data.get("tool_name") or "warehouse tool"
                                            ),
                                            "call_id": str(data.get("call_id") or ""),
                                        }
                                    elif event == "error":
                                        upstream_error = str(
                                            data.get("message")
                                            or data.get("error")
                                            or "Hermes turn failed"
                                        )
                                        data = {"message": upstream_error[:900]}
                                    elif event not in {"assistant.delta", "assistant.completed"}:
                                        skip_forward = True
                                if not skip_forward:
                                    yield _sse(event, data)

                        if upstream_error:
                            raise RuntimeError(upstream_error)
                        projected = project_messages(turn_messages)
                        hop_surfaces = _insert_surfaces(
                            conn_factory,
                            str(session["id"]),
                            turn_id,
                            projected,
                        )
                        if hop_surfaces:
                            inserted_surface_count += len(hop_surfaces)
                            yield _sse("calliope.surfaces", {
                                "turn_id": turn_id,
                                "surfaces": hop_surfaces,
                            })
                        feedback = (
                            _capture_feedback_message(
                                projected,
                                hop_surfaces,
                                config,
                                feedback_count + 1,
                            )
                            if completed and feedback_count < _VISUAL_FEEDBACK_BUDGET
                            else None
                        )
                        if not feedback:
                            break
                        feedback_count += 1
                        next_hermes_message = feedback

                if effective_hermes_id != str(session["hermes_session_id"]):
                    with conn_factory() as conn:
                        conn.execute(
                            "UPDATE rvbbit.calliope_sessions SET hermes_session_id=%s "
                            "WHERE id=%s::uuid",
                            (effective_hermes_id, str(session["id"])),
                        )
                _complete_turn(
                    conn_factory,
                    turn_id,
                    _sanitize_assistant_text(assistant_text),
                    str(hermes_message_id) if hermes_message_id else None,
                    "complete" if completed else "partial",
                )
                yield _sse("calliope.turn.completed", {
                    "turn_id": turn_id,
                    "assistant_message": _sanitize_assistant_text(assistant_text),
                    "surface_count": inserted_surface_count,
                    "visual_checks": feedback_count,
                })
            except asyncio.CancelledError:
                _complete_turn(
                    conn_factory,
                    turn_id,
                    _sanitize_assistant_text(assistant_text),
                    str(hermes_message_id) if hermes_message_id else None,
                    "interrupted",
                    "browser disconnected",
                )
                raise
            except Exception as exc:
                message_text = str(exc)[:900]
                _complete_turn(
                    conn_factory,
                    turn_id,
                    _sanitize_assistant_text(assistant_text),
                    str(hermes_message_id) if hermes_message_id else None,
                    "failed",
                    message_text,
                )
                yield _sse("calliope.error", {
                    "turn_id": turn_id,
                    "message": message_text,
                })

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache, no-store",
                "x-accel-buffering": "no",
                "connection": "keep-alive",
            },
        )

    return True
