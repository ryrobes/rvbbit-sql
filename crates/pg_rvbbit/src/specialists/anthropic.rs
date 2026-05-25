//! anthropic transport — the Anthropic Messages API.
//!
//! An LLM provider backend with `transport => 'anthropic'`. Differs from
//! `openai_chat`: `system` is a top-level field (not a message), `max_tokens`
//! is required, auth is the `x-api-key` header (not bearer), and every
//! request must carry `anthropic-version`. The response `content` is a
//! block array — we take the first text block. No inline cost; derive it
//! from `rvbbit.model_rates` downstream.
//!
//! Register:
//!   SELECT rvbbit.register_backend(
//!       backend_name      => 'anthropic',
//!       backend_endpoint  => 'https://api.anthropic.com/v1/messages',
//!       backend_transport => 'anthropic',
//!       backend_auth_env  => 'ANTHROPIC_API_KEY');

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{http_client, SpecialistResponse, SpecialistSpec, Transport, Usage};
use crate::providers::ProviderError;

const ANTHROPIC_VERSION: &str = "2023-06-01";

pub struct AnthropicTransport {
    semaphore: crate::flow::Semaphore,
}

impl AnthropicTransport {
    pub fn new() -> Self {
        Self {
            semaphore: crate::flow::Semaphore::new(super::provider_max_concurrent()),
        }
    }

    fn one_call(
        &self,
        spec: &SpecialistSpec,
        input: &Value,
    ) -> Result<(String, Usage), ProviderError> {
        let model = input
            .get("model")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ProviderError::Config("chat request missing 'model'".into()))?;
        let user = input.get("user").and_then(|v| v.as_str()).unwrap_or("");
        let system = input
            .get("system")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        let temperature = input.get("temperature").and_then(|v| v.as_f64());
        // The Messages API requires max_tokens.
        let max_tokens = input
            .get("max_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or(1024);

        let body = MsgBody {
            model,
            max_tokens,
            system,
            messages: vec![Msg {
                role: "user",
                content: user,
            }],
            temperature,
        };

        let mut req = http_client()
            .post(&spec.endpoint_url)
            .timeout(Duration::from_millis(spec.timeout_ms))
            .header("anthropic-version", ANTHROPIC_VERSION)
            .json(&body);
        if let Some(token) = spec.auth_token() {
            req = req.header("x-api-key", token);
        }

        let _backend_permit = super::acquire_backend_permit(spec);
        let _permit = self.semaphore.acquire();
        let resp = req.send()?;
        let status = resp.status();
        if !status.is_success() {
            return Err(ProviderError::ApiStatus {
                status: status.as_u16(),
                body: resp.text().unwrap_or_default(),
            });
        }
        let parsed: MsgResp = resp.json()?;
        let content = parsed
            .content
            .into_iter()
            .find(|b| b.kind == "text")
            .and_then(|b| b.text)
            .ok_or_else(|| ProviderError::BadResponse("no text block in response".into()))?;
        Ok((
            content,
            Usage {
                tokens_in: parsed.usage.input_tokens,
                tokens_out: parsed.usage.output_tokens,
                cost_usd: None,
                cost_source: Some("model_rate".to_string()),
                ..Default::default()
            },
        ))
    }
}

impl Transport for AnthropicTransport {
    fn name(&self) -> &'static str {
        "anthropic"
    }

    fn client_batches(&self) -> bool {
        false
    }

    fn predict(
        &self,
        spec: &SpecialistSpec,
        inputs: &[Value],
    ) -> Result<SpecialistResponse, ProviderError> {
        let t0 = std::time::Instant::now();
        let mut outputs = Vec::with_capacity(inputs.len());
        let mut usage = Vec::with_capacity(inputs.len());
        for input in inputs {
            let (content, u) = self.one_call(spec, input)?;
            outputs.push(Value::String(content));
            usage.push(u);
        }
        Ok(SpecialistResponse {
            outputs,
            usage,
            latency_ms: t0.elapsed().as_millis().min(i32::MAX as u128) as i32,
        })
    }
}

#[derive(Serialize)]
struct MsgBody<'a> {
    model: &'a str,
    max_tokens: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    system: Option<&'a str>,
    messages: Vec<Msg<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    temperature: Option<f64>,
}

#[derive(Serialize)]
struct Msg<'a> {
    role: &'a str,
    content: &'a str,
}

#[derive(Deserialize)]
struct MsgResp {
    #[serde(default)]
    content: Vec<Block>,
    #[serde(default)]
    usage: AnthUsage,
}

#[derive(Deserialize)]
struct Block {
    #[serde(rename = "type", default)]
    kind: String,
    #[serde(default)]
    text: Option<String>,
}

#[derive(Deserialize, Default)]
struct AnthUsage {
    #[serde(default)]
    input_tokens: i32,
    #[serde(default)]
    output_tokens: i32,
}
