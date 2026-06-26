#!/usr/bin/env bash
# TATP-style transactional runner.
#
# Usage:
#   ./bench/tatp/run_offline.sh
#   TATP_SUBSCRIBERS=100000 TATP_TXNS=50000 TATP_CLIENTS=4 ./bench/tatp/run_offline.sh
#   TATP_TABLE_AM=heap BENCH_SYSTEMS=rvbbit,pg_baseline ./bench/tatp/run_offline.sh
#   RVBBIT_RESET_EXTENSION=1 ./bench/tatp/run_offline.sh
#   ./bench/tatp/run_offline.sh --rebuild --reset-rvbbit-extension
#   ./bench/tatp/run_offline.sh --test-name nightly-main
#
# Flags:
#   --reset-rvbbit-extension  same as RVBBIT_RESET_EXTENSION=1
#   --load-route-profile      same as RVBBIT_LOAD_ROUTE_PROFILE=1
#   --skip-load               same as SKIP_LOAD=1
#   --test-name NAME          same as BENCH_TEST_NAME=NAME
#   --name NAME               alias for --test-name
#   --rebuild                 same as BENCH_REBUILD=1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

COMPOSE="docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml"
SYSTEMS="${BENCH_SYSTEMS:-rvbbit,pg_baseline,citus,hydra,alloydb}"
SUBSCRIBERS="${TATP_SUBSCRIBERS:-100000}"
TXNS="${TATP_TXNS:-20000}"
CLIENTS="${TATP_CLIENTS:-1}"
TABLE_AM="${TATP_TABLE_AM:-native}"
RVBBIT_RESET_EXTENSION="${RVBBIT_RESET_EXTENSION:-${RESET_RVBBIT_EXTENSION:-}}"
RVBBIT_LOAD_ROUTE_PROFILE="${RVBBIT_LOAD_ROUTE_PROFILE:-}"
BENCH_REBUILD="${BENCH_REBUILD:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${BENCH_RUN_ID:-tatp_${SUBSCRIBERS}_${TXNS}_${CLIENTS}_${STAMP}}"
BENCH_TEST_NAME="${BENCH_TEST_NAME:-tatp}"
BENCH_PERSIST_RESULTS="${BENCH_PERSIST_RESULTS:-1}"
ROW_COUNT=$((SUBSCRIBERS * 11))
REPORT_FILE="bench/tatp/results/tatp_${SUBSCRIBERS}_${TXNS}_${CLIENTS}_${STAMP}.txt"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31mXX %s\033[0m\n' "$*" >&2; exit 1; }
env_on() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}
usage() {
    awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"
}
record_benchmark_history() {
    env_on "${BENCH_PERSIST_RESULTS}" || return 0
    local git_commit git_dirty_arg
    git_commit="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
    if [ -n "$(git status --porcelain 2>/dev/null || true)" ]; then
        git_dirty_arg="--git-dirty"
    else
        git_dirty_arg="--no-git-dirty"
    fi
    say "recording benchmark history (${RUN_ID})"
    if ! ${COMPOSE} exec -T bench python /bench/record_benchmark_run.py \
        --results /bench/tatp/results/last_run.json \
        --results-path bench/tatp/results/last_run.json \
        --report-path "${REPORT_FILE}" \
        --run-id "${RUN_ID}" \
        --test-name "${BENCH_TEST_NAME}" \
        --suite TATP \
        --scale "subscribers=${SUBSCRIBERS}" \
        --row-count "${ROW_COUNT}" \
        --started-at "${STAMP}" \
        --git-commit "${git_commit}" \
        "${git_dirty_arg}" \
        --setting "subscribers=${SUBSCRIBERS}" \
        --setting "row_count=${ROW_COUNT}" \
        --setting "txns=${TXNS}" \
        --setting "clients=${CLIENTS}" \
        --setting "table_am=${TABLE_AM}" \
        --setting "systems=${SYSTEMS}" \
        --setting "skip_load=${SKIP_LOAD:-0}" \
        --setting "rebuild=${BENCH_REBUILD:-0}" \
        --setting "rvbbit_reset_extension=${RVBBIT_RESET_EXTENSION:-0}"; then
        warn "benchmark completed, but history recording failed"
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --reset-rvbbit-extension|--clear-rvbbit-system-data)
            RVBBIT_RESET_EXTENSION=1
            ;;
        --load-route-profile)
            RVBBIT_LOAD_ROUTE_PROFILE=1
            ;;
        --skip-load)
            export SKIP_LOAD=1
            ;;
        --rebuild)
            BENCH_REBUILD=1
            ;;
        --test-name|--name)
            [ "$#" -ge 2 ] || die "$1 requires a value"
            BENCH_TEST_NAME="$2"
            shift
            ;;
        --test-name=*|--name=*)
            BENCH_TEST_NAME="${1#*=}"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
    shift
