#!/usr/bin/env bash
# Rebuild Rvbbit adaptive-route training data from forced executor benchmarks.
#
# This runs each routing-relevant benchmark suite at several sizes with:
#   rvbbit_native_forced, rvbbit_duck_forced,
#   rvbbit_datafusion_mem_forced, rvbbit_datafusion_forced,
#   rvbbit_pg_heap_forced
#
# It then merges the suite/size profiles into one combined route profile.
#
# Defaults are intentionally broad enough to capture row-count crossover
# behavior, but every list can be overridden from the environment.
#
# Examples:
#   ./bench/rebuild_rvbbit_route_training.sh
#
#   CLICKBENCH_LIMITS="50000 500000" TPCH_SCALES="0.1" \
#     BENCH_REPEATS=1 ./bench/rebuild_rvbbit_route_training.sh
#
#   DRY_RUN=1 ./bench/rebuild_rvbbit_route_training.sh
#
# Optional env:
#   ROUTE_TRAIN_SUITES      default: clickbench,tpch
#   CLICKBENCH_LIMITS       default: 5000 50000 500000 5000000
#   TPCH_SCALES             default: 0.05 0.1 0.33 1
#   BENCH_REPEATS           passed through, default is suite default
#   BENCH_TIMEOUT           passed through, default is suite default
#   CLICKBENCH_QUERIES      optional query subset for ClickBench
#   TPCH_QUERIES            optional query subset for TPC-H
#   SKIP_DOWNLOAD           passed through to ClickBench
#   RVBBIT_ROUTE_PROFILE_OUT default: bench/rvbbit_route_profile.json
#   RVBBIT_ROUTE_IMPORT     set to 1 to import/activate the merged profile
#   RVBBIT_ROUTE_PROFILE_NAME default: bench-combined
#   RVBBIT_ROUTE_INCLUDE_HIVE default: 1; set 0 to skip Hive forced paths/layouts
#   RVBBIT_COMPACT_KEEP_HEAP  default: 1 for route training
#   CONTINUE_ON_ERROR       set to 1 to merge successful profiles after failures
#   DRY_RUN                 set to 1 to print commands without running them

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

COMPOSE="docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml"
DEFAULT_FORCED_SYSTEMS="rvbbit_native_forced,rvbbit_duck_forced,rvbbit_datafusion_mem_forced,rvbbit_datafusion_forced,rvbbit_pg_heap_forced"
if [ "${RVBBIT_ROUTE_INCLUDE_HIVE:-1}" != "0" ]; then
    DEFAULT_FORCED_SYSTEMS="rvbbit_native_forced,rvbbit_duck_forced,rvbbit_duck_hive_forced,rvbbit_datafusion_mem_forced,rvbbit_datafusion_forced,rvbbit_datafusion_hive_forced,rvbbit_pg_heap_forced"
fi
FORCED_SYSTEMS="${RVBBIT_ROUTE_FORCED_SYSTEMS:-${DEFAULT_FORCED_SYSTEMS}}"

SUITES="${ROUTE_TRAIN_SUITES:-clickbench,tpch}"
CLICKBENCH_LIMITS="${CLICKBENCH_LIMITS:-5000 50000 500000 5000000}"
TPCH_SCALES="${TPCH_SCALES:-0.05 0.1 0.33 1}"
FINAL_PROFILE="${RVBBIT_ROUTE_PROFILE_OUT:-bench/rvbbit_route_profile.json}"
PROFILE_NAME="${RVBBIT_ROUTE_PROFILE_NAME:-bench-combined}"
DRY_RUN="${DRY_RUN:-}"
CONTINUE_ON_ERROR="${CONTINUE_ON_ERROR:-}"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31mXX %s\033[0m\n' "$*" >&2; exit 1; }

contains_item() {
    local needle="$1"
    local list="${2//,/ }"
    local item
    for item in ${list}; do
        [ "${item}" = "${needle}" ] && return 0
    done
    return 1
}

scale_label() {
    local value="$1"
    value="${value//./_}"
    echo "${value}"
}

run_cmd() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    if [ -z "${DRY_RUN}" ]; then
        "$@"
    fi
}

run_env_cmd() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    if [ -z "${DRY_RUN}" ]; then
        env "$@"
    fi
}

record_profile() {
    local profile="$1"
    PROFILES+=("${profile}")
}

