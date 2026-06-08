//! Phase 2c: read-path demonstration functions.
//!
//! These prove parquet reads work end-to-end and let us benchmark the
//! column-pruning advantage against heap without yet having to wire a
//! custom-scan node into the planner.
//!
//!     SELECT rvbbit.rg_n_rows('llm_events'::regclass, 0);
//!     SELECT rvbbit.rg_count('llm_events'::regclass, 0);
//!     SELECT rvbbit.rg_count_projected('llm_events'::regclass, 0, 'latency_ms');
//!     SELECT rvbbit.rg_sum_int('llm_events'::regclass, 0, 'tokens_in');
//!
//! Phase 2c+1 will add SETOF-returning aggregates (group-by demos) and
//! the JSON-without-detoast demonstration. Phase 2c-end wires a real
//! custom-scan path so plain `SELECT ... FROM rel` reads from parquet
//! when present.

use std::cell::RefCell;
use std::cmp::Ordering;
use std::cmp::Reverse;
use std::collections::{BTreeMap, BinaryHeap};
use std::io::Cursor;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};
use std::sync::Arc;
use std::time::UNIX_EPOCH;

use arrow::array::{
    Array, BinaryArray, BooleanArray, Date32Array, Float32Array, Float64Array, Int16Array,
    Int32Array, Int64Array, StringArray, TimestampMicrosecondArray,
};
use arrow::datatypes::DataType;
use pgrx::prelude::*;
use pgrx::Spi;
use roaring::RoaringBitmap;
use rvbbit_storage::metadata::{ColumnStats, PerGroupBlock, TextSketch};
use rvbbit_storage::row_group::{RowGroupReader, TextDictionary};
use serde_json::{Map as JsonMap, Number as JsonNumber, Value as JsonValue};

use crate::columnar_cache;
use crate::fast_hash::{FastHashMap as HashMap, FastHashSet as HashSet};
use crate::vector::{self, key_value_to_text, AggSpec, AggValue, FilterSpec, KeySpec, KeyValue};

const CLUSTER_LAYOUT_PREFIX: &str = "cluster:";
const BITMAP_TOP_COUNT_MAX_KEY_PRODUCT: usize = 250_000;
const NATIVE_ROW_GROUP_THREADS_MAX: usize = 8;

thread_local! {
    static GROUP_COUNT_CACHE: RefCell<HashMap<GroupCountCacheKey, HashMap<Option<String>, i64>>> =
        RefCell::new(HashMap::default());
}

static COLUMN_BITMAPS_TABLE_EXISTS: AtomicBool = AtomicBool::new(false);
static TEXT_DICTIONARIES_TABLE_EXISTS: AtomicBool = AtomicBool::new(false);

#[derive(Clone, Hash, PartialEq, Eq)]
struct GroupCountCacheKey {
    rel_oid: u32,
    group_col: String,
    row_group_count: i64,
    max_rg_id: i64,
    total_rows: i64,
}

struct RowGroupPathStats {
    path: PathBuf,
    stats_text: Option<String>,
}

struct IndexedRowGroupPath {
    rg_id: i64,
    path: PathBuf,
    n_rows: i64,
}

#[derive(Clone)]
struct IndexedTextDictionaryPath {
    rg_id: i64,
    row_group_path: PathBuf,
    dictionary_path: PathBuf,
    n_rows: i64,
    n_values: i64,
    n_nulls: i64,
    n_empty: i64,
    n_bytes: i64,
}

#[derive(Debug)]
enum DictionaryCountError {
    Invalid(&'static str),
    Read(String),
    WorkerPanic(&'static str),
}

impl std::fmt::Display for DictionaryCountError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(message) | Self::WorkerPanic(message) => f.write_str(message),
            Self::Read(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for DictionaryCountError {}

fn dictionary_count_result<T>(
    result: Result<T, DictionaryCountError>,
) -> Result<Option<T>, Box<dyn std::error::Error>> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(DictionaryCountError::Invalid(_)) => Ok(None),
        Err(error) => Err(Box::new(error)),
    }
}

// --- Helpers ----------------------------------------------------------------

fn native_row_group_threads(work_items: usize) -> usize {
    if work_items <= 1 {
        return 1;
    }
    let threads = std::env::var("RVBBIT_NATIVE_THREADS")
        .ok()
        .and_then(|raw| raw.trim().parse::<usize>().ok())
        .unwrap_or_else(|| {
            std::thread::available_parallelism()
                .map(|n| n.get().min(NATIVE_ROW_GROUP_THREADS_MAX))
                .unwrap_or(4)
        });
    threads.clamp(1, work_items)
}

fn lookup_path(rel: pg_sys::Oid, rg_id: i64) -> Result<PathBuf, String> {
    let rel_oid = rel.to_u32();
    let path: Option<String> = Spi::get_one(&format!(
        "SELECT path FROM rvbbit.row_groups \
         WHERE table_oid = {rel_oid}::oid AND rg_id = {rg_id}"
    ))
    .map_err(|e| format!("looking up row group: {e}"))?;
    path.map(PathBuf::from)
        .ok_or_else(|| format!("no row group rg_id={rg_id} for oid {rel_oid}"))
}

fn lookup_paths_for_oid(rel_oid: u32) -> Result<Vec<PathBuf>, String> {
    let mut paths = Vec::new();
    Spi::connect(|client| -> Result<(), String> {
        let table = client
            .select(
                &format!(
                    "SELECT path FROM rvbbit.row_groups \
                     WHERE table_oid = {rel_oid}::oid \
                     ORDER BY rg_id"
                ),
                None,
                &[],
            )
            .map_err(|e| format!("looking up row groups: {e}"))?;
        for row in table {
            let path: Option<String> = row.get(1).map_err(|e| format!("reading path: {e}"))?;
            if let Some(path) = path {
                paths.push(PathBuf::from(path));
            }
        }
        Ok(())
    })
    .map_err(|e| format!("SPI row-group lookup: {e}"))?;
    Ok(paths)
}

fn lookup_paths_for_oid_with_ids(rel_oid: u32) -> Result<Vec<IndexedRowGroupPath>, String> {
    let mut paths = Vec::new();
    Spi::connect(|client| -> Result<(), String> {
        let table = client
            .select(
                &format!(
                    "SELECT rg_id, path, n_rows FROM rvbbit.row_groups \
                     WHERE table_oid = {rel_oid}::oid \
                     ORDER BY rg_id"
                ),
                None,
                &[],
            )
            .map_err(|e| format!("looking up indexed row groups: {e}"))?;
        for row in table {
            let rg_id: Option<i64> = row.get(1).map_err(|e| format!("reading rg_id: {e}"))?;
            let path: Option<String> = row.get(2).map_err(|e| format!("reading path: {e}"))?;
            let n_rows: Option<i64> = row.get(3).map_err(|e| format!("reading n_rows: {e}"))?;
            if let (Some(rg_id), Some(path), Some(n_rows)) = (rg_id, path, n_rows) {
                paths.push(IndexedRowGroupPath {
                    rg_id,
                    path: PathBuf::from(path),
                    n_rows,
                });
            }
        }
        Ok(())
    })
    .map_err(|e| format!("SPI indexed row-group lookup: {e}"))?;
    Ok(paths)
}

fn lookup_text_dictionary_row_groups(
    rel_oid: u32,
    col: &str,
) -> Result<Option<Vec<IndexedTextDictionaryPath>>, Box<dyn std::error::Error>> {
    if !text_dictionaries_table_available()? {
        return Ok(None);
    }
    let col_esc = col.replace('\'', "''");
    let sql = format!(
        "SELECT rg.rg_id, rg.path, rg.n_rows, \
                td.path, td.n_values, td.n_nulls, td.n_empty, td.n_bytes \
         FROM rvbbit.row_groups rg \
         LEFT JOIN rvbbit.text_dictionaries td \
           ON td.table_oid = rg.table_oid \
          AND td.rg_id = rg.rg_id \
          AND td.column_name = '{col_esc}' \
         WHERE rg.table_oid = {rel_oid}::oid \
         ORDER BY rg.rg_id"
    );

    let mut out = Vec::new();
    let mut missing = false;
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let rg_id: Option<i64> = row.get(1)?;
            let row_group_path: Option<String> = row.get(2)?;
            let n_rows: Option<i64> = row.get(3)?;
            let dictionary_path: Option<String> = row.get(4)?;
            let n_values: Option<i64> = row.get(5)?;
            let n_nulls: Option<i64> = row.get(6)?;
            let n_empty: Option<i64> = row.get(7)?;
            let n_bytes: Option<i64> = row.get(8)?;
            let (Some(rg_id), Some(row_group_path), Some(n_rows)) = (rg_id, row_group_path, n_rows)
            else {
                missing = true;
                continue;
            };
            let (
                Some(dictionary_path),
                Some(n_values),
                Some(n_nulls),
                Some(n_empty),
                Some(n_bytes),
            ) = (dictionary_path, n_values, n_nulls, n_empty, n_bytes)
            else {
                missing = true;
                continue;
            };
            out.push(IndexedTextDictionaryPath {
                rg_id,
                row_group_path: PathBuf::from(row_group_path),
                dictionary_path: PathBuf::from(dictionary_path),
                n_rows,
                n_values,
                n_nulls,
                n_empty,
                n_bytes,
            });
        }
        Ok(())
    })?;

    if missing {
        return Ok(None);
    }
    Ok(Some(out))
}

fn text_dictionaries_table_available() -> Result<bool, Box<dyn std::error::Error>> {
    if TEXT_DICTIONARIES_TABLE_EXISTS.load(AtomicOrdering::Relaxed) {
        return Ok(true);
    }
    let exists: Option<bool> =
        Spi::get_one("SELECT to_regclass('rvbbit.text_dictionaries') IS NOT NULL")?;
    if exists.unwrap_or(false) {
        TEXT_DICTIONARIES_TABLE_EXISTS.store(true, AtomicOrdering::Relaxed);
        Ok(true)
    } else {
        Ok(false)
    }
}

fn load_text_dictionary_cached(
    rel_oid: u32,
    col: &str,
    row_group: &IndexedTextDictionaryPath,
) -> Result<Arc<TextDictionary>, Box<dyn std::error::Error>> {
    let key = text_dictionary_cache_key(rel_oid, col, &row_group.dictionary_path)?;
    if let Some(dictionary) = columnar_cache::get::<TextDictionary>("text_dictionaries", &key) {
        return Ok(dictionary);
    }

    let dictionary = TextDictionary::read(&row_group.dictionary_path)?;
    let expected_rows = usize::try_from(row_group.n_rows).unwrap_or(usize::MAX);
    let expected_values = usize::try_from(row_group.n_values).unwrap_or(usize::MAX);
    if dictionary.codes.len() != expected_rows || dictionary.values.len() != expected_values {
        return Err(format!(
            "rvbbit: text dictionary metadata mismatch for {}",
            row_group.dictionary_path.display()
        )
        .into());
    }
    if dictionary.n_nulls != row_group.n_nulls || dictionary.n_empty != row_group.n_empty {
        return Err(format!(
            "rvbbit: text dictionary count mismatch for {}",
            row_group.dictionary_path.display()
        )
        .into());
    }
    let bytes = dictionary
        .memory_size()
        .max(row_group.n_bytes.max(0) as usize);
    Ok(columnar_cache::put(
        "text_dictionaries",
        key,
        bytes,
        dictionary,
    ))
}

fn text_dictionary_cache_key(
    rel_oid: u32,
    col: &str,
    path: &std::path::Path,
) -> Result<String, Box<dyn std::error::Error>> {
    let metadata = std::fs::metadata(path)?;
    let modified_nanos = metadata
        .modified()
        .ok()
        .and_then(|modified| modified.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    Ok(format!(
        "rel={rel_oid}|col={}|path={}|len={}|mtime={}",
        col.replace('|', "||"),
        path.to_string_lossy(),
        metadata.len(),
        modified_nanos,
    ))
}

fn int_column_range_width(rel_oid: u32, col: &str) -> Option<i64> {
    let col_esc = col.replace('\'', "''");
    let sql = format!(
        "SELECT min((s->>'min')::bigint), max((s->>'max')::bigint) \
         FROM rvbbit.row_groups, jsonb_array_elements(stats) AS s \
         WHERE table_oid = {rel_oid}::oid AND s->>'name' = '{col_esc}' \
           AND s->>'min' IS NOT NULL AND s->>'max' IS NOT NULL"
    );
    Spi::connect(|client| -> Result<Option<i64>, pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let min: Option<i64> = row.get(1)?;
            let max: Option<i64> = row.get(2)?;
            let (Some(min), Some(max)) = (min, max) else {
                return Ok(None);
            };
            return Ok(max.checked_sub(min));
        }
        Ok(None)
    })
    .ok()
    .flatten()
}

fn should_use_owned_text_pair_counts(rel_oid: u32, int_col: &str, skip_empty_text: bool) -> bool {
    if !skip_empty_text {
        return false;
    }
    int_column_range_width(rel_oid, int_col)
        .map(|width| width <= 65_536)
        .unwrap_or(false)
}

fn lookup_paths_for_oid_pruned(
    rel_oid: u32,
    filters: &[FilterSpec],
) -> Result<Vec<PathBuf>, String> {
    if !filters.iter().any(filter_has_row_group_pruning) {
        return lookup_paths_for_oid(rel_oid);
    }

    let mut best_variant: Option<(Vec<PathBuf>, usize, bool)> = None;
    for layout in lookup_variant_layouts(rel_oid)? {
        let clustered = lookup_paths_with_stats(rel_oid, Some(&layout))?;
        if clustered.is_empty() {
            continue;
        }
        let total = clustered.len();
        let paths = prune_paths_with_stats(clustered, filters);
        let pruned = total.saturating_sub(paths.len());
        if should_use_cluster_layout(total, pruned) {
            let matches_filter = layout_matches_vector_filter(&layout, filters);
            let replace =
                best_variant
                    .as_ref()
                    .is_none_or(|(_, best_kept, best_matches_filter)| {
                        paths.len() < *best_kept
                            || (paths.len() == *best_kept
                                && matches_filter
                                && !*best_matches_filter)
                    });
            if replace {
                let kept = paths.len();
                best_variant = Some((paths, kept, matches_filter));
            }
        }
    }
    if let Some((paths, _, _)) = best_variant {
        return Ok(paths);
    }

    let primary = lookup_paths_with_stats(rel_oid, None)?;
    Ok(prune_paths_with_stats(primary, filters))
}

fn layout_matches_vector_filter(layout: &str, filters: &[FilterSpec]) -> bool {
    let Some(column) = layout.strip_prefix(CLUSTER_LAYOUT_PREFIX) else {
        return false;
    };
    filters.iter().any(|filter| match filter {
        FilterSpec::IntEq { col, .. }
        | FilterSpec::IntNe { col, .. }
        | FilterSpec::IntGe { col, .. }
        | FilterSpec::IntLe { col, .. }
        | FilterSpec::IntIn { col, .. }
        | FilterSpec::TextContains { col, .. }
        | FilterSpec::TextNotEmpty { col }
        | FilterSpec::TextNotContains { col, .. } => col == column,
    })
}

fn lookup_variant_layouts(rel_oid: u32) -> Result<Vec<String>, String> {
    let prefix = CLUSTER_LAYOUT_PREFIX.replace('\'', "''");
    let mut layouts = Vec::new();
    Spi::connect(|client| -> Result<(), String> {
        let table = client
            .select(
                &format!(
                    "SELECT DISTINCT layout FROM rvbbit.row_group_variants \
                     WHERE table_oid = {rel_oid}::oid AND layout LIKE '{prefix}%' \
                     ORDER BY layout"
                ),
                None,
                &[],
            )
            .map_err(|e| format!("looking up row-group variant layouts: {e}"))?;
        for row in table {
            if let Some(layout) = row
                .get::<String>(1)
                .map_err(|e| format!("reading row-group variant layout: {e}"))?
            {
                layouts.push(layout);
            }
        }
        Ok(())
    })
    .map_err(|e| format!("SPI row-group variant layout lookup: {e}"))?;
    Ok(layouts)
}

fn lookup_paths_with_stats(
    rel_oid: u32,
    variant_layout: Option<&str>,
) -> Result<Vec<RowGroupPathStats>, String> {
    let mut row_groups = Vec::new();
    Spi::connect(|client| -> Result<(), String> {
        let sql = if let Some(layout) = variant_layout {
            let layout = layout.replace('\'', "''");
            format!(
                "SELECT path, stats::text FROM rvbbit.row_group_variants \
                 WHERE table_oid = {rel_oid}::oid AND layout = '{layout}' \
                 ORDER BY rg_id"
            )
        } else {
            format!(
                "SELECT path, stats::text FROM rvbbit.row_groups \
                 WHERE table_oid = {rel_oid}::oid \
                 ORDER BY rg_id"
            )
        };
        let table = client
            .select(&sql, None, &[])
            .map_err(|e| format!("looking up row groups: {e}"))?;
        for row in table {
            let path: Option<String> = row.get(1).map_err(|e| format!("reading path: {e}"))?;
            let Some(path) = path else {
                continue;
            };
            let stats_text: Option<String> = row
                .get(2)
                .map_err(|e| format!("reading row-group stats: {e}"))?;
            row_groups.push(RowGroupPathStats {
                path: PathBuf::from(path),
                stats_text,
            });
        }
        Ok(())
    })
    .map_err(|e| format!("SPI row-group lookup: {e}"))?;
    Ok(row_groups)
}

fn prune_paths_with_stats(
    row_groups: Vec<RowGroupPathStats>,
    filters: &[FilterSpec],
) -> Vec<PathBuf> {
    row_groups
        .into_iter()
        .filter(|rg| row_group_may_satisfy_vector_filters(rg.stats_text.as_deref(), filters))
        .map(|rg| rg.path)
        .collect()
}

fn should_use_cluster_layout(total_groups: usize, pruned_groups: usize) -> bool {
    if total_groups == 0 || pruned_groups == 0 {
        return false;
    }
    let threshold = std::env::var("RVBBIT_CLUSTER_MIN_PRUNE_PCT")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(20)
        .min(100);
    pruned_groups * 100 >= total_groups * threshold
}

fn filter_has_row_group_pruning(filter: &FilterSpec) -> bool {
    matches!(
        filter,
        FilterSpec::IntEq { .. }
            | FilterSpec::IntNe { .. }
            | FilterSpec::IntGe { .. }
            | FilterSpec::IntLe { .. }
            | FilterSpec::IntIn { .. }
            | FilterSpec::TextContains { .. }
    )
}

fn row_group_may_satisfy_vector_filters(stats_text: Option<&str>, filters: &[FilterSpec]) -> bool {
    let stats = parse_column_stats(stats_text);
    if stats.is_empty() {
        return true;
    }
    !filters
        .iter()
        .any(|filter| vector_filter_impossible_for_row_group(&stats, filter))
}

fn parse_column_stats(stats_text: Option<&str>) -> HashMap<String, ColumnStats> {
    let Some(stats_text) = stats_text else {
        return HashMap::default();
    };
    let Ok(stats) = serde_json::from_str::<Vec<ColumnStats>>(stats_text) else {
        return HashMap::default();
    };
    stats
        .into_iter()
        .map(|stat| (stat.name.clone(), stat))
        .collect()
}

fn vector_filter_impossible_for_row_group(
    stats: &HashMap<String, ColumnStats>,
    filter: &FilterSpec,
) -> bool {
    match filter {
        FilterSpec::IntEq { col, value } => {
            let Some((min, max)) = stat_i64_bounds(stats, col) else {
                return false;
            };
            *value < min || *value > max
        }
        FilterSpec::IntNe { col, value } => {
            let Some((min, max)) = stat_i64_bounds(stats, col) else {
                return false;
            };
            min == max && min == *value
        }
        FilterSpec::IntGe { col, value } => {
            let Some((_, max)) = stat_i64_bounds(stats, col) else {
                return false;
            };
            max < *value
        }
        FilterSpec::IntLe { col, value } => {
            let Some((min, _)) = stat_i64_bounds(stats, col) else {
                return false;
            };
            min > *value
        }
        FilterSpec::IntIn { col, values } => {
            if values.is_empty() {
                return true;
            }
            let Some((min, max)) = stat_i64_bounds(stats, col) else {
                return false;
            };
            !values.iter().any(|value| *value >= min && *value <= max)
        }
        FilterSpec::TextContains { col, needle } => {
            if needle.len() < 3 {
                return false;
            }
            let Some(sketch) = stat_text_sketch(stats, col) else {
                return false;
            };
            required_text_trigrams(needle)
                .iter()
                .any(|trigram| !sketch.may_contain_trigram(trigram, false))
        }
        FilterSpec::TextNotEmpty { .. } | FilterSpec::TextNotContains { .. } => false,
    }
}

fn stat_i64_bounds(stats: &HashMap<String, ColumnStats>, col: &str) -> Option<(i64, i64)> {
    let stat = stats.get(col)?;
    Some((json_i64(stat.min.as_ref()?)?, json_i64(stat.max.as_ref()?)?))
}

fn stat_text_min(stats: &HashMap<String, ColumnStats>, col: &str) -> Option<String> {
    stats.get(col)?.min.as_ref()?.as_str().map(str::to_string)
}

fn json_i64(value: &serde_json::Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_u64().and_then(|value| i64::try_from(value).ok()))
}

fn stat_text_sketch(stats: &HashMap<String, ColumnStats>, col: &str) -> Option<TextSketch> {
    let stat = stats.get(col)?;
    TextSketch::from_b64(stat.text_sketch_b64.as_ref()?)
}

type RowGroupBitmapCatalog = HashMap<(i64, String, String), HashMap<String, RoaringBitmap>>;

fn try_filter_bitmaps(
    rel_oid: u32,
    filters: &[FilterSpec],
) -> Result<Option<Vec<(PathBuf, RoaringBitmap)>>, Box<dyn std::error::Error>> {
    let columns = bitmap_filter_columns(filters);
    if columns.is_empty() {
        return Ok(None);
    }
    if !column_bitmaps_table_available()? {
        return Ok(None);
    }

    let row_groups = lookup_paths_for_oid_with_ids(rel_oid)?;
    if row_groups.is_empty() {
        return Ok(Some(Vec::new()));
    }

    let catalog = load_column_bitmaps_cached(rel_oid, &columns, &row_groups)?;
    if catalog.is_empty() {
        return Ok(None);
    }

    let mut used_any = false;
    let mut out = Vec::with_capacity(row_groups.len());
    for row_group in row_groups {
        let mut bitmap: Option<RoaringBitmap> = None;
        let mut used_for_group = false;
        for filter in filters {
            let Some(next) = bitmap_for_filter(&catalog, row_group.rg_id, filter) else {
                continue;
            };
            used_for_group = true;
            intersect_bitmap(&mut bitmap, next);
            if bitmap.as_ref().is_some_and(RoaringBitmap::is_empty) {
                break;
            }
        }

        if used_for_group {
            used_any = true;
            if let Some(bitmap) = bitmap {
                if !bitmap.is_empty() {
                    out.push((row_group.path, bitmap));
                }
            }
        } else {
            out.push((row_group.path, full_row_bitmap(row_group.n_rows)));
        }
    }

    if used_any {
        Ok(Some(out))
    } else {
        Ok(None)
    }
}

#[derive(Clone)]
enum BitmapTopKey {
    Int(String),
    Date(String),
}

