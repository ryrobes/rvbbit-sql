# Installing pg_rvbbit

Three supported install paths, ordered from easiest to most flexible.

**Supported PostgreSQL version:** 18. (PG17 backport is tracked but not
shipped — `EXPLAIN (SEMANTIC ON)` and parts of the GROUP BY rewriter use
PG18-only APIs.)

## 1. Pre-built Docker image (recommended for getting started)

```bash
docker run -d --name rvbbit \
    -p 55433:5432 \
    -e POSTGRES_PASSWORD=rvbbit \
    -e POSTGRES_DB=bench \
    ghcr.io/ryrobes/rvbbit-postgres:latest

psql postgresql://postgres:rvbbit@localhost:55433/bench \
    -c 'SELECT rvbbit.rvbbit_version();'
```

The image is `postgres:18` + `pg_rvbbit` + `rvbbit-duck` + first-boot init
that runs `CREATE EXTENSION pg_rvbbit;`. `shared_preload_libraries` is
pre-configured. The published tags follow `vMAJOR.MINOR.PATCH`,
`MAJOR.MINOR`, `MAJOR`, plus `latest` for the most recent release.

To pin a version: `ghcr.io/ryrobes/rvbbit-postgres:0.56.0`.

### Full clean-slate stack

For a single-host install that includes Postgres, Lens, and a Warren agent:

```bash
RVBBIT_VERSION=1.0.0 \
docker compose -f docker/docker-compose.release.yml up -d
```

This uses published images only:

- `ghcr.io/ryrobes/rvbbit-postgres:<version>`
- `ghcr.io/ryrobes/rvbbit-lens:<version>`
- `ghcr.io/ryrobes/rvbbit-warren-agent:<version>`
- one versioned image per built-in Warren capability

The Warren service mounts `/var/run/docker.sock` so it can launch capability
containers on the local Docker host. See `docs/RELEASE_IMAGES.md` for the full
image matrix and release script.

### Turnkey uber stack

For the easiest first-run path, use the uber compose file. It starts Postgres,
Lens, Warren, then runs a one-shot bootstrap container that deploys and verifies
the baseline Warren capabilities:

- `smoke/warren-echo`
- `runtimes/python-runtime`
- `runtimes/mcp-gateway`

```bash
RVBBIT_VERSION=1.0.0 \
docker compose -f docker/docker-compose.uber.yml up -d
```

If the GHCR images are private, create a simple Docker auth config and point the
stack at it so Warren can pull capability images:

```bash
export RVBBIT_DOCKER_CONFIG=/tmp/rvbbit-ghcr-auth
mkdir -p "$RVBBIT_DOCKER_CONFIG"
echo "$CR_PAT" | docker --config "$RVBBIT_DOCKER_CONFIG" login ghcr.io -u "$GH_USER" --password-stdin

RVBBIT_VERSION=1.0.0 \
RVBBIT_DOCKER_CONFIG="$RVBBIT_DOCKER_CONFIG" \
docker compose -f docker/docker-compose.uber.yml up -d
```

If the GHCR packages have been made public, no Docker login or
`RVBBIT_DOCKER_CONFIG` is needed for this compose path.

## 2. Release tarball (for installing into existing PG18 hosts)

Download a release archive from
<https://github.com/ryrobes/rvbbit-sql/releases>:

```bash
VER=0.56.0
ARCH=$(dpkg --print-architecture)   # amd64 or arm64
curl -L -o rvbbit.tar.gz \
    https://github.com/ryrobes/rvbbit-sql/releases/download/v${VER}/rvbbit-${VER}-pg18-linux-${ARCH}.tar.gz

tar xzf rvbbit.tar.gz
cd rvbbit-${VER}-pg18-linux-${ARCH}
sudo ./install.sh
```

`install.sh` auto-detects `pg_config` and drops:

- `pg_rvbbit.so` → `$(pg_config --pkglibdir)`
- `pg_rvbbit*.control` + the generated base SQL (`pg_rvbbit--<version>.sql`) and versioned `pg_rvbbit--<from>--<to>.sql` migration scripts → `$(pg_config --sharedir)/extension`
- `rvbbit-duck` binary → `/usr/local/bin/` (override with `DUCK_BIN_DIR=`)

Then add `pg_rvbbit` to `shared_preload_libraries` in `postgresql.conf`,
restart Postgres, and run `CREATE EXTENSION pg_rvbbit;` in your target
database.

### Building a tarball locally

```bash
make package
ls dist/
# rvbbit-0.56.0-pg18-linux-amd64.tar.gz
```

