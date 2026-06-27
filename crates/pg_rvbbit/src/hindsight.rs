//! Hindsight memory-service integration.
//!
//! Hindsight remains an external service. Rvbbit stores only endpoint metadata
//! and exposes thin SQL wrappers for manual retain/recall/reflect calls.

use std::time::Duration;

use pgrx::{extension_sql_file, prelude::*, JsonB, Spi};
use serde_json::{json, Map, Value};

extension_sql_file!(
    "../sql/hindsight.sql",
    name = "hindsight_memory_service",
    requires = ["rvbbit_bootstrap"]
);

#[derive(Debug)]
struct HindsightService {
    endpoint_url: String,
    auth_header_env: Option<String>,
}

fn sql_lit(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

fn path_segment(raw: &str) -> Result<String, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("path segment cannot be empty".into());
    }
    let mut out = String::with_capacity(trimmed.len());
    for b in trimmed.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => {
                use std::fmt::Write;
                let _ = write!(out, "%{b:02X}");
            }
        }
    }
    Ok(out)
}

fn object_from_jsonb(value: JsonB, context: &str) -> Result<Map<String, Value>, String> {
    match value.0 {
        Value::Object(map) => Ok(map),
        _ => Err(format!("{context} must be a JSON object")),
    }
}

fn resolve_hindsight_service(service_name: &str) -> Result<HindsightService, String> {
    let arg = if service_name.trim().is_empty() {
        "NULL".to_string()
    } else {
        sql_lit(service_name.trim())
    };
    let row: Option<JsonB> = Spi::get_one(&format!("SELECT rvbbit.hindsight_service({arg})"))
        .map_err(|e| format!("resolve Hindsight service: {e}"))?;
    let doc = row.map(|j| j.0).ok_or_else(|| {
        if service_name.trim().is_empty() {
            "no ready Hindsight memory service is registered".to_string()
        } else {
            format!(
                "Hindsight memory service {:?} is not registered",
                service_name
            )
        }
    })?;
    let endpoint_url = doc
        .get("endpoint_url")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "registered Hindsight service has no endpoint_url".to_string())?
        .trim_end_matches('/')
        .to_string();
    let auth_header_env = doc
        .get("auth_header_env")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    Ok(HindsightService {
        endpoint_url,
        auth_header_env,
    })
}

fn with_optional_bearer(
    req: reqwest::blocking::RequestBuilder,
    service: &HindsightService,
) -> reqwest::blocking::RequestBuilder {
    match service
        .auth_header_env
        .as_deref()
        .and_then(|env_name| std::env::var(env_name).ok())
        .filter(|token| !token.trim().is_empty())
    {
        Some(token) => req.bearer_auth(token),
        None => req,
    }
}

fn parse_response(
    status: reqwest::StatusCode,
    body: String,
    endpoint: &str,
) -> Result<Value, String> {
    if !status.is_success() {
        let preview: String = body.chars().take(800).collect();
        return Err(format!(
            "Hindsight HTTP {} from {}: {}",
            status.as_u16(),
            endpoint,
            preview
        ));
    }
    serde_json::from_str(&body).or_else(|_| Ok(json!({ "body": body })))
}

fn hindsight_get(
    service: &HindsightService,
    path: &str,
    timeout: Duration,
) -> Result<Value, String> {
    let endpoint = format!("{}{}", service.endpoint_url, path);
    let resp = with_optional_bearer(
        crate::specialists::http_client()
            .get(&endpoint)
            .timeout(timeout),
        service,
    )
    .send()
    .map_err(|e| format!("GET {endpoint} failed: {e}"))?;
    let status = resp.status();
    let body = resp
        .text()
        .map_err(|e| format!("GET {endpoint} response read failed: {e}"))?;
    parse_response(status, body, &endpoint)
}

fn hindsight_post(
    service: &HindsightService,
    path: &str,
    body: &Value,
    timeout: Duration,
) -> Result<Value, String> {
    let endpoint = format!("{}{}", service.endpoint_url, path);
    let resp = with_optional_bearer(
        crate::specialists::http_client()
            .post(&endpoint)
            .timeout(timeout)
            .json(body),
        service,
    )
    .send()
    .map_err(|e| format!("POST {endpoint} failed: {e}"))?;
    let status = resp.status();
    let text = resp
        .text()
        .map_err(|e| format!("POST {endpoint} response read failed: {e}"))?;
    parse_response(status, text, &endpoint)
}

