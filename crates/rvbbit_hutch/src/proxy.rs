//! Upstream dispatch — mock (deterministic, GPU-free) or proxy with
//! per-backend adapters translating the predict contract to the zoo's
//! route shapes (captured live 2026-07-12; see Adapter in config.rs).

use crate::config::{Adapter, BackendCfg, Upstream};
use serde_json::{json, Value};
use std::time::Instant;

pub struct ForwardOk {
    pub outputs: Vec<Value>,
    pub upstream_ms: f64,
}

pub enum ForwardErr {
    /// Upstream answered non-2xx: propagate enough detail to diagnose.
    Status { status: u16, body_head: String },
    /// Transport-level failure (connect, timeout, bad JSON).
    Transport(String),
}

pub async fn forward(
    http: &reqwest::Client,
    upstream: &Upstream,
    backend: &BackendCfg,
    inputs: &[Value],
) -> Result<ForwardOk, ForwardErr> {
    let t0 = Instant::now();
    match upstream {
        Upstream::Mock => {
            if let Some(ms) = backend.mock_delay_ms {
                tokio::time::sleep(std::time::Duration::from_millis(ms)).await;
            }
            let one = backend
                .mock_output
                .clone()
                .unwrap_or_else(|| json!({"mock": true}));
            Ok(ForwardOk {
                outputs: vec![one; inputs.len()],
                upstream_ms: t0.elapsed().as_secs_f64() * 1000.0,
            })
        }
        Upstream::Proxy { base_url } => {
            let path = backend.upstream_path.as_deref().unwrap_or("/predict");
            let url = format!("{}{}", base_url.trim_end_matches('/'), path);
            let outputs = match backend.adapter {
                Adapter::Predict => {
                    let parsed =
                        post_json(http, &url, backend, &json!({ "inputs": inputs })).await?;
                    json_array(&parsed, "outputs")?
                }
                Adapter::OpenaiEmbeddings => {
                    let texts = input_texts(inputs)?;
                    let parsed =
                        post_json(http, &url, backend, &json!({ "input": texts })).await?;
                    json_array(&parsed, "data")?
                        .iter()
                        .map(|item| {
                            item.get("embedding").cloned().ok_or_else(|| {
                                ForwardErr::Transport("embeddings item missing 'embedding'".into())
                            })
                        })
                        .collect::<Result<Vec<_>, _>>()?
                }
                Adapter::ZooSentiment => {
                    let texts = input_texts(inputs)?;
                    let parsed =
                        post_json(http, &url, backend, &json!({ "texts": texts })).await?;
                    let scores = json_array(&parsed, "scores")?;
                    let labels = json_array(&parsed, "labels")?;
                    scores
                        .iter()
                        .zip(labels.iter())
                        .map(|(s, l)| json!({"score": s, "label": l}))
                        .collect()
                }
                Adapter::ZooRerank => rerank_grouped(http, &url, backend, inputs).await?,
                Adapter::ZooNli => {
                    let pairs: Vec<Value> = inputs
                        .iter()
                        .enumerate()
                        .map(|(i, v)| {
                            let p = field_str(v, "premise", i)?;
                            let h = field_str(v, "hypothesis", i)?;
                            Ok(json!({"premise": p, "hypothesis": h}))
                        })
                        .collect::<Result<_, ForwardErr>>()?;
                    let mut body = json!({ "pairs": pairs });
                    merge_params(&mut body, backend);
                    let parsed = post_json(http, &url, backend, &body).await?;
                    json_array(&parsed, "results")?
                        .iter()
                        .map(|r| {
                            let scores = r.get("scores").cloned().unwrap_or(json!({}));
                            json!({"label": argmax_label(&scores), "scores": scores})
                        })
                        .collect()
                }
                Adapter::ZooClassify => {
                    let mut outs = Vec::with_capacity(inputs.len());
                    for (i, v) in inputs.iter().enumerate() {
                        let text = field_str(v, "text", i)?;
                        let labels = labels_input(v, i)?;
                        let mut body = json!({ "text": text, "labels": labels });
                        merge_params(&mut body, backend);
                        let parsed = post_json(http, &url, backend, &body).await?;
                        outs.push(json!({
                            "label": parsed.get("label").cloned().unwrap_or(Value::Null),
                            "score": parsed.get("score").cloned().unwrap_or(Value::Null),
                            "scores": parsed.get("scores").cloned().unwrap_or(json!({})),
                        }));
                    }
                    outs
                }
                Adapter::ZooToxicity => {
                    let texts = input_texts(inputs)?;
                    let mut body = json!({ "texts": texts });
                    merge_params(&mut body, backend);
                    let parsed = post_json(http, &url, backend, &body).await?;
                    json_array(&parsed, "results")?
                        .iter()
                        .map(|r| {
                            json!({
                                "toxic": r.get("is_toxic").cloned().unwrap_or(Value::Null),
                                "score": r.get("overall").cloned().unwrap_or(Value::Null),
                                "scores": r.get("scores").cloned().unwrap_or(json!({})),
                            })
                        })
                        .collect()
                }
                Adapter::ZooLanguage => {
                    let texts = input_texts(inputs)?;
                    let mut body = json!({ "texts": texts });
                    merge_params(&mut body, backend);
                    let parsed = post_json(http, &url, backend, &body).await?;
                    json_array(&parsed, "results")?
                        .iter()
                        .map(|r| {
                            json!({
                                "language": r.get("language").cloned().unwrap_or(Value::Null),
                                "confidence": r.get("confidence").cloned().unwrap_or(Value::Null),
                            })
                        })
                        .collect()
                }
                Adapter::ZooExtract => {
                    let mut outs = Vec::with_capacity(inputs.len());
                    for (i, v) in inputs.iter().enumerate() {
                        let text = field_str(v, "text", i)?;
                        let labels = labels_input(v, i)?;
                        let mut body = json!({ "texts": [text], "labels": labels });
                        merge_params(&mut body, backend);
                        let parsed = post_json(http, &url, backend, &body).await?;
                        let first = json_array(&parsed, "results")?
                            .into_iter()
                            .next()
                            .unwrap_or(json!([]));
                        outs.push(first);
                    }
                    outs
                }
                Adapter::ZooJson => {
                    let mut outs = Vec::with_capacity(inputs.len());
                    for (i, v) in inputs.iter().enumerate() {
                        let mut body = v.clone();
                        if !body.is_object() {
                            return Err(ForwardErr::Transport(format!(
                                "input[{i}] must be a JSON object for this backend"
                            )));
                        }
                        coerce_json_strings(&mut body);
                        merge_params(&mut body, backend);
                        outs.push(post_json(http, &url, backend, &body).await?);
                    }
                    outs
                }
                Adapter::ZooRelations => {
                    let texts = input_texts(inputs)?;
                    let mut body = json!({ "texts": texts });
                    merge_params(&mut body, backend);
                    let parsed = post_json(http, &url, backend, &body).await?;
                    json_array(&parsed, "results")?
                }
                Adapter::ZooImageEmbeddings => {
                    let items: Vec<String> = inputs
                        .iter()
                        .enumerate()
                        .map(|(i, v)| input_text(v, i))
                        .collect::<Result<_, _>>()?;
                    let mut body = json!({ "input": items });
                    merge_params(&mut body, backend);
                    let parsed = post_json(http, &url, backend, &body).await?;
                    json_array(&parsed, "data")?
                        .iter()
                        .map(|item| {
                            item.get("embedding").cloned().ok_or_else(|| {
                                ForwardErr::Transport(
                                    "image_embeddings item missing 'embedding'".into(),
                                )
                            })
                        })
                        .collect::<Result<Vec<_>, _>>()?
                }
            };
            if outputs.len() != inputs.len() {
                return Err(ForwardErr::Transport(format!(
                    "adapter produced {} outputs for {} inputs",
                    outputs.len(),
                    inputs.len()
                )));
            }
            Ok(ForwardOk {
                outputs,
                upstream_ms: t0.elapsed().as_secs_f64() * 1000.0,
            })
        }
    }
}