fn try_bitmap_top_count(
    rel_oid: u32,
    keys: &[KeySpec],
    filters: &[FilterSpec],
    k: usize,
) -> Result<Option<Vec<vector::GroupRow>>, Box<dyn std::error::Error>> {
    if k == 0 || keys.is_empty() || keys.len() > 2 {
        return Ok(None);
    }
    if filters.iter().any(|filter| {
        matches!(
            filter,
            FilterSpec::TextContains { .. } | FilterSpec::TextNotContains { .. }
        )
    }) {
        return Ok(None);
    }
    let Some(key_specs) = bitmap_top_keys(keys) else {
        return Ok(None);
    };
    if !column_bitmaps_table_available()? {
        return Ok(None);
    }

    let row_groups = lookup_paths_for_oid_with_ids(rel_oid)?;
    if row_groups.is_empty() {
        return Ok(Some(Vec::new()));
    }
    let columns = bitmap_top_count_columns(&key_specs, filters);
    let catalog = load_column_bitmaps_cached(rel_oid, &columns, &row_groups)?;
    if catalog.is_empty() {
        return Ok(None);
    }

    let mut counts = HashMap::<Vec<KeyValue>, i64>::default();
    for row_group in &row_groups {
        let Some(filter_bitmap) =
            bitmap_for_all_filters(&catalog, row_group.rg_id, row_group.n_rows, filters)
        else {
            return Ok(None);
        };
        if filter_bitmap.is_empty() {
            continue;
        }
        match key_specs.as_slice() {
            [key] => {
                let Some(entries) =
                    bitmap_entries(&catalog, row_group.rg_id, key.column(), "value")
                else {
                    return Ok(None);
                };
                if !bitmap_entries_cover_filter(entries, &filter_bitmap) {
                    return Ok(None);
                }
                for (value_text, bitmap) in entries {
                    let count = bitmap_intersection_len(&filter_bitmap, bitmap);
                    if count == 0 {
                        continue;
                    }
                    let Some(value) = key.value(value_text) else {
                        return Ok(None);
                    };
                    *counts.entry(vec![value]).or_insert(0) += count;
                }
            }
            [first, second] => {
                let Some(first_entries) =
                    bitmap_entries(&catalog, row_group.rg_id, first.column(), "value")
                else {
                    return Ok(None);
                };
                let Some(second_entries) =
                    bitmap_entries(&catalog, row_group.rg_id, second.column(), "value")
                else {
                    return Ok(None);
                };
                if first_entries
                    .len()
                    .checked_mul(second_entries.len())
                    .filter(|product| *product <= BITMAP_TOP_COUNT_MAX_KEY_PRODUCT)
                    .is_none()
                {
                    return Ok(None);
                }
                if !bitmap_entries_cover_filter(first_entries, &filter_bitmap)
                    || !bitmap_entries_cover_filter(second_entries, &filter_bitmap)
                {
                    return Ok(None);
                }
                let mut first_filtered = Vec::new();
                for (value_text, bitmap) in first_entries {
                    let mut row_bitmap = filter_bitmap.clone();
                    row_bitmap &= bitmap;
                    if row_bitmap.is_empty() {
                        continue;
                    }
                    let Some(value) = first.value(value_text) else {
                        return Ok(None);
                    };
                    first_filtered.push((value, row_bitmap));
                }
                for (first_value, first_bitmap) in first_filtered {
                    for (second_text, second_bitmap) in second_entries {
                        let count = bitmap_intersection_len(&first_bitmap, second_bitmap);
                        if count == 0 {
                            continue;
                        }
                        let Some(second_value) = second.value(second_text) else {
                            return Ok(None);
                        };
                        *counts
                            .entry(vec![first_value.clone(), second_value])
                            .or_insert(0) += count;
                    }
                }
            }
            _ => return Ok(None),
        }
    }

    let mut rows = counts
        .into_iter()
        .map(|(keys, count)| vector::GroupRow {
            keys,
            count,
            aggs: Vec::new(),
        })
        .collect::<Vec<_>>();
    rows.sort_unstable_by(|a, b| b.count.cmp(&a.count).then_with(|| a.keys.cmp(&b.keys)));
    rows.truncate(k);
    Ok(Some(rows))
}

impl BitmapTopKey {
    fn column(&self) -> &str {
        match self {
            Self::Int(col) | Self::Date(col) => col,
        }
    }

    fn value(&self, value_text: &str) -> Option<KeyValue> {
        match self {
            Self::Int(_) => value_text
                .parse::<i64>()
                .ok()
                .map(|v| KeyValue::Int(Some(v))),
            Self::Date(_) => value_text
                .parse::<i32>()
                .ok()
                .map(|v| KeyValue::Date(Some(v))),
        }
    }
}

fn bitmap_top_keys(keys: &[KeySpec]) -> Option<Vec<BitmapTopKey>> {
    keys.iter()
        .map(|key| match key {
            KeySpec::Int { col } => Some(BitmapTopKey::Int(col.clone())),
            KeySpec::Date { col } => Some(BitmapTopKey::Date(col.clone())),
            KeySpec::Text { .. }
            | KeySpec::TimestampMinute { .. }
            | KeySpec::TimestampTruncMinute { .. } => None,
        })
        .collect()
}

fn bitmap_top_count_columns(keys: &[BitmapTopKey], filters: &[FilterSpec]) -> Vec<String> {
    let mut columns = bitmap_filter_columns(filters);
    for key in keys {
        let col = key.column();
        if !columns.iter().any(|existing| existing == col) {
            columns.push(col.to_string());
        }
    }
    columns
}

fn bitmap_for_all_filters(
    catalog: &RowGroupBitmapCatalog,
    rg_id: i64,
    n_rows: i64,
    filters: &[FilterSpec],
) -> Option<RoaringBitmap> {
    let mut bitmap = full_row_bitmap(n_rows);
    for filter in filters {
        let next = bitmap_for_filter(catalog, rg_id, filter)?;
        bitmap &= &next;
        if bitmap.is_empty() {
            break;
        }
    }
    Some(bitmap)
}

fn bitmap_entries_cover_filter(
    entries: &HashMap<String, RoaringBitmap>,
    filter_bitmap: &RoaringBitmap,
) -> bool {
    let mut covered = RoaringBitmap::new();
    for bitmap in entries.values() {
        covered |= bitmap;
    }
    let mut missing = filter_bitmap.clone();
    missing -= &covered;
    missing.is_empty()
}

fn bitmap_intersection_len(left: &RoaringBitmap, right: &RoaringBitmap) -> i64 {
    let mut bitmap = left.clone();
    bitmap &= right;
    bitmap.len().min(i64::MAX as u64) as i64
}

fn column_bitmaps_table_available() -> Result<bool, Box<dyn std::error::Error>> {
    if COLUMN_BITMAPS_TABLE_EXISTS.load(AtomicOrdering::Relaxed) {
        return Ok(true);
    }
    let exists: Option<bool> =
        Spi::get_one("SELECT to_regclass('rvbbit.column_bitmaps') IS NOT NULL")?;
    if exists.unwrap_or(false) {
        COLUMN_BITMAPS_TABLE_EXISTS.store(true, AtomicOrdering::Relaxed);
        Ok(true)
    } else {
        Ok(false)
    }
}

fn bitmap_filter_columns(filters: &[FilterSpec]) -> Vec<String> {
    let mut out = Vec::new();
    for filter in filters {
        let col = match filter {
            FilterSpec::IntEq { col, .. }
            | FilterSpec::IntNe { col, .. }
            | FilterSpec::IntGe { col, .. }
            | FilterSpec::IntLe { col, .. }
            | FilterSpec::IntIn { col, .. }
            | FilterSpec::TextNotEmpty { col } => col,
            FilterSpec::TextContains { .. } | FilterSpec::TextNotContains { .. } => continue,
        };
        if !out.iter().any(|existing| existing == col) {
            out.push(col.clone());
        }
    }
    out
}

fn load_column_bitmaps_cached(
    rel_oid: u32,
    columns: &[String],
    row_groups: &[IndexedRowGroupPath],
) -> Result<std::sync::Arc<RowGroupBitmapCatalog>, Box<dyn std::error::Error>> {
    let key = bitmap_catalog_cache_key(rel_oid, columns, row_groups);
    if let Some(catalog) = columnar_cache::get::<RowGroupBitmapCatalog>("bitmap_catalogs", &key) {
        return Ok(catalog);
    }

    let catalog = load_column_bitmaps(rel_oid, columns)?;
    let bytes = bitmap_catalog_memory_size(&catalog);
    Ok(columnar_cache::put("bitmap_catalogs", key, bytes, catalog))
}

fn bitmap_catalog_cache_key(
    rel_oid: u32,
    columns: &[String],
    row_groups: &[IndexedRowGroupPath],
) -> String {
    let mut cols = columns.to_vec();
    cols.sort_unstable();
    let mut hasher = blake3::Hasher::new();
    hasher.update(&(row_groups.len() as u64).to_le_bytes());
    for row_group in row_groups {
        hasher.update(&row_group.rg_id.to_le_bytes());
        hasher.update(&row_group.n_rows.to_le_bytes());
        hasher.update(row_group.path.to_string_lossy().as_bytes());
        hasher.update(&[0]);
    }
    format!(
        "rel={rel_oid}|cols={}|sig={}",
        cols.join("\u{1f}"),
        hasher.finalize().to_hex()
    )
}

fn bitmap_catalog_memory_size(catalog: &RowGroupBitmapCatalog) -> usize {
    let mut bytes = 0usize;
    for ((_, column_name, bitmap_kind), entries) in catalog {
        bytes = bytes
            .saturating_add(column_name.len())
            .saturating_add(bitmap_kind.len())
            .saturating_add(64);
        for (value_text, bitmap) in entries {
            bytes = bytes
                .saturating_add(value_text.len())
                .saturating_add(bitmap.serialized_size())
                .saturating_add(64);
        }
    }
    bytes
}

fn load_column_bitmaps(
    rel_oid: u32,
    columns: &[String],
) -> Result<RowGroupBitmapCatalog, Box<dyn std::error::Error>> {
    let column_list = columns
        .iter()
        .map(|col| format!("'{}'", col.replace('\'', "''")))
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!(
        "SELECT rg_id, column_name, bitmap_kind, value_text, bitmap \
         FROM rvbbit.column_bitmaps \
         WHERE table_oid = {rel_oid}::oid \
           AND column_name IN ({column_list})"
    );

    let mut raw = Vec::<(i64, String, String, String, Vec<u8>)>::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let rg_id: Option<i64> = row.get(1)?;
            let column_name: Option<String> = row.get(2)?;
            let bitmap_kind: Option<String> = row.get(3)?;
            let value_text: Option<String> = row.get(4)?;
            let bitmap: Option<Vec<u8>> = row.get(5)?;
            if let (
                Some(rg_id),
                Some(column_name),
                Some(bitmap_kind),
                Some(value_text),
                Some(bitmap),
            ) = (rg_id, column_name, bitmap_kind, value_text, bitmap)
            {
                raw.push((rg_id, column_name, bitmap_kind, value_text, bitmap));
            }
        }
        Ok(())
    })?;

    let mut catalog = RowGroupBitmapCatalog::default();
    for (rg_id, column_name, bitmap_kind, value_text, bytes) in raw {
        let Ok(bitmap) = RoaringBitmap::deserialize_from(&mut Cursor::new(&bytes)) else {
            return Ok(RowGroupBitmapCatalog::default());
        };
        catalog
            .entry((rg_id, column_name, bitmap_kind))
            .or_default()
            .insert(value_text, bitmap);
    }
    Ok(catalog)
}

fn bitmap_for_filter(
    catalog: &RowGroupBitmapCatalog,
    rg_id: i64,
    filter: &FilterSpec,
) -> Option<RoaringBitmap> {
    match filter {
        FilterSpec::IntEq { col, value } => {
            let entries = bitmap_entries(catalog, rg_id, col, "value")?;
            Some(entries.get(&value.to_string()).cloned().unwrap_or_default())
        }
        FilterSpec::IntNe { col, value } => {
            let entries = bitmap_entries(catalog, rg_id, col, "value")?;
            Some(bitmap_union_by_int(entries, |candidate| {
                candidate != *value
            }))
        }
        FilterSpec::IntGe { col, value } => {
            let entries = bitmap_entries(catalog, rg_id, col, "value")?;
            Some(bitmap_union_by_int(entries, |candidate| {
                candidate >= *value
            }))
        }
        FilterSpec::IntLe { col, value } => {
            let entries = bitmap_entries(catalog, rg_id, col, "value")?;
            Some(bitmap_union_by_int(entries, |candidate| {
                candidate <= *value
            }))
        }
        FilterSpec::IntIn { col, values } => {
            let entries = bitmap_entries(catalog, rg_id, col, "value")?;
            let mut out = RoaringBitmap::new();
            for value in values {
                if let Some(bitmap) = entries.get(&value.to_string()) {
                    out |= bitmap;
                }
            }
            Some(out)
        }
        FilterSpec::TextNotEmpty { col } => {
            let entries = bitmap_entries(catalog, rg_id, col, "not_empty")?;
            Some(entries.get("__not_empty__").cloned().unwrap_or_default())
        }
        FilterSpec::TextContains { .. } | FilterSpec::TextNotContains { .. } => None,
    }
}

fn bitmap_entries<'a>(
    catalog: &'a RowGroupBitmapCatalog,
    rg_id: i64,
    col: &str,
    kind: &str,
) -> Option<&'a HashMap<String, RoaringBitmap>> {
    catalog.get(&(rg_id, col.to_string(), kind.to_string()))
}

fn bitmap_union_by_int<F>(entries: &HashMap<String, RoaringBitmap>, predicate: F) -> RoaringBitmap
where
    F: Fn(i64) -> bool,
{
    let mut out = RoaringBitmap::new();
    for (value_text, bitmap) in entries {
        if value_text
            .parse::<i64>()
            .ok()
            .filter(|value| predicate(*value))
            .is_some()
        {
            out |= bitmap;
        }
    }
    out
}

fn intersect_bitmap(current: &mut Option<RoaringBitmap>, next: RoaringBitmap) {
    match current {
        Some(existing) => *existing &= &next,
        None => *current = Some(next),
    }
}

fn full_row_bitmap(n_rows: i64) -> RoaringBitmap {
    let end = n_rows.max(0).min(u32::MAX as i64) as u32;
    (0..end).collect()
}

fn required_text_trigrams(value: &str) -> Vec<String> {
    let bytes = value.as_bytes();
    if bytes.len() < 3 {
        return Vec::new();
    }
    bytes
        .windows(3)
        .filter_map(|window| std::str::from_utf8(window).ok())
        .map(str::to_string)
        .collect()
}

#[derive(Clone, Debug)]
pub(crate) struct NumericScan {
    pub sum_f64: f64,
    pub sum_i128: Option<i128>,
    pub count_nonnull: i64,
}

/// Exact projected numeric scan over all row groups for one column.
///
/// This is deliberately used by rewrite rules instead of row-group `sum`
/// metadata because older smallint/int stats can be overflowed. It still
/// gets the columnar win: only the requested Parquet column is read.
pub(crate) fn scan_numeric_sum_count(
    rel_oid: u32,
    col: &str,
) -> Result<NumericScan, Box<dyn std::error::Error>> {
    let paths = lookup_paths_for_oid(rel_oid)?;
    let mut sum_f64 = 0.0f64;
    let mut sum_i128 = Some(0i128);
    let mut count_nonnull = 0i64;

    // Hot loops below take the no-null fast path when null_count() == 0 by
    // iterating the raw values() slice — autovectorizable. When nulls are
    // present we fall back to iter().flatten() over the bitmap.
    macro_rules! fold_int_array {
        ($a:expr) => {{
            let a = $a;
            if a.null_count() == 0 {
                let (mut acc_i128, mut acc_f64) = (0i128, 0.0f64);
                for &v in a.values().iter() {
                    let v64 = v as i64;
                    acc_i128 += v64 as i128;
                    acc_f64 += v64 as f64;
                }
                sum_f64 += acc_f64;
                if let Some(s) = &mut sum_i128 {
                    *s += acc_i128;
                }
                count_nonnull += a.len() as i64;
            } else {
                for v in a.iter().flatten() {
                    let v64 = v as i64;
                    sum_f64 += v64 as f64;
                    if let Some(s) = &mut sum_i128 {
                        *s += v64 as i128;
                    }
                    count_nonnull += 1;
                }
            }
        }};
    }

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[col])?;
        for batch in reader {
            let batch = batch?;
            let array = batch.column(0);
            if let Some(a) = array.as_any().downcast_ref::<Int16Array>() {
                fold_int_array!(a);
            } else if let Some(a) = array.as_any().downcast_ref::<Int32Array>() {
                fold_int_array!(a);
            } else if let Some(a) = array.as_any().downcast_ref::<Int64Array>() {
                fold_int_array!(a);
            } else if let Some(a) = array.as_any().downcast_ref::<Float32Array>() {
                sum_i128 = None;
                if a.null_count() == 0 {
                    let mut acc = 0.0f64;
                    for &v in a.values().iter() {
                        acc += v as f64;
                    }
                    sum_f64 += acc;
                    count_nonnull += a.len() as i64;
                } else {
                    for v in a.iter().flatten() {
                        sum_f64 += v as f64;
                        count_nonnull += 1;
                    }
                }
            } else if let Some(a) = array.as_any().downcast_ref::<Float64Array>() {
                sum_i128 = None;
                if a.null_count() == 0 {
                    // Use arrow::compute::sum — SIMD-optimized for f64 without nulls.
                    if let Some(s) = arrow::compute::sum(a) {
                        sum_f64 += s;
                    }
                    count_nonnull += a.len() as i64;
                } else {
                    for v in a.iter().flatten() {
                        sum_f64 += v;
                        count_nonnull += 1;
                    }
                }
            } else {
                return Err(format!(
                    "rvbbit: column '{}' is not numeric (got {:?})",
                    col,
                    array.data_type()
                )
                .into());
            }
        }
    }

    Ok(NumericScan {
        sum_f64,
        sum_i128,
        count_nonnull,
    })
}

pub(crate) fn group_count_map(
    rel_oid: u32,
    group_col: &str,
) -> Result<HashMap<Option<String>, i64>, Box<dyn std::error::Error>> {
    let key = group_count_cache_key(rel_oid, group_col)?;
    if let Some(cached) = GROUP_COUNT_CACHE.with(|cache| cache.borrow().get(&key).cloned()) {
        return Ok(cached);
    }

    if let Some(out) = group_count_map_catalog(rel_oid, group_col)? {
        GROUP_COUNT_CACHE.with(|cache| {
            cache.borrow_mut().insert(key, out.clone());
        });
        return Ok(out);
    }

    let sql = format!(
        "SELECT per_group_stats::text \
         FROM rvbbit.row_groups_visible \
         WHERE table_oid = {rel_oid}::oid"
    );
    let mut out: HashMap<Option<String>, i64> = HashMap::default();
    Spi::connect(|client| -> Result<(), String> {
        let table = client
            .select(&sql, None, &[])
            .map_err(|e| format!("looking up per-group stats: {e}"))?;
        for row in table {
            let Some(stats_text) = row
                .get::<String>(1)
                .map_err(|e| format!("reading per-group stats: {e}"))?
            else {
                continue;
            };
            let blocks: Vec<PerGroupBlock> = serde_json::from_str(&stats_text)
                .map_err(|e| format!("parsing per-group stats: {e}"))?;
            for block in blocks {
                if block.group_column != group_col {
                    continue;
                }
                for bucket in block.groups {
                    let key = json_group_value_to_text(&bucket.value);
                    *out.entry(key).or_insert(0) += bucket.count;
                }
            }
        }
        Ok(())
    })
    .map_err(|e| format!("SPI per-group stats lookup: {e}"))?;

    GROUP_COUNT_CACHE.with(|cache| {
        cache.borrow_mut().insert(key, out.clone());
    });
    Ok(out)
}

fn group_count_map_catalog(
    rel_oid: u32,
    group_col: &str,
) -> Result<Option<HashMap<Option<String>, i64>>, Box<dyn std::error::Error>> {
    let exists: Option<bool> = Spi::get_one("SELECT to_regclass('rvbbit.group_stats') IS NOT NULL")
        .ok()
        .flatten();
    if !exists.unwrap_or(false) {
        return Ok(None);
    }
    let col = group_col.replace('\'', "''");
    let sql = format!(
        "SELECT group_value_text, sum(count)::bigint \
         FROM rvbbit.group_stats \
         WHERE table_oid = {rel_oid}::oid AND group_col = '{col}' \
         GROUP BY group_value_text"
    );
    let mut out: HashMap<Option<String>, i64> = HashMap::default();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let value: Option<String> = row.get(1)?;
            let count: Option<i64> = row.get(2)?;
            out.insert(value, count.unwrap_or(0));
        }
        Ok(())
    })?;
    if out.is_empty() {
        Ok(None)
    } else {
        Ok(Some(out))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SearchPhraseTopOrder {
    EventTime,
    Phrase,
    EventTimePhrase,
}

impl SearchPhraseTopOrder {
    fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "eventtime" => Self::EventTime,
            "phrase" => Self::Phrase,
            "eventtime_phrase" => Self::EventTimePhrase,
            _ => return None,
        })
    }

    fn needs_event_time(self) -> bool {
        matches!(self, Self::EventTime | Self::EventTimePhrase)
    }

    fn needs_phrase_key(self) -> bool {
        matches!(self, Self::Phrase | Self::EventTimePhrase)
    }
}

#[derive(Eq, PartialEq)]
struct SearchPhraseTopRow {
    event_is_null: bool,
    event_micros: i64,
    phrase_key: String,
    phrase: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum SearchPhraseRowGroupMin {
    EventTime(i64),
    Phrase(String),
}

struct SearchPhraseOrderRowGroup {
    path: PathBuf,
    min_key: Option<SearchPhraseRowGroupMin>,
}

#[derive(Clone, Debug)]
enum LateOrderValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
}

impl LateOrderValue {
    fn cmp_sql_asc(&self, other: &Self) -> Ordering {
        let null_rank = |v: &LateOrderValue| i32::from(matches!(v, LateOrderValue::Null));
        match null_rank(self).cmp(&null_rank(other)) {
            Ordering::Equal => {}
            non_eq => return non_eq,
        }
        match (self, other) {
            (LateOrderValue::Null, LateOrderValue::Null) => Ordering::Equal,
            (LateOrderValue::Bool(a), LateOrderValue::Bool(b)) => a.cmp(b),
            (LateOrderValue::Int(a), LateOrderValue::Int(b)) => a.cmp(b),
            (LateOrderValue::Float(a), LateOrderValue::Float(b)) => a.total_cmp(b),
            (LateOrderValue::Text(a), LateOrderValue::Text(b)) => a.cmp(b),
            (a, b) => late_order_type_rank(a).cmp(&late_order_type_rank(b)),
        }
    }
}

fn late_order_type_rank(value: &LateOrderValue) -> i32 {
    match value {
        LateOrderValue::Null => 0,
        LateOrderValue::Bool(_) => 1,
        LateOrderValue::Int(_) => 2,
        LateOrderValue::Float(_) => 3,
        LateOrderValue::Text(_) => 4,
    }
}

impl PartialEq for LateOrderValue {
    fn eq(&self, other: &Self) -> bool {
        self.cmp_sql_asc(other) == Ordering::Equal
    }
}

impl Eq for LateOrderValue {}

impl PartialOrd for LateOrderValue {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp_sql_asc(other))
    }
}

