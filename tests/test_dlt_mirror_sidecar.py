from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import DateTime, Column, MetaData, Table, select


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "capabilities" / "templates" / "dlt-mirror" / "main.py"
MIGRATION = (
    ROOT
    / "crates"
    / "pg_rvbbit"
    / "sql"
    / "migrations"
    / "0287_dlt_mirror_control_plane.sql"
)
RUNTIME_MIGRATION = (
    ROOT
    / "crates"
    / "pg_rvbbit"
    / "sql"
    / "migrations"
    / "0289_data_mover_runtime_registry.sql"
)
IDENTIFIER_MIGRATION = (
    ROOT
    / "crates"
    / "pg_rvbbit"
    / "sql"
    / "migrations"
    / "0290_dlt_destination_identifier_contract.sql"
)
REGISTRATION_MIGRATION = (
    ROOT
    / "crates"
    / "pg_rvbbit"
    / "sql"
    / "migrations"
    / "0292_dlt_mirror_rvbbit_registration.sql"
)
FOLLOWTHROUGH_MIGRATION = (
    ROOT
    / "crates"
    / "pg_rvbbit"
    / "sql"
    / "migrations"
    / "0293_hosted_onboarding_followthrough.sql"
)
FIXTURE_COMPOSE = ROOT / "docker" / "docker-compose.ingestion-test.yml"
FIXTURE_SOURCES = ROOT / "docker" / "sample-sources"


