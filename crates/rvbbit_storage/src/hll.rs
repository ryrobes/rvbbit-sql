//! Minimal HyperLogLog++ for per-row-group distinct-count sketches
//! (RYR-291). Hand-rolled because the existing crate's serde roundtrip
//! was producing identical bytes for different inputs (silent bug).
//!
//! Algorithm: exact hash-set mode for small sketches, then standard
//! Flajolet HLL with bias-corrected linear counting after the exact
//! threshold. Hash function is blake3 (we already depend on it), reading
//! the first 8 bytes as u64.
//!
//! Storage: small sketches serialize exact hashes; larger sketches serialize
//! raw registers. At precision 12 the register payload is 4098 bytes,
//! ~5500 bytes base64. Comfortably fits inside stats jsonb per row group.
//!
//! Accuracy: ±2.6% RMSE at precision 12 (m=4096 registers).

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use std::collections::HashSet;

const PRECISION: u8 = 12;
const M: usize = 1 << PRECISION as usize; // 4096
const EXACT_THRESHOLD: usize = 1024;
const VERSION_RAW_REGISTERS: u8 = 0x01;
const VERSION_EXACT_OR_HLL: u8 = 0x02;
const MODE_EXACT: u8 = 0x00;
const MODE_HLL: u8 = 0x01;

#[derive(Clone)]
pub struct Hll {
    exact_hashes: Option<HashSet<u64>>,
    registers: Vec<u8>,
}

impl Default for Hll {
    fn default() -> Self {
        Self::new()
    }
}

impl Hll {
    pub fn new() -> Self {
        Self {
            exact_hashes: Some(HashSet::new()),
            registers: vec![0u8; M],
        }
    }

    pub fn insert(&mut self, value: &str) {
        let h = blake3_u64(value.as_bytes());
        self.insert_hash(h);
    }

    fn insert_hash(&mut self, h: u64) {
        if let Some(exact_hashes) = self.exact_hashes.as_mut() {
            exact_hashes.insert(h);
            if exact_hashes.len() > EXACT_THRESHOLD {
                self.exact_hashes = None;
            }
        }
        // Top PRECISION bits select the register; the remaining
        // 64-PRECISION bits get rank+1 of leading zeros.
        let idx = (h >> (64 - PRECISION as u64)) as usize;
        let w = (h << PRECISION as u64) | (1u64 << (PRECISION as u64 - 1));
        let leading = w.leading_zeros() as u8 + 1;
        if self.registers[idx] < leading {
            self.registers[idx] = leading;
        }
    }

    pub fn merge(&mut self, other: &Hll) {
        match (self.exact_hashes.as_mut(), other.exact_hashes.as_ref()) {
            (Some(left), Some(right)) if left.len() + right.len() <= EXACT_THRESHOLD => {
                left.extend(right.iter().copied());
            }
            (Some(left), Some(right)) => {
                left.extend(right.iter().copied());
                if left.len() > EXACT_THRESHOLD {
                    self.exact_hashes = None;
                }
            }
            (Some(_), None) => {
                self.exact_hashes = None;
            }
            (None, _) => {}
        }
        for i in 0..M {
            if other.registers[i] > self.registers[i] {
                self.registers[i] = other.registers[i];
            }
        }
    }

    /// Cardinality estimate using HLL with linear-counting fallback at small N.
    pub fn count(&self) -> u64 {
        if let Some(exact_hashes) = self.exact_hashes.as_ref() {
            return exact_hashes.len() as u64;
        }
        let m_f = M as f64;
        // Raw HLL estimate.
        let sum: f64 = self.registers.iter().map(|&r| 2f64.powi(-(r as i32))).sum();
        let alpha = match M {
            16 => 0.673,
            32 => 0.697,
            64 => 0.709,
            _ => 0.7213 / (1.0 + 1.079 / m_f),
        };
        let est = alpha * m_f * m_f / sum;

        // Small-range correction: linear counting when many zero registers.
        if est <= 2.5 * m_f {
            let zeros = self.registers.iter().filter(|&&r| r == 0).count();
            if zeros > 0 {
                return (m_f * (m_f / zeros as f64).ln()).round() as u64;
            }
        }
        // Large-range correction (32-bit hash collisions) not needed —
        // we use 64-bit hashes so the upper bound is well past usable.
        est.round() as u64
    }

