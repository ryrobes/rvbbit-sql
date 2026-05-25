//! Per-backend execution recorder for EXPLAIN SEMANTIC ANALYZE (RYR-290).
//!
//! The semantic operator path is a three-tier cache cascade:
//!
//!   L1 in-memory LRU  →  L2 rvbbit.receipts  →  fresh execution
//!
//! Cache hits return from `operators::invoke_with_cache` *before*
//! `log_receipt`, so `rvbbit.receipts` records ONLY misses. To report a
//! true execution graph (which tier each call landed in, and which
//! external endpoints the fresh path hit) we need counters on the live
//! execution path itself.
//!
//! Recording is keyed by **call site**, not by operator name. Two calls
//! to `rvbbit.extract(...)` with different criteria are different call
//! sites — they ask different questions and the user must see which is
//! which. A call site = (operator, criterion), where the criterion is
//! every operator input except the primary `text` subject.
//!
//! Inert unless armed: `arm()` is called by EXPLAIN SEMANTIC ANALYZE for
//! the span of one measured query, `disarm()` hands back the snapshot.

use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;

use serde_json::Value;

use crate::unit_of_work::WorkResult;

/// Stats for one external endpoint (an LLM model, a sidecar, or a code fn)
/// accumulated across all fresh executions of a call site.
#[derive(Default, Clone)]
pub struct EndpointStat {
    pub calls: u64,
    pub tokens_in: u64,
    pub tokens_out: u64,
    pub latency_ms: u64,
    pub errors: u64,
}

/// Per-call-site tally accumulated during one measured run.
#[derive(Default, Clone)]
pub struct OpTally {
    /// Operator name, e.g. "extract".
    pub operator: String,
    /// The call-site-defining arguments (everything but the `text`
    /// subject), e.g. "time of day". Empty when the operator takes no
    /// criterion.
    pub criterion: String,
    /// Served from the in-memory LRU — no SPI, no external call.
    pub l1_hits: u64,
    /// Served from rvbbit.receipts — one SPI lookup, no external call.
    pub l2_hits: u64,
    /// Cache miss: the unit-of-work executor actually ran.
    pub fresh: u64,
    /// Fresh executions whose WorkResult carried an error.
    pub errors: u64,
    /// Per-endpoint stats, keyed by (kind, name).
    pub endpoints: BTreeMap<(String, String), EndpointStat>,
}

impl OpTally {
    /// Total calls at this call site = every cascade tier combined.
    pub fn invocations(&self) -> u64 {
        self.l1_hits + self.l2_hits + self.fresh
    }
}

thread_local! {
    static ARMED: Cell<bool> = const { Cell::new(false) };
    static TALLIES: RefCell<BTreeMap<String, OpTally>> = RefCell::new(BTreeMap::new());
}

/// Begin recording. Clears any prior tally.
pub fn arm() {
    TALLIES.with(|t| t.borrow_mut().clear());
    ARMED.with(|a| a.set(true));
}

/// Stop recording and hand back the collected per-call-site tallies.
pub fn disarm() -> BTreeMap<String, OpTally> {
    ARMED.with(|a| a.set(false));
    TALLIES.with(|t| std::mem::take(&mut *t.borrow_mut()))
}

#[inline]
fn armed() -> bool {
    ARMED.with(|a| a.get())
}

/// The call-site-defining arguments: every operator input except the
/// primary `text` subject. Returns "" when the operator follows no
/// `text`-first convention (then call sites group by operator alone).
fn criterion_of(inputs: &Value) -> String {
    let Some(obj) = inputs.as_object() else {
        return String::new();
    };
    if !obj.contains_key("text") {
        return String::new();
    }
    // serde_json's Map is key-ordered, so this is stable run to run.
    let joined = obj
        .iter()
        .filter(|(k, _)| k.as_str() != "text")
        .map(|(_, v)| match v {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        })
        .collect::<Vec<_>>()
        .join(", ");
    // Cap so a column-reference criterion can't bloat the tally key.
    if joined.chars().count() > 80 {
        format!("{}…", joined.chars().take(79).collect::<String>())
    } else {
        joined
    }
}

fn call_site_key(operator: &str, criterion: &str) -> String {
    format!("{operator}\u{1f}{criterion}")
}

fn touch<'a>(
    map: &'a mut BTreeMap<String, OpTally>,
    operator: &str,
    criterion: &str,
) -> &'a mut OpTally {
    let e = map.entry(call_site_key(operator, criterion)).or_default();
    if e.operator.is_empty() {
        e.operator = operator.to_string();
        e.criterion = criterion.to_string();
    }
    e
}

/// Record an L1 (in-memory LRU) cache hit.
pub fn record_l1_hit(operator: &str, inputs: &Value) {
    if !armed() {
        return;
    }
    let crit = criterion_of(inputs);
    TALLIES.with(|t| touch(&mut t.borrow_mut(), operator, &crit).l1_hits += 1);
}