fn bank_path(bank_id: &str, suffix: &str) -> Result<String, String> {
    Ok(format!(
        "/v1/default/banks/{}{}",
        path_segment(bank_id)?,
        suffix
    ))
}

#[pg_extern(volatile)]
fn hindsight_status(service_name: default!(&str, "''")) -> JsonB {
    let result = (|| {
        let service = resolve_hindsight_service(service_name)?;
        hindsight_get(&service, "/health", Duration::from_secs(10))
    })()
    .unwrap_or_else(|e| pgrx::error!("rvbbit.hindsight_status: {e}"));
    JsonB(result)
}

#[pg_extern(volatile)]
fn hindsight_retain(
    bank_id: &str,
    content: &str,
    options: default!(JsonB, "'{}'::jsonb"),
    service_name: default!(&str, "''"),
    async_mode: default!(bool, "true"),
) -> JsonB {
    let result = (|| {
        let service = resolve_hindsight_service(service_name)?;
        let mut item = object_from_jsonb(options, "options")?;
        let document_tags = item.remove("document_tags");
        item.insert("content".into(), Value::String(content.to_string()));

        let mut body = Map::new();
        body.insert("items".into(), Value::Array(vec![Value::Object(item)]));
        body.insert("async".into(), Value::Bool(async_mode));
        if let Some(tags) = document_tags {
            body.insert("document_tags".into(), tags);
        }

        let path = bank_path(bank_id, "/memories")?;
        hindsight_post(
            &service,
            &path,
            &Value::Object(body),
            Duration::from_secs(900),
        )
    })()
    .unwrap_or_else(|e| pgrx::error!("rvbbit.hindsight_retain: {e}"));
    JsonB(result)
}

#[pg_extern(volatile)]
fn hindsight_recall(
    bank_id: &str,
    query: &str,
    options: default!(JsonB, "'{}'::jsonb"),
    service_name: default!(&str, "''"),
) -> JsonB {
    let result = (|| {
        let service = resolve_hindsight_service(service_name)?;
        let mut body = object_from_jsonb(options, "options")?;
        body.insert("query".into(), Value::String(query.to_string()));
        let path = bank_path(bank_id, "/memories/recall")?;
        hindsight_post(
            &service,
            &path,
            &Value::Object(body),
            Duration::from_secs(120),
        )
    })()
    .unwrap_or_else(|e| pgrx::error!("rvbbit.hindsight_recall: {e}"));
    JsonB(result)
}

#[pg_extern(volatile)]
fn hindsight_reflect(
    bank_id: &str,
    query: &str,
    options: default!(JsonB, "'{}'::jsonb"),
    service_name: default!(&str, "''"),
) -> JsonB {
    let result = (|| {
        let service = resolve_hindsight_service(service_name)?;
        let mut body = object_from_jsonb(options, "options")?;
        body.insert("query".into(), Value::String(query.to_string()));
        let path = bank_path(bank_id, "/reflect")?;
        hindsight_post(
            &service,
            &path,
            &Value::Object(body),
            Duration::from_secs(180),
        )
    })()
    .unwrap_or_else(|e| pgrx::error!("rvbbit.hindsight_reflect: {e}"));
    JsonB(result)
}

#[cfg(any(test, feature = "pg_test"))]
#[pgrx::pg_schema]
mod tests {
    use super::sql_lit;
    use pgrx::{prelude::*, JsonB};
    use serde_json::{json, Value};
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::sync::mpsc::{self, Receiver};
    use std::thread::{self, JoinHandle};
    use std::time::Duration;

    #[derive(Debug)]
    struct ObservedRequest {
        method: String,
        path: String,
        authorization: Option<String>,
        body: String,
    }

    struct MockHindsight {
        endpoint: String,
        expected: usize,
        rx: Receiver<ObservedRequest>,
        handle: JoinHandle<()>,
    }

    impl MockHindsight {
        fn start(expected: usize) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let endpoint = format!("http://{}", listener.local_addr().unwrap());
            let (tx, rx) = mpsc::channel();
            let handle = thread::spawn(move || {
                for _ in 0..expected {
                    let (mut stream, _) = listener.accept().unwrap();
                    let request = read_http_request(&mut stream);
                    let body = response_body_for(&request.path);
                    let response = format!(
                        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    tx.send(request).unwrap();
                    stream.write_all(response.as_bytes()).unwrap();
                }
            });
            Self {
                endpoint,
                expected,
                rx,
                handle,
            }
        }

