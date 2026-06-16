//! Phase 1: in-process DataFusion for the `datafusion_*` route candidates.
//!
//! Phase 0 (`df_probe_*`) proved DataFusion 49 embeds cleanly inside a pgrx
//! backend. Phase 1 promotes that substrate to a real engine that the router
//! can call instead of forking `rvbbit-duck`. The dispatch lives in
//! `duck_backend::engine_query_json`; flip `rvbbit.df_inprocess = on` to
//! route the `datafusion` engine through here.
//!
//! Catalog discovery mirrors `rvbbit_duck::main::rvbbit_row_group_catalog`:
//! we ask the same eligibility questions (no dirty heap tail, no pending
//! deletes, parquet files exist) but answer them via SPI instead of a
//! `postgres::Client` round-trip — we're already inside the backend, so
//! catalog access is essentially free.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
#[cfg(not(test))]
use std::ffi::{CStr, CString};
use std::path::Path as FsPath;
use std::sync::{Arc, OnceLock};
use std::time::Instant;

use datafusion::arrow::array::{
    cast::{as_boolean_array, as_primitive_array, as_string_array},
    Array, ArrayRef,
};
use datafusion::arrow::datatypes::{
    DataType, Float32Type, Float64Type, Int16Type, Int32Type, Int64Type, Int8Type, UInt16Type,
    UInt32Type, UInt64Type, UInt8Type,
};
use datafusion::arrow::record_batch::RecordBatch;
use datafusion::arrow::util::display::array_value_to_string;
use datafusion::catalog::{CatalogProvider, MemorySchemaProvider};
use datafusion::datasource::file_format::parquet::ParquetFormat;
use datafusion::datasource::listing::{
    ListingOptions, ListingTable, ListingTableConfig, ListingTableUrl,
};
use datafusion::datasource::MemTable;
use datafusion::prelude::{ParquetReadOptions, SessionConfig, SessionContext};
use lru::LruCache;
use parking_lot::Mutex;
use pgrx::extension_sql;
use pgrx::prelude::*;
use pgrx::{JsonB, Spi};
use serde_json::{json, Value};
use tokio::runtime::{Builder, Runtime};
use vortex::io::session::RuntimeSessionExt;
use vortex::session::VortexSession;
use vortex::VortexSessionDefault;
use vortex_datafusion::VortexFormat;

use crate::time_travel::AsOf;

const PROBE_TABLE: &str = "t";
const DEFAULT_HOT_STORE_BUDGET_MB: usize = 512;
const DEFAULT_HOT_STORE_ROUTE_MAX_ROWS: i64 = 500_000;
const BYTES_PER_MB: usize = 1024 * 1024;

extension_sql!(
    r#"
CREATE TABLE IF NOT EXISTS rvbbit.hot_objects (
    object_key       text PRIMARY KEY,
    table_oid        oid NOT NULL,
    schema_name      text NOT NULL,
    table_name       text NOT NULL,
    columns          text[] NOT NULL DEFAULT ARRAY[]::text[],
    all_columns      boolean NOT NULL DEFAULT true,
    signature        text NOT NULL,
    row_groups       bigint NOT NULL DEFAULT 0,
    row_count        bigint NOT NULL DEFAULT 0,
    parquet_bytes    bigint NOT NULL DEFAULT 0,
    cache_bytes      bigint NOT NULL DEFAULT 0,
    enabled          boolean NOT NULL DEFAULT true,
    loaded_by        text NOT NULL DEFAULT current_user,
    loaded_at        timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    last_error       text,
    CHECK (row_groups >= 0),
    CHECK (row_count >= 0),
    CHECK (parquet_bytes >= 0),
    CHECK (cache_bytes >= 0)
);

CREATE INDEX IF NOT EXISTS hot_objects_table_idx
    ON rvbbit.hot_objects (table_oid, enabled);

CREATE INDEX IF NOT EXISTS hot_objects_updated_idx
    ON rvbbit.hot_objects (updated_at DESC);
"#,
    name = "create_hot_store_catalog",
    requires = ["rvbbit_bootstrap"]
);

thread_local! {
    static RT: RefCell<Option<Runtime>> = const { RefCell::new(None) };
    static CTX: RefCell<Option<SessionContext>> = const { RefCell::new(None) };
    // Phase 1 hot-path optimization: remember which qualified-name we've
    // already registered with what file-set signature, so we can skip the
    // deregister + infer_schema + register round-trip when the catalog
    // hasn't changed between queries. Signature changes the moment a
    // compact() rewrites the file list.
    static REG_CACHE: RefCell<HashMap<String, u64>> = RefCell::new(HashMap::new());
}

/// Drop the DataFusion registration cache for this backend, forcing the next
/// query to re-discover every table's file set. Called from the compaction
/// path so a snapshot_load (which changes row groups AND the visibility floor)
/// can't be served a stale registration in the same session. The signature
/// alone doesn't capture min_visible_generation, so we clear unconditionally.
pub fn invalidate_registration() {
    REG_CACHE.with(|c| c.borrow_mut().clear());
}

#[derive(Clone, Debug)]
struct HotTableState {
    schema: String,
    relname: String,
    row_groups: i64,
    row_count: i64,
    parquet_bytes: i64,
    heap_bytes: i64,
    shadow_heap_retained: bool,
    shadow_heap_dirty: bool,
    delete_count: i64,
    signature: String,
}

#[derive(Clone, Debug)]
struct HotCatalogObject {
    object_key: String,
    table_oid: u32,
    schema: String,
    relname: String,
    columns: Vec<String>,
    all_columns: bool,
    signature: String,
    row_groups: i64,
    row_count: i64,
    parquet_bytes: i64,
    cache_bytes: i64,
}

#[derive(Clone)]
struct HotEntry {
    table_oid: u32,
    bytes: usize,
    batches: Arc<Vec<RecordBatch>>,
}

struct HotStore {
    entries: LruCache<String, HotEntry>,
    bytes: usize,
    hits: u64,
    misses: u64,
    loads: u64,
    evictions: u64,
}

static HOT_STORE: OnceLock<Mutex<HotStore>> = OnceLock::new();

fn hot_store() -> &'static Mutex<HotStore> {
    HOT_STORE.get_or_init(|| {
        Mutex::new(HotStore {
            entries: LruCache::unbounded(),
            bytes: 0,
            hits: 0,
            misses: 0,
            loads: 0,
            evictions: 0,
        })
    })
}

/// Number of worker threads for the per-backend tokio runtime.
///
/// Override via `RVBBIT_DF_THREADS=N`. Special value 0 forces a
/// current_thread runtime (no parallelism, lowest overhead).
///
/// Default: min(available_parallelism, 8). DataFusion's hash aggregate,
/// projection, and parquet scan all use this thread pool, so for the
/// df_inprocess route this is the difference between a single-core run
/// and a real parallel scan. We cap at 8 because each PG backend gets
/// its own pool, and 32-core boxes don't need 32 threads per backend.
fn worker_threads() -> usize {
    if let Ok(raw) = std::env::var("RVBBIT_DF_THREADS") {
        return raw.parse().unwrap_or(0);
    }
    std::thread::available_parallelism()
        .map(|n| n.get().min(8))
        .unwrap_or(4)
}

fn ensure_runtime() {
    RT.with(|cell| {
        if cell.borrow().is_none() {
            let threads = worker_threads();
            let rt = if threads == 0 {
                Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .expect("tokio current_thread runtime")
            } else {
                Builder::new_multi_thread()
                    .worker_threads(threads)
                    .enable_all()
                    .build()
                    .expect("tokio multi_thread runtime")
            };
            *cell.borrow_mut() = Some(rt);
        }
    });
    CTX.with(|cell| {
        if cell.borrow().is_none() {
            // The Utf8View-vs-Utf8 forcing happens on the ParquetFormat
            // we wire up in register_tables, not on the SessionContext.
            // ParquetFormat doesn't honor SessionConfig for that flag
            // (verified against DF 53.1's parquet datasource source).
            let target_partitions = worker_threads().max(1);
            let config = SessionConfig::new().with_target_partitions(target_partitions);
            *cell.borrow_mut() = Some(SessionContext::new_with_config(config));
        }
    });
}

