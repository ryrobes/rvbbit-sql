//! Reusable vector aggregation kernels over projected parquet batches.
//!
//! The rewriter can still recognize SQL shapes, but the execution side should
//! be a small set of composable columnar operators: projected scan, vector
//! filters, derived/group keys, aggregate state updates, and top-k emission.

use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use arrow::array::{
    Array, Date32Array, Int16Array, Int32Array, Int64Array, StringArray, TimestampMicrosecondArray,
};
use arrow::record_batch::RecordBatch;
use roaring::RoaringBitmap;
use rvbbit_storage::row_group::RowGroupReader;

use crate::columnar_cache;
use crate::fast_hash::{FastHashMap as HashMap, FastHashSet as HashSet};

#[derive(Clone)]
pub(crate) enum KeySpec {
    Int { col: String },
    Date { col: String },
    Text { col: String },
    TimestampMinute { col: String },
    TimestampTruncMinute { col: String },
}

#[derive(Clone)]
pub(crate) enum FilterSpec {
    TextNotEmpty { col: String },
    TextContains { col: String, needle: String },
    TextNotContains { col: String, needle: String },
    IntEq { col: String, value: i64 },
    IntNe { col: String, value: i64 },
    IntGe { col: String, value: i64 },
    IntLe { col: String, value: i64 },
    IntIn { col: String, values: Vec<i64> },
}

#[derive(Clone)]
pub(crate) enum AggSpec {
    SumInt { col: String },
    AvgInt { col: String },
    CountDistinctInt { col: String },
    MinText { col: String },
}

#[derive(Clone, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
pub(crate) enum KeyValue {
    Int(Option<i64>),
    Date(Option<i32>),
    Minute(Option<i32>),
    Timestamp(Option<i64>),
    Text(Option<String>),
}

#[derive(Clone, Debug)]
pub(crate) enum AggValue {
    SumInt { sum: i64, non_nulls: i64 },
    AvgInt { sum: i64, non_nulls: i64 },
    CountDistinctInt { count: i64 },
    MinText(Option<String>),
}

#[derive(Clone, Debug)]
pub(crate) struct GroupRow {
    pub keys: Vec<KeyValue>,
    pub count: i64,
    pub aggs: Vec<AggValue>,
}

pub(crate) fn key_value_to_text(value: &KeyValue) -> Option<String> {
    match value {
        KeyValue::Int(value) => value.map(|value| value.to_string()),
        KeyValue::Date(value) => value.map(format_date32),
        KeyValue::Minute(value) => value.map(|value| value.to_string()),
        KeyValue::Timestamp(value) => value.map(format_timestamp_micros),
        KeyValue::Text(value) => value.clone(),
    }
}

#[derive(Eq, PartialEq, Ord, PartialOrd)]
struct TopRow {
    count: i64,
    key: GroupKey,
    group_id: usize,
}

#[derive(Eq, PartialEq, Ord, PartialOrd)]
struct FastTopRow {
    count: i64,
    key: GroupKey,
}

struct GroupState {
    key: GroupKey,
    count: i64,
    aggs: Vec<AggState>,
}

enum AggState {
    SumInt { sum: i64, non_nulls: i64 },
    AvgInt { sum: i64, non_nulls: i64 },
    CountDistinctInt(HashSet<i64>),
    MinText(Option<String>),
}

struct IntRollupAgg {
    count: i64,
    sum_value: i64,
    sum_count: i64,
    avg_sum: i64,
    avg_count: i64,
    distincts: HashSet<i64>,
}

struct TextMinAgg {
    count: i64,
    min_text: Option<String>,
}

struct TextRollupAgg {
    count: i64,
    min_first: Option<String>,
    min_second: Option<String>,
    distincts: HashSet<i64>,
}

