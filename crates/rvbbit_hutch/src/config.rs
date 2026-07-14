//! Hutch service configuration — one YAML file describing the listener, the
//! upstream (mock or a real zoo), and the backend menu. Tenants live in a
//! SEPARATE file (tenants.yaml) so key rotation never touches service config.

use serde::Deserialize;
use serde_json::Value;

fn default_bind() -> String {
    "0.0.0.0:8090".into()
}
fn default_tenants_file() -> String {
    "tenants.yaml".into()
}
fn default_meter_db() -> String {
    "hutch_meter.sqlite".into()
}
fn default_max_body() -> usize {
    2 * 1024 * 1024
}
fn default_lane_grace_ms() -> u64 {
    250
}
fn default_timeout_ms() -> u64 {
    30_000
}

#[derive(Debug, Clone, Deserialize)]
pub struct HutchConfig {
    #[serde(default = "default_bind")]
    pub bind: String,
    #[serde(default = "default_tenants_file")]
    pub tenants_file: String,
    #[serde(default = "default_meter_db")]
    pub meter_db: String,
    /// Bearer token required for /metrics and /admin/*. None = open (dev only).
    #[serde(default)]
    pub admin_token: Option<String>,
    #[serde(default = "default_max_body")]
    pub max_body_bytes: usize,
    /// How long a request may wait for a free lane before 429. Small on
    /// purpose: the client (pg_rvbbit) already throttles via max_concurrent,
    /// so this only smooths bursts, it is not a queue.
    #[serde(default = "default_lane_grace_ms")]
    pub lane_grace_ms: u64,
    pub upstream: Upstream,
    pub backends: Vec<BackendCfg>,
    /// Hosted LLMs on the OpenAI-compatible surface. Empty = specialist-only.
    #[serde(default)]
    pub llms: Vec<LlmCfg>,
    /// Billing sync. Absent = static tenants.yaml only (dev mode).
    #[serde(default)]
    pub polar: Option<PolarCfg>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "mode", rename_all = "lowercase")]
pub enum Upstream {
    /// Deterministic canned outputs — proves the whole middleware chain
    /// (auth → entitlement → lanes → meter) with no GPU anywhere.
    Mock,
    /// Forward {"inputs": [...]} to base_url + backend.upstream_path and
    /// expect {"outputs": [...]} back (the zoo, or anything predict-shaped).
    Proxy { base_url: String },
}

/// How the predict contract translates to the upstream's wire shape.
/// The hutch adapts to the zoo — never the reverse.
#[derive(Debug, Clone, Copy, PartialEq, Default, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Adapter {
    /// Passthrough: upstream already speaks {"inputs":[...]}→{"outputs":[...]}.
    #[default]
    Predict,
    /// OpenAI-shaped embeddings ({"input":[texts]} → data[].embedding),
    /// emitted as bare float arrays (what pg_rvbbit's embed path parses).
    OpenaiEmbeddings,
    /// Zoo /sentiment: {"texts":[...]} → parallel {scores[],labels[]} →
    /// per-item {"score","label"}.
    ZooSentiment,
    /// Zoo /rerank: inputs are {"query","text"} pairs, grouped by query
    /// (one upstream call per distinct query) → per-position score.
    ZooRerank,
    /// Zoo /nli: {"premise","hypothesis"} pairs → {"label", "scores"}.
    ZooNli,
    /// Zoo /classify (single-text zero-shot): {"text","labels"} →
    /// {"label","score","scores"}. One upstream call per input.
    ZooClassify,
    /// Zoo /toxicity: texts → {"toxic","score","scores"}.
    ZooToxicity,
    /// Zoo /language: texts → {"language","confidence"}.
    ZooLanguage,
    /// Zoo /extract (GLiNER): {"text","labels"} → entity array. One
    /// upstream call per input (labels may differ per row).
    ZooExtract,
    /// Generic structured route: each input object IS the upstream request
    /// body (after upstream_params merge + JSON-string coercion for array/
    /// object fields that arrive as SQL text), and the WHOLE upstream
    /// response object is the output. One upstream call per input. Covers
    /// /document/ocr, /transcribe, /forecast, /tabular/fit, /tabular/predict,
    /// /anomaly/fit, /anomaly/score — anything single-shot request/response.
    ZooJson,
    /// Zoo /relations (REBEL): {"texts":[...]} → results[] aligned per text.
    ZooRelations,
    /// Zoo /v1/image_embeddings (SigLIP 2 dual-tower): inputs are bare
    /// strings (image URL / data URI / b64 / plain text — the zoo
    /// classifies which tower) → bare float arrays, same convention as
    /// OpenaiEmbeddings so embedding parse paths work unchanged.
    ZooImageEmbeddings,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BackendCfg {
    /// Route name: POST /b/{name}/predict
    pub name: String,
    /// Entitlement group a tenant must hold (e.g. "clover", "gemma").
    pub entitlement: String,
    /// Path on the upstream in proxy mode (e.g. "/sentiment").
    #[serde(default)]
    pub upstream_path: Option<String>,
    #[serde(default)]
    pub adapter: Adapter,
    /// Extra fields merged into the upstream request body (e.g. NLI model
    /// variant, extract threshold). Server-side tunables — bump
    /// model_version when these change: they move verdicts.
    #[serde(default)]
    pub upstream_params: Option<Value>,
    /// Echoed in every response (header + body) — the verdict-stability
    /// breadcrumb that lands in client receipts.
    pub model_version: String,
    /// Would-have-been à la carte price per input, micro-USD. Metering
    /// records it so receipts can show utilization value under flat subs.
    #[serde(default)]
    pub unit_microusd: i64,
    #[serde(default = "default_timeout_ms")]
    pub timeout_ms: u64,
    /// Mock mode: the output emitted per input (default {"mock": true}).
    #[serde(default)]
    pub mock_output: Option<Value>,
    /// Mock mode: per-request artificial latency — lets lane saturation be
    /// exercised end-to-end without real models.
    #[serde(default)]
    pub mock_delay_ms: Option<u64>,
}

