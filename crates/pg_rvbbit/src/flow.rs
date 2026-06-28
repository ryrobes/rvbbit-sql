//! Flow control primitives: counting semaphore + thread pool.
//!
//! Both are SYNC by design (no tokio) and live per-backend. They exist
//! to bound how much work hits the LLM provider at once, and to give
//! future custom-scan batching a place to dispatch concurrent jobs.
//!
//! Today's UDF call path is per-row sequential from PG's perspective —
//! within a single backend, PG calls UDFs one-by-one. So the pool's
//! main value today is:
//!   1. Multi-step operators that want parallel sub-steps (future)
//!   2. Bounding fan-out when many backends/workers ramp up concurrently
//!   3. Being there when custom-scan-batching lands
//!
//! The semaphore is more immediately useful: every LLM provider call
//! acquires a permit, capping concurrent provider calls per backend.

use std::sync::Arc;

use parking_lot::{Condvar, Mutex};

// ---------------------------------------------------------------------------
// Counting semaphore (sync, blocking)
// ---------------------------------------------------------------------------

/// A counting semaphore. `acquire()` blocks until a permit is available;
/// the returned guard releases on drop.
#[derive(Clone)]
pub struct Semaphore {
    state: Arc<SemState>,
}

struct SemState {
    permits: Mutex<usize>,
    cv: Condvar,
}

pub struct Permit {
    state: Arc<SemState>,
}

impl Semaphore {
    pub fn new(permits: usize) -> Self {
        Self {
            state: Arc::new(SemState {
                permits: Mutex::new(permits),
                cv: Condvar::new(),
            }),
        }
    }

    pub fn acquire(&self) -> Permit {
        let mut p = self.state.permits.lock();
        while *p == 0 {
            self.state.cv.wait(&mut p);
        }
        *p -= 1;
        Permit {
            state: self.state.clone(),
        }
    }
}

impl Drop for Permit {
    fn drop(&mut self) {
        let mut p = self.state.permits.lock();
        *p += 1;
        self.state.cv.notify_one();
    }
}

// ---------------------------------------------------------------------------
// Backend-local thread pool
// ---------------------------------------------------------------------------

use std::sync::OnceLock;
use std::thread::JoinHandle;

use crossbeam_channel::{unbounded, Receiver, Sender};

type Job = Box<dyn FnOnce() + Send + 'static>;

thread_local! {
    /// True on the pool's own worker threads. Lets code that would submit
    /// to the pool detect that it is *already inside* a pool job and run
    /// inline instead — submitting + blocking on the result from within a
    /// worker would deadlock the pool against itself.
    static IN_POOL_WORKER: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// True when called from a pool worker thread.
pub fn in_pool_worker() -> bool {
    IN_POOL_WORKER.with(|c| c.get())
}

pub struct Pool {
    tx: Sender<Option<Job>>,
    workers: Vec<JoinHandle<()>>,
}

impl Pool {
    pub fn new(size: usize) -> Self {
        let (tx, rx) = unbounded::<Option<Job>>();
        let mut workers = Vec::with_capacity(size);
        for _ in 0..size {
            let rx: Receiver<Option<Job>> = rx.clone();
            workers.push(std::thread::spawn(move || {
                IN_POOL_WORKER.with(|c| c.set(true));
                while let Ok(maybe_job) = rx.recv() {
                    match maybe_job {
                        // concurrency-02/resources-03: a panicking job must not
                        // kill the worker thread (which would permanently shrink
                        // the pool toward zero). Catch it here; the job's result
                        // sender drops, so the leader sees a clean RecvError and
                        // raises a statement error instead of the thread dying.
                        // These are plain std threads (not the PG backend), so a
                        // panic here never crosses the C FFI boundary.
                        Some(job) => {
                            let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(job));
                        }
                        None => break,
                    }
                }
            }));
        }
        Self { tx, workers }
    }

    /// Submit a job. Returns a Receiver that yields the result on completion.
    pub fn submit<T, F>(&self, f: F) -> Receiver<T>
    where
        T: Send + 'static,
        F: FnOnce() -> T + Send + 'static,
    {
        let (tx, rx) = crossbeam_channel::bounded::<T>(1);
        let job: Job = Box::new(move || {
            let v = f();
            let _ = tx.send(v);
        });
        // Pool channel is unbounded; submit never blocks.
        let _ = self.tx.send(Some(job));
        rx
    }

    /// Map a slice of inputs through `f` in parallel across the pool,
    /// preserving order. Blocks until all complete. This is the workhorse
    /// for "do N independent things concurrently" — what multi-step
    /// operators with parallel sub-steps will use.
    #[allow(dead_code)]
    pub fn map_parallel<I, O, F>(&self, items: Vec<I>, f: F) -> Vec<O>
    where
        I: Send + 'static,
        O: Send + 'static,
        F: Fn(I) -> O + Send + Sync + 'static + Clone,
    {
        let receivers: Vec<_> = items
            .into_iter()
            .map(|item| {
                let f = f.clone();
                self.submit(move || f(item))
            })
            .collect();
        receivers.into_iter().map(|rx| rx.recv().unwrap()).collect()
    }
}

impl Drop for Pool {
    fn drop(&mut self) {
        for _ in 0..self.workers.len() {
            let _ = self.tx.send(None);
        }
        for w in self.workers.drain(..) {
            let _ = w.join();
        }
    }
}

// ---------------------------------------------------------------------------
// Backend-global pool, lazy init from GUC.
// ---------------------------------------------------------------------------

static POOL: OnceLock<Pool> = OnceLock::new();

/// Get-or-init the backend-local thread pool. Size from
/// `RVBBIT_POOL_SIZE` env var (default 8). Lazy so backends that
/// never call any operator pay zero cost.
pub fn pool() -> &'static Pool {
    POOL.get_or_init(|| {
        let size = std::env::var("RVBBIT_POOL_SIZE")
            .ok()
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(8)
            .max(1)
            .min(128);
        Pool::new(size)
    })
}
