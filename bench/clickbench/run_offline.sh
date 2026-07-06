#!/usr/bin/env bash
# Offline ClickBench runner — single entry point.
#
# Brings up every competitor container, downloads hits.parquet if
# missing, loads each system at the requested scale, runs all 43
# queries, and prints a colored grid.
#
# Usage:
#   ./bench/clickbench/run_offline.sh                       # default: 10M rows, all systems
#   BENCH_LIMIT=100000000 ./bench/clickbench/run_offline.sh # full 100M
#   BENCH_SYSTEMS=rvbbit,duckdb,clickhouse \                # subset
#     ./bench/clickbench/run_offline.sh
#   BENCH_QUERIES=Q0,Q1,Q7 ./bench/clickbench/run_offline.sh # subset
#   SKIP_LOAD=1 ./bench/clickbench/run_offline.sh           # reuse existing data
#   SKIP_DOWNLOAD=1 ./bench/clickbench/run_offline.sh       # assume parquet exists
#   RVBBIT_RESET_EXTENSION=1 ./bench/clickbench/run_offline.sh
#                                                            # destructive reset of rvbbit system/catalog data
#   RVBBIT_LOAD_ROUTE_PROFILE=1 ./bench/clickbench/run_offline.sh
#                                                            # import bench/rvbbit_route_profile.json
#                                                            # NOTE: the shipped profile was trained
#                                                            # pre-Phase-1 (sidecar DataFusion era);
#                                                            # numbers are stale, retraining recommended.
#   BENCH_SYSTEMS=rvbbit,rvbbit_native_forced,rvbbit_datafusion_mem_forced ./bench/clickbench/run_offline.sh
#   BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_datafusion_vortex_forced,rvbbit_duck_vortex_forced ./bench/clickbench/run_offline.sh
#                                                            # forced-route coverage over canonical/vortex layouts
#   RVBBIT_DUCK_HOT_VALIDATE=1 BENCH_SYSTEMS=rvbbit,rvbbit_native_forced ./bench/clickbench/run_offline.sh
#   RVBBIT_DF_INPROCESS=off ./bench/clickbench/run_offline.sh # force legacy sidecar route (A/B vs new)
#   RVBBIT_ACCEL_IDENTITY_MAP=on ./bench/clickbench/run_offline.sh
#                                                            # opt into CTID overlay maps for no-PK mutable tables
#   RVBBIT_COMPACT_SCAN_CHUNK_ROWS=250000 RVBBIT_COMPACT_WRITER_THREADS=8 ./bench/clickbench/run_offline.sh
#                                                            # bulk-load profile: overlap canonical parquet chunk writes
#   RVBBIT_DIRECT_ACCEL_LOAD=1 RVBBIT_DIRECT_ACCEL_CHUNK_ROWS=250000 ./bench/clickbench/run_offline.sh
#                                                            # build canonical files from source chunks instead of heap rescan
#   RVBBIT_GQE_LARGE_ROW_GROUPS=1 RVBBIT_DIRECT_ACCEL_LOAD=1 ./bench/clickbench/run_offline.sh
#                                                            # GQE experiment: fewer/larger parquet files without changing normal defaults
#   RVBBIT_DIRECT_ACCEL_LOAD=1 RVBBIT_DIRECT_ACCEL_METADATA_PROFILE=minimal ./bench/clickbench/run_offline.sh
#                                                            # faster direct-load canonical files with thin metadata
#   RVBBIT_DIRECT_ACCEL_LOAD=1 RVBBIT_DIRECT_ACCEL_METADATA_PROFILE=minimal RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=async ./bench/clickbench/run_offline.sh
#                                                            # canonical accelerator ready first; Hive/Vortex variants build in background
#   RVBBIT_DIRECT_ACCEL_LOAD=1 RVBBIT_DIRECT_ACCEL_STAGING_MODE=offset_chunks ./bench/clickbench/run_offline.sh
#                                                            # A/B older LIMIT/OFFSET source chunk staging
#   RVBBIT_DIRECT_ACCEL_LOAD=1 RVBBIT_DIRECT_ACCEL_STAGING_MODE=source ./bench/clickbench/run_offline.sh
#                                                            # import original /data/hits.parquet directly; no source staging file
#   RVBBIT_DIRECT_ACCEL_LOAD=1 RVBBIT_DIRECT_ACCEL_STAGING_MODE=source RVBBIT_DIRECT_ACCEL_METADATA_PROFILE=minimal RVBBIT_COMPACT_WRITER_THREADS=8 RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=async ./bench/clickbench/run_offline.sh
#                                                            # current fast RVBBIT bulk-load profile
#   ./bench/clickbench/run_offline.sh --rebuild --reset-rvbbit-extension
#                                                            # full bench against current source
#   ./bench/clickbench/run_offline.sh --test-name nightly-main
#                                                            # group persisted benchmark history
#
# Flags:
#   --reset-rvbbit-extension  same as RVBBIT_RESET_EXTENSION=1
#   --load-route-profile      same as RVBBIT_LOAD_ROUTE_PROFILE=1
#   --skip-load               same as SKIP_LOAD=1
#   --skip-download           same as SKIP_DOWNLOAD=1
#   --test-name NAME          same as BENCH_TEST_NAME=NAME
#   --name NAME               alias for --test-name
#   --rebuild                 same as BENCH_REBUILD=1 — rebuilds the
#                             pg-rvbbit + bench container images before
#                             starting the bench. Required after pulling
#                             new rvbbit code so the new .so + sidecar
#                             binary are actually in the running container.
#
# Run from the repo root (./rvbbit/).

set -euo pipefail

# ---- Resolve paths ----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

DATA_DIR="bench/columnar_comparison/data"
HITS_URL="https://datasets.clickhouse.com/hits_compatible/hits.parquet"
HITS="${DATA_DIR}/hits.parquet"
GQE_ADAPTER_VERSION_REQUIRED="${RVBBIT_GQE_ADAPTER_VERSION_REQUIRED:-gqe-adapter-v4-date-year-sidecar}"

LIMIT="${BENCH_LIMIT:-10000000}"
SYSTEMS="${BENCH_SYSTEMS:-rvbbit,duckdb,clickhouse,pg_baseline,citus,hydra,alloydb}"
GPU_GQE_SELECTED=0
if [[ ",${SYSTEMS}," == *",rvbbit_gpu_gqe_forced,"* ]]; then
    GPU_GQE_SELECTED=1
fi
GPU_GQE_SKIP_ACTIVE=0
HOST_NVIDIA_GPU=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    HOST_NVIDIA_GPU=1
fi
DOCKER_NVIDIA_RUNTIME=0
if command -v docker >/dev/null 2>&1 \
    && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi '"nvidia"'; then
    DOCKER_NVIDIA_RUNTIME=1
fi
GPU_COMPOSE_READY=0
if [ "${HOST_NVIDIA_GPU}" = "1" ] && [ "${DOCKER_NVIDIA_RUNTIME}" = "1" ]; then
    GPU_COMPOSE_READY=1
fi
COMPOSE="docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml"
GPU_COMPOSE_DISPLAY="off"
GPU_COMPOSE_ALLOWED=1
GQE_HOST_MOUNT_DISPLAY="off"
GQE_IMAGE_DISPLAY="off"
GQE_IMAGE_SELECTED=0
case "${RVBBIT_GPU_GQE_COMPOSE:-auto}" in
    0|false|FALSE|no|NO|off|OFF|disabled|DISABLED)
        GPU_COMPOSE_ALLOWED=0
        ;;
    *) ;;
esac
# GQE capability for AUTO-routed runs: the auto 'rvbbit' system should run on the
# GQE-capable image whenever this box can actually serve GQE, so the router and
# rvbbit.route_self_train() can measure/choose gpu_gqe (the routing gate now
# defaults on and self-gates on runtime availability). Previously the GQE overlay
# was only selected when rvbbit_gpu_gqe_forced was benched, so auto runs always
# used the plain image and GQE was invisible to routing + training. Deliberately
# conservative: requires host GPU + docker nvidia runtime + an ALREADY-BUILT gqe
# image (or RVBBIT_GQE_HOME) — never triggers the multi-hour CUDA toolchain
# build. Opt out with RVBBIT_GPU_GQE_COMPOSE=off.
if [ "${GPU_GQE_SELECTED}" = "0" ] \
    && [[ ",${SYSTEMS}," == *",rvbbit,"* ]] \
    && [ "${GPU_COMPOSE_READY}" = "1" ] \
    && [ "${GPU_COMPOSE_ALLOWED}" = "1" ]; then
    if [ -n "${RVBBIT_GQE_HOME:-}" ] \
        || docker image inspect "${RVBBIT_GQE_PG_IMAGE:-docker-pg-rvbbit-gqe}" >/dev/null 2>&1; then
        # (say() is defined later in the script; plain printf here)
        printf 'auto rvbbit run on a GQE-capable box: using the GPU/GQE image so gpu_gqe can participate in routing + self-training\n'
        GPU_GQE_SELECTED=1
    fi
