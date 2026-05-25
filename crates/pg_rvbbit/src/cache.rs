//! In-memory LRU cache layered on top of rvbbit.receipts.
//!
//! Why bother:
//!   - Receipts lookup via SPI inside a UDF costs ~1-3ms per call.
//!   - For warm queries (repeated runs over the same inputs) that's
//!     the entire cost.
//!   - HashMap<Vec<u8>, String> lookup is ~1-5μs.
//!   - 200-1000x speedup on cache hits — the difference between
//!     "instant" and "noticeable" for a 5k-row query.
//!
//! Architecture:
//!   1. Operator call computes inputs_hash (blake3 over op+model+inputs+prompt-seed)
//!   2. Check in-memory LRU → hit means we skip both SPI lookup AND provider call
//!   3. Miss: fall back to SPI lookup against rvbbit.receipts (still warm across backends)
//!   4. Miss-miss: actual provider call, log receipt, populate in-mem cache
//!
//! In-memory cache is PER-BACKEND. That's fine — receipts table is the
//! cross-backend persistent cache; this is just the local hot path.

use std::num::NonZeroUsize;
use std::sync::OnceLock;

use lru::LruCache;
use parking_lot::Mutex;

/// Cached entry — operator's RAW LLM output string (not the parsed
/// typed value). The caller's parser turns it into bool/text/float8,
/// which is cheap and deterministic, so we don't bother caching parsed.
pub struct Entry {
    pub output: String,
}

type CacheT = Mutex<LruCache<Vec<u8>, Entry>>;

static CACHE: OnceLock<CacheT> = OnceLock::new();

fn cache() -> &'static CacheT {
    CACHE.get_or_init(|| {
        let cap = std::env::var("RVBBIT_CACHE_SIZE")
            .ok()
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(10_000)
            .max(1);
        Mutex::new(LruCache::new(NonZeroUsize::new(cap).unwrap()))
    })
}

/// Returns Some(output) on hit; None on miss. Fast — single lock + map lookup.
pub fn get(hash: &[u8]) -> Option<String> {
    cache().lock().get(hash).map(|e| e.output.clone())
}

pub fn put(hash: &[u8], output: String) {
    cache().lock().put(hash.to_vec(), Entry { output });
}

/// Drop all in-memory entries. Useful for tests + `rvbbit.flush_cache()`.
pub fn flush() {
    cache().lock().clear();
}

/// Stats for diagnostics / rvbbit.cache_stats().
pub struct Stats {
    pub size: usize,
    pub capacity: usize,
}

pub fn stats() -> Stats {
    let c = cache().lock();
    Stats {
        size: c.len(),
        capacity: c.cap().get(),
    }
}