/// Record an L2 (rvbbit.receipts) cache hit.
pub fn record_l2_hit(operator: &str, inputs: &Value) {
    if !armed() {
        return;
    }
    let crit = criterion_of(inputs);
    TALLIES.with(|t| touch(&mut t.borrow_mut(), operator, &crit).l2_hits += 1);
}

/// Record a fresh execution, folding in its WorkResult: error state and a
/// per-endpoint breakdown of every sub-call.
pub fn record_fresh(operator: &str, inputs: &Value, res: &WorkResult) {
    if !armed() {
        return;
    }
    let crit = criterion_of(inputs);
    TALLIES.with(|t| {
        let mut map = t.borrow_mut();
        let e = touch(&mut map, operator, &crit);
        e.fresh += 1;
        if res.error.is_some() {
            e.errors += 1;
        }
        for sub in &res.sub_calls {
            let name = sub.model.clone().unwrap_or_else(|| "(unknown)".to_string());
            let stat = e.endpoints.entry((sub.kind.clone(), name)).or_default();
            stat.calls += 1;
            stat.tokens_in += sub.tokens_in.max(0) as u64;
            stat.tokens_out += sub.tokens_out.max(0) as u64;
            stat.latency_ms += sub.latency_ms.max(0) as u64;
            if sub.error.is_some() {
                stat.errors += 1;
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::unit_of_work::{SubCall, WorkResult};
    use serde_json::json;

    fn work(subs: Vec<SubCall>, error: Option<String>) -> WorkResult {
        let total_tokens_in = subs.iter().map(|s| s.tokens_in).sum();
        let total_tokens_out = subs.iter().map(|s| s.tokens_out).sum();
        WorkResult {
            output: String::new(),
            sub_calls: subs,
            total_tokens_in,
            total_tokens_out,
            total_latency_ms: 7,
            error,
        }
    }

    fn sub(kind: &str, name: &str, tin: i32, tout: i32, lat: i32) -> SubCall {
        SubCall {
            step: "s".into(),
            kind: kind.into(),
            model: Some(name.into()),
            tokens_in: tin,
            tokens_out: tout,
            latency_ms: lat,
            error: None,
            ..Default::default()
        }
    }

    #[test]
    fn distinct_criteria_are_distinct_call_sites() {
        arm();
        let place = json!({"text": "row text a", "criterion": "place name"});
        let when = json!({"text": "row text b", "criterion": "time of day"});
        record_fresh(
            "extract",
            &place,
            &work(vec![sub("specialist", "extract", 0, 0, 5)], None),
        );
        record_fresh(
            "extract",
            &place,
            &work(vec![sub("specialist", "extract", 0, 0, 6)], None),
        );
        record_fresh(
            "extract",
            &when,
            &work(vec![sub("specialist", "extract", 0, 0, 7)], None),
        );
        let snap = disarm();
        // Two extract calls -> two call sites, NOT one merged "extract".
        assert_eq!(snap.len(), 2, "expected 2 call sites, got {}", snap.len());
        let by_crit: BTreeMap<_, _> = snap
            .values()
            .map(|t| (t.criterion.clone(), t.fresh))
            .collect();
        assert_eq!(by_crit.get("place name"), Some(&2));
        assert_eq!(by_crit.get("time of day"), Some(&1));
    }

    #[test]
    fn criterion_free_operator_groups_by_name() {
        arm();
        // sentiment(observed) — only the `text` subject, no criterion.
        let a = json!({"text": "row a"});
        let b = json!({"text": "row b"});
        record_l1_hit("sentiment", &a);
        record_l2_hit("sentiment", &b);
        let snap = disarm();
        assert_eq!(snap.len(), 1);
        let t = snap.values().next().unwrap();
        assert_eq!(t.operator, "sentiment");
        assert_eq!(t.criterion, "");
        assert_eq!(t.l1_hits, 1);
        assert_eq!(t.l2_hits, 1);
        assert_eq!(t.invocations(), 2);
    }

    #[test]
    fn inert_when_not_armed() {
        let _ = disarm();
        record_l1_hit("means", &json!({"text": "x", "criterion": "y"}));
        arm();
        let snap = disarm();
        assert!(snap.is_empty(), "un-armed records leaked: {}", snap.len());
    }

    #[test]
    fn records_endpoints_and_errors() {
        arm();
        let inp = json!({"text": "t", "criterion": "c"});
        record_fresh(
            "topics",
            &inp,
            &work(
                vec![
                    sub("specialist", "bge-m3", 0, 0, 40),
                    sub("llm", "haiku", 200, 60, 900),
                ],
                None,
            ),
        );
        record_fresh(
            "topics",
            &inp,
            &work(vec![sub("llm", "haiku", 0, 0, 0)], Some("boom".into())),
        );
        let snap = disarm();
        let t = snap.values().next().expect("recorded");
        assert_eq!(t.fresh, 2);
        assert_eq!(t.errors, 1);
        let llm = t
            .endpoints
            .get(&("llm".to_string(), "haiku".to_string()))
            .unwrap();
        assert_eq!(llm.calls, 2);
        assert_eq!(llm.tokens_in, 200);
    }
}
