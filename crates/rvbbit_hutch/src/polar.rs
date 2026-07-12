//! Polar integration — validate-on-first-sight.
//!
//! The money/product split (HUTCH_PLAN rule 1): Polar owns the payment and
//! key lifecycle; the hutch only ever asks "is this key granted, and which
//! benefit minted it?" An unknown key triggers ONE validate call; the
//! answer is cached in memory with a revalidation TTL, so the hot path
//! stays a local hash lookup. If Polar is unreachable at refresh time we
//! serve the stale entry (stale-is-better-than-down); revoked keys die at
//! TTL (webhook-driven instant revocation is the planned fast-follow).
//!
//! benefit_id → {entitlements, lanes} mapping lives in hutch config: the
//! benefit IS the SKU from the gateway's point of view.

use crate::config::PolarCfg;
use crate::tenants::{hash_key, Tenant, TenantStatus};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

struct CacheEntry {
    tenant: Tenant,
    fetched_at: Instant,
}

pub struct PolarSync {
    cfg: PolarCfg,
    cache: Mutex<HashMap<String, CacheEntry>>,
}

pub enum PolarLookup {
    /// Valid subscription → serve.
    Tenant(Tenant),
    /// Polar answered: key exists but is not granted (revoked, disabled…).
    NotGranted(Tenant),
    /// Polar answered: no such key.
    Unknown,
    /// Polar unreachable and nothing cached — indistinguishable from
    /// invalid without risking free service; caller rejects with a hint.
    Unavailable(String),
}

impl PolarSync {
    pub fn new(cfg: PolarCfg) -> Self {
        Self {
            cfg,
            cache: Mutex::new(HashMap::new()),
        }
    }

    fn token(&self) -> Option<String> {
        std::env::var(&self.cfg.token_env).ok().filter(|v| !v.is_empty())
    }

    pub async fn lookup(&self, http: &reqwest::Client, raw_key: &str) -> PolarLookup {
        // Cheap negative gate: Polar keys carry the configured prefix, so
        // random garbage never generates upstream traffic.
        if let Some(prefix) = &self.cfg.key_prefix {
            if !raw_key
                .to_ascii_lowercase()
                .starts_with(&prefix.to_ascii_lowercase())
            {
                return PolarLookup::Unknown;
            }
        }
        let h = hash_key(raw_key);
        let stale: Option<Tenant> = {
            let cache = self.cache.lock().expect("polar cache poisoned");
            match cache.get(&h) {
                Some(e) if e.fetched_at.elapsed() < Duration::from_secs(self.cfg.revalidate_secs) => {
                    let t = e.tenant.clone();
                    return if t.status == TenantStatus::Active {
                        PolarLookup::Tenant(t)
                    } else {
                        PolarLookup::NotGranted(t)
                    };
                }
                Some(e) => Some(e.tenant.clone()),
                None => None,
            }
        };

        match self.validate(http, raw_key).await {
            Ok(Some(tenant)) => {
                let granted = tenant.status == TenantStatus::Active;
                self.cache.lock().expect("polar cache poisoned").insert(
                    h,
                    CacheEntry {
                        tenant: tenant.clone(),
                        fetched_at: Instant::now(),
                    },
                );
                if granted {
                    PolarLookup::Tenant(tenant)
                } else {
                    PolarLookup::NotGranted(tenant)
                }
            }
            Ok(None) => {
                // Polar 404s keys whose grant was revoked. If we knew this
                // key when it was alive, keep an Expired tombstone so the
                // customer gets the renewal message (and we don't re-ask
                // Polar every request) instead of a bare invalid_key.
                match stale {
                    Some(mut t) => {
                        t.status = TenantStatus::Expired;
                        self.cache.lock().expect("polar cache poisoned").insert(
                            h,
                            CacheEntry {
                                tenant: t.clone(),
                                fetched_at: Instant::now(),
                            },
                        );
                        PolarLookup::NotGranted(t)
                    }
                    None => {
                        self.cache.lock().expect("polar cache poisoned").remove(&h);
                        PolarLookup::Unknown
                    }
                }
            }
            Err(e) => match stale {
                Some(t) => {
                    tracing::warn!("polar refresh failed, serving stale tenant '{}': {e}", t.id);
                    if t.status == TenantStatus::Active {
                        PolarLookup::Tenant(t)
                    } else {
                        PolarLookup::NotGranted(t)
                    }
                }
                None => PolarLookup::Unavailable(e),
            },
        }
    }

    /// One validate round-trip. Ok(None) = Polar says the key doesn't exist.
    async fn validate(
        &self,
        http: &reqwest::Client,
        raw_key: &str,
    ) -> Result<Option<Tenant>, String> {
        let url = format!(
            "{}/v1/license-keys/validate",
            self.cfg.api_base.trim_end_matches('/')
        );
        let mut req = http
            .post(&url)
            .timeout(Duration::from_secs(10))
            .json(&json!({
                "key": raw_key,
                "organization_id": self.cfg.organization_id,
            }));
        if let Some(t) = self.token() {
            req = req.bearer_auth(t);
        }
        let resp = req.send().await.map_err(|e| e.to_string())?;
        let status = resp.status();
        if status.as_u16() == 404 || status.as_u16() == 422 {
            return Ok(None);
        }
        if !status.is_success() {
            return Err(format!("polar validate HTTP {}", status.as_u16()));
        }
        let body: Value = resp.json().await.map_err(|e| e.to_string())?;
        Ok(Some(self.tenant_from_validate(&body)))
    }

    fn tenant_from_validate(&self, v: &Value) -> Tenant {
        let benefit_id = v.get("benefit_id").and_then(|b| b.as_str()).unwrap_or("");
        let granted = v.get("status").and_then(|s| s.as_str()) == Some("granted");
        let id = v
            .pointer("/customer/email")
            .and_then(|e| e.as_str())
            .map(|s| s.to_string())
            .or_else(|| {
                v.get("customer_id")
                    .and_then(|c| c.as_str())
                    .map(|s| format!("polar:{}", &s[..s.len().min(8)]))
            })
            .unwrap_or_else(|| "polar:unknown".into());
        let mapping = self
            .cfg
            .benefit_map
            .iter()
            .find(|m| m.benefit_id == benefit_id);
        Tenant {
            id,
            key_sha256: String::new(), // unused for polar-synced tenants
            lanes: mapping.map(|m| m.lanes).unwrap_or(1),
            status: if granted {
                TenantStatus::Active
            } else {
                TenantStatus::Expired
            },
            // Unmapped benefit → no entitlements → clean not_entitled errors
            // instead of accidental free service.
            entitlements: mapping.map(|m| m.entitlements.clone()).unwrap_or_default(),
        }
    }
}
