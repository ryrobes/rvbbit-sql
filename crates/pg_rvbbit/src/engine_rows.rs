//! Direct engine→tuple result path (RYR: the 11µs/row fix).
//!
//! The historical accel result path materialized every row three times:
//! Arrow file → serde_json objects → ONE giant jsonb array → PG's
//! `jsonb_to_recordset` re-parse → typed tuples. ~11µs per OUTPUT row, which
//! dominates any accelerated query returning more than a couple thousand rows
//! (found via the DoomQL frame benchmark: 15.5ms of engine work arriving
//! 103ms later).
//!
//! `rvbbit._engine_rows(engine, layout, query, max_rows)` RETURNS SETOF record
//! decodes the sidecar's Arrow IPC file straight into Datums against the
//! call-site column definition list (`AS t(col type, ...)`), materialize-mode
//! into a tuplestore. Fast paths for ints/floats/bool/text; everything else
//! goes value→string→the column type's input function (universal, correct).
//!
//! Fail-open contract preserved: ANY wrinkle on the direct path (backend
//! error, non-arrow payload, decode mismatch) falls back to the existing
//! `engine_query_json` machinery — which owns retries and heap fail-open —
//! and its JSON objects are converted through the same typinput path. The
//! rewriter only emits this shape when `rvbbit.rows_direct` is on (default),
//! so the GUC is a clean kill switch back to the jsonb pipeline.

use std::ffi::CString;
use std::fs::File;

use arrow::array::{
    Array, BooleanArray, Float32Array, Float64Array, Int16Array, Int32Array, Int64Array,
    LargeStringArray, StringArray,
};
use arrow::datatypes::DataType;
use arrow::ipc::reader::StreamReader;
use arrow::record_batch::RecordBatch;
use arrow::util::display::array_value_to_string;
use pgrx::datum::IntoDatum;
use pgrx::prelude::*;
use serde_json::Value;

/// Per-output-column conversion state, resolved once from the call-site
/// TupleDesc: the attribute type + its input function for the fallback path.
struct ColSpec {
    atttypid: pg_sys::Oid,
    atttypmod: i32,
    infunc: pg_sys::Oid,
    ioparam: pg_sys::Oid,
}

unsafe fn col_specs(desc: pg_sys::TupleDesc) -> Vec<ColSpec> {
    let natts = (*desc).natts as usize;
    let mut out = Vec::with_capacity(natts);
    for i in 0..natts {
        let attr = pgrx::pg_sys::TupleDescAttr(desc, i as i32);
        let mut infunc = pg_sys::InvalidOid;
        let mut ioparam = pg_sys::InvalidOid;
        pg_sys::getTypeInputInfo((*attr).atttypid, &mut infunc, &mut ioparam);
        out.push(ColSpec {
            atttypid: (*attr).atttypid,
            atttypmod: (*attr).atttypmod,
            infunc,
            ioparam,
        });
    }
    out
}

/// Universal fallback: any value rendered as text through the column type's
/// input function. Correct for every PG type; the fast paths above it exist
/// purely to skip the parse for the overwhelmingly common primitives.
unsafe fn datum_via_input(spec: &ColSpec, s: &str) -> pg_sys::Datum {
    // Interior NULs can't reach a cstring input function; scrub defensively.
    let cleaned;
    let src = if s.contains('\0') {
        cleaned = s.replace('\0', "");
        cleaned.as_str()
    } else {
        s
    };
    let c = CString::new(src).unwrap_or_default();
    pg_sys::OidInputFunctionCall(spec.infunc, c.as_ptr() as *mut _, spec.ioparam, spec.atttypmod)
}