fn with_rt_ctx<R>(f: impl FnOnce(&Runtime, &SessionContext) -> R) -> R {
    ensure_runtime();
    RT.with(|rt_cell| {
        let rt_ref = rt_cell.borrow();
        let rt = rt_ref.as_ref().expect("runtime initialized");
        CTX.with(|ctx_cell| {
            let ctx_ref = ctx_cell.borrow();
            let ctx = ctx_ref.as_ref().expect("session context initialized");
            f(rt, ctx)
        })
    })
}

/// Expose the per-backend tokio Runtime to other modules that need
/// async (e.g. Lance dataset operations) without forcing them to also
/// build a DataFusion SessionContext. The Runtime is shared across
/// every async caller in this backend, so they all see the same task
/// queue and there's only one tokio thread pool per PG backend.
pub(crate) fn with_lance_runtime<R>(f: impl FnOnce(&Runtime) -> R) -> R {
    ensure_runtime();
    RT.with(|rt_cell| {
        let rt_ref = rt_cell.borrow();
        let rt = rt_ref.as_ref().expect("runtime initialized");
        f(rt)
    })
}

fn run_sql_to_text(path: &str, sql: &str) -> Result<Vec<String>, String> {
    with_rt_ctx(|rt, ctx| {
        rt.block_on(async {
            // Idempotent register: drop if present, then add. The fixed table
            // name `t` keeps the user's SQL portable across calls.
            let _ = ctx.deregister_table(PROBE_TABLE);
            ctx.register_parquet(PROBE_TABLE, path, ParquetReadOptions::default())
                .await
                .map_err(|e| format!("register_parquet({path}): {e}"))?;

            let df = ctx.sql(sql).await.map_err(|e| format!("sql plan: {e}"))?;
            let batches: Vec<RecordBatch> =
                df.collect().await.map_err(|e| format!("collect: {e}"))?;

            // We don't deregister on the happy path — the table sticks around
            // for hot-path reuse. Re-registration is idempotent above.

            let mut out = Vec::new();
            for batch in batches {
                let ncols = batch.num_columns();
                let nrows = batch.num_rows();
                for r in 0..nrows {
                    let mut parts = Vec::with_capacity(ncols);
                    for c in 0..ncols {
                        let s = array_value_to_string(batch.column(c), r)
                            .map_err(|e| format!("display row {r} col {c}: {e}"))?;
                        parts.push(s);
                    }
                    out.push(parts.join("|"));
                }
            }
            Ok::<Vec<String>, String>(out)
        })
    })
}

/// Initialize the per-backend tokio Runtime and DataFusion SessionContext.
/// Returns a one-line status. Safe to call multiple times.
#[pg_extern]
fn df_probe_init() -> String {
    let t = Instant::now();
    ensure_runtime();
    format!(
        "datafusion runtime + session context ready ({:.3} ms)",
        t.elapsed().as_secs_f64() * 1000.0
    )
}

/// Run `sql` against a parquet file registered as table `t`.
/// Returns each result row as a `|`-joined text line.
#[pg_extern]
fn df_probe(path: &str, sql: &str) -> SetOfIterator<'static, String> {
    match run_sql_to_text(path, sql) {
        Ok(rows) => SetOfIterator::new(rows),
        Err(e) => pgrx::error!("rvbbit.df_probe: {}", e),
    }
}

/// Benchmark `sql` against `path` over `iters` invocations in this backend.
/// First call is cold (includes parquet metadata read + planner warmup).
#[pg_extern]
fn df_probe_bench(path: &str, sql: &str, iters: i32) -> JsonB {
    let iters = iters.max(1) as usize;
    let mut times_ms: Vec<f64> = Vec::with_capacity(iters);
    let mut row_count: Option<usize> = None;
    let mut error: Option<String> = None;

    for i in 0..iters {
        let t = Instant::now();
        match run_sql_to_text(path, sql) {
            Ok(rows) => {
                times_ms.push(t.elapsed().as_secs_f64() * 1000.0);
                if i == 0 {
                    row_count = Some(rows.len());
                }
            }
            Err(e) => {
                error = Some(e);
                break;
            }
        }
    }

    let cold_ms = times_ms.first().copied();
    let mut hot_sorted: Vec<f64> = times_ms.iter().skip(1).copied().collect();
    hot_sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let hot_min = hot_sorted.first().copied();
    let hot_max = hot_sorted.last().copied();
    let hot_p50 = if hot_sorted.is_empty() {
        None
    } else {
        Some(hot_sorted[hot_sorted.len() / 2])
    };
    let hot_mean = if hot_sorted.is_empty() {
        None
    } else {
        Some(hot_sorted.iter().sum::<f64>() / hot_sorted.len() as f64)
    };

    JsonB(json!({
        "iters_requested": iters,
        "iters_completed": times_ms.len(),
        "cold_ms": cold_ms,
        "hot_min_ms": hot_min,
        "hot_p50_ms": hot_p50,
        "hot_max_ms": hot_max,
        "hot_mean_ms": hot_mean,
        "times_ms": times_ms,
        "row_count_first": row_count,
        "error": error,
    }))
}

// ---------------------------------------------------------------------------
// Phase 1: real engine entry point. Discoverable from SQL as
// rvbbit.df_inprocess_query(sql, max_rows) for direct testing; called from
// duck_backend::engine_query_json when rvbbit.df_inprocess GUC is on.
// ---------------------------------------------------------------------------

/// One eligible rvbbit table — schema-qualified name + the parquet files
/// that make up its scan-layout row groups (canonical order by rg_id).
#[derive(Debug, Clone)]
struct RvbbitTable {
    schema: String,
    relname: String,
    paths: Vec<String>,
    columns: Vec<RvbbitColumn>,
    format: AcceleratorFormat,
}

#[derive(Clone, Debug)]
struct RvbbitColumn {
    name: String,
    typname: String,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum AcceleratorFormat {
    Parquet,
    Vortex,
}

impl AcceleratorFormat {
    fn extension(self) -> &'static str {
        match self {
            AcceleratorFormat::Parquet => ".parquet",
            AcceleratorFormat::Vortex => ".vortex",
        }
    }
}

impl RvbbitTable {
    fn qualified(&self) -> String {
        format!("{}.{}", self.schema, self.relname)
    }
}

fn current_asof() -> Option<AsOf> {
    crate::time_travel::active_as_of()
}

#[cfg(not(test))]
fn guc_setting(name: &str) -> Option<String> {
    let cname = CString::new(name).ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        None
    } else {
        Some(unsafe { CStr::from_ptr(ptr).to_string_lossy().into_owned() })
    }
}