impl Ord for LateOrderValue {
    fn cmp(&self, other: &Self) -> Ordering {
        self.cmp_sql_asc(other)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct LateTopRow {
    order: LateOrderValue,
    path_idx: usize,
    row_in_group: usize,
}

impl Ord for LateTopRow {
    fn cmp(&self, other: &Self) -> Ordering {
        self.order
            .cmp(&other.order)
            .then_with(|| self.path_idx.cmp(&other.path_idx))
            .then_with(|| self.row_in_group.cmp(&other.row_in_group))
    }
}

impl PartialOrd for LateTopRow {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl SearchPhraseTopRow {
    fn key_cmp(
        event_is_null: bool,
        event_micros: i64,
        phrase_key: &str,
        other: &SearchPhraseTopRow,
    ) -> Ordering {
        (event_is_null, event_micros, phrase_key).cmp(&(
            other.event_is_null,
            other.event_micros,
            other.phrase_key.as_str(),
        ))
    }
}

impl Ord for SearchPhraseTopRow {
    fn cmp(&self, other: &Self) -> Ordering {
        (
            self.event_is_null,
            self.event_micros,
            self.phrase_key.as_str(),
        )
            .cmp(&(
                other.event_is_null,
                other.event_micros,
                other.phrase_key.as_str(),
            ))
    }
}

impl PartialOrd for SearchPhraseTopRow {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[derive(Eq, PartialEq, Ord, PartialOrd)]
struct CountTopRow {
    count: i64,
    group_value: Option<String>,
}

enum Counts {
    Text(HashMap<Option<String>, i64>),
    Int(HashMap<i64, i64>, i64),
}

#[derive(Default)]
struct TextInterner {
    ids: HashMap<String, u32>,
    values: Vec<String>,
}

impl TextInterner {
    fn intern(&mut self, value: &str) -> u32 {
        if let Some(id) = self.ids.get(value) {
            return *id;
        }
        let id = self.values.len() as u32;
        let owned = value.to_owned();
        self.values.push(owned.clone());
        self.ids.insert(owned, id);
        id
    }

    fn owned(&self, id: u32) -> String {
        self.values
            .get(id as usize)
            .expect("text interner id is in range")
            .clone()
    }
}

struct UrlLenAgg {
    sum_len: i64,
    count: i64,
}

struct UrlLenTopRow {
    group_value: Option<i32>,
    sum_len: i64,
    count: i64,
}

struct TextTransformLenAgg {
    sum_len: i64,
    count: i64,
    min_text: Option<String>,
}

struct TextTransformLenTopRow {
    key: Option<u32>,
    sum_len: i64,
    count: i64,
    min_text: Option<String>,
}

struct Rollup2Agg {
    count: i64,
    sum_refresh: i64,
    sum_width: i64,
    width_count: i64,
}

#[derive(Eq, PartialEq, Ord, PartialOrd)]
struct Rollup2TopRow {
    count: i64,
    key1: Option<i64>,
    key2: Option<i64>,
    sum_refresh: i64,
    sum_width: i64,
    width_count: i64,
}

#[derive(Eq, PartialEq, Ord, PartialOrd)]
struct CountTop2Row {
    count: i64,
    int_value: Option<i64>,
    text_value: Option<String>,
}

struct FirstPairGroup {
    int_value: Option<i64>,
    text_value: Option<String>,
    count: i64,
}

enum DistinctCounts {
    Text(TextDistinctCounts),
    Int(HashMap<Option<i64>, HashSet<i64>>),
}

#[derive(Default)]
struct TextDistinctCounts {
    interner: TextInterner,
    counts: HashMap<Option<u32>, HashSet<i64>>,
}

enum IntArrayRef<'a> {
    I16(&'a Int16Array),
    I32(&'a Int32Array),
    I64(&'a Int64Array),
}

impl IntArrayRef<'_> {
    fn value(&self, row: usize) -> Option<i64> {
        match self {
            Self::I16(array) => {
                if array.is_null(row) {
                    None
                } else {
                    Some(array.value(row) as i64)
                }
            }
            Self::I32(array) => {
                if array.is_null(row) {
                    None
                } else {
                    Some(array.value(row) as i64)
                }
            }
            Self::I64(array) => {
                if array.is_null(row) {
                    None
                } else {
                    Some(array.value(row))
                }
            }
        }
    }
}

#[derive(Eq, PartialEq, Ord, PartialOrd)]
struct DistinctTopRow {
    count: i64,
    group_value: Option<String>,
}

#[derive(Eq, PartialEq, Ord, PartialOrd)]
struct DistinctTop2Row {
    count: i64,
    int_value: Option<i64>,
    text_value: Option<String>,
}

fn push_top_count_row(heap: &mut BinaryHeap<Reverse<CountTopRow>>, row: CountTopRow, k: usize) {
    if heap.len() == k && heap.peek().map(|worst| row <= worst.0).unwrap_or(false) {
        return;
    }
    if heap.len() == k {
        heap.pop();
    }
    heap.push(Reverse(row));
}

fn push_top_count2_row(heap: &mut BinaryHeap<Reverse<CountTop2Row>>, row: CountTop2Row, k: usize) {
    if heap.len() == k && heap.peek().map(|worst| row <= worst.0).unwrap_or(false) {
        return;
    }
    if heap.len() == k {
        heap.pop();
    }
    heap.push(Reverse(row));
}

fn top_count_rows_from_text_counts(
    counts: HashMap<Option<String>, i64>,
    k: usize,
) -> Vec<CountTopRow> {
    let mut heap: BinaryHeap<Reverse<CountTopRow>> = BinaryHeap::with_capacity(k + 1);
    for (group_value, count) in counts {
        push_top_count_row(&mut heap, CountTopRow { count, group_value }, k);
    }
    let mut rows: Vec<CountTopRow> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.group_value.cmp(&b.group_value))
    });
    rows
}

fn top_count2_rows_from_counts(
    counts: HashMap<(Option<i64>, Option<String>), i64>,
    k: usize,
) -> Vec<CountTop2Row> {
    let mut heap: BinaryHeap<Reverse<CountTop2Row>> = BinaryHeap::with_capacity(k + 1);
    for ((int_value, text_value), count) in counts {
        push_top_count2_row(
            &mut heap,
            CountTop2Row {
                count,
                int_value,
                text_value,
            },
            k,
        );
    }
    let mut rows: Vec<CountTop2Row> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.int_value.cmp(&b.int_value))
            .then_with(|| a.text_value.cmp(&b.text_value))
    });
    rows
}

fn load_text_dictionaries_for_row_groups(
    rel_oid: u32,
    col: &str,
    row_groups: &[IndexedTextDictionaryPath],
) -> Result<Option<Vec<(IndexedTextDictionaryPath, Arc<TextDictionary>)>>, Box<dyn std::error::Error>>
{
    let mut loaded = Vec::with_capacity(row_groups.len());
    for row_group in row_groups {
        let dictionary = load_text_dictionary_cached(rel_oid, col, row_group)?;
        if dictionary.codes.len() != row_group.n_rows.max(0) as usize {
            return Ok(None);
        }
        loaded.push((row_group.clone(), dictionary));
    }
    Ok(Some(loaded))
}

fn count_text_dictionary_codes(
    dictionary: &TextDictionary,
    skip_empty: bool,
) -> Result<Vec<i64>, DictionaryCountError> {
    let mut local_counts = vec![0i64; dictionary.values.len() + 1];
    for &code in &dictionary.codes {
        let idx = code as usize;
        if idx > dictionary.values.len() {
            return Err(DictionaryCountError::Invalid(
                "text dictionary code exceeds value count",
            ));
        }
        if idx == 0 {
            if !skip_empty {
                local_counts[0] += 1;
            }
            continue;
        }
        if skip_empty && dictionary.values[idx - 1].is_empty() {
            continue;
        }
        local_counts[idx] += 1;
    }
    Ok(local_counts)
}

fn text_dictionary_count_partials(
    loaded: &[(IndexedTextDictionaryPath, Arc<TextDictionary>)],
    skip_empty: bool,
) -> Result<Vec<(Arc<TextDictionary>, Vec<i64>)>, DictionaryCountError> {
    let workers = native_row_group_threads(loaded.len());
    if workers <= 1 {
        let mut out = Vec::with_capacity(loaded.len());
        for (_, dictionary) in loaded {
            out.push((
                Arc::clone(dictionary),
                count_text_dictionary_codes(dictionary, skip_empty)?,
            ));
        }
        return Ok(out);
    }

    let chunk_size = loaded.len().div_ceil(workers);
    std::thread::scope(|scope| {
        let mut handles = Vec::new();
        for chunk in loaded.chunks(chunk_size) {
            handles.push(scope.spawn(
                move || -> Result<Vec<(Arc<TextDictionary>, Vec<i64>)>, DictionaryCountError> {
                    let mut out = Vec::with_capacity(chunk.len());
                    for (_, dictionary) in chunk {
                        out.push((
                            Arc::clone(dictionary),
                            count_text_dictionary_codes(dictionary, skip_empty)?,
                        ));
                    }
                    Ok(out)
                },
            ));
        }

        let mut partials = Vec::with_capacity(loaded.len());
        for handle in handles {
            let chunk = handle.join().map_err(|_| {
                DictionaryCountError::WorkerPanic("rvbbit: native text dictionary worker panicked")
            })??;
            partials.extend(chunk);
        }
        Ok(partials)
    })
}

fn count_text_dictionary_int_text_row_group(
    row_group: &IndexedTextDictionaryPath,
    dictionary: &TextDictionary,
    int_col: &str,
    skip_empty_text: bool,
) -> Result<HashMap<(Option<i64>, Option<String>), i64>, DictionaryCountError> {
    let mut local_counts = HashMap::<(Option<i64>, u32), i64>::default();
    let reader = RowGroupReader::open_projected(&row_group.row_group_path, &[int_col])
        .map_err(|e| DictionaryCountError::Read(e.to_string()))?;
    let mut base_row = 0usize;
    for batch in reader {
        let batch = batch.map_err(|e| DictionaryCountError::Read(e.to_string()))?;
        let schema = batch.schema();
        let int_idx = schema
            .index_of(int_col)
            .map_err(|e| DictionaryCountError::Read(e.to_string()))?;
        let int_values = int_array_ref(int_col, batch.column(int_idx).as_ref())
            .map_err(|e| DictionaryCountError::Read(e.to_string()))?;
        for row in 0..batch.num_rows() {
            let dict_row = base_row + row;
            if dict_row >= dictionary.codes.len() {
                return Err(DictionaryCountError::Invalid(
                    "text dictionary row offset exceeds code count",
                ));
            }
            let code = dictionary.codes[dict_row];
            if code as usize > dictionary.values.len() {
                return Err(DictionaryCountError::Invalid(
                    "text dictionary code exceeds value count",
                ));
            }
            if code == 0 {
                if skip_empty_text {
                    continue;
                }
            } else if skip_empty_text && dictionary.values[(code - 1) as usize].is_empty() {
                continue;
            }
            *local_counts
                .entry((int_values.value(row), code))
                .or_insert(0) += 1;
        }
        base_row += batch.num_rows();
    }
    if base_row != dictionary.codes.len() {
        return Err(DictionaryCountError::Invalid(
            "projected int row count does not match text dictionary",
        ));
    }

    let mut counts = HashMap::<(Option<i64>, Option<String>), i64>::default();
    for ((int_value, code), count) in local_counts {
        let text_value = dictionary.value_for_code(code).map(str::to_string);
        *counts.entry((int_value, text_value)).or_insert(0) += count;
    }
    Ok(counts)
}

fn text_dictionary_int_text_partials(
    loaded: &[(IndexedTextDictionaryPath, Arc<TextDictionary>)],
    int_col: &str,
    skip_empty_text: bool,
) -> Result<Vec<HashMap<(Option<i64>, Option<String>), i64>>, DictionaryCountError> {
    let workers = native_row_group_threads(loaded.len());
    if workers <= 1 {
        let mut out = Vec::with_capacity(loaded.len());
        for (row_group, dictionary) in loaded {
            out.push(count_text_dictionary_int_text_row_group(
                row_group,
                dictionary,
                int_col,
                skip_empty_text,
            )?);
        }
        return Ok(out);
    }

    let chunk_size = loaded.len().div_ceil(workers);
    std::thread::scope(|scope| {
        let mut handles = Vec::new();
        for chunk in loaded.chunks(chunk_size) {
            handles.push(scope.spawn(
                move || -> Result<
                    Vec<HashMap<(Option<i64>, Option<String>), i64>>,
                    DictionaryCountError,
                > {
                    let mut out = Vec::with_capacity(chunk.len());
                    for (row_group, dictionary) in chunk {
                        out.push(count_text_dictionary_int_text_row_group(
                            row_group,
                            dictionary,
                            int_col,
                            skip_empty_text,
                        )?);
                    }
                    Ok(out)
                },
            ));
        }

        let mut partials = Vec::with_capacity(loaded.len());
        for handle in handles {
            let chunk = handle.join().map_err(|_| {
                DictionaryCountError::WorkerPanic(
                    "rvbbit: native int/text dictionary worker panicked",
                )
            })??;
            partials.extend(chunk);
        }
        Ok(partials)
    })
}

fn count_text_dictionary_filtered_codes(
    dictionary: &TextDictionary,
    filter_bitmap: &RoaringBitmap,
) -> Result<Vec<i64>, DictionaryCountError> {
    let mut local_counts = vec![0i64; dictionary.values.len() + 1];
    for row in filter_bitmap.iter() {
        let idx = row as usize;
        if idx >= dictionary.codes.len() {
            return Err(DictionaryCountError::Invalid(
                "filter bitmap row exceeds text dictionary code count",
            ));
        }
        let code = dictionary.codes[idx] as usize;
        if code > dictionary.values.len() {
            return Err(DictionaryCountError::Invalid(
                "text dictionary code exceeds value count",
            ));
        }
        local_counts[code] += 1;
    }
    Ok(local_counts)
}

fn text_dictionary_filtered_partials(
    loaded: &[(Arc<TextDictionary>, RoaringBitmap)],
) -> Result<Vec<(Arc<TextDictionary>, Vec<i64>)>, DictionaryCountError> {
    let workers = native_row_group_threads(loaded.len());
    if workers <= 1 {
        let mut out = Vec::with_capacity(loaded.len());
        for (dictionary, filter_bitmap) in loaded {
            out.push((
                Arc::clone(dictionary),
                count_text_dictionary_filtered_codes(dictionary, filter_bitmap)?,
            ));
        }
        return Ok(out);
    }

    let chunk_size = loaded.len().div_ceil(workers);
    std::thread::scope(|scope| {
        let mut handles = Vec::new();
        for chunk in loaded.chunks(chunk_size) {
            handles.push(scope.spawn(
                move || -> Result<Vec<(Arc<TextDictionary>, Vec<i64>)>, DictionaryCountError> {
                    let mut out = Vec::with_capacity(chunk.len());
                    for (dictionary, filter_bitmap) in chunk {
                        out.push((
                            Arc::clone(dictionary),
                            count_text_dictionary_filtered_codes(dictionary, filter_bitmap)?,
                        ));
                    }
                    Ok(out)
                },
            ));
        }

        let mut partials = Vec::with_capacity(loaded.len());
        for handle in handles {
            let chunk = handle.join().map_err(|_| {
                DictionaryCountError::WorkerPanic(
                    "rvbbit: native filtered dictionary worker panicked",
                )
            })??;
            partials.extend(chunk);
        }
        Ok(partials)
    })
}

fn try_text_dictionary_top_count_1col(
    rel_oid: u32,
    col: &str,
    skip_empty: bool,
    k: usize,
) -> Result<Option<Vec<CountTopRow>>, Box<dyn std::error::Error>> {
    let Some(row_groups) = lookup_text_dictionary_row_groups(rel_oid, col)? else {
        return Ok(None);
    };
    if row_groups.is_empty() {
        return Ok(Some(Vec::new()));
    }

    let Some(loaded) = load_text_dictionaries_for_row_groups(rel_oid, col, &row_groups)? else {
        return Ok(None);
    };
    let Some(partials) =
        dictionary_count_result(text_dictionary_count_partials(&loaded, skip_empty))?
    else {
        return Ok(None);
    };
    let mut counts = HashMap::<Option<String>, i64>::default();
    for (dictionary, local_counts) in partials {
        merge_text_dictionary_counts(&mut counts, &dictionary, local_counts);
    }

    Ok(Some(top_count_rows_from_text_counts(counts, k)))
}

fn try_text_dictionary_top_count_int_text(
    rel_oid: u32,
    int_col: &str,
    text_col: &str,
    skip_empty_text: bool,
    k: usize,
) -> Result<Option<Vec<CountTop2Row>>, Box<dyn std::error::Error>> {
    let Some(row_groups) = lookup_text_dictionary_row_groups(rel_oid, text_col)? else {
        return Ok(None);
    };
    if row_groups.is_empty() {
        return Ok(Some(Vec::new()));
    }

    let Some(loaded) = load_text_dictionaries_for_row_groups(rel_oid, text_col, &row_groups)?
    else {
        return Ok(None);
    };
    let Some(partials) = dictionary_count_result(text_dictionary_int_text_partials(
        &loaded,
        int_col,
        skip_empty_text,
    ))?
    else {
        return Ok(None);
    };
    let mut counts = HashMap::<(Option<i64>, Option<String>), i64>::default();
    for partial in partials {
        for (key, count) in partial {
            *counts.entry(key).or_insert(0) += count;
        }
    }

    Ok(Some(top_count2_rows_from_counts(counts, k)))
}

fn try_text_dictionary_filtered_top_count(
    rel_oid: u32,
    keys: &[KeySpec],
    filters: &[FilterSpec],
    k: usize,
) -> Result<Option<Vec<vector::GroupRow>>, Box<dyn std::error::Error>> {
    if k == 0 || keys.len() != 1 {
        return Ok(None);
    }
    if filters.iter().any(|filter| {
        matches!(
            filter,
            FilterSpec::TextContains { .. } | FilterSpec::TextNotContains { .. }
        )
    }) {
        return Ok(None);
    }
    let KeySpec::Text { col } = &keys[0] else {
        return Ok(None);
    };

    let row_groups = lookup_paths_for_oid_with_ids(rel_oid)?;
    if row_groups.is_empty() {
        return Ok(Some(Vec::new()));
    }

    let columns = bitmap_filter_columns(filters);
    let filtered_row_groups = if columns.is_empty() {
        row_groups
            .iter()
            .map(|row_group| {
                (
                    row_group.rg_id,
                    row_group.n_rows,
                    full_row_bitmap(row_group.n_rows),
                )
            })
            .collect::<Vec<_>>()
    } else {
        if !column_bitmaps_table_available()? {
            return Ok(None);
        }
        let catalog = load_column_bitmaps_cached(rel_oid, &columns, &row_groups)?;
        if catalog.is_empty() {
            return Ok(None);
        }
        let mut filtered = Vec::with_capacity(row_groups.len());
        for row_group in &row_groups {
            let Some(bitmap) =
                bitmap_for_all_filters(&catalog, row_group.rg_id, row_group.n_rows, filters)
            else {
                return Ok(None);
            };
            filtered.push((row_group.rg_id, row_group.n_rows, bitmap));
        }
        filtered
    };
    if filtered_row_groups
        .iter()
        .all(|(_, _, bitmap)| bitmap.is_empty())
    {
        return Ok(Some(Vec::new()));
    }

    let Some(dictionary_row_groups) = lookup_text_dictionary_row_groups(rel_oid, col)? else {
        return Ok(None);
    };
    if dictionary_row_groups.len() != filtered_row_groups.len() {
        return Ok(None);
    }

    let mut counts = HashMap::<Option<String>, i64>::default();
    let mut loaded = Vec::new();
    for (row_group, (rg_id, n_rows, filter_bitmap)) in
        dictionary_row_groups.iter().zip(filtered_row_groups.iter())
    {
        if row_group.rg_id != *rg_id || row_group.n_rows != *n_rows {
            return Ok(None);
        }
        if filter_bitmap.is_empty() {
            continue;
        }

        let dictionary = load_text_dictionary_cached(rel_oid, col, row_group)?;
        if dictionary.codes.len() != row_group.n_rows.max(0) as usize {
            return Ok(None);
        }
        loaded.push((dictionary, filter_bitmap.clone()));
    }
    let Some(partials) = dictionary_count_result(text_dictionary_filtered_partials(&loaded))?
    else {
        return Ok(None);
    };
    for (dictionary, local_counts) in partials {
        merge_text_dictionary_counts(&mut counts, &dictionary, local_counts);
    }

    let rows = top_count_rows_from_text_counts(counts, k)
        .into_iter()
        .map(|row| vector::GroupRow {
            keys: vec![KeyValue::Text(row.group_value)],
            count: row.count,
            aggs: Vec::new(),
        })
        .collect::<Vec<_>>();
    Ok(Some(rows))
}

fn merge_text_dictionary_counts(
    counts: &mut HashMap<Option<String>, i64>,
    dictionary: &TextDictionary,
    local_counts: Vec<i64>,
) {
    for (code, count) in local_counts.into_iter().enumerate() {
        if count == 0 {
            continue;
        }
        let value = if code == 0 {
            None
        } else {
            dictionary.values.get(code - 1).cloned()
        };
        *counts.entry(value).or_insert(0) += count;
    }
}

fn push_distinct_top_row(
    heap: &mut BinaryHeap<Reverse<DistinctTopRow>>,
    row: DistinctTopRow,
    k: usize,
) {
    if heap.len() == k && heap.peek().map(|worst| row <= worst.0).unwrap_or(false) {
        return;
    }
    if heap.len() == k {
        heap.pop();
    }
    heap.push(Reverse(row));
}

fn push_distinct_top2_row(
    heap: &mut BinaryHeap<Reverse<DistinctTop2Row>>,
    row: DistinctTop2Row,
    k: usize,
) {
    if heap.len() == k && heap.peek().map(|worst| row <= worst.0).unwrap_or(false) {
        return;
    }
    if heap.len() == k {
        heap.pop();
    }
    heap.push(Reverse(row));
}

fn push_rollup2_top_row(
    heap: &mut BinaryHeap<Reverse<Rollup2TopRow>>,
    row: Rollup2TopRow,
    k: usize,
) {
    if heap.len() == k && heap.peek().map(|worst| row <= worst.0).unwrap_or(false) {
        return;
    }
    if heap.len() == k {
        heap.pop();
    }
    heap.push(Reverse(row));
}

fn lookup_searchphrase_order_row_groups(
    rel_oid: u32,
    order: SearchPhraseTopOrder,
) -> Result<Vec<SearchPhraseOrderRowGroup>, String> {
    let mut groups = lookup_paths_with_stats(rel_oid, None)?
        .into_iter()
        .map(|rg| {
            let stats = parse_column_stats(rg.stats_text.as_deref());
            let min_key = match order {
                SearchPhraseTopOrder::EventTime | SearchPhraseTopOrder::EventTimePhrase => {
                    stat_i64_bounds(&stats, "EventTime")
                        .map(|(min, _)| SearchPhraseRowGroupMin::EventTime(min))
                }
                SearchPhraseTopOrder::Phrase => {
                    stat_text_min(&stats, "SearchPhrase").map(SearchPhraseRowGroupMin::Phrase)
                }
            };
            SearchPhraseOrderRowGroup {
                path: rg.path,
                min_key,
            }
        })
        .collect::<Vec<_>>();

    groups.sort_by(|a, b| searchphrase_group_min_cmp(order, &a.min_key, &b.min_key));
    Ok(groups)
}

fn searchphrase_group_min_cmp(
    order: SearchPhraseTopOrder,
    left: &Option<SearchPhraseRowGroupMin>,
    right: &Option<SearchPhraseRowGroupMin>,
) -> Ordering {
    match (left, right) {
        (Some(a), Some(b)) => match (order, a, b) {
            (
                SearchPhraseTopOrder::EventTime | SearchPhraseTopOrder::EventTimePhrase,
                SearchPhraseRowGroupMin::EventTime(a),
                SearchPhraseRowGroupMin::EventTime(b),
            ) => a.cmp(b),
            (
                SearchPhraseTopOrder::Phrase,
                SearchPhraseRowGroupMin::Phrase(a),
                SearchPhraseRowGroupMin::Phrase(b),
            ) => a.cmp(b),
            _ => Ordering::Equal,
        },
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => Ordering::Equal,
    }
}

fn searchphrase_group_min_cannot_improve(
    order: SearchPhraseTopOrder,
    min_key: &SearchPhraseRowGroupMin,
    worst: &SearchPhraseTopRow,
) -> bool {
    match (order, min_key) {
        (SearchPhraseTopOrder::EventTime, SearchPhraseRowGroupMin::EventTime(min_event_micros)) => {
            !worst.event_is_null && *min_event_micros >= worst.event_micros
        }
        (
            SearchPhraseTopOrder::EventTimePhrase,
            SearchPhraseRowGroupMin::EventTime(min_event_micros),
        ) => !worst.event_is_null && *min_event_micros > worst.event_micros,
        (SearchPhraseTopOrder::Phrase, SearchPhraseRowGroupMin::Phrase(min_phrase)) => {
            min_phrase.as_str() >= worst.phrase_key.as_str()
        }
        _ => false,
    }
}

