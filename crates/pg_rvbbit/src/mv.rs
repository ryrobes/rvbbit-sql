//! Incremental semantic materialized views (RYR-292, cheap version).
//!
//! Mental model: a regular PG table mirrors the source's primary key
//! plus one or more projected columns (typically a semantic operator
//! call). Refresh is an anti-join INSERT — rows in source not yet in
//! the MV get computed once and stored. Repeats are free.
//!
//!   -- Set up once
//!   SELECT rvbbit.semantic_mv_create(
//!       mv_name       => 'ticket_triage',
//!       source_rel    => 'tickets'::regclass,
//!       pk_col        => 'id',
//!       projection_sql => 'rvbbit.classify(body, ''support,bug,sales,spam'')',
//!       projection_col => 'triage',
//!       projection_type => 'text');
//!
//!   -- Refresh on a schedule (cron / pg_cron / app-side)
//!   SELECT rvbbit.semantic_mv_refresh('ticket_triage');
//!
//!   -- Query like any table
//!   SELECT t.*, tt.triage FROM tickets t JOIN ticket_triage tt USING (id);
//!
//! Honest limitations:
//!   - INSERT-only source. UPDATE/DELETE on source rows is NOT
//!     detected; the projected value is computed once + cached.
//!     For full re-eval call rvbbit.semantic_mv_drop + create again.
//!   - PK column must be unique + present on both sides.
//!   - One projection column per MV (compose by chaining MVs).

use pgrx::extension_sql;
use pgrx::prelude::*;

extension_sql!(
    r#"
CREATE TABLE rvbbit.semantic_mvs (
    mv_name         text PRIMARY KEY,
    source_oid      oid NOT NULL,
    pk_col          text NOT NULL,
    projection_sql  text NOT NULL,
    projection_col  text NOT NULL,
    projection_type text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_refreshed  timestamptz,
    n_rows_total    bigint NOT NULL DEFAULT 0
);
"#,
    name = "create_semantic_mvs",
    requires = ["rvbbit_bootstrap"]
);

