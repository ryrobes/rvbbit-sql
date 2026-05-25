//! Per-table and per-row-group metadata.
//!
//! The authoritative copy lives in the `rvbbit.tables` / `rvbbit.row_groups`
//! Postgres tables. These structs are the in-memory shape we hand around in
//! Rust code.

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableMeta {
    pub table_oid: u32,
    pub catcher_oid: u32,
    pub data_dir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RowGroupMeta {
    pub rg_id: i64,
    pub path: String,
    pub n_rows: i64,
    pub n_bytes: i64,
    pub min_xid: Option<u64>,
    pub max_xid: Option<u64>,
    pub column_stats: Vec<ColumnStats>,
    /// Per-group aggregate blocks for low-cardinality columns. Each
    /// block partitions this row group's rows by one group column's
    /// distinct values and records count + sum + null-count for every
    /// numeric "other" column. Powers GROUP BY pushdown without scanning.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub per_group_stats: Vec<PerGroupBlock>,
}

/// Per-group aggregate stats for one group column. `groups` holds one
/// bucket per distinct value seen in this row group. Bucket count is
/// capped at compact time (low-cardinality columns only).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PerGroupBlock {
    pub group_column: String,
    pub groups: Vec<GroupBucket>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroupBucket {
    /// The distinct value as JSON. Null represents the NULL group.
    pub value: serde_json::Value,
    pub count: i64,
    /// Per-other-column numeric aggregates (sum + non-null count). Keyed
    /// by column name. Only populated for numeric (int*/float*) columns.
    #[serde(default, skip_serializing_if = "std::collections::HashMap::is_empty")]
    pub agg: std::collections::HashMap<String, NumericAgg>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NumericAgg {
    pub sum: f64,
    pub count_nonnull: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ColumnStats {
    pub name: String,
    pub null_count: i64,
    pub distinct_estimate: Option<i64>,
    /// Min/max/sum as JSON values for type-agnostic transport. Sum is
    /// only populated for numeric columns (int*, float*) — used by the
    /// aggregate-pushdown path for SUM/AVG queries without row scans.
    pub min: Option<serde_json::Value>,
    pub max: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sum: Option<serde_json::Value>,
    /// HyperLogLog state for cross-group distinct estimation
    /// (RYR-291). Base64-encoded sketch bytes. Populated only
    /// for text columns at compact time; cross-group union via
    /// `rvbbit.approx_distinct(rel, col)`. None for older row groups
    /// written before this field existed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hll_b64: Option<String>,
    /// Row-group text membership sketch. Populated for UTF-8 columns and used
    /// by scan-time pruning for equality / IN and positive LIKE patterns with
    /// concrete literal trigrams. False positives are allowed; false negatives
    /// are not.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text_sketch_b64: Option<String>,
}

const TEXT_SKETCH_BITS: usize = 1 << 16;
const TEXT_SKETCH_BYTES: usize = TEXT_SKETCH_BITS / 8;
const TEXT_SKETCH_VERSION: u8 = 0x01;
const TEXT_SKETCH_VERSION_EXACT_TRIGRAMS: u8 = 0x02;
const TEXT_SKETCH_HASHES: usize = 4;
const TEXT_SKETCH_MAX_TRIGRAMS: usize = 32_768;
const TOKEN_VALUE: u8 = 0x01;
const TOKEN_TRIGRAM: u8 = 0x02;
const TOKEN_TRIGRAM_LOWER: u8 = 0x03;

#[derive(Debug, Clone)]
pub struct TextSketch {
    bits: Vec<u8>,
    trigrams: Option<HashSet<u64>>,
    lower_trigrams: Option<HashSet<u64>>,
}

impl Default for TextSketch {
    fn default() -> Self {
        Self::new()
    }
}

impl TextSketch {
    pub fn new() -> Self {
        Self {
            bits: vec![0u8; TEXT_SKETCH_BYTES],
            trigrams: Some(HashSet::new()),
            lower_trigrams: Some(HashSet::new()),
        }
    }

    pub fn insert_value(&mut self, value: &str) {
        self.insert_token(TOKEN_VALUE, value.as_bytes());
        self.insert_trigrams(TOKEN_TRIGRAM, value);
        let lower = value.to_lowercase();
        self.insert_trigrams(TOKEN_TRIGRAM_LOWER, &lower);
    }

    pub fn may_contain_value(&self, value: &str) -> bool {
        self.contains_token(TOKEN_VALUE, value.as_bytes())
    }

    pub fn may_contain_trigram(&self, trigram: &str, case_insensitive: bool) -> bool {
        if case_insensitive {
            let lower = trigram.to_lowercase();
            self.lower_trigrams
                .as_ref()
                .is_none_or(|set| set.contains(&token_hash(TOKEN_TRIGRAM_LOWER, lower.as_bytes())))
        } else {
            self.trigrams
                .as_ref()
                .is_none_or(|set| set.contains(&token_hash(TOKEN_TRIGRAM, trigram.as_bytes())))
        }
    }

    pub fn to_b64(&self) -> String {
        let trigram_len = self.trigrams.as_ref().map(HashSet::len).unwrap_or(0);
        let lower_len = self.lower_trigrams.as_ref().map(HashSet::len).unwrap_or(0);
        let mut bytes =
            Vec::with_capacity(1 + self.bits.len() + 8 + ((trigram_len + lower_len) * 8));
        bytes.push(TEXT_SKETCH_VERSION_EXACT_TRIGRAMS);
        bytes.extend_from_slice(&self.bits);
        append_hash_set(&mut bytes, self.trigrams.as_ref());
        append_hash_set(&mut bytes, self.lower_trigrams.as_ref());
        B64.encode(bytes)
    }

    pub fn from_b64(value: &str) -> Option<Self> {
        let bytes = B64.decode(value.as_bytes()).ok()?;
        match bytes.first().copied()? {
            TEXT_SKETCH_VERSION => {
                if bytes.len() != 1 + TEXT_SKETCH_BYTES {
                    return None;
                }
                Some(Self {
                    bits: bytes[1..].to_vec(),
                    trigrams: None,
                    lower_trigrams: None,
                })
            }
            TEXT_SKETCH_VERSION_EXACT_TRIGRAMS => {
                if bytes.len() < 1 + TEXT_SKETCH_BYTES + 8 {
                    return None;
                }
                let mut offset = 1 + TEXT_SKETCH_BYTES;
                let trigrams = read_hash_set(&bytes, &mut offset)?;
                let lower_trigrams = read_hash_set(&bytes, &mut offset)?;
                if offset != bytes.len() {
                    return None;
                }
                Some(Self {
                    bits: bytes[1..1 + TEXT_SKETCH_BYTES].to_vec(),
                    trigrams,
                    lower_trigrams,
                })
            }
            _ => None,
        }
    }

    fn insert_trigrams(&mut self, tag: u8, value: &str) {
        let bytes = value.as_bytes();
        if bytes.len() < 3 {
            return;
        }
        for trigram in bytes.windows(3) {
            self.insert_token(tag, trigram);
            let target = if tag == TOKEN_TRIGRAM_LOWER {
                &mut self.lower_trigrams
            } else {
                &mut self.trigrams
            };
            if let Some(set) = target.as_mut() {
                set.insert(token_hash(tag, trigram));
                if set.len() > TEXT_SKETCH_MAX_TRIGRAMS {
                    *target = None;
                }
            }
        }
    }

    fn insert_token(&mut self, tag: u8, bytes: &[u8]) {
        for pos in token_positions(tag, bytes) {
            self.bits[pos / 8] |= 1u8 << (pos % 8);
        }
    }

    fn contains_token(&self, tag: u8, bytes: &[u8]) -> bool {
        token_positions(tag, bytes)
            .iter()
            .all(|pos| (self.bits[pos / 8] & (1u8 << (pos % 8))) != 0)
    }
}

fn append_hash_set(out: &mut Vec<u8>, hashes: Option<&HashSet<u64>>) {
    let Some(hashes) = hashes else {
        out.extend_from_slice(&u32::MAX.to_le_bytes());
        return;
    };
    out.extend_from_slice(&(hashes.len() as u32).to_le_bytes());
    let mut sorted: Vec<u64> = hashes.iter().copied().collect();
    sorted.sort_unstable();
    for hash in sorted {
        out.extend_from_slice(&hash.to_le_bytes());
    }
}

fn read_hash_set(bytes: &[u8], offset: &mut usize) -> Option<Option<HashSet<u64>>> {
    if *offset + 4 > bytes.len() {
        return None;
    }
    let len = u32::from_le_bytes([
        bytes[*offset],
        bytes[*offset + 1],
        bytes[*offset + 2],
        bytes[*offset + 3],
    ]);
    *offset += 4;
    if len == u32::MAX {
        return Some(None);
    }
    let len = len as usize;
    if len > TEXT_SKETCH_MAX_TRIGRAMS || *offset + len * 8 > bytes.len() {
        return None;
    }
    let mut set = HashSet::with_capacity(len);
    for _ in 0..len {
        let hash = u64::from_le_bytes([
            bytes[*offset],
            bytes[*offset + 1],
            bytes[*offset + 2],
            bytes[*offset + 3],
            bytes[*offset + 4],
            bytes[*offset + 5],
            bytes[*offset + 6],
            bytes[*offset + 7],
        ]);
        *offset += 8;
        set.insert(hash);
    }
    Some(Some(set))
}

fn token_positions(tag: u8, bytes: &[u8]) -> [usize; TEXT_SKETCH_HASHES] {
    let hash = token_hash_bytes(tag, bytes);
    let b = hash.as_bytes();
    let mut out = [0usize; TEXT_SKETCH_HASHES];
    for i in 0..TEXT_SKETCH_HASHES {
        let off = i * 8;
        let raw = u64::from_le_bytes([
            b[off],
            b[off + 1],
            b[off + 2],
            b[off + 3],
            b[off + 4],
            b[off + 5],
            b[off + 6],
            b[off + 7],
        ]);
        out[i] = (raw as usize) & (TEXT_SKETCH_BITS - 1);
    }
    out
}

fn token_hash(tag: u8, bytes: &[u8]) -> u64 {
    let hash = token_hash_bytes(tag, bytes);
    let b = hash.as_bytes();
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

fn token_hash_bytes(tag: u8, bytes: &[u8]) -> blake3::Hash {
    let mut hasher = blake3::Hasher::new();
    hasher.update(&[tag]);
    hasher.update(bytes);
    hasher.finalize()
}
