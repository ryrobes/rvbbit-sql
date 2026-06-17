//! Object-store backing for the cold tier (`s3://` / `gs://`).
//!
//! rvbbit already has a cold-tier model: `rvbbit.row_groups.cold_url` holds an
//! ObjectStore URL, and any table with cold row groups reads through in-process
//! DataFusion (`df.rs`), which has native ObjectStore parquet support. Until now
//! `migrate_to_cold` only supported `file://`. This module fills the two gaps for
//! real object storage:
//!   - `object_store_for` / `register_for_datafusion`: build + register the S3/GCS
//!     store so DataFusion can READ `s3://` / `gs://` cold_urls.
//!   - `cold_put`: upload a row-group file to an object-store URI (the WRITE side
//!     `rvbbit.migrate_to_cold` calls for remote prefixes).
//!
//! Credentials/region resolve from the environment / instance metadata (an EC2
//! IAM role, a GCE service account, or `AWS_*` / `GOOGLE_*` env vars) — nothing is
//! stored in Postgres. Because cold reads route through DataFusion and the catalog
//! (incl. `cold_url`) is WAL-replicated, a physical standby reads the SAME `s3://`
//! object the primary wrote — an accelerated standby with no per-node file copy.

use std::sync::Arc;

use object_store::aws::AmazonS3Builder;
use object_store::gcp::GoogleCloudStorageBuilder;
use object_store::path::Path as ObjPath;
use object_store::ObjectStore;
use pgrx::prelude::*;
use url::Url;

/// Build an ObjectStore for a cold URL plus the object key within it. Supports
/// `s3://` and `gs://`; credentials/region resolve from the environment / instance
/// metadata. `file://` is handled natively by DataFusion's default store, not here.
pub(crate) fn object_store_for(url: &Url) -> Result<(Arc<dyn ObjectStore>, ObjPath), String> {
    let bucket = url
        .host_str()
        .filter(|h| !h.is_empty())
        .ok_or_else(|| format!("no bucket/host in cold URL '{url}'"))?;
    let key = ObjPath::from(url.path().trim_start_matches('/'));
    let store: Arc<dyn ObjectStore> = match url.scheme() {
        "s3" => Arc::new(
            AmazonS3Builder::from_env()
                .with_bucket_name(bucket)
                .build()
                .map_err(|e| format!("S3 store for '{bucket}': {e}"))?,
        ),
        "gs" => Arc::new(
            GoogleCloudStorageBuilder::from_env()
                .with_bucket_name(bucket)
                .build()
                .map_err(|e| format!("GCS store for '{bucket}': {e}"))?,
        ),
        other => {
            return Err(format!(
                "unsupported cold-tier scheme '{other}://' (use s3:// or gs://)"
            ))
        }
    };
    Ok((store, key))
}

/// Register the object stores referenced by these cold-tier paths on a DataFusion
/// runtime so it can read `s3://` / `gs://` row groups. Bare paths and `file://`
/// use the default store and are skipped. Idempotent per (scheme, bucket). Called
/// from df.rs before scanning a table that has cold row groups.
pub(crate) fn register_for_datafusion(
    runtime: &datafusion::execution::runtime_env::RuntimeEnv,
    paths: &[String],
) -> Result<(), String> {
    use std::collections::HashSet;
    let mut seen: HashSet<(String, String)> = HashSet::new();
    for p in paths {
        let url = match Url::parse(p) {
            Ok(u) => u,
            Err(_) => continue, // bare path / unparseable → default local store
        };
        if !matches!(url.scheme(), "s3" | "gs") {
            continue;
        }
        let bucket = match url.host_str() {
            Some(h) if !h.is_empty() => h.to_string(),
            _ => continue,
        };
        if !seen.insert((url.scheme().to_string(), bucket.clone())) {
            continue;
        }
        let (store, _key) = object_store_for(&url)?;
        let base = Url::parse(&format!("{}://{}", url.scheme(), bucket))
            .map_err(|e| format!("cold base URL: {e}"))?;
        runtime.register_object_store(&base, store);
    }
    Ok(())
}

/// If `rel_oid` has an enabled "keep cold" policy (`rvbbit.cold_tier_policy`),
/// re-upload the freshly written local row groups to the cold tier so the table
/// stays accelerated there without a manual `migrate_to_cold` after every rebuild.
/// Called at the tail of every compaction. Best-effort: a cold-store hiccup logs a
/// warning and leaves the new row groups on local disk rather than failing the
/// compaction that already succeeded — it runs in a subtransaction so a raised
/// error can't abort the caller.
pub(crate) fn maybe_reoffload_cold(rel_oid: u32) {
    // Tolerate the policy table not existing yet (pre-migration backends).
    let has_policy = pgrx::Spi::get_one::<bool>(
        "SELECT to_regclass('rvbbit.cold_tier_policy') IS NOT NULL",
    )
    .ok()
    .flatten()
    .unwrap_or(false);
    if !has_policy {
        return;
    }
    let prefix = match pgrx::Spi::get_one::<String>(&format!(
        "SELECT cold_url_prefix FROM rvbbit.cold_tier_policy \
         WHERE table_oid = {rel_oid}::oid AND enabled"
    )) {
        Ok(Some(p)) => p,
        _ => return, // no policy / disabled
    };
    let escaped = prefix.replace('\'', "''");
    pgrx::PgTryBuilder::new(move || {
        let _ = pgrx::Spi::run(&format!(
            "SELECT rvbbit.migrate_to_cold({rel_oid}::oid::regclass, '{escaped}')"
        ));
    })
    .catch_others(move |caught| {
        pgrx::warning!(
            "rvbbit: keep-cold re-offload for table oid {rel_oid} failed (row groups left on local tier): {caught:?}"
        );
    })
    .execute();
}

/// rvbbit.cold_put(local_path, dest_uri) — upload a local row-group file to an
/// object-store URI (`s3://` / `gs://`); returns bytes uploaded. The WRITE side of
/// the cold tier for remote prefixes (`rvbbit.migrate_to_cold` keeps `file://` as a
/// plain copy). Credentials come from the environment / instance metadata.
#[pg_extern]
fn cold_put(local_path: &str, dest_uri: &str) -> i64 {
    let data = std::fs::read(local_path)
        .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_put: read {local_path}: {e}"));
    let n = data.len() as i64;
    let url = Url::parse(dest_uri)
        .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_put: bad URI '{dest_uri}': {e}"));
    let (store, key) =
        object_store_for(&url).unwrap_or_else(|e| pgrx::error!("rvbbit.cold_put: {e}"));
    crate::df::with_lance_runtime(|rt| {
        rt.block_on(async move {
            // put_opts is the dyn-safe trait method (`put` is RPITIT, not callable
            // through Arc<dyn ObjectStore>).
            store
                .put_opts(&key, data.into(), object_store::PutOptions::default())
                .await
                .map(|_| ())
                .map_err(|e| e.to_string())
        })
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_put: upload to '{dest_uri}': {e}"));
    n
}
