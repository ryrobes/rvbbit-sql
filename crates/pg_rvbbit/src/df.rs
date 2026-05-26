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
use std::ffi::{CStr, CString};
use std::path::Path as FsPath;
use std::sync::Arc;
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
use datafusion::datasource::file_format::parquet::ParquetFormat;
use datafusion::datasource::listing::{
    ListingOptions, ListingTable, ListingTableConfig, ListingTableUrl,
};
use datafusion::prelude::{ParquetReadOptions, SessionContext};
use pgrx::prelude::*;
use pgrx::{JsonB, Spi};
use serde_json::{json, Value};
use tokio::runtime::{Builder, Runtime};

const PROBE_TABLE: &str = "t";

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

// Number of worker threads for the per-backend tokio runtime. 0 = use a
// current_thread runtime (no extra threads, lowest overhead, but DataFusion
// can't parallelize aggregates). Otherwise use a multi_thread runtime with
// this many workers. Override per-backend with `SET rvbbit.df_threads = N`
// before the first probe call.
fn worker_threads() -> usize {
    std::env::var("RVBBIT_DF_THREADS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0)
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
            *cell.borrow_mut() = Some(SessionContext::new());
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

fn run_sql_to_text(path: &str, sql: &str) -> Result<Vec<String>, String> {
    with_rt_ctx(|rt, ctx| {
        rt.block_on(async {
            // Idempotent register: drop if present, then add. The fixed table
            // name `t` keeps the user's SQL portable across calls.
            let _ = ctx.deregister_table(PROBE_TABLE);
            ctx.register_parquet(PROBE_TABLE, path, ParquetReadOptions::default())
                .await
                .map_err(|e| format!("register_parquet({path}): {e}"))?;

            let df = ctx
                .sql(sql)
                .await
                .map_err(|e| format!("sql plan: {e}"))?;
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
}

impl RvbbitTable {
    fn qualified(&self) -> String {
        format!("{}.{}", self.schema, self.relname)
    }
}

/// Phase 2 slice 3: read the rvbbit.as_of_generation GUC. A positive value
/// narrows row group selection to `generation <= asof`. Unset / empty / 0
/// means "no AS OF filter — use the latest visible state". Negative values
/// are normalized to None.
///
/// Uses direct GetConfigOption FFI (microseconds) instead of an SPI
/// roundtrip (milliseconds) so this primitive is cheap enough to call on
/// every datafusion_query_json invocation without a measurable per-query
/// tax. Same pattern as duck_backend::guc_setting.
fn current_asof() -> Option<i64> {
    let cname = CString::new("rvbbit.as_of_generation").ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        return None;
    }
    let raw = unsafe { CStr::from_ptr(ptr).to_string_lossy() };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    trimmed.parse::<i64>().ok().filter(|&g| g > 0)
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
fn discover_catalog_scan(asof: Option<i64>) -> Result<BTreeMap<String, RvbbitTable>, String> {
    // SPI returns plain Datums; we copy into a Vec so the borrow ends before
    // we leave the SPI scope.
    struct Row {
        schema: String,
        relname: String,
        paths: Vec<String>,
        heap_bytes: i64,
        shadow_retained: bool,
        shadow_dirty: bool,
        deletes: i64,
    }
    let mut rows: Vec<Row> = Vec::new();
    let asof_predicate = match asof {
        Some(g) => format!("AND rg.generation <= {g}"),
        None => String::new(),
    };
    let sql = format!(
        "
        SELECT n.nspname::text                                            AS nspname,
               c.relname::text                                            AS relname,
               array_agg(rg.path ORDER BY rg.rg_id)::text[]               AS paths,
               pg_relation_size(c.oid)::bigint                            AS heap_bytes,
               coalesce(t.shadow_heap_retained, false)                    AS shadow_heap_retained,
               coalesce(t.shadow_heap_dirty, false)                       AS shadow_heap_dirty,
               (SELECT count(*)
                  FROM rvbbit.delete_log dl
                 WHERE dl.table_oid = c.oid)::bigint                      AS deletes
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
            let heap_bytes: i64 = row.get::<i64>(4)?.unwrap_or(0);
            let shadow_retained: bool = row.get::<bool>(5)?.unwrap_or(false);
            let shadow_dirty: bool = row.get::<bool>(6)?.unwrap_or(false);
            let deletes: i64 = row.get::<i64>(7)?.unwrap_or(0);
            rows.push(Row {
                schema,
                relname,
                paths,
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
        // Skip the parquet-authoritative check when reading historically:
        // the heap's current state is irrelevant to a snapshot at gen <= asof,
        // and a pending delete is by definition AFTER the snapshot we're
        // reconstructing.
        if asof.is_none()
            && (r.deletes != 0 || (r.heap_bytes != 0 && !(r.shadow_retained && !r.shadow_dirty)))
        {
            continue;
        }
        if r.paths.is_empty() {
            continue;
        }
        if !r.paths.iter().all(|p| FsPath::new(p).exists()) {
            continue;
        }
        let key = format!("{}.{}", r.schema, r.relname);
        out.insert(
            key,
            RvbbitTable {
                schema: r.schema,
                relname: r.relname,
                paths: r.paths,
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
fn table_signature(t: &RvbbitTable, asof: Option<i64>) -> u64 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut h = DefaultHasher::new();
    asof.hash(&mut h);
    t.paths.len().hash(&mut h);
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

/// Register each eligible table with the per-backend SessionContext as a
/// ListingTable backed by its parquet file list. Skips the dance entirely
/// when the table's signature matches the one we registered last time —
/// hot-path optimization, falls back to the full register on any signature
/// change.
async fn register_tables(
    ctx: &SessionContext,
    tables: &BTreeMap<String, RvbbitTable>,
    asof: Option<i64>,
) -> Result<(), String> {
    for (qualified, t) in tables {
        let sig = table_signature(t, asof);
        let cached_sig = REG_CACHE.with(|c| c.borrow().get(qualified).copied());
        if cached_sig == Some(sig) {
            // File set hasn't changed since we last registered — DataFusion
            // still has the table provider; skip the round-trip.
            continue;
        }

        let _ = ctx.deregister_table(qualified);

        // ListingTable with the explicit list of parquet files. We avoid
        // directory globbing because the directory may contain transient
        // files from a concurrent compact; the row_groups catalog is the
        // authoritative file set.
        let urls: Vec<ListingTableUrl> = t
            .paths
            .iter()
            .map(|p| {
                ListingTableUrl::parse(format!("file://{p}"))
                    .map_err(|e| format!("ListingTableUrl({p}): {e}"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let format = Arc::new(ParquetFormat::default());
        let options = ListingOptions::new(format).with_file_extension(".parquet");
        let schema = options
            .infer_schema(&ctx.state(), &urls[0])
            .await
            .map_err(|e| format!("infer_schema({}): {e}", t.qualified()))?;
        let config = ListingTableConfig::new_with_multi_paths(urls)
            .with_listing_options(options)
            .with_schema(schema);
        let table = ListingTable::try_new(config)
            .map_err(|e| format!("ListingTable::try_new({}): {e}", t.qualified()))?;
        ctx.register_table(qualified.as_str(), Arc::new(table))
            .map_err(|e| format!("register_table({qualified}): {e}"))?;

        REG_CACHE.with(|c| c.borrow_mut().insert(qualified.clone(), sig));
    }
    Ok(())
}

/// Run `sql` against the in-process DataFusion engine with all eligible
/// rvbbit tables (canonical scan layout) registered. Returns the
/// sidecar-compatible {status, row_count, columns, rows} JSON shape so
/// `duck_backend::engine_query_json` can consume it unchanged.
pub(crate) fn query_engine(layout: &str, sql: &str, max_rows: i32) -> Result<Value, String> {
    // Phase 1 only handles canonical "scan" layout. Hive/cluster variants
    // can be added in a follow-on slice once we exercise this on real data.
    if !matches!(
        layout.trim().to_ascii_lowercase().as_str(),
        "" | "scan" | "canonical" | "default"
    ) {
        return Err(format!(
            "in-process datafusion currently only supports scan layout, got {layout}"
        ));
    }

    let asof = current_asof();
    let tables = discover_catalog_scan(asof)?;
    if tables.is_empty() {
        return Err(match asof {
            Some(g) => format!("no rvbbit row groups visible at AS OF generation {g}"),
            None => "no authoritative compacted rvbbit parquet tables are visible".to_string(),
        });
    }

    let max_rows = if max_rows > 0 { max_rows as usize } else { usize::MAX };

    with_rt_ctx(|rt, ctx| {
        rt.block_on(async {
            register_tables(ctx, &tables, asof).await?;
            let df = ctx.sql(sql).await.map_err(|e| format!("sql plan: {e}"))?;
            let batches: Vec<RecordBatch> =
                df.collect().await.map_err(|e| format!("collect: {e}"))?;

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

            Ok::<Value, String>(json!({
                "status": "ok",
                "row_count": row_count,
                "columns": columns,
                "rows": rows,
            }))
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
