//! Per-tenant lane enforcement. A lane = one in-flight request. Semaphores
//! are created lazily per tenant and recreated if the configured lane count
//! changes on a tenants reload (same pattern as pg_rvbbit's backend
//! semaphores).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::time::{timeout, Duration};

#[derive(Default)]
pub struct LaneRegistry {
    inner: Mutex<HashMap<String, (usize, Arc<Semaphore>)>>,
}

impl LaneRegistry {
    fn semaphore(&self, tenant_id: &str, lanes: usize) -> Arc<Semaphore> {
        let lanes = lanes.max(1);
        let mut map = self.inner.lock().expect("lane registry poisoned");
        match map.get(tenant_id) {
            Some((max, sem)) if *max == lanes => sem.clone(),
            _ => {
                let sem = Arc::new(Semaphore::new(lanes));
                map.insert(tenant_id.to_string(), (lanes, sem.clone()));
                sem
            }
        }
    }

    /// Try to take a lane, waiting at most `grace_ms` (burst smoothing, not
    /// a queue). None = saturated → caller answers 429.
    pub async fn acquire(
        &self,
        tenant_id: &str,
        lanes: usize,
        grace_ms: u64,
    ) -> Option<OwnedSemaphorePermit> {
        let sem = self.semaphore(tenant_id, lanes);
        match timeout(Duration::from_millis(grace_ms), sem.acquire_owned()).await {
            Ok(Ok(permit)) => Some(permit),
            _ => None,
        }
    }

    /// (in_flight, max) per tenant — for /metrics gauges.
    pub fn snapshot(&self) -> Vec<(String, usize, usize)> {
        let map = self.inner.lock().expect("lane registry poisoned");
        map.iter()
            .map(|(id, (max, sem))| (id.clone(), max.saturating_sub(sem.available_permits()), *max))
            .collect()
    }
}
