#!/usr/bin/env bash
# Build the core Rvbbit Docker release set and optionally push it to GHCR.
#
# Local build:
#   scripts/release/build-and-push.sh --version 1.0.0
#
# Publish:
#   scripts/release/build-and-push.sh --version 1.0.0 --push --tag-latest
#
# Include all catalog capability images, beyond the core runtime/smoke images:
#   scripts/release/build-and-push.sh --version 1.0.0 --with-capabilities
#
# Mutate version files first:
#   scripts/release/build-and-push.sh --version 1.0.1 --bump

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERSION=""
REGISTRY="${REGISTRY:-ghcr.io}"
NAMESPACE="${IMAGE_NAMESPACE:-ryrobes}"
PLATFORM="${PLATFORM:-linux/amd64}"
CAPABILITY_PLATFORM="${CAPABILITY_PLATFORM:-linux/amd64}"
LENS_DIR="${LENS_DIR:-$ROOT/../rvbbit-lens}"
PUSH=0
BUMP=0
TAG_LATEST=0
SKIP_DB=0
SKIP_LENS=0
SKIP_WARREN=0
SKIP_WAREHOUSE_MCP=0
BUILD_CAPABILITIES=0
BUILD_GQE=0
DRY_RUN=0
CHECK_PUBLIC=0
CORE_CAPABILITY_IDS=("runtimes/python-runtime" "runtimes/mcp-gateway" "smoke/warren-echo")

usage() {
    cat >&2 <<EOF
Build the core Rvbbit Docker release set and optionally push it to GHCR.

Examples:
  scripts/release/build-and-push.sh --version 1.0.0
  scripts/release/build-and-push.sh --version 1.0.0 --push --tag-latest
  scripts/release/build-and-push.sh --version 1.0.1 --bump
  scripts/release/build-and-push.sh --version 1.0.0 --with-capabilities

Options:
  --version X.Y.Z          Required release version.
  --registry REGISTRY      Default: $REGISTRY
  --namespace OWNER        Default: $NAMESPACE
  --platform PLATFORM      Core image platform(s). Default: $PLATFORM
  --capability-platform P  Capability image platform(s). Default: $CAPABILITY_PLATFORM
  --lens-dir DIR           Default: $LENS_DIR
  --push                   Push images instead of loading locally.
  --tag-latest             Also tag :latest.
  --bump                   Update Cargo/control/Lens versions before building.
  --skip-db
  --skip-lens
  --skip-warren
  --skip-warehouse-mcp     Skip the Warehouse MCP server image.
  --with-capabilities      Also build/push all catalog capability images.
  --with-gqe               Also build rvbbit-postgres-gqe LOCALLY from the release
                           base (NEVER pushed: its CUDA/RAPIDS layer is >40GB,
                           over GHCR's per-layer limit — GPU deploy boxes build it
                           from the published base via the shipped
                           docker-compose.release-gqe.yml instead).
  --skip-capabilities      Deprecated no-op; full catalog images are skipped by default.
  --check-public          After push, verify anonymous pull access with a clean Docker config.
  --dry-run                Print commands without running Docker builds.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --registry) REGISTRY="$2"; shift 2 ;;
        --namespace) NAMESPACE="$2"; shift 2 ;;
        --platform) PLATFORM="$2"; shift 2 ;;
        --capability-platform) CAPABILITY_PLATFORM="$2"; shift 2 ;;
        --lens-dir) LENS_DIR="$2"; shift 2 ;;
        --push) PUSH=1; shift ;;
        --tag-latest) TAG_LATEST=1; shift ;;
        --bump) BUMP=1; shift ;;
        --skip-db) SKIP_DB=1; shift ;;
        --skip-lens) SKIP_LENS=1; shift ;;
        --skip-warren) SKIP_WARREN=1; shift ;;
        --skip-warehouse-mcp) SKIP_WAREHOUSE_MCP=1; shift ;;
        --with-capabilities) BUILD_CAPABILITIES=1; shift ;;
        --with-gqe) BUILD_GQE=1; shift ;;
        --skip-capabilities) BUILD_CAPABILITIES=0; shift ;;
        --check-public) CHECK_PUBLIC=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

