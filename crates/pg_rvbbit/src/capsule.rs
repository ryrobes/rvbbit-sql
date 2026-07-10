//! Query capsules — the brain-side half of hare (serverless query offload).
//!
//! A capsule is everything a credential-less, stateless worker needs to
//! answer one query: the vetted SQL plus the catalog slice the fleet sidecar
//! would otherwise fetch over its DSN (table manifests, row-group URLs —
//! presigned for object stores — column PG types, generation pins, limits).
//! The manifest mirrors rvbbit-duck's internal `RvbbitDuckTable` shape on
//! purpose: the worker deserializes it straight into the same execution path
//! fleet mode uses. See docs/HARE_PLAN.md.
//!
//! v1 constraints (correctness first):
//!   - tables with pending tombstones are capsule-INELIGIBLE (the skip-bitmap
//!     projection doesn't ride along yet) — the error names the table;
//!   - parquet canonical layout only (no vortex/hive variants);
//!   - `file://` paths pass through unsigned — local-loopback dev mode only.

use pgrx::prelude::*;
use pgrx::JsonB;
use serde_json::json;

/// rvbbit.capsule(sql, ttl_secs, presign) — build a self-contained query
/// capsule for a hare. Superuser-gated in v1: a capsule with presigned URLs
/// IS temporary data access, so minting one is a privilege until role-scoped
/// policies exist.
#[pg_extern]
fn capsule(
    sql: &str,
    ttl_secs: default!(i32, 900),
    presign: default!(bool, true),
) -> JsonB {
    if !unsafe { pgrx::pg_sys::superuser() } {
        pgrx::error!("rvbbit.capsule: permission denied — requires superuser (v1)");
    }
    let ttl = std::time::Duration::from_secs(ttl_secs.max(60) as u64);
    let refs = crate::router::capsule_table_refs(sql);
    if refs.is_empty() {
        pgrx::error!("rvbbit.capsule: query references no accelerated rvbbit tables");
    }

    let mut tables = Vec::with_capacity(refs.len());
    let mut published_only = true;
    for t in &refs {
        // Freshness gate — the SAME predicate the sidecar's DSN catalog uses
        // to drop a table from its view, queried LIVE (the router's table
        // metrics are memoized and can miss a just-executed DELETE): pending
        // tombstones, or a retained shadow heap that is dirty, both mean the
        // parquet no longer equals the heap. A capsule must never serve a
        // stale answer — refuse loudly instead.
        let gate_sql = format!(
            "SELECT (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = {oid}::oid)::bigint, \
                    pg_relation_size({oid}::oid)::bigint, \
                    coalesce(t.shadow_heap_retained, false), \
                    coalesce(t.shadow_heap_dirty, false) \
             FROM rvbbit.tables t WHERE t.table_oid = {oid}::oid",
            oid = t.oid
        );
        let mut deletes: i64 = 0;
        let mut heap_bytes: i64 = 0;
        let mut retained = false;
        let mut dirty = false;
        Spi::connect(|client| {
            let tup = client.select(&gate_sql, None, &[]).unwrap_or_else(|e| {
                pgrx::error!(
                    "rvbbit.capsule: freshness gate for {}.{}: {e}",
                    t.schema,
                    t.relname
                )
            });
            for row in tup {
                deletes = row.get(1).ok().flatten().unwrap_or(0);
                heap_bytes = row.get(2).ok().flatten().unwrap_or(0);
                retained = row.get(3).ok().flatten().unwrap_or(false);
                dirty = row.get(4).ok().flatten().unwrap_or(false);
            }
        });
        if deletes > 0 {
            pgrx::error!(
                "rvbbit.capsule: {}.{} has {} pending tombstone(s) — capsule-ineligible \
                 until rebuild_acceleration() folds them (v1 ships no tombstone bitmaps)",
                t.schema,
                t.relname,
                deletes
            );
        }
        if heap_bytes != 0 && !(retained && !dirty) {
            pgrx::error!(
                "rvbbit.capsule: {}.{} shadow heap is dirty or not retained — the parquet \
                 no longer matches the heap; run rvbbit.compact('{}.{}') first",
                t.schema,
                t.relname,
                t.schema,
                t.relname
            );
        }
        // Row groups: prefer the published object-store copy (what a remote,
        // credential-less worker can actually reach); fall back to the local
        // path for loopback dev. Same coalesce the fleet's remote catalog uses.
        let rg_sql = format!(
            "SELECT coalesce(rg.published_url, 'file://' || rg.path) AS url, \
                    rg.published_url IS NOT NULL AS published, \
                    rg.n_rows, rg.n_bytes, rg.generation \
             FROM rvbbit.row_groups rg \
             WHERE rg.table_oid = {}::oid \
             ORDER BY rg.rg_id",
            t.oid
        );
        let mut paths: Vec<String> = Vec::new();
        let mut rows: i64 = 0;
        let mut bytes: i64 = 0;
        let mut generation: i64 = 0;
        Spi::connect(|client| {
            let tup = client.select(&rg_sql, None, &[]).unwrap_or_else(|e| {
                pgrx::error!("rvbbit.capsule: row_groups for {}.{}: {e}", t.schema, t.relname)
            });
            for row in tup {
                let url: String = row.get(1).ok().flatten().unwrap_or_default();
                let published: bool = row.get(2).ok().flatten().unwrap_or(false);
                let n_rows: i64 = row.get(3).ok().flatten().unwrap_or(0);
                let n_bytes: i64 = row.get(4).ok().flatten().unwrap_or(0);
                let gen: i64 = row.get(5).ok().flatten().unwrap_or(0);
                if url.is_empty() {
                    continue;
                }
                if !published {
                    published_only = false;
                }
                let final_url = if presign {
                    crate::storage::presign_get(&url, ttl).unwrap_or_else(|e| {
                        pgrx::error!("rvbbit.capsule: {e}")
                    })
                } else {
                    url
                };
                paths.push(final_url);
                rows += n_rows;
                bytes += n_bytes;
                generation = generation.max(gen);
            }
        });
        if paths.is_empty() {
            pgrx::error!(
                "rvbbit.capsule: {}.{} has no row groups — refresh_acceleration() first",
                t.schema,
                t.relname
            );
        }

        // Columns with PG type names, attnum order. The WORKER applies its
        // supported_pg_type gate — type policy lives in the engine, symmetric
        // with how fleet mode learns columns over the DSN.
        // attname casts to text: pgrx's String decoder rejects the `name`
        // type (same gotcha as current_database() elsewhere in this codebase).
        let col_sql = format!(
            "SELECT a.attname::text, a.atttypid::regtype::text \
             FROM pg_attribute a \
             WHERE a.attrelid = {}::oid AND a.attnum > 0 AND NOT a.attisdropped \
             ORDER BY a.attnum",
            t.oid
        );
        let mut columns: Vec<serde_json::Value> = Vec::new();
        Spi::connect(|client| {
            let tup = client.select(&col_sql, None, &[]).unwrap_or_else(|e| {
                pgrx::error!("rvbbit.capsule: columns for {}.{}: {e}", t.schema, t.relname)
            });
            for row in tup {
                let name: String = row.get(1).ok().flatten().unwrap_or_default();
                let typ: String = row.get(2).ok().flatten().unwrap_or_default();
                if !name.is_empty() {
                    columns.push(json!([name, typ]));
                }
            }
        });

        tables.push(json!({
            "schema": t.schema,
            "relname": t.relname,
            "columns": columns,
            "paths": paths,
            "row_group_rows": rows,
            "row_group_bytes": bytes,
            "generation": generation,
        }));
    }

    JsonB(json!({
        "capsule": 1,
        "sql": sql,
        "engine": "duck",
        "max_rows": 10_000,
        "timeout_s": 60,
        "expires_s": ttl.as_secs(),
        "published_only": published_only,
        "tables": tables,
    }))
}