fi
if [ "${GPU_GQE_SELECTED}" = "1" ]; then
    if [ -n "${RVBBIT_GQE_HOME:-}" ]; then
        COMPOSE="${COMPOSE} -f docker/docker-compose.gqe-host.yml"
        GQE_HOST_MOUNT_DISPLAY="${RVBBIT_GQE_HOME}"
    else
        case "${RVBBIT_GPU_GQE_INSTALL:-auto}" in
            0|false|FALSE|no|NO|off|OFF|disabled|DISABLED)
                GQE_IMAGE_DISPLAY="off"
                ;;
            host|HOST)
                GQE_IMAGE_DISPLAY="host-missing"
                ;;
            1|true|TRUE|yes|YES|on|ON|image|IMAGE)
                COMPOSE="${COMPOSE} -f docker/docker-compose.gqe-image.yml"
                GQE_IMAGE_DISPLAY="${RVBBIT_GQE_PG_IMAGE:-docker-pg-rvbbit-gqe}"
                GQE_IMAGE_SELECTED=1
                ;;
            *)
                if [ "${GPU_COMPOSE_READY}" = "1" ] && [ "${GPU_COMPOSE_ALLOWED}" = "1" ]; then
                    COMPOSE="${COMPOSE} -f docker/docker-compose.gqe-image.yml"
                    GQE_IMAGE_DISPLAY="${RVBBIT_GQE_PG_IMAGE:-docker-pg-rvbbit-gqe} (auto)"
                    GQE_IMAGE_SELECTED=1
                elif [ "${GPU_COMPOSE_READY}" = "1" ]; then
                    GQE_IMAGE_DISPLAY="auto-gpu-compose-off"
                elif [ "${HOST_NVIDIA_GPU}" = "1" ]; then
                    GQE_IMAGE_DISPLAY="auto-no-docker-gpu"
                else
                    GQE_IMAGE_DISPLAY="auto-no-gpu"
                fi
                ;;
        esac
    fi
fi
if [ "${GPU_GQE_SELECTED}" = "1" ]; then
    if [ "${GQE_IMAGE_SELECTED}" = "1" ]; then
        GPU_COMPOSE_DISPLAY="gqe-image"
    elif [ "${GPU_COMPOSE_READY}" = "1" ] && [ "${GPU_COMPOSE_ALLOWED}" = "1" ]; then
        COMPOSE="${COMPOSE} -f docker/docker-compose.gpu.yml"
        GPU_COMPOSE_DISPLAY="on"
    fi
fi
RVBBIT_SELECTED=0
if [[ ",${SYSTEMS}," == *",rvbbit,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_native,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_native_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_hot,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_auto,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_hive_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_vortex_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_hive_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_vortex_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_mem_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_gpu_gqe_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_pg_heap_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_pg_heap,"* ]] || [[ ",${SYSTEMS}," == *",pg_heap,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_native_vortex,"* ]]; then
    RVBBIT_SELECTED=1
fi
HIVE_FORCED_SELECTED=0
if [[ ",${SYSTEMS}," == *",rvbbit_duck_hive_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_hive_forced,"* ]]; then
    HIVE_FORCED_SELECTED=1
fi
VORTEX_FORCED_SELECTED=0
if [[ ",${SYSTEMS}," == *",rvbbit_datafusion_vortex_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_vortex_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_native_vortex,"* ]]; then
    VORTEX_FORCED_SELECTED=1
fi
VORTEX_AUTO_SELECTED=0
case "${RVBBIT_ROUTE_DUCK_VORTEX:-}" in
    0|false|FALSE|no|NO|off|OFF|disabled|DISABLED) ;;
    *)
        if [ "${RVBBIT_SELECTED}" = "1" ]; then
            VORTEX_AUTO_SELECTED=1
        fi
        ;;
esac
HIVE_REFRESH_DEFAULT="off"
if [ "${RVBBIT_SELECTED}" = "1" ]; then
    HIVE_REFRESH_DEFAULT="sync"
fi
HIVE_REFRESH_DISPLAY="${RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD:-${HIVE_REFRESH_DEFAULT}}"
VORTEX_LAYOUT_DISPLAY="${RVBBIT_COMPACT_VORTEX_LAYOUT:-off}"
if { [ "${VORTEX_FORCED_SELECTED}" = "1" ] || [ "${VORTEX_AUTO_SELECTED}" = "1" ]; } && [ -z "${RVBBIT_COMPACT_VORTEX_LAYOUT:-}" ]; then
    VORTEX_LAYOUT_DISPLAY="on"
fi
case "${RVBBIT_GQE_LARGE_ROW_GROUPS:-}" in
    1|true|TRUE|yes|YES|on|ON)
        RVBBIT_GQE_ROW_GROUP_CHUNK_ROWS="${RVBBIT_GQE_ROW_GROUP_CHUNK_ROWS:-1000000}"
        RVBBIT_DIRECT_ACCEL_CHUNK_ROWS="${RVBBIT_DIRECT_ACCEL_CHUNK_ROWS:-${RVBBIT_GQE_ROW_GROUP_CHUNK_ROWS}}"
        RVBBIT_COMPACT_SCAN_CHUNK_ROWS="${RVBBIT_COMPACT_SCAN_CHUNK_ROWS:-${RVBBIT_GQE_ROW_GROUP_CHUNK_ROWS}}"
        export RVBBIT_DIRECT_ACCEL_CHUNK_ROWS RVBBIT_COMPACT_SCAN_CHUNK_ROWS
        ;;
    *) ;;
