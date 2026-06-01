//! Managed Python runtime for operator `kind:"python"` nodes.
//!
//! The catalog (`rvbbit.python_envs` + `rvbbit.python_handlers`) owns the
//! desired state. A separate CPython sidecar reconciles env specs into venvs
//! and runs handler code against JSON inputs. This module keeps a thread-safe
//! cache of handler specs so prewarm pool workers can call the sidecar without
//! doing SPI from worker threads.

use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, OnceLock, RwLock};
use std::time::Duration;

use pgrx::prelude::*;
use pgrx::Spi;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pgrx::extension_sql!(
    r#"
CREATE TABLE IF NOT EXISTS rvbbit.python_envs (
    name            text PRIMARY KEY,
    runtime_name    text,
    python_version  text NOT NULL DEFAULT '3.12',
    requirements    text[] NOT NULL DEFAULT ARRAY[]::text[],
    env_hash        text NOT NULL,
    endpoint_url    text,
    timeout_ms      int NOT NULL DEFAULT 1000,
    status          text NOT NULL DEFAULT 'registered',
    status_message  text,
    created_by      oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT python_envs_status_check CHECK (
        status IN ('registered', 'building', 'ready', 'failed', 'disabled')
    ),
    CONSTRAINT python_envs_timeout_check CHECK (timeout_ms BETWEEN 1 AND 600000)
);

CREATE TABLE IF NOT EXISTS rvbbit.python_handlers (
    name          text PRIMARY KEY,
    env_name      text NOT NULL REFERENCES rvbbit.python_envs(name) ON DELETE RESTRICT,
    code          text NOT NULL,
    code_hash     text NOT NULL,
    entrypoint    text NOT NULL DEFAULT 'run',
    description   text,
    created_by    oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT python_handlers_entrypoint_check CHECK (entrypoint ~ '^[A-Za-z_][A-Za-z0-9_]*$')
);

CREATE TABLE IF NOT EXISTS rvbbit.python_runtimes (
    name                  text PRIMARY KEY,
    endpoint_url          text NOT NULL,
    language              text NOT NULL DEFAULT 'python',
    status                text NOT NULL DEFAULT 'ready',
    labels                jsonb NOT NULL DEFAULT '{}'::jsonb,
    runtime_source        text NOT NULL DEFAULT 'manual',
    warren_deployment_id  uuid,
    install_manifest      jsonb NOT NULL DEFAULT '{}'::jsonb,
    health                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by            oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT python_runtimes_name_check CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT python_runtimes_language_check CHECK (language = 'python'),
    CONSTRAINT python_runtimes_status_check CHECK (
        status IN ('starting', 'ready', 'failed', 'disabled')
    ),
    CONSTRAINT python_runtimes_endpoint_check CHECK (endpoint_url ~ '^https?://'),
    CONSTRAINT python_runtimes_labels_is_object CHECK (jsonb_typeof(labels) = 'object'),
    CONSTRAINT python_runtimes_manifest_is_object CHECK (jsonb_typeof(install_manifest) = 'object'),
    CONSTRAINT python_runtimes_health_is_object CHECK (jsonb_typeof(health) = 'object')
);

CREATE OR REPLACE FUNCTION rvbbit.touch_python_envs_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS python_envs_touch_updated_at ON rvbbit.python_envs;
CREATE TRIGGER python_envs_touch_updated_at
    BEFORE UPDATE ON rvbbit.python_envs
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_python_envs_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.touch_python_handlers_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS python_handlers_touch_updated_at ON rvbbit.python_handlers;
CREATE TRIGGER python_handlers_touch_updated_at
    BEFORE UPDATE ON rvbbit.python_handlers
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_python_handlers_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.touch_python_runtimes_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS python_runtimes_touch_updated_at ON rvbbit.python_runtimes;
CREATE TRIGGER python_runtimes_touch_updated_at
    BEFORE UPDATE ON rvbbit.python_runtimes
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_python_runtimes_updated_at();

INSERT INTO rvbbit.settings (key, value)
VALUES ('python_runtime_endpoint', to_jsonb('http://rvbbit-python-runtime:8080/run'::text))
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION rvbbit.python_runtime_endpoint()
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(
        (SELECT value #>> '{}' FROM rvbbit.settings WHERE key = 'python_runtime_endpoint'),
        'http://rvbbit-python-runtime:8080/run'
    )
$$;

CREATE OR REPLACE FUNCTION rvbbit.set_python_runtime_endpoint(endpoint_url text)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    normalized text := nullif(btrim(endpoint_url), '');
BEGIN
    IF normalized IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_python_runtime_endpoint: endpoint_url cannot be empty';
    END IF;
    INSERT INTO rvbbit.settings (key, value, updated_at)
    VALUES ('python_runtime_endpoint', to_jsonb(normalized), clock_timestamp())
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        updated_at = clock_timestamp();
    BEGIN
        PERFORM rvbbit.reload_python_runtime();
    EXCEPTION WHEN undefined_function THEN
        NULL;
    END;
    RETURN normalized;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.python_env_hash(
    python_version text,
    requirements text[]
) RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    normalized_requirements text[];
BEGIN
    normalized_requirements := ARRAY(
        SELECT btrim(req)
        FROM unnest(coalesce(requirements, ARRAY[]::text[])) AS r(req)
        WHERE btrim(req) <> ''
        ORDER BY btrim(req)
    );
    RETURN md5(
        coalesce(python_version, '') || E'\x1f' ||
        coalesce(array_to_string(normalized_requirements, E'\x1e'), '')
    );
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.require_python_admin()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper
    ) AND NOT (
        EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rvbbit_warren')
        AND pg_has_role(current_user, 'rvbbit_warren', 'member')
    ) THEN
        RAISE EXCEPTION 'rvbbit Python runtime DDL requires a superuser or rvbbit_warren role membership in this release';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.register_python_runtime(
    runtime_name text,
    endpoint_url text,
    runtime_status text DEFAULT 'ready',
    runtime_labels jsonb DEFAULT '{}'::jsonb,
    runtime_source text DEFAULT 'manual',
    warren_deployment_id uuid DEFAULT NULL,
    install_manifest jsonb DEFAULT '{}'::jsonb,
    health jsonb DEFAULT '{}'::jsonb,
    set_default boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_name text := nullif(btrim(runtime_name), '');
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
    normalized_status text := coalesce(nullif(btrim(runtime_status), ''), 'ready');
    normalized_source text := coalesce(nullif(btrim(runtime_source), ''), 'manual');
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_python_admin();
    IF normalized_name IS NULL THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: runtime_name cannot be empty';
    END IF;
    IF normalized_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: runtime_name must be an identifier-like name';
    END IF;
    IF normalized_endpoint IS NULL OR normalized_endpoint !~ '^https?://' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: endpoint_url must be an http(s) URL';
    END IF;
    IF normalized_status NOT IN ('starting', 'ready', 'failed', 'disabled') THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: unsupported status "%"', runtime_status;
    END IF;
    IF jsonb_typeof(coalesce(runtime_labels, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: runtime_labels must be a JSON object';
    END IF;
    IF jsonb_typeof(coalesce(install_manifest, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: install_manifest must be a JSON object';
    END IF;
    IF jsonb_typeof(coalesce(health, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: health must be a JSON object';
    END IF;

    INSERT INTO rvbbit.python_runtimes
        (name, endpoint_url, language, status, labels, runtime_source,
         warren_deployment_id, install_manifest, health)
    VALUES
        (normalized_name, normalized_endpoint, 'python', normalized_status,
         coalesce(runtime_labels, '{}'::jsonb), normalized_source,
         register_python_runtime.warren_deployment_id,
         coalesce(install_manifest, '{}'::jsonb), coalesce(health, '{}'::jsonb))
    ON CONFLICT (name) DO UPDATE SET
        endpoint_url = EXCLUDED.endpoint_url,
        status = EXCLUDED.status,
        labels = EXCLUDED.labels,
        runtime_source = EXCLUDED.runtime_source,
        warren_deployment_id = EXCLUDED.warren_deployment_id,
        install_manifest = EXCLUDED.install_manifest,
        health = EXCLUDED.health;

    IF coalesce(set_default, true) AND normalized_status = 'ready' THEN
        PERFORM rvbbit.set_python_runtime_endpoint(normalized_endpoint);
    ELSE
        BEGIN
            PERFORM rvbbit.reload_python_runtime();
        EXCEPTION WHEN undefined_function THEN
            NULL;
        END;
    END IF;

    SELECT to_jsonb(r) INTO row_doc FROM rvbbit.python_runtimes r WHERE r.name = normalized_name;
    RETURN row_doc;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.create_python_env(
    env_name text,
    python_version text DEFAULT '3.12',
    requirements text[] DEFAULT ARRAY[]::text[],
    endpoint_url text DEFAULT NULL,
    timeout_ms int DEFAULT 1000,
    runtime_name text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_name text := nullif(btrim(env_name), '');
    normalized_version text := coalesce(nullif(btrim(python_version), ''), '3.12');
    normalized_requirements text[];
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
    normalized_runtime text := nullif(btrim(runtime_name), '');
    resolved_runtime_endpoint text;
    computed_hash text;
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_python_admin();
    IF normalized_name IS NULL THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: env_name cannot be empty';
    END IF;
    IF normalized_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: env_name must be an identifier-like name';
    END IF;
    normalized_requirements := ARRAY(
        SELECT btrim(req)
        FROM unnest(coalesce(requirements, ARRAY[]::text[])) AS r(req)
        WHERE btrim(req) <> ''
        ORDER BY btrim(req)
    );
    IF EXISTS (
        SELECT 1 FROM unnest(normalized_requirements) AS r(req)
        WHERE req ~ E'[\\r\\n]'
    ) THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: requirements cannot contain newlines';
    END IF;
    IF normalized_runtime IS NOT NULL THEN
        IF normalized_endpoint IS NOT NULL THEN
            RAISE EXCEPTION 'rvbbit.create_python_env: pass endpoint_url or runtime_name, not both';
        END IF;
        SELECT r.endpoint_url INTO resolved_runtime_endpoint
        FROM rvbbit.python_runtimes r
        WHERE r.name = normalized_runtime
          AND r.status = 'ready';
        IF resolved_runtime_endpoint IS NULL THEN
            RAISE EXCEPTION 'rvbbit.create_python_env: python runtime "%" is not registered or ready',
                runtime_name;
        END IF;
    END IF;
    computed_hash := rvbbit.python_env_hash(normalized_version, normalized_requirements);

    INSERT INTO rvbbit.python_envs
        (name, runtime_name, python_version, requirements, env_hash, endpoint_url, timeout_ms,
         status, status_message)
    VALUES
        (normalized_name, normalized_runtime, normalized_version, normalized_requirements,
         computed_hash, normalized_endpoint,
         greatest(coalesce(timeout_ms, 1000), 1), 'registered', NULL)
    ON CONFLICT (name) DO UPDATE SET
        runtime_name = EXCLUDED.runtime_name,
        python_version = EXCLUDED.python_version,
        requirements = EXCLUDED.requirements,
        env_hash = EXCLUDED.env_hash,
        endpoint_url = EXCLUDED.endpoint_url,
        timeout_ms = EXCLUDED.timeout_ms,
        status = 'registered',
        status_message = NULL;

    BEGIN
        PERFORM rvbbit.reload_python_runtime();
    EXCEPTION WHEN undefined_function THEN
        NULL;
    END;

    SELECT to_jsonb(e) INTO row_doc FROM rvbbit.python_envs e WHERE e.name = normalized_name;
    RETURN row_doc;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.create_python_handler(
    handler_name text,
    env_name text,
    code text,
    entrypoint text DEFAULT 'run',
    description text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_handler text := nullif(btrim(handler_name), '');
    normalized_env text := nullif(btrim(env_name), '');
    normalized_entrypoint text := coalesce(nullif(btrim(entrypoint), ''), 'run');
    normalized_code text := coalesce(code, '');
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_python_admin();
    IF normalized_handler IS NULL THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: handler_name cannot be empty';
    END IF;
    IF normalized_handler !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: handler_name must be an identifier-like name';
    END IF;
    IF normalized_env IS NULL OR NOT EXISTS (
        SELECT 1 FROM rvbbit.python_envs WHERE name = normalized_env
    ) THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: unknown env "%"', env_name;
    END IF;
    IF normalized_code = '' THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: code cannot be empty';
    END IF;
    IF normalized_entrypoint !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: entrypoint must be an identifier';
    END IF;

    INSERT INTO rvbbit.python_handlers
        (name, env_name, code, code_hash, entrypoint, description)
    VALUES
        (normalized_handler, normalized_env, normalized_code, md5(normalized_code),
         normalized_entrypoint, description)
    ON CONFLICT (name) DO UPDATE SET
        env_name = EXCLUDED.env_name,
        code = EXCLUDED.code,
        code_hash = EXCLUDED.code_hash,
        entrypoint = EXCLUDED.entrypoint,
        description = EXCLUDED.description;

    BEGIN
        PERFORM rvbbit.reload_python_runtime();
    EXCEPTION WHEN undefined_function THEN
        NULL;
    END;

    SELECT to_jsonb(h) INTO row_doc FROM rvbbit.python_handlers h WHERE h.name = normalized_handler;
    RETURN row_doc;
END
$$;
"#,
    name = "python_runtime_catalog",
    requires = ["rvbbit_bootstrap"],
);

#[derive(Debug, Clone)]
pub struct PythonSpec {
    pub handler_name: String,
    pub env_name: String,
    pub code: String,
    pub code_hash: String,
    pub entrypoint: String,
    pub python_version: String,
    pub requirements: Vec<String>,
    pub env_hash: String,
    pub endpoint_url: String,
    pub timeout_ms: u64,
}

#[derive(Debug)]
pub struct PythonRun {
    pub output: Value,
    pub duration_ms: i32,
}

static SPEC_CACHE: OnceLock<RwLock<HashMap<String, Arc<PythonSpec>>>> = OnceLock::new();

fn cache() -> &'static RwLock<HashMap<String, Arc<PythonSpec>>> {
    SPEC_CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

#[pg_extern]
fn reload_python_runtime() -> i32 {
    let names = match load_handler_names() {
        Ok(n) => n,
        Err(e) => pgrx::error!("rvbbit.reload_python_runtime: {e}"),
    };
    if let Ok(mut w) = cache().write() {
        w.clear();
    }
    let mut loaded = 0;
    for name in names {
        match load_spec_from_spi(&name, None) {
            Ok(spec) => {
                if let Ok(mut w) = cache().write() {
                    w.insert(name, Arc::new(spec));
                }
                loaded += 1;
            }
            Err(e) => pgrx::warning!("rvbbit.reload_python_runtime: {e}"),
        }
    }
    loaded
}

pub fn get_cached_spec(handler_name: &str) -> Option<Arc<PythonSpec>> {
    cache().read().ok()?.get(handler_name).cloned()
}

/// Load a handler spec. Leader calls refresh from SPI so handler/env DDL takes
/// effect immediately; pool workers can only use the pre-warmed cache.
pub fn load_spec(
    handler_name: &str,
    expected_env: Option<&str>,
) -> Result<Arc<PythonSpec>, String> {
    if crate::flow::in_pool_worker() {
        let spec = get_cached_spec(handler_name)
            .ok_or_else(|| format!("python handler '{handler_name}' was not preloaded"))?;
        check_expected_env(&spec, expected_env)?;
        return Ok(spec);
    }

    let spec = load_spec_from_spi(handler_name, expected_env)?;
    let arc = Arc::new(spec);
    if let Ok(mut w) = cache().write() {
        w.insert(handler_name.to_string(), arc.clone());
    }
    Ok(arc)
}

/// Preload every python handler an operator may touch. Call from the leader
/// before dispatching any operator work to the pool.
pub fn warm_operator_specs(steps: Option<&Value>, takes: Option<&Value>) {
    for (handler, expected_env) in collect_python_refs(steps, takes) {
        let _ = load_spec_from_spi(&handler, expected_env.as_deref()).map(|spec| {
            if let Ok(mut w) = cache().write() {
                w.insert(handler, Arc::new(spec));
            }
        });
    }
}

/// Content seed folded into the operator cache key. This is what makes
/// `create_python_handler(...)` or `create_python_env(...)` invalidate old
/// cached operator results without touching the operator row.
pub fn dependency_seed(steps: Option<&Value>, takes: Option<&Value>) -> String {
    let refs = collect_python_refs(steps, takes);
    if refs.is_empty() {
        return String::new();
    }
    let mut parts: Vec<String> = Vec::new();
    for (handler, expected_env) in refs {
        let cached = get_cached_spec(&handler)
            .filter(|spec| check_expected_env(spec, expected_env.as_deref()).is_ok())
            .map(|spec| spec.as_ref().clone());
        match cached.or_else(|| {
            load_spec_from_spi(&handler, expected_env.as_deref())
                .map(|spec| {
                    if let Ok(mut w) = cache().write() {
                        w.insert(handler.clone(), Arc::new(spec.clone()));
                    }
                    spec
                })
                .ok()
        }) {
            Some(spec) => parts.push(format!(
                "{}:{}:{}:{}:{}",
                spec.handler_name,
                spec.code_hash,
                spec.env_name,
                spec.env_hash,
                spec.python_version
            )),
            None => match load_spec_from_spi(&handler, expected_env.as_deref()) {
                Ok(spec) => parts.push(format!(
                    "{}:{}:{}:{}:{}",
                    spec.handler_name,
                    spec.code_hash,
                    spec.env_name,
                    spec.env_hash,
                    spec.python_version
                )),
                Err(e) => parts.push(format!("{handler}:error:{e}")),
            },
        }
    }
    parts.sort();
    parts.join("\0")
}

pub fn run(
    spec: &PythonSpec,
    inputs: &Value,
    timeout_override_ms: Option<u64>,
) -> Result<PythonRun, String> {
    let timeout_ms = timeout_override_ms.unwrap_or(spec.timeout_ms).max(1);
    let req = RunRequest {
        env: EnvRequest {
            name: &spec.env_name,
            python_version: &spec.python_version,
            requirements: &spec.requirements,
            env_hash: &spec.env_hash,
        },
        handler: HandlerRequest {
            name: &spec.handler_name,
            code: &spec.code,
            code_hash: &spec.code_hash,
            entrypoint: &spec.entrypoint,
        },
        inputs,
        timeout_ms,
    };

    let http_timeout = Duration::from_millis(timeout_ms.saturating_add(5_000));
    let resp = crate::specialists::http_client()
        .post(&spec.endpoint_url)
        .timeout(http_timeout)
        .json(&req)
        .send()
        .map_err(|e| format!("python sidecar request failed: {e}"))?;
    let status = resp.status();
    let body = resp
        .text()
        .map_err(|e| format!("python sidecar response read failed: {e}"))?;
    if !status.is_success() {
        return Err(format!(
            "python sidecar returned HTTP {}: {}",
            status.as_u16(),
            truncate(&body, 500)
        ));
    }
    let out: RunResponse = serde_json::from_str(&body).map_err(|e| {
        format!(
            "python sidecar returned invalid JSON: {e}: {}",
            truncate(&body, 500)
        )
    })?;
    if !out.ok {
        return Err(out
            .error
            .unwrap_or_else(|| "python handler failed".to_string()));
    }
    Ok(PythonRun {
        output: out.output,
        duration_ms: out.duration_ms.unwrap_or(0).max(0).min(i32::MAX as i64) as i32,
    })
}

pub fn label(spec: &PythonSpec) -> String {
    format!(
        "{}/{}@{}",
        spec.env_name,
        spec.handler_name,
        short_hash(&spec.code_hash)
    )
}

fn collect_python_refs(
    steps: Option<&Value>,
    takes: Option<&Value>,
) -> Vec<(String, Option<String>)> {
    let mut refs: BTreeSet<(String, Option<String>)> = BTreeSet::new();
    collect_python_refs_from_nodes(steps, &mut refs);
    collect_python_refs_from_nodes(takes.and_then(|t| t.get("nodes")), &mut refs);
    refs.into_iter().collect()
}

fn collect_python_refs_from_nodes(
    nodes: Option<&Value>,
    out: &mut BTreeSet<(String, Option<String>)>,
) {
    let Some(arr) = nodes.and_then(|n| n.as_array()) else {
        return;
    };
    for node in arr {
        if node.get("kind").and_then(|k| k.as_str()) != Some("python") {
            continue;
        }
        let Some(handler) = node.get("handler").and_then(|h| h.as_str()) else {
            continue;
        };
        let expected_env = node
            .get("env")
            .and_then(|e| e.as_str())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string());
        out.insert((handler.to_string(), expected_env));
    }
}

fn load_handler_names() -> Result<Vec<String>, String> {
    let mut names = Vec::new();
    Spi::connect(|client| {
        let table = client.select(
            "SELECT name FROM rvbbit.python_handlers ORDER BY name",
            None,
            &[],
        )?;
        for row in table {
            if let Some(name) = row.get::<String>(1)? {
                names.push(name);
            }
        }
        Ok::<(), pgrx::spi::Error>(())
    })
    .map_err(|e| e.to_string())?;
    Ok(names)
}

fn load_spec_from_spi(
    handler_name: &str,
    expected_env: Option<&str>,
) -> Result<PythonSpec, String> {
    let escaped = handler_name.replace('\'', "''");
    let sql = format!(
        "SELECT h.name, h.env_name, h.code, h.code_hash, h.entrypoint, \
                e.python_version, e.requirements, e.env_hash, \
                coalesce(r.endpoint_url, e.endpoint_url, rvbbit.python_runtime_endpoint()) AS endpoint_url, \
                e.timeout_ms \
         FROM rvbbit.python_handlers h \
         JOIN rvbbit.python_envs e ON e.name = h.env_name \
         LEFT JOIN rvbbit.python_runtimes r ON r.name = e.runtime_name \
         WHERE h.name = '{escaped}' \
           AND e.status <> 'disabled' \
           AND (e.runtime_name IS NULL OR r.status = 'ready')"
    );
    let mut result: Option<PythonSpec> = None;
    Spi::connect(|client| {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let handler: Option<String> = row.get(1)?;
            let env_name: Option<String> = row.get(2)?;
            let code: Option<String> = row.get(3)?;
            let code_hash: Option<String> = row.get(4)?;
            let entrypoint: Option<String> = row.get(5)?;
            let python_version: Option<String> = row.get(6)?;
            let requirements: Option<Vec<Option<String>>> = row.get(7)?;
            let env_hash: Option<String> = row.get(8)?;
            let endpoint_url: Option<String> = row.get(9)?;
            let timeout_ms: Option<i32> = row.get(10)?;
            if let (
                Some(handler_name),
                Some(env_name),
                Some(code),
                Some(code_hash),
                Some(entrypoint),
                Some(python_version),
                Some(env_hash),
                Some(endpoint_url),
            ) = (
                handler,
                env_name,
                code,
                code_hash,
                entrypoint,
                python_version,
                env_hash,
                endpoint_url,
            ) {
                let requirements = requirements
                    .unwrap_or_default()
                    .into_iter()
                    .flatten()
                    .collect();
                result = Some(PythonSpec {
                    handler_name,
                    env_name,
                    code,
                    code_hash,
                    entrypoint,
                    python_version,
                    requirements,
                    env_hash,
                    endpoint_url: runtime_endpoint_override(endpoint_url),
                    timeout_ms: timeout_ms.unwrap_or(1000).max(1) as u64,
                });
            }
        }
        Ok::<(), pgrx::spi::Error>(())
    })
    .map_err(|e| e.to_string())?;

    let spec =
        result.ok_or_else(|| format!("python handler '{handler_name}' is not registered"))?;
    check_expected_env(&spec, expected_env)?;
    Ok(spec)
}

fn check_expected_env(spec: &PythonSpec, expected_env: Option<&str>) -> Result<(), String> {
    if let Some(expected) = expected_env {
        if expected != spec.env_name {
            return Err(format!(
                "python handler '{}' is bound to env '{}', but node requested env '{}'",
                spec.handler_name, spec.env_name, expected
            ));
        }
    }
    Ok(())
}

fn runtime_endpoint_override(db_endpoint: String) -> String {
    std::env::var("RVBBIT_PYTHON_RUNTIME_URL")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or(db_endpoint)
}

fn short_hash(hash: &str) -> String {
    hash.chars().take(8).collect()
}

fn truncate(s: &str, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        return s.to_string();
    }
    let mut out: String = s.chars().take(max_chars).collect();
    out.push_str("...");
    out
}

#[derive(Serialize)]
struct RunRequest<'a> {
    env: EnvRequest<'a>,
    handler: HandlerRequest<'a>,
    inputs: &'a Value,
    timeout_ms: u64,
}

#[derive(Serialize)]
struct EnvRequest<'a> {
    name: &'a str,
    python_version: &'a str,
    requirements: &'a [String],
    env_hash: &'a str,
}

#[derive(Serialize)]
struct HandlerRequest<'a> {
    name: &'a str,
    code: &'a str,
    code_hash: &'a str,
    entrypoint: &'a str,
}

#[derive(Deserialize)]
struct RunResponse {
    ok: bool,
    #[serde(default)]
    output: Value,
    error: Option<String>,
    duration_ms: Option<i64>,
}
