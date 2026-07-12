//! Tenant store — the entitlement side of the house.
//!
//! Phase 1: a YAML file of {id, key_sha256, lanes, status, entitlements}.
//! This module is the Polar seam: when billing arrives, a webhook handler
//! rewrites tenants.yaml (or a successor store) and POSTs /admin/reload-
//! tenants — nothing else in the service changes.
//!
//! Keys are looked up BY HASH: the raw key is sha256'd and used as the map
//! key, so no comparison ever touches secret bytes and timing is uniform.

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TenantStatus {
    Active,
    Expired,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Tenant {
    pub id: String,
    pub key_sha256: String,
    pub lanes: usize,
    #[serde(default = "default_status")]
    pub status: TenantStatus,
    #[serde(default)]
    pub entitlements: Vec<String>,
}

fn default_status() -> TenantStatus {
    TenantStatus::Active
}

#[derive(Debug, Deserialize)]
struct TenantsFile {
    tenants: Vec<Tenant>,
}

#[derive(Debug, Default)]
pub struct TenantStore {
    by_hash: HashMap<String, Tenant>,
}

pub fn hash_key(raw: &str) -> String {
    hex::encode(Sha256::digest(raw.as_bytes()))
}

impl TenantStore {
    pub fn load(path: &str) -> Result<Self, String> {
        let raw = std::fs::read_to_string(path)
            .map_err(|e| format!("cannot read tenants file '{path}': {e}"))?;
        let parsed: TenantsFile =
            serde_yaml::from_str(&raw).map_err(|e| format!("bad tenants file '{path}': {e}"))?;
        let mut by_hash = HashMap::new();
        for t in parsed.tenants {
            let h = t.key_sha256.trim().to_lowercase();
            if h.len() != 64 || hex::decode(&h).is_err() {
                return Err(format!("tenant '{}': key_sha256 is not 64-char hex", t.id));
            }
            if by_hash.insert(h, t.clone()).is_some() {
                return Err(format!("duplicate key hash (tenant '{}')", t.id));
            }
        }
        Ok(Self { by_hash })
    }

    pub fn lookup(&self, raw_key: &str) -> Option<&Tenant> {
        self.by_hash.get(&hash_key(raw_key))
    }

    pub fn len(&self) -> usize {
        self.by_hash.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lookup_by_raw_key() {
        let key = "hutch_test_key_1";
        let yaml = format!(
            "tenants:\n  - id: acme\n    key_sha256: \"{}\"\n    lanes: 4\n    entitlements: [clover]\n",
            hash_key(key)
        );
        let dir = std::env::temp_dir().join("hutch_tenant_test");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("tenants.yaml");
        std::fs::write(&p, yaml).unwrap();
        let store = TenantStore::load(p.to_str().unwrap()).unwrap();
        let t = store.lookup(key).expect("tenant found");
        assert_eq!(t.id, "acme");
        assert_eq!(t.lanes, 4);
        assert_eq!(t.status, TenantStatus::Active);
        assert!(store.lookup("wrong_key").is_none());
    }
}