esac
QUERIES_ENV=()
[ -n "${BENCH_QUERIES:-}" ] && QUERIES_ENV=(-e "BENCH_QUERIES=${BENCH_QUERIES}")
DUCK_HOT_ENV=()
[ -n "${RVBBIT_DUCK_HOT_DEBUG:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_DUCK_HOT_DEBUG=${RVBBIT_DUCK_HOT_DEBUG}")
[ -n "${RVBBIT_DUCK_HOT_VALIDATE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_DUCK_HOT_VALIDATE=${RVBBIT_DUCK_HOT_VALIDATE}")
[ -n "${RVBBIT_DUCK_HOT_MODE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_DUCK_HOT_MODE=${RVBBIT_DUCK_HOT_MODE}")
[ -n "${RVBBIT_ROUTE_PROFILE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_PROFILE=${RVBBIT_ROUTE_PROFILE}")
[ -n "${RVBBIT_ROUTE_TRACE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_TRACE=${RVBBIT_ROUTE_TRACE}")
[ -n "${RVBBIT_ROUTE_LOG:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_LOG=${RVBBIT_ROUTE_LOG}")
[ -n "${RVBBIT_ROUTE_PROFILE_MIN_CONFIDENCE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_PROFILE_MIN_CONFIDENCE=${RVBBIT_ROUTE_PROFILE_MIN_CONFIDENCE}")
[ -n "${RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE=${RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE}")
[ -n "${RVBBIT_ROUTE_DUCK_VECTOR:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DUCK_VECTOR=${RVBBIT_ROUTE_DUCK_VECTOR}")
[ -n "${RVBBIT_ROUTE_DUCK_HIVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DUCK_HIVE=${RVBBIT_ROUTE_DUCK_HIVE}")
[ -n "${RVBBIT_ROUTE_DUCK_VORTEX:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DUCK_VORTEX=${RVBBIT_ROUTE_DUCK_VORTEX}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_MEM:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_MEM=${RVBBIT_ROUTE_DATAFUSION_MEM}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_VECTOR:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_VECTOR=${RVBBIT_ROUTE_DATAFUSION_VECTOR}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_HIVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_HIVE=${RVBBIT_ROUTE_DATAFUSION_HIVE}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_VORTEX:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_VORTEX=${RVBBIT_ROUTE_DATAFUSION_VORTEX}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_VORTEX_ALLOW_TEMPORAL:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_VORTEX_ALLOW_TEMPORAL=${RVBBIT_ROUTE_DATAFUSION_VORTEX_ALLOW_TEMPORAL}")
[ -n "${RVBBIT_ROUTE_HIVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_HIVE=${RVBBIT_ROUTE_HIVE}")
[ -n "${RVBBIT_ROUTE_PG_ROWSTORE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_PG_ROWSTORE=${RVBBIT_ROUTE_PG_ROWSTORE}")
[ -n "${RVBBIT_ROUTE_RVBBIT_NATIVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_RVBBIT_NATIVE=${RVBBIT_ROUTE_RVBBIT_NATIVE}")
[ -n "${RVBBIT_ROUTE_FORCE_CANDIDATE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_FORCE_CANDIDATE=${RVBBIT_ROUTE_FORCE_CANDIDATE}")
[ -n "${RVBBIT_GQE_BIN:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_GQE_BIN=${RVBBIT_GQE_BIN}")
[ -n "${RVBBIT_GQE_ALLOW_RISKY_SHAPES:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_GQE_ALLOW_RISKY_SHAPES=${RVBBIT_GQE_ALLOW_RISKY_SHAPES}")
[ -n "${RVBBIT_NATIVE_ROUTER:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_NATIVE_ROUTER=${RVBBIT_NATIVE_ROUTER}")
[ -n "${RVBBIT_ROUTE_OBSERVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_OBSERVE=${RVBBIT_ROUTE_OBSERVE}")
[ -n "${RVBBIT_ROUTE_EXPLORE_PCT:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_EXPLORE_PCT=${RVBBIT_ROUTE_EXPLORE_PCT}")
[ -n "${RVBBIT_HIVE_LAYOUT:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_HIVE_LAYOUT=${RVBBIT_HIVE_LAYOUT}")
[ -n "${RVBBIT_HOT_STORE_BUDGET_MB:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_HOT_STORE_BUDGET_MB=${RVBBIT_HOT_STORE_BUDGET_MB}")
[ -n "${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_HOT_STORE_ROUTE_MAX_ROWS=${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS}")
# In-process DataFusion vs legacy sidecar route. Default is on (post-Phase-1);
# pass RVBBIT_DF_INPROCESS=off to force the sidecar path for A/B benches.
[ -n "${RVBBIT_DF_INPROCESS:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_DF_INPROCESS=${RVBBIT_DF_INPROCESS}")
LOAD_ENV=()
[ -n "${RVBBIT_COMPACT_KEEP_HEAP:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_KEEP_HEAP=${RVBBIT_COMPACT_KEEP_HEAP}")
[ -n "${RVBBIT_HOT_LOAD_AFTER_LOAD:-}" ] && LOAD_ENV+=(-e "RVBBIT_HOT_LOAD_AFTER_LOAD=${RVBBIT_HOT_LOAD_AFTER_LOAD}")
[ -n "${RVBBIT_HOT_STORE_BUDGET_MB:-}" ] && LOAD_ENV+=(-e "RVBBIT_HOT_STORE_BUDGET_MB=${RVBBIT_HOT_STORE_BUDGET_MB}")
[ -n "${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS:-}" ] && LOAD_ENV+=(-e "RVBBIT_HOT_STORE_ROUTE_MAX_ROWS=${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS}")
[ -n "${RVBBIT_ACCEL_IDENTITY_MAP:-}" ] && LOAD_ENV+=(-e "RVBBIT_ACCEL_IDENTITY_MAP=${RVBBIT_ACCEL_IDENTITY_MAP}")
[ -n "${RVBBIT_ACCEL_IDENTITY_BATCH_ROWS:-}" ] && LOAD_ENV+=(-e "RVBBIT_ACCEL_IDENTITY_BATCH_ROWS=${RVBBIT_ACCEL_IDENTITY_BATCH_ROWS}")
[ -n "${RVBBIT_COMPACT_SCAN_CHUNK_ROWS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_SCAN_CHUNK_ROWS=${RVBBIT_COMPACT_SCAN_CHUNK_ROWS}")
[ -n "${RVBBIT_COMPACT_WRITER_THREADS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_WRITER_THREADS=${RVBBIT_COMPACT_WRITER_THREADS}")
[ -n "${RVBBIT_DIRECT_ACCEL_LOAD:-}" ] && LOAD_ENV+=(-e "RVBBIT_DIRECT_ACCEL_LOAD=${RVBBIT_DIRECT_ACCEL_LOAD}")
[ -n "${RVBBIT_DIRECT_ACCEL_CHUNK_ROWS:-}" ] && LOAD_ENV+=(-e "RVBBIT_DIRECT_ACCEL_CHUNK_ROWS=${RVBBIT_DIRECT_ACCEL_CHUNK_ROWS}")
[ -n "${RVBBIT_DIRECT_ACCEL_STAGING_MODE:-}" ] && LOAD_ENV+=(-e "RVBBIT_DIRECT_ACCEL_STAGING_MODE=${RVBBIT_DIRECT_ACCEL_STAGING_MODE}")
[ -n "${RVBBIT_IMPORT_EPOCH_SECONDS_COLUMNS:-}" ] && LOAD_ENV+=(-e "RVBBIT_IMPORT_EPOCH_SECONDS_COLUMNS=${RVBBIT_IMPORT_EPOCH_SECONDS_COLUMNS}")
[ -n "${RVBBIT_DIRECT_ACCEL_KEEP_CHUNKS:-}" ] && LOAD_ENV+=(-e "RVBBIT_DIRECT_ACCEL_KEEP_CHUNKS=${RVBBIT_DIRECT_ACCEL_KEEP_CHUNKS}")
[ -n "${RVBBIT_DIRECT_ACCEL_METADATA_PROFILE:-}" ] && LOAD_ENV+=(-e "RVBBIT_DIRECT_ACCEL_METADATA_PROFILE=${RVBBIT_DIRECT_ACCEL_METADATA_PROFILE}")
[ -n "${RVBBIT_COMPACT_METADATA_PROFILE:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_METADATA_PROFILE=${RVBBIT_COMPACT_METADATA_PROFILE}")
[ -n "${RVBBIT_COMPACT_TEXT_STATS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_TEXT_STATS=${RVBBIT_COMPACT_TEXT_STATS}")
[ -n "${RVBBIT_COMPACT_PER_GROUP_STATS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_PER_GROUP_STATS=${RVBBIT_COMPACT_PER_GROUP_STATS}")
[ -n "${RVBBIT_COMPACT_VALUE_BITMAPS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_VALUE_BITMAPS=${RVBBIT_COMPACT_VALUE_BITMAPS}")
[ -n "${RVBBIT_COMPACT_TEXT_DICTIONARIES:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_TEXT_DICTIONARIES=${RVBBIT_COMPACT_TEXT_DICTIONARIES}")
[ -n "${RVBBIT_PARQUET_BLOOM:-}" ] && LOAD_ENV+=(-e "RVBBIT_PARQUET_BLOOM=${RVBBIT_PARQUET_BLOOM}")
[ -n "${RVBBIT_COMPACT_VARIANTS_SYNC:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_VARIANTS_SYNC=${RVBBIT_COMPACT_VARIANTS_SYNC}")
if [ "${HIVE_FORCED_SELECTED}" = "1" ]; then
    LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_LAYOUT=${RVBBIT_COMPACT_HIVE_LAYOUT:-on}")
elif [ -n "${RVBBIT_COMPACT_HIVE_LAYOUT:-}" ]; then
    LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_LAYOUT=${RVBBIT_COMPACT_HIVE_LAYOUT}")
fi
if [ "${VORTEX_FORCED_SELECTED}" = "1" ] || [ "${VORTEX_AUTO_SELECTED}" = "1" ]; then
    LOAD_ENV+=(-e "RVBBIT_COMPACT_VORTEX_LAYOUT=${VORTEX_LAYOUT_DISPLAY}")
elif [ -n "${RVBBIT_COMPACT_VORTEX_LAYOUT:-}" ]; then
    LOAD_ENV+=(-e "RVBBIT_COMPACT_VORTEX_LAYOUT=${RVBBIT_COMPACT_VORTEX_LAYOUT}")
fi
if [ "${RVBBIT_SELECTED}" = "1" ] || [ -n "${RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD:-}" ]; then
    LOAD_ENV+=(-e "RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=${HIVE_REFRESH_DISPLAY}")