/// Arrow cell → Datum for one output column. Returns (datum, isnull).
unsafe fn arrow_cell_to_datum(
    array: &dyn Array,
    row: usize,
    spec: &ColSpec,
) -> Result<(pg_sys::Datum, bool), String> {
    if array.is_null(row) {
        return Ok((pg_sys::Datum::from(0), true));
    }
    let t = spec.atttypid;
    let dt = array.data_type();
    let fast: Option<Option<pg_sys::Datum>> = if t == pg_sys::INT8OID && *dt == DataType::Int64 {
        array
            .as_any()
            .downcast_ref::<Int64Array>()
            .map(|a| a.value(row).into_datum())
    } else if t == pg_sys::INT8OID && *dt == DataType::Int32 {
        array
            .as_any()
            .downcast_ref::<Int32Array>()
            .map(|a| (a.value(row) as i64).into_datum())
    } else if t == pg_sys::INT4OID && *dt == DataType::Int32 {
        array
            .as_any()
            .downcast_ref::<Int32Array>()
            .map(|a| a.value(row).into_datum())
    } else if t == pg_sys::INT4OID && *dt == DataType::Int64 {
        array
            .as_any()
            .downcast_ref::<Int64Array>()
            .and_then(|a| i32::try_from(a.value(row)).ok())
            .map(|v| v.into_datum())
    } else if t == pg_sys::INT2OID && *dt == DataType::Int16 {
        array
            .as_any()
            .downcast_ref::<Int16Array>()
            .map(|a| a.value(row).into_datum())
    } else if t == pg_sys::INT2OID && *dt == DataType::Int32 {
        array
            .as_any()
            .downcast_ref::<Int32Array>()
            .and_then(|a| i16::try_from(a.value(row)).ok())
            .map(|v| v.into_datum())
    } else if t == pg_sys::INT2OID && *dt == DataType::Int64 {
        array
            .as_any()
            .downcast_ref::<Int64Array>()
            .and_then(|a| i16::try_from(a.value(row)).ok())
            .map(|v| v.into_datum())
    } else if t == pg_sys::FLOAT8OID && *dt == DataType::Float64 {
        array
            .as_any()
            .downcast_ref::<Float64Array>()
            .map(|a| a.value(row).into_datum())
    } else if t == pg_sys::FLOAT8OID && *dt == DataType::Float32 {
        array
            .as_any()
            .downcast_ref::<Float32Array>()
            .map(|a| (a.value(row) as f64).into_datum())
    } else if t == pg_sys::FLOAT4OID && *dt == DataType::Float32 {
        array
            .as_any()
            .downcast_ref::<Float32Array>()
            .map(|a| a.value(row).into_datum())
    } else if t == pg_sys::BOOLOID && *dt == DataType::Boolean {
        array
            .as_any()
            .downcast_ref::<BooleanArray>()
            .map(|a| a.value(row).into_datum())
    } else if (t == pg_sys::TEXTOID || t == pg_sys::VARCHAROID) && *dt == DataType::Utf8 {
        array
            .as_any()
            .downcast_ref::<StringArray>()
            .map(|a| a.value(row).into_datum())
    } else if (t == pg_sys::TEXTOID || t == pg_sys::VARCHAROID) && *dt == DataType::LargeUtf8 {
        array
            .as_any()
            .downcast_ref::<LargeStringArray>()
            .map(|a| a.value(row).into_datum())
    } else {
        None
    };
    if let Some(Some(datum)) = fast {
        return Ok((datum, false));
    }
    let text = array_value_to_string(array, row)
        .map_err(|e| format!("rendering Arrow value for input function: {e}"))?;
    Ok((datum_via_input(spec, &text), false))
}

/// JSON value → Datum (fallback path rows are serde objects keyed by column).
unsafe fn json_cell_to_datum(
    value: Option<&Value>,
    spec: &ColSpec,
) -> Result<(pg_sys::Datum, bool), String> {
    let value = match value {
        None | Some(Value::Null) => return Ok((pg_sys::Datum::from(0), true)),
        Some(v) => v,
    };
    let rendered;
    let s: &str = match value {
        Value::String(s) => s.as_str(),
        Value::Bool(b) => {
            if *b {
                "true"
            } else {
                "false"
            }
        }
        other => {
            rendered = other.to_string();
            rendered.as_str()
        }
    };
    Ok((datum_via_input(spec, s), false))
}

struct Sink {
    store: *mut pg_sys::Tuplestorestate,
    desc: pg_sys::TupleDesc,
    values: Vec<pg_sys::Datum>,
    nulls: Vec<bool>,
    rows: usize,
    cap: usize,
}

impl Sink {
    unsafe fn push(&mut self) {
        let tuple = pg_sys::heap_form_tuple(self.desc, self.values.as_mut_ptr(), self.nulls.as_mut_ptr());
        pg_sys::tuplestore_puttuple(self.store, tuple);
        pg_sys::heap_freetuple(tuple);
        self.rows += 1;
    }
    fn full(&self) -> bool {
        self.rows >= self.cap
    }
}

unsafe fn fill_from_arrow(path: &str, specs: &[ColSpec], sink: &mut Sink) -> Result<(), String> {
    let file = File::open(path).map_err(|e| format!("opening Arrow IPC file {path}: {e}"))?;
    let reader = StreamReader::try_new(file, None)
        .map_err(|e| format!("reading Arrow IPC stream {path}: {e}"))?;
    for batch in reader {
        let batch: RecordBatch = batch.map_err(|e| format!("reading Arrow IPC batch: {e}"))?;
        if batch.num_columns() != specs.len() {
            return Err(format!(
                "Arrow width {} does not match column definition list width {}",
                batch.num_columns(),
                specs.len()
            ));
        }
        let cols: Vec<_> = batch.columns().iter().map(|c| c.as_ref()).collect();
        for row in 0..batch.num_rows() {
            if sink.full() {
                return Ok(());
            }
            for (i, spec) in specs.iter().enumerate() {
                let (d, isnull) = arrow_cell_to_datum(cols[i], row, spec)?;
                sink.values[i] = d;
                sink.nulls[i] = isnull;
            }
            sink.push();
        }
    }
    Ok(())
}

unsafe fn fill_from_json_objects(
    objects: &[Value],
    names: &[String],
    specs: &[ColSpec],
    sink: &mut Sink,
) -> Result<(), String> {
    for obj in objects {
        if sink.full() {
            return Ok(());
        }
        let map = obj
            .as_object()
            .ok_or_else(|| "fallback row is not a JSON object".to_string())?;
        for (i, spec) in specs.iter().enumerate() {
            let (d, isnull) = json_cell_to_datum(map.get(&names[i]), spec)?;
            sink.values[i] = d;
            sink.nulls[i] = isnull;
        }
        sink.push();
    }
    Ok(())
}