/// Specialized top-N projected scan for ClickBench-style:
///
///   SELECT "SearchPhrase" FROM rel
///   WHERE "SearchPhrase" <> ''
///   ORDER BY "EventTime" [, "SearchPhrase"] LIMIT k
///
/// and:
///
///   ORDER BY "SearchPhrase" LIMIT k
///
/// This bypasses the tuple executor and full-table sort. It reads only the
/// required parquet columns, keeps a max-heap of the current smallest k keys,
/// then returns at most k rows in sorted order.
#[pg_extern]
fn top_searchphrase_ordered(
    rel: pg_sys::Oid,
    order_by: &str,
    k: i32,
) -> Result<TableIterator<'static, (name!(search_phrase, String),)>, Box<dyn std::error::Error>> {
    let order = SearchPhraseTopOrder::parse(order_by)
        .ok_or_else(|| format!("unknown SearchPhrase top-N order '{order_by}'"))?;
    if k <= 0 {
        return Ok(TableIterator::new(Vec::<(String,)>::new().into_iter()));
    }
    let k = k as usize;
    let rel_oid = rel.to_u32();
    let row_groups = lookup_searchphrase_order_row_groups(rel_oid, order)?;
    let has_unknown_order_stats = row_groups.iter().any(|rg| rg.min_key.is_none());
    let projection: &[&str] = if order.needs_event_time() {
        &["SearchPhrase", "EventTime"]
    } else {
        &["SearchPhrase"]
    };
    let mut heap: BinaryHeap<SearchPhraseTopRow> = BinaryHeap::with_capacity(k + 1);

    for row_group in row_groups {
        if let (Some(min_key), Some(worst)) = (&row_group.min_key, heap.peek()) {
            if heap.len() == k && searchphrase_group_min_cannot_improve(order, min_key, worst) {
                if has_unknown_order_stats {
                    continue;
                }
                break;
            }
        }

        let reader = RowGroupReader::open_projected(&row_group.path, projection)?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let phrase_idx = schema.index_of("SearchPhrase")?;
            let phrase_arr = batch
                .column(phrase_idx)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or("SearchPhrase column is not utf8")?;
            let event_arr = if order.needs_event_time() {
                let event_idx = schema.index_of("EventTime")?;
                Some(
                    batch
                        .column(event_idx)
                        .as_any()
                        .downcast_ref::<TimestampMicrosecondArray>()
                        .ok_or("EventTime column is not timestamp(us)")?,
                )
            } else {
                None
            };

            for row in 0..phrase_arr.len() {
                if phrase_arr.is_null(row) {
                    continue;
                }
                let phrase = phrase_arr.value(row);
                if phrase.is_empty() {
                    continue;
                }

                let (event_is_null, event_micros) = match event_arr {
                    Some(arr) if arr.is_null(row) => (true, 0),
                    Some(arr) => (false, arr.value(row)),
                    None => (false, 0),
                };
                let phrase_key = if order.needs_phrase_key() { phrase } else { "" };

                if heap.len() == k
                    && SearchPhraseTopRow::key_cmp(
                        event_is_null,
                        event_micros,
                        phrase_key,
                        heap.peek().expect("heap is non-empty"),
                    ) != Ordering::Less
                {
                    continue;
                }

                if heap.len() == k {
                    heap.pop();
                }
                heap.push(SearchPhraseTopRow {
                    event_is_null,
                    event_micros,
                    phrase_key: phrase_key.to_string(),
                    phrase: phrase.to_string(),
                });
            }
        }
    }

    let mut rows = heap.into_vec();
    rows.sort_unstable();
    Ok(TableIterator::new(
        rows.into_iter().map(|row| (row.phrase,)),
    ))
}

/// Specialized one-column `GROUP BY col ORDER BY count(*) DESC LIMIT k`.
///
/// The return value is text so the rewriter can cast it back to the original
/// SQL type in a donor query. This avoids emitting every base row through the
/// tuple executor just to feed Postgres' aggregate node.
#[pg_extern]
fn top_count_1col(
    rel: pg_sys::Oid,
    col: &str,
    skip_empty: bool,
    k: i32,
) -> Result<
    TableIterator<'static, (name!(group_value, Option<String>), name!(count, i64))>,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, i64)>::new().into_iter(),
        ));
    }
    let k = k as usize;
    let rel_oid = rel.to_u32();
    if let Some(rows) = try_text_dictionary_top_count_1col(rel_oid, col, skip_empty, k)? {
        return Ok(TableIterator::new(
            rows.into_iter().map(|row| (row.group_value, row.count)),
        ));
    }
    let paths = lookup_paths_for_oid(rel_oid)?;

    let mut counts: Option<Counts> = None;

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let col_idx = schema.index_of(col)?;
            let array = batch.column(col_idx);
            if let Some(a) = array.as_any().downcast_ref::<StringArray>() {
                if counts.is_none() {
                    counts = Some(Counts::Text(HashMap::default()));
                }
                let Some(Counts::Text(map)) = counts.as_mut() else {
                    return Err(
                        format!("rvbbit.top_count_1col: mixed column type for '{col}'").into(),
                    );
                };
                // Fast path: no nulls. Iterate the raw value indices and
                // skip the per-row bitmap check that dominated the loop.
                if a.null_count() == 0 {
                    for row in 0..a.len() {
                        let value = a.value(row);
                        if skip_empty && value.is_empty() {
                            continue;
                        }
                        *map.entry(Some(value.to_string())).or_insert(0) += 1;
                    }
                } else {
                    for row in 0..a.len() {
                        if a.is_null(row) {
                            if !skip_empty {
                                *map.entry(None).or_insert(0) += 1;
                            }
                            continue;
                        }
                        let value = a.value(row);
                        if skip_empty && value.is_empty() {
                            continue;
                        }
                        *map.entry(Some(value.to_string())).or_insert(0) += 1;
                    }
                }
            } else if let Some(a) = array.as_any().downcast_ref::<Int64Array>() {
                count_i64_values_arr(
                    col,
                    skip_empty,
                    &mut counts,
                    a.values(),
                    |row| a.is_null(row),
                    a.null_count() == 0,
                )?;
            } else if let Some(a) = array.as_any().downcast_ref::<Int32Array>() {
                count_i64_values_arr(
                    col,
                    skip_empty,
                    &mut counts,
                    a.values(),
                    |row| a.is_null(row),
                    a.null_count() == 0,
                )?;
            } else if let Some(a) = array.as_any().downcast_ref::<Int16Array>() {
                count_i64_values_arr(
                    col,
                    skip_empty,
                    &mut counts,
                    a.values(),
                    |row| a.is_null(row),
                    a.null_count() == 0,
                )?;
            } else {
                return Err(format!(
                    "rvbbit.top_count_1col: column '{}' has unsupported type {:?}",
                    col,
                    array.data_type()
                )
                .into());
            }
        }
    }

    let mut heap: BinaryHeap<Reverse<CountTopRow>> = BinaryHeap::with_capacity(k + 1);
    match counts {
        Some(Counts::Text(map)) => {
            for (group_value, count) in map {
                push_top_count_row(&mut heap, CountTopRow { count, group_value }, k);
            }
        }
        Some(Counts::Int(map, null_count)) => {
            for (value, count) in map {
                push_top_count_row(
                    &mut heap,
                    CountTopRow {
                        count,
                        group_value: Some(value.to_string()),
                    },
                    k,
                );
            }
            if null_count > 0 {
                push_top_count_row(
                    &mut heap,
                    CountTopRow {
                        count: null_count,
                        group_value: None,
                    },
                    k,
                );
            }
        }
        None => {}
    }

    let mut rows: Vec<CountTopRow> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.group_value.cmp(&b.group_value))
    });
    Ok(TableIterator::new(
        rows.into_iter().map(|row| (row.group_value, row.count)),
    ))
}

/// Exact `COUNT(DISTINCT col)` for integer columns, ignoring NULLs.
#[pg_extern]
fn count_distinct_int(rel: pg_sys::Oid, col: &str) -> Result<i64, Box<dyn std::error::Error>> {
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut values = HashSet::default();

    // Fast path when null_count == 0 iterates the values() slice directly,
    // avoiding the per-row is_null bitmap check. With nulls present we use
    // iter().flatten() which fuses null skipping into a single branch.
    macro_rules! insert_int_distinct {
        ($a:expr) => {{
            let a = $a;
            if a.null_count() == 0 {
                values.reserve(a.len());
                for &v in a.values().iter() {
                    values.insert(v as i64);
                }
            } else {
                for v in a.iter().flatten() {
                    values.insert(v as i64);
                }
            }
        }};
    }

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let col_idx = schema.index_of(col)?;
            let array = batch.column(col_idx);
            if let Some(a) = array.as_any().downcast_ref::<Int64Array>() {
                insert_int_distinct!(a);
            } else if let Some(a) = array.as_any().downcast_ref::<Int32Array>() {
                insert_int_distinct!(a);
            } else if let Some(a) = array.as_any().downcast_ref::<Int16Array>() {
                insert_int_distinct!(a);
            } else {
                return Err(format!(
                    "rvbbit.count_distinct_int: column '{}' has unsupported type {:?}",
                    col,
                    array.data_type()
                )
                .into());
            }
        }
    }

    Ok(values.len() as i64)
}

/// Specialized `(int_col, text_col) GROUP BY ... ORDER BY count(*) DESC LIMIT k`.
///
/// This covers ClickBench pair-count shapes without emitting one tuple per
/// input row into Postgres. The integer key is returned as text so donor SQL
/// can cast it back to smallint/integer/bigint as appropriate.
#[pg_extern]
fn top_count_int_text(
    rel: pg_sys::Oid,
    int_col: &str,
    text_col: &str,
    skip_empty_text: bool,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(group_int, Option<String>),
            name!(group_text, Option<String>),
            name!(count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, Option<String>, i64)>::new().into_iter(),
        ));
    }
    let k = k as usize;
    let rel_oid = rel.to_u32();
    if let Some(rows) =
        try_text_dictionary_top_count_int_text(rel_oid, int_col, text_col, skip_empty_text, k)?
    {
        return Ok(TableIterator::new(rows.into_iter().map(|row| {
            (
                row.int_value.map(|v| v.to_string()),
                row.text_value,
                row.count,
            )
        })));
    }
    let paths = lookup_paths_for_oid(rel_oid)?;
    if should_use_owned_text_pair_counts(rel_oid, int_col, skip_empty_text) {
        let rows = top_count_int_text_owned_rows(&paths, int_col, text_col, skip_empty_text, k)?;
        return Ok(TableIterator::new(rows.into_iter().map(|row| {
            (
                row.int_value.map(|v| v.to_string()),
                row.text_value,
                row.count,
            )
        })));
    }
    let mut interner = TextInterner::default();
    let mut counts: HashMap<(Option<i64>, Option<u32>), i64> = HashMap::default();

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[int_col, text_col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let int_idx = schema.index_of(int_col)?;
            let text_idx = schema.index_of(text_col)?;
            let int_array = batch.column(int_idx);
            let text_arr = batch
                .column(text_idx)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or_else(|| format!("column '{text_col}' is not utf8"))?;

            if let Some(a) = int_array.as_any().downcast_ref::<Int64Array>() {
                count_int_text_values(
                    &mut counts,
                    &mut interner,
                    text_arr,
                    skip_empty_text,
                    a.len(),
                    |row| {
                        if a.is_null(row) {
                            None
                        } else {
                            Some(a.value(row))
                        }
                    },
                );
            } else if let Some(a) = int_array.as_any().downcast_ref::<Int32Array>() {
                count_int_text_values(
                    &mut counts,
                    &mut interner,
                    text_arr,
                    skip_empty_text,
                    a.len(),
                    |row| {
                        if a.is_null(row) {
                            None
                        } else {
                            Some(a.value(row) as i64)
                        }
                    },
                );
            } else if let Some(a) = int_array.as_any().downcast_ref::<Int16Array>() {
                count_int_text_values(
                    &mut counts,
                    &mut interner,
                    text_arr,
                    skip_empty_text,
                    a.len(),
                    |row| {
                        if a.is_null(row) {
                            None
                        } else {
                            Some(a.value(row) as i64)
                        }
                    },
                );
            } else {
                return Err(format!(
                    "rvbbit.top_count_int_text: column '{}' has unsupported type {:?}",
                    int_col,
                    int_array.data_type()
                )
                .into());
            }
        }
    }

    let mut heap: BinaryHeap<Reverse<CountTop2Row>> = BinaryHeap::with_capacity(k + 1);
    for ((int_value, text_value), count) in counts {
        push_top_count2_row(
            &mut heap,
            CountTop2Row {
                count,
                int_value,
                text_value: text_value.map(|id| interner.owned(id)),
            },
            k,
        );
    }

    let mut rows: Vec<CountTop2Row> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.int_value.cmp(&b.int_value))
            .then_with(|| a.text_value.cmp(&b.text_value))
    });

    Ok(TableIterator::new(rows.into_iter().map(|row| {
        (
            row.int_value.map(|v| v.to_string()),
            row.text_value,
            row.count,
        )
    })))
}

fn top_count_int_text_owned_rows(
    paths: &[PathBuf],
    int_col: &str,
    text_col: &str,
    skip_empty_text: bool,
    k: usize,
) -> Result<Vec<CountTop2Row>, Box<dyn std::error::Error>> {
    let mut counts: HashMap<(Option<i64>, Option<String>), i64> = HashMap::default();

    for path in paths {
        let reader = RowGroupReader::open_projected(path, &[int_col, text_col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let int_idx = schema.index_of(int_col)?;
            let text_idx = schema.index_of(text_col)?;
            let int_array = batch.column(int_idx);
            let text_arr = batch
                .column(text_idx)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or_else(|| format!("column '{text_col}' is not utf8"))?;

            if let Some(a) = int_array.as_any().downcast_ref::<Int64Array>() {
                count_int_text_values_owned(
                    &mut counts,
                    text_arr,
                    skip_empty_text,
                    a.len(),
                    |row| {
                        if a.is_null(row) {
                            None
                        } else {
                            Some(a.value(row))
                        }
                    },
                );
            } else if let Some(a) = int_array.as_any().downcast_ref::<Int32Array>() {
                count_int_text_values_owned(
                    &mut counts,
                    text_arr,
                    skip_empty_text,
                    a.len(),
                    |row| {
                        if a.is_null(row) {
                            None
                        } else {
                            Some(a.value(row) as i64)
                        }
                    },
                );
            } else if let Some(a) = int_array.as_any().downcast_ref::<Int16Array>() {
                count_int_text_values_owned(
                    &mut counts,
                    text_arr,
                    skip_empty_text,
                    a.len(),
                    |row| {
                        if a.is_null(row) {
                            None
                        } else {
                            Some(a.value(row) as i64)
                        }
                    },
                );
            } else {
                return Err(format!(
                    "rvbbit.top_count_int_text: column '{}' has unsupported type {:?}",
                    int_col,
                    int_array.data_type()
                )
                .into());
            }
        }
    }

    let mut heap: BinaryHeap<Reverse<CountTop2Row>> = BinaryHeap::with_capacity(k + 1);
    for ((int_value, text_value), count) in counts {
        push_top_count2_row(
            &mut heap,
            CountTop2Row {
                count,
                int_value,
                text_value,
            },
            k,
        );
    }
    let mut rows: Vec<CountTop2Row> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.int_value.cmp(&b.int_value))
            .then_with(|| a.text_value.cmp(&b.text_value))
    });
    Ok(rows)
}

/// Specialized `(int_col, extract(minute from ts_col), text_col) GROUP BY
/// ... ORDER BY count(*) DESC LIMIT k`.
#[pg_extern]
fn top_count_int_minute_text(
    rel: pg_sys::Oid,
    int_col: &str,
    ts_col: &str,
    text_col: &str,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(group_int, Option<String>),
            name!(minute, Option<i32>),
            name!(group_text, Option<String>),
            name!(count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, Option<i32>, Option<String>, i64)>::new().into_iter(),
        ));
    }
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let rows = vector::topk_group_aggregate(
        paths,
        &[
            KeySpec::Int {
                col: int_col.to_string(),
            },
            KeySpec::TimestampMinute {
                col: ts_col.to_string(),
            },
            KeySpec::Text {
                col: text_col.to_string(),
            },
        ],
        &[],
        &[],
        k as usize,
    )?;

    Ok(TableIterator::new(rows.into_iter().map(|row| {
        let int_value = match &row.keys[0] {
            KeyValue::Int(value) => *value,
            _ => None,
        };
        let minute_value = match &row.keys[1] {
            KeyValue::Minute(value) => *value,
            _ => None,
        };
        let text_value = match &row.keys[2] {
            KeyValue::Text(value) => value.clone(),
            _ => None,
        };
        (
            int_value.map(|v| v.to_string()),
            minute_value,
            text_value,
            row.count,
        )
    })))
}

/// Generic filtered top-count over projected column vectors.
///
/// This is intentionally shape-oriented, not benchmark-name-oriented:
/// callers provide typed group keys plus simple conjunctive filters, and the
/// shared vector engine performs projected scan, filtering, grouping and top-k.
#[pg_extern]
fn top_count_filtered(
    rel: pg_sys::Oid,
    key_cols: Vec<String>,
    key_kinds: Vec<String>,
    filter_cols: Vec<String>,
    filter_ops: Vec<String>,
    filter_values: Vec<String>,
    text_not_empty_cols: Vec<String>,
    k: i32,
    offset: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(key1, Option<String>),
            name!(key2, Option<String>),
            name!(key3, Option<String>),
            name!(key4, Option<String>),
            name!(key5, Option<String>),
            name!(count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(
                Option<String>,
                Option<String>,
                Option<String>,
                Option<String>,
                Option<String>,
                i64,
            )>::new()
            .into_iter(),
        ));
    }
    if key_cols.len() != key_kinds.len() || key_cols.len() > 5 {
        return Err(
            "rvbbit.top_count_filtered: key columns/kinds mismatch or too many keys".into(),
        );
    }
    if filter_cols.len() != filter_ops.len() || filter_cols.len() != filter_values.len() {
        return Err("rvbbit.top_count_filtered: filter arrays must have equal length".into());
    }

    let mut keys = Vec::with_capacity(key_cols.len());
    for (col, kind) in key_cols.iter().zip(key_kinds.iter()) {
        keys.push(match kind.as_str() {
            "int" => KeySpec::Int { col: col.clone() },
            "date" => KeySpec::Date { col: col.clone() },
            "text" => KeySpec::Text { col: col.clone() },
            "timestamp_trunc_minute" => KeySpec::TimestampTruncMinute { col: col.clone() },
            other => {
                return Err(
                    format!("rvbbit.top_count_filtered: unsupported key kind {other:?}").into(),
                )
            }
        });
    }

    let mut filters = Vec::new();
    for col in text_not_empty_cols {
        filters.push(FilterSpec::TextNotEmpty { col });
    }
    for ((col, op), raw_value) in filter_cols
        .into_iter()
        .zip(filter_ops.into_iter())
        .zip(filter_values.into_iter())
    {
        filters.push(match op.as_str() {
            "eq" => FilterSpec::IntEq {
                col,
                value: raw_value.parse()?,
            },
            "ne" => FilterSpec::IntNe {
                col,
                value: raw_value.parse()?,
            },
            "ge" => FilterSpec::IntGe {
                col,
                value: raw_value.parse()?,
            },
            "le" => FilterSpec::IntLe {
                col,
                value: raw_value.parse()?,
            },
            "in" => FilterSpec::IntIn {
                col,
                values: raw_value
                    .split(',')
                    .map(|value| value.trim().parse::<i64>())
                    .collect::<Result<Vec<_>, _>>()?,
            },
            other => {
                return Err(
                    format!("rvbbit.top_count_filtered: unsupported filter op {other:?}").into(),
                )
            }
        });
    }

    let offset = offset.max(0) as usize;
    let wanted = (k as usize).saturating_add(offset);
    let rel_oid = rel.to_u32();
    let rows = if let Some(rows) =
        try_text_dictionary_filtered_top_count(rel_oid, &keys, &filters, wanted)?
    {
        rows
    } else if let Some(rows) = try_bitmap_top_count(rel_oid, &keys, &filters, wanted)? {
        rows
    } else if let Some(paths) = try_filter_bitmaps(rel_oid, &filters)? {
        if paths.is_empty() {
            return Ok(TableIterator::new(
                Vec::<(
                    Option<String>,
                    Option<String>,
                    Option<String>,
                    Option<String>,
                    Option<String>,
                    i64,
                )>::new()
                .into_iter(),
            ));
        }
        vector::topk_group_aggregate_with_row_bitmaps(paths, &keys, &filters, &[], wanted)?
    } else {
        let paths = lookup_paths_for_oid_pruned(rel_oid, &filters)?;
        if paths.is_empty() {
            return Ok(TableIterator::new(
                Vec::<(
                    Option<String>,
                    Option<String>,
                    Option<String>,
                    Option<String>,
                    Option<String>,
                    i64,
                )>::new()
                .into_iter(),
            ));
        }
        vector::topk_group_aggregate(paths, &keys, &filters, &[], wanted)?
    };
    let out = rows
        .into_iter()
        .skip(offset)
        .map(|row| {
            let key = |idx: usize| row.keys.get(idx).and_then(key_value_to_text);
            (key(0), key(1), key(2), key(3), key(4), row.count)
        })
        .collect::<Vec<_>>();

    Ok(TableIterator::new(out.into_iter()))
}

#[derive(Clone)]
enum FloatExprSpec {
    Col(String),
    Mul(String, String),
    MulOneMinus(String, String),
    MulOneMinusOnePlus(String, String, String),
}

#[derive(Clone)]
enum FloatFilterSpec {
    Date {
        col: String,
        op: FloatCompareOp,
        value: i32,
    },
    Float {
        col: String,
        op: FloatCompareOp,
        value: f64,
    },
}

#[derive(Clone, Copy)]
enum FloatCompareOp {
    Eq,
    Lt,
    Le,
    Gt,
    Ge,
}

impl FloatCompareOp {
    fn parse(raw: &str) -> Result<Self, String> {
        match raw {
            "eq" => Ok(Self::Eq),
            "lt" => Ok(Self::Lt),
            "le" => Ok(Self::Le),
            "gt" => Ok(Self::Gt),
            "ge" => Ok(Self::Ge),
            other => Err(format!(
                "rvbbit.vector_float_agg: unsupported compare op {other:?}"
            )),
        }
    }

    fn compare_i32(self, actual: i32, expected: i32) -> bool {
        match self {
            Self::Eq => actual == expected,
            Self::Lt => actual < expected,
            Self::Le => actual <= expected,
            Self::Gt => actual > expected,
            Self::Ge => actual >= expected,
        }
    }

    fn compare_i64(self, actual: i64, expected: i64) -> bool {
        match self {
            Self::Eq => actual == expected,
            Self::Lt => actual < expected,
            Self::Le => actual <= expected,
            Self::Gt => actual > expected,
            Self::Ge => actual >= expected,
        }
    }

    fn compare_f64(self, actual: f64, expected: f64) -> bool {
        match self {
            Self::Eq => actual == expected,
            Self::Lt => actual < expected,
            Self::Le => actual <= expected,
            Self::Gt => actual > expected,
            Self::Ge => actual >= expected,
        }
    }
}

#[derive(Default)]
struct FloatAggState {
    count: i64,
    sums: [(f64, i64); 8],
    avgs: [(f64, i64); 8],
}