fi
[ -n "${RVBBIT_COMPACT_HIVE_KEYS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_KEYS=${RVBBIT_COMPACT_HIVE_KEYS}")
[ -n "${RVBBIT_COMPACT_HIVE_VARIANTS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_VARIANTS=${RVBBIT_COMPACT_HIVE_VARIANTS}")
[ -n "${RVBBIT_COMPACT_HIVE_MIN_DISTINCT:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_MIN_DISTINCT=${RVBBIT_COMPACT_HIVE_MIN_DISTINCT}")
[ -n "${RVBBIT_COMPACT_HIVE_MAX_DISTINCT:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_MAX_DISTINCT=${RVBBIT_COMPACT_HIVE_MAX_DISTINCT}")
REPEATS="${BENCH_REPEATS:-3}"
TIMEOUT_S="${BENCH_TIMEOUT:-300}"
RVBBIT_RESET_EXTENSION="${RVBBIT_RESET_EXTENSION:-${RESET_RVBBIT_EXTENSION:-}}"
RVBBIT_LOAD_ROUTE_PROFILE="${RVBBIT_LOAD_ROUTE_PROFILE:-}"
BENCH_REBUILD="${BENCH_REBUILD:-}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${BENCH_RUN_ID:-clickbench_${STAMP}}"
BENCH_TEST_NAME="${BENCH_TEST_NAME:-clickbench}"
BENCH_PERSIST_RESULTS="${BENCH_PERSIST_RESULTS:-1}"
REPORT_FILE="bench/clickbench/results/clickbench_${LIMIT}_${STAMP}.txt"