/// SPI-driven mirror of `rvbbit_duck::main::rvbbit_row_group_catalog` for the
/// canonical "scan" layout. Eligibility = no pending deletes and either no
/// retained heap or a clean shadow heap.
///
/// When `asof` is Some(g), the catalog is narrowed to row groups with
/// `generation <= g` AND the heap-authoritative check is bypassed — a
/// historical read doesn't care about the current heap, only about what
/// parquet existed at that generation.
///
/// Returns BTreeMap so iteration order is deterministic across calls (helps
/// when registering many tables — DataFusion order can shift planning).
fn discover_catalog_scan(asof: Option<AsOf>) -> Result<BTreeMap<String, RvbbitTable>, String> {
    // Cross-backend: flush stale caches if another backend compacted.
    crate::custom_scan::refresh_caches_if_stale();
    // SPI returns plain Datums; we copy into a Vec so the borrow ends before
    // we leave the SPI scope.
    struct Row {
        schema: String,
        relname: String,
        paths: Vec<String>,
        columns: Vec<RvbbitColumn>,
        heap_bytes: i64,
        shadow_retained: bool,
        shadow_dirty: bool,
        deletes: i64,
    }
    let mut rows: Vec<Row> = Vec::new();
    // AS OF → generation <= asof; latest (no AS OF) → apply the snapshot
    // visibility floor (generation >= min_visible_generation) so the
    // in-process DataFusion path agrees with the native custom_scan path and
    // a snapshot-load table shows only its newest snapshot. (`t` is the
    // rvbbit.tables LEFT JOIN already in the query below.)
    let asof_predicate = match asof.as_ref() {
        Some(asof) => crate::time_travel::row_group_predicate(asof, "c.oid", "rg.generation"),
        None => crate::time_travel::latest_predicate("c.oid", "rg.generation"),
    };
    let tombstone_predicate = match asof.as_ref() {
        Some(asof) => crate::time_travel::tombstone_predicate(asof, "c.oid", "deleted_generation"),
        None => String::new(),
    };
    // Phase 2 ObjectStore tiered storage: prefer the cold_url when it's set
    // (row group has been migrated to an ObjectStore-addressable location).
    // Otherwise wrap the bare local path as a file:// URL so DataFusion's
    // ListingTableUrl::parse picks the right ObjectStore consistently
    // (LocalFileSystem for file://, ObjectStoreRegistry-resolved for others).
    let sql = format!(
        "
        SELECT n.nspname::text                                            AS nspname,
               c.relname::text                                            AS relname,
               array_agg(coalesce(rg.cold_url,
                                  'file://' || rg.path)
                         ORDER BY rg.rg_id)::text[]                       AS paths,
               coalesce((
                   SELECT array_agg(a.attname::text || E'\t' ||
                                    a.atttypid::regtype::text
                                    ORDER BY a.attnum)::text[]
                   FROM pg_attribute a
                   WHERE a.attrelid = c.oid
                     AND a.attnum > 0
                     AND NOT a.attisdropped
               ), ARRAY[]::text[])                                         AS columns,
               pg_relation_size(c.oid)::bigint                            AS heap_bytes,
               coalesce(t.shadow_heap_retained, false)                    AS shadow_heap_retained,
               coalesce(t.shadow_heap_dirty, false)                       AS shadow_heap_dirty,
               (SELECT count(*)
                  FROM rvbbit.delete_log dl
                 WHERE dl.table_oid = c.oid
                   {tombstone_predicate})::bigint                         AS deletes
        FROM rvbbit.row_groups rg
        JOIN pg_class c       ON c.oid = rg.table_oid
        JOIN pg_namespace n   ON n.oid = c.relnamespace
        JOIN pg_am am         ON am.oid = c.relam
        LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
        WHERE am.amname = 'rvbbit'
          {asof_predicate}
        GROUP BY n.nspname, c.oid, c.relname, t.shadow_heap_retained, t.shadow_heap_dirty
        "
    );
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let schema: String = row.get::<String>(1)?.unwrap_or_default();
            let relname: String = row.get::<String>(2)?.unwrap_or_default();
            let paths: Vec<String> = row
                .get::<Vec<Option<String>>>(3)?
                .unwrap_or_default()
                .into_iter()
                .flatten()
                .collect();
            let columns: Vec<RvbbitColumn> = row
                .get::<Vec<Option<String>>>(4)?
                .unwrap_or_default()
                .into_iter()
                .flatten()
                .filter_map(|entry| {
                    let (name, typname) = entry.split_once('\t')?;
                    Some(RvbbitColumn {
                        name: name.to_string(),
                        typname: typname.to_string(),
                    })
                })
                .collect();
            let heap_bytes: i64 = row.get::<i64>(5)?.unwrap_or(0);
            let shadow_retained: bool = row.get::<bool>(6)?.unwrap_or(false);
            let shadow_dirty: bool = row.get::<bool>(7)?.unwrap_or(false);
            let deletes: i64 = row.get::<i64>(8)?.unwrap_or(0);
            rows.push(Row {
                schema,
                relname,
                paths,
                columns,
                heap_bytes,
                shadow_retained,
                shadow_dirty,
                deletes,
            });
        }
        Ok(())
    })
    .map_err(|e| format!("catalog SPI: {e}"))?;

    let mut out = BTreeMap::new();
    for r in rows {
        // Always reject when relevant tombstones exist — the catalog SQL
        // above already narrowed `r.deletes` to tombstones at <= asof
        // (or all tombstones when asof is unset). In-process DataFusion
        // can't apply a per-(rg_id, ordinal) bitmap during result
        // rendering (DataFusion's RecordBatch doesn't expose origin
        // file/index), so we fall back to the native scan path which
        // does the bitmap filter in custom_scan.rs.
        if r.deletes != 0 {
            continue;
        }
        // Heap-authoritative check only matters at "latest" — historical
        // reads don't care about the current heap.
        if asof.is_none() && r.heap_bytes != 0 && !(r.shadow_retained && !r.shadow_dirty) {
            continue;
        }
        if r.paths.is_empty() {
            continue;
        }
        // Filesystem-existence check only makes sense for local (file://)
        // paths. Remote ObjectStore URLs (s3://, gs://, ...) are checked
        // by DataFusion at registration time; here we trust the catalog.
        if !r.paths.iter().all(|p| {
            if let Some(local) = p.strip_prefix("file://") {
                FsPath::new(local).exists()
            } else {
                // Non-file scheme — assume DataFusion's ObjectStore will
                // surface any I/O error at registration/scan time.
                true
            }
        }) {
            continue;
        }
        let key = format!("{}.{}", r.schema, r.relname);
        out.insert(
            key,
            RvbbitTable {
                schema: r.schema,
                relname: r.relname,
                paths: r.paths,
                columns: r.columns,
                format: AcceleratorFormat::Parquet,
            },
        );
    }
    Ok(out)
}

fn discover_catalog_vortex() -> Result<BTreeMap<String, RvbbitTable>, String> {
    struct Row {
        schema: String,
        relname: String,
        paths: Vec<String>,
        columns: Vec<RvbbitColumn>,
        heap_bytes: i64,
        shadow_retained: bool,
        shadow_dirty: bool,
        deletes: i64,
    }
    let mut rows: Vec<Row> = Vec::new();
    let sql = "
        SELECT n.nspname::text                                      AS nspname,
               c.relname::text                                      AS relname,
               array_agg('file://' || v.path ORDER BY v.rg_id)::text[] AS paths,
               coalesce((
                   SELECT array_agg(a.attname::text || E'\t' ||
                                    a.atttypid::regtype::text
                                    ORDER BY a.attnum)::text[]
                   FROM pg_attribute a
                   WHERE a.attrelid = c.oid
                     AND a.attnum > 0
                     AND NOT a.attisdropped
               ), ARRAY[]::text[])                                  AS columns,
               pg_relation_size(c.oid)::bigint                      AS heap_bytes,
               coalesce(t.shadow_heap_retained, false)              AS shadow_heap_retained,
               coalesce(t.shadow_heap_dirty, false)                 AS shadow_heap_dirty,
               (SELECT count(*)
                  FROM rvbbit.delete_log dl
                 WHERE dl.table_oid = c.oid)::bigint                AS deletes
        FROM rvbbit.row_group_variants v
        JOIN rvbbit.layout_variant_status s
          ON s.table_oid = v.table_oid
         AND s.layout = v.layout
         AND s.status = 'ready'
        JOIN pg_class c       ON c.oid = v.table_oid
        JOIN pg_namespace n   ON n.oid = c.relnamespace
        JOIN pg_am am         ON am.oid = c.relam
        LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
        WHERE am.amname = 'rvbbit'
          AND v.layout = 'vortex_scan'
        GROUP BY n.nspname, c.oid, c.relname, t.shadow_heap_retained, t.shadow_heap_dirty
    ";
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(sql, None, &[])?;
        for row in table {
            let schema: String = row.get::<String>(1)?.unwrap_or_default();
            let relname: String = row.get::<String>(2)?.unwrap_or_default();
            let paths: Vec<String> = row
                .get::<Vec<Option<String>>>(3)?
                .unwrap_or_default()
                .into_iter()
                .flatten()
                .collect();
            let columns: Vec<RvbbitColumn> = row
                .get::<Vec<Option<String>>>(4)?
                .unwrap_or_default()
                .into_iter()
                .flatten()
                .filter_map(|entry| {
                    let (name, typname) = entry.split_once('\t')?;
                    Some(RvbbitColumn {
                        name: name.to_string(),
                        typname: typname.to_string(),
                    })
                })
                .collect();
            let heap_bytes: i64 = row.get::<i64>(5)?.unwrap_or(0);
            let shadow_retained: bool = row.get::<bool>(6)?.unwrap_or(false);
            let shadow_dirty: bool = row.get::<bool>(7)?.unwrap_or(false);
            let deletes: i64 = row.get::<i64>(8)?.unwrap_or(0);
            rows.push(Row {
                schema,
                relname,
                paths,
                columns,
                heap_bytes,
                shadow_retained,
                shadow_dirty,
                deletes,
            });
        }
        Ok(())
    })
    .map_err(|e| format!("vortex catalog SPI: {e}"))?;

    let mut out = BTreeMap::new();
    for r in rows {
        if r.deletes != 0 {
            continue;
        }
        if r.heap_bytes != 0 && !(r.shadow_retained && !r.shadow_dirty) {
            continue;
        }
        if r.paths.is_empty() {
            continue;
        }
        if !r.paths.iter().all(|p| {
            p.strip_prefix("file://")
                .is_some_and(|local| FsPath::new(local).exists())
        }) {
            continue;
        }
        let key = format!("{}.{}", r.schema, r.relname);
        out.insert(
            key,
            RvbbitTable {
                schema: r.schema,
                relname: r.relname,
                paths: r.paths,
                columns: r.columns,
                format: AcceleratorFormat::Vortex,
            },
        );
    }
    Ok(out)
}