[[ -n "$VERSION" ]] || { usage; exit 2; }
VERSION="${VERSION#v}"
IMAGE_PREFIX="${REGISTRY%/}/${NAMESPACE}"
RELEASE_DIR="$ROOT/dist/release/$VERSION"
CONTEXT_DIR="$RELEASE_DIR/context/rvbbit-sql"
LENS_CONTEXT_DIR="$RELEASE_DIR/context/rvbbit-lens"
CATALOG_JSON="$RELEASE_DIR/capabilities.catalog.$VERSION.json"
SEED_JSON="$RELEASE_DIR/capability_catalog_seed.$VERSION.json"
CAPABILITY_PLAN="$RELEASE_DIR/capability-images.$VERSION.json"

if [[ "$PUSH" -eq 0 && "$PLATFORM" == *,* ]]; then
    echo "multi-platform core builds require --push" >&2
    exit 2
fi
if [[ "$PUSH" -eq 0 && "$CAPABILITY_PLATFORM" == *,* ]]; then
    echo "multi-platform capability builds require --push" >&2
    exit 2
fi

run() {
    echo "+ $*"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        "$@"
    fi
}

run_always() {
    echo "+ $*"
    "$@"
}

build_image() {
    local name="$1"
    local dockerfile="$2"
    local context="$3"
    local platform="$4"
    shift 4
    local image="${IMAGE_PREFIX}/${name}:${VERSION}"
    local cmd=(docker buildx build --platform "$platform" -f "$dockerfile" -t "$image")
    if [[ "$TAG_LATEST" -eq 1 ]]; then
        cmd+=(-t "${IMAGE_PREFIX}/${name}:latest")
    fi
    cmd+=(
        --label "org.opencontainers.image.source=https://github.com/ryrobes/rvbbit-sql"
        --label "org.opencontainers.image.version=$VERSION"
        "$@"
    )
    if [[ "$PUSH" -eq 1 ]]; then
        cmd+=(--push)
    else
        cmd+=(--load)
    fi
    cmd+=("$context")
    run "${cmd[@]}"
}

build_capability_images() {
    local plan_output="$1"
    shift
    local cap_args=(
        "$ROOT/scripts/release/capability-images.py"
        --image-prefix "$IMAGE_PREFIX"
        --version "$VERSION"
        --out-dir "$RELEASE_DIR/capability-builds"
        --platform "$CAPABILITY_PLATFORM"
        --plan-output "$plan_output"
    )
    [[ "$PUSH" -eq 1 ]] && cap_args+=(--push)
    [[ "$TAG_LATEST" -eq 1 ]] && cap_args+=(--tag-latest)
    [[ "$DRY_RUN" -eq 1 ]] && cap_args+=(--dry-run)
    cap_args+=("$@")
    run "${cap_args[@]}"
}

if [[ "$BUMP" -eq 1 ]]; then
    run "$ROOT/scripts/release/bump-version.py" "$VERSION" --lens-dir "$LENS_DIR"
fi