run_clickbench_size() {
    local limit="$1"
    local out="bench/clickbench/results/rvbbit_route_profile.clickbench_${limit}.json"
    local results="bench/clickbench/results/rvbbit_route_results.clickbench_${limit}.json"
    local env_args=(
        "BENCH_LIMIT=${limit}"
        "BENCH_SYSTEMS=${FORCED_SYSTEMS}"
        "RVBBIT_COMPACT_KEEP_HEAP=${RVBBIT_COMPACT_KEEP_HEAP:-1}"
        "RVBBIT_ROUTE_PROFILE_OUT=${out}"
        "RVBBIT_ROUTE_RESULTS_OUT=${results}"
    )
    [ -n "${BENCH_REPEATS:-}" ] && env_args+=("BENCH_REPEATS=${BENCH_REPEATS}")
    [ -n "${BENCH_TIMEOUT:-}" ] && env_args+=("BENCH_TIMEOUT=${BENCH_TIMEOUT}")
    [ -n "${CLICKBENCH_QUERIES:-}" ] && env_args+=("BENCH_QUERIES=${CLICKBENCH_QUERIES}")
    [ -n "${SKIP_DOWNLOAD:-}" ] && env_args+=("SKIP_DOWNLOAD=${SKIP_DOWNLOAD}")

    say "ClickBench route training: BENCH_LIMIT=${limit}"
    run_env_cmd "${env_args[@]}" ./bench/clickbench/build_rvbbit_route_profile.sh || return
    record_profile "${out}"
}

run_tpch_scale() {
    local scale="$1"
    local label
    label="$(scale_label "${scale}")"
    local out="bench/tpch/results/rvbbit_route_profile.tpch_sf${label}.json"
    local results="bench/tpch/results/rvbbit_route_results.tpch_sf${label}.json"
    local env_args=(
        "TPCH_SCALE=${scale}"
        "BENCH_SYSTEMS=${FORCED_SYSTEMS}"
        "RVBBIT_COMPACT_KEEP_HEAP=${RVBBIT_COMPACT_KEEP_HEAP:-1}"
        "RVBBIT_ROUTE_PROFILE_OUT=${out}"
        "RVBBIT_ROUTE_RESULTS_OUT=${results}"
    )
    [ -n "${BENCH_REPEATS:-}" ] && env_args+=("BENCH_REPEATS=${BENCH_REPEATS}")
    [ -n "${BENCH_TIMEOUT:-}" ] && env_args+=("BENCH_TIMEOUT=${BENCH_TIMEOUT}")
    [ -n "${TPCH_QUERIES:-}" ] && env_args+=("BENCH_QUERIES=${TPCH_QUERIES}")

    say "TPC-H route training: TPCH_SCALE=${scale}"
    run_env_cmd "${env_args[@]}" ./bench/tpch/build_rvbbit_route_profile.sh || return
    record_profile "${out}"
}

run_or_handle_failure() {
    local label="$1"
    shift
    if "$@"; then
        return 0
    fi
    if [ -n "${CONTINUE_ON_ERROR}" ]; then
        warn "${label} failed; continuing because CONTINUE_ON_ERROR is set"
        return 0
    fi
    die "${label} failed"
}

command -v docker >/dev/null || die "docker not found in PATH"
[ -f "docker/docker-compose.yml" ] || die "expected repo root"

say "Rvbbit route training rebuild"
echo "   suites        : ${SUITES}"
echo "   forced systems: ${FORCED_SYSTEMS}"
echo "   click limits  : ${CLICKBENCH_LIMITS}"
echo "   tpch scales   : ${TPCH_SCALES}"
echo "   repeats       : ${BENCH_REPEATS:-suite default}"
echo "   timeout       : ${BENCH_TIMEOUT:-suite default}"
echo "   final profile : ${FINAL_PROFILE}"
echo "   import active : ${RVBBIT_ROUTE_IMPORT:-0}"
if [ -n "${DRY_RUN}" ]; then
    echo "   mode          : dry run"
fi

PROFILES=()

if contains_item "clickbench" "${SUITES}"; then
    for limit in ${CLICKBENCH_LIMITS//,/ }; do
        [ -n "${limit}" ] || continue
        run_or_handle_failure "ClickBench ${limit}" run_clickbench_size "${limit}"
    done
fi

if contains_item "tpch" "${SUITES}"; then
    for scale in ${TPCH_SCALES//,/ }; do
        [ -n "${scale}" ] || continue
        run_or_handle_failure "TPC-H sf=${scale}" run_tpch_scale "${scale}"
    done
fi

if contains_item "tatp" "${SUITES}"; then
    warn "TATP is transactional and does not currently produce forced route profiles; skipping"
fi

[ "${#PROFILES[@]}" -gt 0 ] || die "no route profiles were produced"

say "Merging ${#PROFILES[@]} route profiles"
merge_env=("RVBBIT_ROUTE_PROFILE_OUT=${FINAL_PROFILE}")
run_env_cmd "${merge_env[@]}" ./bench/build_combined_rvbbit_route_profile.sh "${PROFILES[@]}"

say "Evaluating merged profile"
final_container="/bench/${FINAL_PROFILE#bench/}"
run_cmd ${COMPOSE} exec -T bench python /bench/rvbbit_route_eval.py --profile "${final_container}"

if [ "${RVBBIT_ROUTE_IMPORT:-0}" = "1" ]; then
    say "Importing merged profile as ${PROFILE_NAME}"
    run_cmd ${COMPOSE} exec -T bench python /bench/rvbbit_route_load_profile.py \
        --profile "${final_container}" \
        --name "${PROFILE_NAME}"
fi

say "Done"
echo "route profile: ${FINAL_PROFILE}"
printf 'input profiles:\n'
printf '  %s\n' "${PROFILES[@]}"