def _load_module(monkeypatch, tmp_path):
    monkeypatch.setenv("RVBBIT_DSN", "postgresql://control:control-secret@postgres/rvbbit")
    monkeypatch.setenv("RVBBIT_MIRROR_STATE_DIR", str(tmp_path / "state"))
    spec = importlib.util.spec_from_file_location("rvbbit_dlt_mirror_test", MAIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dlt_mirror_redacts_connection_strings(monkeypatch, tmp_path):
    mirror = _load_module(monkeypatch, tmp_path)
    source = "postgresql://reader:very-secret@db.internal:5432/erp?api_key=also-secret"

    redacted = mirror._redact_uri(source)
    message = mirror._safe_error(
        RuntimeError(
            f"could not connect to {source}; password very-secret; token also-secret"
        ),
        source,
    )

    assert "very-secret" not in redacted
    assert "also-secret" not in redacted
    assert "very-secret" not in message
    assert "also-secret" not in message
    assert "credential redacted" in message


def test_dlt_mirror_http_admin_endpoints_fail_closed_without_a_token(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("RVBBIT_MIRROR_TOKEN", raising=False)
    mirror = _load_module(monkeypatch, tmp_path)

    with pytest.raises(mirror.HTTPException) as missing:
        mirror._check_token(None)
    assert missing.value.status_code == 503

    monkeypatch.setenv("RVBBIT_MIRROR_TOKEN", "controller-token")
    configured = _load_module(monkeypatch, tmp_path)
    configured._check_token("Bearer controller-token")
    with pytest.raises(configured.HTTPException) as rejected:
        configured._check_token("Bearer wrong-token")
    assert rejected.value.status_code == 401


def test_dlt_mirror_health_fails_when_controller_auth_is_unconfigured(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("RVBBIT_MIRROR_TOKEN", raising=False)
    mirror = _load_module(monkeypatch, tmp_path)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def execute(_query):
            return SimpleNamespace(
                fetchone=lambda: {
                    "database": "rvbbit",
                    "ready": True,
                    "queued": 0,
                    "running": 0,
                }
            )

    monkeypatch.setattr(mirror, "_connect", Connection)
    response = mirror.Response()

    result = mirror.health(response)

    assert result["ok"] is False
    assert result["database"] is True
    assert result["controller_auth_configured"] is False
    assert response.status_code == 503


def test_dlt_mirror_builds_snapshot_and_incremental_resources(monkeypatch, tmp_path):
    mirror = _load_module(monkeypatch, tmp_path)
    calls = []

    class Resource:
        def __init__(self):
            self.hints = {}
            self.max_table_nesting = None

        def apply_hints(self, **kwargs):
            self.hints.update(kwargs)

    def fake_sql_table(**kwargs):
        resource = Resource()
        calls.append((kwargs, resource))
        return resource

    def fake_incremental(**kwargs):
        return {"incremental": kwargs}

    mirror.dlt = SimpleNamespace(sources=SimpleNamespace(incremental=fake_incremental))
    mirror.sql_table = fake_sql_table
    job = {
        "source_schema": "dbo",
        "chunk_size": 25000,
        "reflection_level": "full",
    }

    snapshot = mirror._resource_for(
        job,
        {
            "source_table": "Customers",
            "destination_table": "customers",
            "load_mode": "snapshot",
            "primary_key": ["CustomerID"],
            "cursor_column": None,
            "initial_cursor": None,
            "included_columns": None,
        },
        "mssql+pymssql://reader:secret@db/erp",
    )
    incremental = mirror._resource_for(
        job,
        {
            "source_table": "Orders",
            "destination_table": "orders",
            "load_mode": "incremental_upsert",
            "primary_key": ["OrderID"],
            "cursor_column": "UpdatedAt",
            "initial_cursor": "2026-01-01T00:00:00Z",
            "included_columns": ["OrderID", "UpdatedAt", "Payload"],
        },
        "mssql+pymssql://reader:secret@db/erp",
    )

    snapshot_call, incremental_call = calls
    assert snapshot_call[0]["write_disposition"] == "replace"
    assert snapshot.hints == {"table_name": "customers"}
    assert snapshot.max_table_nesting == 0
    assert incremental_call[0]["write_disposition"] == {
        "disposition": "merge",
        "strategy": "upsert",
    }
    assert incremental_call[0]["incremental"] == {
        "incremental": {
            "cursor_path": "UpdatedAt",
            "initial_value": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    }
    assert incremental_call[0]["query_adapter_callback"] is mirror._mssql_query_adapter
    assert incremental.hints == {"table_name": "orders"}
    assert incremental.max_table_nesting == 0


def test_dlt_mirror_coerces_json_safe_cursor_values(monkeypatch, tmp_path):
    mirror = _load_module(monkeypatch, tmp_path)

    assert mirror._coerce_initial_cursor(42) == 42
    assert mirror._coerce_initial_cursor("account-42") == "account-42"
    assert mirror._coerce_initial_cursor("2026-08-11") == mirror.date(2026, 8, 11)
    assert mirror._coerce_initial_cursor("2026-08-11T12:30:00Z") == mirror.datetime(
        2026, 8, 11, 12, 30, tzinfo=mirror.timezone.utc
    )


def test_dlt_mirror_normalizes_mssql_datetimeoffset_binds_to_naive_utc(
    monkeypatch, tmp_path
):
    mirror = _load_module(monkeypatch, tmp_path)
    source = Table(
        "orders",
        MetaData(),
        Column("updated_at", DateTime(timezone=True)),
    )
    cursor = datetime.fromisoformat("2026-08-11T09:10:00-05:00")
    query = select(source).where(source.c.updated_at >= cursor)

    adapted = mirror._mssql_query_adapter(query)

    assert list(adapted.compile().params.values()) == [datetime(2026, 8, 11, 14, 10)]


def test_dlt_mirror_relaxes_oracle_reflection_nullability(monkeypatch, tmp_path):
    mirror = _load_module(monkeypatch, tmp_path)
    source = Table(
        "store_orders",
        MetaData(),
        Column("store_name", nullable=False),
        Column("order_status", nullable=False),
    )

    assert mirror._oracle_table_adapter(source) is source
    assert all(column.nullable for column in source.columns)


def test_dlt_mirror_applies_oracle_table_adapter(monkeypatch, tmp_path):
    mirror = _load_module(monkeypatch, tmp_path)
    captured = {}

    class Resource:
        max_table_nesting = None

        @staticmethod
        def apply_hints(**_kwargs):
            pass

    def fake_sql_table(**kwargs):
        captured.update(kwargs)
        return Resource()

    mirror.dlt = SimpleNamespace()
    mirror.sql_table = fake_sql_table
    mirror._resource_for(
        {
            "source_schema": "CO",
            "chunk_size": 50000,
            "reflection_level": "full",
        },
        {
            "source_table": "store_orders",
            "destination_table": "store_orders",
            "load_mode": "snapshot",
            "primary_key": None,
            "cursor_column": None,
            "initial_cursor": None,
            "included_columns": None,
        },
        "oracle+oracledb://reader:secret@db/?service_name=FREEPDB1",
    )

    assert captured["table_adapter_callback"] is mirror._oracle_table_adapter


def test_dlt_mirror_reports_the_exact_normalized_destination_name(
    monkeypatch, tmp_path
):
    mirror = _load_module(monkeypatch, tmp_path)

    class Naming:
        @staticmethod
        def normalize_table_identifier(value):
            return {"Orders": "orders", "OrderLines": "order_lines"}[value]

        @staticmethod
        def shorten_identifier(normalized, _original, maximum):
            return normalized[:maximum]

    monkeypatch.setattr(mirror, "_destination_naming", Naming())
    assert mirror._destination_table_name("Orders") == "orders"
    assert mirror._destination_table_name("OrderLines") == "order_lines"


def test_dlt_mirror_discovers_selectable_non_system_schemas(monkeypatch, tmp_path):
    mirror = _load_module(monkeypatch, tmp_path)

    class Inspector:
        default_schema_name = "public"

        @staticmethod
        def get_schema_names():
            return ["sales", "pg_catalog", "public", "information_schema", "support"]

    class Engine:
        def dispose(self):
            pass

    monkeypatch.setattr(
        mirror,
        "_resolve_source_dsn",
        lambda _name: ({"dialect": "postgresql", "metadata": {}}, "postgresql://safe"),
    )
    monkeypatch.setattr(mirror, "create_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(mirror, "inspect", lambda _engine: Inspector())

    result = mirror._discover_schemas("erp")

    assert result["introspection_supported"] is True
    assert result["schemas"] == ["public", "sales", "support"]
    assert result["default_schema"] == "public"


def test_dlt_mirror_uses_oracle_privileges_to_hide_maintained_schemas(
    monkeypatch, tmp_path
):
    mirror = _load_module(monkeypatch, tmp_path)
    executed = []

    class Scalars:
        @staticmethod
        def __iter__():
            return iter(["CO", "SH"])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def execute(query):
            executed.append(str(query))
            return SimpleNamespace(scalars=lambda: Scalars())

    class Engine:
        @staticmethod
        def connect():
            return Connection()

        @staticmethod
        def dispose():
            pass

    class Inspector:
        default_schema_name = "MIRROR_READER"

        @staticmethod
        def get_schema_names():
            raise AssertionError("Oracle discovery must not expose ALL_USERS")

    monkeypatch.setattr(
        mirror,
        "_resolve_source_dsn",
        lambda _name: ({"dialect": "oracle", "metadata": {}}, "oracle://safe"),
    )
    monkeypatch.setattr(mirror, "create_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(mirror, "inspect", lambda _engine: Inspector())

    result = mirror._discover_schemas("erp")

    assert result["introspection_supported"] is True
    assert result["schemas"] == ["CO", "SH"]
    assert result["default_schema"] == "CO"
    assert executed and "oracle_maintained" in executed[0].lower()
    assert "all_objects" in executed[0].lower()


def test_dlt_mirror_pipeline_uses_registered_heap_with_transient_merge_staging(
    monkeypatch, tmp_path
):
    mirror = _load_module(monkeypatch, tmp_path)
    captured = {}

    def fake_postgres(**kwargs):
        captured["destination"] = kwargs
        return "destination"

    def fake_pipeline(**kwargs):
        captured["pipeline"] = kwargs
        return "pipeline"

    mirror.dlt = SimpleNamespace(
        destinations=SimpleNamespace(postgres=fake_postgres), pipeline=fake_pipeline
    )
    result = mirror._pipeline_for(
        {"job_name": "erp_core", "destination_schema": "erp"}
    )

    assert result == "pipeline"
    assert captured["destination"]["replace_strategy"] == "truncate-and-insert"
    assert captured["destination"]["credentials"] == mirror.CONTROL_DSN
    assert captured["destination"]["destination_name"] == "postgres"
    assert captured["destination"]["staging_dataset_name_layout"] == "_dlt_%s_stage"
    assert captured["pipeline"]["dataset_name"] == "erp"
    assert mirror.os.environ["LOAD__TRUNCATE_STAGING_DATASET"] == "true"


def test_dlt_mirror_creates_only_user_destination_tables_using_rvbbit(
    monkeypatch, tmp_path
):
    mirror = _load_module(monkeypatch, tmp_path)
    create = ['CREATE TABLE "erp"."orders" (\n"id" bigint\n)']

    assert mirror._rvbbit_table_update_sql(
        create,
        generate_alter=False,
        in_staging_dataset=False,
        is_dlt_table=False,
    ) == [create[0] + " USING rvbbit"]
    assert mirror._rvbbit_table_update_sql(
        create,
        generate_alter=False,
        in_staging_dataset=True,
        is_dlt_table=False,
    ) == create
    assert mirror._rvbbit_table_update_sql(
        create,
        generate_alter=False,
        in_staging_dataset=False,
        is_dlt_table=True,
    ) == create
    assert mirror._rvbbit_table_update_sql(
        ['ALTER TABLE "erp"."orders" ADD COLUMN "note" varchar'],
        generate_alter=True,
        in_staging_dataset=False,
        is_dlt_table=False,
    ) == ['ALTER TABLE "erp"."orders" ADD COLUMN "note" varchar']


def test_dlt_mirror_control_plane_is_reference_only_and_registered():
    sql = MIGRATION.read_text(encoding="utf-8")
    identifier_sql = IDENTIFIER_MIGRATION.read_text(encoding="utf-8")
    registration_sql = REGISTRATION_MIGRATION.read_text(encoding="utf-8")
    followthrough_sql = FOLLOWTHROUGH_MIGRATION.read_text(encoding="utf-8")
    worker = MAIN.read_text(encoding="utf-8")
    registry = (
        ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs"
    ).read_text(encoding="utf-8")

    assert "0287_dlt_mirror_control_plane" in registry
    assert 'include_str!("../sql/migrations/0287_dlt_mirror_control_plane.sql")' in registry
    assert "'mirror/' || connection_name || '/SOURCE_DSN'" in sql
    assert "resolve_mirror_connection_credential" in sql
    assert "load_mode text NOT NULL DEFAULT 'snapshot'" in sql
    assert "destination_schema ~ '^[a-z][a-z0-9_]{0,47}$'" in sql
    assert "destination_schema !~ '^pg_'" in sql
    assert "destination_table ~ '^_?[a-z0-9]+(_[a-z0-9]+)*$'" in sql
    assert "0290_dlt_destination_identifier_contract" in registry
    assert "NOT VALID" in identifier_sql
    assert "VALIDATE CONSTRAINT mirror_tables_destination_check" in identifier_sql
    assert "CREATE OR REPLACE VIEW rvbbit.mirror_lineage" in sql
    assert "CREATE OR REPLACE VIEW rvbbit.mirror_run_status" in sql
    assert "nullif(c.metadata ->> 'host', '') AS source_host" in sql
    assert "0292_dlt_mirror_rvbbit_registration" in registry
    assert "0293_hosted_onboarding_followthrough" in registry
    assert "UNIQUE (destination_schema)" not in sql
    assert "mirror_jobs_destination_schema_key" in followthrough_sql
    assert "mirror_tables_destination_relation_key" in followthrough_sql
    assert "destination_registered" in registration_sql
    assert "acceleration_enabled" in registration_sql
    assert "rvbbit.enable_table" in registration_sql
    assert "RVBBIT_MIRROR_DESTINATION_DSN" not in worker
    assert 'updated[0] = f"{updated[0]} USING rvbbit"' in worker
    assert "rvbbit.enable_table" in worker
    assert 'app.get("/connections/{connection_name}/schemas")' in worker
    assert "inspector.get_schema_names()" in worker
    assert "CALL rvbbit.catalog_crawl_run(schemas => %s)" in worker
    assert "_catalog_queue.put(str(job[\"destination_schema\"]))" in worker
    assert "to_regclass(quote_ident(%s) || '.' || quote_ident(%s))" in worker
    assert "server.password" not in sql


def test_dlt_mirror_dependencies_are_pinned():
    requirements = (
        ROOT / "capabilities" / "templates" / "dlt-mirror" / "requirements.txt"
    ).read_text(encoding="utf-8")
    assert "dlt[postgres,sql_database]==1.29.1" in requirements
    assert all("==" in line for line in requirements.splitlines() if line.strip())


def test_ingestion_fixtures_are_network_only_and_use_read_only_principals():
    compose = FIXTURE_COMPOSE.read_text(encoding="utf-8")
    hermes = (FIXTURE_SOURCES / "hermes-stub.py").read_text(encoding="utf-8")
    postgres = (FIXTURE_SOURCES / "postgres-init.sql").read_text(encoding="utf-8")
    mysql = (FIXTURE_SOURCES / "mysql-init.sql").read_text(encoding="utf-8")
    mssql = (FIXTURE_SOURCES / "mssql-init.sql").read_text(encoding="utf-8")
    oracle_dockerfile = (FIXTURE_SOURCES / "oracle" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    oracle_init = (FIXTURE_SOURCES / "oracle" / "rvbbit-init.sh").read_text(
        encoding="utf-8"
    )

    assert "SAMPLE_HERMES_IMAGE:-python:3.12.11-slim-bookworm@sha256:" in compose
    assert "SAMPLE_POSTGRES_IMAGE:-postgres@sha256:" in compose
    assert "SAMPLE_MYSQL_IMAGE:-mysql@sha256:" in compose
    assert "SAMPLE_MSSQL_IMAGE:-mcr.microsoft.com/mssql/server@sha256:" in compose
    assert "SAMPLE_ORACLE_BASE_IMAGE:-gvenzl/oracle-free:23.26.2-slim-faststart@sha256:" in compose
    assert "ports:" not in compose
    assert "external: true" in compose
    assert "rvbbit_uber" in compose
    assert "hermes-session-api" in compose
    assert '"status": "degraded"' in hermes
    assert '"model": {"status": "error"}' in hermes
    assert "/api/sessions" in hermes
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA sales, support TO mirror_reader" in postgres
    assert "GRANT SELECT ON crm.* TO 'mirror_reader'@'%'" in mysql
    assert "GRANT SELECT ON SCHEMA::field_ops TO mirror_reader" in mssql
    assert "6660bad68c07bd143430ace58565b3f727e17263" in oracle_dockerfile
    assert "e5365e55840dd1f23a712704951e6d77f928bb0100807781576ff92fa1cff6ad" in oracle_dockerfile
    assert "sqlldr" in oracle_init
    assert "rvbbit-oracle-fixture-ready" in oracle_init
    assert "test -f /tmp/rvbbit-oracle-fixture-ready" in compose
    assert "GRANT SELECT ON" in oracle_init
    assert "ALTER USER co ACCOUNT LOCK" in oracle_init
    assert "ALTER USER sh ACCOUNT LOCK" in oracle_init


def test_hosted_smoke_includes_both_official_oracle_sample_schemas():
    smoke = (ROOT / "scripts" / "hosted-appliance-smoke.py").read_text(
        encoding="utf-8"
    )

    assert '"oracle+oracledb://mirror_reader:' in smoke
    assert '(("CO", "oracle_co"), ("SH", "oracle_sh"))' in smoke
    assert '"--fixture"' in smoke
    assert '"--skip-profile"' in smoke


def test_warren_registers_and_health_probes_data_mover_runtimes():
    migration = RUNTIME_MIGRATION.read_text(encoding="utf-8")
    registry = (
        ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs"
    ).read_text(encoding="utf-8")
    warren = (ROOT / "warren" / "agent" / "src" / "main.rs").read_text(
        encoding="utf-8"
    )
    python_runtime = (
        ROOT / "crates" / "pg_rvbbit" / "src" / "python_runtime.rs"
    ).read_text(encoding="utf-8")
    bootstrap = (ROOT / "docker" / "uber" / "bootstrap.sh").read_text(
        encoding="utf-8"
    )

    assert "language IN ('python', 'data_mover')" in migration
    assert "register_data_mover_runtime" in migration
    assert "0289_data_mover_runtime_registry" in registry
    assert '"data_mover" => {' in warren
    assert "registering data-mover runtime" in warren
    assert '| "data_mover"' in warren
    assert python_runtime.count("r.language = 'python'") >= 2
    assert "data/dlt-mirror)" in bootstrap
    assert "r.language = 'data_mover'" in bootstrap