/// Fingerprint a table's catalog state for the registration cache. Any
/// change in path list, order, or mtime/size of any file changes the
/// signature — so a compact() between queries is reflected on the next
/// call. mtime is cheap (one stat() per file) and catches the case where
/// the file was rewritten in place under the same name.
///
/// `asof` participates in the signature so that the same table queried at
/// a different AS OF generation gets re-registered with the narrowed file
/// set instead of reusing the cached (full) registration.
fn table_signature(t: &RvbbitTable, asof: Option<&AsOf>) -> u64 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut h = DefaultHasher::new();
    asof.hash(&mut h);
    t.format.hash(&mut h);
    t.paths.len().hash(&mut h);
    t.columns.len().hash(&mut h);
    for c in &t.columns {
        c.name.hash(&mut h);
        c.typname.hash(&mut h);
    }
    for p in &t.paths {
        p.hash(&mut h);
        if let Ok(meta) = std::fs::metadata(p) {
            meta.len().hash(&mut h);
            if let Ok(mtime) = meta.modified() {
                if let Ok(d) = mtime.duration_since(std::time::UNIX_EPOCH) {
                    d.as_nanos().hash(&mut h);
                }
            }
        }
    }
    h.finish()
}

/// DataFusion's default `SessionContext` ships only catalog `datafusion` / schema `public`, so
/// registering a table under a qualified name like `cubes.foo` fails with "failed to resolve
/// schema: cubes" until that schema exists in the catalog. Register an empty in-memory schema
/// provider on demand so any Postgres schema can host accelerated tables. (Surfaced by the
/// `cubes.*` tables — the only schema with materialized row groups — but it applies to every
/// non-`public` schema.) No-op once the schema is present.
fn ensure_df_schema(ctx: &SessionContext, schema: &str) {
    if schema.is_empty() || schema == "public" {
        return;
    }
    // "datafusion" is the default catalog name (SessionConfig::new()).
    if let Some(catalog) = ctx.catalog("datafusion") {
        if catalog.schema(schema).is_none() {
            let _ = catalog.register_schema(schema, Arc::new(MemorySchemaProvider::new()));
        }
    }
}

async fn register_listing_table(
    ctx: &SessionContext,
    qualified: &str,
    t: &RvbbitTable,
) -> Result<(), String> {
    ensure_df_schema(ctx, &t.schema);
    let _ = ctx.deregister_table(qualified);
    let raw_name = raw_table_name(t);
    let date_projection = t.columns.iter().any(|c| c.typname == "date");
    if date_projection {
        let _ = ctx.deregister_table(&raw_name);
    }

    // ListingTable with the explicit list of parquet files. We avoid
    // directory globbing because the directory may contain transient
    // files from a concurrent compact; the row_groups catalog is the
    // authoritative file set.
    // Paths in t.paths are full URLs (file:// for local rows; s3://,
    // gs://, etc. for cold-tier rows when migrated).
    let urls: Vec<ListingTableUrl> = t
        .paths
        .iter()
        .map(|p| ListingTableUrl::parse(p).map_err(|e| format!("ListingTableUrl({p}): {e}")))
        .collect::<Result<Vec<_>, _>>()?;
    let options = match t.format {
        AcceleratorFormat::Parquet => {
            // DataFusion 53 defaults parquet string columns to Utf8View, which
            // our custom_scan tuple-fill code (StringArray-based) doesn't
            // accept. ParquetFormat doesn't honor SessionConfig; the option
            // has to be set on the format directly.
            let format = Arc::new(ParquetFormat::default().with_force_view_types(false));
            ListingOptions::new(format).with_file_extension(t.format.extension())
        }
        AcceleratorFormat::Vortex => {
            let format = Arc::new(VortexFormat::new(VortexSession::default().with_tokio()));
            ListingOptions::new(format).with_file_extension(t.format.extension())
        }
    };
    let schema = options
        .infer_schema(&ctx.state(), &urls[0])
        .await
        .map_err(|e| format!("infer_schema({}): {e}", t.qualified()))?;
    let config = ListingTableConfig::new_with_multi_paths(urls)
        .with_listing_options(options)
        .with_schema(schema);
    let table = ListingTable::try_new(config)
        .map_err(|e| format!("ListingTable::try_new({}): {e}", t.qualified()))?;
    let target_name = if date_projection {
        raw_name.as_str()
    } else {
        qualified
    };
    ctx.register_table(target_name, Arc::new(table))
        .map_err(|e| format!("register_table({target_name}): {e}"))?;
    if date_projection {
        let select_list = t
            .columns
            .iter()
            .map(datafusion_select_expr)
            .collect::<Vec<_>>()
            .join(", ");
        let view_sql = format!("SELECT {select_list} FROM {}", quote_df_ident(&raw_name));
        let view_df = ctx
            .sql(&view_sql)
            .await
            .map_err(|e| format!("planning date-aware view for {}: {e}", t.qualified()))?;
        ctx.register_table(qualified, view_df.into_view())
            .map_err(|e| format!("register date-aware table {qualified}: {e}"))?;
    }
    Ok(())
}

fn raw_table_name(t: &RvbbitTable) -> String {
    format!(
        "__rvbbit_raw_{}_{}",
        sanitize_datafusion_ident(&t.schema),
        sanitize_datafusion_ident(&t.relname)
    )
}

