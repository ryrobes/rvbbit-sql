#!/usr/bin/env bash
# Host-side wrapper for the sidecar concurrency harness.
#
# It runs the Python client load test inside the bench container, while this
# shell process samples rvbbit-duck child processes from the pg-rvbbit
# container. Run from the repo root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

COMPOSE="docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${SIDECAR_LOAD_RUN_ID:-sidecar_load_${STAMP}}"
RESULT_DIR="bench/sidecar_load/results"
JSON_OUT="${SIDECAR_LOAD_JSON_OUT:-/bench/sidecar_load/results/${RUN_ID}.json}"
PROC_OUT="${RESULT_DIR}/${RUN_ID}_processes.jsonl"
SAMPLE_INTERVAL="${SIDECAR_LOAD_SAMPLE_INTERVAL_S:-1}"
EXEC_ENV=()

pass_env() {
    local name="$1"
    local value="${!name:-}"
    if [ -n "${value}" ]; then
        EXEC_ENV+=("-e" "${name}=${value}")
    fi
}

for name in \
    RVBBIT_DSN \
    RVBBIT_DUCK_THREADS \
    SIDECAR_LOAD_ARROW_IPC \
    SIDECAR_LOAD_CANDIDATE \
    SIDECAR_LOAD_CLIENTS \
    SIDECAR_LOAD_DUCK_THREADS \
    SIDECAR_LOAD_DURATION_S \
    SIDECAR_LOAD_FAIL_OPEN \
    SIDECAR_LOAD_JSON_OUT \
    SIDECAR_LOAD_PERSISTENT \
    SIDECAR_LOAD_QUERIES \
    SIDECAR_LOAD_SAMPLE_INTERVAL_S \
    SIDECAR_LOAD_STATEMENT_TIMEOUT_S \
    SIDECAR_LOAD_WARMUP_S; do
    pass_env "${name}"
done

mkdir -p "${RESULT_DIR}"

sample_sidecars() {
    ${COMPOSE} exec -T pg-rvbbit sh -lc '
        count=0
        rss=0
        for p in /proc/[0-9]*; do
            [ -r "$p/comm" ] || continue
            comm="$(cat "$p/comm" 2>/dev/null || true)"
            [ "$comm" = "rvbbit-duck" ] || continue
            count=$((count + 1))
            if [ -r "$p/status" ]; then
                r="$(awk "/^VmRSS:/ {print \$2}" "$p/status" 2>/dev/null || true)"
                rss=$((rss + ${r:-0}))
            fi
        done
        printf "%s %s\n" "$count" "$rss"
    '
}

monitor_sidecars() {
    local target_pid="$1"
    : > "${PROC_OUT}"
    while kill -0 "${target_pid}" 2>/dev/null; do
        local sample count rss
        sample="$(sample_sidecars | tr -d "\r" | tail -n 1)"
        count="$(printf "%s" "${sample}" | awk "{print \$1}")"
        rss="$(printf "%s" "${sample}" | awk "{print \$2}")"
        printf '{"ts":"%s","sidecar_count":%s,"sidecar_rss_kb":%s}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${count:-0}" "${rss:-0}" >> "${PROC_OUT}"
        sleep "${SAMPLE_INTERVAL}"
    done
}

echo "== starting pg-rvbbit + bench"
${COMPOSE} up -d pg-rvbbit bench >/dev/null

echo "== running sidecar load harness (${RUN_ID})"
set +e
${COMPOSE} exec -T "${EXEC_ENV[@]}" bench python /bench/sidecar_load/run.py \
    --json-out "${JSON_OUT}" \
    "$@" &
bench_pid=$!
monitor_sidecars "${bench_pid}" &
monitor_pid=$!
wait "${bench_pid}"
status=$?
set -e
wait "${monitor_pid}" 2>/dev/null || true

if [ -s "${PROC_OUT}" ]; then
    echo
    echo "== sidecar process summary"
    python - "${PROC_OUT}" <<'PY'
import json
import statistics
import sys

path = sys.argv[1]
rows = [json.loads(line) for line in open(path) if line.strip()]
counts = [int(row.get("sidecar_count") or 0) for row in rows]
rss = [int(row.get("sidecar_rss_kb") or 0) for row in rows]
if rows:
    print(f"samples       : {len(rows)}")
    print(f"sidecars max  : {max(counts)}")
    print(f"sidecars avg  : {statistics.mean(counts):.2f}")
    print(f"rss max       : {max(rss) / 1024:.1f} MiB")
    print(f"rss avg       : {statistics.mean(rss) / 1024:.1f} MiB")
PY
    echo "process samples saved to ${PROC_OUT}"
fi

exit "${status}"
