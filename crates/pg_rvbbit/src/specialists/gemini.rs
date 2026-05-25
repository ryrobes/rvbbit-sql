//! gemini transport — Google's generativelanguage API.
//!
//! An LLM provider backend with `transport => 'gemini'`. Gemini puts the
//! model in the URL path, so the backend's `endpoint_url` is a template
//! containing `{model}` (substituted per call). System text is
//! `systemInstruction`; auth is either the `x-goog-api-key` header or
//! Google ADC/OAuth Bearer mode; tuning lives under `generationConfig`.
//! No inline cost; derive it downstream.
//!
//! Register:
//!   SELECT rvbbit.register_backend(
//!       backend_name      => 'gemini',
//!       backend_endpoint  => 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
//!       backend_transport => 'gemini',
//!       backend_auth_env  => 'GEMINI_API_KEY');

use std::collections::HashMap;
use std::fs;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{http_client, SpecialistResponse, SpecialistSpec, Transport, Usage};
use crate::providers::ProviderError;

const GOOGLE_ADC_ENV: &str = "GOOGLE_APPLICATION_CREDENTIALS";
const GOOGLE_TOKEN_URI: &str = "https://oauth2.googleapis.com/token";
const GOOGLE_SCOPE: &str = "https://www.googleapis.com/auth/generative-language";
const GOOGLE_JWT_GRANT: &str = "urn:ietf:params:oauth:grant-type:jwt-bearer";

static GOOGLE_TOKEN_CACHE: OnceLock<Mutex<HashMap<String, CachedAccessToken>>> = OnceLock::new();

pub struct GeminiTransport {
    semaphore: crate::flow::Semaphore,
}

impl GeminiTransport {
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
        let max_tokens = input.get("max_tokens").and_then(|v| v.as_u64());

        // The model lives in the URL; the endpoint_url is a {model} template.
        let url = spec.endpoint_url.replace("{model}", model);
        let body = GenBody {
            contents: vec![Content {
                role: "user",
                parts: vec![Part { text: user }],
            }],
            system_instruction: system.map(|s| SysInstr {
                parts: vec![Part { text: s }],
            }),
            generation_config: GenConfig {
                temperature,
                max_output_tokens: max_tokens,
            },
        };

        let mut req = http_client()
            .post(&url)
            .timeout(Duration::from_millis(spec.timeout_ms))
            .json(&body);
        match auth_mode(spec).as_str() {
            "google_adc" | "adc" | "oauth" | "service_account" => {
                let (token, user_project) = google_access_token(spec)?;
                req = req.bearer_auth(token);
                if let Some(project) = user_project {
                    req = req.header("x-goog-user-project", project);
                }
            }
            _ => {
                if let Some(token) = spec.auth_token() {
                    req = req.header("x-goog-api-key", token);
                }
            }
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
        let parsed: GenResp = resp.json()?;
        let content: String = parsed
            .candidates
            .into_iter()
            .next()
            .and_then(|c| c.content)
            .map(|cc| {
                cc.parts
                    .into_iter()
                    .filter_map(|p| p.text)
                    .collect::<String>()
            })
            .filter(|s| !s.is_empty())
            // A candidate with no text means a safety block or empty finish.
            .ok_or_else(|| ProviderError::BadResponse("no candidate text in response".into()))?;
        Ok((
            content,
            Usage {
                tokens_in: parsed.usage_metadata.prompt_token_count,
                tokens_out: parsed.usage_metadata.candidates_token_count
                    + parsed.usage_metadata.thoughts_token_count,
                cost_usd: None,
                cost_source: Some("model_rate".to_string()),
                native_tokens_out: Some(parsed.usage_metadata.candidates_token_count),
                reasoning_tokens: nonzero_i32(parsed.usage_metadata.thoughts_token_count),
                cached_tokens: nonzero_i32(parsed.usage_metadata.cached_content_token_count),
                raw: serde_json::to_value(&parsed.usage_metadata).ok(),
                ..Default::default()
            },
        ))
    }
}

impl Transport for GeminiTransport {
    fn name(&self) -> &'static str {
        "gemini"
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
#[serde(rename_all = "camelCase")]
struct GenBody<'a> {
    contents: Vec<Content<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    system_instruction: Option<SysInstr<'a>>,
    generation_config: GenConfig,
}

#[derive(Serialize)]
struct Content<'a> {
    role: &'a str,
    parts: Vec<Part<'a>>,
}

#[derive(Serialize)]
struct SysInstr<'a> {
    parts: Vec<Part<'a>>,
}

#[derive(Serialize)]
struct Part<'a> {
    text: &'a str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct GenConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    temperature: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_output_tokens: Option<u64>,
}

#[derive(Deserialize)]
struct GenResp {
    #[serde(default)]
    candidates: Vec<Candidate>,
    #[serde(default, rename = "usageMetadata")]
    usage_metadata: GeminiUsage,
}

#[derive(Deserialize)]
struct Candidate {
    #[serde(default)]
    content: Option<CandContent>,
}

#[derive(Deserialize)]
struct CandContent {
    #[serde(default)]
    parts: Vec<RespPart>,
}

#[derive(Deserialize)]
struct RespPart {
    #[serde(default)]
    text: Option<String>,
}

#[derive(Deserialize, Serialize, Default)]
#[serde(rename_all = "camelCase")]
struct GeminiUsage {
    #[serde(default)]
    prompt_token_count: i32,
    #[serde(default)]
    candidates_token_count: i32,
    #[serde(default)]
    thoughts_token_count: i32,
    #[serde(default)]
    cached_content_token_count: i32,
    #[serde(default)]
    total_token_count: i32,
}

