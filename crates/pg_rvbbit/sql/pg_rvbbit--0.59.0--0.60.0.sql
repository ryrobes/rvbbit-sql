-- pg_rvbbit 0.59.0 -> 0.60.0
-- Managed Python runtime catalog for operator `kind: python` nodes.

CREATE TABLE IF NOT EXISTS rvbbit.python_envs (
    name            text PRIMARY KEY,
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
    ) THEN
        RAISE EXCEPTION 'rvbbit Python runtime DDL requires a superuser in this release';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.create_python_env(
    env_name text,
    python_version text DEFAULT '3.12',
    requirements text[] DEFAULT ARRAY[]::text[],
    endpoint_url text DEFAULT NULL,
    timeout_ms int DEFAULT 1000
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_name text := nullif(btrim(env_name), '');
    normalized_version text := coalesce(nullif(btrim(python_version), ''), '3.12');
    normalized_requirements text[];
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
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
    computed_hash := rvbbit.python_env_hash(normalized_version, normalized_requirements);

    INSERT INTO rvbbit.python_envs
        (name, python_version, requirements, env_hash, endpoint_url, timeout_ms,
         status, status_message)
    VALUES
        (normalized_name, normalized_version, normalized_requirements, computed_hash,
         normalized_endpoint, greatest(coalesce(timeout_ms, 1000), 1), 'registered', NULL)
    ON CONFLICT (name) DO UPDATE SET
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

-- Lance text acceleration -----------------------------------------------------

CREATE TABLE IF NOT EXISTS rvbbit.lance_text_indexes (
    table_oid      oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    column_name    text NOT NULL,
    specialist     text NOT NULL DEFAULT 'embed',
    lance_url      text NOT NULL,
    dim            int NOT NULL,
    n_values       bigint NOT NULL DEFAULT 0,
    status         text NOT NULL DEFAULT 'ready',
    status_message text,
    refreshed_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (table_oid, column_name, specialist),
    CONSTRAINT lance_text_indexes_status_check CHECK (
        status IN ('ready', 'refreshing', 'failed', 'disabled')
    )
);

CREATE OR REPLACE FUNCTION rvbbit.lance_enable_text(
    reloid oid,
    col text,
    lance_url text,
    specialist text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'lance_enable_text_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.lance_refresh_text(
    reloid oid,
    col text,
    specialist text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'lance_refresh_text_wrapper';

-- Knowledge graph Lance acceleration -----------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.kg_normalize_label(value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
BEGIN
    RETURN regexp_replace(lower(btrim(value)), '\s+', ' ', 'g');
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.kg_normalize_predicate(value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
BEGIN
    RETURN regexp_replace(lower(btrim(value)), '\s+', '_', 'g');
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.kg_normalize_graph(value text DEFAULT NULL)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN COALESCE(NULLIF(regexp_replace(lower(btrim(value)), '\s+', '_', 'g'), ''), 'default');
END
$$;

CREATE TABLE IF NOT EXISTS rvbbit.kg_lance_indexes (
    graph_id       text NOT NULL DEFAULT 'default',
    kind           text NOT NULL,
    target         text NOT NULL DEFAULT 'nodes',
    specialist     text NOT NULL DEFAULT 'embed',
    lance_url      text NOT NULL,
    dim            int NOT NULL DEFAULT 0,
    n_values       bigint NOT NULL DEFAULT 0,
    status         text NOT NULL DEFAULT 'ready',
    status_message text,
    refreshed_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (graph_id, kind, target, specialist),
    CONSTRAINT kg_lance_indexes_target_check CHECK (target IN ('nodes')),
    CONSTRAINT kg_lance_indexes_status_check CHECK (
        status IN ('ready', 'refreshing', 'failed', 'disabled')
    )
);

CREATE INDEX IF NOT EXISTS kg_lance_indexes_status_idx
    ON rvbbit.kg_lance_indexes(graph_id, kind, target, status);

CREATE OR REPLACE FUNCTION rvbbit.kg_lance_enable(
    node_kind text,
    graph text DEFAULT '',
    specialist text DEFAULT '',
    lance_url text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'kg_lance_enable_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.kg_lance_refresh(
    node_kind text,
    graph text DEFAULT '',
    specialist text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'kg_lance_refresh_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.kg_lance_resolve_nodes(
    node_kind text,
    node_label text,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT '',
    limit_count integer DEFAULT 10
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'kg_lance_resolve_nodes_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.kg_resolve_node(
    node_kind text,
    node_label text,
    specialist text DEFAULT '',
    match_threshold double precision DEFAULT 0.92,
    graph text DEFAULT NULL
) RETURNS TABLE (
    node_id bigint,
    kind text,
    label text,
    score double precision,
    match_method text
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    norm_kind text;
    norm_label text;
    norm_graph text;
BEGIN
    IF node_kind IS NULL OR btrim(node_kind) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_resolve_node: node_kind must be non-empty';
    END IF;
    IF node_label IS NULL OR btrim(node_label) = '' THEN
        RAISE EXCEPTION 'rvbbit.kg_resolve_node: node_label must be non-empty';
    END IF;
    IF match_threshold IS NULL THEN
        match_threshold := 0.92;
    END IF;
    IF match_threshold < 0.0 OR match_threshold > 1.0 THEN
        RAISE EXCEPTION 'rvbbit.kg_resolve_node: match_threshold must be between 0 and 1';
    END IF;

    norm_kind := rvbbit.kg_normalize_label(node_kind);
    norm_label := rvbbit.kg_normalize_label(node_label);
    norm_graph := rvbbit.kg_normalize_graph(graph);

    RETURN QUERY
    SELECT n.node_id, n.kind, n.label, 1.0::double precision, 'alias'::text
    FROM rvbbit.kg_aliases a
    JOIN rvbbit.kg_nodes n ON n.node_id = a.node_id
    WHERE a.graph_id = norm_graph
      AND n.graph_id = norm_graph
      AND a.kind = norm_kind
      AND a.alias_norm = norm_label
    ORDER BY a.confidence DESC, n.node_id
    LIMIT 1;

    IF FOUND THEN
        RETURN;
    END IF;

    IF match_threshold > 0.0 THEN
        RETURN QUERY
        SELECT (r.doc->>'node_id')::bigint,
               r.doc->>'kind',
               r.doc->>'label',
               (r.doc->>'score')::double precision,
               'lance'::text
        FROM jsonb_array_elements(
            rvbbit.kg_lance_resolve_nodes(
                norm_kind,
                node_label,
                specialist,
                match_threshold,
                norm_graph,
                10
            )
        ) AS r(doc)
        ORDER BY (r.doc->>'score')::double precision DESC,
                 (r.doc->>'node_id')::bigint
        LIMIT 10;

        IF FOUND THEN
            RETURN;
        END IF;

        RETURN QUERY
        SELECT n.node_id, n.kind, n.label, s.score, 'embedding'::text
        FROM rvbbit.kg_nodes n
        CROSS JOIN LATERAL (
            SELECT rvbbit.similarity(node_label, n.label, specialist) AS score
        ) s
        WHERE n.graph_id = norm_graph
          AND n.kind = norm_kind
          AND s.score >= match_threshold
        ORDER BY s.score DESC, n.node_id
        LIMIT 10;
    END IF;
END $$;

-- Validated layout variants ---------------------------------------------------

CREATE TABLE IF NOT EXISTS rvbbit.row_group_variants (
    table_oid       oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    layout          text NOT NULL,
    rg_id           bigint NOT NULL,
    path            text NOT NULL,
    n_rows          bigint NOT NULL,
    n_bytes         bigint NOT NULL,
    stats           jsonb,
    per_group_stats jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, layout, rg_id)
);

CREATE TABLE IF NOT EXISTS rvbbit.layout_variant_status (
    table_oid       oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    layout          text NOT NULL,
    status          text NOT NULL DEFAULT 'ready',
    expected_rows   bigint NOT NULL DEFAULT 0,
    actual_rows     bigint NOT NULL DEFAULT 0,
    file_count      integer NOT NULL DEFAULT 0,
    status_message  text,
    refreshed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, layout),
    CHECK (status IN ('ready', 'refreshing', 'invalid', 'failed'))
);

CREATE OR REPLACE FUNCTION rvbbit.layout_variant_status_for(rel regclass)
RETURNS TABLE (
    layout text,
    status text,
    expected_rows bigint,
    actual_rows bigint,
    file_count integer,
    status_message text,
    refreshed_at timestamptz
)
LANGUAGE sql
STABLE
AS $$
    SELECT s.layout,
           s.status,
           s.expected_rows,
           s.actual_rows,
           s.file_count,
           s.status_message,
           s.refreshed_at
    FROM rvbbit.layout_variant_status s
    WHERE s.table_oid = rel
    ORDER BY s.layout;
$$;

-- Shadow learned-router observability ----------------------------------------

CREATE TABLE IF NOT EXISTS rvbbit.route_shadow_decisions (
    id                bigserial PRIMARY KEY,
    observed_at       timestamptz NOT NULL DEFAULT now(),
    query_hash        text NOT NULL,
    shape_key         text NOT NULL,
    shape_family      text NOT NULL,
    chosen_candidate  text,
    shadow_candidate  text,
    shadow_source     text,
    confidence        double precision,
    table_rows        bigint NOT NULL DEFAULT 0,
    features          jsonb NOT NULL,
    decision          jsonb NOT NULL,
    CHECK (confidence IS NULL OR confidence >= 0)
);

CREATE INDEX IF NOT EXISTS route_shadow_decisions_shape_idx
    ON rvbbit.route_shadow_decisions (shape_key, observed_at DESC);

CREATE OR REPLACE FUNCTION rvbbit.route_shadow_explain(
    query text,
    log boolean DEFAULT false
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_shadow_explain_wrapper';