async fn post_json(
    http: &reqwest::Client,
    url: &str,
    backend: &BackendCfg,
    body: &Value,
) -> Result<Value, ForwardErr> {
    let resp = http
        .post(url)
        .timeout(std::time::Duration::from_millis(backend.timeout_ms))
        .json(body)
        .send()
        .await
        .map_err(|e| ForwardErr::Transport(e.to_string()))?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(ForwardErr::Status {
            status: status.as_u16(),
            body_head: body.chars().take(300).collect(),
        });
    }
    resp.json()
        .await
        .map_err(|e| ForwardErr::Transport(format!("bad upstream JSON: {e}")))
}

fn json_array(v: &Value, key: &str) -> Result<Vec<Value>, ForwardErr> {
    v.get(key)
        .and_then(|o| o.as_array())
        .cloned()
        .ok_or_else(|| ForwardErr::Transport(format!("upstream response missing '{key}' array")))
}

/// A text input is either a bare string or {"text": "..."} (the shape
/// pg_rvbbit's embed path sends).
fn input_text(v: &Value, i: usize) -> Result<String, ForwardErr> {
    if let Some(s) = v.as_str() {
        return Ok(s.to_string());
    }
    v.get("text")
        .and_then(|t| t.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| {
            ForwardErr::Transport(format!(
                "input[{i}] is neither a string nor an object with a 'text' field"
            ))
        })
}