fn default_polar_api() -> String {
    "https://api.polar.sh".into()
}
fn default_polar_token_env() -> String {
    "POLAR_TOKEN".into()
}
fn default_revalidate_secs() -> u64 {
    900
}

/// Polar billing sync (see polar.rs). Absent = static tenants only.
#[derive(Debug, Clone, Deserialize)]
pub struct PolarCfg {
    #[serde(default = "default_polar_api")]
    pub api_base: String,
    pub organization_id: String,
    /// Env var NAME holding the org access token (never the token itself).
    #[serde(default = "default_polar_token_env")]
    pub token_env: String,
    #[serde(default = "default_revalidate_secs")]
    pub revalidate_secs: u64,
    /// Fast local reject for keys that can't be Polar's (e.g. "rvb_").
    #[serde(default)]
    pub key_prefix: Option<String>,
    /// The benefit IS the SKU: benefit_id → what it entitles.
    pub benefit_map: Vec<BenefitMap>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BenefitMap {
    pub benefit_id: String,
    pub entitlements: Vec<String>,
    pub lanes: usize,
}

fn default_llm_upstream() -> String {
    "http://localhost:8000".into()
}
fn default_llm_timeout_ms() -> u64 {
    120_000
}

/// A hosted LLM behind the OpenAI-compatible surface (/v1/chat/completions
/// with model-name routing). One entry per public model id; the same
/// surface serves pg_rvbbit's openai_chat transport, agent()/flow steps,
/// and raw OpenAI SDKs — no per-consumer plumbing.
#[derive(Debug, Clone, Deserialize)]
pub struct LlmCfg {
    /// Public model id — what subscribers put in the request's `model`.
    pub name: String,
    pub entitlement: String,
    #[serde(default = "default_llm_upstream")]
    pub upstream_base: String,
    /// Served model name at the upstream (e.g. the vLLM --model id).
    pub upstream_model: String,
    pub model_version: String,
    /// Would-be à la carte rates for receipts, micro-USD per 1k tokens.
    #[serde(default)]
    pub prompt_microusd_per_1k: i64,
    #[serde(default)]
    pub completion_microusd_per_1k: i64,
    #[serde(default = "default_llm_timeout_ms")]
    pub timeout_ms: u64,
}

impl HutchConfig {
    pub fn load(path: &str) -> Result<Self, String> {
        let raw = std::fs::read_to_string(path)
            .map_err(|e| format!("cannot read config '{path}': {e}"))?;
        let cfg: HutchConfig =
            serde_yaml::from_str(&raw).map_err(|e| format!("bad config '{path}': {e}"))?;
        if cfg.backends.is_empty() {
            return Err("config has no backends".into());
        }
        if let Upstream::Proxy { .. } = cfg.upstream {
            for b in &cfg.backends {
                if b.upstream_path.is_none() {
                    return Err(format!(
                        "backend '{}' has no upstream_path but upstream.mode is proxy",
                        b.name
                    ));
                }
            }
        }
        Ok(cfg)
    }

    pub fn backend(&self, name: &str) -> Option<&BackendCfg> {
        self.backends.iter().find(|b| b.name == name)
    }

    pub fn llm(&self, name: &str) -> Option<&LlmCfg> {
        self.llms.iter().find(|l| l.name == name)
    }
}