# ---- Helpers ---------------------------------------------------------------
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mXX %s\033[0m\n' "$*" >&2; exit 1; }
system_selected() {
    [[ ",${SYSTEMS}," == *",$1,"* ]]
}
sql_literal() {
    local value
    value="$(printf "%s" "${1:-}" | sed "s/'/''/g")"
    printf "'%s'" "${value}"
}
env_on() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}
configure_gqe_pooled_sidecar_defaults() {
    [ "${GPU_GQE_SELECTED}" = "1" ] || return 0
    export RVBBIT_GQE_CLIENT_MODE="${RVBBIT_GQE_CLIENT_MODE:-flight}"
    case "${RVBBIT_GQE_SHARED_BACKEND:-true}" in
        0|false|FALSE|no|NO|off|OFF|disabled|DISABLED) return 0 ;;
        *) ;;
    esac
    export RVBBIT_DUCK_BACKEND_SHARED="${RVBBIT_DUCK_BACKEND_SHARED:-true}"
    export RVBBIT_DUCK_BACKEND_SHARED_LAUNCH="${RVBBIT_DUCK_BACKEND_SHARED_LAUNCH:-true}"
    local targets="${RVBBIT_DUCK_BACKEND_SHARED_TARGETS:-}"
    if [ -z "${targets}" ]; then
        targets="gpu_gqe"
    elif [[ ",${targets}," != *",gpu_gqe,"* ]] \
        && [[ ",${targets}," != *",all,"* ]] \
        && [[ ",${targets}," != *",\*,"* ]]; then
        targets="${targets},gpu_gqe"
    fi
    export RVBBIT_DUCK_BACKEND_SHARED_TARGETS="${targets}"
    # ClickBench runs each system serially. A multi-worker shared sidecar gives
    # each worker its own GQE Flight client/catalog state, so the first query can
    # measure per-worker warmup instead of steady-state execution.
    export RVBBIT_DUCK_BACKEND_SHARED_WORKERS="${RVBBIT_DUCK_BACKEND_SHARED_WORKERS:-1}"
    export RVBBIT_DUCK_TELEMETRY_BATCH="${RVBBIT_DUCK_TELEMETRY_BATCH:-1}"
}
gqe_rebuild_mode_is_full() {
    case "${RVBBIT_GPU_GQE_REBUILD_MODE:-refresh}" in
        full|FULL|toolchain|TOOLCHAIN) return 0 ;;
        *) return 1 ;;
    esac
}
backup_clickbench_gqe_image_before_full_rebuild() {
    local gqe_image backup_tag
    if ! env_on "${RVBBIT_GQE_BACKUP_BEFORE_FULL_REBUILD:-1}"; then
        return 0
    fi
    gqe_image="${RVBBIT_GQE_PG_IMAGE:-docker-pg-rvbbit-gqe}"
    if ! docker image inspect "${gqe_image}" >/dev/null 2>&1; then
        return 0
    fi
    backup_tag="${RVBBIT_GQE_BACKUP_TAG:-${gqe_image}-backup-$(date -u +%Y%m%dT%H%M%SZ)}"
    say "tagging existing GPU/GQE image backup as ${backup_tag}"
    docker tag "${gqe_image}" "${backup_tag}"
}
flatten_clickbench_gqe_image_if_needed() {
    local source_image target_image threshold layers container_id
    source_image="$1"
    target_image="$2"
    threshold="${RVBBIT_GQE_FLATTEN_LAYER_THRESHOLD:-110}"
    layers="$(docker image inspect "${source_image}" --format '{{len .RootFS.Layers}}' 2>/dev/null || printf '0')"
    if [ "${layers:-0}" -lt "${threshold}" ]; then
        printf "%s" "${source_image}"
        return 0
    fi

    say "flattening GPU/GQE refresh base ${source_image} (${layers} layers) as ${target_image}" >&2
    container_id="$(docker create "${source_image}")"
    if ! docker export "${container_id}" | docker import \
        --change 'ENTRYPOINT ["docker-entrypoint.sh"]' \
        --change 'CMD ["postgres"]' \
        --change 'EXPOSE 5432' \
        --change 'VOLUME ["/var/lib/postgresql"]' \
        --change 'WORKDIR /' \
        --change 'ENV PATH=/conda/envs/gqe/bin:/conda/bin:/opt/gqe/rust/target/release:/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/18/bin' \
        --change 'ENV GOSU_VERSION=1.19' \
        --change 'ENV LANG=en_US.utf8' \
        --change 'ENV PG_MAJOR=18' \
        --change 'ENV PG_VERSION=18.3-1.pgdg13+1' \
        --change 'ENV PGDATA=/var/lib/postgresql/18/docker' \
        --change 'ENV RVBBIT_CAPABILITY_ROOT=/usr/share/rvbbit/capabilities' \
        --change 'ENV RVBBIT_CAPABILITY_PACKS_DIR=/usr/share/rvbbit/capabilities/packs' \
        --change 'ENV NVIDIA_VISIBLE_DEVICES=all' \
        --change 'ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility' \
        --change 'ENV LD_LIBRARY_PATH=/conda/envs/gqe/lib:/usr/local/lib' \
        --change 'ENV RVBBIT_GQE_CLI=/opt/gqe/rust/target/release/gqe-cli' \
        --change 'ENV RVBBIT_GQE_NODE_MANAGER=/opt/gqe/build/src/node_manager/gqe_node_manager' \
        --change 'ENV RVBBIT_GQE_TASK_MANAGER=/opt/gqe/build/src/task_manager/gqe_task_manager' \
        --change 'ENV RVBBIT_GQE_SERVER_URL=http://127.0.0.1:50051' \
        --change 'ENV RVBBIT_GQE_AUTO_START=true' \
        - "${target_image}" >/dev/null; then
        docker rm -f "${container_id}" >/dev/null 2>&1 || true
        return 1
    fi
    docker rm -f "${container_id}" >/dev/null
    printf "%s" "${target_image}"
}
refresh_clickbench_gqe_image() {
    local gqe_image base_image refresh_base explicit_refresh_base
    local flattened_base
    gqe_image="${RVBBIT_GQE_PG_IMAGE:-docker-pg-rvbbit-gqe}"
    base_image="${RVBBIT_BASE_IMAGE:-${RVBBIT_PG_IMAGE:-docker-pg-rvbbit}}"
    refresh_base="${gqe_image}-pre-refresh"
    explicit_refresh_base="${RVBBIT_GQE_REFRESH_BASE_IMAGE:-}"
    if [ -n "${explicit_refresh_base}" ] && docker image inspect "${explicit_refresh_base}" >/dev/null 2>&1; then
        say "refreshing GPU/GQE image from explicit base ${explicit_refresh_base}"
        refresh_base="${explicit_refresh_base}"
    elif docker image inspect "${gqe_image}" >/dev/null 2>&1; then
        docker tag "${gqe_image}" "${refresh_base}"
    elif docker image inspect "${refresh_base}" >/dev/null 2>&1; then
        say "refreshing GPU/GQE image from preserved backup ${refresh_base}"
    else
        return 2
    fi
    flattened_base="$(
        flatten_clickbench_gqe_image_if_needed \
            "${refresh_base}" \
            "${RVBBIT_GQE_FLAT_REFRESH_BASE_IMAGE:-${gqe_image}-flat-refresh-base}"
    )" || return 1
    docker build \
        -f docker/Dockerfile.rvbbit-gqe-refresh \
        --build-arg "RVBBIT_BASE_IMAGE=${base_image}" \
        --build-arg "RVBBIT_GQE_BASE_IMAGE=${flattened_base}" \
        -t "${gqe_image}" .
}
gqe_bridge_has_required_marker() {
    local marker
    marker="${GQE_ADAPTER_VERSION_REQUIRED}"
    ${COMPOSE} exec -T pg-rvbbit sh -lc \
        "grep -a -q -- '${marker}' /opt/rvbbit/gqe/bin/rvbbit-gqe-bridge"
}
wait_for_pg_rvbbit_ready() {
    local timeout_s="${RVBBIT_PG_READY_TIMEOUT:-120}"
    local start_s now_s
    start_s="$(date +%s)"
    while true; do
        if ${COMPOSE} exec -T pg-rvbbit pg_isready -U postgres -d bench >/dev/null 2>&1; then
            return 0
        fi
        now_s="$(date +%s)"
        if [ $((now_s - start_s)) -ge "${timeout_s}" ]; then
            die "timed out waiting for pg-rvbbit to accept connections after ${timeout_s}s"
        fi
        sleep 1
    done
}
wait_for_bench_session_drain() {
    local timeout_s="${RVBBIT_RESET_DRAIN_TIMEOUT:-30}"
    local start_s now_s remaining
    start_s="$(date +%s)"
    while true; do
        remaining="$(${COMPOSE} exec -T pg-rvbbit psql -U postgres -d postgres -Atq -v ON_ERROR_STOP=1 -c "
            SELECT count(*)
            FROM pg_stat_activity
            WHERE datname = 'bench'
              AND pid <> pg_backend_pid();
        " | tr -d '[:space:]')"
        [ "${remaining:-0}" = "0" ] && return 0
        now_s="$(date +%s)"
        if [ $((now_s - start_s)) -ge "${timeout_s}" ]; then
            warn "bench session drain timed out with ${remaining} session(s) still visible; continuing with reset lock timeout"
            return 0
        fi
        sleep 1
    done
}
hive_refresh_explicitly_disabled() {
    case "${RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD:-}" in
        0|false|FALSE|no|NO|off|OFF|disabled|DISABLED) return 0 ;;
        *) return 1 ;;
    esac
}
wait_for_layout_variant_refresh() {
    local timeout_s="${RVBBIT_HIVE_VARIANT_WAIT_TIMEOUT:-3600}"
    local start_s now_s active
    start_s="$(date +%s)"
    while true; do
        active="$(${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -Atq -v ON_ERROR_STOP=1 -c "
            SELECT EXISTS (
                SELECT 1
                FROM pg_stat_activity
                WHERE pid <> pg_backend_pid()
                  AND datname = current_database()
                  AND state <> 'idle'
                  AND query LIKE '%refresh_layout_variants%'
            );
        " | tr -d '[:space:]')"
        [ "${active}" != "t" ] && break
        now_s="$(date +%s)"
        if [ $((now_s - start_s)) -ge "${timeout_s}" ]; then
            die "timed out waiting for async layout variant refresh after ${timeout_s}s"
        fi
        sleep 5
    done
}
wait_for_hive_variant_refresh() {
    wait_for_layout_variant_refresh
}
clickbench_hive_variants_ready() {
    ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -Atq -v ON_ERROR_STOP=1 -c "
        SELECT EXISTS (
            SELECT 1
            FROM rvbbit.row_group_variants rg
            JOIN rvbbit.layout_variant_status s
              ON s.table_oid = rg.table_oid AND s.layout = rg.layout
            WHERE rg.table_oid = 'hits'::regclass
              AND rg.layout LIKE 'hive:%'
              AND s.status = 'ready'
        );
    " | tr -d '[:space:]'
}
clickbench_vortex_variants_ready() {
    ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -Atq -v ON_ERROR_STOP=1 -c "
        SELECT EXISTS (
            SELECT 1
            FROM rvbbit.row_group_variants rg
            JOIN rvbbit.layout_variant_status s
              ON s.table_oid = rg.table_oid AND s.layout = rg.layout
            WHERE rg.table_oid = 'hits'::regclass
              AND rg.layout = 'vortex_scan'
              AND s.status = 'ready'
        );
    " | tr -d '[:space:]'
}
ensure_clickbench_hive_variants_ready() {
    [ "${HIVE_FORCED_SELECTED}" = "1" ] || return 0
    say "ensuring Hive variants are ready for forced-Hive systems"
    wait_for_hive_variant_refresh
    if [ "$(clickbench_hive_variants_ready)" = "t" ]; then
        return 0
    fi
    if hive_refresh_explicitly_disabled; then
        die "forced-Hive variants are missing for public.hits and RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD disables refresh"
    fi
    if [ -n "${RVBBIT_COMPACT_KEEP_HEAP:-}" ] && ! env_on "${RVBBIT_COMPACT_KEEP_HEAP}"; then
        die "forced-Hive benchmarks need retained heap until variant refresh can rebuild from canonical parquet; unset RVBBIT_COMPACT_KEEP_HEAP or set it to 1"
    fi
    ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 \
        -v hive_layout="${RVBBIT_COMPACT_HIVE_LAYOUT:-on}" \
        -v hive_keys="${RVBBIT_COMPACT_HIVE_KEYS:-}" \
        -v hive_variants="${RVBBIT_COMPACT_HIVE_VARIANTS:-}" \
        -v hive_min_distinct="${RVBBIT_COMPACT_HIVE_MIN_DISTINCT:-}" \
        -v hive_max_distinct="${RVBBIT_COMPACT_HIVE_MAX_DISTINCT:-}" <<'SQL'
SELECT set_config('rvbbit.compact_hive_layout', :'hive_layout', false);
SELECT set_config('rvbbit.compact_hive_keys', :'hive_keys', false) WHERE :'hive_keys' <> '';
SELECT set_config('rvbbit.compact_hive_variants', :'hive_variants', false) WHERE :'hive_variants' <> '';
SELECT set_config('rvbbit.compact_hive_min_distinct', :'hive_min_distinct', false) WHERE :'hive_min_distinct' <> '';
SELECT set_config('rvbbit.compact_hive_max_distinct', :'hive_max_distinct', false) WHERE :'hive_max_distinct' <> '';
SELECT rvbbit.refresh_layout_variants('hits'::regclass);
SQL
    if [ "$(clickbench_hive_variants_ready)" != "t" ]; then
        die "forced-Hive variants are still missing for public.hits after refresh"
    fi
}
ensure_clickbench_vortex_variants_ready() {
    [ "${VORTEX_FORCED_SELECTED}" = "1" ] || return 0
    say "ensuring Vortex variants are ready for forced-Vortex systems"
    wait_for_layout_variant_refresh
    if [ "$(clickbench_vortex_variants_ready)" = "t" ]; then
        return 0
    fi
    if hive_refresh_explicitly_disabled; then
        die "forced-Vortex variants are missing for public.hits and RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD disables refresh"
    fi
    if [ -n "${RVBBIT_COMPACT_KEEP_HEAP:-}" ] && ! env_on "${RVBBIT_COMPACT_KEEP_HEAP}"; then
        die "forced-Vortex benchmarks need retained heap until variant refresh can rebuild from canonical parquet; unset RVBBIT_COMPACT_KEEP_HEAP or set it to 1"
    fi
    ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 \
        -v vortex_layout="${VORTEX_LAYOUT_DISPLAY}" <<'SQL'
SELECT set_config('rvbbit.compact_vortex_layout', :'vortex_layout', false);
SELECT rvbbit.refresh_layout_variants('hits'::regclass);
SQL
    if [ "$(clickbench_vortex_variants_ready)" != "t" ]; then
        die "forced-Vortex variants are still missing for public.hits after refresh"
    fi
}
ensure_clickbench_route_shape_samples() {
    [ "${RVBBIT_SELECTED}" = "1" ] || return 0
    ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE IF NOT EXISTS rvbbit.route_shape_samples (
    shape_key      text PRIMARY KEY,
    shape_family   text NOT NULL,
    sql            text NOT NULL,
    search_path    text,
    last_tested_at timestamptz,
    last_result    text,
    captured_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE rvbbit.route_shape_samples ADD COLUMN IF NOT EXISTS search_path text;
ALTER TABLE rvbbit.route_shape_samples ADD COLUMN IF NOT EXISTS last_tested_at timestamptz;
ALTER TABLE rvbbit.route_shape_samples ADD COLUMN IF NOT EXISTS last_result text;
CREATE INDEX IF NOT EXISTS route_shape_samples_family_idx ON rvbbit.route_shape_samples (shape_family);
SQL
}
mark_clickbench_gpu_gqe_skip() {
    local reason="$1"
    reason="$(printf "%s" "${reason}" | tr '\r\n' '  ' | cut -c1-500)"
    if env_on "${RVBBIT_REQUIRE_GPU_GQE:-}"; then
        die "rvbbit_gpu_gqe_forced selected, but gpu_gqe is unavailable (${reason:-unknown})"
    fi
    GPU_GQE_SKIP_ACTIVE=1
    warn "rvbbit_gpu_gqe_forced selected, but gpu_gqe is unavailable; marking it SKIP (${reason:-unknown})"
    warn "set RVBBIT_REQUIRE_GPU_GQE=1 to fail instead"
    QUERIES_ENV+=(-e "RVBBIT_GPU_GQE_SKIP_REASON=${reason:-rvbbit-gqe bridge unavailable}")
}
prestart_clickbench_gpu_gqe() {
    ${COMPOSE} exec -T \
        -e "RVBBIT_GQE_WORK_DIR=${RVBBIT_GQE_WORK_DIR:-/tmp/rvbbit-gqe}" \
        pg-rvbbit bash -s <<'SH'
set -euo pipefail
work_dir="${RVBBIT_GQE_WORK_DIR:-/tmp/rvbbit-gqe}"
mkdir -p "${work_dir}"
chown -R postgres:postgres "${work_dir}" 2>/dev/null || true
chmod 1777 "${work_dir}" 2>/dev/null || true
SH
    ${COMPOSE} exec -T \
        -u postgres \
        -e "RVBBIT_GQE_PREFLIGHT_TIMEOUT_S=${RVBBIT_GQE_PREFLIGHT_TIMEOUT_S:-75}" \
        -e "RVBBIT_GQE_MIN_SHM_BYTES=${RVBBIT_GQE_MIN_SHM_BYTES:-6442450944}" \
        -e "RVBBIT_GQE_WORK_DIR=${RVBBIT_GQE_WORK_DIR:-/tmp/rvbbit-gqe}" \
        pg-rvbbit bash -s <<'SH'
set -euo pipefail

min_shm_bytes="${RVBBIT_GQE_MIN_SHM_BYTES:-6442450944}"
shm_bytes="$(df -B1 /dev/shm | awk 'NR==2 {print $2}')"
if [ -z "${shm_bytes}" ] || [ "${shm_bytes}" -lt "${min_shm_bytes}" ]; then
    echo "GQE /dev/shm too small (${shm_bytes:-unknown} bytes; need >= ${min_shm_bytes}; set RVBBIT_GQE_SHM_SIZE=8gb and recreate pg-rvbbit)"
    exit 20
fi

server_url="${RVBBIT_GQE_SERVER_URL:-http://127.0.0.1:50051}"
endpoint="${server_url#http://}"
endpoint="${endpoint#https://}"
endpoint="${endpoint%%/*}"
host="${endpoint%:*}"
port="${endpoint##*:}"
if [ "${host}" = "${endpoint}" ] || ! [[ "${port}" =~ ^[0-9]+$ ]]; then
    echo "GQE server URL must include host:port, got ${server_url}"
    exit 21
fi
listen_host="${host}"
connect_host="${host}"
if [ "${connect_host}" = "0.0.0.0" ]; then
    connect_host="127.0.0.1"
fi

tcp_check() {
    timeout 1 bash -lc ":</dev/tcp/${connect_host}/${port}" >/dev/null 2>&1
}

if tcp_check; then
    echo "server already listening at ${connect_host}:${port}"
    exit 0
fi

node="${RVBBIT_GQE_NODE_MANAGER:-/opt/gqe/build/src/node_manager/gqe_node_manager}"
task="${RVBBIT_GQE_TASK_MANAGER:-/opt/gqe/build/src/task_manager/gqe_task_manager}"
cli="${RVBBIT_GQE_CLI:-/opt/gqe/rust/target/release/gqe-cli}"
for binary in "${node}" "${task}" "${cli}"; do
    if [ ! -x "${binary}" ]; then
        echo "GQE binary is missing or not executable: ${binary}"
        exit 22
    fi
done

work_dir="${RVBBIT_GQE_WORK_DIR:-/tmp/rvbbit-gqe}"
mkdir -p "${work_dir}"
chmod 1777 "${work_dir}" 2>/dev/null || true
log_path="${work_dir}/node-manager.log"
pidfile="${work_dir}/node-manager.pid"

if [ -s "${pidfile}" ]; then
    pid="$(cat "${pidfile}" 2>/dev/null || true)"
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
        echo "GQE node manager pid ${pid} is running but ${connect_host}:${port} is not reachable"
        tail -40 "${log_path}" 2>/dev/null || true
        exit 23
    fi
fi

rm -f "${work_dir}/node-manager-start.lock"
nohup "${node}" \
    --address "${listen_host}" \
    --port "${port}" \
    --num-gpus "${RVBBIT_GQE_NUM_GPUS:-1}" \
    --task-manager-binary "${task}" \
    >>"${log_path}" 2>&1 &
pid="$!"
echo "${pid}" > "${pidfile}"

timeout_s="${RVBBIT_GQE_PREFLIGHT_TIMEOUT_S:-75}"
for _ in $(seq 1 "${timeout_s}"); do
    if tcp_check; then
        echo "server listening at ${connect_host}:${port}"
        exit 0
    fi
    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "GQE node manager exited while starting"
        tail -80 "${log_path}" 2>/dev/null || true
        exit 24
    fi
    sleep 1
done

echo "GQE server ${server_url} did not become reachable after ${timeout_s}s"
tail -80 "${log_path}" 2>/dev/null || true
exit 25
SH
}
ensure_clickbench_gpu_gqe_available() {
    system_selected "rvbbit_gpu_gqe_forced" || return 0
    say "checking GPU/GQE bridge availability"
    local status_line routes_available binary_found binary_path reason
    local preflight_output
    if env_on "${RVBBIT_GQE_PREFLIGHT_START:-true}"; then
        if preflight_output="$(prestart_clickbench_gpu_gqe 2>&1)"; then
            echo "   gpu_gqe    : ${preflight_output}"
        else
            mark_clickbench_gpu_gqe_skip "${preflight_output}"
            return 0
        fi
    fi
    if ! env_on "${RVBBIT_GQE_ALLOW_STALE_BRIDGE:-}" && ! gqe_bridge_has_required_marker; then
        mark_clickbench_gpu_gqe_skip "stale rvbbit-gqe bridge in ${RVBBIT_GQE_PG_IMAGE:-docker-pg-rvbbit-gqe}; missing ${GQE_ADAPTER_VERSION_REQUIRED}; rerun with --rebuild (fast refresh) or set RVBBIT_GPU_GQE_REBUILD_MODE=full for a full GQE rebuild"
        return 0
    fi
    local gqe_bin_lit
    gqe_bin_lit="$(sql_literal "${RVBBIT_GQE_BIN:-}")"
    if ! status_line="$(${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -Atq -F '|' -v ON_ERROR_STOP=1 -c "
        WITH configured AS (
            SELECT pg_catalog.set_config('rvbbit.gqe_bin', ${gqe_bin_lit}, false)
            WHERE ${gqe_bin_lit} <> ''
        ),
        status AS (
            SELECT rvbbit.accelerator_runtime_status(false) AS value
        )
        SELECT coalesce(value->'gpu_gqe'->>'routes_available', 'false'),
               coalesce(value->'gpu_gqe'->>'binary_found', 'false'),
               coalesce(value->'gpu_gqe'->>'binary_path', ''),
               coalesce(value->'gpu_gqe'->>'reason', '')
        FROM status, (SELECT count(*) FROM configured) AS applied;
    " | tr -d '\r')"; then
        mark_clickbench_gpu_gqe_skip "GPU/GQE status unavailable"
    else
        IFS='|' read -r routes_available binary_found binary_path reason <<< "${status_line}"
        if [ "${routes_available}" = "true" ] || [ "${routes_available}" = "t" ]; then
            echo "   gpu_gqe    : available (${binary_path:-rvbbit-gqe on PATH})"
            return 0
        fi
        mark_clickbench_gpu_gqe_skip "${reason:-rvbbit-gqe bridge unavailable}"
    fi
}
gqe_prewarm_enabled() {
    case "${RVBBIT_GQE_PREWARM:-auto}" in
        0|false|FALSE|no|NO|off|OFF|disabled|DISABLED) return 1 ;;
        *) return 0 ;;
    esac
}
prewarm_clickbench_gpu_gqe_catalog() {
    system_selected "rvbbit_gpu_gqe_forced" || return 0
    [ "${GPU_GQE_SKIP_ACTIVE}" = "0" ] || return 0
    gqe_prewarm_enabled || return 0

    say "prewarming GPU/GQE catalog"
    local prewarm_output
    if prewarm_output="$(${COMPOSE} exec -T \
        -u postgres \
        -e "RVBBIT_GQE_PREFLIGHT_TIMEOUT_S=${RVBBIT_GQE_PREFLIGHT_TIMEOUT_S:-75}" \
        -e "RVBBIT_GQE_WORK_DIR=${RVBBIT_GQE_WORK_DIR:-/tmp/rvbbit-gqe}" \
        -e "RVBBIT_GQE_PREWARM_SQL=${RVBBIT_GQE_PREWARM_SQL:-}" \
        -e "RVBBIT_GQE_PREWARM_DSN=${RVBBIT_GQE_PREWARM_DSN:-}" \
        pg-rvbbit bash -s 2>&1 <<'SH'
set -euo pipefail

work_dir="${RVBBIT_GQE_WORK_DIR:-/tmp/rvbbit-gqe}"
mkdir -p "${work_dir}"
chmod 1777 "${work_dir}" 2>/dev/null || true

pgdata="${PGDATA:-/var/lib/postgresql/18/docker}"
dsn="${RVBBIT_GQE_PREWARM_DSN:-postgresql://postgres:rvbbit@127.0.0.1:5432/bench}"
sql="${RVBBIT_GQE_PREWARM_SQL:-SELECT 1 FROM hits LIMIT 0}"
timeout_s="${RVBBIT_GQE_PREFLIGHT_TIMEOUT_S:-75}"
json_path="${work_dir}/prewarm.json"

/usr/local/bin/rvbbit-gqe \
    --engine gpu_gqe \
    --layout scan \
    --dsn "${dsn}" \
    --sql "${sql}" \
    --explain-only \
    --timeout-s "${timeout_s}" \
    --max-rows 1 \
    --pgdata-prefix "${pgdata}" \
    --visible-pgdata-prefix "${pgdata}" \
    >"${json_path}"

elapsed="$(tr -d '\n' < "${json_path}" | sed -n 's/.*"elapsed_ms"[[:space:]]*:[[:space:]]*\([0-9.][0-9.]*\).*/\1/p')"
if [ -n "${elapsed}" ]; then
    echo "catalog ready (${elapsed}ms explain-only)"
else
    echo "catalog ready"
fi
SH
    )"; then
        echo "   gpu_gqe    : ${prewarm_output}"
    else
        mark_clickbench_gpu_gqe_skip "GQE prewarm failed: ${prewarm_output}"
    fi
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
        --results /bench/clickbench/results/last_run.json \
        --results-path bench/clickbench/results/last_run.json \
        --report-path "${REPORT_FILE}" \
        --run-id "${RUN_ID}" \
        --test-name "${BENCH_TEST_NAME}" \
        --suite ClickBench \
        --scale "${LIMIT}" \
        --row-count "${LIMIT}" \
        --started-at "${STAMP}" \
        --git-commit "${git_commit}" \
        "${git_dirty_arg}" \
        --setting "limit=${LIMIT}" \
        --setting "systems=${SYSTEMS}" \
        --setting "repeats=${REPEATS}" \
        --setting "timeout_s=${TIMEOUT_S}" \
        --setting "queries=${BENCH_QUERIES:-}" \
        --setting "skip_load=${SKIP_LOAD:-0}" \
        --setting "skip_download=${SKIP_DOWNLOAD:-0}" \
        --setting "rebuild=${BENCH_REBUILD:-0}" \
        --setting "rvbbit_reset_extension=${RVBBIT_RESET_EXTENSION:-0}" \
        --setting "hive_refresh=${HIVE_REFRESH_DISPLAY}" \
        --setting "vortex_layout=${VORTEX_LAYOUT_DISPLAY}" \
        --setting "df_inprocess=${RVBBIT_DF_INPROCESS:-on}" \
        --setting "hot_store_budget_mb=${RVBBIT_HOT_STORE_BUDGET_MB:-512}" \
        --setting "hot_store_route_max_rows=${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS:-500000}"; then
        warn "benchmark completed, but history recording failed"
    fi
}
usage() {
    awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"
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
        --skip-download)
            export SKIP_DOWNLOAD=1
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
configure_gqe_pooled_sidecar_defaults