done

command -v docker >/dev/null || die "docker not found in PATH"
[ -f "docker/docker-compose.yml" ] || die "expected repo root"

say "configuration"
echo "   systems     : ${SYSTEMS}"
echo "   subscribers : ${SUBSCRIBERS}"
echo "   txns/system : ${TXNS}"
echo "   clients     : ${CLIENTS}"
echo "   table AM    : ${TABLE_AM}"
echo "   report file : ${REPORT_FILE}"
echo "   run id      : ${RUN_ID}"
echo "   test name   : ${BENCH_TEST_NAME}"
echo "   rvbbit reset: $(env_on "${RVBBIT_RESET_EXTENSION}" && echo destructive || echo preserve-system-data)"
echo "   route import: $(env_on "${RVBBIT_LOAD_ROUTE_PROFILE}" && echo yes || echo no)"
echo "   rebuild     : $(env_on "${BENCH_REBUILD}" && echo yes || echo no)"
echo "   persist     : $(env_on "${BENCH_PERSIST_RESULTS}" && echo yes || echo no)"

if env_on "${BENCH_REBUILD}"; then
    say "rebuilding pg-rvbbit + bench images from current source"
    ${COMPOSE} --profile bench build pg-rvbbit bench
fi

say "starting competitor containers (profile=bench)"
${COMPOSE} --profile bench up -d
sleep 5

if [[ ",${SYSTEMS}," == *",rvbbit,"* ]]; then
    if env_on "${RVBBIT_RESET_EXTENSION}"; then
        say "resetting pg_rvbbit extension (DESTRUCTIVE: drops rvbbit system/catalog data)"
        ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 <<'SQL'
DROP EXTENSION IF EXISTS pg_rvbbit CASCADE;
DROP EVENT TRIGGER IF EXISTS rvbbit_on_create_table;
DROP EVENT TRIGGER IF EXISTS rvbbit_on_drop_table;
DROP EVENT TRIGGER IF EXISTS rvbbit_partition_dirty_triggers_on_alter;
SET rvbbit.duck_backend = off;
DROP SCHEMA IF EXISTS rvbbit CASCADE;
CREATE SCHEMA rvbbit;
CREATE EXTENSION pg_rvbbit;
SQL
    else
        say "ensuring pg_rvbbit extension (preserves rvbbit system/catalog data)"
        ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_rvbbit;
ALTER EXTENSION pg_rvbbit UPDATE;
SQL
    fi

    if env_on "${RVBBIT_LOAD_ROUTE_PROFILE}" && [ -f "bench/rvbbit_route_profile.json" ]; then
        say "loading Rvbbit route profile"
        ${COMPOSE} exec -T bench python /bench/rvbbit_route_load_profile.py \
            --profile /bench/rvbbit_route_profile.json \
            --name bench-combined
    fi
fi

say "running TATP-style workload"
mkdir -p "$(dirname "${REPORT_FILE}")"
${COMPOSE} exec -T \
    -e "BENCH_SYSTEMS=${SYSTEMS}" \
    -e "TATP_SUBSCRIBERS=${SUBSCRIBERS}" \
    -e "TATP_TXNS=${TXNS}" \
    -e "TATP_CLIENTS=${CLIENTS}" \
    -e "TATP_TABLE_AM=${TABLE_AM}" \
    ${SKIP_LOAD:+-e SKIP_LOAD=1} \
    bench python /bench/tatp/run.py | tee "${REPORT_FILE}"

record_benchmark_history

say "report saved to ${REPORT_FILE}"
echo "raw JSON at bench/tatp/results/last_run.json"