fn sanitize_datafusion_ident(value: &str) -> String {
    value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

fn quote_df_ident(ident: &str) -> String {
    format!("\"{}\"", ident.replace('"', "\"\""))
}

fn datafusion_select_expr(column: &RvbbitColumn) -> String {
    let ident = quote_df_ident(&column.name);
    if column.typname == "date" {
        format!("CAST({ident} AS DATE) AS {ident}")
    } else {
        ident
    }
}

/// Register each eligible table with the per-backend SessionContext as a
/// ListingTable backed by its parquet file list. Skips the dance entirely
/// when the table's signature matches the one we registered last time —
/// hot-path optimization, falls back to the full register on any signature
/// change.
async fn register_tables(
    ctx: &SessionContext,
    tables: &BTreeMap<String, RvbbitTable>,
    asof: Option<&AsOf>,
) -> Result<(), String> {
    for (qualified, t) in tables {
        let sig = table_signature(t, asof);
        let cached_sig = REG_CACHE.with(|c| c.borrow().get(qualified).copied());
        if cached_sig == Some(sig) {
            // File set hasn't changed since we last registered — DataFusion
            // still has the table provider; skip the round-trip.
            continue;
        }

        register_listing_table(ctx, qualified.as_str(), t).await?;

        REG_CACHE.with(|c| c.borrow_mut().insert(qualified.clone(), sig));
    }
    Ok(())
}

fn hot_budget_bytes() -> usize {
    let configured = {
        #[cfg(not(test))]
        {
            guc_setting("rvbbit.hot_store_budget_mb")
                .or_else(|| std::env::var("RVBBIT_HOT_STORE_BUDGET_MB").ok())
        }
        #[cfg(test)]
        {
            std::env::var("RVBBIT_HOT_STORE_BUDGET_MB").ok()
        }
    };
    configured
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(DEFAULT_HOT_STORE_BUDGET_MB)
        .saturating_mul(BYTES_PER_MB)
}

pub(crate) fn hot_store_route_max_rows() -> i64 {
    let configured = {
        #[cfg(not(test))]
        {
            guc_setting("rvbbit.hot_store_route_max_rows")
                .or_else(|| std::env::var("RVBBIT_HOT_STORE_ROUTE_MAX_ROWS").ok())
        }
        #[cfg(test)]
        {
            std::env::var("RVBBIT_HOT_STORE_ROUTE_MAX_ROWS").ok()
        }
    };
    configured
        .and_then(|value| value.trim().parse::<i64>().ok())
        .filter(|value| *value >= 0)
        .unwrap_or(DEFAULT_HOT_STORE_ROUTE_MAX_ROWS)
}

fn hot_catalog_exists() -> bool {
    Spi::get_one::<bool>("SELECT to_regclass('rvbbit.hot_objects') IS NOT NULL")
        .ok()
        .flatten()
        .unwrap_or(false)
}

fn hot_cache_key(object_key: &str, signature: &str) -> String {
    format!("{object_key}|sig={signature}")
}

fn sql_lit(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn sql_text_array(values: &[String]) -> String {
    if values.is_empty() {
        return "ARRAY[]::text[]".to_string();
    }
    format!(
        "ARRAY[{}]::text[]",
        values
            .iter()
            .map(|value| sql_lit(value))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

fn quote_ident(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}

fn quote_qualified(schema: &str, relname: &str) -> String {
    format!("{}.{}", quote_ident(schema), quote_ident(relname))
}

fn object_key_for(table_oid: u32, all_columns: bool, columns: &[String]) -> String {
    if all_columns {
        return format!("rel={table_oid}|cols=*");
    }
    let mut normalized = columns
        .iter()
        .map(|c| c.to_ascii_lowercase())
        .collect::<Vec<_>>();
    normalized.sort();
    let hash = blake3::hash(normalized.join("\0").as_bytes()).to_hex();
    format!("rel={table_oid}|cols={}", &hash[..16])
}

fn hot_current_table_state(table_oid: u32) -> Result<HotTableState, String> {
    let mut state: Option<HotTableState> = None;
    let sql = format!(
        r#"
        SELECT n.nspname::text,
               c.relname::text,
               count(rg.*)::bigint,
               coalesce(sum(rg.n_rows), 0)::bigint,
               coalesce(sum(rg.n_bytes), 0)::bigint,
               pg_relation_size(c.oid)::bigint,
               coalesce(t.shadow_heap_retained, false),
               coalesce(t.shadow_heap_dirty, false),
               (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid)::bigint,
               coalesce(string_agg(
                   rg.rg_id::text || ':' ||
                   coalesce(rg.generation, 0)::text || ':' ||
                   coalesce(rg.n_rows, 0)::text || ':' ||
                   coalesce(rg.n_bytes, 0)::text || ':' ||
                   coalesce(rg.cold_url, rg.path, ''),
                   ',' ORDER BY rg.rg_id
               ), '')::text
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_am am ON am.oid = c.relam
        LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
        LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = c.oid
        WHERE c.oid = {table_oid}::oid
          AND am.amname = 'rvbbit'
        GROUP BY n.nspname, c.oid, c.relname, t.shadow_heap_retained, t.shadow_heap_dirty
        "#
    );
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(&sql, None, &[])?;
        for row in rows {
            let schema: String = row.get(1)?.unwrap_or_default();
            let relname: String = row.get(2)?.unwrap_or_default();
            let row_groups: i64 = row.get(3)?.unwrap_or(0);
            let row_count: i64 = row.get(4)?.unwrap_or(0);
            let parquet_bytes: i64 = row.get(5)?.unwrap_or(0);
            let heap_bytes: i64 = row.get(6)?.unwrap_or(0);
            let shadow_heap_retained: bool = row.get(7)?.unwrap_or(false);
            let shadow_heap_dirty: bool = row.get(8)?.unwrap_or(false);
            let delete_count: i64 = row.get(9)?.unwrap_or(0);
            let raw_signature: String = row.get(10)?.unwrap_or_default();
            let signature = blake3::hash(
                format!(
                    "{table_oid}|{row_groups}|{row_count}|{parquet_bytes}|{heap_bytes}|{shadow_heap_retained}|{shadow_heap_dirty}|{delete_count}|{raw_signature}"
                )
                .as_bytes(),
            )
            .to_hex()
            .to_string();
            state = Some(HotTableState {
                schema,
                relname,
                row_groups,
                row_count,
                parquet_bytes,
                heap_bytes,
                shadow_heap_retained,
                shadow_heap_dirty,
                delete_count,
                signature,
            });
        }
        Ok(())
    })
    .map_err(|e| format!("hot table state SPI: {e}"))?;
    state.ok_or_else(|| format!("relation {table_oid} is not an rvbbit table"))
}

fn table_column_names(table_oid: u32) -> Result<Vec<String>, String> {
    let mut out = Vec::new();
    let sql = format!(
        "SELECT attname::text \
         FROM pg_attribute \
         WHERE attrelid = {table_oid}::oid \
           AND attnum > 0 \
           AND NOT attisdropped \
         ORDER BY attnum"
    );
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(&sql, None, &[])?;
        for row in rows {
            if let Some(name) = row.get::<String>(1)? {
                out.push(name);
            }
        }
        Ok(())
    })
    .map_err(|e| format!("column lookup SPI: {e}"))?;
    Ok(out)
}

fn normalize_hot_columns(
    table_oid: u32,
    requested: Option<Vec<String>>,
) -> Result<(bool, Vec<String>), String> {
    let all = table_column_names(table_oid)?;
    if all.is_empty() {
        return Err(format!("relation {table_oid} has no visible columns"));
    }
    let Some(requested) = requested else {
        return Ok((true, all));
    };
    let requested = requested
        .into_iter()
        .map(|c| c.trim().trim_matches('"').to_string())
        .filter(|c| !c.is_empty())
        .collect::<Vec<_>>();
    if requested.is_empty() {
        return Ok((true, all));
    }
    let mut out = Vec::with_capacity(requested.len());
    for column in requested {
        let Some(actual) = all
            .iter()
            .find(|candidate| candidate.eq_ignore_ascii_case(&column))
        else {
            return Err(format!(
                "column '{column}' does not exist on relation {table_oid}"
            ));
        };
        if !out.iter().any(|existing: &String| existing == actual) {
            out.push(actual.clone());
        }
    }
    Ok((out.len() == all.len(), out))
}

fn hot_record_batch_bytes(batches: &[RecordBatch]) -> usize {
    batches.iter().map(RecordBatch::get_array_memory_size).sum()
}

fn hot_cache_get(object: &HotCatalogObject) -> Option<HotEntry> {
    let key = hot_cache_key(&object.object_key, &object.signature);
    let mut store = hot_store().lock();
    match store.entries.get(&key).cloned() {
        Some(entry) => {
            store.hits = store.hits.saturating_add(1);
            Some(entry)
        }
        None => {
            store.misses = store.misses.saturating_add(1);
            None
        }
    }
}

fn hot_cache_put(object: &HotCatalogObject, batches: Vec<RecordBatch>) -> Result<HotEntry, String> {
    let bytes = hot_record_batch_bytes(&batches);
    let budget = hot_budget_bytes();
    if budget == 0 {
        return Err("rvbbit hot store budget is 0".to_string());
    }
    if bytes == 0 {
        return Err("hot object has zero decoded bytes".to_string());
    }
    if bytes > budget {
        return Err(format!(
            "hot object needs {} byte(s), exceeding hot store budget {}",
            bytes, budget
        ));
    }
    let entry = HotEntry {
        table_oid: object.table_oid,
        bytes,
        batches: Arc::new(batches),
    };
    let key = hot_cache_key(&object.object_key, &object.signature);
    let mut store = hot_store().lock();
    if let Some(old) = store.entries.put(key, entry.clone()) {
        store.bytes = store.bytes.saturating_sub(old.bytes);
    }
    store.bytes = store.bytes.saturating_add(bytes);
    store.loads = store.loads.saturating_add(1);
    while store.bytes > budget {
        let Some((_key, old)) = store.entries.pop_lru() else {
            store.bytes = 0;
            break;
        };
        store.bytes = store.bytes.saturating_sub(old.bytes);
        store.evictions = store.evictions.saturating_add(1);
    }
    Ok(entry)
}

