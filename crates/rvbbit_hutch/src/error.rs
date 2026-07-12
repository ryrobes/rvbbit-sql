//! Error responses. These bodies transit pg_rvbbit as
//! ProviderError::ApiStatus{status, body} and surface in operator
//! degradation paths and receipts — so every message is written for the
//! HUMAN who will read it there, and every code is stable for machines.

use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

pub struct HutchError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
    pub retry_after_ms: Option<u64>,
}

impl HutchError {
    pub fn invalid_key() -> Self {
        Self {
            status: StatusCode::UNAUTHORIZED,
            code: "invalid_key",
            message: "hutch: no valid API key (Authorization: Bearer or X-Rvbbit-Token). \
                      Check the env var named by this backend's auth_header_env."
                .into(),
            retry_after_ms: None,
        }
    }

    pub fn subscription_expired(tenant: &str) -> Self {
        Self {
            status: StatusCode::FORBIDDEN,
            code: "subscription_expired",
            message: format!(
                "hutch: subscription for '{tenant}' has expired — operators degrade gracefully; \
                 renew to restore managed inference"
            ),
            retry_after_ms: None,
        }
    }

    pub fn not_entitled(tenant: &str, backend: &str, entitlement: &str) -> Self {
        Self {
            status: StatusCode::FORBIDDEN,
            code: "not_entitled",
            message: format!(
                "hutch: '{tenant}' is not subscribed to '{entitlement}' (backend '{backend}')"
            ),
            retry_after_ms: None,
        }
    }

    pub fn unknown_model(model: &str) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            code: "unknown_model",
            message: format!(
                "hutch: no hosted model named '{model}' — GET /v1/models lists what your key can use"
            ),
            retry_after_ms: None,
        }
    }

    pub fn unknown_backend(backend: &str) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            code: "unknown_backend",
            message: format!("hutch: no backend named '{backend}' on this gateway"),
            retry_after_ms: None,
        }
    }

    pub fn lanes_saturated(tenant: &str, lanes: usize) -> Self {
        Self {
            status: StatusCode::TOO_MANY_REQUESTS,
            code: "lanes_saturated",
            message: format!(
                "hutch: all {lanes} lanes for '{tenant}' are in flight — retry shortly \
                 or raise the lane count on your plan"
            ),
            retry_after_ms: Some(500),
        }
    }

    pub fn upstream(backend: &str, detail: String) -> Self {
        Self {
            status: StatusCode::BAD_GATEWAY,
            code: "upstream_error",
            message: format!("hutch: backend '{backend}' failed upstream: {detail}"),
            retry_after_ms: None,
        }
    }

    pub fn bad_request(detail: String) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "bad_request",
            message: format!("hutch: {detail}"),
            retry_after_ms: None,
        }
    }
}

impl IntoResponse for HutchError {
    fn into_response(self) -> Response {
        let body = Json(json!({
            "error": {
                "code": self.code,
                "message": self.message,
                "retry_after_ms": self.retry_after_ms,
            }
        }));
        let mut resp = (self.status, body).into_response();
        if let Some(ms) = self.retry_after_ms {
            let secs = ms.div_ceil(1000).max(1);
            if let Ok(v) = HeaderValue::from_str(&secs.to_string()) {
                resp.headers_mut().insert(header::RETRY_AFTER, v);
            }
        }
        resp
    }
}