# ---- 0. Sanity --------------------------------------------------------------
command -v docker >/dev/null || die "docker not found in PATH"
[ -f "docker/docker-compose.yml" ] || die "expected to run from repo root (no docker/docker-compose.yml here)"

say "configuration"
echo "   limit       : ${LIMIT}"
echo "   systems     : ${SYSTEMS}"
echo "   repeats     : ${REPEATS}"
echo "   timeout/q   : ${TIMEOUT_S}s"
echo "   report file : ${REPORT_FILE}"
echo "   run id      : ${RUN_ID}"
echo "   test name   : ${BENCH_TEST_NAME}"
echo "   rvbbit reset: $(env_on "${RVBBIT_RESET_EXTENSION}" && echo destructive || echo preserve-system-data)"
echo "   route import: $(env_on "${RVBBIT_LOAD_ROUTE_PROFILE}" && echo yes || echo no)"
echo "   rebuild     : $(env_on "${BENCH_REBUILD}" && echo yes || echo no)"
echo "   persist     : $(env_on "${BENCH_PERSIST_RESULTS}" && echo yes || echo no)"
echo "   df_inprocess: ${RVBBIT_DF_INPROCESS:-on (default)}"
echo "   gpu compose : ${GPU_COMPOSE_DISPLAY}"
echo "   gqe image   : ${GQE_IMAGE_DISPLAY}"
echo "   gqe home    : ${GQE_HOST_MOUNT_DISPLAY}"
echo "   gqe client  : ${RVBBIT_GQE_CLIENT_MODE:-flight}"
echo "   gqe shared  : ${RVBBIT_DUCK_BACKEND_SHARED:-default} targets=${RVBBIT_DUCK_BACKEND_SHARED_TARGETS:-default}"
echo "   hive refresh: ${HIVE_REFRESH_DISPLAY}"
echo "   vortex      : ${VORTEX_LAYOUT_DISPLAY}"
echo "   chunks      : direct=${RVBBIT_DIRECT_ACCEL_CHUNK_ROWS:-default} compact=${RVBBIT_COMPACT_SCAN_CHUNK_ROWS:-default} gqe_large=$(env_on "${RVBBIT_GQE_LARGE_ROW_GROUPS:-}" && echo yes || echo no)"
echo "   hot store   : budget=${RVBBIT_HOT_STORE_BUDGET_MB:-512}MB route_max_rows=${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS:-500000}"