/// rvbbit.presign(uri, ttl_secs) — presigned GET for one artifact URI.
/// The building block rvbbit.capsule() uses per row group, exposed for
/// doctoring ("can this bucket presign at all?") and future manifest tools.
#[pg_extern]
fn presign(uri: &str, ttl_secs: default!(i32, 900)) -> String {
    if !unsafe { pgrx::pg_sys::superuser() } {
        pgrx::error!("rvbbit.presign: permission denied — requires superuser (v1)");
    }
    let ttl = std::time::Duration::from_secs(ttl_secs.max(60) as u64);
    crate::storage::presign_get(uri, ttl).unwrap_or_else(|e| pgrx::error!("rvbbit.presign: {e}"))
}

/// rvbbit.hare_run(sql, endpoint, ttl_secs) — mint a capsule, POST it to a
/// hare, record the invocation, return a summary. The OBSERVABLE surface for
/// the serverless experiment: every call lands in rvbbit.hare_invocations
/// with server-side vs wire timings, so "does the query eat the network tax?"
/// is answerable from SQL instead of the Cloud Run console. Not router-wired
/// — this is the manual/benchmark path. Endpoint resolution: arg →
/// `SET rvbbit.hare_endpoint` GUC. Auth token from RVBBIT_ENGINE_TOKEN env
/// (sent as X-Rvbbit-Token — Cloud Run's frontend eats Authorization).
#[pg_extern]
fn hare_run(
    sql: &str,
    endpoint: default!(Option<&str>, "NULL"),
    ttl_secs: default!(i32, 900),
) -> JsonB {
    if !unsafe { pgrx::pg_sys::superuser() } {
        pgrx::error!("rvbbit.hare_run: permission denied — requires superuser (v1)");
    }
    let endpoint = endpoint
        .map(str::to_string)
        .or_else(hare_endpoint_guc)
        .unwrap_or_else(|| {
            pgrx::error!(
                "rvbbit.hare_run: no endpoint — pass one or SET rvbbit.hare_endpoint = 'https://...'"
            )
        });
    let token = std::env::var("RVBBIT_ENGINE_TOKEN").unwrap_or_default();
    if token.trim().is_empty() {
        pgrx::error!("rvbbit.hare_run: RVBBIT_ENGINE_TOKEN is not set in the server environment");
    }

    let capsule_doc = capsule(sql, ttl_secs, true).0;
    let n_tables = capsule_doc["tables"].as_array().map(|a| a.len()).unwrap_or(0) as i32;
    let capsule_body = capsule_doc.to_string();
    let capsule_bytes = capsule_body.len() as i64;
    let url = format!("{}/capsule", endpoint.trim_end_matches('/'));

    let started = std::time::Instant::now();
    let response = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(180))
        .build()
        .and_then(|c| {
            c.post(&url)
                .header("X-Rvbbit-Token", token.trim())
                .header("Content-Type", "application/json")
                .body(capsule_body)
                .send()
        });
    let total_ms = started.elapsed().as_secs_f64() * 1000.0;

    let (http_status, body_json, error): (i32, serde_json::Value, Option<String>) = match response
    {
        Ok(resp) => {
            let status = resp.status().as_u16() as i32;
            match resp.text() {
                Ok(text) => match serde_json::from_str::<serde_json::Value>(&text) {
                    Ok(v) => {
                        let err = v.get("error").and_then(|e| e.as_str()).map(str::to_string);
                        (status, v, err)
                    }
                    Err(e) => (status, json!({}), Some(format!("non-JSON response: {e}"))),
                },
                Err(e) => (status, json!({}), Some(format!("reading response: {e}"))),
            }
        }
        Err(e) => (0, json!({}), Some(format!("request failed: {e}"))),
    };
    let ok = error.is_none() && (200..300).contains(&http_status);
    let server_ms = body_json
        .get("server_elapsed_ms")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);
    let engine_ms = body_json.get("elapsed_ms").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let row_count = body_json.get("row_count").and_then(|v| v.as_i64()).unwrap_or(0);
    // wire_ms = everything that ISN'T the hare's own handling: network both
    // ways + TLS + platform routing (+ cold start, when there is one).
    let wire_ms = (total_ms - server_ms).max(0.0);

    let insert = format!(
        "INSERT INTO rvbbit.hare_invocations \
             (endpoint, sql_hash, sql, ok, http_status, row_count, n_tables, capsule_bytes, \
              engine_ms, server_ms, wire_ms, total_ms, error) \
         VALUES ({}, md5({}), {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {})",
        lit(&endpoint),
        lit(sql),
        lit(sql),
        ok,
        http_status,
        row_count,
        n_tables,
        capsule_bytes,
        engine_ms,
        server_ms,
        wire_ms,
        total_ms,
        error.as_deref().map(lit).unwrap_or_else(|| "NULL".to_string()),
    );
    if let Err(e) = Spi::run(&insert) {
        pgrx::warning!("rvbbit.hare_run: telemetry insert failed (result still returned): {e}");
    }

    JsonB(json!({
        "ok": ok,
        "endpoint": endpoint,
        "http_status": http_status,
        "row_count": row_count,
        "engine_ms": engine_ms,
        "server_ms": server_ms,
        "wire_ms": wire_ms,
        "total_ms": total_ms,
        "capsule_bytes": capsule_bytes,
        "error": error,
        "rows_preview": body_json.get("rows").and_then(|r| r.as_array())
            .map(|a| serde_json::Value::Array(a.iter().take(5).cloned().collect()))
            .unwrap_or(serde_json::Value::Null),
    }))
}

