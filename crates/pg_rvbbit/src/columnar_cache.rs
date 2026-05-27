//! Budgeted per-backend cache for decoded columnar accelerator objects.
//!
//! This is deliberately separate from the semantic operator cache. These
//! entries are physical execution artifacts: decoded Arrow batches, selected
//! row batches, bitmap catalogs, and future dictionary/expression sidecars.

use std::any::Any;
use std::collections::HashMap as StdHashMap;
use std::ffi::{CStr, CString};
use std::sync::{Arc, OnceLock};

use lru::LruCache;
use parking_lot::Mutex;
use pgrx::{name, pg_extern, pg_sys};

const DEFAULT_COLUMNAR_CACHE_MB: usize = 256;
const BYTES_PER_MB: usize = 1024 * 1024;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct HotKey {
    namespace: &'static str,
    key: String,
}

struct HotEntry {
    value: Arc<dyn Any + Send + Sync>,
    bytes: usize,
}

struct HotCache {
    entries: LruCache<HotKey, HotEntry>,
    bytes: usize,
    hits: u64,
    misses: u64,
    inserts: u64,
    evictions: u64,
}

impl HotCache {
    fn new() -> Self {
        Self {
            entries: LruCache::unbounded(),
            bytes: 0,
            hits: 0,
            misses: 0,
            inserts: 0,
            evictions: 0,
        }
    }
}

static CACHE: OnceLock<Mutex<HotCache>> = OnceLock::new();

fn cache() -> &'static Mutex<HotCache> {
    CACHE.get_or_init(|| Mutex::new(HotCache::new()))
}

pub(crate) fn get<T>(namespace: &'static str, key: &str) -> Option<Arc<T>>
where
    T: Any + Send + Sync + 'static,
{
    if budget_bytes() == 0 {
        return None;
    }

    let hot_key = HotKey {
        namespace,
        key: key.to_string(),
    };
    let value = {
        let mut cache = cache().lock();
        match cache.entries.get(&hot_key) {
            Some(entry) => {
                let value = Arc::clone(&entry.value);
                cache.hits = cache.hits.saturating_add(1);
                value
            }
            None => {
                cache.misses = cache.misses.saturating_add(1);
                return None;
            }
        }
    };
    Arc::downcast::<T>(value).ok()
}

pub(crate) fn put<T>(namespace: &'static str, key: String, bytes: usize, value: T) -> Arc<T>
where
    T: Any + Send + Sync + 'static,
{
    let value = Arc::new(value);
    let budget = budget_bytes();
    if budget == 0 || bytes == 0 || bytes > budget {
        return value;
    }

    let hot_key = HotKey { namespace, key };
    let erased: Arc<dyn Any + Send + Sync> = value.clone();
    let mut cache = cache().lock();
    if let Some(old) = cache.entries.put(
        hot_key,
        HotEntry {
            value: erased,
            bytes,
        },
    ) {
        cache.bytes = cache.bytes.saturating_sub(old.bytes);
    }
    cache.bytes = cache.bytes.saturating_add(bytes);
    cache.inserts = cache.inserts.saturating_add(1);
    evict_to_budget(&mut cache, budget);
    value
}

pub(crate) fn invalidate_table(table_oid: u32) {
    let prefix = format!("rel={table_oid}|");
    let mut cache = cache().lock();
    let keys = cache
        .entries
        .iter()
        .filter_map(|(key, _)| key.key.starts_with(&prefix).then(|| key.clone()))
        .collect::<Vec<_>>();
    for key in keys {
        if let Some(entry) = cache.entries.pop(&key) {
            cache.bytes = cache.bytes.saturating_sub(entry.bytes);
            cache.evictions = cache.evictions.saturating_add(1);
        }
    }
}

fn evict_to_budget(cache: &mut HotCache, budget: usize) {
    while cache.bytes > budget {
        let Some((_key, entry)) = cache.entries.pop_lru() else {
            cache.bytes = 0;
            break;
        };
        cache.bytes = cache.bytes.saturating_sub(entry.bytes);
        cache.evictions = cache.evictions.saturating_add(1);
    }
}

fn budget_bytes() -> usize {
    let mb = guc_setting("rvbbit.columnar_cache_mb")
        .or_else(|| std::env::var("RVBBIT_COLUMNAR_CACHE_MB").ok())
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(DEFAULT_COLUMNAR_CACHE_MB);
    mb.saturating_mul(BYTES_PER_MB)
}

fn guc_setting(name: &str) -> Option<String> {
    let cname = CString::new(name).ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        None
    } else {
        Some(
            unsafe { CStr::from_ptr(ptr) }
                .to_string_lossy()
                .into_owned(),
        )
    }
}

#[pg_extern]
fn columnar_cache_reset() -> i64 {
    let mut cache = cache().lock();
    let n = cache.entries.len() as i64;
    cache.entries.clear();
    cache.bytes = 0;
    n
}

#[pg_extern]
fn columnar_cache_stats() -> pgrx::iter::TableIterator<
    'static,
    (
        name!(cache, String),
        name!(entries, i64),
        name!(bytes, i64),
        name!(budget_bytes, i64),
        name!(hits, i64),
        name!(misses, i64),
        name!(inserts, i64),
        name!(evictions, i64),
    ),
> {
    let cache = cache().lock();
    let row = (
        "columnar".to_string(),
        cache.entries.len() as i64,
        cache.bytes as i64,
        budget_bytes() as i64,
        cache.hits as i64,
        cache.misses as i64,
        cache.inserts as i64,
        cache.evictions as i64,
    );
    pgrx::iter::TableIterator::new(vec![row].into_iter())
}

#[pg_extern]
fn columnar_cache_entries() -> pgrx::iter::TableIterator<
    'static,
    (
        name!(namespace, String),
        name!(entries, i64),
        name!(bytes, i64),
    ),
> {
    let cache = cache().lock();
    let mut grouped = StdHashMap::<String, (i64, i64)>::new();
    for (key, entry) in cache.entries.iter() {
        let stats = grouped.entry(key.namespace.to_string()).or_default();
        stats.0 += 1;
        stats.1 += entry.bytes as i64;
    }
    let mut rows = grouped
        .into_iter()
        .map(|(namespace, (entries, bytes))| (namespace, entries, bytes))
        .collect::<Vec<_>>();
    rows.sort_unstable_by(|a, b| a.0.cmp(&b.0));
    pgrx::iter::TableIterator::new(rows.into_iter())
}