if [ "${RVBBIT_SELECTED:-1}" = "1" ] && ! env_on "${BENCH_REBUILD}" && ! env_on "${RVBBIT_RESET_EXTENSION}"; then
    warn "benching the pg-rvbbit image without --rebuild + --reset-rvbbit-extension."
    warn "if you pulled new rvbbit code (Phase 1+ post-2026-05-25), the running"
    warn "container may have a stale .so and stale catalog. For an apples-to-"
    warn "apples bench against new machinery, use:"
    warn ""
    warn "    ./bench/clickbench/run_offline.sh --rebuild --reset-rvbbit-extension"
    warn ""
fi
if env_on "${RVBBIT_LOAD_ROUTE_PROFILE}"; then
    warn "bench/rvbbit_route_profile.json was trained pre-Phase-1 (sidecar"
    warn "DataFusion era). Latency curves there don't reflect the in-process"
    warn "DataFusion path. Profile-driven route decisions may be suboptimal."
fi

# ---- 1. Download hits.parquet ----------------------------------------------
if [ -z "${SKIP_DOWNLOAD:-}" ] && [ ! -f "${HITS}" ]; then
    say "downloading hits.parquet (~14 GB) — this is the slow part"
    mkdir -p "${DATA_DIR}"
    curl -L --fail --progress-bar -o "${HITS}.part" "${HITS_URL}"
    mv "${HITS}.part" "${HITS}"