fn hare_endpoint_guc() -> Option<String> {
    let cname = std::ffi::CString::new("rvbbit.hare_endpoint").ok()?;
    let ptr = unsafe { pgrx::pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        return None;
    }
    let value = unsafe { std::ffi::CStr::from_ptr(ptr).to_string_lossy().into_owned() };
    let value = value.trim().to_string();
    (!value.is_empty()).then_some(value)
}

fn lit(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

/// rvbbit.brain_pressure() — a cheap live gauge of "how stressed is the
/// brain?" Pure observability for now: collect it alongside benchmark runs
/// and routing breadcrumbs so a future dynamic offload gate ("cube refresh
/// running → export everything eligible") is designed from data, not vibes.
#[pg_extern]
fn brain_pressure() -> JsonB {
    let (load1, load5) = std::fs::read_to_string("/proc/loadavg")
        .ok()
        .and_then(|s| {
            let mut it = s.split_whitespace();
            let l1 = it.next()?.parse::<f64>().ok()?;
            let l5 = it.next()?.parse::<f64>().ok()?;
            Some((l1, l5))
        })
        .unwrap_or((0.0, 0.0));
    let cores = std::thread::available_parallelism()
        .map(|n| n.get() as f64)
        .unwrap_or(1.0);
    let (active, total, waiting): (i64, i64, i64) = Spi::get_three(
        "SELECT count(*) FILTER (WHERE state = 'active' AND pid <> pg_backend_pid())::bigint, \
                count(*)::bigint, \
                count(*) FILTER (WHERE wait_event_type IS NOT NULL AND state = 'active')::bigint \
         FROM pg_stat_activity WHERE backend_type = 'client backend'",
    )
    .map(|(a, b, c)| (a.unwrap_or(0), b.unwrap_or(0), c.unwrap_or(0)))
    .unwrap_or((0, 0, 0));
    // Background churn signals: an active compaction/rebuild or a running
    // cube refresh is exactly the "brain is busy doing important writes"
    // moment the offload gate would care about.
    let ops_active: i64 = Spi::get_one(
        "SELECT count(*)::bigint FROM rvbbit.acceleration_operations \
         WHERE finished_at IS NULL AND started_at > now() - interval '1 hour'",
    )
    .ok()
    .flatten()
    .unwrap_or(0);
    let load_ratio = if cores > 0.0 { load1 / cores } else { 0.0 };
    JsonB(json!({
        "load1": load1,
        "load5": load5,
        "cores": cores,
        "load_ratio": load_ratio,
        "active_backends": active,
        "waiting_backends": waiting,
        "client_backends": total,
        "accel_operations_active": ops_active,
        "pressure": (load_ratio.min(2.0) / 2.0f64).max(
            (active as f64 / (cores * 2.0).max(1.0)).min(1.0)
        ),
    }))
}