/// SQL operator args are text, so array/object-valued fields (tabular X/y,
/// forecast context, feature_names) often arrive as JSON *strings*. Any
/// top-level string value that looks like a JSON array/object and parses
/// cleanly is replaced with the parsed value; everything else (URLs, b64
/// documents, labels) passes through untouched.
fn coerce_json_strings(body: &mut Value) {
    if let Some(obj) = body.as_object_mut() {
        for (_, v) in obj.iter_mut() {
            if let Some(s) = v.as_str() {
                let t = s.trim_start();
                if t.starts_with('[') || t.starts_with('{') {
                    if let Ok(parsed) = serde_json::from_str::<Value>(s) {
                        *v = parsed;
                    }
                }
            }
        }
    }
}

fn merge_params(body: &mut Value, backend: &BackendCfg) {
    if let (Some(Value::Object(params)), Some(obj)) =
        (backend.upstream_params.as_ref(), body.as_object_mut())
    {
        for (k, v) in params {
            obj.entry(k.clone()).or_insert_with(|| v.clone());
        }
    }
}

fn field_str(v: &Value, key: &str, i: usize) -> Result<String, ForwardErr> {
    v.get(key)
        .and_then(|s| s.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| ForwardErr::Transport(format!("input[{i}] missing '{key}'")))
}

/// Labels arrive as a JSON array or a comma-separated string (SQL args are
/// text, so 'billing, shipping, returns' must Just Work).
fn labels_input(v: &Value, i: usize) -> Result<Vec<String>, ForwardErr> {
    match v.get("labels") {
        Some(Value::Array(a)) => Ok(a
            .iter()
            .filter_map(|x| x.as_str().map(|s| s.trim().to_string()))
            .filter(|s| !s.is_empty())
            .collect()),
        Some(Value::String(s)) => Ok(s
            .split(',')
            .map(|p| p.trim().to_string())
            .filter(|p| !p.is_empty())
            .collect()),
        _ => Err(ForwardErr::Transport(format!(
            "input[{i}] missing 'labels' (array or comma-separated string)"
        ))),
    }
}

fn argmax_label(scores: &Value) -> Value {
    scores
        .as_object()
        .and_then(|m| {
            m.iter()
                .max_by(|a, b| {
                    let fa = a.1.as_f64().unwrap_or(f64::MIN);
                    let fb = b.1.as_f64().unwrap_or(f64::MIN);
                    fa.partial_cmp(&fb).unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(k, _)| Value::String(k.clone()))
        })
        .unwrap_or(Value::Null)
}

fn input_texts(inputs: &[Value]) -> Result<Vec<String>, ForwardErr> {
    inputs
        .iter()
        .enumerate()
        .map(|(i, v)| input_text(v, i))
        .collect()
}

/// Rerank inputs are {"query","text"} pairs. The zoo scores one query
/// against many passages per call, so group by query (the common case —
/// one criterion, many rows — is exactly one upstream call) and scatter
/// scores back to original positions.
async fn rerank_grouped(
    http: &reqwest::Client,
    url: &str,
    backend: &BackendCfg,
    inputs: &[Value],
) -> Result<Vec<Value>, ForwardErr> {
    let mut groups: Vec<(String, Vec<(usize, String)>)> = Vec::new();
    for (i, v) in inputs.iter().enumerate() {
        let query = v
            .get("query")
            .and_then(|q| q.as_str())
            .ok_or_else(|| ForwardErr::Transport(format!("input[{i}] missing 'query'")))?;
        let text = v
            .get("text")
            .and_then(|t| t.as_str())
            .ok_or_else(|| ForwardErr::Transport(format!("input[{i}] missing 'text'")))?;
        match groups.iter_mut().find(|(q, _)| q == query) {
            Some((_, members)) => members.push((i, text.to_string())),
            None => groups.push((query.to_string(), vec![(i, text.to_string())])),
        }
    }
    let mut outputs = vec![Value::Null; inputs.len()];
    for (query, members) in &groups {
        let passages: Vec<&str> = members.iter().map(|(_, t)| t.as_str()).collect();
        let parsed = post_json(
            http,
            url,
            backend,
            &json!({ "query": query, "passages": passages }),
        )
        .await?;
        let scores = json_array(&parsed, "scores")?;
        if scores.len() != members.len() {
            return Err(ForwardErr::Transport(format!(
                "rerank returned {} scores for {} passages",
                scores.len(),
                members.len()
            )));
        }
        for ((idx, _), score) in members.iter().zip(scores.iter()) {
            outputs[*idx] = score.clone();
        }
    }
    Ok(outputs)
}
