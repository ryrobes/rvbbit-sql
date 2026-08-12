from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from uuid import UUID

import psycopg
from fastapi import FastAPI, Header, HTTPException, Response
from psycopg.rows import dict_row
from pydantic import BaseModel, SecretStr
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import BindParameter

try:
    import dlt
    from dlt.common.schema import Schema
    from dlt.destinations.impl.postgres.factory import (
        postgres as DltPostgresDestination,
    )
    from dlt.destinations.impl.postgres.postgres import (
        PostgresClient as DltPostgresClient,
    )
    from dlt.sources.sql_database import sql_table
except ImportError:  # pragma: no cover - health reports this in a broken image
    dlt = None
    Schema = None
    DltPostgresDestination = None
    DltPostgresClient = None
    sql_table = None


log = logging.getLogger("rvbbit.dlt_mirror")
logging.basicConfig(level=os.environ.get("RVBBIT_MIRROR_LOG_LEVEL", "INFO"))

CONTROL_DSN = os.environ.get("RVBBIT_DSN", "")
# pg_rvbbit is database-bound: control metadata, dlt receipts, and mirrored
# schemas deliberately share this one extension-enabled RVBBIT database.
DESTINATION_DSN = CONTROL_DSN
WORKER_TOKEN = os.environ.get("RVBBIT_MIRROR_TOKEN", "")
WORKER_ID = os.environ.get("RVBBIT_MIRROR_WORKER_ID") or (
    f"{socket.gethostname()}:{os.getpid()}"
)
STATE_DIR = Path(os.environ.get("RVBBIT_MIRROR_STATE_DIR", "/var/lib/rvbbit/dlt"))
POLL_SECONDS = max(1.0, float(os.environ.get("RVBBIT_MIRROR_POLL_SECONDS", "5")))
HEARTBEAT_SECONDS = max(
    10.0, float(os.environ.get("RVBBIT_MIRROR_HEARTBEAT_SECONDS", "30"))
)
STALE_SECONDS = max(60, int(os.environ.get("RVBBIT_MIRROR_STALE_SECONDS", "1800")))
MAX_ATTEMPTS = max(1, int(os.environ.get("RVBBIT_MIRROR_MAX_ATTEMPTS", "5")))
MAX_DISCOVERY_TABLES = max(
    1, min(int(os.environ.get("RVBBIT_MIRROR_MAX_DISCOVERY_TABLES", "500")), 5000)
)

# dlt's merge/upsert implementation uses a technical staging dataset. Keep it
# empty after successful loads and clearly outside the canonical mirror schema.
os.environ.setdefault("LOAD__TRUNCATE_STAGING_DATASET", "true")

_SECRET_QUERY_KEYS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "access_token",
    "secret",
    "api_key",
    "key",
}
_DIALECT_ALIASES = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "mysql": "mysql",
    "mariadb": "mariadb",
    "mssql": "mssql",
    "oracle": "oracle",
    "db2": "db2",
    "ibm_db_sa": "db2",
}
_DRIVERS = {
    "postgresql": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "mssql": "mssql+pymssql",
    "oracle": "oracle+oracledb",
    "db2": "db2+ibm_db",
}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,62}$")
_destination_naming = Schema("rvbbit_mirror").naming if Schema else None
_runner_task: asyncio.Task[None] | None = None
_stop_event: asyncio.Event | None = None
_catalog_queue: Queue[str | None] = Queue()
_catalog_thread: threading.Thread | None = None


def _rvbbit_table_update_sql(
    statements: list[str],
    *,
    generate_alter: bool,
    in_staging_dataset: bool,
    is_dlt_table: bool,
) -> list[str]:
    """Create mirrored user relations through the RVBBIT registration alias."""
    if generate_alter or in_staging_dataset or is_dlt_table or not statements:
        return statements
    updated = list(statements)
    updated[0] = f"{updated[0]} USING rvbbit"
    return updated


