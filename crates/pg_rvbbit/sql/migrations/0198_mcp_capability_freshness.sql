-- 0198: MCP tools discoverable + callable with optional args — the two bugs
-- a stranger agent (Claude Desktop over warehouse-mcp) found on day one:
--
--   1. capability_search returned ZERO for an installed Linear server: the
--      rvbbit_capabilities graph only updates when rvbbit.capability_crawl()
--      runs, and nothing ran it after install — a manual step nobody knows
--      exists. Fix: generate_mcp_operators (the common tail of MCP install
--      and re-scan) now re-crawls automatically (exception-guarded), and
--      capability_search_stale() lets any caller (the warehouse-mcp tool
--      self-heals with it) detect a stale index cheaply.
--
--   2. Generated MCP operators (rvbbit.linear_searchIssues, ...) were
--      STRICT like every operator wrapper — correct NULL-propagation for
--      single-input semantic ops, but for multi-arg tools it means ANY null
--      optional arg silently returns NULL without calling the tool. Fix:
--      MCP-generated wrappers are flipped to CALLED ON NULL INPUT here, and
--      the engine's mcp step now OMITS null args from the payload (same
--      doctrine as the per-server schema wrappers: unset means omitted,
--      never null).

-- Cheap staleness probe: the graph is stale when it's empty while sources
-- exist, or any capability source changed after the graph's last write.
CREATE OR REPLACE FUNCTION rvbbit.capability_search_stale() RETURNS boolean
LANGUAGE sql STABLE AS $fn$
    WITH g AS (
        SELECT count(*) AS n, max(updated_at) AS ts
        FROM rvbbit.catalog_docs WHERE graph_id = 'rvbbit_capabilities'
    )
    SELECT (SELECT n FROM g) = 0
        OR coalesce((SELECT max(updated_at) FROM rvbbit.operators), 'epoch'::timestamptz)
               > coalesce((SELECT ts FROM g), 'epoch'::timestamptz)
        OR coalesce((SELECT max(updated_at) FROM rvbbit.capability_catalog), 'epoch'::timestamptz)
               > coalesce((SELECT ts FROM g), 'epoch'::timestamptz);
$fn$;

COMMENT ON FUNCTION rvbbit.capability_search_stale() IS
    'True when the rvbbit_capabilities graph is missing or older than the operator/catalog sources — run rvbbit.capability_crawl() to refresh.';

-- generate_mcp_operators v2: same wrapper generation, plus (a) generated
-- wrappers are CALLED ON NULL INPUT so optional args work, (b) an
-- exception-guarded capability_crawl() keeps discovery current.
CREATE OR REPLACE FUNCTION rvbbit.generate_mcp_operators(server_name text)
RETURNS int LANGUAGE plpgsql AS $gmo$
DECLARE
    t record; prop record; r record;
    v_op text; v_args text[]; v_types text[]; v_inputs jsonb; v_steps jsonb; v_n int := 0;
BEGIN
    FOR t IN SELECT name, description, input_schema FROM rvbbit.mcp_tools WHERE server = server_name LOOP
        v_op := server_name || '_' ||
            CASE WHEN left(t.name, length(server_name) + 1) = server_name || '_'
                 THEN substr(t.name, length(server_name) + 2) ELSE t.name END;
        FOR r IN SELECT oid::regprocedure AS sig FROM pg_proc
                 WHERE proname IN (v_op, '_op_' || v_op) AND pronamespace = 'rvbbit'::regnamespace LOOP
            EXECUTE 'DROP FUNCTION IF EXISTS ' || r.sig::text || ' CASCADE';
        END LOOP;
        DELETE FROM rvbbit.operators WHERE name = v_op;
        v_args := ARRAY[]::text[]; v_types := ARRAY[]::text[]; v_inputs := '{}'::jsonb;
        IF jsonb_typeof(t.input_schema->'properties') = 'object' THEN
            FOR prop IN SELECT key, value FROM jsonb_each(t.input_schema->'properties') ORDER BY key LOOP
                v_args := v_args || prop.key;
                v_types := v_types || CASE prop.value->>'type'
                    WHEN 'integer' THEN 'bigint' WHEN 'number' THEN 'double precision'
                    WHEN 'boolean' THEN 'boolean' WHEN 'object' THEN 'jsonb'
                    WHEN 'array' THEN 'jsonb' ELSE 'text' END;
                v_inputs := v_inputs || jsonb_build_object(prop.key, '{{ inputs.' || prop.key || ' }}');
            END LOOP;
        END IF;
        v_steps := jsonb_build_array(jsonb_build_object(
            'name','call','kind','mcp','server',server_name,'tool',t.name,'inputs',v_inputs));
        PERFORM rvbbit.create_operator(
            op_name => v_op, op_arg_names => v_args, op_return_type => 'text',
            op_shape => 'scalar', op_arg_types => v_types,
            op_description => coalesce(t.description,'') || ' [MCP ' || server_name || '.' || t.name || ']',
            op_steps => v_steps);
        -- Tools have OPTIONAL args: STRICT (any-null -> NULL, tool never
        -- called) is wrong here. The engine's mcp step omits null args from
        -- the payload, so CALLED ON NULL INPUT is safe end to end.
        FOR r IN SELECT oid::regprocedure AS sig FROM pg_proc
                 WHERE proname IN (v_op, '_op_' || v_op) AND pronamespace = 'rvbbit'::regnamespace LOOP
            EXECUTE 'ALTER FUNCTION ' || r.sig::text || ' CALLED ON NULL INPUT';
        END LOOP;
        v_n := v_n + 1;
    END LOOP;

    -- Keep discovery current: new tools should be capability_search-able the
    -- moment install finishes. Best-effort — wrapper generation must never
    -- fail because embedding/crawl had a bad day.
    IF v_n > 0 THEN
        BEGIN
            PERFORM rvbbit.capability_crawl();
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'generate_mcp_operators: capability_crawl failed (%) — run rvbbit.capability_crawl() manually', SQLERRM;
        END;
    END IF;
    RETURN v_n;
END $gmo$;

-- Existing installs: flip already-generated MCP operator wrappers (their
-- operators carry an mcp step) to CALLED ON NULL INPUT in place.
DO $fix$
DECLARE op record; r record;
BEGIN
    FOR op IN SELECT name FROM rvbbit.operators o
              WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(o.steps) s WHERE s->>'kind' = 'mcp') LOOP
        FOR r IN SELECT oid::regprocedure AS sig FROM pg_proc
                 WHERE proname IN (op.name, '_op_' || op.name) AND pronamespace = 'rvbbit'::regnamespace LOOP
            EXECUTE 'ALTER FUNCTION ' || r.sig::text || ' CALLED ON NULL INPUT';
        END LOOP;
    END LOOP;
END $fix$;
