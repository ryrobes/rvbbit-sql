#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DSN="${RVBBIT_DSN:-postgresql://postgres:rvbbit@localhost:55433/bench}"
SAMPLE_ROWS="${BIGFOOT_SAMPLE_ROWS:-500}"
CLASSIFY_ROWS="${BIGFOOT_CLASSIFY_ROWS:-250}"
EXTRACT_ROWS="${BIGFOOT_EXTRACT_ROWS:-12}"
KG_ROWS="${BIGFOOT_KG_ROWS:-250}"
LIVE_ROWS="${BIGFOOT_LIVE_ROWS:-3}"

run_sql() {
  local file="$1"
  shift
  echo
  echo "== ${file}"
  psql "${DSN}" \
    -v "sample_rows=${SAMPLE_ROWS}" \
    -v "classify_rows=${CLASSIFY_ROWS}" \
    -v "extract_rows=${EXTRACT_ROWS}" \
    -v "kg_rows=${KG_ROWS}" \
    -v "live_rows=${LIVE_ROWS}" \
    "$@" \
    -f "${ROOT}/examples/bigfoot/${file}"
}

run_sql 00_load.sql
run_sql 01_profile.sql
run_sql 02_retrieval.sql
run_sql 03_semantic_map.sql
run_sql 04_knowledge_graph.sql

if [[ "${BIGFOOT_LIVE:-0}" == "1" ]]; then
  run_sql 05_live_triples_receipts.sql
else
  echo
  echo "== skipping 05_live_triples_receipts.sql (set BIGFOOT_LIVE=1 to run provider-backed calls)"
fi