if DltPostgresClient is not None and DltPostgresDestination is not None:

    class RvbbitPostgresClient(DltPostgresClient):
        def _get_table_update_sql(
            self, table_name: str, new_columns: Any, generate_alter: bool
        ) -> list[str]:
            statements = super()._get_table_update_sql(
                table_name, new_columns, generate_alter
            )
            return _rvbbit_table_update_sql(
                statements,
                generate_alter=generate_alter,
                in_staging_dataset=self.in_staging_dataset_mode,
                is_dlt_table=table_name in self.schema.dlt_table_names(),
            )


    class RvbbitPostgresDestination(DltPostgresDestination):
        @property
        def client_class(self) -> Any:
            return RvbbitPostgresClient

        @property
        def destination_type(self) -> str:
            # Preserve dlt's durable pipeline identity across this client-only
            # DDL specialization.
            return "dlt.destinations.postgres"

else:  # pragma: no cover - only used by the dependency-missing health path
    RvbbitPostgresClient = None
    RvbbitPostgresDestination = None


class CredentialRequest(BaseModel):
    source_dsn: SecretStr


class DiscoverRequest(BaseModel):
    source_schema: str | None = None
    include_views: bool = False
    limit: int = 200


class QueueRequest(BaseModel):
    trigger: str = "api"


def _connect():
    if not CONTROL_DSN:
        raise RuntimeError("RVBBIT_DSN is required")
    return psycopg.connect(CONTROL_DSN, row_factory=dict_row, autocommit=True)


def _check_token(authorization: str | None) -> None:
    if not WORKER_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="mirror controller authentication is not configured",
        )
    if authorization != f"Bearer {WORKER_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


def _normalize_name(value: str, kind: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _NAME_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail=f"invalid {kind} name")
    return normalized