#[derive(Clone)]
struct CachedAccessToken {
    token: String,
    expires_at: i64,
}

#[derive(Deserialize)]
struct ServiceAccountKey {
    #[serde(default)]
    project_id: Option<String>,
    #[serde(default)]
    quota_project_id: Option<String>,
    client_email: String,
    private_key: String,
    #[serde(default)]
    token_uri: Option<String>,
    #[serde(rename = "type", default)]
    key_type: Option<String>,
}

#[derive(Serialize)]
struct JwtClaims<'a> {
    iss: &'a str,
    scope: &'a str,
    aud: &'a str,
    exp: usize,
    iat: usize,
}

#[derive(Deserialize)]
struct TokenResp {
    access_token: String,
    #[serde(default)]
    expires_in: Option<i64>,
    #[serde(default)]
    token_type: Option<String>,
}

fn auth_mode(spec: &SpecialistSpec) -> String {
    spec.transport_opts
        .get("auth_mode")
        .and_then(|v| v.as_str())
        .unwrap_or("api_key")
        .trim()
        .to_ascii_lowercase()
}

pub(crate) fn google_access_token(
    spec: &SpecialistSpec,
) -> Result<(String, Option<String>), ProviderError> {
    let creds = load_service_account(spec)?;
    if let Some(t) = &creds.key_type {
        if t != "service_account" {
            return Err(ProviderError::Config(format!(
                "Gemini google_adc currently supports service_account credentials, got '{}'",
                t
            )));
        }
    }
    let scope = spec
        .transport_opts
        .get("scope")
        .and_then(|v| v.as_str())
        .unwrap_or(GOOGLE_SCOPE);
    let now = now_epoch();
    let token_uri = creds.token_uri.as_deref().unwrap_or(GOOGLE_TOKEN_URI);
    let cache_key = format!("{}|{}|{}", creds.client_email, token_uri, scope);
    let cache = GOOGLE_TOKEN_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Ok(guard) = cache.lock() {
        if let Some(cached) = guard.get(&cache_key) {
            if cached.expires_at > now + 60 {
                return Ok((cached.token.clone(), google_user_project(spec, &creds)));
            }
        }
    }

    let iat = now.max(0) as usize;
    let claims = JwtClaims {
        iss: &creds.client_email,
        scope,
        aud: token_uri,
        iat,
        exp: iat + 3600,
    };
    let key = EncodingKey::from_rsa_pem(creds.private_key.as_bytes())
        .map_err(|e| ProviderError::Config(format!("invalid Google service account key: {e}")))?;
    let assertion = encode(&Header::new(Algorithm::RS256), &claims, &key)
        .map_err(|e| ProviderError::Config(format!("failed to sign Google JWT: {e}")))?;
    let params = [
        ("grant_type", GOOGLE_JWT_GRANT),
        ("assertion", assertion.as_str()),
    ];
    let resp = http_client()
        .post(token_uri)
        .timeout(Duration::from_millis(spec.timeout_ms))
        .form(&params)
        .send()?;
    let status = resp.status();
    if !status.is_success() {
        return Err(ProviderError::ApiStatus {
            status: status.as_u16(),
            body: resp.text().unwrap_or_default(),
        });
    }
    let parsed: TokenResp = resp.json()?;
    if let Some(kind) = parsed.token_type.as_deref() {
        if !kind.eq_ignore_ascii_case("bearer") {
            return Err(ProviderError::BadResponse(format!(
                "unexpected Google token type '{}'",
                kind
            )));
        }
    }
    let expires_at = now + parsed.expires_in.unwrap_or(3600).max(60);
    if let Ok(mut guard) = cache.lock() {
        guard.insert(
            cache_key,
            CachedAccessToken {
                token: parsed.access_token.clone(),
                expires_at,
            },
        );
    }
    Ok((parsed.access_token, google_user_project(spec, &creds)))
}

fn load_service_account(spec: &SpecialistSpec) -> Result<ServiceAccountKey, ProviderError> {
    let env_name = spec.auth_header_env.as_deref().unwrap_or(GOOGLE_ADC_ENV);
    let raw = std::env::var(env_name).map_err(|_| {
        ProviderError::Config(format!(
            "Gemini google_adc needs {} to contain service account JSON or a credentials file path",
            env_name
        ))
    })?;
    let trimmed = raw.trim();
    let json = if trimmed.starts_with('{') {
        trimmed.to_string()
    } else {
        fs::read_to_string(trimmed).map_err(|e| {
            ProviderError::Config(format!(
                "failed to read Google credentials path from {}: {}",
                env_name, e
            ))
        })?
    };
    serde_json::from_str(&json)
        .map_err(|e| ProviderError::Config(format!("invalid Google credentials JSON: {e}")))
}

fn google_user_project(spec: &SpecialistSpec, creds: &ServiceAccountKey) -> Option<String> {
    spec.transport_opts
        .get("user_project")
        .or_else(|| spec.transport_opts.get("quota_project"))
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .or_else(|| std::env::var("GOOGLE_CLOUD_QUOTA_PROJECT").ok())
        .or_else(|| std::env::var("GOOGLE_CLOUD_PROJECT").ok())
        .or_else(|| creds.quota_project_id.clone())
        .or_else(|| creds.project_id.clone())
        .filter(|s| !s.trim().is_empty())
}

fn now_epoch() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn nonzero_i32(n: i32) -> Option<i32> {
    if n > 0 {
        Some(n)
    } else {
        None
    }
}