fi
[ -f "${HITS}" ] || die "hits.parquet missing at ${HITS} (re-run without SKIP_DOWNLOAD)"

HITS_SIZE="$(du -h "${HITS}" | awk '{print $1}')"
say "hits.parquet present: ${HITS_SIZE}"

# ---- 2. Bring up containers ------------------------------------------------
if env_on "${BENCH_REBUILD}"; then
    if [ "${GQE_IMAGE_SELECTED}" = "1" ]; then
        say "rebuilding base pg-rvbbit image for GPU/GQE image"
        docker compose -f docker/docker-compose.yml build pg-rvbbit
        case "${RVBBIT_GPU_GQE_REBUILD_MODE:-refresh}" in
            full|FULL|toolchain|TOOLCHAIN)
                say "rebuilding full GPU/GQE image"
                backup_clickbench_gqe_image_before_full_rebuild
                ${COMPOSE} --profile bench build pg-rvbbit
                ;;
            refresh|REFRESH|adapter|ADAPTER)
                say "refreshing GPU/GQE image with current pg_rvbbit + bridge artifacts"
                refresh_rc=0
                refresh_clickbench_gqe_image || refresh_rc=$?
                if [ "${refresh_rc}" -eq 2 ]; then
                    die "existing ${RVBBIT_GQE_PG_IMAGE:-docker-pg-rvbbit-gqe} image not found; refusing to recompile the full GPU/GQE toolchain in refresh mode. Run once with RVBBIT_GPU_GQE_REBUILD_MODE=full if you intentionally need the toolchain image rebuilt."
                elif [ "${refresh_rc}" -ne 0 ]; then
                    die "GPU/GQE image refresh failed even though a reusable base image was found. Check the Docker error above; set RVBBIT_GPU_GQE_REBUILD_MODE=full only if the base image is actually unusable."
                fi
                ;;
            *)
                die "unknown RVBBIT_GPU_GQE_REBUILD_MODE=${RVBBIT_GPU_GQE_REBUILD_MODE}; use refresh or full"
                ;;
        esac
        say "rebuilding bench image from current source"
        ${COMPOSE} --profile bench build bench
    else
        say "rebuilding pg-rvbbit + bench images from current source"
        # Build serially: pg-rvbbit's cargo build pulls a large dep graph;
        # bench is small. No need to parallelize.
        ${COMPOSE} --profile bench build pg-rvbbit bench
    fi
fi

UP_BUILD_ARGS=()
if [ "${GQE_IMAGE_SELECTED}" = "1" ] && ! gqe_rebuild_mode_is_full; then
    # docker compose up can implicitly build a missing service image. For GQE
    # that means a multi-hour CUDA/RAPIDS toolchain build, so refresh/default
    # mode makes implicit builds impossible. Full mode remains explicit.
    UP_BUILD_ARGS+=(--no-build)
    # --no-build must not strand a fresh box that has never built the BENCH
    # image (observed on a clean deploy: "No such image: docker-bench").
    # Build just that one explicitly; it never triggers the CUDA toolchain.
    if ! docker image inspect docker-bench >/dev/null 2>&1; then
        say "building missing bench image (no-build mode active for GQE)"
        ${COMPOSE} --profile bench build bench
    fi
fi

say "starting competitor containers (profile=bench)"
if [ "${GPU_GQE_SELECTED}" = "1" ]; then
    say "recreating pg-rvbbit to apply GPU/GQE runtime settings"
    ${COMPOSE} --profile bench up -d "${UP_BUILD_ARGS[@]}" --force-recreate pg-rvbbit
fi
${COMPOSE} --profile bench up -d "${UP_BUILD_ARGS[@]}"
wait_for_pg_rvbbit_ready

# ---- 3. Prepare rvbbit extension ------------------------------------------
if [ "${RVBBIT_SELECTED}" = "1" ]; then
    if env_on "${RVBBIT_RESET_EXTENSION}"; then
        say "draining existing bench sessions before pg_rvbbit reset"
        ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'bench'
  AND pid <> pg_backend_pid();
SQL
        wait_for_bench_session_drain
        say "resetting pg_rvbbit extension (DESTRUCTIVE: drops rvbbit system/catalog data)"
        ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 <<'SQL'
SET lock_timeout = '30s';
SET statement_timeout = '5min';
DROP EXTENSION IF EXISTS pg_rvbbit CASCADE;
DROP EVENT TRIGGER IF EXISTS rvbbit_on_create_table;
DROP EVENT TRIGGER IF EXISTS rvbbit_on_drop_table;
DROP EVENT TRIGGER IF EXISTS rvbbit_partition_dirty_triggers_on_alter;
-- pg_rvbbit is preloaded, so hooks remain active in this backend after
-- DROP EXTENSION. Keep routing disabled while replacing the extension schema.
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

    # Apply stacked schema migrations after (re)creating the extension. CREATE
    # EXTENSION only installs the base SQL — the migrations (route_model, route
    # bindings, brain/cubes/metrics schema, etc.) are applied by rvbbit.migrate(),
    # which a --reset-rvbbit-extension wipes. Idempotent ("up to date" when
    # nothing pending), so it's safe on the non-reset path too. Mirrors
    # `make reload-extension`.
    say "applying rvbbit.migrate() (idempotent schema migrations)"
    ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 -tA \
        -f - < crates/pg_rvbbit/sql/migrate.sql | tail -1 | cut -c1-100

    say "ensuring route shape sample table"
    ensure_clickbench_route_shape_samples

    if env_on "${RVBBIT_LOAD_ROUTE_PROFILE}" && [ -f "bench/rvbbit_route_profile.json" ]; then
        say "loading Rvbbit route profile"
        ${COMPOSE} exec -T bench python /bench/rvbbit_route_load_profile.py \
            --profile /bench/rvbbit_route_profile.json \
            --name bench-combined
    fi
fi

ensure_clickbench_gpu_gqe_available

# ---- 4. Load --------------------------------------------------------------
if [ -z "${SKIP_LOAD:-}" ]; then
    say "loading ${LIMIT} rows into [${SYSTEMS}]"
    ${COMPOSE} exec -T \
        -e "BENCH_LIMIT=${LIMIT}" -e "BENCH_SYSTEMS=${SYSTEMS}" "${LOAD_ENV[@]}" \
        bench python /bench/clickbench/load_all.py
else
    say "skipping load (SKIP_LOAD set)"
fi

ensure_clickbench_hive_variants_ready
ensure_clickbench_vortex_variants_ready
prewarm_clickbench_gpu_gqe_catalog

# ---- 5. Run queries -------------------------------------------------------
say "running queries"
${COMPOSE} exec -T \
    -e "BENCH_SYSTEMS=${SYSTEMS}" -e "BENCH_REPEATS=${REPEATS}" \
    -e "BENCH_TIMEOUT=${TIMEOUT_S}" "${QUERIES_ENV[@]}" "${DUCK_HOT_ENV[@]}" \
    bench python /bench/clickbench/run_queries.py

# ---- 6. Pretty-print + save ----------------------------------------------
say "formatting report"
mkdir -p "$(dirname "${REPORT_FILE}")"

# Save uncolored to file (NO_COLOR) and print colored to console
${COMPOSE} exec -T -e NO_COLOR=1 bench \
    python /bench/clickbench/format_report.py \
    > "${REPORT_FILE}"

${COMPOSE} exec -T -e FORCE_COLOR=1 bench \
    python /bench/clickbench/format_report.py

record_benchmark_history

# Regenerate the interactive HTML bench browser (dark-mode, per-query engine
# badges) from bench_history — best-effort, never fails the run.
if command -v python3 >/dev/null 2>&1; then
    python3 bench/report/generate_report.py 2>/dev/null \
        || warn "HTML report generation failed (bench/report/generate_report.py)"
fi

say "report saved to ${REPORT_FILE}"
echo "raw JSON at bench/clickbench/results/last_run.json"