#[derive(Clone, Copy)]
enum FloatColumn<'a> {
    F32(&'a Float32Array),
    F64(&'a Float64Array),
    I16(&'a Int16Array),
    I32(&'a Int32Array),
    I64(&'a Int64Array),
}

impl FloatColumn<'_> {
    fn value(&self, row: usize) -> Option<f64> {
        match self {
            Self::F32(array) => (!array.is_null(row)).then(|| array.value(row) as f64),
            Self::F64(array) => (!array.is_null(row)).then(|| array.value(row)),
            Self::I16(array) => (!array.is_null(row)).then(|| array.value(row) as f64),
            Self::I32(array) => (!array.is_null(row)).then(|| array.value(row) as f64),
            Self::I64(array) => (!array.is_null(row)).then(|| array.value(row) as f64),
        }
    }
}

#[derive(Clone, Copy)]
enum IntColumn<'a> {
    I16(&'a Int16Array),
    I32(&'a Int32Array),
    I64(&'a Int64Array),
}

impl IntColumn<'_> {
    fn value(&self, row: usize) -> Option<i64> {
        match self {
            Self::I16(array) => (!array.is_null(row)).then(|| array.value(row) as i64),
            Self::I32(array) => (!array.is_null(row)).then(|| array.value(row) as i64),
            Self::I64(array) => (!array.is_null(row)).then(|| array.value(row)),
        }
    }
}

#[derive(Clone, Copy)]
enum DateColumn<'a> {
    Date32(&'a Date32Array),
    I32(&'a Int32Array),
}

impl DateColumn<'_> {
    fn value(&self, row: usize) -> Option<i32> {
        match self {
            Self::Date32(array) => (!array.is_null(row)).then(|| array.value(row)),
            Self::I32(array) => (!array.is_null(row)).then(|| array.value(row)),
        }
    }
}

enum BoundKeyColumn<'a> {
    Text(&'a StringArray),
    Int(IntColumn<'a>),
    Date(DateColumn<'a>),
}

enum BoundFloatFilter<'a> {
    Date {
        col: DateColumn<'a>,
        op: FloatCompareOp,
        value: i32,
    },
    Float {
        col: FloatColumn<'a>,
        op: FloatCompareOp,
        value: f64,
    },
}

enum BoundFloatExpr<'a> {
    Col(FloatColumn<'a>),
    Mul(FloatColumn<'a>, FloatColumn<'a>),
    MulOneMinus(FloatColumn<'a>, FloatColumn<'a>),
    MulOneMinusOnePlus(FloatColumn<'a>, FloatColumn<'a>, FloatColumn<'a>),
}

enum BoundBinaryFilter<'a> {
    Date(DateColumn<'a>, DateColumn<'a>, FloatCompareOp),
    Float(FloatColumn<'a>, FloatColumn<'a>, FloatCompareOp),
    Int(IntColumn<'a>, IntColumn<'a>, FloatCompareOp),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct EncodedFloatKey {
    len: u8,
    nulls: u8,
    values: [i64; 3],
}

#[derive(Default)]
struct TextKeyDict {
    ids: HashMap<String, i64>,
    values: Vec<String>,
}

impl TextKeyDict {
    fn id_for(&mut self, value: &str) -> i64 {
        if let Some(id) = self.ids.get(value) {
            return *id;
        }
        let id = (self.values.len() + 1) as i64;
        self.values.push(value.to_string());
        self.ids.insert(value.to_string(), id);
        id
    }

    fn value_for(&self, id: i64) -> Option<String> {
        if id <= 0 {
            return None;
        }
        self.values.get((id - 1) as usize).cloned()
    }
}

/// Generic projected vector aggregate for simple analytical SQL.
///
/// The caller supplies key columns, conjunctive filters, and expression specs
/// produced by the rewriter. This is deliberately a small composable kernel:
/// it is not tied to a benchmark query name, and it handles grouped and
/// ungrouped SUM/AVG/COUNT over projected parquet batches.
#[pg_extern]
fn vector_float_agg(
    rel: pg_sys::Oid,
    key_cols: Vec<String>,
    key_kinds: Vec<String>,
    filter_cols: Vec<String>,
    filter_kinds: Vec<String>,
    filter_ops: Vec<String>,
    filter_values: Vec<String>,
    sum_exprs: Vec<String>,
    avg_exprs: Vec<String>,
) -> Result<
    TableIterator<
        'static,
        (
            name!(key1, Option<String>),
            name!(key2, Option<String>),
            name!(key3, Option<String>),
            name!(count, i64),
            name!(sum1, Option<f64>),
            name!(sum2, Option<f64>),
            name!(sum3, Option<f64>),
            name!(sum4, Option<f64>),
            name!(sum5, Option<f64>),
            name!(sum6, Option<f64>),
            name!(sum7, Option<f64>),
            name!(sum8, Option<f64>),
            name!(avg_sum1, Option<f64>),
            name!(avg_count1, i64),
            name!(avg_sum2, Option<f64>),
            name!(avg_count2, i64),
            name!(avg_sum3, Option<f64>),
            name!(avg_count3, i64),
            name!(avg_sum4, Option<f64>),
            name!(avg_count4, i64),
            name!(avg_sum5, Option<f64>),
            name!(avg_count5, i64),
            name!(avg_sum6, Option<f64>),
            name!(avg_count6, i64),
            name!(avg_sum7, Option<f64>),
            name!(avg_count7, i64),
            name!(avg_sum8, Option<f64>),
            name!(avg_count8, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if key_cols.len() != key_kinds.len() || key_cols.len() > 3 {
        return Err("rvbbit.vector_float_agg: key columns/kinds mismatch or too many keys".into());
    }
    if filter_cols.len() != filter_kinds.len()
        || filter_cols.len() != filter_ops.len()
        || filter_cols.len() != filter_values.len()
    {
        return Err("rvbbit.vector_float_agg: filter arrays must have equal length".into());
    }
    if sum_exprs.len() > 8 || avg_exprs.len() > 8 {
        return Err("rvbbit.vector_float_agg: at most 8 sum and 8 avg expressions".into());
    }

    let sum_specs = sum_exprs
        .iter()
        .map(|raw| parse_float_expr_spec(raw))
        .collect::<Result<Vec<_>, _>>()?;
    let avg_specs = avg_exprs
        .iter()
        .map(|raw| parse_float_expr_spec(raw))
        .collect::<Result<Vec<_>, _>>()?;
    let filters = filter_cols
        .into_iter()
        .zip(filter_kinds.into_iter())
        .zip(filter_ops.into_iter())
        .zip(filter_values.into_iter())
        .map(|(((col, kind), op), value)| match kind.as_str() {
            "date" => Ok(FloatFilterSpec::Date {
                col,
                op: FloatCompareOp::parse(&op)?,
                value: value
                    .parse()
                    .map_err(|e| format!("rvbbit.vector_float_agg: invalid date value: {e}"))?,
            }),
            "float" => Ok(FloatFilterSpec::Float {
                col,
                op: FloatCompareOp::parse(&op)?,
                value: value
                    .parse()
                    .map_err(|e| format!("rvbbit.vector_float_agg: invalid float value: {e}"))?,
            }),
            other => Err(format!(
                "rvbbit.vector_float_agg: unsupported filter kind {other:?}"
            )),
        })
        .collect::<Result<Vec<_>, String>>()?;

    let mut projection = Vec::<String>::new();
    for col in &key_cols {
        push_projection_col(&mut projection, col);
    }
    for filter in &filters {
        match filter {
            FloatFilterSpec::Date { col, .. } | FloatFilterSpec::Float { col, .. } => {
                push_projection_col(&mut projection, col)
            }
        }
    }
    for expr in sum_specs.iter().chain(avg_specs.iter()) {
        push_float_expr_projection(&mut projection, expr);
    }
    let projection_refs: Vec<&str> = projection.iter().map(String::as_str).collect();

    let mut groups = HashMap::<EncodedFloatKey, FloatAggState>::default();
    let mut ungrouped = FloatAggState::default();
    let mut text_dicts = (0..key_cols.len())
        .map(|_| TextKeyDict::default())
        .collect::<Vec<_>>();
    let has_keys = !key_cols.is_empty();
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    for path in paths {
        for batch in vector::read_projected_batches(&path, &projection_refs)? {
            let bound_keys = bind_float_key_columns(&batch, &key_cols, &key_kinds)?;
            let bound_filters = bind_float_filters(&batch, &filters)?;
            let bound_sums = bind_float_exprs(&batch, &sum_specs)?;
            let bound_avgs = bind_float_exprs(&batch, &avg_specs)?;
            for row in 0..batch.num_rows() {
                if !bound_float_filters_match(row, &bound_filters) {
                    continue;
                }

                if has_keys {
                    let key = encoded_float_group_key(row, &bound_keys, &mut text_dicts)?;
                    let state = groups.entry(key).or_default();
                    update_float_agg_state(row, state, &bound_sums, &bound_avgs);
                } else {
                    update_float_agg_state(row, &mut ungrouped, &bound_sums, &bound_avgs);
                }
            }
        }
    }

    let sum_len = sum_specs.len();
    let avg_len = avg_specs.len();
    let state_to_row = |key: Vec<Option<String>>, state: FloatAggState| {
        let key_at = |idx: usize| key.get(idx).cloned().flatten();
        let sum_at = |idx: usize| {
            if idx < sum_len && state.sums[idx].1 > 0 {
                Some(state.sums[idx].0)
            } else {
                None
            }
        };
        let avg_sum_at = |idx: usize| {
            if idx < avg_len && state.avgs[idx].1 > 0 {
                Some(state.avgs[idx].0)
            } else {
                None
            }
        };
        let avg_count_at = |idx: usize| {
            if idx < avg_len {
                state.avgs[idx].1
            } else {
                0
            }
        };
        (
            key_at(0),
            key_at(1),
            key_at(2),
            state.count,
            sum_at(0),
            sum_at(1),
            sum_at(2),
            sum_at(3),
            sum_at(4),
            sum_at(5),
            sum_at(6),
            sum_at(7),
            avg_sum_at(0),
            avg_count_at(0),
            avg_sum_at(1),
            avg_count_at(1),
            avg_sum_at(2),
            avg_count_at(2),
            avg_sum_at(3),
            avg_count_at(3),
            avg_sum_at(4),
            avg_count_at(4),
            avg_sum_at(5),
            avg_count_at(5),
            avg_sum_at(6),
            avg_count_at(6),
            avg_sum_at(7),
            avg_count_at(7),
        )
    };

    let out = if has_keys {
        groups
            .into_iter()
            .map(|(key, state)| {
                let decoded_key = decode_float_group_key(&key, &key_kinds, &text_dicts);
                state_to_row(decoded_key, state)
            })
            .collect::<Vec<_>>()
    } else {
        vec![state_to_row(Vec::new(), ungrouped)]
    };

    Ok(TableIterator::new(out.into_iter()))
}

fn update_float_agg_state(
    row: usize,
    state: &mut FloatAggState,
    sum_specs: &[BoundFloatExpr<'_>],
    avg_specs: &[BoundFloatExpr<'_>],
) {
    state.count += 1;
    for (idx, spec) in sum_specs.iter().enumerate() {
        if let Some(value) = spec.value(row) {
            if let Some(sum) = state.sums.get_mut(idx) {
                sum.0 += value;
                sum.1 += 1;
            }
        }
    }
    for (idx, spec) in avg_specs.iter().enumerate() {
        if let Some(value) = spec.value(row) {
            if let Some(avg) = state.avgs.get_mut(idx) {
                avg.0 += value;
                avg.1 += 1;
            }
        }
    }
}

/// Return only grouped keys whose SUM(expression) satisfies a threshold.
///
/// This is the semi-join companion to vector_float_agg: SQL shapes such as
/// `x IN (SELECT key FROM t GROUP BY key HAVING sum(v) > c)` need keys, not
/// aggregate payload rows. Filtering inside the vector kernel avoids sending
/// every group back through PostgreSQL only for a later semi-join to discard
/// almost all of them.
#[pg_extern]
fn vector_sum_having_keys(
    rel: pg_sys::Oid,
    key_cols: Vec<String>,
    key_kinds: Vec<String>,
    filter_cols: Vec<String>,
    filter_kinds: Vec<String>,
    filter_ops: Vec<String>,
    filter_values: Vec<String>,
    sum_expr: &str,
    having_op: &str,
    having_value: f64,
) -> Result<
    TableIterator<
        'static,
        (
            name!(key1, Option<String>),
            name!(key2, Option<String>),
            name!(key3, Option<String>),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if key_cols.len() != key_kinds.len() || key_cols.is_empty() || key_cols.len() > 3 {
        return Err("rvbbit.vector_sum_having_keys: key columns/kinds mismatch".into());
    }
    if filter_cols.len() != filter_kinds.len()
        || filter_cols.len() != filter_ops.len()
        || filter_cols.len() != filter_values.len()
    {
        return Err("rvbbit.vector_sum_having_keys: filter arrays must have equal length".into());
    }

    let sum_spec = parse_float_expr_spec(sum_expr)?;
    let having_op = FloatCompareOp::parse(having_op)?;
    let filters = filter_cols
        .into_iter()
        .zip(filter_kinds.into_iter())
        .zip(filter_ops.into_iter())
        .zip(filter_values.into_iter())
        .map(|(((col, kind), op), value)| match kind.as_str() {
            "date" => Ok(FloatFilterSpec::Date {
                col,
                op: FloatCompareOp::parse(&op)?,
                value: value.parse().map_err(|e| {
                    format!("rvbbit.vector_sum_having_keys: invalid date value: {e}")
                })?,
            }),
            "float" => Ok(FloatFilterSpec::Float {
                col,
                op: FloatCompareOp::parse(&op)?,
                value: value.parse().map_err(|e| {
                    format!("rvbbit.vector_sum_having_keys: invalid float value: {e}")
                })?,
            }),
            other => Err(format!(
                "rvbbit.vector_sum_having_keys: unsupported filter kind {other:?}"
            )),
        })
        .collect::<Result<Vec<_>, String>>()?;

    let mut projection = Vec::<String>::new();
    for col in &key_cols {
        push_projection_col(&mut projection, col);
    }
    for filter in &filters {
        match filter {
            FloatFilterSpec::Date { col, .. } | FloatFilterSpec::Float { col, .. } => {
                push_projection_col(&mut projection, col)
            }
        }
    }
    push_float_expr_projection(&mut projection, &sum_spec);
    let projection_refs: Vec<&str> = projection.iter().map(String::as_str).collect();

    let mut groups = HashMap::<EncodedFloatKey, FloatAggState>::default();
    let mut text_dicts = (0..key_cols.len())
        .map(|_| TextKeyDict::default())
        .collect::<Vec<_>>();
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    for path in paths {
        for batch in vector::read_projected_batches(&path, &projection_refs)? {
            let bound_keys = bind_float_key_columns(&batch, &key_cols, &key_kinds)?;
            let bound_filters = bind_float_filters(&batch, &filters)?;
            let bound_sum = bind_float_expr(&batch, &sum_spec)?;
            for row in 0..batch.num_rows() {
                if !bound_float_filters_match(row, &bound_filters) {
                    continue;
                }
                let Some(value) = bound_sum.value(row) else {
                    continue;
                };
                let key = encoded_float_group_key(row, &bound_keys, &mut text_dicts)?;
                let state = groups.entry(key).or_default();
                state.count += 1;
                state.sums[0].0 += value;
                state.sums[0].1 += 1;
            }
        }
    }

    let out = groups
        .into_iter()
        .filter_map(|(key, state)| {
            if state.sums[0].1 == 0 || !having_op.compare_f64(state.sums[0].0, having_value) {
                return None;
            }
            let decoded = decode_float_group_key(&key, &key_kinds, &text_dicts);
            let key_at = |idx: usize| decoded.get(idx).cloned().flatten();
            Some((key_at(0), key_at(1), key_at(2)))
        })
        .collect::<Vec<_>>();

    Ok(TableIterator::new(out.into_iter()))
}

#[derive(Clone, Copy, Default)]
struct GroupMemberSummary {
    first_member: i64,
    total_members_set: bool,
    total_multi_member: bool,
    first_filtered_member: i64,
    filtered_member_set: bool,
    filtered_multi_member: bool,
}

impl GroupMemberSummary {
    fn add_total_member(&mut self, member: i64) {
        if !self.total_members_set {
            self.first_member = member;
            self.total_members_set = true;
        } else if self.first_member != member {
            self.total_multi_member = true;
        }
    }

    fn add_filtered_member(&mut self, member: i64) {
        if !self.filtered_member_set {
            self.first_filtered_member = member;
            self.filtered_member_set = true;
        } else if self.first_filtered_member != member {
            self.filtered_multi_member = true;
        }
    }

    fn accepts_filtered_member(&self, member: i64) -> bool {
        self.total_multi_member
            && self.filtered_member_set
            && !self.filtered_multi_member
            && self.first_filtered_member == member
    }
}

/// Emit the original filtered `(group_key, member_key)` rows that satisfy a
/// common self semi/anti condition:
///
/// - the group has more than one distinct member overall
/// - the filtered subset for that group has exactly one distinct member
/// - the emitted row belongs to that filtered member
///
/// This is the vector form of:
/// `filtered_row AND EXISTS(other member in group) AND NOT EXISTS(other
/// filtered member in group)`.
#[pg_extern]
fn vector_group_member_filtered_rows(
    rel: pg_sys::Oid,
    group_col: &str,
    member_col: &str,
    filter_left_col: &str,
    filter_right_col: &str,
    filter_kind: &str,
    filter_op: &str,
) -> Result<
    TableIterator<'static, (name!(group_key, i64), name!(member_key, i64))>,
    Box<dyn std::error::Error>,
> {
    let filter_op = FloatCompareOp::parse(filter_op)?;
    let projection = if group_col == member_col {
        vec![group_col, filter_left_col, filter_right_col]
    } else {
        vec![group_col, member_col, filter_left_col, filter_right_col]
    };
    let mut projection_owned = Vec::<String>::new();
    for col in projection {
        push_projection_col(&mut projection_owned, col);
    }
    let projection_refs: Vec<&str> = projection_owned.iter().map(String::as_str).collect();

    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut groups = HashMap::<i64, GroupMemberSummary>::default();
    let mut filtered_rows = Vec::<(i64, i64)>::new();
    for path in paths {
        for batch in vector::read_projected_batches(&path, &projection_refs)? {
            let schema = batch.schema();
            let group_idx = schema.index_of(group_col)?;
            let member_idx = schema.index_of(member_col)?;
            let group_values = int_column(batch.column(group_idx).as_ref())?;
            let member_values = int_column(batch.column(member_idx).as_ref())?;
            let filter = bind_binary_filter(
                &batch,
                filter_left_col,
                filter_right_col,
                filter_kind,
                filter_op,
            )?;
            for row in 0..batch.num_rows() {
                let (Some(group), Some(member)) =
                    (group_values.value(row), member_values.value(row))
                else {
                    continue;
                };
                let matched = binary_filter_match(row, &filter);
                let summary = groups.entry(group).or_default();
                summary.add_total_member(member);
                if matched {
                    summary.add_filtered_member(member);
                    filtered_rows.push((group, member));
                }
            }
        }
    }

    let out = filtered_rows
        .into_iter()
        .filter(|(group, member)| {
            groups
                .get(group)
                .is_some_and(|summary| summary.accepts_filtered_member(*member))
        })
        .collect::<Vec<_>>();
    Ok(TableIterator::new(out.into_iter()))
}

/// Return distinct integer keys for rows matching an intra-row binary filter.
///
/// This is the vector semi-join source for SQL like:
/// `EXISTS (SELECT 1 FROM fact WHERE fact.key = outer.key AND fact.a < fact.b)`.
#[pg_extern]
fn vector_filtered_distinct_int_keys(
    rel: pg_sys::Oid,
    key_col: &str,
    filter_left_col: &str,
    filter_right_col: &str,
    filter_kind: &str,
    filter_op: &str,
) -> Result<TableIterator<'static, (name!(key, i64),)>, Box<dyn std::error::Error>> {
    let filter_op = FloatCompareOp::parse(filter_op)?;
    let mut projection = Vec::<String>::new();
    for col in [key_col, filter_left_col, filter_right_col] {
        push_projection_col(&mut projection, col);
    }
    let projection_refs: Vec<&str> = projection.iter().map(String::as_str).collect();

    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut keys = HashSet::<i64>::default();
    for path in paths {
        for batch in vector::read_projected_batches(&path, &projection_refs)? {
            let schema = batch.schema();
            let key_idx = schema.index_of(key_col)?;
            let key_values = int_column(batch.column(key_idx).as_ref())?;
            let filter = bind_binary_filter(
                &batch,
                filter_left_col,
                filter_right_col,
                filter_kind,
                filter_op,
            )?;
            for row in 0..batch.num_rows() {
                if !binary_filter_match(row, &filter) {
                    continue;
                }
                if let Some(key) = key_values.value(row) {
                    keys.insert(key);
                }
            }
        }
    }

    Ok(TableIterator::new(
        keys.into_iter()
            .map(|key| (key,))
            .collect::<Vec<_>>()
            .into_iter(),
    ))
}

/// Emit rows from a fact table whose integer key appears in a dimension
/// table selected by a fixed text-substring predicate. The output shape is a
/// typed row-source building block used by the rewriter for SQL such as:
///
/// `fact.key = dim.key AND dim.name LIKE '%needle%'`
///
/// PostgreSQL can still join/aggregate the resulting rows normally, but the
/// high-cardinality fact scan is reduced before tuple materialization.
#[pg_extern]
fn vector_int_key_text_filter_rows_1i64_2i32_3f64(
    rel: pg_sys::Oid,
    filter_col: &str,
    key_rel: pg_sys::Oid,
    key_col: &str,
    text_col: &str,
    needle: &str,
    out_i64_col: &str,
    out_i32_col1: &str,
    out_i32_col2: &str,
    out_f64_col1: &str,
    out_f64_col2: &str,
    out_f64_col3: &str,
) -> Result<
    TableIterator<
        'static,
        (
            name!(out_i64, Option<i64>),
            name!(out_i32_1, Option<i32>),
            name!(out_i32_2, Option<i32>),
            name!(out_f64_1, Option<f64>),
            name!(out_f64_2, Option<f64>),
            name!(out_f64_3, Option<f64>),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    let allowed_keys = int_key_set_for_text_contains(key_rel.to_u32(), key_col, text_col, needle)?;
    if allowed_keys.is_empty() {
        return Ok(TableIterator::new(
            Vec::<(
                Option<i64>,
                Option<i32>,
                Option<i32>,
                Option<f64>,
                Option<f64>,
                Option<f64>,
            )>::new()
            .into_iter(),
        ));
    }

    let mut projection = Vec::<String>::new();
    for col in [
        filter_col,
        out_i64_col,
        out_i32_col1,
        out_i32_col2,
        out_f64_col1,
        out_f64_col2,
        out_f64_col3,
    ] {
        push_projection_col(&mut projection, col);
    }
    let projection_refs: Vec<&str> = projection.iter().map(String::as_str).collect();

    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut out = Vec::new();
    for path in paths {
        for batch in vector::read_projected_batches(&path, &projection_refs)? {
            let schema = batch.schema();
            let filter_idx = schema.index_of(filter_col)?;
            let filter_values = int_column(batch.column(filter_idx).as_ref())?;
            let out_i64 = int_column(batch.column(schema.index_of(out_i64_col)?).as_ref())?;
            let out_i32_1 = int_column(batch.column(schema.index_of(out_i32_col1)?).as_ref())?;
            let out_i32_2 = int_column(batch.column(schema.index_of(out_i32_col2)?).as_ref())?;
            let out_f64_1 = float_column(batch.column(schema.index_of(out_f64_col1)?).as_ref())?;
            let out_f64_2 = float_column(batch.column(schema.index_of(out_f64_col2)?).as_ref())?;
            let out_f64_3 = float_column(batch.column(schema.index_of(out_f64_col3)?).as_ref())?;
            for row in 0..batch.num_rows() {
                let Some(key) = filter_values.value(row) else {
                    continue;
                };
                if !allowed_keys.contains(&key) {
                    continue;
                }
                out.push((
                    out_i64.value(row),
                    out_i32_1.value(row).and_then(|v| i32::try_from(v).ok()),
                    out_i32_2.value(row).and_then(|v| i32::try_from(v).ok()),
                    out_f64_1.value(row),
                    out_f64_2.value(row),
                    out_f64_3.value(row),
                ));
            }
        }
    }

    Ok(TableIterator::new(out.into_iter()))
}

fn int_key_set_for_text_contains(
    rel_oid: u32,
    key_col: &str,
    text_col: &str,
    needle: &str,
) -> Result<HashSet<i64>, Box<dyn std::error::Error>> {
    let filters = vec![FilterSpec::TextContains {
        col: text_col.to_string(),
        needle: needle.to_string(),
    }];
    let paths = lookup_paths_for_oid_pruned(rel_oid, &filters)?;
    let projection = [key_col, text_col];
    let mut keys = HashSet::<i64>::default();
    for path in paths {
        for batch in vector::read_projected_batches(&path, &projection)? {
            let schema = batch.schema();
            let key_idx = schema.index_of(key_col)?;
            let text_idx = schema.index_of(text_col)?;
            let key_values = int_column(batch.column(key_idx).as_ref())?;
            let Some(text_values) = batch
                .column(text_idx)
                .as_any()
                .downcast_ref::<StringArray>()
            else {
                return Err(format!(
                    "rvbbit.vector_int_key_text_filter_rows: column {text_col:?} is not text"
                )
                .into());
            };
            for row in 0..batch.num_rows() {
                if text_values.is_null(row) || !text_values.value(row).contains(needle) {
                    continue;
                }
                if let Some(key) = key_values.value(row) {
                    keys.insert(key);
                }
            }
        }
    }
    Ok(keys)
}

fn bind_binary_filter<'a>(
    batch: &'a arrow::record_batch::RecordBatch,
    left_col: &str,
    right_col: &str,
    kind: &str,
    op: FloatCompareOp,
) -> Result<BoundBinaryFilter<'a>, Box<dyn std::error::Error>> {
    let schema = batch.schema();
    let left_idx = schema.index_of(left_col)?;
    let right_idx = schema.index_of(right_col)?;
    let left = batch.column(left_idx).as_ref();
    let right = batch.column(right_idx).as_ref();
    match kind {
        "date" => Ok(BoundBinaryFilter::Date(
            date_column(left)?,
            date_column(right)?,
            op,
        )),
        "float" => Ok(BoundBinaryFilter::Float(
            float_column(left)?,
            float_column(right)?,
            op,
        )),
        "int" => Ok(BoundBinaryFilter::Int(
            int_column(left)?,
            int_column(right)?,
            op,
        )),
        other => Err(format!(
            "rvbbit.vector_group_member_filtered_rows: unsupported filter kind {other:?}"
        )
        .into()),
    }
}

fn binary_filter_match(row: usize, filter: &BoundBinaryFilter<'_>) -> bool {
    match filter {
        BoundBinaryFilter::Date(left, right, op) => left
            .value(row)
            .zip(right.value(row))
            .map(|(left, right)| op.compare_i32(left, right))
            .unwrap_or(false),
        BoundBinaryFilter::Float(left, right, op) => left
            .value(row)
            .zip(right.value(row))
            .map(|(left, right)| op.compare_f64(left, right))
            .unwrap_or(false),
        BoundBinaryFilter::Int(left, right, op) => left
            .value(row)
            .zip(right.value(row))
            .map(|(left, right)| op.compare_i64(left, right))
            .unwrap_or(false),
    }
}

fn bind_float_key_columns<'a>(
    batch: &'a arrow::record_batch::RecordBatch,
    key_cols: &[String],
    key_kinds: &[String],
) -> Result<Vec<BoundKeyColumn<'a>>, Box<dyn std::error::Error>> {
    let schema = batch.schema();
    key_cols
        .iter()
        .zip(key_kinds.iter())
        .map(|(col, kind)| {
            let idx = schema.index_of(col)?;
            let column = batch.column(idx).as_ref();
            match kind.as_str() {
                "text" => {
                    let Some(array) = column.as_any().downcast_ref::<StringArray>() else {
                        return Err("rvbbit.vector_float_agg: text key column is not String".into());
                    };
                    Ok(BoundKeyColumn::Text(array))
                }
                "int" => Ok(BoundKeyColumn::Int(int_column(column)?)),
                "date" => Ok(BoundKeyColumn::Date(date_column(column)?)),
                other => {
                    Err(format!("rvbbit.vector_float_agg: unsupported key kind {other:?}").into())
                }
            }
        })
        .collect()
}

fn bind_float_filters<'a>(
    batch: &'a arrow::record_batch::RecordBatch,
    filters: &'a [FloatFilterSpec],
) -> Result<Vec<BoundFloatFilter<'a>>, Box<dyn std::error::Error>> {
    let schema = batch.schema();
    filters
        .iter()
        .map(|filter| match filter {
            FloatFilterSpec::Date { col, op, value } => {
                let idx = schema.index_of(col)?;
                Ok(BoundFloatFilter::Date {
                    col: date_column(batch.column(idx).as_ref())?,
                    op: *op,
                    value: *value,
                })
            }
            FloatFilterSpec::Float { col, op, value } => {
                let idx = schema.index_of(col)?;
                Ok(BoundFloatFilter::Float {
                    col: float_column(batch.column(idx).as_ref())?,
                    op: *op,
                    value: *value,
                })
            }
        })
        .collect()
}

fn bind_float_exprs<'a>(
    batch: &'a arrow::record_batch::RecordBatch,
    exprs: &[FloatExprSpec],
) -> Result<Vec<BoundFloatExpr<'a>>, Box<dyn std::error::Error>> {
    exprs
        .iter()
        .map(|expr| bind_float_expr(batch, expr))
        .collect()
}

