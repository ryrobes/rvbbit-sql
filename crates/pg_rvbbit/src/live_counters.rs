//! Live, cross-connection semantic-call counters.
//!
//! Semantic-operator receipts (`rvbbit.receipts` / `cost_events`) are written
//! *inside* the running query's transaction, so they are invisible to every
//! other connection until that query commits. That makes "how many `about()` /
//! `summarize()` calls has this query made so far?" impossible to observe live
//! from SQL — the data exists but is transaction-isolated.
//!
//! This module keeps a tiny per-backend tally in PostgreSQL **shared memory**
//! that the leader bumps as it drains prewarm batches / per-row results, so a
//! *separate poller connection* can read live progress via
//! `rvbbit.live_call_counts()` and join it to `pg_stat_activity` by pid.
//!
//! Threading: every write happens on the **leader** — the prewarm dispatch
//! drain loops (`prewarm::dispatch_*`, which collect pool results on the leader)
//! and the per-row executor path (`operators::invoke_with_cache`). Pool worker
//! threads never call in here, so the `pg_sys` accesses below are always on a
//! valid backend. Reads (`live_call_counts`) take a shared lock; writes take an
//! exclusive lock — both held for only a few nanoseconds.
//!
//! Requires `pg_rvbbit` in `shared_preload_libraries` (it is) so `_PG_init`
//! runs in the postmaster and the shmem segment is actually allocated.

use std::sync::atomic::{AtomicBool, Ordering};

use pgrx::prelude::*;
use pgrx::lwlock::PgLwLock;
use pgrx::shmem::PGRXSharedMemory;

/// Max backends tracked. ProcNumber indexes this array; sized comfortably above
/// a default `max_connections` (100) + autovacuum/bgworker/aux slots. A backend
/// whose ProcNumber lands past the end is simply not tracked (graceful).
const NSLOTS: usize = 512;
/// Distinct operators tracked within a single query. A query referencing more
/// than this many distinct operators drops the overflow ones from the tally.
const NOPS: usize = 12;
/// Operator-name bytes retained (truncated, NUL-padded).
const NAMELEN: usize = 24;

#[derive(Copy, Clone)]
struct OpEntry {
    /// Operator name, NUL-padded. All-zero name = empty entry.
    name: [u8; NAMELEN],
    calls: u64,
}
unsafe impl PGRXSharedMemory for OpEntry {}

#[derive(Copy, Clone)]
struct Slot {
    /// Owning backend pid; 0 = free.
    pid: i32,
    /// Statement start (PG-epoch microseconds). Identifies the query: when it
    /// changes for a given backend, the slot is a stale prior query and is reset.
    stmt_start: i64,
    ops: [OpEntry; NOPS],
}
unsafe impl PGRXSharedMemory for Slot {}

const EMPTY_OP: OpEntry = OpEntry { name: [0u8; NAMELEN], calls: 0 };
const EMPTY_SLOT: Slot = Slot { pid: 0, stmt_start: 0, ops: [EMPTY_OP; NOPS] };

static LIVE: PgLwLock<[Slot; NSLOTS]> = unsafe { PgLwLock::new(c"rvbbit_live_call_counts") };

/// True only once the shmem startup hook has actually run (i.e. the extension
/// was preloaded so the segment exists). When false — e.g. a non-preloaded test
/// instance — `tick`/`live_call_counts` no-op instead of panicking on an
/// uninitialized `PgLwLock`.
static SHMEM_READY: AtomicBool = AtomicBool::new(false);

/// Register the shmem segment + LWLock. Must be called from `_PG_init` (in the
/// postmaster, via `shared_preload_libraries`). The init expression runs inside
/// the shmem *startup* hook, so it fires only when the segment is truly
/// allocated — exactly when it's safe to flip `SHMEM_READY`.
pub fn register_shmem() {
    pgrx::pg_shmem_init!(
        LIVE = {
            SHMEM_READY.store(true, Ordering::SeqCst);
            [EMPTY_SLOT; NSLOTS]
        }
    );
}

