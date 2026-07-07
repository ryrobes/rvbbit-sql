#!/usr/bin/env bash
set -euo pipefail

# Everything resolves relative to this script, so it works both from a repo
# checkout and as a standalone download (curl -fsSL https://rvbbit.ai/bigfoot/run_all.sh):
# any notebook SQL file not sitting next to it is fetched on first run.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${BIGFOOT_BASE_URL:-https://rvbbit.ai/bigfoot}"
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
CSV_PATH="${BIGFOOT_CSV:-${SCRIPT_DIR}/bigfoot_sightings.csv}"
CSV_URL="${BIGFOOT_CSV_URL:-https://rvbbit.ai/data/bigfoot_sightings.csv}"

SQL_FILES=(00_load.sql 01_profile.sql 02_retrieval.sql 03_semantic_map.sql
           04_knowledge_graph.sql 06_capability_operators.sql
           07_live_triples_receipts.sql 08_predict_class.sql)
for f in "${SQL_FILES[@]}"; do
  if [[ ! -f "${SCRIPT_DIR}/${f}" ]]; then
    echo "== fetching ${f}"
    curl -fsSL "${BASE_URL}/${f}" -o "${SCRIPT_DIR}/${f}"
  fi
done

if [[ ! -f "${CSV_PATH}" ]]; then
  echo "== fetching BFRO sightings CSV (~14MB) -> ${CSV_PATH}"
  curl -fsSL "${CSV_URL}" -o "${CSV_PATH}"
fi

run_sql() {
  local file="$1"
  shift
  echo
  echo "== ${file}"
  psql "${DSN}" \
    -v "csv_path=${CSV_PATH}" \
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
    -f "${SCRIPT_DIR}/${file}"
}

require_capability_operators() {
  local missing
  missing="$(
    psql "${DSN}" -X -A -t -v ON_ERROR_STOP=1 <<'SQL'
WITH required(name) AS (
  VALUES
    ('extract_entities'),
    ('contains_entity'),
    ('has_pii'),
    ('semantic_score'),
    ('emotion')
)
SELECT COALESCE(string_agg(r.name, ', ' ORDER BY r.name), '')
FROM required r
LEFT JOIN rvbbit.operators o ON o.name = r.name
WHERE o.name IS NULL;
SQL
  )"
  if [[ -n "${missing}" ]]; then
    cat >&2 <<'EOF'

Missing capability operators for 06_capability_operators.sql.
Install the required Warren packs first (plain SQL, via psql):

  SELECT rvbbit.deploy_catalog_capability('extract/gliner-medium-v2.1',     '{"capability":true,"docker":true}'::jsonb);
  SELECT rvbbit.deploy_catalog_capability('rerank/bge-reranker-v2-m3',      '{"capability":true,"docker":true}'::jsonb);
  SELECT rvbbit.deploy_catalog_capability('classify/emotion-distilroberta', '{"capability":true,"docker":true}'::jsonb);

  -- watch until all three report 'completed':
  SELECT name, status, coalesce(phase,'') FROM rvbbit.warren_jobs ORDER BY created_at DESC LIMIT 3;

To run only the non-capability notebook sections, set BIGFOOT_SKIP_CAPABILITIES=1.
EOF
    echo "Missing operators: ${missing}" >&2
    exit 1
  fi
}

run_sql 00_load.sql
run_sql 01_profile.sql
run_sql 02_retrieval.sql
run_sql 03_semantic_map.sql
run_sql 04_knowledge_graph.sql

if [[ "${BIGFOOT_SKIP_CAPABILITIES:-0}" == "1" ]]; then
  echo
  echo "== skipping 06_capability_operators.sql (BIGFOOT_SKIP_CAPABILITIES=1)"
else
  require_capability_operators
  run_sql 06_capability_operators.sql
fi

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