def _redact_uri(value: str) -> str:
    try:
        parts = urlsplit(value)
    except Exception:
        return re.sub(r"://([^:/?#]+):([^@/?#]+)@", r"://\1:****@", value)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        user = credentials.split(":", 1)[0]
        netloc = f"{user}:****@{host}" if user else f"****@{host}"
    query = urlencode(
        [
            (
                key,
                "****" if any(secret in key.lower() for secret in _SECRET_QUERY_KEYS) else val,
            )
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _safe_error(exc: BaseException, *secret_values: str) -> str:
    text = str(exc)
    for secret in secret_values:
        if secret:
            text = text.replace(secret, "[credential redacted]")
            try:
                parts = urlsplit(secret)
                fragments = [parts.password or ""]
                fragments.extend(
                    value
                    for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    if any(marker in key.lower() for marker in _SECRET_QUERY_KEYS)
                )
                for fragment in fragments:
                    for candidate in {fragment, unquote(fragment)}:
                        if candidate:
                            text = text.replace(candidate, "[credential redacted]")
            except Exception:
                pass
    text = re.sub(r"[A-Za-z][A-Za-z0-9+._-]*://[^\s'\"]+", lambda m: _redact_uri(m.group(0)), text)
    return text[:2000] or type(exc).__name__


def _dsn_dialect(source_dsn: str) -> str:
    scheme = source_dsn.split("://", 1)[0].strip().lower()
    base = scheme.split("+", 1)[0]
    return _DIALECT_ALIASES.get(base, _DIALECT_ALIASES.get(scheme, base))


def _destination_table_name(source_name: str) -> str:
    """Return the exact Postgres relation name dlt will materialize."""
    if _destination_naming is None:
        raise RuntimeError("dlt identifier normalization is unavailable")
    normalized = _destination_naming.normalize_table_identifier(source_name)
    return _destination_naming.shorten_identifier(normalized, source_name, 63)


def _coerce_initial_cursor(value: Any) -> Any:
    """Turn JSON-safe ISO cursors back into the source driver's native type."""
    if not isinstance(value, str):
        return value
    candidate = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            return date.fromisoformat(candidate)
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", candidate):
            parsed = datetime.fromisoformat(
                candidate[:-1] + "+00:00" if candidate.lower().endswith("z") else candidate
            )
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return value


def _mssql_query_adapter(query: Any, *_args: Any) -> Any:
    """Bind datetimeoffset cursors in the form pymssql can round-trip.

    dlt restores ISO cursors as timezone-aware Pendulum datetimes.  pymssql
    serializes those values to a string that SQL Server rejects when it is
    bound to a reflected datetimeoffset predicate.  A naive UTC ``datetime``
    has the same instant semantics for our UTC-normalized cursor state and is
    accepted by the driver.  Restrict this conversion to SQL Server queries;
    the other drivers keep their native timezone-aware values.
    """
    def replace(element: Any) -> Any:
        if (
            isinstance(element, BindParameter)
            and isinstance(element.value, datetime)
            and element.value.tzinfo is not None
        ):
            value = element.value.astimezone(timezone.utc).replace(tzinfo=None)
            return element._with_value(value, maintain_key=True)
        return None

    return visitors.replacement_traverse(query, {}, replace)


def _oracle_table_adapter(table: Any) -> Any:
    """Treat reflected Oracle nullability as advisory for reporting mirrors.

    Oracle derives view-column nullability from underlying expressions and can
    report NOT NULL even when an outer join or aggregate emits NULL.  dlt then
    rejects a valid source row during normalization.  The local mirror does not
    need source-side NOT NULL enforcement, so preserve the values and retain
    primary-key hints separately from the discovery plan.
    """
    for column in table.columns:
        column.nullable = True
    return table


def _connection_record(connection_name: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT connection_name, label, dialect, credential_ref, enabled, metadata "
            "FROM rvbbit.mirror_connections WHERE connection_name = %s",
            (connection_name,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="mirror connection not found")
    return dict(row)


def _resolve_source_dsn(connection_name: str) -> tuple[dict[str, Any], str]:
    record = _connection_record(connection_name)
    if not record["enabled"]:
        raise RuntimeError("mirror connection is disabled")
    with _connect() as conn:
        row = conn.execute(
            "SELECT rvbbit.resolve_mirror_connection_credential(%s) AS source_dsn",
            (connection_name,),
        ).fetchone()
    source_dsn = str((row or {}).get("source_dsn") or "")
    if not source_dsn:
        raise RuntimeError("mirror connection credential is not configured")
    if _dsn_dialect(source_dsn) != record["dialect"]:
        raise RuntimeError("mirror connection dialect does not match its credential")
    return record, source_dsn


def _set_test_result(connection_name: str, ok: bool, error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE rvbbit.mirror_connections "
            "SET last_tested_at = clock_timestamp(), last_test_ok = %s, "
            "last_test_error = %s, updated_at = updated_at "
            "WHERE connection_name = %s",
            (ok, error, connection_name),
        )


def _probe_connection(connection_name: str) -> dict[str, Any]:
    _record, source_dsn = _resolve_source_dsn(connection_name)
    started = time.monotonic()
    engine = create_engine(source_dsn, pool_pre_ping=True, pool_recycle=300)
    try:
        with engine.connect() as conn:
            value = conn.execute(select(1)).scalar_one()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _set_test_result(connection_name, True)
        return {"ok": value == 1, "connection_name": connection_name, "elapsed_ms": elapsed_ms}
    except Exception as exc:
        message = _safe_error(exc, source_dsn)
        _set_test_result(connection_name, False, message)
        raise HTTPException(status_code=422, detail=message) from exc
    finally:
        engine.dispose()


def _discover_connection(
    connection_name: str, schema: str | None, include_views: bool, limit: int
) -> dict[str, Any]:
    record, source_dsn = _resolve_source_dsn(connection_name)
    chosen_schema = schema or str(record.get("metadata", {}).get("default_schema") or "") or None
    engine = create_engine(source_dsn, pool_pre_ping=True, pool_recycle=300)
    try:
        inspector = inspect(engine)
        names = list(inspector.get_table_names(schema=chosen_schema))
        if include_views:
            names.extend(inspector.get_view_names(schema=chosen_schema))
        names = sorted(set(names))
        discovery_limit = max(1, min(limit, MAX_DISCOVERY_TABLES))
        truncated = len(names) > discovery_limit
        names = names[:discovery_limit]
        tables: list[dict[str, Any]] = []
        for name in names:
            columns = inspector.get_columns(name, schema=chosen_schema)
            pk = inspector.get_pk_constraint(name, schema=chosen_schema) or {}
            tables.append(
                {
                    "name": name,
                    "destination_table": _destination_table_name(name),
                    "columns": [
                        {
                            "name": str(column.get("name")),
                            "type": str(column.get("type")),
                            "nullable": bool(column.get("nullable", True)),
                        }
                        for column in columns
                    ],
                    "primary_key": [str(value) for value in (pk.get("constrained_columns") or [])],
                }
            )
        return {
            "connection_name": connection_name,
            "dialect": record["dialect"],
            "schema": chosen_schema,
            "tables": tables,
            "truncated": truncated,
        }
    except Exception as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc, source_dsn)) from exc
    finally:
        engine.dispose()


def _discover_schemas(connection_name: str) -> dict[str, Any]:
    """Return selectable source schemas when the SQLAlchemy dialect exposes them."""
    record, source_dsn = _resolve_source_dsn(connection_name)
    engine = create_engine(source_dsn, pool_pre_ping=True, pool_recycle=300)
    try:
        inspector = inspect(engine)
        dialect = str(record["dialect"])
        try:
            if dialect == "oracle":
                # Oracle's SQLAlchemy inspector reads ALL_USERS, which includes
                # dozens of Oracle-maintained accounts even for a narrowly
                # privileged reporting login.  ALL_OBJECTS is privilege-aware,
                # and ORACLE_MAINTAINED keeps the setup picker focused on actual
                # application owners without assuming their names.
                with engine.connect() as conn:
                    raw_names = list(
                        conn.execute(
                            text(
                                "SELECT DISTINCT o.owner "
                                "FROM all_objects o "
                                "JOIN all_users u ON u.username = o.owner "
                                "WHERE u.oracle_maintained = 'N' "
                                "AND o.object_type IN "
                                "('TABLE', 'VIEW', 'MATERIALIZED VIEW') "
                                "ORDER BY o.owner"
                            )
                        ).scalars()
                    )
            else:
                raw_names = inspector.get_schema_names()
        except (NotImplementedError, AttributeError):
            return {
                "connection_name": connection_name,
                "dialect": dialect,
                "introspection_supported": False,
                "schemas": [],
                "default_schema": None,
            }
        excluded = {
            "information_schema",
            "pg_catalog",
            "pg_toast",
            "mysql",
            "performance_schema",
            "sys",
        }
        names = sorted({
            str(name).strip()
            for name in raw_names
            if str(name or "").strip()
            and str(name).strip().lower() not in excluded
            and not str(name).strip().lower().startswith("pg_")
        }, key=str.casefold)
        configured = str(record.get("metadata", {}).get("default_schema") or "").strip()
        dialect_default = str(getattr(inspector, "default_schema_name", "") or "").strip()
        preferred = [configured, dialect_default]
        if dialect == "postgresql":
            preferred.append("public")
        elif dialect == "mssql":
            preferred.append("dbo")
        default_schema = next((name for name in preferred if name in names), None)
        if default_schema is None and names:
            default_schema = names[0]
        return {
            "connection_name": connection_name,
            "dialect": dialect,
            "introspection_supported": True,
            "schemas": names,
            "default_schema": default_schema,
        }
    except Exception as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc, source_dsn)) from exc
    finally:
        engine.dispose()