/// Fixed-width key for an operator name (truncated to NAMELEN, NUL-padded).
fn name_key(op_name: &str) -> [u8; NAMELEN] {
    let mut key = [0u8; NAMELEN];
    let bytes = op_name.as_bytes();
    let len = bytes.len().min(NAMELEN);
    key[..len].copy_from_slice(&bytes[..len]);
    key
}

/// Record `n` calls of `op_name` against the *current* backend's running query.
/// Leader-only. Self-resets the slot when a new statement starts on this backend.
pub fn tick(op_name: &str, n: u64) {
    if n == 0 || !SHMEM_READY.load(Ordering::Relaxed) {
        return;
    }
    // ProcNumber can be INVALID (-1) outside a normal backend; `as usize` then
    // overflows past NSLOTS and we skip — exactly the graceful no-track we want.
    let idx = unsafe { pgrx::pg_sys::MyProcNumber } as usize;
    if idx >= NSLOTS {
        return;
    }
    let pid = unsafe { pgrx::pg_sys::MyProcPid };
    let stmt_start = unsafe { pgrx::pg_sys::GetCurrentStatementStartTimestamp() } as i64;
    let key = name_key(op_name);

    let mut guard = LIVE.exclusive();
    let slot = &mut guard[idx];
    if slot.pid != pid || slot.stmt_start != stmt_start {
        // First write of a new query on this backend — clear the prior tally.
        *slot = EMPTY_SLOT;
        slot.pid = pid;
        slot.stmt_start = stmt_start;
    }
    let mut free: Option<usize> = None;
    for i in 0..NOPS {
        if slot.ops[i].name == key {
            slot.ops[i].calls += n;
            return;
        }
        if free.is_none() && slot.ops[i].name == [0u8; NAMELEN] {
            free = Some(i);
        }
    }
    if let Some(i) = free {
        slot.ops[i].name = key;
        slot.ops[i].calls = n;
    }
    // else: > NOPS distinct operators this query — overflow dropped.
}

/// Live per-(backend, operator) call counts across all in-flight queries.
/// A poller joins this to `pg_stat_activity` by `pid` to attribute counts to a
/// specific running query. Counts persist after a query ends until that backend
/// starts its next query (so filter by the active pid on the read side).
#[pg_extern]
fn live_call_counts() -> TableIterator<
    'static,
    (
        name!(pid, i32),
        name!(operator, String),
        name!(calls, i64),
    ),
> {
    if !SHMEM_READY.load(Ordering::Relaxed) {
        return TableIterator::new(Vec::new());
    }
    let rows: Vec<(i32, String, i64)> = {
        let guard = LIVE.share();
        let mut rows = Vec::new();
        for slot in guard.iter() {
            if slot.pid == 0 {
                continue;
            }
            for op in slot.ops.iter() {
                if op.name == [0u8; NAMELEN] {
                    continue;
                }
                let end = op.name.iter().position(|&c| c == 0).unwrap_or(NAMELEN);
                let name = String::from_utf8_lossy(&op.name[..end]).into_owned();
                rows.push((slot.pid, name, op.calls as i64));
            }
        }
        rows
    };
    TableIterator::new(rows)
}

#[cfg(any(test, feature = "pg_test"))]
#[pg_schema]
mod tests {
    use pgrx::prelude::*;

    #[pg_test]
    fn live_call_counts_is_callable() {
        // Must succeed whether or not the segment is initialized (a
        // non-preloaded test instance returns an empty set, not an error).
        let n: Option<i64> =
            Spi::get_one("SELECT count(*) FROM rvbbit.live_call_counts()").unwrap();
        assert!(n.is_some());
    }

    #[pg_test]
    fn tick_never_panics() {
        // Safe no-op when SHMEM_READY is false; a real increment otherwise.
        super::tick("about", 5);
        super::tick("", 0); // zero-count + empty name are both fine
    }
}