#[no_mangle]
pub extern "C" fn pg_finfo_rvbbit_engine_rows_c() -> &'static pg_sys::Pg_finfo_record {
    const V1: pg_sys::Pg_finfo_record = pg_sys::Pg_finfo_record { api_version: 1 };
    &V1
}

/// `rvbbit._engine_rows(engine text, layout text, query text, max_rows int)`
/// RETURNS SETOF record — see module docs.
#[no_mangle]
#[pg_guard]
pub unsafe extern "C-unwind" fn rvbbit_engine_rows_c(
    fcinfo: pg_sys::FunctionCallInfo,
) -> pg_sys::Datum {
    let engine: String = pgrx::fcinfo::pg_getarg(fcinfo, 0).unwrap_or_default();
    let layout: String = pgrx::fcinfo::pg_getarg(fcinfo, 1).unwrap_or_default();
    let query: String = pgrx::fcinfo::pg_getarg(fcinfo, 2).unwrap_or_default();
    let max_rows: i32 = pgrx::fcinfo::pg_getarg(fcinfo, 3).unwrap_or(0);

    let rsi = (*fcinfo).resultinfo as *mut pg_sys::ReturnSetInfo;
    if rsi.is_null() || !pgrx::is_a((*fcinfo).resultinfo, pg_sys::NodeTag::T_ReturnSetInfo) {
        pgrx::error!("rvbbit._engine_rows: set-valued function called in context that cannot accept a set");
    }
    if ((*rsi).allowedModes & pg_sys::SetFunctionReturnMode::SFRM_Materialize as i32) == 0 {
        pgrx::error!("rvbbit._engine_rows: materialize mode required, but it is not allowed in this context");
    }
    if (*rsi).expectedDesc.is_null() {
        pgrx::error!("rvbbit._engine_rows: a column definition list is required (call as ... AS t(col type, ...))");
    }

    let per_query = (*(*rsi).econtext).ecxt_per_query_memory;
    let old = pg_sys::MemoryContextSwitchTo(per_query);
    let desc = pg_sys::BlessTupleDesc(pg_sys::CreateTupleDescCopy((*rsi).expectedDesc));
    let store = pg_sys::tuplestore_begin_heap(true, false, pg_sys::work_mem);
    pg_sys::MemoryContextSwitchTo(old);

    let specs = col_specs(desc);
    let natts = specs.len();
    let cap = if max_rows > 0 {
        max_rows as usize
    } else {
        crate::duck_backend::max_rows() as usize
    };
    let mut sink = Sink {
        store,
        desc,
        values: vec![pg_sys::Datum::from(0); natts],
        nulls: vec![true; natts],
        rows: 0,
        cap,
    };

    // Direct path: happy-case payload acquisition + Arrow decode. Any error
    // anywhere degrades to the battle-tested json pipeline below.
    let mut direct_err: Option<String> = None;
    match crate::duck_backend::engine_query_payload_direct(&engine, &layout, &query, cap as i32) {
        Ok(payload) => {
            let format = payload
                .get("result_format")
                .and_then(Value::as_str)
                .unwrap_or("json");
            let arrow_path = payload.get("arrow_ipc_path").and_then(Value::as_str);
            if format == "arrow_ipc_file" && arrow_path.is_some() {
                let path = arrow_path.unwrap().to_string();
                let result = fill_from_arrow(&path, &specs, &mut sink);
                let _ = std::fs::remove_file(&path);
                if let Err(e) = result {
                    direct_err = Some(e);
                    sink.rows = 0; // rebuilt below via fallback
                    pg_sys::tuplestore_clear(store);
                }
            } else {
                direct_err = Some(format!("payload format {format} (no arrow path)"));
            }
        }
        Err(e) => direct_err = Some(e),
    }

    if let Some(reason) = direct_err {
        // Fallback: the existing engine_query_json owns retries + heap
        // fail-open; convert its objects through the same typinput path.
        pgrx::debug1!("rvbbit._engine_rows: direct path unavailable ({reason}); using json pipeline");
        let names: Vec<String> = (0..natts)
            .map(|i| {
                let attr = pgrx::pg_sys::TupleDescAttr(desc, i as i32);
                pgrx::name_data_to_str(&(*attr).attname).to_string()
            })
            .collect();
        let columns_json = Value::Array(names.iter().cloned().map(Value::String).collect());
        let objects = crate::duck_backend::engine_query_json_objects(
            &engine,
            &layout,
            &query,
            pgrx::JsonB(columns_json),
            cap as i32,
        );
        if let Err(e) = fill_from_json_objects(&objects, &names, &specs, &mut sink) {
            pgrx::error!("rvbbit._engine_rows: fallback row conversion failed: {e}");
        }
    }

    (*rsi).returnMode = pg_sys::SetFunctionReturnMode::SFRM_Materialize;
    (*rsi).setResult = store;
    (*rsi).setDesc = desc;
    (*fcinfo).isnull = true;
    pg_sys::Datum::from(0)
}