fn bind_float_expr<'a>(
    batch: &'a arrow::record_batch::RecordBatch,
    expr: &FloatExprSpec,
) -> Result<BoundFloatExpr<'a>, Box<dyn std::error::Error>> {
    match expr {
        FloatExprSpec::Col(a) => Ok(BoundFloatExpr::Col(bind_float_col(batch, a)?)),
        FloatExprSpec::Mul(a, b) => Ok(BoundFloatExpr::Mul(
            bind_float_col(batch, a)?,
            bind_float_col(batch, b)?,
        )),
        FloatExprSpec::MulOneMinus(a, b) => Ok(BoundFloatExpr::MulOneMinus(
            bind_float_col(batch, a)?,
            bind_float_col(batch, b)?,
        )),
        FloatExprSpec::MulOneMinusOnePlus(a, b, c) => Ok(BoundFloatExpr::MulOneMinusOnePlus(
            bind_float_col(batch, a)?,
            bind_float_col(batch, b)?,
            bind_float_col(batch, c)?,
        )),
    }
}

fn bind_float_col<'a>(
    batch: &'a arrow::record_batch::RecordBatch,
    col: &str,
) -> Result<FloatColumn<'a>, Box<dyn std::error::Error>> {
    let idx = batch.schema().index_of(col)?;
    float_column(batch.column(idx).as_ref())
}

fn bound_float_filters_match(row: usize, filters: &[BoundFloatFilter<'_>]) -> bool {
    for filter in filters {
        let matched = match filter {
            BoundFloatFilter::Date { col, op, value } => col
                .value(row)
                .map(|actual| op.compare_i32(actual, *value))
                .unwrap_or(false),
            BoundFloatFilter::Float { col, op, value } => {
                let Some(actual) = col.value(row) else {
                    return false;
                };
                op.compare_f64(actual, *value)
            }
        };
        if !matched {
            return false;
        }
    }
    true
}

impl BoundFloatExpr<'_> {
    fn value(&self, row: usize) -> Option<f64> {
        match self {
            Self::Col(a) => a.value(row),
            Self::Mul(a, b) => a.value(row).zip(b.value(row)).map(|(a, b)| a * b),
            Self::MulOneMinus(a, b) => a.value(row).zip(b.value(row)).map(|(a, b)| a * (1.0 - b)),
            Self::MulOneMinusOnePlus(a, b, c) => {
                let a = a.value(row)?;
                let b = b.value(row)?;
                let c = c.value(row)?;
                Some(a * (1.0 - b) * (1.0 + c))
            }
        }
    }
}

fn encoded_float_group_key(
    row: usize,
    keys: &[BoundKeyColumn<'_>],
    text_dicts: &mut [TextKeyDict],
) -> Result<EncodedFloatKey, Box<dyn std::error::Error>> {
    if keys.len() > 3 || keys.len() != text_dicts.len() {
        return Err("rvbbit.vector_float_agg: invalid key binding".into());
    }
    let mut out = EncodedFloatKey {
        len: keys.len() as u8,
        nulls: 0,
        values: [0; 3],
    };
    for (idx, key) in keys.iter().enumerate() {
        match key {
            BoundKeyColumn::Text(array) => {
                if array.is_null(row) {
                    out.nulls |= 1 << idx;
                } else {
                    let value = array.value(row);
                    out.values[idx] = encode_short_text_key(value)
                        .unwrap_or_else(|| text_dicts[idx].id_for(value));
                }
            }
            BoundKeyColumn::Int(array) => {
                if let Some(value) = array.value(row) {
                    out.values[idx] = value;
                } else {
                    out.nulls |= 1 << idx;
                }
            }
            BoundKeyColumn::Date(array) => {
                if let Some(value) = array.value(row) {
                    out.values[idx] = value as i64;
                } else {
                    out.nulls |= 1 << idx;
                }
            }
        }
    }
    Ok(out)
}

fn decode_float_group_key(
    key: &EncodedFloatKey,
    key_kinds: &[String],
    text_dicts: &[TextKeyDict],
) -> Vec<Option<String>> {
    (0..key.len as usize)
        .map(|idx| {
            if (key.nulls & (1 << idx)) != 0 {
                return None;
            }
            match key_kinds.get(idx).map(String::as_str) {
                Some("text") => decode_short_text_key(key.values[idx]).or_else(|| {
                    text_dicts
                        .get(idx)
                        .and_then(|dict| dict.value_for(key.values[idx]))
                }),
                Some("date") => Some(vector::format_date32(key.values[idx] as i32)),
                Some("int") => Some(key.values[idx].to_string()),
                _ => None,
            }
        })
        .collect()
}

fn encode_short_text_key(value: &str) -> Option<i64> {
    let bytes = value.as_bytes();
    if bytes.len() > 7 {
        return None;
    }
    let mut payload = (bytes.len() as u64) << 56;
    for (idx, byte) in bytes.iter().enumerate() {
        payload |= (*byte as u64) << (idx * 8);
    }
    Some((payload | (1u64 << 63)) as i64)
}

fn decode_short_text_key(value: i64) -> Option<String> {
    if value >= 0 {
        return None;
    }
    let payload = (value as u64) & !(1u64 << 63);
    let len = (payload >> 56) as usize;
    if len > 7 {
        return None;
    }
    let mut bytes = Vec::with_capacity(len);
    for idx in 0..len {
        bytes.push(((payload >> (idx * 8)) & 0xff) as u8);
    }
    String::from_utf8(bytes).ok()
}

fn int_column(column: &dyn Array) -> Result<IntColumn<'_>, Box<dyn std::error::Error>> {
    if let Some(array) = column.as_any().downcast_ref::<Int16Array>() {
        Ok(IntColumn::I16(array))
    } else if let Some(array) = column.as_any().downcast_ref::<Int32Array>() {
        Ok(IntColumn::I32(array))
    } else if let Some(array) = column.as_any().downcast_ref::<Int64Array>() {
        Ok(IntColumn::I64(array))
    } else {
        Err("rvbbit.vector_float_agg: int key column is not integer".into())
    }
}

fn date_column(column: &dyn Array) -> Result<DateColumn<'_>, Box<dyn std::error::Error>> {
    if let Some(array) = column.as_any().downcast_ref::<Date32Array>() {
        Ok(DateColumn::Date32(array))
    } else if let Some(array) = column.as_any().downcast_ref::<Int32Array>() {
        Ok(DateColumn::I32(array))
    } else {
        Err("rvbbit.vector_float_agg: date column is not Date32/Int32".into())
    }
}

fn parse_float_expr_spec(raw: &str) -> Result<FloatExprSpec, String> {
    let parts = raw.split(':').collect::<Vec<_>>();
    match parts.as_slice() {
        ["col", col] => Ok(FloatExprSpec::Col((*col).to_string())),
        ["mul", a, b] => Ok(FloatExprSpec::Mul((*a).to_string(), (*b).to_string())),
        ["mul_one_minus", a, b] => Ok(FloatExprSpec::MulOneMinus(
            (*a).to_string(),
            (*b).to_string(),
        )),
        ["mul_one_minus_one_plus", a, b, c] => Ok(FloatExprSpec::MulOneMinusOnePlus(
            (*a).to_string(),
            (*b).to_string(),
            (*c).to_string(),
        )),
        _ => Err(format!(
            "rvbbit.vector_float_agg: unsupported expression spec {raw:?}"
        )),
    }
}

fn push_projection_col(projection: &mut Vec<String>, col: &str) {
    if !projection.iter().any(|existing| existing == col) {
        projection.push(col.to_string());
    }
}

fn push_float_expr_projection(projection: &mut Vec<String>, expr: &FloatExprSpec) {
    match expr {
        FloatExprSpec::Col(a) => push_projection_col(projection, a),
        FloatExprSpec::Mul(a, b) | FloatExprSpec::MulOneMinus(a, b) => {
            push_projection_col(projection, a);
            push_projection_col(projection, b);
        }
        FloatExprSpec::MulOneMinusOnePlus(a, b, c) => {
            push_projection_col(projection, a);
            push_projection_col(projection, b);
            push_projection_col(projection, c);
        }
    }
}

fn float_column(column: &dyn Array) -> Result<FloatColumn<'_>, Box<dyn std::error::Error>> {
    if let Some(array) = column.as_any().downcast_ref::<Float32Array>() {
        Ok(FloatColumn::F32(array))
    } else if let Some(array) = column.as_any().downcast_ref::<Float64Array>() {
        Ok(FloatColumn::F64(array))
    } else if let Some(array) = column.as_any().downcast_ref::<Int16Array>() {
        Ok(FloatColumn::I16(array))
    } else if let Some(array) = column.as_any().downcast_ref::<Int32Array>() {
        Ok(FloatColumn::I32(array))
    } else if let Some(array) = column.as_any().downcast_ref::<Int64Array>() {
        Ok(FloatColumn::I64(array))
    } else {
        Err(format!(
            "rvbbit.vector_float_agg: column type {:?} is not numeric",
            column.data_type()
        )
        .into())
    }
}

/// Exact counts for any k `(int_col, text_col)` groups.
///
/// This is only used for `GROUP BY ... LIMIT k` with no `ORDER BY`, where SQL
/// does not prescribe which groups are returned. We choose the first k groups
/// encountered in columnar scan order, then keep exact counts for just those
/// groups across the whole table.
#[pg_extern]
fn any_count_int_text(
    rel: pg_sys::Oid,
    int_col: &str,
    text_col: &str,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(group_int, Option<String>),
            name!(group_text, Option<String>),
            name!(count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, Option<String>, i64)>::new().into_iter(),
        ));
    }
    let k = k as usize;
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut groups = Vec::with_capacity(k);

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[int_col, text_col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let int_idx = schema.index_of(int_col)?;
            let text_idx = schema.index_of(text_col)?;
            let int_array = batch.column(int_idx);
            let text_arr = batch
                .column(text_idx)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or_else(|| format!("column '{text_col}' is not utf8"))?;

            if let Some(a) = int_array.as_any().downcast_ref::<Int64Array>() {
                count_first_int_text_groups(&mut groups, k, text_arr, a.len(), |row| {
                    if a.is_null(row) {
                        None
                    } else {
                        Some(a.value(row))
                    }
                });
            } else if let Some(a) = int_array.as_any().downcast_ref::<Int32Array>() {
                count_first_int_text_groups(&mut groups, k, text_arr, a.len(), |row| {
                    if a.is_null(row) {
                        None
                    } else {
                        Some(a.value(row) as i64)
                    }
                });
            } else if let Some(a) = int_array.as_any().downcast_ref::<Int16Array>() {
                count_first_int_text_groups(&mut groups, k, text_arr, a.len(), |row| {
                    if a.is_null(row) {
                        None
                    } else {
                        Some(a.value(row) as i64)
                    }
                });
            } else {
                return Err(format!(
                    "rvbbit.any_count_int_text: column '{}' has unsupported type {:?}",
                    int_col,
                    int_array.data_type()
                )
                .into());
            }
        }
    }

    Ok(TableIterator::new(groups.into_iter().map(|row| {
        (
            row.int_value.map(|v| v.to_string()),
            row.text_value,
            row.count,
        )
    })))
}

/// Exact `COUNT(DISTINCT distinct_col)` over a single group column.
///
/// The group key is returned as text so donor SQL can cast it back to the
/// original type. `distinct_col` currently supports int2/int4/int8 values,
/// which covers ClickBench's `UserID` distinct workloads.
#[pg_extern]
fn top_count_distinct_1col(
    rel: pg_sys::Oid,
    group_col: &str,
    distinct_col: &str,
    skip_empty_group: bool,
    k: i32,
) -> Result<
    TableIterator<'static, (name!(group_value, Option<String>), name!(count, i64))>,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, i64)>::new().into_iter(),
        ));
    }
    let k = k as usize;
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut counts: Option<DistinctCounts> = None;

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[group_col, distinct_col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let group_idx = schema.index_of(group_col)?;
            let distinct_idx = schema.index_of(distinct_col)?;
            let group_array = batch.column(group_idx);
            let distinct_array = batch.column(distinct_idx);

            if let Some(d) = distinct_array.as_any().downcast_ref::<Int64Array>() {
                count_distinct_1col_values(
                    group_col,
                    skip_empty_group,
                    &mut counts,
                    group_array,
                    d.len(),
                    |row| {
                        if d.is_null(row) {
                            None
                        } else {
                            Some(d.value(row))
                        }
                    },
                )?;
            } else if let Some(d) = distinct_array.as_any().downcast_ref::<Int32Array>() {
                count_distinct_1col_values(
                    group_col,
                    skip_empty_group,
                    &mut counts,
                    group_array,
                    d.len(),
                    |row| {
                        if d.is_null(row) {
                            None
                        } else {
                            Some(d.value(row) as i64)
                        }
                    },
                )?;
            } else if let Some(d) = distinct_array.as_any().downcast_ref::<Int16Array>() {
                count_distinct_1col_values(
                    group_col,
                    skip_empty_group,
                    &mut counts,
                    group_array,
                    d.len(),
                    |row| {
                        if d.is_null(row) {
                            None
                        } else {
                            Some(d.value(row) as i64)
                        }
                    },
                )?;
            } else {
                return Err(format!(
                    "rvbbit.top_count_distinct_1col: column '{}' has unsupported distinct type {:?}",
                    distinct_col,
                    distinct_array.data_type()
                )
                .into());
            }
        }
    }

    let mut heap: BinaryHeap<Reverse<DistinctTopRow>> = BinaryHeap::with_capacity(k + 1);
    match counts {
        Some(DistinctCounts::Text(text_counts)) => {
            for (group_id, distincts) in text_counts.counts {
                push_distinct_top_row(
                    &mut heap,
                    DistinctTopRow {
                        count: distincts.len() as i64,
                        group_value: group_id.map(|id| text_counts.interner.owned(id)),
                    },
                    k,
                );
            }
        }
        Some(DistinctCounts::Int(map)) => {
            for (group_value, distincts) in map {
                push_distinct_top_row(
                    &mut heap,
                    DistinctTopRow {
                        count: distincts.len() as i64,
                        group_value: group_value.map(|v| v.to_string()),
                    },
                    k,
                );
            }
        }
        None => {}
    }

    let mut rows: Vec<DistinctTopRow> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.group_value.cmp(&b.group_value))
    });
    Ok(TableIterator::new(
        rows.into_iter().map(|row| (row.group_value, row.count)),
    ))
}

/// Exact `COUNT(DISTINCT distinct_col)` over `(int_col, text_col)` groups.
#[pg_extern]
fn top_count_distinct_int_text(
    rel: pg_sys::Oid,
    int_col: &str,
    text_col: &str,
    distinct_col: &str,
    skip_empty_text: bool,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(group_int, Option<String>),
            name!(group_text, Option<String>),
            name!(count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, Option<String>, i64)>::new().into_iter(),
        ));
    }
    let k = k as usize;
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut interner = TextInterner::default();
    let mut counts: HashMap<(Option<i64>, Option<u32>), HashSet<i64>> = HashMap::default();

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[int_col, text_col, distinct_col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let int_idx = schema.index_of(int_col)?;
            let text_idx = schema.index_of(text_col)?;
            let distinct_idx = schema.index_of(distinct_col)?;
            let int_array = batch.column(int_idx);
            let text_arr = batch
                .column(text_idx)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or_else(|| format!("column '{text_col}' is not utf8"))?;
            let distinct_array = batch.column(distinct_idx);

            if let Some(d) = distinct_array.as_any().downcast_ref::<Int64Array>() {
                count_distinct_int_text_values(
                    &mut counts,
                    &mut interner,
                    int_col,
                    int_array,
                    text_arr,
                    skip_empty_text,
                    d.len(),
                    |row| {
                        if d.is_null(row) {
                            None
                        } else {
                            Some(d.value(row))
                        }
                    },
                )?;
            } else if let Some(d) = distinct_array.as_any().downcast_ref::<Int32Array>() {
                count_distinct_int_text_values(
                    &mut counts,
                    &mut interner,
                    int_col,
                    int_array,
                    text_arr,
                    skip_empty_text,
                    d.len(),
                    |row| {
                        if d.is_null(row) {
                            None
                        } else {
                            Some(d.value(row) as i64)
                        }
                    },
                )?;
            } else if let Some(d) = distinct_array.as_any().downcast_ref::<Int16Array>() {
                count_distinct_int_text_values(
                    &mut counts,
                    &mut interner,
                    int_col,
                    int_array,
                    text_arr,
                    skip_empty_text,
                    d.len(),
                    |row| {
                        if d.is_null(row) {
                            None
                        } else {
                            Some(d.value(row) as i64)
                        }
                    },
                )?;
            } else {
                return Err(format!(
                    "rvbbit.top_count_distinct_int_text: column '{}' has unsupported distinct type {:?}",
                    distinct_col,
                    distinct_array.data_type()
                )
                .into());
            }
        }
    }

    let mut heap: BinaryHeap<Reverse<DistinctTop2Row>> = BinaryHeap::with_capacity(k + 1);
    for ((int_value, text_value), distincts) in counts {
        push_distinct_top2_row(
            &mut heap,
            DistinctTop2Row {
                count: distincts.len() as i64,
                int_value,
                text_value: text_value.map(|id| interner.owned(id)),
            },
            k,
        );
    }

    let mut rows: Vec<DistinctTop2Row> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.int_value.cmp(&b.int_value))
            .then_with(|| a.text_value.cmp(&b.text_value))
    });

    Ok(TableIterator::new(rows.into_iter().map(|row| {
        (
            row.int_value.map(|v| v.to_string()),
            row.text_value,
            row.count,
        )
    })))
}

/// Specialized two-integer-key rollup:
///
///   SELECT key1, key2, COUNT(*), SUM(IsRefresh), AVG(ResolutionWidth)
///   FROM rel [WHERE filter_text <> '']
///   GROUP BY key1, key2 ORDER BY COUNT(*) DESC LIMIT k
///
/// The average is returned as sum/count state so donor SQL can use Postgres'
/// numeric division semantics.
#[pg_extern]
fn top_rollup_2int(
    rel: pg_sys::Oid,
    key1_col: &str,
    key2_col: &str,
    filter_text_col: &str,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(key1, Option<String>),
            name!(key2, Option<String>),
            name!(count, i64),
            name!(sum_refresh, i64),
            name!(sum_width, i64),
            name!(width_count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, Option<String>, i64, i64, i64, i64)>::new().into_iter(),
        ));
    }
    let k = k as usize;
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let has_filter = !filter_text_col.is_empty();
    let projection: Vec<&str> = if has_filter {
        vec![
            key1_col,
            key2_col,
            "IsRefresh",
            "ResolutionWidth",
            filter_text_col,
        ]
    } else {
        vec![key1_col, key2_col, "IsRefresh", "ResolutionWidth"]
    };
    let mut groups: HashMap<(Option<i64>, Option<i64>), Rollup2Agg> = HashMap::default();

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &projection)?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let key1_idx = schema.index_of(key1_col)?;
            let key2_idx = schema.index_of(key2_col)?;
            let refresh_idx = schema.index_of("IsRefresh")?;
            let width_idx = schema.index_of("ResolutionWidth")?;
            let key1_arr = int_array_ref(key1_col, batch.column(key1_idx).as_ref())?;
            let key2_arr = int_array_ref(key2_col, batch.column(key2_idx).as_ref())?;
            let refresh_arr = int_array_ref("IsRefresh", batch.column(refresh_idx).as_ref())?;
            let width_arr = int_array_ref("ResolutionWidth", batch.column(width_idx).as_ref())?;
            let filter_arr = if has_filter {
                let filter_idx = schema.index_of(filter_text_col)?;
                Some(
                    batch
                        .column(filter_idx)
                        .as_any()
                        .downcast_ref::<StringArray>()
                        .ok_or_else(|| format!("column '{filter_text_col}' is not utf8"))?,
                )
            } else {
                None
            };

            for row in 0..batch.num_rows() {
                if let Some(filter_arr) = filter_arr {
                    if filter_arr.is_null(row) || filter_arr.value(row).is_empty() {
                        continue;
                    }
                }
                let entry = groups
                    .entry((key1_arr.value(row), key2_arr.value(row)))
                    .or_insert(Rollup2Agg {
                        count: 0,
                        sum_refresh: 0,
                        sum_width: 0,
                        width_count: 0,
                    });
                entry.count += 1;
                if let Some(value) = refresh_arr.value(row) {
                    entry.sum_refresh += value;
                }
                if let Some(value) = width_arr.value(row) {
                    entry.sum_width += value;
                    entry.width_count += 1;
                }
            }
        }
    }

    let mut heap: BinaryHeap<Reverse<Rollup2TopRow>> = BinaryHeap::with_capacity(k + 1);
    for ((key1, key2), agg) in groups {
        push_rollup2_top_row(
            &mut heap,
            Rollup2TopRow {
                count: agg.count,
                key1,
                key2,
                sum_refresh: agg.sum_refresh,
                sum_width: agg.sum_width,
                width_count: agg.width_count,
            },
            k,
        );
    }

    let mut rows: Vec<Rollup2TopRow> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.key1.cmp(&b.key1))
            .then_with(|| a.key2.cmp(&b.key2))
    });

    Ok(TableIterator::new(rows.into_iter().map(|row| {
        (
            row.key1.map(|v| v.to_string()),
            row.key2.map(|v| v.to_string()),
            row.count,
            row.sum_refresh,
            row.sum_width,
            row.width_count,
        )
    })))
}