        fn finish(self) -> Vec<ObservedRequest> {
            let mut requests = Vec::with_capacity(self.expected);
            for _ in 0..self.expected {
                requests.push(
                    self.rx
                        .recv_timeout(Duration::from_secs(10))
                        .expect("mock Hindsight request"),
                );
            }
            self.handle.join().expect("mock Hindsight thread");
            requests
        }
    }

    fn read_http_request(stream: &mut TcpStream) -> ObservedRequest {
        stream
            .set_read_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        let mut bytes = Vec::new();
        let mut buf = [0u8; 4096];
        loop {
            let n = stream.read(&mut buf).unwrap();
            assert!(n > 0, "connection closed before request headers");
            bytes.extend_from_slice(&buf[..n]);
            if header_end(&bytes).is_some() {
                break;
            }
        }

        let header_end = header_end(&bytes).unwrap();
        let headers = String::from_utf8_lossy(&bytes[..header_end]).to_string();
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                if name.eq_ignore_ascii_case("content-length") {
                    value.trim().parse::<usize>().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0);
        let target_len = header_end + 4 + content_length;
        while bytes.len() < target_len {
            let n = stream.read(&mut buf).unwrap();
            assert!(n > 0, "connection closed before request body");
            bytes.extend_from_slice(&buf[..n]);
        }

        let mut first = headers.lines().next().unwrap_or("").split_whitespace();
        let method = first.next().unwrap_or("").to_string();
        let path = first.next().unwrap_or("").to_string();
        let authorization = headers.lines().find_map(|line| {
            let (name, value) = line.split_once(':')?;
            if name.eq_ignore_ascii_case("authorization") {
                Some(value.trim().to_string())
            } else {
                None
            }
        });
        let body = String::from_utf8_lossy(&bytes[header_end + 4..target_len]).to_string();
        ObservedRequest {
            method,
            path,
            authorization,
            body,
        }
    }

