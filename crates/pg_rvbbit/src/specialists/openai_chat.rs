//! openai_chat transport — the OpenAI chat-completions wire format.
//!
//! An LLM provider is just a backend with a chat transport. This one wire
//! format covers OpenRouter AND every OpenAI-compatible endpoint: a local
//! vLLM or Ollama, OpenAI itself, Together, Groq, Fireworks. Register any
//! of them with `rvbbit.register_backend(..., 'openai_chat', ...)`.
//!
//! Each input is one chat request object — `{model, system, user,
//! temperature, max_tokens}` — built by `providers::chat`. Chat completions
//! has no batch API, so N inputs become N sequential calls here (the warm
//! engine parallelizes across rows via the pool); `client_batches` is false.
//! Semaphores cap both per-backend and total openai_chat fan-out so a bulk
//! query cannot accidentally hammer an endpoint past its rate limit.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{http_client, SpecialistResponse, SpecialistSpec, Transport, Usage};
use crate::providers::ProviderError;

pub struct OpenAiChatTransport {
    /// Cap on concurrent in-flight calls across all openai_chat providers
    /// from this backend. Sized once from RVBBIT_PROVIDER_MAX_CONCURRENT.
    semaphore: crate::flow::Semaphore,
}

impl OpenAiChatTransport {
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
        let max_tokens = input
            .get("max_tokens")
            .and_then(|v| v.as_u64())
            .map(|n| n.max(16));

        let mut messages = Vec::with_capacity(2);
        if let Some(s) = system {
            messages.push(Msg {
                role: "system",
                content: s,
            });
        }
        messages.push(Msg {
            role: "user",
            content: user,
        });
        let use_max_completion_tokens = spec
            .transport_opts
            .get("max_tokens_field")
            .and_then(|v| v.as_str())
            .map(|field| field == "max_completion_tokens")
            .unwrap_or_else(|| spec.endpoint_url.contains("api.openai.com"));
        let body = ChatBody {
            model,
            messages,
            temperature,
            max_tokens: if use_max_completion_tokens {
                None
            } else {
                max_tokens
            },
            max_completion_tokens: if use_max_completion_tokens {
                max_tokens
            } else {
                None
            },
        };

        let mut req = http_client()
            .post(&spec.endpoint_url)
            .timeout(Duration::from_millis(spec.timeout_ms))
            // OpenRouter uses these for attribution; harmless elsewhere.
            .header("HTTP-Referer", "https://github.com/rvbbit-postgres/rvbbit")
            .header("X-Title", "rvbbit")
            .json(&body);
        if let Some(token) = spec.auth_token() {
            req = req.bearer_auth(token);
        }

        // _backend_permit respects the catalog max_concurrent for this
        // backend; _permit caps total openai_chat fan-out in this process.
        let _backend_permit = super::acquire_backend_permit(spec);
        let _permit = self.semaphore.acquire();
        let resp = super::send_with_lane_retry(req, spec.timeout_ms)?;
        let status = resp.status();
        if !status.is_success() {
            return Err(ProviderError::ApiStatus {
                status: status.as_u16(),
                body: resp.text().unwrap_or_default(),
            });
        }
        let parsed: ChatResp = resp.json()?;
        let response_id = parsed.id.clone();
        let raw_usage = serde_json::to_value(&parsed.usage).ok();
        let content = parsed
            .choices
            .into_iter()
            .next()
            .and_then(|c| c.message.content)
            .ok_or_else(|| ProviderError::BadResponse("no choices/content".into()))?;
        Ok((
            content,
            Usage {
                tokens_in: parsed.usage.prompt_tokens,
                tokens_out: parsed.usage.completion_tokens,
                cost_usd: parsed.usage.cost,
                cost_source: parsed.usage.cost.map(|_| "inline".to_string()).or_else(|| {
                    response_id
                        .as_ref()
                        .filter(|_| {
                            spec.name == "openrouter" || spec.endpoint_url.contains("openrouter.ai")
                        })
                        .map(|_| "openrouter_generation".to_string())
                }),
                provider_generation_id: response_id,
                raw: raw_usage,
                ..Default::default()
            },
        ))
    }
}

impl Transport for OpenAiChatTransport {
    fn name(&self) -> &'static str {
        "openai_chat"
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

