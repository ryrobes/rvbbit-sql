//! Local CPU embedding transport.
//!
//! This makes embeddings available on a fresh Rvbbit install without a sidecar
//! or external API. It is still just another backend transport: users can
//! re-register the `embed` backend to `openai`, `rvbbit`, `gradio`, etc.
//! whenever they want a different model provider.

use std::collections::HashMap;
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::{Arc, OnceLock};

use fastembed::{EmbeddingModel, TextEmbedding, TextInitOptions};
use parking_lot::Mutex;
use serde_json::Value;

use super::{SpecialistResponse, SpecialistSpec, Transport};
use crate::providers::ProviderError;

pub struct LocalEmbedTransport;

impl LocalEmbedTransport {
    pub fn new() -> Self {
        Self
    }
}

impl Transport for LocalEmbedTransport {
    fn name(&self) -> &'static str {
        "local_embed"
    }

    fn client_batches(&self) -> bool {
        true
    }

    fn predict(
        &self,
        spec: &SpecialistSpec,
        inputs: &[Value],
    ) -> Result<SpecialistResponse, ProviderError> {
        let opts = LocalEmbedOptions::from_spec(spec)?;
        let texts: Vec<String> = inputs
            .iter()
            .map(|v| {
                v.get("text")
                    .and_then(|t| t.as_str())
                    .unwrap_or("")
                    .to_string()
            })
            .collect();

        let t0 = std::time::Instant::now();
        let model = model_for(&opts)?;
        let embeddings = {
            let mut guard = model.lock();
            guard
                .embed(texts, Some(spec.batch_size.max(1)))
                .map_err(|e| ProviderError::BadResponse(format!("local_embed: {e}")))?
        };
        let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

        let outputs = embeddings
            .into_iter()
            .map(|embedding| {
                Value::Array(
                    embedding
                        .into_iter()
                        .map(|f| {
                            serde_json::Number::from_f64(f as f64)
                                .map(Value::Number)
                                .unwrap_or(Value::Null)
                        })
                        .collect(),
                )
            })
            .collect();

        Ok(SpecialistResponse {
            outputs,
            usage: Vec::new(),
            latency_ms,
        })
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct LocalEmbedOptions {
    model: EmbeddingModel,
    cache_dir: Option<PathBuf>,
    max_length: Option<usize>,
}

impl LocalEmbedOptions {
    fn from_spec(spec: &SpecialistSpec) -> Result<Self, ProviderError> {
        let model_name = spec
            .transport_opts
            .get("model")
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .or_else(|| {
                std::env::var("RVBBIT_LOCAL_EMBED_MODEL")
                    .ok()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
            })
            .unwrap_or_else(|| "bge-small-en-v1.5".to_string());
        let model = parse_model(&model_name).map_err(|e| {
            ProviderError::Config(format!(
                "specialist '{}': invalid local_embed model '{}': {e}",
                spec.name, model_name
            ))
        })?;
        let cache_dir = spec
            .transport_opts
            .get("cache_dir")
            .and_then(|v| v.as_str())
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var("RVBBIT_LOCAL_EMBED_CACHE")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .map(PathBuf::from)
            })
            .or_else(default_embed_cache_dir);
        let max_length = spec
            .transport_opts
            .get("max_length")
            .and_then(|v| v.as_u64())
            .map(|n| n.clamp(8, 8192) as usize);
        Ok(Self {
            model,
            cache_dir,
            max_length,
        })
    }
}

/// Last-resort cache location: `$data_directory/rvbbit/embed_cache/`.
/// Always writable by the postgres user and travels with `pg_basebackup`.
/// Returns `None` if `data_directory` cannot be read (rare; only at very
/// early init).
fn default_embed_cache_dir() -> Option<PathBuf> {
    use std::ffi::{CStr, CString};
    let name = CString::new("data_directory").ok()?;
    let ptr = unsafe { pgrx::pg_sys::GetConfigOption(name.as_ptr(), true, false) };
    if ptr.is_null() {
        return None;
    }
    let dir = unsafe { CStr::from_ptr(ptr).to_string_lossy().into_owned() };
    let mut path = PathBuf::from(dir);
    path.push("rvbbit");
    path.push("embed_cache");
    // fastembed will create subdirs, but the root must exist. If we can't
    // create it (e.g. permissions on a bare-metal install with a weird
    // setup), fall back to None — fastembed will then use its own default.
    std::fs::create_dir_all(&path).ok()?;
    Some(path)
}

type SharedModel = Arc<Mutex<TextEmbedding>>;

static MODEL_CACHE: OnceLock<Mutex<HashMap<LocalEmbedOptions, SharedModel>>> = OnceLock::new();

fn model_cache() -> &'static Mutex<HashMap<LocalEmbedOptions, SharedModel>> {
    MODEL_CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

fn model_for(opts: &LocalEmbedOptions) -> Result<SharedModel, ProviderError> {
    if let Some(model) = model_cache().lock().get(opts).cloned() {
        return Ok(model);
    }

    let mut init = TextInitOptions::new(opts.model.clone()).with_show_download_progress(false);
    if let Some(cache_dir) = &opts.cache_dir {
        init = init.with_cache_dir(cache_dir.clone());
    }
    if let Some(max_length) = opts.max_length {
        init = init.with_max_length(max_length);
    }
    let model = TextEmbedding::try_new(init)
        .map_err(|e| ProviderError::Config(format!("local_embed model init failed: {e}")))?;
    let model = Arc::new(Mutex::new(model));
    model_cache().lock().insert(opts.clone(), model.clone());
    Ok(model)
}

fn parse_model(name: &str) -> Result<EmbeddingModel, String> {
    let normalized = name.trim().to_ascii_lowercase();
    match normalized.as_str() {
        ""
        | "default"
        | "bge-small"
        | "bge-small-en"
        | "bge-small-en-v1.5"
        | "baai/bge-small-en-v1.5" => Ok(EmbeddingModel::BGESmallENV15),
        "bge-small-en-v1.5-q" | "bge-small-q" | "bgesmallenv15q" => {
            Ok(EmbeddingModel::BGESmallENV15Q)
        }
        "all-minilm-l6-v2" | "allminilml6v2" | "sentence-transformers/all-minilm-l6-v2" => {
            Ok(EmbeddingModel::AllMiniLML6V2)
        }
        "all-minilm-l6-v2-q" | "allminilml6v2q" => Ok(EmbeddingModel::AllMiniLML6V2Q),
        "bge-m3" | "baai/bge-m3" => Ok(EmbeddingModel::BGEM3),
        "nomic-embed-text-v1.5" | "nomic-ai/nomic-embed-text-v1.5" => {
            Ok(EmbeddingModel::NomicEmbedTextV15)
        }
        "nomic-embed-text-v1.5-q" | "nomic-embed-text-v15q" => {
            Ok(EmbeddingModel::NomicEmbedTextV15Q)
        }
        other => EmbeddingModel::from_str(other),
    }
}

#[cfg(test)]
mod tests {
    use super::parse_model;
    use fastembed::EmbeddingModel;

    #[test]
    fn parse_common_model_aliases() {
        assert_eq!(
            parse_model("BAAI/bge-small-en-v1.5").unwrap(),
            EmbeddingModel::BGESmallENV15
        );
        assert_eq!(
            parse_model("all-MiniLM-L6-v2").unwrap(),
            EmbeddingModel::AllMiniLML6V2
        );
        assert_eq!(parse_model("bge-m3").unwrap(), EmbeddingModel::BGEM3);
    }
}