def _enqueue_due() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT rvbbit.enqueue_due_mirror_runs() AS queued").fetchone()
    return int((row or {}).get("queued") or 0)


def _requeue_stale() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT rvbbit.requeue_stale_mirror_runs(%s, %s) AS changed",
            (STALE_SECONDS, MAX_ATTEMPTS),
        ).fetchone()
    return int((row or {}).get("changed") or 0)


def _claim_run() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM rvbbit.claim_mirror_run(%s)", (WORKER_ID,)
        ).fetchone()
    return dict(row) if row else None


def _heartbeat(run_id: UUID | str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT rvbbit.heartbeat_mirror_run(%s, %s) AS ok",
            (run_id, WORKER_ID),
        ).fetchone()
    return bool((row or {}).get("ok"))


def _load_job(run_id: UUID | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with _connect() as conn:
        job = conn.execute(
            "SELECT r.run_id, r.job_name, r.attempt, j.connection_name, "
            "j.source_schema, j.destination_schema, j.chunk_size, "
            "j.reflection_level, j.loader_file_format "
            "FROM rvbbit.mirror_runs r "
            "JOIN rvbbit.mirror_jobs j USING (job_name) "
            "JOIN rvbbit.mirror_connections c USING (connection_name) "
            "WHERE r.run_id = %s AND r.status = 'running' "
            "AND j.enabled AND c.enabled",
            (run_id,),
        ).fetchone()
        tables = conn.execute(
            "SELECT t.source_table, t.destination_table, t.load_mode, "
            "t.primary_key, t.cursor_column, t.initial_cursor, t.included_columns "
            "FROM rvbbit.mirror_tables t "
            "JOIN rvbbit.mirror_runs r USING (job_name) "
            "WHERE r.run_id = %s AND t.enabled ORDER BY t.source_table",
            (run_id,),
        ).fetchall()
    if not job:
        raise RuntimeError("claimed mirror run has no enabled job configuration")
    if not tables:
        raise RuntimeError("mirror job has no enabled tables")
    return dict(job), [dict(row) for row in tables]


def _record_table(
    run_id: UUID | str,
    table: dict[str, Any],
    status: str,
    rows_loaded: int = 0,
    load_ids: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "SELECT rvbbit.record_mirror_table_run("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                table["source_table"],
                table["destination_table"],
                table["load_mode"],
                status,
                max(0, int(rows_loaded)),
                load_ids or [],
                error_code,
                error_message,
            ),
        )


