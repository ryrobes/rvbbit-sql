//! Phase 0 spike: DataFusion embedded in-process.
//!
//! Today the `datafusion_*` routes spawn `rvbbit-duck` as a child process and
//! talk to it over stdin/stdout JSON (see `duck_backend.rs`). That's an install
//! step, a fork/exec tax, and a hard wall against query cancellation,
//! streaming, and DataFusion UDF integration with our semantic operators.
//!
//! This module proves the alternative: a single thread-local tokio runtime
//! plus a thread-local `SessionContext` per Postgres backend, with three
//! probe functions to test viability and measure cost.
//!
//! If the spike succeeds, this module becomes the substrate for Phase 1
//! (real `df::query` integrated into the router).

use std::cell::RefCell;
use std::time::Instant;

use datafusion::arrow::record_batch::RecordBatch;
use datafusion::arrow::util::display::array_value_to_string;
use datafusion::prelude::{ParquetReadOptions, SessionContext};
use pgrx::prelude::*;
use pgrx::JsonB;
use serde_json::json;
use tokio::runtime::{Builder, Runtime};

const PROBE_TABLE: &str = "t";

thread_local! {
    static RT: RefCell<Option<Runtime>> = const { RefCell::new(None) };
    static CTX: RefCell<Option<SessionContext>> = const { RefCell::new(None) };
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
