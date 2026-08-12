from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "crates"
    / "pg_rvbbit"
    / "sql"
    / "migrations"
    / "0286_canonical_credentials.sql"
)
RUST = ROOT / "crates" / "pg_rvbbit" / "src" / "credentials.rs"


def test_canonical_credentials_are_enveloped_and_reference_bound():
    rust = RUST.read_text(encoding="utf-8")

    assert "aead::AES_256_GCM" in rust
    assert "aead::Aad::from(credential_ref.as_bytes())" in rust
    assert "SystemRandom" in rust
    assert 'b"RVC1"' in rust
    assert "RVBBIT_CREDENTIAL_KEYS_FILE" in rust
    assert "RVBBIT_CREDENTIAL_KEY_FILE" in rust
    assert "WAREHOUSE_JWT_SECRET" not in rust


def test_canonical_store_has_metadata_only_audit_and_explicit_rewrap():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ciphertext bytea" in sql
    assert "secret_value" not in sql[sql.index("CREATE TABLE IF NOT EXISTS rvbbit.credential_events") : sql.index("CREATE INDEX IF NOT EXISTS credential_events_ref_created_idx")]
    assert "CREATE OR REPLACE FUNCTION rvbbit.rewrap_credentials()" in sql
    assert "plaintext := rvbbit.credential_unseal" in sql
    assert "ciphertext = rvbbit.credential_seal" in sql
    assert "'rewrapped'" in sql
    assert "REVOKE ALL ON FUNCTION rvbbit.rewrap_credentials() FROM PUBLIC" in sql


def test_legacy_migration_preserves_an_existing_canonical_value():
    sql = MIGRATION.read_text(encoding="utf-8")
    migration = sql[
        sql.index("CREATE OR REPLACE FUNCTION rvbbit.migrate_legacy_secrets()") :
        sql.index("CREATE OR REPLACE FUNCTION rvbbit.rewrap_credentials()")
    ]

    preserved = migration.index("canonical_preserved := EXISTS")
    conditional_write = migration.index("IF NOT canonical_preserved THEN")
    canonical_write = migration.index("rvbbit.put_credential")
    legacy_delete = migration.index("DELETE FROM rvbbit.secrets")
    assert preserved < conditional_write < canonical_write < legacy_delete
    assert "'canonical_preserved', canonical_preserved" in migration


def test_canonical_functions_are_not_public_resolution_surfaces():
    sql = MIGRATION.read_text(encoding="utf-8")

    for signature in (
        "rvbbit.resolve_credential(text, text, text)",
        "rvbbit.set_mcp_credential(text, text, text)",
        "rvbbit.resolve_mcp_credential(text, text)",
        "rvbbit.list_mcp_credentials()",
    ):
        assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in sql

    # Backend compatibility is the one deliberate exception until old
    # provider callers move to dedicated service roles.
    assert "GRANT EXECUTE ON FUNCTION rvbbit.get_secret(text) TO PUBLIC" in sql


def test_mcp_revocation_retains_a_ciphertext_free_tombstone():
    sql = MIGRATION.read_text(encoding="utf-8")
    revoke = sql[
        sql.index("CREATE OR REPLACE FUNCTION rvbbit.delete_mcp_credential(") :
        sql.index("CREATE OR REPLACE FUNCTION rvbbit.list_mcp_credentials()")
    ]

    assert "SET ciphertext = NULL" in revoke
    assert "status = 'revoked'" in revoke
    assert "version = version + 1" in revoke
    assert "'revoked'" in revoke
    assert "rvbbit.delete_credential" not in revoke