# Gate PUBLISHING on a contiguous ALTER EXTENSION UPDATE chain: an existing
# install must be able to upgrade in place to the new default_version. Local
# (--load) builds are not blocked so day-to-day dev keeps working.
if [[ "$PUSH" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
    run_always "$ROOT/scripts/release/check-migration-chain.py"
fi

mkdir -p "$RELEASE_DIR"

# Captured MCP servers live only in the committed extension seed (no on-disk
# pack), so carry their kind=mcp entries through the packs-only regeneration —
# otherwise the release would silently drop every MCP server from the catalog.
COMMITTED_SEED="$ROOT/crates/pg_rvbbit/src/capability_catalog_seed.json"
run_always "$ROOT/capabilities/tools/rvbbit-capability" catalog build \
    --image-prefix "$IMAGE_PREFIX" \
    --image-tag "$VERSION" \
    --carry-from "$COMMITTED_SEED" \
    --carry-kinds mcp \
    --output "$CATALOG_JSON"
run_always "$ROOT/capabilities/tools/rvbbit-capability" catalog seed-json \
    --image-prefix "$IMAGE_PREFIX" \
    --image-tag "$VERSION" \
    --carry-from "$COMMITTED_SEED" \
    --carry-kinds mcp \
    --output "$SEED_JSON"

if [[ "$SKIP_DB" -eq 0 || "$SKIP_WARREN" -eq 0 ]]; then
    run rm -rf "$CONTEXT_DIR"
    run mkdir -p "$CONTEXT_DIR"
    run rsync -a --delete \
        --exclude .git \
        --exclude .mypy_cache \
        --exclude .pytest_cache \
        --exclude dist \
        --exclude .rvbbit \
        --exclude bench \
        --exclude node_modules \
        --exclude results \
        --exclude target \
        --exclude test_runs \
        --exclude __pycache__ \
        --exclude '*.log' \
        --exclude '*.pyc' \
        ./ "$CONTEXT_DIR"/
    run cp "$SEED_JSON" "$CONTEXT_DIR/crates/pg_rvbbit/src/capability_catalog_seed.json"
    run cp "$CATALOG_JSON" "$CONTEXT_DIR/capabilities/catalog.json"
fi

if [[ "$SKIP_LENS" -eq 0 ]]; then
    [[ -d "$LENS_DIR" ]] || { echo "Lens dir not found: $LENS_DIR" >&2; exit 2; }
    run rm -rf "$LENS_CONTEXT_DIR"
    run mkdir -p "$LENS_CONTEXT_DIR/rvbbit-capabilities"
    run rsync -a --delete \
        --exclude .git \
        --exclude node_modules \
        --exclude .next \
        --exclude .playwright-mcp \
        --exclude '*.log' \
        "$LENS_DIR"/ "$LENS_CONTEXT_DIR"/
    run rsync -a --delete \
        "$ROOT/capabilities/packs" \
        "$ROOT/capabilities/templates" \
        "$ROOT/capabilities/tools" \
        "$LENS_CONTEXT_DIR/rvbbit-capabilities"/
    run cp "$CATALOG_JSON" "$LENS_CONTEXT_DIR/rvbbit-capabilities/catalog.json"
fi

if [[ "$SKIP_DB" -eq 0 ]]; then
    build_image rvbbit-postgres "$CONTEXT_DIR/docker/Dockerfile.rvbbit" "$CONTEXT_DIR" "$PLATFORM" \
        --label "org.opencontainers.image.title=rvbbit-postgres"
fi

if [[ "$SKIP_WARREN" -eq 0 ]]; then
    build_image rvbbit-warren-agent "$CONTEXT_DIR/docker/Dockerfile.warren-agent" "$CONTEXT_DIR" "$PLATFORM" \
        --label "org.opencontainers.image.title=rvbbit-warren-agent"

    run cargo build --release --locked -p warren-agent
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) asset_arch=amd64 ;;
        aarch64|arm64) asset_arch=arm64 ;;
        *) asset_arch="$arch" ;;
    esac
    run cp "$ROOT/target/release/warren-agent" "$RELEASE_DIR/warren-agent-linux-$asset_arch"
fi

if [[ "$SKIP_LENS" -eq 0 ]]; then
    build_image rvbbit-lens "$LENS_CONTEXT_DIR/Dockerfile" "$LENS_CONTEXT_DIR" "$PLATFORM" \
        --label "org.opencontainers.image.title=rvbbit-lens"
fi

