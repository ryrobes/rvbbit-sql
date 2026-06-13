//! Stacked, idempotent, run-once SQL migrations — decoupled from the extension
//! version.
//!
//! Postgres's native `ALTER EXTENSION UPDATE FROM..TO` forces a contiguous
//! version-edge graph that we'd coupled to the docker release tag; it drifts and
//! breaks. Instead, every file in `sql/migrations/NNNN_*.sql` is applied in
//! order exactly once, tracked in `rvbbit.schema_migrations`, by
//! `rvbbit.migrate()` — "run whatever hasn't run yet", never by version. The
//! extension version now only governs the C/.so bindings.
//!
//! Adding a migration:
//!   1. drop `sql/migrations/NNNN_description.sql` (idempotent DDL: CREATE OR
//!      REPLACE / IF NOT EXISTS), NNNN strictly increasing;
//!   2. add one line to `MIGRATIONS` below;
//!   3. rebuild the image. Deploy runs `SELECT rvbbit.migrate();` (Makefile
//!      reload-extension / docker init), which applies the new one.
//!
//! Never reorder or edit an already-shipped migration's SQL — add a new one.

use pgrx::prelude::*;

/// Ordered, compile-time-embedded migration list. APPEND ONLY.
const MIGRATIONS: &[(&str, &str)] = &[
    (
        "0001_durable_catalog_crawl",
        include_str!("../sql/migrations/0001_durable_catalog_crawl.sql"),
    ),
    (
        "0002_default_embedder",
        include_str!("../sql/migrations/0002_default_embedder.sql"),
    ),
    (
        "0003_parallel_catalog_crawl",
        include_str!("../sql/migrations/0003_parallel_catalog_crawl.sql"),
    ),
    (
        "0004_cubes",
        include_str!("../sql/migrations/0004_cubes.sql"),
    ),
    (
        "0005_cubes_enrich",
        include_str!("../sql/migrations/0005_cubes_enrich.sql"),
    ),
    (
        "0006_cubes_v3",
        include_str!("../sql/migrations/0006_cubes_v3.sql"),
    ),
    (
        "0007_cubes_sql_trim",
        include_str!("../sql/migrations/0007_cubes_sql_trim.sql"),
    ),
    (
        "0008_proposals",
        include_str!("../sql/migrations/0008_proposals.sql"),
    ),
    (
        "0009_cube_model",
        include_str!("../sql/migrations/0009_cube_model.sql"),
    ),
];

const SCHEMA_MIGRATIONS_DDL: &str = "\
CREATE TABLE IF NOT EXISTS rvbbit.schema_migrations (
    name        text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)";

fn sql_quote(s: &str) -> String {
    s.replace('\'', "''")
}

/// Apply every embedded migration not yet recorded, in order, recording each in
/// `rvbbit.schema_migrations`. Returns a one-line summary.
///
/// Runs in the caller's transaction: the pending set is applied atomically — if
/// any migration fails the whole call aborts and nothing is recorded, so you fix
/// it and re-run (migrations are idempotent). Safe to call on every deploy; a
/// no-op once everything is applied.
#[pg_extern]
fn migrate() -> String {
    Spi::run(SCHEMA_MIGRATIONS_DDL).expect("rvbbit.migrate: create schema_migrations");

    let mut applied: Vec<&str> = Vec::new();
    for (name, sql) in MIGRATIONS {
        let done = Spi::get_one::<bool>(&format!(
            "SELECT EXISTS(SELECT 1 FROM rvbbit.schema_migrations WHERE name = '{}')",
            sql_quote(name)
        ))
        .unwrap_or(Some(false))
        .unwrap_or(false);
        if done {
            continue;
        }
        if let Err(e) = Spi::run(sql) {
            pgrx::error!("rvbbit.migrate: migration '{}' failed: {:?}", name, e);
        }
        Spi::run(&format!(
            "INSERT INTO rvbbit.schema_migrations (name) VALUES ('{}')",
            sql_quote(name)
        ))
        .unwrap_or_else(|e| pgrx::error!("rvbbit.migrate: recording '{}' failed: {:?}", name, e));
        applied.push(name);
    }

    if applied.is_empty() {
        format!(
            "rvbbit.migrate: up to date ({} migration(s) known)",
            MIGRATIONS.len()
        )
    } else {
        format!(
            "rvbbit.migrate: applied {} of {} — {}",
            applied.len(),
            MIGRATIONS.len(),
            applied.join(", ")
        )
    }
}

/// List every embedded migration and whether it has been applied. Read-only
/// companion to migrate() for inspection.
#[pg_extern]
fn migrations_status() -> TableIterator<'static, (name!(name, String), name!(applied, bool))> {
    // Tolerate a never-migrated database (no schema_migrations yet).
    let _ = Spi::run(SCHEMA_MIGRATIONS_DDL);
    let rows: Vec<(String, bool)> = MIGRATIONS
        .iter()
        .map(|(name, _)| {
            let done = Spi::get_one::<bool>(&format!(
                "SELECT EXISTS(SELECT 1 FROM rvbbit.schema_migrations WHERE name = '{}')",
                sql_quote(name)
            ))
            .unwrap_or(Some(false))
            .unwrap_or(false);
            (name.to_string(), done)
        })
        .collect();
    TableIterator::new(rows.into_iter())
}
