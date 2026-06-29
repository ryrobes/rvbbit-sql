-- 0111_n8n_operator_node
--
-- Add a lightweight n8n bridge for semantic operator pipelines. Execution is
-- through production webhooks; direct n8n database reads are only used for
-- read-only discovery in Lens.

CREATE TABLE IF NOT EXISTS rvbbit.n8n_runtimes (
    name                text PRIMARY KEY,
    base_url            text NOT NULL,
    webhook_path_prefix text NOT NULL DEFAULT '/webhook',
    auth_header_name    text,
    auth_header_env     text,
    status              text NOT NULL DEFAULT 'configured',
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (btrim(name) <> ''),
    CHECK (btrim(base_url) ~* '^https?://'),
    CHECK (status IN ('configured', 'online', 'offline', 'error'))
);

COMMENT ON TABLE rvbbit.n8n_runtimes IS
    'Optional external n8n runtimes addressable by operator steps with kind:"n8n". Execution uses production webhooks; DB introspection is read-only.';

CREATE OR REPLACE FUNCTION rvbbit.register_n8n_runtime(
    runtime_name text,
    runtime_base_url text,
    runtime_webhook_path_prefix text DEFAULT '/webhook',
    runtime_auth_header_name text DEFAULT NULL,
    runtime_auth_header_env text DEFAULT NULL,
    runtime_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    v_name text := nullif(btrim(runtime_name), '');
    v_base text := regexp_replace(nullif(btrim(runtime_base_url), ''), '/+$', '');
    v_prefix text := coalesce(nullif(btrim(runtime_webhook_path_prefix), ''), '/webhook');
    v_row jsonb;
BEGIN
    IF v_name IS NULL THEN
        RAISE EXCEPTION 'rvbbit.register_n8n_runtime: runtime_name must not be empty';
    END IF;
    IF v_base IS NULL OR v_base !~* '^https?://' THEN
        RAISE EXCEPTION 'rvbbit.register_n8n_runtime: base_url must start with http:// or https://';
    END IF;
    IF left(v_prefix, 1) <> '/' THEN
        v_prefix := '/' || v_prefix;
    END IF;
    v_prefix := regexp_replace(v_prefix, '/+$', '');
    IF v_prefix = '' THEN
        v_prefix := '/webhook';
    END IF;

    INSERT INTO rvbbit.n8n_runtimes (
        name, base_url, webhook_path_prefix, auth_header_name, auth_header_env,
        status, metadata, updated_at
    )
    VALUES (
        v_name,
        v_base,
        v_prefix,
        nullif(btrim(runtime_auth_header_name), ''),
        nullif(btrim(runtime_auth_header_env), ''),
        'configured',
        coalesce(runtime_metadata, '{}'::jsonb),
        clock_timestamp()
    )
    ON CONFLICT (name) DO UPDATE SET
        base_url = EXCLUDED.base_url,
        webhook_path_prefix = EXCLUDED.webhook_path_prefix,
        auth_header_name = EXCLUDED.auth_header_name,
        auth_header_env = EXCLUDED.auth_header_env,
        status = EXCLUDED.status,
        metadata = EXCLUDED.metadata,
        updated_at = clock_timestamp()
    RETURNING to_jsonb(rvbbit.n8n_runtimes.*) INTO v_row;

    RETURN v_row;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.n8n_runtime_status()
RETURNS TABLE (
    name text,
    base_url text,
    webhook_path_prefix text,
    auth_configured boolean,
    status text,
    metadata jsonb,
    updated_at timestamptz
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        r.name,
        r.base_url,
        r.webhook_path_prefix,
        nullif(btrim(coalesce(r.auth_header_name, '')), '') IS NOT NULL
            AND nullif(btrim(coalesce(r.auth_header_env, '')), '') IS NOT NULL AS auth_configured,
        r.status,
        r.metadata,
        r.updated_at
    FROM rvbbit.n8n_runtimes r
    ORDER BY r.name
$$;

CREATE OR REPLACE FUNCTION rvbbit.n8n_workflows(n8n_schema text DEFAULT NULL)
RETURNS TABLE (
    workflow_id text,
    workflow_name text,
    active boolean,
    trigger_paths text[],
    input_schema jsonb,
    webhook_nodes jsonb,
    created_at timestamptz,
    updated_at timestamptz
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_schema text := nullif(btrim(n8n_schema), '');
    v_has_id boolean;
    v_has_name boolean;
    v_has_active boolean;
    v_has_nodes boolean;
    v_created_col text;
    v_updated_col text;
    v_sql text;
    v_id_expr text;
    v_name_expr text;
    v_active_expr text;
    v_nodes_expr text;
    v_created_expr text;
    v_updated_expr text;
BEGIN
    IF v_schema IS NULL THEN
        SELECT n.nspname
          INTO v_schema
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relname = 'workflow_entity'
           AND c.relkind IN ('r', 'p')
         ORDER BY (n.nspname = 'public') DESC, n.nspname
         LIMIT 1;
    END IF;

    IF v_schema IS NULL OR to_regclass(format('%I.workflow_entity', v_schema)) IS NULL THEN
        RETURN;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = v_schema AND table_name = 'workflow_entity' AND column_name = 'id'
    ) INTO v_has_id;
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = v_schema AND table_name = 'workflow_entity' AND column_name = 'name'
    ) INTO v_has_name;
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = v_schema AND table_name = 'workflow_entity' AND column_name = 'active'
    ) INTO v_has_active;
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = v_schema AND table_name = 'workflow_entity' AND column_name = 'nodes'
    ) INTO v_has_nodes;
    SELECT column_name
      INTO v_created_col
      FROM information_schema.columns
     WHERE table_schema = v_schema
       AND table_name = 'workflow_entity'
       AND column_name IN ('createdAt', 'created_at', 'created')
     ORDER BY CASE column_name WHEN 'createdAt' THEN 1 WHEN 'created_at' THEN 2 ELSE 3 END
     LIMIT 1;
    SELECT column_name
      INTO v_updated_col
      FROM information_schema.columns
     WHERE table_schema = v_schema
       AND table_name = 'workflow_entity'
       AND column_name IN ('updatedAt', 'updated_at', 'updated')
     ORDER BY CASE column_name WHEN 'updatedAt' THEN 1 WHEN 'updated_at' THEN 2 ELSE 3 END
     LIMIT 1;

    v_id_expr := CASE WHEN v_has_id THEN format('%I::text', 'id') ELSE 'NULL::text' END;
    v_name_expr := CASE WHEN v_has_name THEN format('%I::text', 'name') ELSE '''(unnamed)''::text' END;
    v_active_expr := CASE WHEN v_has_active THEN format('%I::boolean', 'active') ELSE 'NULL::boolean' END;
    v_nodes_expr := CASE WHEN v_has_nodes THEN format('coalesce(%I::jsonb, ''[]''::jsonb)', 'nodes') ELSE '''[]''::jsonb' END;
    v_created_expr := CASE WHEN v_created_col IS NOT NULL THEN format('%I::timestamptz', v_created_col) ELSE 'NULL::timestamptz' END;
    v_updated_expr := CASE WHEN v_updated_col IS NOT NULL THEN format('%I::timestamptz', v_updated_col) ELSE 'NULL::timestamptz' END;

    v_sql := format($SQL$
        WITH wf AS (
            SELECT
                %s AS workflow_id,
                %s AS workflow_name,
                %s AS active,
                %s AS nodes,
                %s AS created_at,
                %s AS updated_at
            FROM %I.workflow_entity
        ),
        expanded AS (
            SELECT
                wf.*,
                node,
                coalesce(node->'parameters', '{}'::jsonb) AS params
            FROM wf
            LEFT JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(wf.nodes) = 'array' THEN wf.nodes ELSE '[]'::jsonb END
            ) AS node ON true
        ),
        rolled AS (
            SELECT
                workflow_id,
                workflow_name,
                active,
                coalesce(
                    array_remove(array_agg(DISTINCT nullif(coalesce(params->>'path', node->>'webhookId'), '')), NULL),
                    ARRAY[]::text[]
                ) AS trigger_paths,
                coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'node_name', node->>'name',
                            'node_type', node->>'type',
                            'path', nullif(coalesce(params->>'path', node->>'webhookId'), ''),
                            'method', coalesce(params->>'httpMethod', 'POST'),
                            'response_mode', coalesce(params->>'responseMode', params->>'responseData'),
                            'parameters', params
                        )
                        ORDER BY node->>'name'
                    ) FILTER (WHERE coalesce(node->>'type', '') ILIKE '%%webhook%%'),
                    '[]'::jsonb
                ) AS webhook_nodes,
                created_at,
                updated_at
            FROM expanded
            GROUP BY workflow_id, workflow_name, active, created_at, updated_at
        )
        SELECT
            workflow_id,
            workflow_name,
            active,
            trigger_paths,
            jsonb_build_object(
                'type', 'object',
                'additionalProperties', true,
                'description', 'n8n webhook payload. n8n does not publish a strict input schema here; RVBBIT sends the rendered node inputs as JSON.',
                'properties', jsonb_build_object(
                    'body', jsonb_build_object('type', 'object'),
                    'query', jsonb_build_object('type', 'object'),
                    'headers', jsonb_build_object('type', 'object')
                )
            ) AS input_schema,
            webhook_nodes,
            created_at,
            updated_at
        FROM rolled
        ORDER BY active DESC NULLS LAST, workflow_name, workflow_id
    $SQL$,
        v_id_expr,
        v_name_expr,
        v_active_expr,
        v_nodes_expr,
        v_created_expr,
        v_updated_expr,
        v_schema
    );

    RETURN QUERY EXECUTE v_sql;
END
$$;
