//! Rvbbit-native transport — the minimal POST /predict batch contract.
//!
//! Wire format (request):
//!   POST {endpoint_url}        (the catalog row's URL is the full endpoint,
//!                                conventionally ending in /predict)
//!   Content-Type: application/json
//!   Authorization: Bearer <env>   (if auth_header_env set)
//!   {"inputs": [ {...}, {...}, ... ]}
//!
//! Wire format (response):
//!   200 OK
//!   {"outputs": [ ..., ..., ... ]}   (same length, same order)
//!
//! Any 4xx/5xx is propagated as ProviderError::ApiStatus. A mismatched
//! output length is caught one layer up in predict_batch.

use super::{
    http_client, PredictRequest, PredictResponse, SpecialistResponse, SpecialistSpec, Transport,
};
use crate::providers::ProviderError;
use serde_json::Value;
use std::time::Duration;

pub struct RvbbitTransport;

impl RvbbitTransport {
    pub fn new() -> Self {
        Self
    }
}

impl Transport for RvbbitTransport {
    fn name(&self) -> &'static str {
        "rvbbit"
    }

    fn client_batches(&self) -> bool {
        true
    }

    fn predict(
        &self,
        spec: &SpecialistSpec,
        inputs: &[Value],
    ) -> Result<SpecialistResponse, ProviderError> {
        let body = PredictRequest { inputs };
        let mut req = http_client()
            .post(&spec.endpoint_url)
            .timeout(Duration::from_millis(spec.timeout_ms))
            .json(&body);
        if let Some(token) = spec.auth_token() {
            req = req.bearer_auth(token);
        }

        let t0 = std::time::Instant::now();
        let resp = req.send()?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().unwrap_or_default();
            return Err(ProviderError::ApiStatus {
                status: status.as_u16(),
                body,
            });
        }
        let parsed: PredictResponse = resp.json()?;
        let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

        Ok(SpecialistResponse {
            outputs: parsed.outputs,
            usage: Vec::new(),
            latency_ms,
        })
    }
}
