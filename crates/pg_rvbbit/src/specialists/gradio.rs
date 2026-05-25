//! Gradio transport — talks to Gradio Spaces or self-hosted Gradio apps
//! via the sync /api/predict (or /run/<api_name>) endpoint.
//!
//! Wire format (request):
//!   POST {endpoint_url}
//!   {"data": [arg1, arg2, ...], "fn_index": <opt>}
//!
//! Wire format (response):
//!   200 OK
//!   {"data": [out1, out2, ...]}
//!
//! Per-row dispatch only — Gradio's batching model is server-side: configure
//! gr.Interface(batch=True) and Gradio coalesces concurrent requests into
//! a forward-pass batch. rvbbit sends one HTTP call per input; the pool +
//! per-specialist semaphore provide concurrency.
//!
//! User catalog wiring — `inputs.data` IS the positional args array:
//!   {"name":"g","kind":"specialist","specialist":"my_gradio",
//!    "inputs":{"data":["{{ inputs.text }}", 0.7]}}
//!
//! Optional `transport_opts.fn_index` is forwarded; useful for Gradio apps
//! with multiple endpoints behind a single Blocks app.
//!
//! Output extraction: by default `response.data[0]` is the operator's
//! returned value. Set `transport_opts.output_index` to pick a different
//! slot for multi-output Gradio interfaces.

use super::{http_client, SpecialistResponse, SpecialistSpec, Transport};
use crate::providers::ProviderError;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::Duration;

pub struct GradioTransport;

impl GradioTransport {
    pub fn new() -> Self {
        Self
    }
}

#[derive(Serialize)]
struct GradioRequest<'a> {
    data: &'a Vec<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    fn_index: Option<i64>,
}

#[derive(Deserialize)]
struct GradioResponse {
    #[serde(default)]
    data: Vec<Value>,
}

impl Transport for GradioTransport {
    fn name(&self) -> &'static str {
        "gradio"
    }

    fn client_batches(&self) -> bool {
        false
    }

    fn predict(
        &self,
        spec: &SpecialistSpec,
        inputs: &[Value],
    ) -> Result<SpecialistResponse, ProviderError> {
        // For per-row dispatch we expect inputs.len() == 1, but support
        // longer slices by issuing them sequentially in this call — the
        // caller decides whether to parallelize across calls.
        let fn_index = spec.transport_opts.get("fn_index").and_then(|v| v.as_i64());
        let output_index = spec
            .transport_opts
            .get("output_index")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as usize;

        let mut outputs = Vec::with_capacity(inputs.len());
        let t0 = std::time::Instant::now();
        for input in inputs {
            let data = match input.get("data") {
                Some(Value::Array(arr)) => arr.clone(),
                Some(other) => vec![other.clone()],
                None => {
                    // Fallback: treat the whole input map as the only arg.
                    vec![input.clone()]
                }
            };

            let mut req = http_client()
                .post(&spec.endpoint_url)
                .timeout(Duration::from_millis(spec.timeout_ms))
                .json(&GradioRequest {
                    data: &data,
                    fn_index,
                });
            if let Some(token) = spec.auth_token() {
                req = req.bearer_auth(token);
            }

            let resp = req.send()?;
            let status = resp.status();
            if !status.is_success() {
                let body = resp.text().unwrap_or_default();
                return Err(ProviderError::ApiStatus {
                    status: status.as_u16(),
                    body,
                });
            }
            let parsed: GradioResponse = resp.json()?;
            let output = parsed.data.into_iter().nth(output_index).ok_or_else(|| {
                ProviderError::BadResponse(format!(
                    "gradio response missing data[{}]",
                    output_index
                ))
            })?;
            outputs.push(output);
        }
        let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

        Ok(SpecialistResponse {
            outputs,
            usage: Vec::new(),
            latency_ms,
        })
    }
}