#[derive(Clone, Copy)]
enum BatchColumn<'a> {
    Int(IntArrayRef<'a>),
    Text(&'a StringArray),
    Timestamp(&'a TimestampMicrosecondArray),
}

#[derive(Clone, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
enum GroupKey {
    One(KeyValue),
    Two(KeyValue, KeyValue),
    Three(KeyValue, KeyValue, KeyValue),
    Many(Vec<KeyValue>),
}

struct VectorPlan {
    projection: Vec<String>,
    keys: Vec<KeyPlan>,
    filters: Vec<FilterPlan>,
    aggs: Vec<AggPlan>,
}

enum KeyPlan {
    Int(usize),
    Date(usize),
    Text(usize),
    TimestampMinute(usize),
    TimestampTruncMinute(usize),
}

enum FilterPlan {
    TextNotEmpty { idx: usize },
    TextContains { idx: usize, needle: String },
    TextNotContains { idx: usize, needle: String },
    IntEq { idx: usize, value: i64 },
    IntNe { idx: usize, value: i64 },
    IntGe { idx: usize, value: i64 },
    IntLe { idx: usize, value: i64 },
    IntIn { idx: usize, values: Vec<i64> },
}

enum AggPlan {
    SumInt { idx: usize },
    AvgInt { idx: usize },
    CountDistinctInt { idx: usize },
    MinText { idx: usize },
}

struct BatchPlan<'a> {
    keys: Vec<BatchKey<'a>>,
    filters: Vec<BatchFilter<'a>>,
    aggs: Vec<BatchAgg<'a>>,
}

enum BatchKey<'a> {
    Int(IntArrayRef<'a>),
    Date(IntArrayRef<'a>),
    Text(&'a StringArray),
    TimestampMinute(&'a TimestampMicrosecondArray),
    TimestampTruncMinute(&'a TimestampMicrosecondArray),
}

enum BatchFilter<'a> {
    TextNotEmpty {
        array: &'a StringArray,
    },
    TextContains {
        array: &'a StringArray,
        needle: String,
    },
    TextNotContains {
        array: &'a StringArray,
        needle: String,
    },
    IntEq {
        array: IntArrayRef<'a>,
        value: i64,
    },
    IntNe {
        array: IntArrayRef<'a>,
        value: i64,
    },
    IntGe {
        array: IntArrayRef<'a>,
        value: i64,
    },
    IntLe {
        array: IntArrayRef<'a>,
        value: i64,
    },
    IntIn {
        array: IntArrayRef<'a>,
        values: Vec<i64>,
    },
}

enum BatchAgg<'a> {
    SumInt(IntArrayRef<'a>),
    AvgInt(IntArrayRef<'a>),
    CountDistinctInt(IntArrayRef<'a>),
    MinText(&'a StringArray),
}

impl GroupKey {
    fn into_values(self) -> Vec<KeyValue> {
        match self {
            Self::One(a) => vec![a],
            Self::Two(a, b) => vec![a, b],
            Self::Three(a, b, c) => vec![a, b, c],
            Self::Many(values) => values,
        }
    }
}

#[derive(Clone, Copy)]
enum IntArrayRef<'a> {
    Date32(&'a Date32Array),
    I16(&'a Int16Array),
    I32(&'a Int32Array),
    I64(&'a Int64Array),
}

impl IntArrayRef<'_> {
    fn value(&self, row: usize) -> Option<i64> {
        match self {
            Self::Date32(array) => {
                if array.is_null(row) {
                    None
                } else {
                    Some(array.value(row) as i64)
                }
            }
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

pub(crate) fn count_rows(
    paths: Vec<PathBuf>,
    filters: &[FilterSpec],
) -> Result<i64, Box<dyn std::error::Error>> {
    if let Some(count) = try_count_fast_path(&paths, filters)? {
        return Ok(count);
    }

    let plan = compile_plan(&[], filters, &[])?;
    let projection_refs: Vec<&str> = plan.projection.iter().map(String::as_str).collect();
    let mut count = 0i64;

    for path in paths {
        for batch in read_projected_batches(&path, &projection_refs)? {
            let columns = batch_columns(&batch, &plan.projection)?;
            let batch_plan = batch_plan(&plan, &columns)?;
            for row in 0..batch.num_rows() {
                if filters_match(row, &batch_plan.filters) {
                    count += 1;
                }
            }
        }
    }

    Ok(count)
}

pub(crate) fn topk_group_aggregate(
    paths: Vec<PathBuf>,
    keys: &[KeySpec],
    filters: &[FilterSpec],
    aggs: &[AggSpec],
    k: usize,
) -> Result<Vec<GroupRow>, Box<dyn std::error::Error>> {
    if k == 0 {
        return Ok(Vec::new());
    }
    if let Some(rows) = try_topk_fast_path(&paths, keys, filters, aggs, k)? {
        return Ok(rows);
    }

    let plan = compile_plan(keys, filters, aggs)?;
    let projection_refs: Vec<&str> = plan.projection.iter().map(String::as_str).collect();
    let mut states = Vec::<GroupState>::new();
    let mut group_ids = HashMap::<GroupKey, usize>::default();

    for path in paths {
        for batch in read_projected_batches(&path, &projection_refs)? {
            let columns = batch_columns(&batch, &plan.projection)?;
            let batch_plan = batch_plan(&plan, &columns)?;
            for row in 0..batch.num_rows() {
                if !filters_match(row, &batch_plan.filters) {
                    continue;
                }
                update_group_state_for_row(
                    row,
                    &batch_plan,
                    &plan.aggs,
                    &mut states,
                    &mut group_ids,
                );
            }
        }
    }

    Ok(finish_topk_group_rows(&states, k))
}

pub(crate) fn topk_group_aggregate_with_row_bitmaps(
    paths: Vec<(PathBuf, RoaringBitmap)>,
    keys: &[KeySpec],
    filters: &[FilterSpec],
    aggs: &[AggSpec],
    k: usize,
) -> Result<Vec<GroupRow>, Box<dyn std::error::Error>> {
    if k == 0 {
        return Ok(Vec::new());
    }

    let plan = compile_plan(keys, filters, aggs)?;
    let projection_refs: Vec<&str> = plan.projection.iter().map(String::as_str).collect();
    let mut states = Vec::<GroupState>::new();
    let mut group_ids = HashMap::<GroupKey, usize>::default();

    for (path, bitmap) in paths {
        if bitmap.is_empty() {
            continue;
        }
        for batch in read_projected_selected_batches(&path, &projection_refs, &bitmap)? {
            let columns = batch_columns(&batch, &plan.projection)?;
            let batch_plan = batch_plan(&plan, &columns)?;
            for row in 0..batch.num_rows() {
                if !filters_match(row, &batch_plan.filters) {
                    continue;
                }
                update_group_state_for_row(
                    row,
                    &batch_plan,
                    &plan.aggs,
                    &mut states,
                    &mut group_ids,
                );
            }
        }
    }

    Ok(finish_topk_group_rows(&states, k))
}

fn update_group_state_for_row(
    row: usize,
    batch_plan: &BatchPlan<'_>,
    aggs: &[AggPlan],
    states: &mut Vec<GroupState>,
    group_ids: &mut HashMap<GroupKey, usize>,
) {
    let key = key_for_row(row, &batch_plan.keys);
    let group_id = match group_ids.get(&key) {
        Some(id) => *id,
        None => {
            let id = states.len();
            group_ids.insert(key.clone(), id);
            states.push(GroupState {
                key,
                count: 0,
                aggs: aggs.iter().map(AggState::new).collect(),
            });
            id
        }
    };
    let state = &mut states[group_id];
    state.count += 1;
    for (slot, spec) in state.aggs.iter_mut().zip(&batch_plan.aggs) {
        slot.update(row, spec);
    }
}

fn finish_topk_group_rows(states: &[GroupState], k: usize) -> Vec<GroupRow> {
    let mut heap = BinaryHeap::<Reverse<TopRow>>::with_capacity(k + 1);
    for (group_id, state) in states.iter().enumerate() {
        let row = TopRow {
            count: state.count,
            key: state.key.clone(),
            group_id,
        };
        if heap.len() == k && heap.peek().map(|worst| row <= worst.0).unwrap_or(false) {
            continue;
        }
        if heap.len() == k {
            heap.pop();
        }
        heap.push(Reverse(row));
    }

    let mut top_rows: Vec<TopRow> = heap.into_iter().map(|Reverse(row)| row).collect();
    top_rows.sort_unstable_by(|a, b| b.count.cmp(&a.count).then_with(|| a.key.cmp(&b.key)));

    top_rows
        .into_iter()
        .map(|top| {
            let state = states
                .get(top.group_id)
                .expect("rvbbit vector aggregate top row points at state");
            GroupRow {
                keys: state.key.clone().into_values(),
                count: state.count,
                aggs: state.aggs.iter().map(AggState::finish).collect(),
            }
        })
        .collect()
}

fn try_count_fast_path(
    paths: &[PathBuf],
    filters: &[FilterSpec],
) -> Result<Option<i64>, Box<dyn std::error::Error>> {
    match filters {
        [FilterSpec::TextNotEmpty { col }] => Ok(Some(count_text_filter(
            paths,
            col,
            FastTextFilter::NotEmpty,
        )?)),
        [FilterSpec::TextContains { col, needle }] => Ok(Some(count_text_filter(
            paths,
            col,
            FastTextFilter::Contains(needle),
        )?)),
        [FilterSpec::TextNotContains { col, needle }] => Ok(Some(count_text_filter(
            paths,
            col,
            FastTextFilter::NotContains(needle),
        )?)),
        _ => Ok(None),
    }
}

fn try_topk_fast_path(
    paths: &[PathBuf],
    keys: &[KeySpec],
    filters: &[FilterSpec],
    aggs: &[AggSpec],
    k: usize,
) -> Result<Option<Vec<GroupRow>>, Box<dyn std::error::Error>> {
    match (keys, filters, aggs) {
        (
            [KeySpec::Int { col: int_col }, KeySpec::TimestampMinute { col: ts_col }, KeySpec::Text { col: text_col }],
            [],
            [],
        ) => Ok(Some(topk_count_int_minute_text_fast(
            paths, int_col, ts_col, text_col, k,
        )?)),
        (
            [KeySpec::Int { col: group_col }],
            [],
            [AggSpec::SumInt { col: sum_col }, AggSpec::AvgInt { col: avg_col }, AggSpec::CountDistinctInt { col: distinct_col }],
        ) => Ok(Some(topk_int_rollup_fast(
            paths,
            group_col,
            sum_col,
            avg_col,
            distinct_col,
            k,
        )?)),
        (
            [KeySpec::Text { col: key_col }],
            [FilterSpec::TextNotEmpty { col: not_empty_col }, FilterSpec::TextContains {
                col: filter_col,
                needle,
            }],
            [AggSpec::MinText { col: min_col }],
        ) if not_empty_col == key_col => Ok(Some(topk_text_min_contains_fast(
            paths, key_col, filter_col, needle, min_col, k,
        )?)),
        (
            [KeySpec::Text { col: key_col }],
            [FilterSpec::TextNotEmpty { col: not_empty_col }, FilterSpec::TextContains {
                col: contains_col,
                needle: contains_needle,
            }, FilterSpec::TextNotContains {
                col: not_contains_col,
                needle: not_contains_needle,
            }],
            [AggSpec::MinText { col: min_first_col }, AggSpec::MinText {
                col: min_second_col,
            }, AggSpec::CountDistinctInt { col: distinct_col }],
        ) if not_empty_col == key_col => Ok(Some(topk_text_rollup_fast(
            paths,
            key_col,
            contains_col,
            contains_needle,
            not_contains_col,
            not_contains_needle,
            min_first_col,
            min_second_col,
            distinct_col,
            k,
        )?)),
        _ => Ok(None),
    }
}

enum FastTextFilter<'a> {
    NotEmpty,
    Contains(&'a str),
    NotContains(&'a str),
}

fn count_text_filter(
    paths: &[PathBuf],
    col: &str,
    filter: FastTextFilter<'_>,
) -> Result<i64, Box<dyn std::error::Error>> {
    let mut count = 0i64;
    for path in paths {
        for batch in read_projected_batches(path, &[col])? {
            let array = projected_text_array(&batch, col)?;
            for row in 0..array.len() {
                if array.is_null(row) {
                    continue;
                }
                let value = array.value(row);
                let matched = match filter {
                    FastTextFilter::NotEmpty => !value.is_empty(),
                    FastTextFilter::Contains(needle) => value.contains(needle),
                    FastTextFilter::NotContains(needle) => !value.contains(needle),
                };
                if matched {
                    count += 1;
                }
            }
        }
    }
    Ok(count)
}

fn topk_count_int_minute_text_fast(
    paths: &[PathBuf],
    int_col: &str,
    ts_col: &str,
    text_col: &str,
    k: usize,
) -> Result<Vec<GroupRow>, Box<dyn std::error::Error>> {
    let projection = projection_from_cols(&[int_col, ts_col, text_col]);
    let projection_refs: Vec<&str> = projection.iter().map(String::as_str).collect();
    let mut counts = HashMap::<(Option<i64>, Option<i32>, Option<String>), i64>::default();

    for path in paths {
        for batch in read_projected_batches(path, &projection_refs)? {
            let int_arr = projected_int_array(&batch, int_col)?;
            let ts_arr = projected_timestamp_array(&batch, ts_col)?;
            let text_arr = projected_text_array(&batch, text_col)?;

            for row in 0..batch.num_rows() {
                let int_value = int_arr.value(row);
                let minute = if ts_arr.is_null(row) {
                    None
                } else {
                    Some(timestamp_minute(ts_arr.value(row)))
                };
                let text = if text_arr.is_null(row) {
                    None
                } else {
                    Some(text_arr.value(row).to_string())
                };
                *counts.entry((int_value, minute, text)).or_insert(0) += 1;
            }
        }
    }

    let mut heap = BinaryHeap::<Reverse<FastTopRow>>::with_capacity(k + 1);
    for ((int_value, minute, text), count) in counts {
        push_fast_top_row(
            &mut heap,
            FastTopRow {
                count,
                key: GroupKey::Three(
                    KeyValue::Int(int_value),
                    KeyValue::Minute(minute),
                    KeyValue::Text(text),
                ),
            },
            k,
        );
    }
    Ok(fast_count_rows_from_heap(heap))
}

fn topk_int_rollup_fast(
    paths: &[PathBuf],
    group_col: &str,
    sum_col: &str,
    avg_col: &str,
    distinct_col: &str,
    k: usize,
) -> Result<Vec<GroupRow>, Box<dyn std::error::Error>> {
    let projection = projection_from_cols(&[group_col, sum_col, avg_col, distinct_col]);
    let projection_refs: Vec<&str> = projection.iter().map(String::as_str).collect();
    let mut groups = HashMap::<Option<i64>, IntRollupAgg>::default();

    for path in paths {
        for batch in read_projected_batches(path, &projection_refs)? {
            let group_arr = projected_int_array(&batch, group_col)?;
            let sum_arr = projected_int_array(&batch, sum_col)?;
            let avg_arr = projected_int_array(&batch, avg_col)?;
            let distinct_arr = projected_int_array(&batch, distinct_col)?;

            for row in 0..batch.num_rows() {
                let entry = groups
                    .entry(group_arr.value(row))
                    .or_insert_with(|| IntRollupAgg {
                        count: 0,
                        sum_value: 0,
                        sum_count: 0,
                        avg_sum: 0,
                        avg_count: 0,
                        distincts: HashSet::default(),
                    });
                entry.count += 1;
                if let Some(value) = sum_arr.value(row) {
                    entry.sum_value += value;
                    entry.sum_count += 1;
                }
                if let Some(value) = avg_arr.value(row) {
                    entry.avg_sum += value;
                    entry.avg_count += 1;
                }
                if let Some(value) = distinct_arr.value(row) {
                    entry.distincts.insert(value);
                }
            }
        }
    }

    let mut rows: Vec<(Option<i64>, IntRollupAgg)> = groups.into_iter().collect();
    rows.sort_unstable_by(|a, b| b.1.count.cmp(&a.1.count).then_with(|| a.0.cmp(&b.0)));
    rows.truncate(k);
    Ok(rows
        .into_iter()
        .map(|(group_value, agg)| GroupRow {
            keys: vec![KeyValue::Int(group_value)],
            count: agg.count,
            aggs: vec![
                AggValue::SumInt {
                    sum: agg.sum_value,
                    non_nulls: agg.sum_count,
                },
                AggValue::AvgInt {
                    sum: agg.avg_sum,
                    non_nulls: agg.avg_count,
                },
                AggValue::CountDistinctInt {
                    count: agg.distincts.len() as i64,
                },
            ],
        })
        .collect())
}

fn topk_text_min_contains_fast(
    paths: &[PathBuf],
    key_col: &str,
    filter_col: &str,
    needle: &str,
    min_col: &str,
    k: usize,
) -> Result<Vec<GroupRow>, Box<dyn std::error::Error>> {
    let projection = projection_from_cols(&[key_col, filter_col, min_col]);
    let projection_refs: Vec<&str> = projection.iter().map(String::as_str).collect();
    let mut groups = HashMap::<String, TextMinAgg>::default();

    for path in paths {
        for batch in read_projected_batches(path, &projection_refs)? {
            let key_arr = projected_text_array(&batch, key_col)?;
            let filter_arr = projected_text_array(&batch, filter_col)?;
            let min_arr = projected_text_array(&batch, min_col)?;

            for row in 0..batch.num_rows() {
                if key_arr.is_null(row) {
                    continue;
                }
                let key = key_arr.value(row);
                if key.is_empty()
                    || filter_arr.is_null(row)
                    || !filter_arr.value(row).contains(needle)
                {
                    continue;
                }
                let entry = groups.entry(key.to_string()).or_insert(TextMinAgg {
                    count: 0,
                    min_text: None,
                });
                entry.count += 1;
                if !min_arr.is_null(row) {
                    update_min_string(&mut entry.min_text, min_arr.value(row));
                }
            }
        }
    }

    let mut rows: Vec<(String, TextMinAgg)> = groups.into_iter().collect();
    rows.sort_unstable_by(|a, b| b.1.count.cmp(&a.1.count).then_with(|| a.0.cmp(&b.0)));
    rows.truncate(k);
    Ok(rows
        .into_iter()
        .map(|(key, agg)| GroupRow {
            keys: vec![KeyValue::Text(Some(key))],
            count: agg.count,
            aggs: vec![AggValue::MinText(agg.min_text)],
        })
        .collect())
}

#[allow(clippy::too_many_arguments)]
fn topk_text_rollup_fast(
    paths: &[PathBuf],
    key_col: &str,
    contains_col: &str,
    contains_needle: &str,
    not_contains_col: &str,
    not_contains_needle: &str,
    min_first_col: &str,
    min_second_col: &str,
    distinct_col: &str,
    k: usize,
) -> Result<Vec<GroupRow>, Box<dyn std::error::Error>> {
    let projection = projection_from_cols(&[
        key_col,
        contains_col,
        not_contains_col,
        min_first_col,
        min_second_col,
        distinct_col,
    ]);
    let projection_refs: Vec<&str> = projection.iter().map(String::as_str).collect();
    let mut groups = HashMap::<String, TextRollupAgg>::default();

    for path in paths {
        for batch in read_projected_batches(path, &projection_refs)? {
            let key_arr = projected_text_array(&batch, key_col)?;
            let contains_arr = projected_text_array(&batch, contains_col)?;
            let not_contains_arr = projected_text_array(&batch, not_contains_col)?;
            let min_first_arr = projected_text_array(&batch, min_first_col)?;
            let min_second_arr = projected_text_array(&batch, min_second_col)?;
            let distinct_arr = projected_int_array(&batch, distinct_col)?;

            for row in 0..batch.num_rows() {
                if key_arr.is_null(row) {
                    continue;
                }
                let key = key_arr.value(row);
                if key.is_empty()
                    || contains_arr.is_null(row)
                    || !contains_arr.value(row).contains(contains_needle)
                    || not_contains_arr.is_null(row)
                    || not_contains_arr.value(row).contains(not_contains_needle)
                {
                    continue;
                }
                let entry = groups
                    .entry(key.to_string())
                    .or_insert_with(|| TextRollupAgg {
                        count: 0,
                        min_first: None,
                        min_second: None,
                        distincts: HashSet::default(),
                    });
                entry.count += 1;
                if !min_first_arr.is_null(row) {
                    update_min_string(&mut entry.min_first, min_first_arr.value(row));
                }
                if !min_second_arr.is_null(row) {
                    update_min_string(&mut entry.min_second, min_second_arr.value(row));
                }
                if let Some(value) = distinct_arr.value(row) {
                    entry.distincts.insert(value);
                }
            }
        }
    }

    let mut rows: Vec<(String, TextRollupAgg)> = groups.into_iter().collect();
    rows.sort_unstable_by(|a, b| b.1.count.cmp(&a.1.count).then_with(|| a.0.cmp(&b.0)));
    rows.truncate(k);
    Ok(rows
        .into_iter()
        .map(|(key, agg)| GroupRow {
            keys: vec![KeyValue::Text(Some(key))],
            count: agg.count,
            aggs: vec![
                AggValue::MinText(agg.min_first),
                AggValue::MinText(agg.min_second),
                AggValue::CountDistinctInt {
                    count: agg.distincts.len() as i64,
                },
            ],
        })
        .collect())
}

fn push_fast_top_row(heap: &mut BinaryHeap<Reverse<FastTopRow>>, row: FastTopRow, k: usize) {
    if heap.len() == k && heap.peek().map(|worst| row <= worst.0).unwrap_or(false) {
        return;
    }
    if heap.len() == k {
        heap.pop();
    }
    heap.push(Reverse(row));
}

fn fast_count_rows_from_heap(heap: BinaryHeap<Reverse<FastTopRow>>) -> Vec<GroupRow> {
    let mut rows: Vec<FastTopRow> = heap.into_iter().map(|Reverse(row)| row).collect();
    rows.sort_unstable_by(|a, b| b.count.cmp(&a.count).then_with(|| a.key.cmp(&b.key)));
    rows.into_iter()
        .map(|row| GroupRow {
            keys: row.key.into_values(),
            count: row.count,
            aggs: Vec::new(),
        })
        .collect()
}

impl AggState {
    fn new(spec: &AggPlan) -> Self {
        match spec {
            AggPlan::SumInt { .. } => Self::SumInt {
                sum: 0,
                non_nulls: 0,
            },
            AggPlan::AvgInt { .. } => Self::AvgInt {
                sum: 0,
                non_nulls: 0,
            },
            AggPlan::CountDistinctInt { .. } => Self::CountDistinctInt(HashSet::default()),
            AggPlan::MinText { .. } => Self::MinText(None),
        }
    }

    fn update(&mut self, row: usize, spec: &BatchAgg<'_>) {
        match (self, spec) {
            (Self::SumInt { sum, non_nulls }, BatchAgg::SumInt(array))
            | (Self::AvgInt { sum, non_nulls }, BatchAgg::AvgInt(array)) => {
                if let Some(value) = array.value(row) {
                    *sum += value;
                    *non_nulls += 1;
                }
            }
            (Self::CountDistinctInt(values), BatchAgg::CountDistinctInt(array)) => {
                if let Some(value) = array.value(row) {
                    values.insert(value);
                }
            }
            (Self::MinText(min), BatchAgg::MinText(array)) => {
                if !array.is_null(row) {
                    let value = array.value(row);
                    update_min_string(min, value);
                }
            }
            _ => unreachable!("rvbbit vector aggregate internal agg mismatch"),
        }
    }

    fn finish(&self) -> AggValue {
        match self {
            Self::SumInt { sum, non_nulls } => AggValue::SumInt {
                sum: *sum,
                non_nulls: *non_nulls,
            },
            Self::AvgInt { sum, non_nulls } => AggValue::AvgInt {
                sum: *sum,
                non_nulls: *non_nulls,
            },
            Self::CountDistinctInt(values) => AggValue::CountDistinctInt {
                count: values.len() as i64,
            },
            Self::MinText(value) => AggValue::MinText(value.clone()),
        }
    }
}

fn projection_for(keys: &[KeySpec], filters: &[FilterSpec], aggs: &[AggSpec]) -> Vec<String> {
    let mut cols = Vec::<String>::new();
    for key in keys {
        push_unique(
            &mut cols,
            match key {
                KeySpec::Int { col }
                | KeySpec::Date { col }
                | KeySpec::Text { col }
                | KeySpec::TimestampMinute { col }
                | KeySpec::TimestampTruncMinute { col } => col,
            },
        );
    }
    for filter in filters {
        push_unique(
            &mut cols,
            match filter {
                FilterSpec::TextNotEmpty { col }
                | FilterSpec::TextContains { col, .. }
                | FilterSpec::TextNotContains { col, .. }
                | FilterSpec::IntEq { col, .. }
                | FilterSpec::IntNe { col, .. }
                | FilterSpec::IntGe { col, .. }
                | FilterSpec::IntLe { col, .. }
                | FilterSpec::IntIn { col, .. } => col,
            },
        );
    }
    for agg in aggs {
        push_unique(
            &mut cols,
            match agg {
                AggSpec::SumInt { col }
                | AggSpec::AvgInt { col }
                | AggSpec::CountDistinctInt { col }
                | AggSpec::MinText { col } => col,
            },
        );
    }
    cols
}

fn projection_from_cols(cols: &[&str]) -> Vec<String> {
    let mut projection = Vec::new();
    for col in cols {
        push_unique(&mut projection, col);
    }
    projection
}

pub(crate) fn read_projected_batches(
    path: &Path,
    columns: &[&str],
) -> Result<Vec<RecordBatch>, Box<dyn std::error::Error>> {
    let key = projected_batch_cache_key(path, columns)?;
    if let Some(batches) = columnar_cache::get::<Vec<RecordBatch>>("projected_batches", &key) {
        return Ok((*batches).clone());
    }

    let mut batches = Vec::new();
    let reader = RowGroupReader::open_projected(path, columns)?;
    for batch in reader {
        batches.push(batch?);
    }

    let bytes = record_batches_memory_size(&batches);
    let cached = columnar_cache::put("projected_batches", key, bytes, batches);
    Ok((*cached).clone())
}

fn read_projected_selected_batches(
    path: &Path,
    columns: &[&str],
    bitmap: &RoaringBitmap,
) -> Result<Vec<RecordBatch>, Box<dyn std::error::Error>> {
    let key = projected_selected_batch_cache_key(path, columns, bitmap)?;
    if let Some(batches) = columnar_cache::get::<Vec<RecordBatch>>("selected_batches", &key) {
        return Ok((*batches).clone());
    }

    let rows = bitmap.iter().map(|row| row as usize).collect::<Vec<_>>();
    let reader = RowGroupReader::open_projected_selected_rows(path, columns, &rows)?;
    let mut batches = Vec::new();
    for batch in reader {
        batches.push(batch?);
    }

    let bytes = record_batches_memory_size(&batches);
    let cached = columnar_cache::put("selected_batches", key, bytes, batches);
    Ok((*cached).clone())
}

fn projected_batch_cache_key(
    path: &Path,
    columns: &[&str],
) -> Result<String, Box<dyn std::error::Error>> {
    let metadata = std::fs::metadata(path)?;
    let modified_nanos = metadata
        .modified()
        .ok()
        .and_then(|modified| modified.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    Ok(format!(
        "path={}|cols={}|len={}|mtime={}",
        path.to_string_lossy(),
        columns.join("\u{1f}"),
        metadata.len(),
        modified_nanos,
    ))
}

fn projected_selected_batch_cache_key(
    path: &Path,
    columns: &[&str],
    bitmap: &RoaringBitmap,
) -> Result<String, Box<dyn std::error::Error>> {
    let base = projected_batch_cache_key(path, columns)?;
    let mut hasher = blake3::Hasher::new();
    hasher.update(&bitmap.len().to_le_bytes());
    for row in bitmap.iter() {
        hasher.update(&row.to_le_bytes());
    }
    Ok(format!("{base}|rows={}", hasher.finalize().to_hex()))
}

fn record_batches_memory_size(batches: &[RecordBatch]) -> usize {
    batches
        .iter()
        .map(RecordBatch::get_array_memory_size)
        .sum::<usize>()
}

fn compile_plan(
    keys: &[KeySpec],
    filters: &[FilterSpec],
    aggs: &[AggSpec],
) -> Result<VectorPlan, Box<dyn std::error::Error>> {
    let projection = projection_for(keys, filters, aggs);
    let compiled_keys = keys
        .iter()
        .map(|key| match key {
            KeySpec::Int { col } => Ok(KeyPlan::Int(projection_index(&projection, col)?)),
            KeySpec::Date { col } => Ok(KeyPlan::Date(projection_index(&projection, col)?)),
            KeySpec::Text { col } => Ok(KeyPlan::Text(projection_index(&projection, col)?)),
            KeySpec::TimestampMinute { col } => Ok(KeyPlan::TimestampMinute(projection_index(
                &projection,
                col,
            )?)),
            KeySpec::TimestampTruncMinute { col } => Ok(KeyPlan::TimestampTruncMinute(
                projection_index(&projection, col)?,
            )),
        })
        .collect::<Result<Vec<_>, Box<dyn std::error::Error>>>()?;
    let compiled_filters = filters
        .iter()
        .map(|filter| match filter {
            FilterSpec::TextNotEmpty { col } => Ok(FilterPlan::TextNotEmpty {
                idx: projection_index(&projection, col)?,
            }),
            FilterSpec::TextContains { col, needle } => Ok(FilterPlan::TextContains {
                idx: projection_index(&projection, col)?,
                needle: needle.clone(),
            }),
            FilterSpec::TextNotContains { col, needle } => Ok(FilterPlan::TextNotContains {
                idx: projection_index(&projection, col)?,
                needle: needle.clone(),
            }),
            FilterSpec::IntEq { col, value } => Ok(FilterPlan::IntEq {
                idx: projection_index(&projection, col)?,
                value: *value,
            }),
            FilterSpec::IntNe { col, value } => Ok(FilterPlan::IntNe {
                idx: projection_index(&projection, col)?,
                value: *value,
            }),
            FilterSpec::IntGe { col, value } => Ok(FilterPlan::IntGe {
                idx: projection_index(&projection, col)?,
                value: *value,
            }),
            FilterSpec::IntLe { col, value } => Ok(FilterPlan::IntLe {
                idx: projection_index(&projection, col)?,
                value: *value,
            }),
            FilterSpec::IntIn { col, values } => Ok(FilterPlan::IntIn {
                idx: projection_index(&projection, col)?,
                values: values.clone(),
            }),
        })
        .collect::<Result<Vec<_>, Box<dyn std::error::Error>>>()?;
    let compiled_aggs = aggs
        .iter()
        .map(|agg| match agg {
            AggSpec::SumInt { col } => Ok(AggPlan::SumInt {
                idx: projection_index(&projection, col)?,
            }),
            AggSpec::AvgInt { col } => Ok(AggPlan::AvgInt {
                idx: projection_index(&projection, col)?,
            }),
            AggSpec::CountDistinctInt { col } => Ok(AggPlan::CountDistinctInt {
                idx: projection_index(&projection, col)?,
            }),
            AggSpec::MinText { col } => Ok(AggPlan::MinText {
                idx: projection_index(&projection, col)?,
            }),
        })
        .collect::<Result<Vec<_>, Box<dyn std::error::Error>>>()?;

    Ok(VectorPlan {
        projection,
        keys: compiled_keys,
        filters: compiled_filters,
        aggs: compiled_aggs,
    })
}

fn projection_index(projection: &[String], col: &str) -> Result<usize, Box<dyn std::error::Error>> {
    projection
        .iter()
        .position(|candidate| candidate == col)
        .ok_or_else(|| format!("rvbbit vector aggregate: missing projected column '{col}'").into())
}

fn push_unique(cols: &mut Vec<String>, col: &str) {
    if !cols.iter().any(|existing| existing == col) {
        cols.push(col.to_string());
    }
}

fn batch_columns<'a>(
    batch: &'a RecordBatch,
    projection: &[String],
) -> Result<Vec<BatchColumn<'a>>, Box<dyn std::error::Error>> {
    let mut columns = Vec::with_capacity(projection.len());
    let schema = batch.schema();
    for col in projection {
        let idx = schema.index_of(col)?;
        let array = batch.column(idx).as_ref();
        let column = if let Some(array) = array.as_any().downcast_ref::<StringArray>() {
            BatchColumn::Text(array)
        } else if let Some(array) = array.as_any().downcast_ref::<TimestampMicrosecondArray>() {
            BatchColumn::Timestamp(array)
        } else if let Some(array) = array.as_any().downcast_ref::<Date32Array>() {
            BatchColumn::Int(IntArrayRef::Date32(array))
        } else if let Some(array) = array.as_any().downcast_ref::<Int16Array>() {
            BatchColumn::Int(IntArrayRef::I16(array))
        } else if let Some(array) = array.as_any().downcast_ref::<Int32Array>() {
            BatchColumn::Int(IntArrayRef::I32(array))
        } else if let Some(array) = array.as_any().downcast_ref::<Int64Array>() {
            BatchColumn::Int(IntArrayRef::I64(array))
        } else {
            return Err(format!(
                "rvbbit vector aggregate: unsupported projected column '{}' type {:?}",
                col,
                array.data_type()
            )
            .into());
        };
        columns.push(column);
    }
    Ok(columns)
}

fn batch_plan<'a>(
    plan: &VectorPlan,
    columns: &[BatchColumn<'a>],
) -> Result<BatchPlan<'a>, Box<dyn std::error::Error>> {
    let keys = plan
        .keys
        .iter()
        .map(|key| match key {
            KeyPlan::Int(idx) => Ok(BatchKey::Int(int_column(columns, *idx)?)),
            KeyPlan::Date(idx) => Ok(BatchKey::Date(int_column(columns, *idx)?)),
            KeyPlan::Text(idx) => Ok(BatchKey::Text(text_column(columns, *idx)?)),
            KeyPlan::TimestampMinute(idx) => {
                Ok(BatchKey::TimestampMinute(timestamp_column(columns, *idx)?))
            }
            KeyPlan::TimestampTruncMinute(idx) => Ok(BatchKey::TimestampTruncMinute(
                timestamp_column(columns, *idx)?,
            )),
        })
        .collect::<Result<Vec<_>, Box<dyn std::error::Error>>>()?;
    let filters = plan
        .filters
        .iter()
        .map(|filter| match filter {
            FilterPlan::TextNotEmpty { idx } => Ok(BatchFilter::TextNotEmpty {
                array: text_column(columns, *idx)?,
            }),
            FilterPlan::TextContains { idx, needle } => Ok(BatchFilter::TextContains {
                array: text_column(columns, *idx)?,
                needle: needle.clone(),
            }),
            FilterPlan::TextNotContains { idx, needle } => Ok(BatchFilter::TextNotContains {
                array: text_column(columns, *idx)?,
                needle: needle.clone(),
            }),
            FilterPlan::IntEq { idx, value } => Ok(BatchFilter::IntEq {
                array: int_column(columns, *idx)?,
                value: *value,
            }),
            FilterPlan::IntNe { idx, value } => Ok(BatchFilter::IntNe {
                array: int_column(columns, *idx)?,
                value: *value,
            }),
            FilterPlan::IntGe { idx, value } => Ok(BatchFilter::IntGe {
                array: int_column(columns, *idx)?,
                value: *value,
            }),
            FilterPlan::IntLe { idx, value } => Ok(BatchFilter::IntLe {
                array: int_column(columns, *idx)?,
                value: *value,
            }),
            FilterPlan::IntIn { idx, values } => Ok(BatchFilter::IntIn {
                array: int_column(columns, *idx)?,
                values: values.clone(),
            }),
        })
        .collect::<Result<Vec<_>, Box<dyn std::error::Error>>>()?;
    let aggs = plan
        .aggs
        .iter()
        .map(|agg| match agg {
            AggPlan::SumInt { idx } => Ok(BatchAgg::SumInt(int_column(columns, *idx)?)),
            AggPlan::AvgInt { idx } => Ok(BatchAgg::AvgInt(int_column(columns, *idx)?)),
            AggPlan::CountDistinctInt { idx } => {
                Ok(BatchAgg::CountDistinctInt(int_column(columns, *idx)?))
            }
            AggPlan::MinText { idx } => Ok(BatchAgg::MinText(text_column(columns, *idx)?)),
        })
        .collect::<Result<Vec<_>, Box<dyn std::error::Error>>>()?;

    Ok(BatchPlan {
        keys,
        filters,
        aggs,
    })
}

fn filters_match(row: usize, filters: &[BatchFilter<'_>]) -> bool {
    for filter in filters {
        match filter {
            BatchFilter::TextNotEmpty { array } => {
                if array.is_null(row) || array.value(row).is_empty() {
                    return false;
                }
            }
            BatchFilter::TextContains { array, needle } => {
                if array.is_null(row) || !array.value(row).contains(needle.as_str()) {
                    return false;
                }
            }
            BatchFilter::TextNotContains { array, needle } => {
                if array.is_null(row) || array.value(row).contains(needle.as_str()) {
                    return false;
                }
            }
            BatchFilter::IntEq { array, value } => {
                if array.value(row) != Some(*value) {
                    return false;
                }
            }
            BatchFilter::IntNe { array, value } => {
                if array.value(row).map(|v| v == *value).unwrap_or(true) {
                    return false;
                }
            }
            BatchFilter::IntGe { array, value } => {
                if array.value(row).map(|v| v < *value).unwrap_or(true) {
                    return false;
                }
            }
            BatchFilter::IntLe { array, value } => {
                if array.value(row).map(|v| v > *value).unwrap_or(true) {
                    return false;
                }
            }
            BatchFilter::IntIn { array, values } => {
                let Some(value) = array.value(row) else {
                    return false;
                };
                if !values.contains(&value) {
                    return false;
                }
            }
        }
    }
    true
}

fn key_for_row(row: usize, keys: &[BatchKey<'_>]) -> GroupKey {
    match keys {
        [a] => GroupKey::One(key_value(row, a)),
        [a, b] => GroupKey::Two(key_value(row, a), key_value(row, b)),
        [a, b, c] => GroupKey::Three(key_value(row, a), key_value(row, b), key_value(row, c)),
        _ => {
            let mut values = Vec::with_capacity(keys.len());
            for key in keys {
                values.push(key_value(row, key));
            }
            GroupKey::Many(values)
        }
    }
}

fn key_value(row: usize, key: &BatchKey<'_>) -> KeyValue {
    match key {
        BatchKey::Int(array) => KeyValue::Int(array.value(row)),
        BatchKey::Date(array) => KeyValue::Date(array.value(row).map(|value| value as i32)),
        BatchKey::Text(array) => KeyValue::Text(if array.is_null(row) {
            None
        } else {
            Some(array.value(row).to_string())
        }),
        BatchKey::TimestampMinute(array) => {
            let value = if array.is_null(row) {
                None
            } else {
                Some(timestamp_minute(array.value(row)))
            };
            KeyValue::Minute(value)
        }
        BatchKey::TimestampTruncMinute(array) => {
            let value = if array.is_null(row) {
                None
            } else {
                Some(trunc_timestamp_minute(array.value(row)))
            };
            KeyValue::Timestamp(value)
        }
    }
}

fn int_column<'a>(
    columns: &[BatchColumn<'a>],
    idx: usize,
) -> Result<IntArrayRef<'a>, Box<dyn std::error::Error>> {
    match columns.get(idx) {
        Some(BatchColumn::Int(array)) => Ok(*array),
        Some(_) => Err("rvbbit vector aggregate: column is not integer".into()),
        None => Err("rvbbit vector aggregate: missing integer column".into()),
    }
}

fn text_column<'a>(
    columns: &[BatchColumn<'a>],
    idx: usize,
) -> Result<&'a StringArray, Box<dyn std::error::Error>> {
    match columns.get(idx) {
        Some(BatchColumn::Text(array)) => Ok(*array),
        Some(_) => Err("rvbbit vector aggregate: column is not text".into()),
        None => Err("rvbbit vector aggregate: missing text column".into()),
    }
}

fn timestamp_column<'a>(
    columns: &[BatchColumn<'a>],
    idx: usize,
) -> Result<&'a TimestampMicrosecondArray, Box<dyn std::error::Error>> {
    match columns.get(idx) {
        Some(BatchColumn::Timestamp(array)) => Ok(*array),
        Some(_) => Err("rvbbit vector aggregate: column is not timestamp".into()),
        None => Err("rvbbit vector aggregate: missing timestamp column".into()),
    }
}

fn projected_text_array<'a>(
    batch: &'a RecordBatch,
    col: &str,
) -> Result<&'a StringArray, Box<dyn std::error::Error>> {
    let idx = batch.schema().index_of(col)?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(|| format!("rvbbit vector aggregate: column '{col}' is not text").into())
}

fn projected_int_array<'a>(
    batch: &'a RecordBatch,
    col: &str,
) -> Result<IntArrayRef<'a>, Box<dyn std::error::Error>> {
    let idx = batch.schema().index_of(col)?;
    let array = batch.column(idx).as_ref();
    if let Some(array) = array.as_any().downcast_ref::<Date32Array>() {
        Ok(IntArrayRef::Date32(array))
    } else if let Some(array) = array.as_any().downcast_ref::<Int16Array>() {
        Ok(IntArrayRef::I16(array))
    } else if let Some(array) = array.as_any().downcast_ref::<Int32Array>() {
        Ok(IntArrayRef::I32(array))
    } else if let Some(array) = array.as_any().downcast_ref::<Int64Array>() {
        Ok(IntArrayRef::I64(array))
    } else {
        Err(format!(
            "rvbbit vector aggregate: column '{}' is not integer, found {:?}",
            col,
            array.data_type()
        )
        .into())
    }
}

fn projected_timestamp_array<'a>(
    batch: &'a RecordBatch,
    col: &str,
) -> Result<&'a TimestampMicrosecondArray, Box<dyn std::error::Error>> {
    let idx = batch.schema().index_of(col)?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<TimestampMicrosecondArray>()
        .ok_or_else(|| format!("rvbbit vector aggregate: column '{col}' is not timestamp").into())
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

fn timestamp_minute(epoch_micros: i64) -> i32 {
    const MICROS_PER_MINUTE: i64 = 60_000_000;
    epoch_micros.div_euclid(MICROS_PER_MINUTE).rem_euclid(60) as i32
}

fn trunc_timestamp_minute(epoch_micros: i64) -> i64 {
    const MICROS_PER_MINUTE: i64 = 60_000_000;
    epoch_micros.div_euclid(MICROS_PER_MINUTE) * MICROS_PER_MINUTE
}

fn format_timestamp_micros(epoch_micros: i64) -> String {
    let seconds = epoch_micros.div_euclid(1_000_000);
    let days = seconds.div_euclid(86_400);
    let second_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = second_of_day / 3_600;
    let minute = (second_of_day % 3_600) / 60;
    let second = second_of_day % 60;
    format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}")
}

pub(crate) fn format_date32(days_since_epoch: i32) -> String {
    let (year, month, day) = civil_from_days(days_since_epoch as i64);
    format!("{year:04}-{month:02}-{day:02}")
}

fn civil_from_days(days_since_epoch: i64) -> (i32, u32, u32) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 }.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096).div_euclid(365);
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2).div_euclid(153);
    let day = doy - (153 * mp + 2).div_euclid(5) + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if month <= 2 { 1 } else { 0 };
    (year as i32, month as u32, day as u32)
}
