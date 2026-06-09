#!/usr/bin/env bash
# Build the core Rvbbit Docker release set and optionally push it to GHCR.
#
# Local build:
#   scripts/release/build-and-push.sh --version 1.0.0
#
# Publish:
#   scripts/release/build-and-push.sh --version 1.0.0 --push --tag-latest
#
# Include all catalog capability images, beyond the core smoke image:
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
BUILD_CAPABILITIES=0
DRY_RUN=0
CHECK_PUBLIC=0
CORE_CAPABILITY_IDS=("smoke/warren-echo")

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
  --with-capabilities      Also build/push all catalog capability images.
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
        --with-capabilities) BUILD_CAPABILITIES=1; shift ;;
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

mkdir -p "$RELEASE_DIR"

run_always "$ROOT/capabilities/tools/rvbbit-capability" catalog build \
    --image-prefix "$IMAGE_PREFIX" \
    --image-tag "$VERSION" \
    --output "$CATALOG_JSON"
run_always "$ROOT/capabilities/tools/rvbbit-capability" catalog seed-json \
    --image-prefix "$IMAGE_PREFIX" \
    --image-tag "$VERSION" \
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

Clean-slate compose:
  RVBBIT_VERSION=$VERSION docker compose -f docker/docker-compose.release.yml up -d

Turnkey uber compose:
  RVBBIT_VERSION=$VERSION docker compose -f docker/docker-compose.uber.yml up -d
EOF