    fn header_end(bytes: &[u8]) -> Option<usize> {
        bytes.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn response_body_for(path: &str) -> String {
        if path == "/health" {
            json!({"status": "ok"}).to_string()
        } else if path.ends_with("/memories/recall") {
            json!({
                "ok": true,
                "memories": [{"content": "Hermes remembered the blue dashboard"}]
            })
            .to_string()
        } else if path.ends_with("/reflect") {
            json!({"ok": true, "answer": "The dashboard was blue."}).to_string()
        } else if path.ends_with("/memories") {
            json!({"ok": true, "stored": 1}).to_string()
        } else {
            json!({"ok": true}).to_string()
        }
    }

    #[pg_test]
    fn hindsight_embedding_env_maps_openai_and_openrouter_backends() {
        Spi::run(
            "INSERT INTO rvbbit.backends \
             (name, transport, endpoint_url, auth_header_env, transport_opts, source_provider, source_model, install_manifest) \
             VALUES \
             ('hindsight_openai_embed', 'openai', 'https://proxy.example/v1/embeddings', 'OPENAI_API_KEY', \
              '{\"dimensions\":1536}'::jsonb, 'openai', 'text-embedding-3-small', \
              '{\"rvbbit_default_embedder\":{\"source_backend\":\"embed\"}}'::jsonb), \
             ('hindsight_openrouter_embed', 'openai', 'https://openrouter.ai/api/v1/embeddings', 'OPENROUTER_API_KEY', \
              '{}'::jsonb, 'openrouter', 'openai/text-embedding-3-small', '{}'::jsonb) \
             ON CONFLICT (name) DO UPDATE SET \
               transport = EXCLUDED.transport, endpoint_url = EXCLUDED.endpoint_url, \
               auth_header_env = EXCLUDED.auth_header_env, transport_opts = EXCLUDED.transport_opts, \
               source_provider = EXCLUDED.source_provider, source_model = EXCLUDED.source_model, \
               install_manifest = EXCLUDED.install_manifest",
        )
        .unwrap();

        let openai: JsonB =
            Spi::get_one("SELECT rvbbit.hindsight_embedding_env('hindsight_openai_embed')")
                .unwrap()
                .unwrap();
        assert_eq!(openai.0["compatible"], true);
        assert_eq!(
            openai.0["env"]["HINDSIGHT_API_EMBEDDINGS_PROVIDER"],
            "openai"
        );
        assert_eq!(
            openai.0["env"]["HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL"],
            "text-embedding-3-small"
        );
        assert_eq!(
            openai.0["env"]["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"],
            "${OPENAI_API_KEY:-}"
        );
        assert_eq!(
            openai.0["env"]["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"],
            "https://proxy.example/v1"
        );
        assert_eq!(
            openai.0["env"]["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"],
            "1536"
        );
        assert_eq!(openai.0["source_backend"], "embed");

        let openrouter: JsonB =
            Spi::get_one("SELECT rvbbit.hindsight_embedding_env('hindsight_openrouter_embed')")
                .unwrap()
                .unwrap();
        assert_eq!(openrouter.0["compatible"], true);
        assert_eq!(
            openrouter.0["env"]["HINDSIGHT_API_EMBEDDINGS_PROVIDER"],
            "openrouter"
        );
        assert_eq!(
            openrouter.0["env"]["HINDSIGHT_API_EMBEDDINGS_OPENROUTER_MODEL"],
            "openai/text-embedding-3-small"
        );
        assert_eq!(
            openrouter.0["env"]["HINDSIGHT_API_EMBEDDINGS_OPENROUTER_API_KEY"],
            "${OPENROUTER_API_KEY:-}"
        );
    }

    #[pg_test]
    fn hindsight_sql_wrappers_call_registered_service() {
        let mock = MockHindsight::start(4);
        std::env::set_var("HINDSIGHT_TEST_TOKEN", "test-token");

        Spi::run(&format!(
            "SELECT rvbbit.register_memory_service(\
                 service_name => 'hindsight_unit', \
                 endpoint_url => {}, \
                 service_provider => 'hindsight', \
                 auth_header_env => 'HINDSIGHT_TEST_TOKEN', \
                 service_status => 'ready', \
                 service_source => 'test', \
                 set_default => true)",
            sql_lit(&mock.endpoint)
        ))
        .unwrap();

        let status: JsonB = Spi::get_one("SELECT rvbbit.hindsight_status('hindsight_unit')")
            .unwrap()
            .unwrap();
        assert_eq!(status.0["status"], "ok");

        let retain: JsonB = Spi::get_one(
            "SELECT rvbbit.hindsight_retain(\
                 'unit_bank', \
                 'Hermes saw a blue dashboard', \
                 '{\"source\":\"pg_test\",\"document_tags\":{\"tenant\":\"acme\"}}'::jsonb, \
                 'hindsight_unit', \
                 false)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(retain.0["stored"], 1);

        let recall: JsonB = Spi::get_one(
            "SELECT rvbbit.hindsight_recall(\
                 'unit_bank', \
                 'what color was the dashboard?', \
                 '{\"top_k\":2}'::jsonb, \
                 'hindsight_unit')",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            recall.0["memories"][0]["content"],
            "Hermes remembered the blue dashboard"
        );

        let reflect: JsonB = Spi::get_one(
            "SELECT rvbbit.hindsight_reflect(\
                 'unit_bank', \
                 'summarize the dashboard memory', \
                 '{}'::jsonb, \
                 'hindsight_unit')",
        )
        .unwrap()
        .unwrap();
        assert_eq!(reflect.0["answer"], "The dashboard was blue.");

        let requests = mock.finish();
        assert_eq!(requests[0].method, "GET");
        assert_eq!(requests[0].path, "/health");
        assert_eq!(requests[1].method, "POST");
        assert_eq!(requests[1].path, "/v1/default/banks/unit_bank/memories");
        assert_eq!(
            requests[2].path,
            "/v1/default/banks/unit_bank/memories/recall"
        );
        assert_eq!(requests[3].path, "/v1/default/banks/unit_bank/reflect");
        for request in &requests {
            assert_eq!(request.authorization.as_deref(), Some("Bearer test-token"));
        }

        let retain_body: Value = serde_json::from_str(&requests[1].body).unwrap();
        assert_eq!(
            retain_body["items"][0]["content"],
            "Hermes saw a blue dashboard"
        );
        assert_eq!(retain_body["items"][0]["source"], "pg_test");
        assert_eq!(retain_body["document_tags"]["tenant"], "acme");
        assert_eq!(retain_body["async"], false);

        let recall_body: Value = serde_json::from_str(&requests[2].body).unwrap();
        assert_eq!(recall_body["query"], "what color was the dashboard?");
        assert_eq!(recall_body["top_k"], 2);
    }
}