/// Create a semantic materialized view backed by a regular PG table
/// at `rvbbit.<mv_name>`. Initial population computes the projection
/// for every row in the source. Subsequent `semantic_mv_refresh`
/// calls compute only the new rows.
#[pg_extern(volatile)]
#[allow(clippy::too_many_arguments)]
fn semantic_mv_create(
    mv_name: &str,
    source_rel: pg_sys::Oid,
    pk_col: &str,
    projection_sql: &str,
    projection_col: default!(&str, "'value'"),
    projection_type: default!(&str, "'text'"),
) -> i64 {
    let source_oid = source_rel.to_u32();
    let mv_ident = quote_ident(mv_name);
    let pk_ident = quote_ident(pk_col);
    let proj_ident = quote_ident(projection_col);

    let qualified: String =
        match Spi::get_one::<String>(&format!("SELECT {source_oid}::oid::regclass::text")) {
            Ok(Some(s)) => s,
            _ => pgrx::error!("rvbbit.semantic_mv_create: bad regclass oid {source_oid}"),
        };
    // We need the SQL type of the pk column so the MV table picks it up.
    let pk_type: String = match Spi::get_one::<String>(&format!(
        "SELECT format_type(atttypid, atttypmod) \
         FROM pg_attribute \
         WHERE attrelid = {source_oid}::oid \
           AND attname = {} \
           AND NOT attisdropped",
        sql_string_literal(pk_col)
    )) {
        Ok(Some(s)) => s,
        _ => pgrx::error!("rvbbit.semantic_mv_create: column {pk_col:?} not found on {qualified}"),
    };

    // Create the MV table.
    Spi::run(&format!(
        "DROP TABLE IF EXISTS rvbbit.{mv_ident}; \
         CREATE TABLE rvbbit.{mv_ident} ( \
             {pk_ident} {pk_type} PRIMARY KEY, \
             {proj_ident} {projection_type} \
         )"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.semantic_mv_create: CREATE TABLE: {e}"));

    // Catalog row.
    Spi::run(&format!(
        "INSERT INTO rvbbit.semantic_mvs \
            (mv_name, source_oid, pk_col, projection_sql, projection_col, projection_type) \
         VALUES ({}, {source_oid}::oid, {}, {}, {}, {}) \
         ON CONFLICT (mv_name) DO UPDATE SET \
             source_oid = EXCLUDED.source_oid, \
             pk_col = EXCLUDED.pk_col, \
             projection_sql = EXCLUDED.projection_sql, \
             projection_col = EXCLUDED.projection_col, \
             projection_type = EXCLUDED.projection_type, \
             last_refreshed = NULL, \
             n_rows_total = 0",
        sql_string_literal(mv_name),
        sql_string_literal(pk_col),
        sql_string_literal(projection_sql),
        sql_string_literal(projection_col),
        sql_string_literal(projection_type),
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.semantic_mv_create: catalog INSERT: {e}"));

    // Initial population.
    refresh_inner(mv_name)
}

/// Refresh a semantic MV — anti-join INSERT for rows in source not
/// yet in the MV. Returns the number of new rows added.
#[pg_extern(volatile)]
fn semantic_mv_refresh(mv_name: &str) -> i64 {
    refresh_inner(mv_name)
}

/// Drop a semantic MV (table + catalog row). Returns 1 if dropped, 0
/// if not found.
#[pg_extern(volatile)]
fn semantic_mv_drop(mv_name: &str) -> i64 {
    let exists: Option<i64> = Spi::get_one(&format!(
        "SELECT 1::bigint FROM rvbbit.semantic_mvs WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .ok()
    .flatten();
    if exists.is_none() {
        return 0;
    }
    let mv_ident = quote_ident(mv_name);
    Spi::run(&format!(
        "DROP TABLE IF EXISTS rvbbit.{mv_ident}; \
         DELETE FROM rvbbit.semantic_mvs WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.semantic_mv_drop: {e}"));
    1
}

// ---------------------------------------------------------------------------

fn refresh_inner(mv_name: &str) -> i64 {
    // Probe existence first so an unknown name produces a clean
    // "not found" rather than a confusing SPI "tuple table positioned"
    // error from get_three / get_one returning Err.
    let exists: i64 = Spi::get_one::<i64>(&format!(
        "SELECT count(*)::bigint FROM rvbbit.semantic_mvs WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    if exists == 0 {
        pgrx::error!("rvbbit.semantic_mv_refresh: MV {mv_name:?} not found");
    }

    let source_oid: u32 = Spi::get_one::<i64>(&format!(
        "SELECT source_oid::oid::int8 FROM rvbbit.semantic_mvs WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .ok()
    .flatten()
    .map(|x| x as u32)
    .unwrap_or_else(|| pgrx::error!("rvbbit.semantic_mv_refresh: source_oid missing"));
    let pk_col: String = Spi::get_one(&format!(
        "SELECT pk_col FROM rvbbit.semantic_mvs WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .ok()
    .flatten()
    .unwrap_or_else(|| pgrx::error!("rvbbit.semantic_mv_refresh: pk_col missing"));
    let projection_sql: String = Spi::get_one(&format!(
        "SELECT projection_sql FROM rvbbit.semantic_mvs WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .ok()
    .flatten()
    .unwrap_or_else(|| pgrx::error!("rvbbit.semantic_mv_refresh: projection_sql missing"));
    let projection_col: String = Spi::get_one(&format!(
        "SELECT projection_col FROM rvbbit.semantic_mvs WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .ok()
    .flatten()
    .unwrap_or_else(|| pgrx::error!("rvbbit.semantic_mv_refresh: projection_col missing"));

    let qualified_source: String =
        match Spi::get_one::<String>(&format!("SELECT {source_oid}::oid::regclass::text")) {
            Ok(Some(s)) => s,
            _ => pgrx::error!("rvbbit.semantic_mv_refresh: source table missing"),
        };
    let mv_ident = quote_ident(mv_name);
    let pk_ident = quote_ident(&pk_col);
    let proj_ident = quote_ident(&projection_col);

    // Pre-warm: if the projection is a plain rvbbit.<op>(...) call, run the
    // pending rows through the batched + concurrent execution engine first.
    // This fills L1 + receipts so the per-row INSERT below resolves from
    // cache instead of issuing one provider call per row (43s -> seconds on
    // a 500-row sentiment MV). A non-operator projection returns None and
    // the INSERT simply runs un-warmed.
    if let Some(stats) =
        crate::prewarm::warm_mv_projection(&qualified_source, &mv_ident, &pk_ident, &projection_sql)
    {
        if stats.n_executed > 0 || stats.n_errors > 0 {
            pgrx::notice!(
                "rvbbit.semantic_mv: pre-warmed {} pending rows ({} executed, {} cached, {} errors)",
                stats.n_inputs,
                stats.n_executed,
                stats.n_cache_hits,
                stats.n_errors
            );
        }
    }

    // Anti-join INSERT — every row in source whose PK is missing from
    // the MV gets the projection computed once + stored.
    let insert_sql = format!(
        "INSERT INTO rvbbit.{mv_ident} ({pk_ident}, {proj_ident}) \
         SELECT s.{pk_ident}, ({projection_sql}) AS {proj_ident} \
         FROM {qualified_source} s \
         LEFT JOIN rvbbit.{mv_ident} t ON t.{pk_ident} = s.{pk_ident} \
         WHERE t.{pk_ident} IS NULL"
    );
    Spi::run(&insert_sql).unwrap_or_else(|e| pgrx::error!("rvbbit.semantic_mv_refresh: {e}"));

    let n_total: i64 = Spi::get_one(&format!("SELECT count(*) FROM rvbbit.{mv_ident}"))
        .ok()
        .flatten()
        .unwrap_or(0);

    let prev_total: i64 = Spi::get_one(&format!(
        "SELECT coalesce(n_rows_total, 0) FROM rvbbit.semantic_mvs WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let new_rows = (n_total - prev_total).max(0);

    Spi::run(&format!(
        "UPDATE rvbbit.semantic_mvs \
         SET last_refreshed = now(), n_rows_total = {n_total} \
         WHERE mv_name = {}",
        sql_string_literal(mv_name)
    ))
    .ok();

    new_rows
}

// ---------------------------------------------------------------------------
// Quoting helpers — defensive, since user input gets interpolated.

fn quote_ident(s: &str) -> String {
    // Double any embedded double-quote, wrap in quotes.
    let escaped = s.replace('"', "\"\"");
    format!("\"{escaped}\"")
}

fn sql_string_literal(s: &str) -> String {
    let escaped = s.replace('\'', "''");
    format!("'{escaped}'")
}