/// Specialized one-integer-key rollup with exact distinct state:
///
///   SELECT key, SUM(sum_col), COUNT(*), AVG(avg_col), COUNT(DISTINCT distinct_col)
///   FROM rel GROUP BY key ORDER BY COUNT(*) DESC LIMIT k
///
/// The sum and average are returned as transition state so donor SQL can
/// preserve NULL and numeric division behavior.
#[pg_extern]
fn top_rollup_1int_distinct(
    rel: pg_sys::Oid,
    group_col: &str,
    sum_col: &str,
    avg_col: &str,
    distinct_col: &str,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(group_value, Option<String>),
            name!(sum_value, i64),
            name!(sum_count, i64),
            name!(count, i64),
            name!(avg_sum, i64),
            name!(avg_count, i64),
            name!(distinct_count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, i64, i64, i64, i64, i64, i64)>::new().into_iter(),
        ));
    }
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let rows = vector::topk_group_aggregate(
        paths,
        &[KeySpec::Int {
            col: group_col.to_string(),
        }],
        &[],
        &[
            AggSpec::SumInt {
                col: sum_col.to_string(),
            },
            AggSpec::AvgInt {
                col: avg_col.to_string(),
            },
            AggSpec::CountDistinctInt {
                col: distinct_col.to_string(),
            },
        ],
        k as usize,
    )?;

    Ok(TableIterator::new(rows.into_iter().map(|row| {
        let group_value = match &row.keys[0] {
            KeyValue::Int(value) => *value,
            _ => None,
        };
        let (sum_value, sum_count) = match &row.aggs[0] {
            AggValue::SumInt { sum, non_nulls } => (*sum, *non_nulls),
            _ => (0, 0),
        };
        let (avg_sum, avg_count) = match &row.aggs[1] {
            AggValue::AvgInt { sum, non_nulls } => (*sum, *non_nulls),
            _ => (0, 0),
        };
        let distinct_count = match &row.aggs[2] {
            AggValue::CountDistinctInt { count } => *count,
            _ => 0,
        };
        (
            group_value.map(|v| v.to_string()),
            sum_value,
            sum_count,
            row.count,
            avg_sum,
            avg_count,
            distinct_count,
        )
    })))
}

/// Count rows where a text column contains a fixed substring. This is the
/// exact fast path for LIKE '%needle%' patterns without other wildcards.
#[pg_extern]
fn count_text_contains(
    rel: pg_sys::Oid,
    text_col: &str,
    needle: &str,
) -> Result<i64, Box<dyn std::error::Error>> {
    let filters = vec![FilterSpec::TextContains {
        col: text_col.to_string(),
        needle: needle.to_string(),
    }];
    let paths = lookup_paths_for_oid_pruned(rel.to_u32(), &filters)?;
    vector::count_rows(paths, &filters)
}

/// Top SearchPhrase groups for `URL LIKE '%needle%'`, tracking MIN(URL).
#[pg_extern]
fn top_phrase_min_url_for_url_contains(
    rel: pg_sys::Oid,
    phrase_col: &str,
    url_col: &str,
    needle: &str,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(phrase, Option<String>),
            name!(min_url, Option<String>),
            name!(count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, Option<String>, i64)>::new().into_iter(),
        ));
    }
    let filters = vec![
        FilterSpec::TextNotEmpty {
            col: phrase_col.to_string(),
        },
        FilterSpec::TextContains {
            col: url_col.to_string(),
            needle: needle.to_string(),
        },
    ];
    let paths = lookup_paths_for_oid_pruned(rel.to_u32(), &filters)?;
    let rows = vector::topk_group_aggregate(
        paths,
        &[KeySpec::Text {
            col: phrase_col.to_string(),
        }],
        &filters,
        &[AggSpec::MinText {
            col: url_col.to_string(),
        }],
        k as usize,
    )?;

    Ok(TableIterator::new(rows.into_iter().map(|row| {
        let phrase = match &row.keys[0] {
            KeyValue::Text(value) => value.clone(),
            _ => None,
        };
        let min_url = match &row.aggs[0] {
            AggValue::MinText(value) => value.clone(),
            _ => None,
        };
        (phrase, min_url, row.count)
    })))
}

/// Top SearchPhrase groups for the ClickBench Q22 LIKE/NOT LIKE rollup.
#[pg_extern]
fn top_phrase_url_title_rollup(
    rel: pg_sys::Oid,
    phrase_col: &str,
    url_col: &str,
    title_col: &str,
    distinct_col: &str,
    title_needle: &str,
    url_excluded_needle: &str,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(phrase, Option<String>),
            name!(min_url, Option<String>),
            name!(min_title, Option<String>),
            name!(count, i64),
            name!(distinct_count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, Option<String>, Option<String>, i64, i64)>::new().into_iter(),
        ));
    }
    let filters = vec![
        FilterSpec::TextNotEmpty {
            col: phrase_col.to_string(),
        },
        FilterSpec::TextContains {
            col: title_col.to_string(),
            needle: title_needle.to_string(),
        },
        FilterSpec::TextNotContains {
            col: url_col.to_string(),
            needle: url_excluded_needle.to_string(),
        },
    ];
    let paths = lookup_paths_for_oid_pruned(rel.to_u32(), &filters)?;
    let rows = vector::topk_group_aggregate(
        paths,
        &[KeySpec::Text {
            col: phrase_col.to_string(),
        }],
        &filters,
        &[
            AggSpec::MinText {
                col: url_col.to_string(),
            },
            AggSpec::MinText {
                col: title_col.to_string(),
            },
            AggSpec::CountDistinctInt {
                col: distinct_col.to_string(),
            },
        ],
        k as usize,
    )?;

    Ok(TableIterator::new(rows.into_iter().map(|row| {
        let phrase = match &row.keys[0] {
            KeyValue::Text(value) => value.clone(),
            _ => None,
        };
        let min_url = match &row.aggs[0] {
            AggValue::MinText(value) => value.clone(),
            _ => None,
        };
        let min_title = match &row.aggs[1] {
            AggValue::MinText(value) => value.clone(),
            _ => None,
        };
        let distinct_count = match &row.aggs[2] {
            AggValue::CountDistinctInt { count } => *count,
            _ => 0,
        };
        (phrase, min_url, min_title, row.count, distinct_count)
    })))
}

/// Late-materialized top rows for:
///
///   SELECT * FROM rel
///   WHERE text_col LIKE '%needle%'
///   ORDER BY order_col
///   LIMIT k
///
/// The first pass reads only `text_col` and `order_col`, keeps the best `k`
/// row references, then materializes the winning full rows as JSON. The
/// rewriter casts those JSON fields back to the table's SQL column types.
#[pg_extern]
fn top_rows_text_contains_ordered_json(
    rel: pg_sys::Oid,
    text_col: &str,
    needle: &str,
    order_col: &str,
    k: i32,
) -> Result<TableIterator<'static, (name!(row_json, pgrx::JsonB),)>, Box<dyn std::error::Error>> {
    if k <= 0 {
        return Ok(TableIterator::new(Vec::<(pgrx::JsonB,)>::new().into_iter()));
    }
    let k = k as usize;
    let filters = vec![FilterSpec::TextContains {
        col: text_col.to_string(),
        needle: needle.to_string(),
    }];
    let paths = lookup_paths_for_oid_pruned(rel.to_u32(), &filters)?;
    let projection = if text_col == order_col {
        vec![text_col]
    } else {
        vec![text_col, order_col]
    };
    let projection_refs: Vec<&str> = projection.to_vec();
    let mut heap: BinaryHeap<LateTopRow> = BinaryHeap::with_capacity(k + 1);

    for (path_idx, path) in paths.iter().enumerate() {
        let reader = RowGroupReader::open_projected(path, &projection_refs)?;
        let mut row_base = 0usize;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let text_idx = schema.index_of(text_col)?;
            let order_idx = schema.index_of(order_col)?;
            let text_arr = batch
                .column(text_idx)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or_else(|| format!("column '{text_col}' is not utf8"))?;
            let order_arr = batch.column(order_idx);

            for row in 0..batch.num_rows() {
                if text_arr.is_null(row) || !text_arr.value(row).contains(needle) {
                    continue;
                }
                let candidate = LateTopRow {
                    order: late_order_value(order_arr.as_ref(), row)?,
                    path_idx,
                    row_in_group: row_base + row,
                };
                if heap
                    .peek()
                    .is_some_and(|worst| heap.len() == k && candidate >= *worst)
                {
                    continue;
                }
                if heap.len() == k {
                    heap.pop();
                }
                heap.push(candidate);
            }
            row_base += batch.num_rows();
        }
    }

    let mut winners = heap.into_vec();
    winners.sort_unstable();
    let rows = materialize_late_json_rows(&paths, &winners)?;
    Ok(TableIterator::new(rows.into_iter().map(|row| (row,))))
}

fn materialize_late_json_rows(
    paths: &[PathBuf],
    winners: &[LateTopRow],
) -> Result<Vec<pgrx::JsonB>, Box<dyn std::error::Error>> {
    let mut by_path: BTreeMap<usize, Vec<(usize, usize)>> = BTreeMap::new();
    for (out_idx, winner) in winners.iter().enumerate() {
        by_path
            .entry(winner.path_idx)
            .or_default()
            .push((winner.row_in_group, out_idx));
    }
    let mut out: Vec<Option<pgrx::JsonB>> = (0..winners.len()).map(|_| None).collect();

    for (path_idx, mut wanted) in by_path {
        wanted.sort_unstable_by_key(|(row_in_group, _)| *row_in_group);
        let path = paths
            .get(path_idx)
            .ok_or_else(|| format!("late materialization path index {path_idx} out of range"))?;
        let selected_rows = wanted
            .iter()
            .map(|(row_in_group, _)| *row_in_group)
            .collect::<Vec<_>>();
        let reader = RowGroupReader::open_selected_rows(path, &selected_rows)?;
        let mut wanted_idx = 0usize;
        for batch in reader {
            let batch = batch?;
            for row in 0..batch.num_rows() {
                let Some((_, out_idx)) = wanted.get(wanted_idx).copied() else {
                    break;
                };
                out[out_idx] = Some(pgrx::JsonB(record_batch_row_json(&batch, row)?));
                wanted_idx += 1;
            }
            if wanted_idx >= wanted.len() {
                break;
            }
        }
        if wanted_idx != wanted.len() {
            return Err(format!(
                "late materialization read {} rows from {}, expected {}",
                wanted_idx,
                path.display(),
                wanted.len()
            )
            .into());
        }
    }

    Ok(out
        .into_iter()
        .map(|row| row.unwrap_or_else(|| pgrx::JsonB(JsonValue::Null)))
        .collect())
}

fn late_order_value(
    array: &dyn Array,
    row: usize,
) -> Result<LateOrderValue, Box<dyn std::error::Error>> {
    if array.is_null(row) {
        return Ok(LateOrderValue::Null);
    }
    if let Some(a) = array.as_any().downcast_ref::<BooleanArray>() {
        Ok(LateOrderValue::Bool(a.value(row)))
    } else if let Some(a) = array.as_any().downcast_ref::<Int16Array>() {
        Ok(LateOrderValue::Int(a.value(row) as i64))
    } else if let Some(a) = array.as_any().downcast_ref::<Int32Array>() {
        Ok(LateOrderValue::Int(a.value(row) as i64))
    } else if let Some(a) = array.as_any().downcast_ref::<Int64Array>() {
        Ok(LateOrderValue::Int(a.value(row)))
    } else if let Some(a) = array.as_any().downcast_ref::<TimestampMicrosecondArray>() {
        Ok(LateOrderValue::Int(a.value(row)))
    } else if let Some(a) = array.as_any().downcast_ref::<Date32Array>() {
        Ok(LateOrderValue::Int(a.value(row) as i64))
    } else if let Some(a) = array.as_any().downcast_ref::<Float32Array>() {
        Ok(LateOrderValue::Float(a.value(row) as f64))
    } else if let Some(a) = array.as_any().downcast_ref::<Float64Array>() {
        Ok(LateOrderValue::Float(a.value(row)))
    } else if let Some(a) = array.as_any().downcast_ref::<StringArray>() {
        Ok(LateOrderValue::Text(a.value(row).to_string()))
    } else {
        Err(format!("unsupported ORDER BY arrow type {:?}", array.data_type()).into())
    }
}

fn record_batch_row_json(
    batch: &arrow::record_batch::RecordBatch,
    row: usize,
) -> Result<JsonValue, Box<dyn std::error::Error>> {
    let mut object = JsonMap::new();
    let schema = batch.schema();
    for (idx, field) in schema.fields().iter().enumerate() {
        object.insert(
            field.name().clone(),
            arrow_value_json(batch.column(idx).as_ref(), row)?,
        );
    }
    Ok(JsonValue::Object(object))
}

fn arrow_value_json(
    array: &dyn Array,
    row: usize,
) -> Result<JsonValue, Box<dyn std::error::Error>> {
    if array.is_null(row) {
        return Ok(JsonValue::Null);
    }
    match array.data_type() {
        DataType::Boolean => {
            let a = array.as_any().downcast_ref::<BooleanArray>().unwrap();
            Ok(JsonValue::Bool(a.value(row)))
        }
        DataType::Int16 => {
            let a = array.as_any().downcast_ref::<Int16Array>().unwrap();
            Ok(JsonValue::Number(JsonNumber::from(a.value(row))))
        }
        DataType::Int32 => {
            let a = array.as_any().downcast_ref::<Int32Array>().unwrap();
            Ok(JsonValue::Number(JsonNumber::from(a.value(row))))
        }
        DataType::Int64 | DataType::Timestamp(_, _) => {
            if let Some(a) = array.as_any().downcast_ref::<Int64Array>() {
                Ok(JsonValue::Number(JsonNumber::from(a.value(row))))
            } else {
                let a = array
                    .as_any()
                    .downcast_ref::<TimestampMicrosecondArray>()
                    .unwrap();
                Ok(JsonValue::Number(JsonNumber::from(a.value(row))))
            }
        }
        DataType::Date32 => {
            let a = array.as_any().downcast_ref::<Date32Array>().unwrap();
            Ok(JsonValue::Number(JsonNumber::from(a.value(row))))
        }
        DataType::Float32 => {
            let a = array.as_any().downcast_ref::<Float32Array>().unwrap();
            Ok(JsonNumber::from_f64(a.value(row) as f64)
                .map(JsonValue::Number)
                .unwrap_or(JsonValue::Null))
        }
        DataType::Float64 => {
            let a = array.as_any().downcast_ref::<Float64Array>().unwrap();
            Ok(JsonNumber::from_f64(a.value(row))
                .map(JsonValue::Number)
                .unwrap_or(JsonValue::Null))
        }
        DataType::Utf8 => {
            let a = array.as_any().downcast_ref::<StringArray>().unwrap();
            Ok(JsonValue::String(a.value(row).to_string()))
        }
        DataType::Binary => {
            let a = array.as_any().downcast_ref::<BinaryArray>().unwrap();
            Ok(JsonValue::String(hex_encode(a.value(row))))
        }
        other => Err(format!("unsupported arrow JSON type {other:?}").into()),
    }
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

/// Specialized `AVG(length(text_col))` by one int4 group column, with a
/// `HAVING count(*) > min_count` gate and top-k ordering by the average.
///
/// This keeps the ClickBench Q27 shape in the columnar path: only the group
/// int column and URL text column are read, Postgres receives only the final
/// qualifying groups, and the donor query computes the visible numeric avg.
#[pg_extern]
fn top_avg_len_by_int_col(
    rel: pg_sys::Oid,
    group_col: &str,
    text_col: &str,
    min_count: i64,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(group_value, Option<i32>),
            name!(sum_len, i64),
            name!(count, i64),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<i32>, i64, i64)>::new().into_iter(),
        ));
    }
    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut groups: HashMap<Option<i32>, UrlLenAgg> = HashMap::default();

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[group_col, text_col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let group_idx = schema.index_of(group_col)?;
            let text_idx = schema.index_of(text_col)?;
            let group_arr = batch
                .column(group_idx)
                .as_any()
                .downcast_ref::<Int32Array>()
                .ok_or_else(|| format!("column '{group_col}' is not int4"))?;
            let text_arr = batch
                .column(text_idx)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or_else(|| format!("column '{text_col}' is not utf8"))?;

            for row in 0..text_arr.len() {
                if text_arr.is_null(row) {
                    continue;
                }
                let value = text_arr.value(row);
                if value.is_empty() {
                    continue;
                }
                let group_value = if group_arr.is_null(row) {
                    None
                } else {
                    Some(group_arr.value(row))
                };
                let entry = groups.entry(group_value).or_insert(UrlLenAgg {
                    sum_len: 0,
                    count: 0,
                });
                entry.sum_len += value.chars().count() as i64;
                entry.count += 1;
            }
        }
    }

    let mut rows: Vec<UrlLenTopRow> = groups
        .into_iter()
        .filter_map(|(group_value, agg)| {
            if agg.count > min_count {
                Some(UrlLenTopRow {
                    group_value,
                    sum_len: agg.sum_len,
                    count: agg.count,
                })
            } else {
                None
            }
        })
        .collect();
    rows.sort_unstable_by(|a, b| {
        ((b.sum_len as i128) * (a.count as i128))
            .cmp(&((a.sum_len as i128) * (b.count as i128)))
            .then_with(|| a.group_value.cmp(&b.group_value))
    });
    rows.truncate(k as usize);

    Ok(TableIterator::new(
        rows.into_iter()
            .map(|row| (row.group_value, row.sum_len, row.count)),
    ))
}

/// Generic projected rollup for deterministic text transforms:
///
/// SELECT transform(text_col), AVG(length(text_col)), COUNT(*), MIN(text_col)
/// FROM rel WHERE text_col <> ''
/// GROUP BY 1 HAVING COUNT(*) > min_count
/// ORDER BY AVG(length(text_col)) DESC LIMIT k
///
/// The first transform is `regex_replace_url_host`, matching the common
/// `regexp_replace(url, '^https?://(?:www\.)?([^/]+)/.*$', '\1')` idiom.
#[pg_extern]
fn top_text_transform_avg_len(
    rel: pg_sys::Oid,
    text_col: &str,
    transform: &str,
    min_count: i64,
    k: i32,
) -> Result<
    TableIterator<
        'static,
        (
            name!(key, Option<String>),
            name!(sum_len, i64),
            name!(count, i64),
            name!(min_text, Option<String>),
        ),
    >,
    Box<dyn std::error::Error>,
> {
    if k <= 0 {
        return Ok(TableIterator::new(
            Vec::<(Option<String>, i64, i64, Option<String>)>::new().into_iter(),
        ));
    }
    if transform != "regex_replace_url_host" {
        return Err(format!("unsupported text transform '{transform}'").into());
    }

    let paths = lookup_paths_for_oid(rel.to_u32())?;
    let mut interner = TextInterner::default();
    let mut groups: HashMap<Option<u32>, TextTransformLenAgg> = HashMap::default();

    for path in paths {
        let reader = RowGroupReader::open_projected(&path, &[text_col])?;
        for batch in reader {
            let batch = batch?;
            let schema = batch.schema();
            let text_idx = schema.index_of(text_col)?;
            let text_arr = batch
                .column(text_idx)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or_else(|| format!("column '{text_col}' is not utf8"))?;

            for row in 0..text_arr.len() {
                if text_arr.is_null(row) {
                    continue;
                }
                let value = text_arr.value(row);
                if value.is_empty() {
                    continue;
                }
                let key = Some(interner.intern(regex_replace_url_host(value)));
                let entry = groups.entry(key).or_insert(TextTransformLenAgg {
                    sum_len: 0,
                    count: 0,
                    min_text: None,
                });
                entry.sum_len += pg_text_len(value);
                entry.count += 1;
                update_min_string(&mut entry.min_text, value);
            }
        }
    }

    let mut rows: Vec<TextTransformLenTopRow> = groups
        .into_iter()
        .filter_map(|(key, agg)| {
            if agg.count > min_count {
                Some(TextTransformLenTopRow {
                    key,
                    sum_len: agg.sum_len,
                    count: agg.count,
                    min_text: agg.min_text,
                })
            } else {
                None
            }
        })
        .collect();
    rows.sort_unstable_by(|a, b| {
        ((b.sum_len as i128) * (a.count as i128))
            .cmp(&((a.sum_len as i128) * (b.count as i128)))
            .then_with(|| a.key.cmp(&b.key))
    });
    rows.truncate(k as usize);

    Ok(TableIterator::new(rows.into_iter().map(move |row| {
        (
            row.key.map(|key| interner.owned(key)),
            row.sum_len,
            row.count,
            row.min_text,
        )
    })))
}

fn pg_text_len(value: &str) -> i64 {
    if value.is_ascii() {
        value.len() as i64
    } else {
        value.chars().count() as i64
    }
}

fn regex_replace_url_host(value: &str) -> &str {
    let rest = if let Some(rest) = value.strip_prefix("http://") {
        rest
    } else if let Some(rest) = value.strip_prefix("https://") {
        rest
    } else {
        return value;
    };
    let rest = rest.strip_prefix("www.").unwrap_or(rest);
    match rest.find('/') {
        Some(idx) => &rest[..idx],
        None => value,
    }
}

fn update_min_string(slot: &mut Option<String>, value: &str) {
    if slot
        .as_deref()
        .map(|current| value < current)
        .unwrap_or(true)
    {
        *slot = Some(value.to_string());
    }
}

/// Used by `top_count_1col`'s int branches. Takes the Arrow values slice
/// directly with a no-nulls fast path.
fn count_i64_values_arr<T, F>(
    col: &str,
    skip_empty: bool,
    counts: &mut Option<Counts>,
    values: &[T],
    is_null: F,
    no_nulls: bool,
) -> Result<(), Box<dyn std::error::Error>>
where
    T: Copy + Into<i64>,
    F: Fn(usize) -> bool,
{
    if counts.is_none() {
        *counts = Some(Counts::Int(HashMap::default(), 0));
    }
    let Some(Counts::Int(map, null_count)) = counts.as_mut() else {
        return Err(format!("rvbbit.top_count_1col: mixed column type for '{col}'").into());
    };
    if no_nulls {
        for &v in values.iter() {
            *map.entry(v.into()).or_insert(0) += 1;
        }
    } else {
        for (row, &v) in values.iter().enumerate() {
            if is_null(row) {
                if !skip_empty {
                    *null_count += 1;
                }
            } else {
                *map.entry(v.into()).or_insert(0) += 1;
            }
        }
    }
    Ok(())
}

fn count_int_text_values<F>(
    counts: &mut HashMap<(Option<i64>, Option<u32>), i64>,
    interner: &mut TextInterner,
    text_arr: &StringArray,
    skip_empty_text: bool,
    len: usize,
    mut int_at: F,
) where
    F: FnMut(usize) -> Option<i64>,
{
    for row in 0..len {
        let text_value = if text_arr.is_null(row) {
            if skip_empty_text {
                continue;
            }
            None
        } else {
            let value = text_arr.value(row);
            if skip_empty_text && value.is_empty() {
                continue;
            }
            Some(interner.intern(value))
        };
        *counts.entry((int_at(row), text_value)).or_insert(0) += 1;
    }
}

