#!/usr/bin/env bash
# Merge suite-specific Rvbbit route profiles into one router input.
#
# Usage:
#   ./bench/build_combined_rvbbit_route_profile.sh \
#     bench/clickbench/results/rvbbit_route_profile.clickbench.json \
#     bench/tpch/results/rvbbit_route_profile.tpch.json
#
# Optional env:
#   RVBBIT_ROUTE_PROFILE_OUT defaults to bench/rvbbit_route_profile.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if [ "$#" -lt 1 ]; then
    echo "usage: $0 PROFILE [PROFILE ...]" >&2
    exit 2
fi

OUT_HOST="${RVBBIT_ROUTE_PROFILE_OUT:-bench/rvbbit_route_profile.json}"
OUT_CONTAINER="/bench/${OUT_HOST#bench/}"

ARGS=()
for profile in "$@"; do
    case "${profile}" in
        /bench/*) ARGS+=(--profile "${profile}") ;;
        bench/*) ARGS+=(--profile "/bench/${profile#bench/}") ;;
        *) ARGS+=(--profile "${profile}") ;;
    esac
done

docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml \
    exec -T bench python /bench/rvbbit_route_merge.py \
    "${ARGS[@]}" \
    --output "${OUT_CONTAINER}"

echo "route profile: ${OUT_HOST}"