fn hot_cache_evict_table(table_oid: u32) -> i64 {
    let mut store = hot_store().lock();
    let keys = store
        .entries
        .iter()
        .filter_map(|(key, entry)| (entry.table_oid == table_oid).then(|| key.clone()))
        .collect::<Vec<_>>();
    let mut removed = 0i64;
    for key in keys {
        if let Some(entry) = store.entries.pop(&key) {
            store.bytes = store.bytes.saturating_sub(entry.bytes);
            store.evictions = store.evictions.saturating_add(1);
            removed += 1;
        }
    }
    removed
}

fn hot_catalog_upsert(object: &HotCatalogObject) -> Result<(), String> {
    let object_key = sql_lit(&object.object_key);
    let schema = sql_lit(&object.schema);
    let relname = sql_lit(&object.relname);
    let columns = sql_text_array(&object.columns);
    let signature = sql_lit(&object.signature);
    let last_error = "NULL";
    Spi::run(&format!(
        r#"
        INSERT INTO rvbbit.hot_objects
            (object_key, table_oid, schema_name, table_name, columns, all_columns,
             signature, row_groups, row_count, parquet_bytes, cache_bytes, enabled,
             loaded_by, loaded_at, updated_at, last_error)
        VALUES
            ({object_key}, {table_oid}::oid, {schema}, {relname}, {columns},
             {all_columns}, {signature}, {row_groups}, {row_count}, {parquet_bytes},
             {cache_bytes}, true, current_user, now(), now(), {last_error})
        ON CONFLICT (object_key) DO UPDATE SET
             table_oid = EXCLUDED.table_oid,
             schema_name = EXCLUDED.schema_name,
             table_name = EXCLUDED.table_name,
             columns = EXCLUDED.columns,
             all_columns = EXCLUDED.all_columns,
             signature = EXCLUDED.signature,
             row_groups = EXCLUDED.row_groups,
             row_count = EXCLUDED.row_count,
             parquet_bytes = EXCLUDED.parquet_bytes,
             cache_bytes = EXCLUDED.cache_bytes,
             enabled = EXCLUDED.enabled,
             loaded_by = EXCLUDED.loaded_by,
             loaded_at = EXCLUDED.loaded_at,
             updated_at = EXCLUDED.updated_at,
             last_error = EXCLUDED.last_error
        "#,
        table_oid = object.table_oid,
        all_columns = object.all_columns,
        row_groups = object.row_groups,
        row_count = object.row_count,
        parquet_bytes = object.parquet_bytes,
        cache_bytes = object.cache_bytes,
    ))
    .map_err(|e| format!("upsert rvbbit.hot_objects: {e}"))
}

fn discover_hot_catalog() -> Result<Vec<HotCatalogObject>, String> {
    if !hot_catalog_exists() {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(
            "SELECT object_key, table_oid::bigint, schema_name, table_name, columns, \
                    all_columns, signature, row_groups, row_count, parquet_bytes, cache_bytes \
             FROM rvbbit.hot_objects \
             WHERE enabled \
             ORDER BY updated_at DESC",
            None,
            &[],
        )?;
        for row in rows {
            let object_key: String = row.get(1)?.unwrap_or_default();
            let table_oid: i64 = row.get(2)?.unwrap_or(0);
            let schema: String = row.get(3)?.unwrap_or_default();
            let relname: String = row.get(4)?.unwrap_or_default();
            let columns: Vec<String> = row
                .get::<Vec<Option<String>>>(5)?
                .unwrap_or_default()
                .into_iter()
                .flatten()
                .collect();
            let all_columns: bool = row.get(6)?.unwrap_or(false);
            let signature: String = row.get(7)?.unwrap_or_default();
            let row_groups: i64 = row.get(8)?.unwrap_or(0);
            let row_count: i64 = row.get(9)?.unwrap_or(0);
            let parquet_bytes: i64 = row.get(10)?.unwrap_or(0);
            let cache_bytes: i64 = row.get(11)?.unwrap_or(0);
            if table_oid > 0 {
                out.push(HotCatalogObject {
                    object_key,
                    table_oid: table_oid as u32,
                    schema,
                    relname,
                    columns,
                    all_columns,
                    signature,
                    row_groups,
                    row_count,
                    parquet_bytes,
                    cache_bytes,
                });
            }
        }
        Ok(())
    })
    .map_err(|e| format!("discover hot catalog: {e}"))?;
    Ok(out)
}

fn hot_object_is_fresh(object: &HotCatalogObject) -> bool {
    hot_current_table_state(object.table_oid)
        .map(|state| state.signature == object.signature)
        .unwrap_or(false)
}

pub(crate) fn hot_tables_available(tables: &[(u32, String)]) -> (bool, String) {
    if current_asof().is_some() {
        return (false, "hot store is not used for AS OF queries".to_string());
    }
    if hot_budget_bytes() == 0 {
        return (false, "hot store budget is 0".to_string());
    }
    if tables.is_empty() {
        return (false, "query does not reference Rvbbit tables".to_string());
    }
    let objects = match discover_hot_catalog() {
        Ok(objects) => objects,
        Err(err) => return (false, err),
    };
    if objects.is_empty() {
        return (false, "no enabled rvbbit.hot_objects entries".to_string());
    }
    for (oid, qualified) in tables {
        let Some(object) = objects
            .iter()
            .find(|object| object.table_oid == *oid && object.all_columns)
        else {
            return (
                false,
                format!("{qualified} is not loaded as an all-column hot object"),
            );
        };
        if !hot_object_is_fresh(object) {
            return (
                false,
                format!("{qualified} hot object is stale; reload it with rvbbit.hot_load"),
            );
        }
    }
    (
        true,
        "DataFusion in-memory hot columnar object available".to_string(),
    )
}