# Warehouse MCP — a self-contained sidecar (server.py + requirements), so it builds
# straight from services/warehouse-mcp; no staged release context needed.
if [[ "$SKIP_WAREHOUSE_MCP" -eq 0 ]]; then
    build_image rvbbit-warehouse-mcp "$ROOT/services/warehouse-mcp/Dockerfile" "$ROOT/services/warehouse-mcp" "$PLATFORM" \
        --label "org.opencontainers.image.title=rvbbit-warehouse-mcp"
fi

# Document-brain sidecars — self-contained, build straight from sidecars/.
# doc-extract is compose-DEFAULT (the brain must read PDFs out of the box;
# 0047 pre-registers its backend at the compose service name). The Drive
# connector ships versioned but runs behind the "gdrive" compose profile —
# credential-gated by nature (GDRIVE_SA_KEY).
build_image rvbbit-doc-extract "$ROOT/sidecars/doc-extract/Dockerfile" "$ROOT/sidecars/doc-extract" "$PLATFORM" \
    --label "org.opencontainers.image.title=rvbbit-doc-extract"
build_image rvbbit-gdrive-connector "$ROOT/sidecars/gdrive-connector/Dockerfile" "$ROOT/sidecars/gdrive-connector" "$PLATFORM" \
    --label "org.opencontainers.image.title=rvbbit-gdrive-connector"

# GPU/GQE image — LOCAL ONLY. The CUDA/RAPIDS toolchain layer is >40GB (over
# GHCR's per-layer limit), so this can never be pushed; GPU deploy boxes build
# it themselves from the PUBLISHED base via docker-compose.release-gqe.yml
# (Dockerfile.rvbbit-gqe copies nothing from its context, so those two files
# are all a GPU box needs). This flag builds/refreshes the same image here for
# release validation on this machine.
if [[ "$BUILD_GQE" -eq 1 ]]; then
    if [[ "$SKIP_DB" -eq 1 ]]; then
        echo "--with-gqe requires the rvbbit-postgres build (drop --skip-db)" >&2
        exit 2
    fi
    run docker buildx build \
        -f "$CONTEXT_DIR/docker/Dockerfile.rvbbit-gqe" \
        -t "rvbbit-postgres-gqe:${VERSION}" \
        --build-arg "RVBBIT_BASE_IMAGE=${IMAGE_PREFIX}/rvbbit-postgres:${VERSION}" \
        --load \
        "$CONTEXT_DIR/docker"
fi

# Deploy kit: everything a fresh box needs, in one folder. The GQE overlay +
# Dockerfile ride along so a GPU box can build its engine from the published
# base without cloning the repo.
DEPLOY_DIR="$RELEASE_DIR/deploy"
run mkdir -p "$DEPLOY_DIR"
for f in docker-compose.release.yml docker-compose.release-gqe.yml docker-compose.uber.yml Dockerfile.rvbbit-gqe; do
    run cp "$ROOT/docker/$f" "$DEPLOY_DIR/$f"
done
if [[ "$DRY_RUN" -eq 0 ]]; then
    cat > "$DEPLOY_DIR/README.md" <<DEPLOY
# Rvbbit ${VERSION} — deploy kit

Fresh box (CPU):
    RVBBIT_VERSION=${VERSION} docker compose -f docker-compose.release.yml up -d

Fresh box (NVIDIA GPU) — PREFLIGHT first (validated on GCP g4/Blackwell):
    # 1. driver present? (if not: install + REBOOT — modprobe alone leaves
    #    NVML "driver/library version mismatch")
    nvidia-smi -L || { sudo apt-get install -y nvidia-driver-580-open nvidia-utils-580 && sudo reboot; }
    # 2. docker GPU runtime present? (if not: nvidia-container-toolkit)
    docker info --format '{{json .Runtimes}}' | grep -q nvidia || {
      # add NVIDIA's apt repo, then:
      sudo apt-get install -y nvidia-container-toolkit
      sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker; }
    # 3. sanity: GPU visible inside the release image
    docker run --rm --gpus all ghcr.io/ryrobes/rvbbit-postgres:${VERSION} nvidia-smi -L
    # 4. start with the GPU overlay — pulls the prebuilt GQE image (~9GB,
    #    covers all CUDA CC 8.0+ GPUs: RTX 30/40/50-series, A100/H100/B200):
    RVBBIT_VERSION=${VERSION} docker compose -f docker-compose.release.yml -f docker-compose.release-gqe.yml up -d
    # (building GQE from source instead stays supported — see the comments in
    #  docker-compose.release-gqe.yml)