    /// Bytes:
    /// - v2 exact: version + precision + mode + u32 len + sorted u64 hashes.
    /// - v2 HLL: version + precision + mode + 4096 register bytes.
    /// - v1 raw registers are still accepted by `from_bytes`.
    pub fn to_bytes(&self) -> Vec<u8> {
        if let Some(exact_hashes) = self.exact_hashes.as_ref() {
            let mut hashes: Vec<u64> = exact_hashes.iter().copied().collect();
            hashes.sort_unstable();
            let mut out = Vec::with_capacity(3 + 4 + hashes.len() * 8);
            out.push(VERSION_EXACT_OR_HLL);
            out.push(PRECISION);
            out.push(MODE_EXACT);
            out.extend_from_slice(&(hashes.len() as u32).to_le_bytes());
            for hash in hashes {
                out.extend_from_slice(&hash.to_le_bytes());
            }
            return out;
        }

        let mut out = Vec::with_capacity(3 + M);
        out.push(VERSION_EXACT_OR_HLL);
        out.push(PRECISION);
        out.push(MODE_HLL);
        out.extend_from_slice(&self.registers);
        out
    }

    pub fn from_bytes(bytes: &[u8]) -> Option<Self> {
        if bytes.len() < 2 || bytes[1] != PRECISION {
            return None;
        }
        match bytes[0] {
            VERSION_RAW_REGISTERS => {
                if bytes.len() != 2 + M {
                    return None;
                }
                Some(Self {
                    exact_hashes: None,
                    registers: bytes[2..].to_vec(),
                })
            }
            VERSION_EXACT_OR_HLL => {
                if bytes.len() < 3 {
                    return None;
                }
                match bytes[2] {
                    MODE_EXACT => {
                        if bytes.len() < 7 {
                            return None;
                        }
                        let len = u32::from_le_bytes(bytes[3..7].try_into().ok()?) as usize;
                        if bytes.len() != 7 + len * 8 {
                            return None;
                        }
                        let mut hll = Self::new();
                        for chunk in bytes[7..].chunks_exact(8) {
                            hll.insert_hash(u64::from_le_bytes(chunk.try_into().ok()?));
                        }
                        Some(hll)
                    }
                    MODE_HLL => {
                        if bytes.len() != 3 + M {
                            return None;
                        }
                        Some(Self {
                            exact_hashes: None,
                            registers: bytes[3..].to_vec(),
                        })
                    }
                    _ => None,
                }
            }
            _ => None,
        }
    }

    pub fn to_b64(&self) -> String {
        B64.encode(self.to_bytes())
    }

    pub fn from_b64(s: &str) -> Option<Self> {
        let bytes = B64.decode(s.as_bytes()).ok()?;
        Self::from_bytes(&bytes)
    }
}

/// First 8 bytes of blake3 as a u64. Deterministic, fast.
fn blake3_u64(bytes: &[u8]) -> u64 {
    let h = blake3::hash(bytes);
    let b = h.as_bytes();
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_small_count() {
        let mut h = Hll::new();
        for i in 0..100 {
            h.insert(&format!("item_{i}"));
        }
        // Linear counting in small-N regime — should be exact or within 1.
        let c = h.count();
        assert!((c as i64 - 100).abs() <= 2, "got {c}");
    }

    #[test]
    fn merge_unions_distinct_sets() {
        let mut a = Hll::new();
        let mut b = Hll::new();
        for i in 0..100 {
            a.insert(&format!("a_{i}"));
            b.insert(&format!("b_{i}"));
        }
        let before = a.count();
        a.merge(&b);
        let after = a.count();
        assert!(before <= 105 && before >= 95, "before={before}");
        assert!(after <= 210 && after >= 190, "after={after}");
    }

    #[test]
    fn merge_overlap_doesnt_double_count() {
        let mut a = Hll::new();
        let mut b = Hll::new();
        for i in 0..100 {
            a.insert(&format!("shared_{i}"));
            b.insert(&format!("shared_{i}"));
        }
        a.merge(&b);
        assert!(
            (a.count() as i64 - 100).abs() <= 5,
            "identical sets should merge back to ~100; got {}",
            a.count()
        );
    }

    #[test]
    fn merge_small_overlap_is_exact() {
        let mut a = Hll::new();
        let mut b = Hll::new();
        for i in 0..100 {
            a.insert(&format!("item_{i}"));
        }
        for i in 50..150 {
            b.insert(&format!("item_{i}"));
        }
        a.merge(&b);
        assert_eq!(a.count(), 150);
    }

    #[test]
    fn roundtrip_via_b64() {
        let mut h = Hll::new();
        for i in 0..1000 {
            h.insert(&format!("x_{i}"));
        }
        let original = h.count();
        let s = h.to_b64();
        let decoded = Hll::from_b64(&s).expect("decode");
        assert_eq!(decoded.count(), original);
    }

    #[test]
    fn larger_distinct_within_3_percent() {
        let mut h = Hll::new();
        for i in 0..10_000 {
            h.insert(&format!("x_{i}"));
        }
        let c = h.count();
        let err = (c as f64 - 10000.0).abs() / 10000.0;
        assert!(err < 0.03, "count={c}, err={err}");
    }
}
