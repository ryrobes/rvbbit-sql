# Installing pg_rvbbit

Three supported install paths, ordered from easiest to most flexible.

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
- `pg_rvbbit*.control` + 56 SQL migration files → `$(pg_config --sharedir)/extension`
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

To target a different PG major version, add the matching feature to
`crates/pg_rvbbit/Cargo.toml` (e.g. `pg17 = ["pgrx/pg17"]`) and pass
`--features pg17 --no-default-features` to `cargo pgrx install`. Hooks
and the TAM alias are stable across PG14+; the lock to PG18 is only the
default feature.

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

Drop in the new artifacts (via tarball, docker pull, or rebuild), then:

```sql
ALTER EXTENSION pg_rvbbit UPDATE;
```

The 56 versioned migration scripts are installed alongside the .control
file. `ALTER EXTENSION ... UPDATE` walks the upgrade chain from your
current installed version to the new `default_version`. Database state
(parquet files, judgment cache, embeddings, KG, route decisions) is
preserved.

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
