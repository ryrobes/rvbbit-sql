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

/// Presign a GET for an object-store URI so a credential-less worker (a hare)
/// can fetch the artifact directly. `s3://` covers real S3 AND GCS via the
/// S3-interop endpoint (our deployed publish path); native `gs://` signing
/// needs a literal service-account key file (object_store's GCS from_env has
/// no metadata fallback for signing). `file://` / bare paths / already-https
/// URIs pass through untouched — that's the local-loopback dev mode.
pub(crate) fn presign_get(uri: &str, ttl: std::time::Duration) -> Result<String, String> {
    if uri.starts_with("file://")
        || uri.starts_with("http://")
        || uri.starts_with("https://")
        || !uri.contains("://")
    {
        return Ok(uri.to_string());
    }
    let url = Url::parse(uri).map_err(|e| format!("bad URI '{uri}': {e}"))?;
    let bucket = url
        .host_str()
        .filter(|h| !h.is_empty())
        .ok_or_else(|| format!("no bucket/host in URI '{uri}'"))?;
    let key = ObjPath::from(url.path().trim_start_matches('/'));
    fn sign<S: object_store::signer::Signer>(
        store: &S,
        key: &ObjPath,
        ttl: std::time::Duration,
    ) -> Result<String, String> {
        crate::df::with_lance_runtime(|rt| {
            rt.block_on(async move {
                store
                    .signed_url(reqwest::Method::GET, key, ttl)
                    .await
                    .map(|u| u.to_string())
                    .map_err(|e| e.to_string())
            })
        })
    }
    match url.scheme() {
        "s3" => {
            let store = AmazonS3Builder::from_env()
                .with_bucket_name(bucket)
                .build()
                .map_err(|e| format!("S3 store for '{bucket}': {e}"))?;
            sign(&store, &key, ttl).map_err(|e| format!("presign '{uri}': {e}"))
        }
        "gs" => {
            let store = GoogleCloudStorageBuilder::from_env()
                .with_bucket_name(bucket)
                .build()
                .map_err(|e| format!("GCS store for '{bucket}': {e}"))?;
            sign(&store, &key, ttl).map_err(|e| format!("presign '{uri}': {e}"))
        }
        other => Err(format!(
            "unsupported scheme '{other}://' for presigning (use s3:// or gs://)"
        )),
    }
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

/// rvbbit.cold_stat(uri) — HEAD an object-store URI, returning its size in
/// bytes (or erroring if absent). The verification half of eviction and the
/// read probe of the storage doctor. `file://` stats the local path so the
/// same SQL works in dev. Read-only; no superuser gate needed.
#[pg_extern]
fn cold_stat(uri: &str) -> i64 {
    if let Some(path) = uri.strip_prefix("file://") {
        return std::fs::metadata(path)
            .map(|m| m.len() as i64)
            .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_stat: stat {path}: {e}"));
    }
    let url = Url::parse(uri)
        .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_stat: bad URI '{uri}': {e}"));
    let (store, key) =
        object_store_for(&url).unwrap_or_else(|e| pgrx::error!("rvbbit.cold_stat: {e}"));
    let size = crate::df::with_lance_runtime(|rt| {
        rt.block_on(async move {
            // get_opts(head: true) is the dyn-safe HEAD (`head` is RPITIT —
            // same reason cold_put uses put_opts).
            let opts = object_store::GetOptions {
                head: true,
                ..Default::default()
            };
            store
                .get_opts(&key, opts)
                .await
                .map(|r| r.meta.size as i64)
                .map_err(|e| e.to_string())
        })
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_stat: head '{uri}': {e}"));
    size
}

/// rvbbit.cold_delete(uri) — remove an object from the store (or a local file
/// for `file://`). Superuser-gated like cold_put: this is the destructive half
/// of republish cleanup and future bucket GC. Returns true when the object was
/// deleted (idempotent: absent = false, not an error).
#[pg_extern]
fn cold_delete(uri: &str) -> bool {
    if !unsafe { pgrx::pg_sys::superuser() } {
        pgrx::error!("rvbbit.cold_delete: permission denied — requires superuser");
    }
    if let Some(path) = uri.strip_prefix("file://") {
        return match std::fs::remove_file(path) {
            Ok(()) => true,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
            Err(e) => pgrx::error!("rvbbit.cold_delete: unlink {path}: {e}"),
        };
    }
    let url = Url::parse(uri)
        .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_delete: bad URI '{uri}': {e}"));
    let (store, key) =
        object_store_for(&url).unwrap_or_else(|e| pgrx::error!("rvbbit.cold_delete: {e}"));
    crate::df::with_lance_runtime(|rt| {
        rt.block_on(async move {
            // delete_stream is the dyn-safe deletion path (`delete` is RPITIT).
            use futures::StreamExt;
            let locations = futures::stream::iter([Ok(key)]).boxed();
            let mut results = store.delete_stream(locations);
            match results.next().await {
                Some(Ok(_)) => Ok(true),
                Some(Err(object_store::Error::NotFound { .. })) => Ok(false),
                Some(Err(e)) => Err(e.to_string()),
                None => Ok(false),
            }
        })
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_delete: delete '{uri}': {e}"))
}

/// If a publish store is configured (`rvbbit.settings` key `publish_store`) and
/// the table hasn't opted out, upload the freshly written row groups to shared
/// object storage — KEEPING local files and local reads (`published_url`, not
/// `cold_url`). This is the read fleet's publication hook: warrens consume the
/// published copies; the brain never depends on the network for its own scans.
/// Best-effort, same contract as maybe_reoffload_cold: a store hiccup logs a
/// warning and leaves the generation unpublished rather than failing a
/// compaction that already succeeded.
pub(crate) fn maybe_publish(rel_oid: u32) {
    // Tolerate pre-0134 backends (function/table not migrated yet).
    let has_fn = pgrx::Spi::get_one::<bool>(
        "SELECT to_regprocedure('rvbbit.publish_row_groups(regclass)') IS NOT NULL",
    )
    .ok()
    .flatten()
    .unwrap_or(false);
    if !has_fn {
        return;
    }
    // Cheap pre-check so unconfigured installs pay one indexed SELECT, not a
    // subtransaction, on every compact.
    let configured = pgrx::Spi::get_one::<bool>(
        "SELECT coalesce((value->>'enabled')::boolean, false) \
         FROM rvbbit.settings WHERE key = 'publish_store'",
    )
    .ok()
    .flatten()
    .unwrap_or(false);
    if !configured {
        return;
    }
    pgrx::PgTryBuilder::new(move || {
        let _ = pgrx::Spi::run(&format!(
            "SELECT rvbbit.publish_row_groups({rel_oid}::oid::regclass)"
        ));
    })
    .catch_others(move |caught| {
        pgrx::warning!(
            "rvbbit: publication for table oid {rel_oid} failed (generation left unpublished; local reads unaffected): {caught:?}"
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
    // cold_put reads an arbitrary local file (as the postgres OS user) and
    // uploads it to object storage with the server's ambient cloud creds — a
    // file-read + exfiltration primitive. Gate it behind superuser, stricter
    // than the pg_execute_server_program role that `COPY ... TO PROGRAM`
    // requires. Without this a non-superuser holding only USAGE on the rvbbit
    // schema could read e.g. /etc/passwd or SSL keys.
    if !unsafe { pgrx::pg_sys::superuser() } {
        pgrx::error!(
            "rvbbit.cold_put: permission denied — requires superuser (reads a local \
             file and uploads it to object storage)"
        );
    }
    let data = std::fs::read(local_path)
        .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_put: read {local_path}: {e}"));
    let n = data.len() as i64;
    // file:// parity with cold_stat/cold_delete — dev + NFS prefixes work
    // through the same primitive the object-store path uses.
    if let Some(dest) = dest_uri.strip_prefix("file://") {
        if let Some(parent) = std::path::Path::new(dest).parent() {
            std::fs::create_dir_all(parent)
                .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_put: mkdir {}: {e}", parent.display()));
        }
        std::fs::write(dest, &data)
            .unwrap_or_else(|e| pgrx::error!("rvbbit.cold_put: write {dest}: {e}"));
        return n;
    }
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