    fn chat_with_tools(
        &self,
        spec: &SpecialistSpec,
        model: &str,
        messages: &[crate::providers::ChatMessage],
        tools: &[crate::providers::ToolSpec],
    ) -> Result<crate::providers::ChatToolsResponse, ProviderError> {
        use crate::providers::{ChatToolsResponse, ToolCall};
        let t0 = std::time::Instant::now();

        // Wire messages: a Value array so we can echo `tool_calls` verbatim and
        // attach `tool_call_id` on tool-result turns without a struct per shape.
        let wire_msgs: Vec<Value> = messages
            .iter()
            .map(|m| {
                let mut o = serde_json::Map::new();
                o.insert("role".into(), Value::String(m.role.clone()));
                // role=assistant with tool_calls may carry null content; keep the key.
                o.insert(
                    "content".into(),
                    m.content.clone().map(Value::String).unwrap_or(Value::Null),
                );
                if let Some(tc) = &m.tool_calls {
                    o.insert("tool_calls".into(), tc.clone());
                }
                if let Some(id) = &m.tool_call_id {
                    o.insert("tool_call_id".into(), Value::String(id.clone()));
                }
                Value::Object(o)
            })
            .collect();

        let mut body = serde_json::json!({ "model": model, "messages": wire_msgs });
        if !tools.is_empty() {
            let wire_tools: Vec<Value> = tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                    })
                })
                .collect();
            body["tools"] = Value::Array(wire_tools);
        }
        // Generous completion headroom — agents write reports; cap field varies.
        let use_mct = spec
            .transport_opts
            .get("max_tokens_field")
            .and_then(|v| v.as_str())
            .map(|field| field == "max_completion_tokens")
            .unwrap_or_else(|| spec.endpoint_url.contains("api.openai.com"));
        body[if use_mct { "max_completion_tokens" } else { "max_tokens" }] =
            serde_json::json!(4096);

        let mut req = http_client()
            .post(&spec.endpoint_url)
            .timeout(Duration::from_millis(spec.timeout_ms))
            .header("HTTP-Referer", "https://github.com/rvbbit-postgres/rvbbit")
            .header("X-Title", "rvbbit")
            .json(&body);
        if let Some(token) = spec.auth_token() {
            req = req.bearer_auth(token);
        }

        let _backend_permit = super::acquire_backend_permit(spec);
        let _permit = self.semaphore.acquire();
        let resp = super::send_with_lane_retry(req, spec.timeout_ms)?;
        let status = resp.status();
        if !status.is_success() {
            return Err(ProviderError::ApiStatus {
                status: status.as_u16(),
                body: resp.text().unwrap_or_default(),
            });
        }
        let parsed: Value = resp.json()?;

        let gen_id = parsed
            .get("id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let usage = parsed.get("usage").cloned();
        let prompt_tokens = usage
            .as_ref()
            .and_then(|u| u.get("prompt_tokens"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0) as i32;
        let completion_tokens = usage
            .as_ref()
            .and_then(|u| u.get("completion_tokens"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0) as i32;
        let cost_usd = usage
            .as_ref()
            .and_then(|u| u.get("cost"))
            .and_then(|v| v.as_f64());

        let choice = parsed
            .get("choices")
            .and_then(|c| c.get(0))
            .cloned()
            .unwrap_or(Value::Null);
        let finish_reason = choice
            .get("finish_reason")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let msg = choice.get("message").cloned().unwrap_or(Value::Null);
        let content = msg
            .get("content")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string());
        let raw_tool_calls = msg
            .get("tool_calls")
            .filter(|v| !v.is_null())
            .cloned();
        let tool_calls: Vec<ToolCall> = raw_tool_calls
            .as_ref()
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|tc| {
                        let id = tc.get("id").and_then(|v| v.as_str())?.to_string();
                        let func = tc.get("function")?;
                        let name = func.get("name").and_then(|v| v.as_str())?.to_string();
                        let args_str =
                            func.get("arguments").and_then(|v| v.as_str()).unwrap_or("{}");
                        let arguments =
                            serde_json::from_str(args_str).unwrap_or_else(|_| serde_json::json!({}));
                        Some(ToolCall { id, name, arguments })
                    })
                    .collect()
            })
            .unwrap_or_default();

        let cost_source = cost_usd.map(|_| "inline".to_string()).or_else(|| {
            gen_id
                .as_ref()
                .filter(|_| {
                    spec.name == "openrouter" || spec.endpoint_url.contains("openrouter.ai")
                })
                .map(|_| "openrouter_generation".to_string())
        });

        Ok(ChatToolsResponse {
            content,
            tool_calls,
            raw_tool_calls,
            finish_reason,
            model: model.to_string(),
            provider: spec.name.clone(),
            prompt_tokens,
            completion_tokens,
            cost_usd,
            cost_source,
            provider_generation_id: gen_id,
            raw_usage: usage,
            latency_ms: t0.elapsed().as_millis().min(i32::MAX as u128) as i32,
        })
    }
}

#[derive(Serialize)]
struct ChatBody<'a> {
    model: &'a str,
    messages: Vec<Msg<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    temperature: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_tokens: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_completion_tokens: Option<u64>,
}

#[derive(Serialize)]
struct Msg<'a> {
    role: &'a str,
    content: &'a str,
}

#[derive(Deserialize)]
struct ChatResp {
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    choices: Vec<Choice>,
    #[serde(default)]
    usage: RespUsage,
}

#[derive(Deserialize)]
struct Choice {
    message: ChoiceMsg,
}

#[derive(Deserialize)]
struct ChoiceMsg {
    content: Option<String>,
}

#[derive(Deserialize, Serialize, Default)]
struct RespUsage {
    #[serde(default)]
    prompt_tokens: i32,
    #[serde(default)]
    completion_tokens: i32,
    /// OpenRouter returns actual settled cost here; other providers omit it
    /// and cost is derived from rvbbit.model_rates downstream.
    #[serde(default)]
    cost: Option<f64>,
}
