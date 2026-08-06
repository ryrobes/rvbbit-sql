from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"


def test_calendar_base_view_preserves_forward_canonical_columns():
    """0237 must not narrow a calendar view already created at the 0238 shape."""
    sql = (MIGRATIONS / "0237_calliope_google_calendar.sql").read_text(
        encoding="utf-8"
    )

    assert "SELECT raw_edges.*" in sql
    for declaration in (
        "NULL::bigint AS kg_node_id",
        "NULL::text AS graph_id",
        "NULL::text AS node_kind",
        "NULL::text AS canonical_label",
        "NULL::text AS match_basis",
        "NULL::double precision AS match_confidence",
    ):
        assert declaration in sql
