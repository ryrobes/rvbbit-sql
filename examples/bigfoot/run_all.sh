#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DSN="${RVBBIT_DSN:-postgresql://postgres:rvbbit@localhost:55433/bench}"
SAMPLE_ROWS="${BIGFOOT_SAMPLE_ROWS:-500}"
CLASSIFY_ROWS="${BIGFOOT_CLASSIFY_ROWS:-250}"
EXTRACT_ROWS="${BIGFOOT_EXTRACT_ROWS:-12}"
KG_ROWS="${BIGFOOT_KG_ROWS:-250}"
CAPABILITY_ENTITY_ROWS="${BIGFOOT_CAPABILITY_ENTITY_ROWS:-8}"
CAPABILITY_RERANK_CANDIDATES="${BIGFOOT_CAPABILITY_RERANK_CANDIDATES:-24}"
CAPABILITY_CLASSIFY_ROWS="${BIGFOOT_CAPABILITY_CLASSIFY_ROWS:-8}"
LIVE_ROWS="${BIGFOOT_LIVE_ROWS:-3}"
TRAIN_ESTIMATORS="${BIGFOOT_TRAIN_ESTIMATORS:-64}"
TRAIN_SEED="${BIGFOOT_TRAIN_SEED:-13}"
TRAIN_WAIT="${BIGFOOT_TRAIN_WAIT:-180}"

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
    -v "capability_entity_rows=${CAPABILITY_ENTITY_ROWS}" \
    -v "capability_rerank_candidates=${CAPABILITY_RERANK_CANDIDATES}" \
    -v "capability_classify_rows=${CAPABILITY_CLASSIFY_ROWS}" \
    -v "live_rows=${LIVE_ROWS}" \
    -v "train_estimators=${TRAIN_ESTIMATORS}" \
    -v "train_seed=${TRAIN_SEED}" \
    -v "train_wait_seconds=${TRAIN_WAIT}" \
    "$@" \
    -f "${ROOT}/examples/bigfoot/${file}"
}

run_sql 00_load.sql
run_sql 01_profile.sql
run_sql 02_retrieval.sql
run_sql 03_semantic_map.sql
run_sql 04_knowledge_graph.sql
run_sql 06_capability_operators.sql

if [[ "${BIGFOOT_LIVE:-0}" == "1" ]]; then
  run_sql 07_live_triples_receipts.sql
else
  echo
  echo "== skipping 07_live_triples_receipts.sql (set BIGFOOT_LIVE=1 to run provider-backed calls)"
fi

if [[ "${BIGFOOT_TRAIN:-0}" == "1" ]]; then
  run_sql 08_predict_class.sql
else
  echo
  echo "== skipping 08_predict_class.sql (set BIGFOOT_TRAIN=1; needs an rvbbit-trainer worker -- see README)"
fi