async fn collect_hot_batches_from_parquet(
    object: &HotCatalogObject,
) -> Result<Vec<RecordBatch>, String> {
    let tables = discover_catalog_scan(current_asof())?;
    let qualified = format!("{}.{}", object.schema, object.relname);
    let Some(table) = tables.get(&qualified) else {
        return Err(format!(
            "{qualified} is not eligible for authoritative parquet scan"
        ));
    };
    let target_partitions = worker_threads().max(1);
    let config = SessionConfig::new().with_target_partitions(target_partitions);
    let ctx = SessionContext::new_with_config(config);
    register_listing_table(&ctx, &qualified, table).await?;
    let projection = if object.all_columns {
        "*".to_string()
    } else {
        object
            .columns
            .iter()
            .map(|column| quote_ident(column))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let sql = format!(
        "SELECT {projection} FROM {}",
        quote_qualified(&object.schema, &object.relname)
    );
    let df = ctx
        .sql(&sql)
        .await
        .map_err(|e| format!("hot load plan: {e}"))?;
    df.collect()
        .await
        .map_err(|e| format!("hot load collect: {e}"))
}

async fn ensure_hot_entry(object: &HotCatalogObject) -> Result<HotEntry, String> {
    if !hot_object_is_fresh(object) {
        return Err(format!(
            "{}.{} hot object is stale; reload it with rvbbit.hot_load",
            object.schema, object.relname
        ));
    }
    if let Some(entry) = hot_cache_get(object) {
        return Ok(entry);
    }
    let batches = collect_hot_batches_from_parquet(object).await?;
    if batches.is_empty() {
        return Err(format!(
            "{}.{} produced no hot batches",
            object.schema, object.relname
        ));
    }
    hot_cache_put(object, batches)
}

async fn register_hot_tables(ctx: &SessionContext) -> Result<Vec<HotCatalogObject>, String> {
    let objects = discover_hot_catalog()?;
    let mut registered = Vec::new();
    for object in objects {
        if !hot_object_is_fresh(&object) {
            continue;
        }
        let entry = ensure_hot_entry(&object).await?;
        let Some(first) = entry.batches.first() else {
            continue;
        };
        let qualified = format!("{}.{}", object.schema, object.relname);
        ensure_df_schema(ctx, &object.schema);
        REG_CACHE.with(|cache| {
            cache.borrow_mut().remove(&qualified);
        });
        let _ = ctx.deregister_table(&qualified);
        let partitions = entry
            .batches
            .iter()
            .cloned()
            .map(|batch| vec![batch])
            .collect::<Vec<_>>();
        let table = MemTable::try_new(first.schema(), partitions).map_err(|e| {
            format!(
                "MemTable::try_new({}.{}): {e}",
                object.schema, object.relname
            )
        })?;
        ctx.register_table(&qualified, Arc::new(table))
            .map_err(|e| {
                format!(
                    "register hot table {}.{}: {e}",
                    object.schema, object.relname
                )
            })?;
        registered.push(object);
    }
    Ok(registered)
}

async fn execute_sql_json(
    ctx: &SessionContext,
    sql: &str,
    max_rows: usize,
) -> Result<Value, String> {
    let df = ctx.sql(sql).await.map_err(|e| format!("sql plan: {e}"))?;
    let batches: Vec<RecordBatch> = df.collect().await.map_err(|e| format!("collect: {e}"))?;

    let columns: Vec<String> = batches
        .first()
        .map(|b| {
            b.schema()
                .fields()
                .iter()
                .map(|f| f.name().clone())
                .collect()
        })
        .unwrap_or_default();

    let mut rows: Vec<Value> = Vec::new();
    let mut row_count = 0usize;
    for batch in &batches {
        for row_idx in 0..batch.num_rows() {
            if rows.len() < max_rows {
                let mut row = Vec::with_capacity(batch.num_columns());
                for col_idx in 0..batch.num_columns() {
                    row.push(arrow_value_to_json(batch.column(col_idx), row_idx)?);
                }
                rows.push(Value::Array(row));
            }
            row_count += 1;
        }
    }

    Ok(json!({
        "status": "ok",
        "row_count": row_count,
        "columns": columns,
        "rows": rows,
    }))
}

/// Run `sql` against the in-process DataFusion engine with all eligible
/// rvbbit tables (canonical scan layout) registered. Returns the
/// sidecar-compatible {status, row_count, columns, rows} JSON shape so
/// `duck_backend::engine_query_json` can consume it unchanged.
pub(crate) fn query_engine(layout: &str, sql: &str, max_rows: i32) -> Result<Value, String> {
    let normalized_layout = layout.trim().to_ascii_lowercase();
    let use_hot_mem = normalized_layout == "mem" || normalized_layout == "memory";
    let use_vortex = normalized_layout == "vortex" || normalized_layout == "vortex_scan";
    if !matches!(
        normalized_layout.as_str(),
        "" | "scan" | "canonical" | "default" | "mem" | "memory" | "vortex" | "vortex_scan"
    ) {
        return Err(format!(
            "in-process datafusion currently only supports scan, mem, and vortex layouts, got {layout}"
        ));
    }

    let asof = current_asof();
    if use_hot_mem && asof.is_some() {
        return Err("hot store is not used for AS OF queries".to_string());
    }
    if use_vortex && asof.is_some() {
        return Err("Vortex accelerator is not used for AS OF queries".to_string());
    }
    let tables = if use_vortex {
        discover_catalog_vortex()?
    } else {
        discover_catalog_scan(asof.clone())?
    };
    if tables.is_empty() {
        return Err(match asof {
            Some(_) if use_vortex => {
                "no ready rvbbit Vortex accelerator files are visible".to_string()
            }
            Some(ref asof) => format!(
                "no rvbbit row groups visible at AS OF {}",
                crate::time_travel::label(asof)
            ),
            None if use_vortex => {
                "no ready rvbbit Vortex accelerator files are visible".to_string()
            }
            None => "no authoritative compacted rvbbit parquet tables are visible".to_string(),
        });
    }

    let max_rows = if max_rows > 0 {
        max_rows as usize
    } else {
        usize::MAX
    };

    with_rt_ctx(|rt, ctx| {
        rt.block_on(async {
            if use_hot_mem {
                let target_partitions = worker_threads().max(1);
                let config = SessionConfig::new().with_target_partitions(target_partitions);
                let mem_ctx = SessionContext::new_with_config(config);
                let registered = register_hot_tables(&mem_ctx).await?;
                if registered.is_empty() {
                    return Err("no fresh hot objects are available".to_string());
                }
                execute_sql_json(&mem_ctx, sql, max_rows).await
            } else {
                register_tables(ctx, &tables, asof.as_ref()).await?;
                execute_sql_json(ctx, sql, max_rows).await
            }
        })
    })
}

/// Arrow → JSON, mirroring `rvbbit_duck::main::arrow_value_to_json`. Keeping
/// the shape identical means the caller never has to know whether the rows
/// came from the sidecar or from here.
fn arrow_value_to_json(array: &ArrayRef, row_idx: usize) -> Result<Value, String> {
    if array.is_null(row_idx) {
        return Ok(Value::Null);
    }
    let v = match array.data_type() {
        DataType::Boolean => json!(as_boolean_array(array.as_ref()).value(row_idx)),
        DataType::Int8 => json!(as_primitive_array::<Int8Type>(array.as_ref()).value(row_idx)),
        DataType::Int16 => json!(as_primitive_array::<Int16Type>(array.as_ref()).value(row_idx)),
        DataType::Int32 => json!(as_primitive_array::<Int32Type>(array.as_ref()).value(row_idx)),
        DataType::Int64 => json!(as_primitive_array::<Int64Type>(array.as_ref()).value(row_idx)),
        DataType::UInt8 => json!(as_primitive_array::<UInt8Type>(array.as_ref()).value(row_idx)),
        DataType::UInt16 => json!(as_primitive_array::<UInt16Type>(array.as_ref()).value(row_idx)),
        DataType::UInt32 => json!(as_primitive_array::<UInt32Type>(array.as_ref()).value(row_idx)),
        DataType::UInt64 => json!(as_primitive_array::<UInt64Type>(array.as_ref()).value(row_idx)),
        DataType::Float32 => {
            json!(as_primitive_array::<Float32Type>(array.as_ref()).value(row_idx))
        }
        DataType::Float64 => {
            json!(as_primitive_array::<Float64Type>(array.as_ref()).value(row_idx))
        }
        DataType::Utf8 => json!(as_string_array(array.as_ref()).value(row_idx)),
        // Date/Timestamp/etc. fall through to display formatting; matches sidecar
        _ => json!(array_value_to_string(array.as_ref(), row_idx)
            .map_err(|e| format!("display row {row_idx}: {e}"))?),
    };
    Ok(v)
}

/// Direct SQL entry point for testing the in-process engine without going
/// through the router/dispatch glue. Exposed as rvbbit.df_inprocess_query.
#[pg_extern]
fn df_inprocess_query(sql: &str, max_rows: default!(i32, 100000)) -> JsonB {
    match query_engine("scan", sql, max_rows) {
        Ok(v) => JsonB(v),
        Err(e) => pgrx::error!("rvbbit.df_inprocess_query: {e}"),
    }
}

#[pg_extern]
fn df_hot_query(sql: &str, max_rows: default!(i32, 100000)) -> JsonB {
    match query_engine("mem", sql, max_rows) {
        Ok(v) => JsonB(v),
        Err(e) => pgrx::error!("rvbbit.df_hot_query: {e}"),
    }
}

fn parse_hot_columns_json(value: &Value) -> Result<Option<Vec<String>>, String> {
    if value.is_null() {
        return Ok(None);
    }
    if value
        .as_str()
        .is_some_and(|s| s.trim() == "*" || s.trim().eq_ignore_ascii_case("all"))
    {
        return Ok(None);
    }
    let Some(items) = value.as_array() else {
        return Err("columns must be null, '*', or a JSON string array".to_string());
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let Some(column) = item.as_str() else {
            return Err("columns JSON array must contain only strings".to_string());
        };
        let column = column.trim();
        if !column.is_empty() && !out.iter().any(|existing: &String| existing == column) {
            out.push(column.to_string());
        }
    }
    if out.is_empty() {
        Ok(None)
    } else {
        Ok(Some(out))
    }
}

fn hot_load_inner(
    rel: pg_sys::Oid,
    requested_columns: Option<Vec<String>>,
) -> Result<Value, String> {
    let table_oid = rel.to_u32();
    let state = hot_current_table_state(table_oid)?;
    if state.row_groups <= 0 {
        return Err(format!(
            "{}.{} has no compacted parquet row groups",
            state.schema, state.relname
        ));
    }
    if state.delete_count > 0 {
        return Err(format!(
            "{}.{} has {} pending delete-log row(s); hot store requires authoritative parquet",
            state.schema, state.relname, state.delete_count
        ));
    }
    if state.heap_bytes > 0 && !(state.shadow_heap_retained && !state.shadow_heap_dirty) {
        return Err(format!(
            "{}.{} has a dirty heap tail; compact before loading the hot store",
            state.schema, state.relname
        ));
    }
    let (all_columns, columns) = normalize_hot_columns(table_oid, requested_columns)?;
    let object_key = object_key_for(table_oid, all_columns, &columns);
    let mut object = HotCatalogObject {
        object_key,
        table_oid,
        schema: state.schema.clone(),
        relname: state.relname.clone(),
        columns,
        all_columns,
        signature: state.signature.clone(),
        row_groups: state.row_groups,
        row_count: state.row_count,
        parquet_bytes: state.parquet_bytes,
        cache_bytes: 0,
    };
    let batches = with_rt_ctx(|rt, _ctx| rt.block_on(collect_hot_batches_from_parquet(&object)))?;
    if batches.is_empty() {
        return Err(format!(
            "{}.{} produced no RecordBatches",
            object.schema, object.relname
        ));
    }
    object.cache_bytes = hot_record_batch_bytes(&batches) as i64;
    let entry = hot_cache_put(&object, batches)?;
    hot_catalog_upsert(&object)?;
    Ok(json!({
        "status": "ok",
        "object_key": object.object_key,
        "table": format!("{}.{}", object.schema, object.relname),
        "table_oid": object.table_oid,
        "all_columns": object.all_columns,
        "columns": object.columns,
        "row_groups": object.row_groups,
        "row_count": object.row_count,
        "parquet_bytes": object.parquet_bytes,
        "cache_bytes": entry.bytes,
        "budget_bytes": hot_budget_bytes(),
        "signature": object.signature,
    }))
}

/// Manually load an rvbbit table into the per-backend hot columnar store and
/// record the intent in rvbbit.hot_objects for lazy loading by other backends.
#[pg_extern(volatile)]
fn hot_load(rel: pg_sys::Oid) -> JsonB {
    match hot_load_inner(rel, None) {
        Ok(value) => JsonB(value),
        Err(err) => pgrx::error!("rvbbit.hot_load: {err}"),
    }
}

/// Manually load a column projection into the hot store. `columns` is either
/// null/'*' for all columns, or a JSON array of column names.
#[pg_extern(volatile)]
fn hot_load_columns(rel: pg_sys::Oid, columns: JsonB) -> JsonB {
    let requested = parse_hot_columns_json(&columns.0)
        .unwrap_or_else(|err| pgrx::error!("rvbbit.hot_load_columns: {err}"));
    match hot_load_inner(rel, requested) {
        Ok(value) => JsonB(value),
        Err(err) => pgrx::error!("rvbbit.hot_load_columns: {err}"),
    }
}

#[pg_extern(volatile)]
fn hot_evict(rel: pg_sys::Oid) -> JsonB {
    let table_oid = rel.to_u32();
    let cache_entries = hot_cache_evict_table(table_oid);
    let catalog_rows = Spi::get_one::<i64>(&format!(
        "WITH deleted AS (DELETE FROM rvbbit.hot_objects WHERE table_oid = {table_oid}::oid RETURNING 1) \
         SELECT count(*)::bigint FROM deleted"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    JsonB(json!({
        "status": "ok",
        "table_oid": table_oid,
        "cache_entries_evicted": cache_entries,
        "catalog_rows_deleted": catalog_rows,
    }))
}

#[pg_extern(volatile)]
fn hot_cache_reset() -> JsonB {
    let mut store = hot_store().lock();
    let entries = store.entries.len() as i64;
    store.entries.clear();
    store.bytes = 0;
    JsonB(json!({
        "status": "ok",
        "entries_evicted": entries,
    }))
}

#[pg_extern]
fn hot_status() -> JsonB {
    let catalog = discover_hot_catalog().unwrap_or_default();
    let (entries, bytes, hits, misses, loads, evictions, cached_keys) = {
        let store = hot_store().lock();
        (
            store.entries.len(),
            store.bytes,
            store.hits,
            store.misses,
            store.loads,
            store.evictions,
            store
                .entries
                .iter()
                .map(|(key, _)| key.clone())
                .collect::<Vec<_>>(),
        )
    };
    let objects = catalog
        .iter()
        .map(|object| {
            let fresh = hot_object_is_fresh(object);
            let cache_key = hot_cache_key(&object.object_key, &object.signature);
            let cached = cached_keys.iter().any(|key| key == &cache_key);
            json!({
                "object_key": object.object_key,
                "table": format!("{}.{}", object.schema, object.relname),
                "table_oid": object.table_oid,
                "all_columns": object.all_columns,
                "columns": object.columns,
                "fresh": fresh,
                "cached_in_backend": cached,
                "row_groups": object.row_groups,
                "row_count": object.row_count,
                "parquet_bytes": object.parquet_bytes,
                "cache_bytes": object.cache_bytes,
                "signature": object.signature,
            })
        })
        .collect::<Vec<_>>();
    JsonB(json!({
        "enabled": hot_budget_bytes() > 0,
        "budget_bytes": hot_budget_bytes(),
        "route_max_rows": hot_store_route_max_rows(),
        "backend_cache": {
            "entries": entries,
            "bytes": bytes,
            "hits": hits,
            "misses": misses,
            "loads": loads,
            "evictions": evictions,
        },
        "objects": objects,
    }))
}

/// Phase 2 ObjectStore: read a single rvbbit table's full row set as
/// RecordBatches through the in-process DataFusion path. The custom_scan
/// node calls this when it sees a table whose row groups have all been
/// migrated to a cold tier (cold_url IS NOT NULL) and the native local-
/// file scan therefore returns no row groups — DataFusion reads via its
/// ObjectStore-aware parquet reader, which the std::fs-based
/// `RowGroupReader` cannot.
///
/// Honors `rvbbit.as_of_generation` (the same catalog discovery that
/// query_engine uses); rejects tables with relevant tombstones via the
/// same eligibility logic — operators see a clear error in that
/// (rare and documented) corner.
///
/// Returns RecordBatches with the table's columns in their natural order.
/// CustomScan's fill_slot_from_batch picks out the projection it needs.
#[allow(dead_code)]
pub(crate) fn collect_batches_for_table(table_oid: u32) -> Result<Vec<RecordBatch>, String> {
    let asof = current_asof();
    collect_batches_for_table_asof(table_oid, asof)
}

pub(crate) fn collect_batches_for_table_asof(
    table_oid: u32,
    asof: Option<AsOf>,
) -> Result<Vec<RecordBatch>, String> {
    let tables = discover_catalog_scan(asof.clone())?;
    if tables.is_empty() {
        return Err(format!(
            "no eligible rvbbit row groups for table oid {table_oid} \
             (tombstones at <= asof, or no rows visible at the as_of generation)"
        ));
    }

    // Resolve the table_oid to its qualified name and confirm the table
    // is in the eligible catalog. Looking it up via pg_class avoids any
    // dependency on the ordering DataFusion uses internally.
    let qualified: String = Spi::get_one::<String>(&format!(
        "SELECT n.nspname::text || '.' || c.relname::text \
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
         WHERE c.oid = {table_oid}::oid"
    ))
    .map_err(|e| format!("resolve qualified name: {e}"))?
    .ok_or_else(|| format!("table oid {table_oid} does not exist"))?;

    if !tables.contains_key(&qualified) {
        return Err(format!(
            "table {qualified} is not eligible for in-process DataFusion scan \
             (pending tombstones, dirty heap, or not a rvbbit table)"
        ));
    }

    with_rt_ctx(|rt, ctx| {
        rt.block_on(async {
            register_tables(ctx, &tables, asof.as_ref()).await?;
            // Quote schema + table identifiers so case-sensitive names work.
            // DataFusion accepts standard SQL double-quotes for identifiers.
            let parts: Vec<&str> = qualified.splitn(2, '.').collect();
            let sql = if parts.len() == 2 {
                format!("SELECT * FROM \"{}\".\"{}\"", parts[0], parts[1])
            } else {
                format!("SELECT * FROM \"{}\"", qualified)
            };
            let df = ctx.sql(&sql).await.map_err(|e| format!("sql plan: {e}"))?;
            let batches: Vec<RecordBatch> =
                df.collect().await.map_err(|e| format!("collect: {e}"))?;
            Ok::<Vec<RecordBatch>, String>(batches)
        })
    })
}