Turnkey (lens + warren + bootstrap + capabilities):
    RVBBIT_VERSION=${VERSION} docker compose -f docker-compose.uber.yml up -d

Your first accelerated table (the part everyone hits):
    psql postgresql://postgres:rvbbit@localhost:55433/rvbbit
    CREATE TABLE t USING rvbbit AS SELECT ...;   -- note: USING rvbbit
    SELECT rvbbit.refresh_acceleration('t'::regclass, true);
    -- plain-heap tables work normally but are not accelerated;
    -- refresh_acceleration on one errors with "not an rvbbit table".

Notes:
- If docker requires sudo: sudo usermod -aG docker \$USER (re-login).
- First boot creates the extension and applies all schema migrations
  (including the factory-trained routing models). The 'migrate' one-shot
  service re-applies pending migrations on every 'up', so image upgrades over
  an existing data volume are safe.
- GPU routing (gpu_gqe) is enabled by default and self-gates on runtime
  availability: the plain compose behaves identically on non-GPU boxes.
- Set OPENAI_API_KEY / ANTHROPIC_API_KEY / etc. in the environment (or a .env
  file next to the compose files) to enable semantic operators.
DEPLOY
fi

if [[ "$BUILD_CAPABILITIES" -eq 1 ]]; then
    build_capability_images "$CAPABILITY_PLAN"
else
    core_capability_args=()
    for capability_id in "${CORE_CAPABILITY_IDS[@]}"; do
        core_capability_args+=(--only "$capability_id")
    done
    build_capability_images "$CAPABILITY_PLAN" "${core_capability_args[@]}"
fi

if [[ "$CHECK_PUBLIC" -eq 1 ]]; then
    if [[ "$PUSH" -eq 0 ]]; then
        echo "--check-public requires --push; images must exist in the registry" >&2
        exit 2
    fi
    public_args=(
        "$ROOT/scripts/release/check-public-images.py"
        --image-prefix "$IMAGE_PREFIX"
        --version "$VERSION"
    )
    [[ "$SKIP_DB" -eq 1 ]] && public_args+=(--skip-db)
    [[ "$SKIP_LENS" -eq 1 ]] && public_args+=(--skip-lens)
    [[ "$SKIP_WARREN" -eq 1 ]] && public_args+=(--skip-warren)
    [[ "$BUILD_CAPABILITIES" -eq 1 ]] && public_args+=(--with-capabilities)
    run "${public_args[@]}"
fi

cat <<EOF

Release artifacts staged in:
  $RELEASE_DIR

Image prefix:
  $IMAGE_PREFIX

Release catalog:
  $CATALOG_JSON
  $SEED_JSON

Capability images:
  core: ${CORE_CAPABILITY_IDS[*]}
  full catalog: $(if [[ "$BUILD_CAPABILITIES" -eq 1 ]]; then printf 'included'; else printf 'skipped (use --with-capabilities to build it)'; fi)

Deploy kit (copy this folder to target boxes):
  $DEPLOY_DIR

Clean-slate compose:
  RVBBIT_VERSION=$VERSION docker compose -f docker/docker-compose.release.yml up -d

GPU box (builds GQE engine from the published base; see deploy/README.md):
  RVBBIT_VERSION=$VERSION docker compose -f docker/docker-compose.release.yml -f docker/docker-compose.release-gqe.yml up -d

Turnkey uber compose:
  RVBBIT_VERSION=$VERSION docker compose -f docker/docker-compose.uber.yml up -d
EOF
