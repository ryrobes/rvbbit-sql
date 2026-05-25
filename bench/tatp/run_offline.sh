#!/usr/bin/env bash
# TATP-style transactional runner.
#
# Usage:
#   ./bench/tatp/run_offline.sh
#   TATP_SUBSCRIBERS=100000 TATP_TXNS=50000 TATP_CLIENTS=4 ./bench/tatp/run_offline.sh
#   TATP_TABLE_AM=heap BENCH_SYSTEMS=rvbbit,pg_baseline ./bench/tatp/run_offline.sh
#   RVBBIT_RESET_EXTENSION=1 ./bench/tatp/run_offline.sh
#
# Flags:
#   --reset-rvbbit-extension  same as RVBBIT_RESET_EXTENSION=1
#   --skip-load               same as SKIP_LOAD=1

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
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_FILE="bench/tatp/results/tatp_${SUBSCRIBERS}_${TXNS}_${CLIENTS}_${STAMP}.txt"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
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

for arg in "$@"; do
    case "${arg}" in
        --reset-rvbbit-extension|--clear-rvbbit-system-data)
            RVBBIT_RESET_EXTENSION=1
            ;;
        --skip-load)
            export SKIP_LOAD=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: ${arg}"
            ;;
    esac
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
echo "   rvbbit reset: $(env_on "${RVBBIT_RESET_EXTENSION}" && echo destructive || echo preserve-system-data)"

say "starting competitor containers (profile=bench)"
${COMPOSE} --profile bench up -d
sleep 5

if [[ ",${SYSTEMS}," == *",rvbbit,"* ]]; then
    if env_on "${RVBBIT_RESET_EXTENSION}"; then
        say "resetting pg_rvbbit extension (DESTRUCTIVE: drops rvbbit system/catalog data)"
        ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 <<'SQL'
DROP EXTENSION IF EXISTS pg_rvbbit CASCADE;
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

say "report saved to ${REPORT_FILE}"
echo "raw JSON at bench/tatp/results/last_run.json"