def _finish_run(
    run_id: UUID | str,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "SELECT rvbbit.finish_mirror_run(%s, %s, %s, %s)",
            (run_id, status, error_code, error_message),
        )


def _crawl_destination_catalog(destination_schemas: list[str]) -> None:
    """Refresh semantic catalog state after a mirror, outside its durable receipt."""
    with _connect() as conn:
        conn.execute(
            "CALL rvbbit.catalog_crawl_run(schemas => %s)",
            (destination_schemas,),
        )


def _catalog_refresh_loop() -> None:
    while True:
        schema = _catalog_queue.get()
        if schema is None:
            _catalog_queue.task_done()
            return
        schemas = {schema}
        consumed = 1
        # Coalesce mirrors that completed together into one crawl while keeping
        # the dlt queue free to claim its next durable run.
        while True:
            try:
                pending = _catalog_queue.get_nowait()
            except Empty:
                break
            consumed += 1
            if pending is None:
                for _ in range(consumed):
                    _catalog_queue.task_done()
                return
            schemas.add(pending)
        try:
            _crawl_destination_catalog(sorted(schemas))
        except Exception:
            log.exception(
                "background catalog refresh failed for schemas %s",
                ", ".join(sorted(schemas)),
            )
        finally:
            for _ in range(consumed):
                _catalog_queue.task_done()


def _start_catalog_refresh_worker() -> None:
    global _catalog_thread
    if _catalog_thread and _catalog_thread.is_alive():
        return
    _catalog_thread = threading.Thread(
        target=_catalog_refresh_loop,
        name="rvbbit-mirror-catalog-refresh",
        daemon=True,
    )
    _catalog_thread.start()