`make package` builds the docker image (so it inherits all of Docker's
build-time guarantees) and extracts the binaries into a self-contained
archive. The resulting tarball is portable across any Debian/Ubuntu host
running PG18 with matching glibc + libstdc++ — no other runtime deps.

## 3. Build from source (for development or non-PG18 builds)

Requirements:
- Rust stable 1.95+ (`rustup`)
- `cargo-pgrx = 0.18.0` (`cargo install --locked cargo-pgrx --version 0.18.0`)
- PostgreSQL 18 development headers (`postgresql-server-dev-18` on Debian)
- `libzstd-dev`, `libclang-dev`, `pkg-config`, `build-essential`

Build:

```bash
cargo pgrx init --pg18 $(which pg_config)
cargo pgrx install --release --pg-config $(which pg_config)
cargo build --release --locked --manifest-path crates/rvbbit_duck/Cargo.toml
sudo cp crates/rvbbit_duck/target/release/rvbbit-duck /usr/local/bin/
```

## Runtime characteristics

- **No system libs beyond glibc + libstdc++**. `libzstd`, `libduckdb`,
  ONNX Runtime, and tiktoken encodings are all statically linked /
  embedded. `ldd` on the .so and on `rvbbit-duck` returns only the
  standard system libraries.
- **`shared_preload_libraries = 'pg_rvbbit'` is mandatory**. Without it,
  `CREATE EXTENSION` succeeds but planner hooks never register and no
  parquet routing happens — silent fallback to heap scans. `install.sh`
  warns on missing preload.
- **`CREATE EXTENSION pg_rvbbit` requires superuser**. The control file
  sets `superuser = true`. Not usable on hosted Postgres without
  superuser access (RDS, Cloud SQL, Supabase, Neon).
- **First call to `rvbbit.embed()` downloads ~130MB** of model weights
  (`BAAI/bge-small-en-v1.5`) into `$PGDATA/rvbbit/embed_cache/`. Override
  with `RVBBIT_LOCAL_EMBED_CACHE=/path/to/dir` or per-backend
  `transport_opts.cache_dir`.
- **`rvbbit-duck` is looked up at `/usr/local/bin/rvbbit-duck`** by
  default. Override with `RVBBIT_DUCK_BIN=/path/to/binary`.
- **PGDATA paths are read from the `data_directory` GUC**. Override only
  if the `rvbbit-duck` sidecar runs in a separate mount namespace via
  `RVBBIT_PGDATA_PREFIX` + `RVBBIT_VISIBLE_PGDATA_PREFIX`.

## Upgrading

**2.0.14 is the first generally-available release.** In-place
`ALTER EXTENSION ... UPDATE` is supported only *from 2.0.14 onward* —
the versioned `pg_rvbbit--<from>--<to>.sql` migration scripts are
installed alongside the `.control` file and Postgres walks that chain
from your installed version to the new `default_version`, preserving
database state (parquet files, judgment cache, embeddings, KG, route
decisions).

To upgrade a 2.0.14+ install, drop in the new artifacts (tarball,
docker pull, or rebuild), then:

```sql
ALTER EXTENSION pg_rvbbit UPDATE;
```

**Pre-2.0.14 builds were dev/preview and have no in-place upgrade
path.** To move one to a supported release, recreate the extension:

```sql
DROP EXTENSION pg_rvbbit CASCADE;   -- drops the rvbbit schema + catalog
CREATE EXTENSION pg_rvbbit;          -- installs at default_version
```

(Parquet files under `$PGDATA/rvbbit/` survive `DROP EXTENSION`; remove
them too for a fully clean slate.)

The release tooling guarantees the chain stays intact going forward:
`scripts/release/bump-version.py` auto-creates a contiguous upgrade
stub on every version bump, and `make migration-check`
(`scripts/release/check-migration-chain.py`, also enforced before
`--push`) fails the release if any supported version cannot reach
`default_version`.

## Uninstalling

```sql
DROP EXTENSION pg_rvbbit CASCADE;
```

The `CASCADE` drops the `rvbbit` schema and all data. The parquet files
under `$PGDATA/rvbbit/` are *not* removed by `DROP EXTENSION` and must
be cleaned manually:

```bash
sudo rm -rf $PGDATA/rvbbit/
```

To also remove the binaries:

```bash
sudo rm /usr/lib/postgresql/18/lib/pg_rvbbit.so
sudo rm /usr/share/postgresql/18/extension/pg_rvbbit*
sudo rm /usr/local/bin/rvbbit-duck
```