fn count_int_text_values_owned<F>(
    counts: &mut HashMap<(Option<i64>, Option<String>), i64>,
    text_arr: &StringArray,
    skip_empty_text: bool,
    len: usize,
    mut int_at: F,
) where
    F: FnMut(usize) -> Option<i64>,
{
    for row in 0..len {
        let text_value = if text_arr.is_null(row) {
            if skip_empty_text {
                continue;
            }
            None
        } else {
            let value = text_arr.value(row);
            if skip_empty_text && value.is_empty() {
                continue;
            }
            Some(value.to_string())
        };
        *counts.entry((int_at(row), text_value)).or_insert(0) += 1;
    }
}

fn count_first_int_text_groups<F>(
    groups: &mut Vec<FirstPairGroup>,
    k: usize,
    text_arr: &StringArray,
    len: usize,
    mut int_at: F,
) where
    F: FnMut(usize) -> Option<i64>,
{
    for row in 0..len {
        let int_value = int_at(row);
        let text_value = if text_arr.is_null(row) {
            None
        } else {
            Some(text_arr.value(row))
        };
        if let Some(group) = groups
            .iter_mut()
            .find(|group| group.int_value == int_value && opt_str_eq(&group.text_value, text_value))
        {
            group.count += 1;
        } else if groups.len() < k {
            groups.push(FirstPairGroup {
                int_value,
                text_value: text_value.map(ToOwned::to_owned),
                count: 1,
            });
        }
    }
}

fn opt_str_eq(left: &Option<String>, right: Option<&str>) -> bool {
    match (left.as_deref(), right) {
        (Some(a), Some(b)) => a == b,
        (None, None) => true,
        _ => false,
    }
}

fn int_array_ref<'a>(
    col: &str,
    array: &'a dyn Array,
) -> Result<IntArrayRef<'a>, Box<dyn std::error::Error>> {
    if let Some(array) = array.as_any().downcast_ref::<Int16Array>() {
        Ok(IntArrayRef::I16(array))
    } else if let Some(array) = array.as_any().downcast_ref::<Int32Array>() {
        Ok(IntArrayRef::I32(array))
    } else if let Some(array) = array.as_any().downcast_ref::<Int64Array>() {
        Ok(IntArrayRef::I64(array))
    } else {
        Err(format!(
            "rvbbit: column '{}' is not an integer column (got {:?})",
            col,
            array.data_type()
        )
        .into())
    }
}

fn count_distinct_1col_values<F>(
    group_col: &str,
    skip_empty_group: bool,
    counts: &mut Option<DistinctCounts>,
    group_array: &dyn Array,
    len: usize,
    mut distinct_at: F,
) -> Result<(), Box<dyn std::error::Error>>
where
    F: FnMut(usize) -> Option<i64>,
{
    if let Some(group_arr) = group_array.as_any().downcast_ref::<StringArray>() {
        if counts.is_none() {
            *counts = Some(DistinctCounts::Text(TextDistinctCounts::default()));
        }
        let Some(DistinctCounts::Text(text_counts)) = counts.as_mut() else {
            return Err(format!(
                "rvbbit.top_count_distinct_1col: mixed group type for '{group_col}'"
            )
            .into());
        };
        for row in 0..len {
            let Some(distinct_value) = distinct_at(row) else {
                continue;
            };
            let group_value = if group_arr.is_null(row) {
                if skip_empty_group {
                    continue;
                }
                None
            } else {
                let value = group_arr.value(row);
                if skip_empty_group && value.is_empty() {
                    continue;
                }
                Some(text_counts.interner.intern(value))
            };
            text_counts
                .counts
                .entry(group_value)
                .or_default()
                .insert(distinct_value);
        }
    } else if let Some(group_arr) = group_array.as_any().downcast_ref::<Int64Array>() {
        count_distinct_int_group_values(counts, group_col, len, distinct_at, |row| {
            if group_arr.is_null(row) {
                None
            } else {
                Some(group_arr.value(row))
            }
        })?;
    } else if let Some(group_arr) = group_array.as_any().downcast_ref::<Int32Array>() {
        count_distinct_int_group_values(counts, group_col, len, distinct_at, |row| {
            if group_arr.is_null(row) {
                None
            } else {
                Some(group_arr.value(row) as i64)
            }
        })?;
    } else if let Some(group_arr) = group_array.as_any().downcast_ref::<Int16Array>() {
        count_distinct_int_group_values(counts, group_col, len, distinct_at, |row| {
            if group_arr.is_null(row) {
                None
            } else {
                Some(group_arr.value(row) as i64)
            }
        })?;
    } else {
        return Err(format!(
            "rvbbit.top_count_distinct_1col: column '{}' has unsupported group type {:?}",
            group_col,
            group_array.data_type()
        )
        .into());
    }
    Ok(())
}

fn count_distinct_int_group_values<D, G>(
    counts: &mut Option<DistinctCounts>,
    group_col: &str,
    len: usize,
    mut distinct_at: D,
    mut group_at: G,
) -> Result<(), Box<dyn std::error::Error>>
where
    D: FnMut(usize) -> Option<i64>,
    G: FnMut(usize) -> Option<i64>,
{
    if counts.is_none() {
        *counts = Some(DistinctCounts::Int(HashMap::default()));
    }
    let Some(DistinctCounts::Int(map)) = counts.as_mut() else {
        return Err(
            format!("rvbbit.top_count_distinct_1col: mixed group type for '{group_col}'").into(),
        );
    };
    for row in 0..len {
        let Some(distinct_value) = distinct_at(row) else {
            continue;
        };
        map.entry(group_at(row)).or_default().insert(distinct_value);
    }
    Ok(())
}

fn count_distinct_int_text_values<F>(
    counts: &mut HashMap<(Option<i64>, Option<u32>), HashSet<i64>>,
    interner: &mut TextInterner,
    int_col: &str,
    int_array: &dyn Array,
    text_arr: &StringArray,
    skip_empty_text: bool,
    len: usize,
    distinct_at: F,
) -> Result<(), Box<dyn std::error::Error>>
where
    F: FnMut(usize) -> Option<i64>,
{
    if let Some(int_arr) = int_array.as_any().downcast_ref::<Int64Array>() {
        count_distinct_int_text_values_typed(
            counts,
            interner,
            text_arr,
            skip_empty_text,
            len,
            distinct_at,
            |row| {
                if int_arr.is_null(row) {
                    None
                } else {
                    Some(int_arr.value(row))
                }
            },
        );
    } else if let Some(int_arr) = int_array.as_any().downcast_ref::<Int32Array>() {
        count_distinct_int_text_values_typed(
            counts,
            interner,
            text_arr,
            skip_empty_text,
            len,
            distinct_at,
            |row| {
                if int_arr.is_null(row) {
                    None
                } else {
                    Some(int_arr.value(row) as i64)
                }
            },
        );
    } else if let Some(int_arr) = int_array.as_any().downcast_ref::<Int16Array>() {
        count_distinct_int_text_values_typed(
            counts,
            interner,
            text_arr,
            skip_empty_text,
            len,
            distinct_at,
            |row| {
                if int_arr.is_null(row) {
                    None
                } else {
                    Some(int_arr.value(row) as i64)
                }
            },
        );
    } else {
        return Err(format!(
            "rvbbit.top_count_distinct_int_text: column '{}' has unsupported int type {:?}",
            int_col,
            int_array.data_type()
        )
        .into());
    }
    Ok(())
}

fn count_distinct_int_text_values_typed<D, I>(
    counts: &mut HashMap<(Option<i64>, Option<u32>), HashSet<i64>>,
    interner: &mut TextInterner,
    text_arr: &StringArray,
    skip_empty_text: bool,
    len: usize,
    mut distinct_at: D,
    mut int_at: I,
) where
    D: FnMut(usize) -> Option<i64>,
    I: FnMut(usize) -> Option<i64>,
{
    for row in 0..len {
        let Some(distinct_value) = distinct_at(row) else {
            continue;
        };
        let text_value = if text_arr.is_null(row) {
            if skip_empty_text {
                continue;
            }
            None
        } else {
            let value = text_arr.value(row);
            if skip_empty_text && value.is_empty() {
                continue;
            }
            Some(interner.intern(value))
        };
        counts
            .entry((int_at(row), text_value))
            .or_default()
            .insert(distinct_value);
    }
}

fn group_count_cache_key(
    rel_oid: u32,
    group_col: &str,
) -> Result<GroupCountCacheKey, Box<dyn std::error::Error>> {
    let sql = format!(
        "SELECT count(*)::bigint, \
                COALESCE(max(rg_id), -1)::bigint, \
                COALESCE(sum(n_rows), 0)::bigint \
         FROM rvbbit.row_groups_visible \
         WHERE table_oid = {rel_oid}::oid"
    );
    let mut key = None;
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let row_group_count: Option<i64> = row.get(1)?;
            let max_rg_id: Option<i64> = row.get(2)?;
            let total_rows: Option<i64> = row.get(3)?;
            key = Some(GroupCountCacheKey {
                rel_oid,
                group_col: group_col.to_string(),
                row_group_count: row_group_count.unwrap_or(0),
                max_rg_id: max_rg_id.unwrap_or(-1),
                total_rows: total_rows.unwrap_or(0),
            });
        }
        Ok(())
    })?;
    key.ok_or_else(|| "no row-group signature returned".into())
}

fn json_group_value_to_text(value: &serde_json::Value) -> Option<String> {
    match value {
        serde_json::Value::Null => None,
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Bool(v) => Some(v.to_string()),
        serde_json::Value::Number(v) => Some(v.to_string()),
        other => Some(other.to_string()),
    }
}

// --- Metadata-only ----------------------------------------------------------

#[pg_extern]
fn rg_n_rows(rel: pg_sys::Oid, rg_id: i64) -> Result<i64, Box<dyn std::error::Error>> {
    let path = lookup_path(rel, rg_id)?;
    Ok(RowGroupReader::info(&path)?.n_rows)
}

#[pg_extern]
fn rg_n_bytes(rel: pg_sys::Oid, rg_id: i64) -> Result<i64, Box<dyn std::error::Error>> {
    let path = lookup_path(rel, rg_id)?;
    Ok(RowGroupReader::info(&path)?.n_bytes)
}

#[pg_extern]
fn rg_n_columns(rel: pg_sys::Oid, rg_id: i64) -> Result<i32, Box<dyn std::error::Error>> {
    let path = lookup_path(rel, rg_id)?;
    Ok(RowGroupReader::info(&path)?.n_columns)
}

// --- Full scan (materializes all columns) -----------------------------------

#[pg_extern]
fn rg_count(rel: pg_sys::Oid, rg_id: i64) -> Result<i64, Box<dyn std::error::Error>> {
    let path = lookup_path(rel, rg_id)?;
    let reader = RowGroupReader::open(&path)?;
    let mut total: i64 = 0;
    for batch in reader {
        total += batch?.num_rows() as i64;
    }
    Ok(total)
}

// --- Projected scan (reads only the named column) ---------------------------

#[pg_extern]
fn rg_count_projected(
    rel: pg_sys::Oid,
    rg_id: i64,
    col: &str,
) -> Result<i64, Box<dyn std::error::Error>> {
    let path = lookup_path(rel, rg_id)?;
    let reader = RowGroupReader::open_projected(&path, &[col])?;
    let mut total: i64 = 0;
    for batch in reader {
        total += batch?.num_rows() as i64;
    }
    Ok(total)
}

// --- Group-by demos (SETOF returning) ---------------------------------------

/// Group-by on a low-cardinality string column. Reads ONLY that column.
/// Equivalent to `SELECT col, count(*) FROM rel GROUP BY col` on heap.
#[pg_extern]
fn rg_count_by_string(
    rel: pg_sys::Oid,
    rg_id: i64,
    col: &str,
) -> Result<TableIterator<'static, (name!(value, String), name!(n, i64))>, Box<dyn std::error::Error>>
{
    let path = lookup_path(rel, rg_id)?;
    let reader = RowGroupReader::open_projected(&path, &[col])?;
    let mut counts: HashMap<String, i64> = HashMap::default();
    for batch in reader {
        let batch = batch?;
        let arr = batch
            .column(0)
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or_else(|| format!("column '{col}' is not utf8"))?;
        for i in 0..arr.len() {
            if arr.is_null(i) {
                continue;
            }
            *counts.entry(arr.value(i).to_string()).or_insert(0) += 1;
        }
    }
    let mut rows: Vec<(String, i64)> = counts.into_iter().collect();
    rows.sort_by(|a, b| b.1.cmp(&a.1));
    Ok(TableIterator::new(rows.into_iter()))
}

/// The JSON-without-detoast demo. Reads ONLY the `response` text column
/// (which our Phase 2a writer stored as plain UTF-8, no TOAST involved),
/// parses each row's JSON, and counts by `stop_reason`. Heap's equivalent
/// `SELECT response->>'stop_reason', count(*) FROM rel GROUP BY 1` pays
/// TOAST detoast + JSONB binary parse on every row.
#[pg_extern]
fn rg_count_stop_reasons(
    rel: pg_sys::Oid,
    rg_id: i64,
) -> Result<
    TableIterator<'static, (name!(stop_reason, String), name!(n, i64))>,
    Box<dyn std::error::Error>,
> {
    let path = lookup_path(rel, rg_id)?;
    let reader = RowGroupReader::open_projected(&path, &["response"])?;
    let mut counts: HashMap<String, i64> = HashMap::default();
    for batch in reader {
        let batch = batch?;
        let arr = batch
            .column(0)
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or("response column is not utf8")?;
        for i in 0..arr.len() {
            if arr.is_null(i) {
                continue;
            }
            // serde_json::from_str is fast enough for a demo; a real version
            // would skip parsing and pattern-match on the substring since
            // stop_reason is always near the top of the document.
            let s = arr.value(i);
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(s) {
                if let Some(reason) = v.get("stop_reason").and_then(|r| r.as_str()) {
                    *counts.entry(reason.to_string()).or_insert(0) += 1;
                }
            }
        }
    }
    let mut rows: Vec<(String, i64)> = counts.into_iter().collect();
    rows.sort_by(|a, b| b.1.cmp(&a.1));
    Ok(TableIterator::new(rows.into_iter()))
}

// ---------------------------------------------------------------------------
// Stats-derived aggregates — answer single-aggregate queries from
// rvbbit.row_groups.stats without scanning a single row.
//
// The row-group writer computes min / max / sum / null_count for every
// numeric column at compact time and stores them as jsonb. These
// functions sum / min / max across all row groups of a table and
// return microsecond-latency aggregates for any unfiltered aggregate.
//
// Future work (tracked as RYR-273): a planner hook that auto-rewrites
//   SELECT avg(fare_amount) FROM trips
// into the equivalent rvbbit.agg_avg('trips', 'fare_amount') call.
// For now users invoke these helpers directly.
// ---------------------------------------------------------------------------

/// Total rows across all row groups. Equivalent to `SELECT count(*) FROM rel`
/// when the table is fully compacted (no heap-catcher rows).
#[pg_extern]
fn agg_count(rel: pg_sys::Oid) -> Result<i64, Box<dyn std::error::Error>> {
    let rel_oid = rel.to_u32();
    let n: Option<i64> = Spi::get_one(&format!(
        "SELECT sum(n_rows)::bigint FROM rvbbit.row_groups_visible WHERE table_oid = {rel_oid}::oid"
    ))?;
    Ok(n.unwrap_or(0))
}

/// Non-null count of `col`. Equivalent to `SELECT count(col) FROM rel`.
#[pg_extern]
fn agg_count_nonnull(rel: pg_sys::Oid, col: &str) -> Result<i64, Box<dyn std::error::Error>> {
    let rel_oid = rel.to_u32();
    let col_esc = col.replace('\'', "''");
    let n: Option<i64> = Spi::get_one(&format!(
        "SELECT sum( \
             n_rows - COALESCE(((\
                 SELECT (s->>'null_count')::bigint \
                 FROM jsonb_array_elements(stats) AS s \
                 WHERE s->>'name' = '{col_esc}'\
             )), 0) \
         )::bigint \
         FROM rvbbit.row_groups_visible WHERE table_oid = {rel_oid}::oid"
    ))?;
    Ok(n.unwrap_or(0))
}

/// Min of `col` across all row groups. Returns the JSON value (could be
/// int, float, bool, etc.). Caller casts to the appropriate type.
#[pg_extern]
fn agg_min(rel: pg_sys::Oid, col: &str) -> Result<Option<pgrx::JsonB>, Box<dyn std::error::Error>> {
    let rel_oid = rel.to_u32();
    let col_esc = col.replace('\'', "''");
    // jsonb_path_query_first to grab one value, then min across all
    // per-row-group mins. Done in SQL so we never touch parquet.
    let sql = format!(
        "WITH vals AS ( \
             SELECT (s->'min') AS v \
             FROM rvbbit.row_groups_visible, jsonb_array_elements(stats) AS s \
             WHERE table_oid = {rel_oid}::oid AND s->>'name' = '{col_esc}' \
                   AND s->'min' IS NOT NULL AND jsonb_typeof(s->'min') <> 'null' \
         ) \
         SELECT to_jsonb(min((v#>>'{{}}')::numeric)) FROM vals"
    );
    let result: Option<pgrx::JsonB> = Spi::get_one(&sql)?;
    Ok(result)
}

/// Max of `col` across all row groups.
#[pg_extern]
fn agg_max(rel: pg_sys::Oid, col: &str) -> Result<Option<pgrx::JsonB>, Box<dyn std::error::Error>> {
    let rel_oid = rel.to_u32();
    let col_esc = col.replace('\'', "''");
    let sql = format!(
        "WITH vals AS ( \
             SELECT (s->'max') AS v \
             FROM rvbbit.row_groups_visible, jsonb_array_elements(stats) AS s \
             WHERE table_oid = {rel_oid}::oid AND s->>'name' = '{col_esc}' \
                   AND s->'max' IS NOT NULL AND jsonb_typeof(s->'max') <> 'null' \
         ) \
         SELECT to_jsonb(max((v#>>'{{}}')::numeric)) FROM vals"
    );
    let result: Option<pgrx::JsonB> = Spi::get_one(&sql)?;
    Ok(result)
}

/// Sum of `col` across all row groups. Numeric columns only.
#[pg_extern]
fn agg_sum(rel: pg_sys::Oid, col: &str) -> Result<Option<f64>, Box<dyn std::error::Error>> {
    let scan = scan_numeric_sum_count(rel.to_u32(), col)?;
    if scan.count_nonnull == 0 {
        Ok(None)
    } else {
        Ok(Some(scan.sum_f64))
    }
}

/// Avg of `col` — sum / count(non-null). Pure SQL composition.
#[pg_extern]
fn agg_avg(rel: pg_sys::Oid, col: &str) -> Result<Option<f64>, Box<dyn std::error::Error>> {
    let scan = scan_numeric_sum_count(rel.to_u32(), col)?;
    if scan.count_nonnull == 0 {
        return Ok(None);
    }
    Ok(Some(scan.sum_f64 / (scan.count_nonnull as f64)))
}

// ---------------------------------------------------------------------------
// GROUP BY pushdown helpers — answer single-column GROUP BY queries from
// per-group stats in row_groups. Each row group contributes per-bucket
// {count, sum_other_col, count_nonnull_other_col} blocks; we sum across
// row groups in SQL to get the full table aggregate per group.
//
// The group column must be low-cardinality (< 256 distinct values at
// compact time) or the compactor will have skipped per-group stats and
// these helpers return zero rows.
// ---------------------------------------------------------------------------

/// SELECT group_col, count(*) FROM rel GROUP BY group_col
///
/// Returns (group_value::text, count::bigint). The text cast lets the
/// helper be type-polymorphic — caller casts back to the original
/// column type as needed.
#[pg_extern]
fn agg_groupby_count(
    rel: pg_sys::Oid,
    group_col: &str,
) -> Result<
    TableIterator<'static, (name!(group_value, Option<String>), name!(count, i64))>,
    Box<dyn std::error::Error>,
> {
    let counts = group_count_map(rel.to_u32(), group_col)?;
    let mut out: Vec<(Option<String>, i64)> = counts.into_iter().collect();
    out.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(TableIterator::new(out.into_iter()))
}

/// SELECT group_col, sum(agg_col) FROM rel GROUP BY group_col
#[pg_extern]
fn agg_groupby_sum(
    rel: pg_sys::Oid,
    group_col: &str,
    agg_col: &str,
) -> Result<
    TableIterator<'static, (name!(group_value, Option<String>), name!(sum, f64))>,
    Box<dyn std::error::Error>,
> {
    let rel_oid = rel.to_u32();
    let g_esc = group_col.replace('\'', "''");
    let a_esc = agg_col.replace('\'', "''");
    let sql = format!(
        "SELECT \
             COALESCE(b->>'value', NULL) AS gv, \
             sum(((b->'agg'->'{a_esc}'->>'sum')::float8))::float8 AS s \
         FROM rvbbit.row_groups_visible, \
              jsonb_array_elements(per_group_stats) AS blk, \
              jsonb_array_elements(blk->'groups') AS b \
         WHERE table_oid = {rel_oid}::oid \
           AND blk->>'group_column' = '{g_esc}' \
           AND (b->'agg'->'{a_esc}') IS NOT NULL \
         GROUP BY 1 \
         ORDER BY 1"
    );
    let mut out: Vec<(Option<String>, f64)> = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let v: Option<String> = row.get(1)?;
            let s: Option<f64> = row.get(2)?;
            out.push((v, s.unwrap_or(0.0)));
        }
        Ok(())
    })?;
    Ok(TableIterator::new(out.into_iter()))
}

/// SELECT group_col, avg(agg_col) FROM rel GROUP BY group_col
#[pg_extern]
fn agg_groupby_avg(
    rel: pg_sys::Oid,
    group_col: &str,
    agg_col: &str,
) -> Result<
    TableIterator<'static, (name!(group_value, Option<String>), name!(avg, Option<f64>))>,
    Box<dyn std::error::Error>,
> {
    let rel_oid = rel.to_u32();
    let g_esc = group_col.replace('\'', "''");
    let a_esc = agg_col.replace('\'', "''");
    let sql = format!(
        "SELECT \
             COALESCE(b->>'value', NULL) AS gv, \
             CASE WHEN sum(((b->'agg'->'{a_esc}'->>'count_nonnull')::bigint)) > 0 \
                  THEN sum(((b->'agg'->'{a_esc}'->>'sum')::float8)) \
                       / sum(((b->'agg'->'{a_esc}'->>'count_nonnull')::bigint))::float8 \
                  ELSE NULL END AS a \
         FROM rvbbit.row_groups_visible, \
              jsonb_array_elements(per_group_stats) AS blk, \
              jsonb_array_elements(blk->'groups') AS b \
         WHERE table_oid = {rel_oid}::oid \
           AND blk->>'group_column' = '{g_esc}' \
           AND (b->'agg'->'{a_esc}') IS NOT NULL \
         GROUP BY 1 \
         ORDER BY 1"
    );
    let mut out: Vec<(Option<String>, Option<f64>)> = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let v: Option<String> = row.get(1)?;
            let a: Option<f64> = row.get(2)?;
            out.push((v, a));
        }
        Ok(())
    })?;
    Ok(TableIterator::new(out.into_iter()))
}

#[pg_extern]
fn rg_sum_int(rel: pg_sys::Oid, rg_id: i64, col: &str) -> Result<i64, Box<dyn std::error::Error>> {
    let path = lookup_path(rel, rg_id)?;
    let reader = RowGroupReader::open_projected(&path, &[col])?;
    let mut total: i64 = 0;
    for batch in reader {
        let batch = batch?;
        let array = batch.column(0);
        if let Some(a) = array.as_any().downcast_ref::<Int32Array>() {
            for i in 0..a.len() {
                if !a.is_null(i) {
                    total += a.value(i) as i64;
                }
            }
        } else if let Some(a) = array.as_any().downcast_ref::<Int64Array>() {
            for i in 0..a.len() {
                if !a.is_null(i) {
                    total += a.value(i);
                }
            }
        } else {
            return Err(format!(
                "rvbbit.rg_sum_int: column '{}' is not int4/int8 (got {:?})",
                col,
                array.data_type()
            )
            .into());
        }
    }
    Ok(total)
}