def _ensure_destination_registered(job: dict[str, Any], table: dict[str, Any]) -> None:
    """Backfill registration for mirrors created before USING rvbbit shipped."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT to_regclass(quote_ident(%s) || '.' || quote_ident(%s))::oid "
            "AS table_oid",
            (job["destination_schema"], table["destination_table"]),
        ).fetchone()
        table_oid = (row or {}).get("table_oid")
        if table_oid is None:
            raise RuntimeError("dlt did not materialize the destination relation")
        registered = conn.execute(
            "SELECT rvbbit.is_rvbbit_table(%s::oid::regclass) AS registered",
            (table_oid,),
        ).fetchone()
        if not bool((registered or {}).get("registered")):
            conn.execute(
                "SELECT rvbbit.enable_table(%s::oid::regclass)",
                (table_oid,),
            )
        verified = conn.execute(
            "SELECT rvbbit.is_rvbbit_table(%s::oid::regclass) AS registered",
            (table_oid,),
        ).fetchone()
    if not bool((verified or {}).get("registered")):
        raise RuntimeError("destination relation was not registered for acceleration")


def _pipeline_for(job: dict[str, Any]):
    if dlt is None:
        raise RuntimeError("dlt is not installed")
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        # Read-only or orchestrator-owned mounts may control the directory mode.
        pass
    destination_factory = RvbbitPostgresDestination or dlt.destinations.postgres
    destination = destination_factory(
        credentials=DESTINATION_DSN,
        destination_name="postgres",
        replace_strategy="truncate-and-insert",
        staging_dataset_name_layout="_dlt_%s_stage",
    )
    return dlt.pipeline(
        pipeline_name=f"rvbbit_mirror_{job['job_name']}",
        destination=destination,
        dataset_name=job["destination_schema"],
        pipelines_dir=str(STATE_DIR),
        dev_mode=False,
    )


def _resource_for(job: dict[str, Any], table: dict[str, Any], source_dsn: str):
    if dlt is None or sql_table is None:
        raise RuntimeError("dlt SQL source is not installed")
    incremental = None
    write_disposition: Any = "replace"
    if table["load_mode"] == "incremental_upsert":
        incremental_kwargs: dict[str, Any] = {"cursor_path": table["cursor_column"]}
        if table.get("initial_cursor") is not None:
            incremental_kwargs["initial_value"] = _coerce_initial_cursor(
                table["initial_cursor"]
            )
        incremental = dlt.sources.incremental(**incremental_kwargs)
        write_disposition = {"disposition": "merge", "strategy": "upsert"}
    resource = sql_table(
        credentials=source_dsn,
        table=table["source_table"],
        schema=job["source_schema"],
        incremental=incremental,
        chunk_size=int(job["chunk_size"]),
        backend="sqlalchemy",
        reflection_level=job["reflection_level"],
        included_columns=table.get("included_columns"),
        write_disposition=write_disposition,
        primary_key=table.get("primary_key"),
        table_adapter_callback=(
            _oracle_table_adapter
            if _dsn_dialect(source_dsn) == "oracle"
            else None
        ),
        query_adapter_callback=(
            _mssql_query_adapter
            if incremental is not None and _dsn_dialect(source_dsn) == "mssql"
            else None
        ),
    )
    resource.apply_hints(table_name=table["destination_table"])
    resource.max_table_nesting = 0
    return resource


def _row_count(pipeline: Any, destination_table: str) -> int:
    trace = getattr(pipeline, "last_trace", None)
    normalize = getattr(trace, "last_normalize_info", None)
    counts = getattr(normalize, "row_counts", {}) or {}
    if destination_table in counts:
        return max(0, int(counts[destination_table]))
    return max(0, sum(int(value) for value in counts.values()))


def _process_run(claim: dict[str, Any]) -> None:
    run_id = claim["run_id"]
    source_dsn = ""
    try:
        job, tables = _load_job(run_id)
        _connection, source_dsn = _resolve_source_dsn(job["connection_name"])
        pipeline = _pipeline_for(job)
        succeeded = 0
        failures: list[str] = []
        for table in tables:
            _record_table(run_id, table, "running")
            try:
                resource = _resource_for(job, table, source_dsn)
                load_info = pipeline.run(
                    resource,
                    loader_file_format=job["loader_file_format"],
                )
                load_info.raise_on_failed_jobs()
                _ensure_destination_registered(job, table)
                load_ids = [str(value) for value in (load_info.loads_ids or [])]
                _record_table(
                    run_id,
                    table,
                    "succeeded",
                    _row_count(pipeline, table["destination_table"]),
                    load_ids,
                )
                succeeded += 1
            except Exception as exc:
                message = _safe_error(exc, source_dsn, DESTINATION_DSN)
                failures.append(table["source_table"])
                _record_table(
                    run_id,
                    table,
                    "failed",
                    error_code=type(exc).__name__[:120],
                    error_message=message,
                )
                log.warning(
                    "mirror run %s table %s failed: %s",
                    run_id,
                    table["source_table"],
                    message,
                )
        if failures and succeeded:
            _finish_run(run_id, "partial", "TABLE_FAILED", f"{len(failures)} table(s) failed")
        elif failures:
            _finish_run(run_id, "failed", "ALL_TABLES_FAILED", "all mirror tables failed")
        else:
            _finish_run(run_id, "succeeded")
        # The mirror is already terminal before this is queued. A separate
        # single-file background worker coalesces schemas, so neither the UI nor
        # the dlt run queue waits on semantic indexing. The nightly full crawl
        # remains the repair path after a process interruption.
        if succeeded:
            _catalog_queue.put(str(job["destination_schema"]))
    except Exception as exc:
        message = _safe_error(exc, source_dsn, DESTINATION_DSN)
        log.warning("mirror run %s failed before table completion: %s", run_id, message)
        try:
            _finish_run(run_id, "failed", type(exc).__name__[:120], message)
        except Exception:
            log.exception("mirror run %s could not write its terminal receipt", run_id)


async def _heartbeat_loop(run_id: UUID | str, done: asyncio.Event) -> None:
    while not done.is_set():
        try:
            await asyncio.wait_for(done.wait(), timeout=HEARTBEAT_SECONDS)
        except TimeoutError:
            try:
                await asyncio.to_thread(_heartbeat, run_id)
            except Exception:
                log.exception("mirror run %s heartbeat failed", run_id)


async def _worker_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(_requeue_stale)
            await asyncio.to_thread(_enqueue_due)
            claim = await asyncio.to_thread(_claim_run)
            if claim:
                done = asyncio.Event()
                heartbeat = asyncio.create_task(_heartbeat_loop(claim["run_id"], done))
                try:
                    await asyncio.to_thread(_process_run, claim)
                finally:
                    done.set()
                    await heartbeat
                continue
        except Exception:
            log.exception("dlt mirror worker poll failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _runner_task, _stop_event
    if not WORKER_TOKEN:
        log.error(
            "RVBBIT_MIRROR_TOKEN is empty; HTTP administration endpoints are disabled"
        )
    _start_catalog_refresh_worker()
    _stop_event = asyncio.Event()
    _runner_task = asyncio.create_task(_worker_loop(_stop_event))
    yield
    _stop_event.set()
    if _runner_task:
        await _runner_task
    _catalog_queue.put(None)


app = FastAPI(title="RVBBIT dlt Mirror", lifespan=lifespan)


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    database_ok = False
    queued = running = 0
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT current_database() AS database, "
                "to_regclass('rvbbit.mirror_runs') IS NOT NULL AS ready, "
                "count(*) FILTER (WHERE status = 'queued') AS queued, "
                "count(*) FILTER (WHERE status = 'running') AS running "
                "FROM rvbbit.mirror_runs"
            ).fetchone()
        database_ok = bool((row or {}).get("ready"))
        database_name = str((row or {}).get("database") or "")
        queued = int((row or {}).get("queued") or 0)
        running = int((row or {}).get("running") or 0)
    except Exception:
        database_ok = False
        database_name = ""
    dlt_version = getattr(dlt, "__version__", None) if dlt else None
    healthy = bool(database_ok and dlt_version and WORKER_TOKEN)
    if not healthy:
        response.status_code = 503
    return {
        "ok": healthy,
        "database": database_ok,
        "database_name": database_name,
        "dlt_version": dlt_version,
        "controller_auth_configured": bool(WORKER_TOKEN),
        "worker_id": WORKER_ID,
        "queued": queued,
        "running": running,
        "default_load_mode": "snapshot",
        "canonical_tables": "rvbbit_registered_postgres_heap",
    }


@app.get("/drivers")
def drivers() -> dict[str, Any]:
    return {"drivers": _DRIVERS, "dlt_version": getattr(dlt, "__version__", None)}


@app.put("/connections/{connection_name}/credential")
def set_connection_credential(
    connection_name: str,
    request: CredentialRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    normalized = _normalize_name(connection_name, "connection")
    source_dsn = request.source_dsn.get_secret_value()
    if not source_dsn or "://" not in source_dsn:
        raise HTTPException(status_code=400, detail="a SQLAlchemy source DSN is required")
    record = _connection_record(normalized)
    if _dsn_dialect(source_dsn) != record["dialect"]:
        raise HTTPException(status_code=400, detail="credential dialect does not match connection")
    with _connect() as conn:
        row = conn.execute(
            "SELECT rvbbit.set_mirror_connection_credential(%s, %s) AS credential_ref",
            (normalized, source_dsn),
        ).fetchone()
    return {
        "ok": True,
        "connection_name": normalized,
        "credential_ref": (row or {}).get("credential_ref"),
    }


@app.delete("/connections/{connection_name}/credential")
def delete_connection_credential(
    connection_name: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    normalized = _normalize_name(connection_name, "connection")
    with _connect() as conn:
        row = conn.execute(
            "SELECT rvbbit.delete_mirror_connection_credential(%s) AS deleted",
            (normalized,),
        ).fetchone()
    return {"ok": True, "connection_name": normalized, "deleted": bool((row or {}).get("deleted"))}


@app.post("/connections/{connection_name}/probe")
async def probe_connection(
    connection_name: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    normalized = _normalize_name(connection_name, "connection")
    return await asyncio.to_thread(_probe_connection, normalized)


@app.post("/connections/{connection_name}/discover")
async def discover_connection(
    connection_name: str,
    request: DiscoverRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    normalized = _normalize_name(connection_name, "connection")
    limit = max(1, min(int(request.limit), MAX_DISCOVERY_TABLES))
    return await asyncio.to_thread(
        _discover_connection,
        normalized,
        request.source_schema,
        request.include_views,
        limit,
    )


@app.get("/connections/{connection_name}/schemas")
async def discover_schemas(
    connection_name: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    normalized = _normalize_name(connection_name, "connection")
    return await asyncio.to_thread(_discover_schemas, normalized)


@app.post("/jobs/{job_name}/runs")
def queue_run(
    job_name: str,
    request: QueueRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    normalized = _normalize_name(job_name, "job")
    trigger = str(request.trigger or "api").strip().lower()
    if trigger not in {"manual", "bootstrap", "retry", "api"}:
        raise HTTPException(status_code=400, detail="invalid run trigger")
    with _connect() as conn:
        row = conn.execute(
            "SELECT rvbbit.request_mirror_run(%s, %s) AS run_id",
            (normalized, trigger),
        ).fetchone()
    return {"ok": True, "job_name": normalized, "run_id": str((row or {}).get("run_id"))}


@app.get("/runs/{run_id}")
def run_status(
    run_id: UUID,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    with _connect() as conn:
        run = conn.execute(
            "SELECT run_id, job_name, trigger, status, worker_id, attempt, "
            "requested_at, started_at, heartbeat_at, finished_at, "
            "tables_succeeded, tables_failed, rows_loaded, load_ids, "
            "error_code, error_message "
            "FROM rvbbit.mirror_runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        tables = conn.execute(
            "SELECT source_table, destination_table, load_mode, status, "
            "rows_loaded, load_ids, started_at, finished_at, error_code, error_message "
            "FROM rvbbit.mirror_table_runs WHERE run_id = %s ORDER BY source_table",
            (run_id,),
        ).fetchall()
    if not run:
        raise HTTPException(status_code=404, detail="mirror run not found")
    return {"run": dict(run), "tables": [dict(row) for row in tables]}
