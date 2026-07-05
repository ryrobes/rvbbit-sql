#!/usr/bin/env bash
# Export the CURRENT trained rvbbit.route_model rows into a factory-seed
# migration so the extension ships with them ("canon" routing models).
#
# Workflow: train on the bench box (benches + rvbbit.route_self_train()) until
# happy -> run this -> review + commit the regenerated migration. Fresh installs
# get the models via rvbbit.migrate(); existing installs keep their own (the
# seed INSERTs use ON CONFLICT (engine) DO NOTHING, so a locally retrained
# model always wins — the factory model is only a warm prior).
#
# The model is pure data (JSONB tree ensembles evaluated in Rust), so shipping
# it is just shipping rows. n_samples/notes are preserved for provenance; the
# notes are prefixed with 'factory-seed' so a shipped model is distinguishable
# from a locally trained one (and rvbbit.train_route_model() overwrites it).
#
# Usage:
#   ./scripts/export_route_model_seed.sh                 # from the bench container DB
#   DSN=postgresql://... ./scripts/export_route_model_seed.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT="crates/pg_rvbbit/sql/migrations/0130_route_model_factory_seed.sql"
PSQL=(docker compose -f docker/docker-compose.yml exec -T pg-rvbbit psql -U postgres -d bench)
[ -n "${DSN:-}" ] && PSQL=(psql "${DSN}")

{
    cat <<'HEADER'
-- 0130_route_model_factory_seed
--
-- Factory-trained ML routing models ("canon"): per-engine gradient-boosted
-- latency models trained on the reference bench box (ClickBench + TPC-H +
-- TPC-DS via rvbbit.route_self_train). Pure data — JSONB tree ensembles the
-- router evaluates in-process (router.rs ml_models / route_model.rs).
--
-- Seed semantics: ON CONFLICT (engine) DO NOTHING — an install that has ever
-- trained its own models keeps them; the factory rows only warm-start fresh
-- installs. rvbbit.route_self_train() retrains from local traffic and
-- OVERWRITES these rows, so the seed self-corrects to local hardware/scale.
-- Inert unless rvbbit.route_ml_enabled = on.
--
-- GENERATED FILE — do not hand-edit. Regenerate with:
--   ./scripts/export_route_model_seed.sh
HEADER
    "${PSQL[@]}" -tA -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'INSERT INTO rvbbit.route_model (engine, params, feature_schema, n_samples, trained_at, notes) VALUES ('
    || quote_literal(engine) || ', '
    || quote_literal(params::text) || '::jsonb, '
    || coalesce(feature_schema::text, '1') || ', '
    || coalesce(n_samples::text, 'NULL') || ', now(), '
    || quote_literal('factory-seed ' || coalesce(notes, ''))
    || ') ON CONFLICT (engine) DO NOTHING;'
FROM rvbbit.route_model
ORDER BY engine;
SQL
} > "${OUT}"

echo "wrote ${OUT} ($(du -h "${OUT}" | cut -f1), $(grep -c '^INSERT' "${OUT}") engines)"
echo "remember: it must be registered in crates/pg_rvbbit/src/migrations.rs (0130 entry)"
