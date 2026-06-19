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
    (
        "0010_metric_proposals",
        include_str!("../sql/migrations/0010_metric_proposals.sql"),
    ),
    (
        "0011_near_frontier_proposals",
        include_str!("../sql/migrations/0011_near_frontier_proposals.sql"),
    ),
    (
        "0012_semantic_model",
        include_str!("../sql/migrations/0012_semantic_model.sql"),
    ),
    (
        "0013_proposal_lifecycle",
        include_str!("../sql/migrations/0013_proposal_lifecycle.sql"),
    ),
    (
        "0014_materialize_all_metrics",
        include_str!("../sql/migrations/0014_materialize_all_metrics.sql"),
    ),
    (
        "0015_proposal_exemplars",
        include_str!("../sql/migrations/0015_proposal_exemplars.sql"),
    ),
    (
        "0016_activity_discovery",
        include_str!("../sql/migrations/0016_activity_discovery.sql"),
    ),
    (
        "0017_usage_weighted_search",
        include_str!("../sql/migrations/0017_usage_weighted_search.sql"),
    ),
    (
        "0018_proposal_quality",
        include_str!("../sql/migrations/0018_proposal_quality.sql"),
    ),
    (
        "0019_metric_check_resilience",
        include_str!("../sql/migrations/0019_metric_check_resilience.sql"),
    ),
    (
        "0020_proposal_categories",
        include_str!("../sql/migrations/0020_proposal_categories.sql"),
    ),
    (
        "0021_edit_primitives",
        include_str!("../sql/migrations/0021_edit_primitives.sql"),
    ),
    (
        "0022_cube_categories",
        include_str!("../sql/migrations/0022_cube_categories.sql"),
    ),
    (
        "0023_propose_column_profile",
        include_str!("../sql/migrations/0023_propose_column_profile.sql"),
    ),
    (
        "0024_metric_lineage_breaching",
        include_str!("../sql/migrations/0024_metric_lineage_breaching.sql"),
    ),
    (
        "0025_fk_inference",
        include_str!("../sql/migrations/0025_fk_inference.sql"),
    ),
    (
        "0026_refresh_all_cubes",
        include_str!("../sql/migrations/0026_refresh_all_cubes.sql"),
    ),
    (
        "0027_dimensional_metrics",
        include_str!("../sql/migrations/0027_dimensional_metrics.sql"),
    ),
    (
        "0028_drop_stale_overloads",
        include_str!("../sql/migrations/0028_drop_stale_overloads.sql"),
    ),
    (
        "0029_cube_refresh_pacing_and_accel_exclude",
        include_str!("../sql/migrations/0029_cube_refresh_pacing_and_accel_exclude.sql"),
    ),
    (
        "0030_fix_refresh_all_cubes_overload",
        include_str!("../sql/migrations/0030_fix_refresh_all_cubes_overload.sql"),
    ),
    (
        "0031_brain_phase0",
        include_str!("../sql/migrations/0031_brain_phase0.sql"),
    ),
    (
        "0032_batch_embed_crawl_prewarm",
        include_str!("../sql/migrations/0032_batch_embed_crawl_prewarm.sql"),
    ),
    (
        "0033_crawl_exclude_operational_schemas",
        include_str!("../sql/migrations/0033_crawl_exclude_operational_schemas.sql"),
    ),
    (
        "0034_brain_acl_management",
        include_str!("../sql/migrations/0034_brain_acl_management.sql"),
    ),
    (
        "0035_fast_fingerprint_materialize_sample",
        include_str!("../sql/migrations/0035_fast_fingerprint_materialize_sample.sql"),
    ),
    (
        "0036_fingerprint_reltuples_rowcount",
        include_str!("../sql/migrations/0036_fingerprint_reltuples_rowcount.sql"),
    ),
    (
        "0037_fingerprint_always_materialize",
        include_str!("../sql/migrations/0037_fingerprint_always_materialize.sql"),
    ),
    (
        "0038_fingerprint_force_heap_scan",
        include_str!("../sql/migrations/0038_fingerprint_force_heap_scan.sql"),
    ),
    (
        "0039_route_overlay",
        include_str!("../sql/migrations/0039_route_overlay.sql"),
    ),
    (
        "0040_route_optimization_candidates",
        include_str!("../sql/migrations/0040_route_optimization_candidates.sql"),
    ),
    (
        "0041_route_shape_samples",
        include_str!("../sql/migrations/0041_route_shape_samples.sql"),
    ),
    (
        "0042_route_optimize_runs",
        include_str!("../sql/migrations/0042_route_optimize_runs.sql"),
    ),
    (
        "0043_cube_refresh_resource_cap",
        include_str!("../sql/migrations/0043_cube_refresh_resource_cap.sql"),
    ),
    (
        "0044_cold_tier_object_store",
        include_str!("../sql/migrations/0044_cold_tier_object_store.sql"),
    ),
    (
        "0045_cold_tier_keep_policy",
        include_str!("../sql/migrations/0045_cold_tier_keep_policy.sql"),
    ),
    (
        "0046_brain_remote_sources",
        include_str!("../sql/migrations/0046_brain_remote_sources.sql"),
    ),
    (
        "0047_brain_sync_orchestration",
        include_str!("../sql/migrations/0047_brain_sync_orchestration.sql"),
    ),
    (
        "0048_brain_kg_enrichment",
        include_str!("../sql/migrations/0048_brain_kg_enrichment.sql"),
    ),
    (
        "0049_brain_source_delete_and_relations",
        include_str!("../sql/migrations/0049_brain_source_delete_and_relations.sql"),
    ),
    (
        "0050_brain_agent_retrieval",
        include_str!("../sql/migrations/0050_brain_agent_retrieval.sql"),
    ),
    (
        "0051_brain_chunk_entities",
        include_str!("../sql/migrations/0051_brain_chunk_entities.sql"),
    ),
    (
        "0052_brain_ner_pass",
        include_str!("../sql/migrations/0052_brain_ner_pass.sql"),
    ),
    (
        "0053_brain_enrich_clean_and_filter",
        include_str!("../sql/migrations/0053_brain_enrich_clean_and_filter.sql"),
    ),
    (
        "0054_brain_related_shared_entities",
        include_str!("../sql/migrations/0054_brain_related_shared_entities.sql"),
    ),
    (
        "0055_brain_ner_full_coverage",
        include_str!("../sql/migrations/0055_brain_ner_full_coverage.sql"),
    ),
    (
        "0056_brain_entity_denoise",
        include_str!("../sql/migrations/0056_brain_entity_denoise.sql"),
    ),
    (
        "0057_brain_related_tfidf",
        include_str!("../sql/migrations/0057_brain_related_tfidf.sql"),
    ),
    (
        "0058_brain_entity_normalization",
        include_str!("../sql/migrations/0058_brain_entity_normalization.sql"),
    ),
    (
        "0059_brain_norm_cache",
        include_str!("../sql/migrations/0059_brain_norm_cache.sql"),
    ),
    (
        "0060_brain_shared_idf_floor",
        include_str!("../sql/migrations/0060_brain_shared_idf_floor.sql"),
    ),
    (
        "0061_brain_norm_preserve",
        include_str!("../sql/migrations/0061_brain_norm_preserve.sql"),
    ),
    (
        "0062_brain_query_sources",
        include_str!("../sql/migrations/0062_brain_query_sources.sql"),
    ),
    (
        "0063_brain_structured_edges",
        include_str!("../sql/migrations/0063_brain_structured_edges.sql"),
    ),
    (
        "0064_brain_enrich_source",
        include_str!("../sql/migrations/0064_brain_enrich_source.sql"),
    ),
    (
        "0065_brain_enrich_pending_fair",
        include_str!("../sql/migrations/0065_brain_enrich_pending_fair.sql"),
    ),
    (
        "0066_vector_sources_tier",
        include_str!("../sql/migrations/0066_vector_sources_tier.sql"),
    ),
    (
        "0067_brain_search_filtered",
        include_str!("../sql/migrations/0067_brain_search_filtered.sql"),
    ),
    (
        "0068_brain_doc_type_facets",
        include_str!("../sql/migrations/0068_brain_doc_type_facets.sql"),
    ),
    (
        "0069_fireflies_mcp_capability",
        include_str!("../sql/migrations/0069_fireflies_mcp_capability.sql"),
    ),
    (
        "0070_agent_loop",
        include_str!("../sql/migrations/0070_agent_loop.sql"),
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
