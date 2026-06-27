-- Pipeline cascades — step store, synth cache, and seeded rowset operators.
-- Loaded by src/pipeline.rs (extension_sql_file!, requires rvbbit_bootstrap).
-- See docs/PIPELINE_CASCADES_PLAN.md.

-- Per-step resultset store: one row per (flow run, step). Populated best-effort
-- by rvbbit.flow(); inspect with rvbbit.flow_step(run_id, idx) or by querying
-- this table directly. Reap with rvbbit.reap_flow_steps(interval).
CREATE TABLE IF NOT EXISTS rvbbit.flow_steps (
    run_id        uuid        NOT NULL,
    step_idx      int         NOT NULL,
    stage         text        NOT NULL,
    spec          text,
    generated_sql text,                                  -- synth-sql stages: the SQL the model authored
    rows          jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- capped sample; n_rows is the true count
    n_rows        int         NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, step_idx)
);
CREATE INDEX IF NOT EXISTS flow_steps_created_at_idx ON rvbbit.flow_steps (created_at);

-- Shape-keyed SQL synthesis cache (Phase 2): the LLM authors SQL once per
-- structural shape, validated and reused. Keyed by (operator, shape, prompt).
CREATE TABLE IF NOT EXISTS rvbbit.synth_cache (
    operator          text        NOT NULL,
    shape_fingerprint text        NOT NULL,
    prompt_hash       text        NOT NULL,
    generated_sql     text        NOT NULL,
    status            text        NOT NULL DEFAULT 'unverified',
    sample            jsonb,
    -- The authoritative result shape of generated_sql, captured at compile time:
    -- [{"name","type","oid"}, …]. For query synth this is the column list the
    -- downstream re-materializers (lens projection, flow head-unwrap) consume
    -- instead of re-inferring types from sampled rows. NULL for scalar/rowset.
    result_schema     jsonb,
    pinned            boolean     NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (operator, shape_fingerprint, prompt_hash)
);

-- Inspect one step's rows (Bret-Victor "what did the data look like here?").
CREATE OR REPLACE FUNCTION rvbbit.flow_step(p_run_id uuid, p_step_idx int)
RETURNS SETOF jsonb LANGUAGE sql STABLE AS $fn$
    SELECT jsonb_array_elements(s.rows)
    FROM rvbbit.flow_steps s
    WHERE s.run_id = p_run_id AND s.step_idx = p_step_idx
$fn$;

-- TTL cleanup for the step store (call from pg_cron or app-side).
CREATE OR REPLACE FUNCTION rvbbit.reap_flow_steps(max_age interval DEFAULT '24 hours')
RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE n bigint;
BEGIN
    DELETE FROM rvbbit.flow_steps WHERE created_at < clock_timestamp() - max_age;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $fn$;

-- Seed the first rowset (pipeline-stage) operator: ANALYZE — value-mode, sends
-- the table to the model as JSON and returns a small findings table.
SELECT rvbbit.create_operator(
    op_name        => 'analyze',
    op_arg_names   => ARRAY['prompt'],
    op_return_type => 'jsonb',
    op_system      => 'You are a precise data analyst. You are given a table as a JSON array of row objects and a request. Answer ONLY about the data provided; do not invent rows or columns. Respond with STRICT JSON of the form {"data": [ {"finding": <text>, "detail": <text>}, ... ]} and nothing else.',
    op_user        => E'REQUEST: {{ prompt }}\n\nTABLE ({{ _table_row_count }} rows; columns: {{ _table_columns }}):\n{{ _table }}\n\nReturn ONLY the JSON object described above.',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_max_tokens  => 1024,
    op_description => 'Pipeline rowset stage: analyze the whole resultset and return a findings table.'
);

-- Structural synth-sql rowset operators (Phase 2). parser='sql' selects the
-- shape-keyed synthesis strategy: the model authors ONE standard-PostgreSQL
-- SELECT over a table named _input, keyed/cached by the rowset's structural
-- shape, then executed natively (rvbbit.synth_cache). Seeded with one shared
-- template; the user's prompt arg drives the actual transform.
DO $seed$
DECLARE
    v_user text := $tmpl$REQUEST: {{ prompt }}

The data is a table named _input with these columns:
{{ _table_schema }}

Distinct values of low-cardinality text columns:
{{ _table_distinct }}

Rules:
- Write exactly ONE standard PostgreSQL SELECT over _input.
- Use ONLY _input and the columns listed above. No DuckDB syntax, no PIVOT keyword, no semicolons, no WITH/CTE.
- For a crosstab/pivot, use conditional aggregation: count(*) FILTER (WHERE col = 'value') AS alias (one column per distinct value listed above).
- Return STRICT JSON and nothing else: {"sql": "<the SELECT statement>"}.

If a previous attempt failed, this is the Postgres error to fix (empty on the first try):
{{ _last_sql_error }}$tmpl$;
    r record;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('pivot',  'Crosstab/pivot a resultset using conditional aggregation.'),
        ('group',  'Group and aggregate a resultset.'),
        ('top',    'Order a resultset and keep the top rows.'),
        ('filter', 'Filter a resultset to matching rows, same columns.'),
        ('normalize', 'Normalize, repair, or add derived columns across a resultset.')
    ) AS t(nm, descr) LOOP
        PERFORM rvbbit.create_operator(
            op_name        => r.nm,
            op_arg_names   => ARRAY['prompt'],
            op_return_type => 'jsonb',
            op_shape       => 'rowset',
            op_parser      => 'sql',
            op_system      => 'You translate a request into ONE standard PostgreSQL SELECT over a table named _input, returning only SQL via JSON. Intent: ' || r.descr,
            op_user        => v_user,
            op_max_tokens  => 1200,
            op_description => 'Pipeline rowset stage (synth-sql): ' || r.descr
        );
    END LOOP;
END $seed$;

-- ENRICH: LLM value-mode rowset operator (ported from larsql). The model sees
-- the whole table and returns it with new computed columns added per row, the
-- originals preserved. parser='json' -> the value path parses {"data":[...]}.
SELECT rvbbit.create_operator(
    op_name        => 'enrich',
    op_arg_names   => ARRAY['prompt'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_system      => 'You are a data enrichment specialist: add new computed columns to each row of a table. Respond with STRICT JSON of the form {"data": [ { ...all original columns preserved exactly..., "new_column": <value>, ... }, ... ]} and nothing else. Preserve every original column and value; add columns per the request using snake_case names; keep column names and value types consistent across all rows; new values must be strings, numbers, or booleans (no nested objects).',
    op_user        => E'ENRICHMENT REQUEST: {{ prompt }}\n\nDATA ({{ _table_row_count }} rows; columns: {{ _table_columns }}):\n{{ _table }}\n\nReturn ONLY the JSON object described above.',
    op_max_tokens  => 2048,
    op_description => 'Pipeline rowset stage: add LLM-computed columns to each row.'
);

-- UI artifact rowset operators. These are deterministic rowset -> renderable
-- artifact transforms: the caller supplies data, and the operator emits rows the
-- RVBBIT UI can interpret. They intentionally stay in the same workflow substrate
-- as every other THEN stage.
SELECT rvbbit.create_operator(
    op_name        => 'metric_card',
    op_arg_names   => ARRAY['label','value','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_metric_card',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'label', '{{ inputs.label }}',
            'value', '{{ inputs.value }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a metric-card UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'bar_chart',
    op_arg_names   => ARRAY['x','y','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_bar_chart',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'x', '{{ inputs.x }}',
            'y', '{{ inputs.y }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a Vega-Lite bar-chart UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'line_chart',
    op_arg_names   => ARRAY['x','y','title','color'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_line_chart',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'x', '{{ inputs.x }}',
            'y', '{{ inputs.y }}',
            'title', '{{ inputs.title }}',
            'color', '{{ inputs.color }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a Vega-Lite line-chart UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'scatter_plot',
    op_arg_names   => ARRAY['x','y','title','color','size'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_scatter_plot',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'x', '{{ inputs.x }}',
            'y', '{{ inputs.y }}',
            'title', '{{ inputs.title }}',
            'color', '{{ inputs.color }}',
            'size', '{{ inputs.size }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a Vega-Lite scatter-plot UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'table_view',
    op_arg_names   => ARRAY['title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_table_view',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a table UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'vega_lite',
    op_arg_names   => ARRAY['spec','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_vega_lite',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'spec', '{{ inputs.spec }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline visual stage: emit a custom Vega-Lite UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'filter_control',
    op_arg_names   => ARRAY['field','kind','title','operator','default','value','default_value'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_filter_control',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'field', '{{ inputs.field }}',
            'kind', '{{ inputs.kind }}',
            'title', '{{ inputs.title }}',
            'operator', '{{ inputs.operator }}',
            'default', '{{ inputs.default }}',
            'value', '{{ inputs.value }}',
            'default_value', '{{ inputs.default_value }}'
        )
    )),
    op_description => 'Pipeline control stage: emit a parameter-publishing filter-control UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'action_button',
    op_arg_names   => ARRAY['label','sql','title','confirm','variant','refresh'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_action_button',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'label', '{{ inputs.label }}',
            'sql', '{{ inputs.sql }}',
            'title', '{{ inputs.title }}',
            'confirm', '{{ inputs.confirm }}',
            'variant', '{{ inputs.variant }}',
            'refresh', '{{ inputs.refresh }}'
        )
    )),
    op_description => 'Pipeline action stage: emit a SQL action button UI artifact from the current resultset.'
);

SELECT rvbbit.create_operator(
    op_name        => 'tile_name',
    op_arg_names   => ARRAY['name','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_tile_name',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'name', '{{ inputs.name }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline meta stage: attach a stable layout alias to the current UI artifact statement.'
);

SELECT rvbbit.create_operator(
    op_name        => 'bind_filter',
    op_arg_names   => ARRAY['target','field','operator','title'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_bind_filter',
        'inputs', jsonb_build_object(
            'rows', '{{ inputs._table }}',
            'target', '{{ inputs.target }}',
            'field', '{{ inputs.field }}',
            'operator', '{{ inputs.operator }}',
            'title', '{{ inputs.title }}'
        )
    )),
    op_description => 'Pipeline meta stage: bind a control artifact to a named target tile for cross-filtering.'
);

SELECT rvbbit.create_operator(
    op_name        => 'layout_grid',
    op_arg_names   => ARRAY['layout','title','rows','mode'],
    op_return_type => 'jsonb',
    op_shape       => 'rowset',
    op_parser      => 'json',
    op_steps       => jsonb_build_array(jsonb_build_object(
        'name', 'emit',
        'kind', 'code',
        'fn', 'ui_layout_grid',
        'inputs', jsonb_build_object(
            'layout', '{{ inputs.layout }}',
            'title', '{{ inputs.title }}',
            'layout_rows', '{{ inputs.rows }}',
            'mode', '{{ inputs.mode }}'
        )
    )),
    op_description => 'Pipeline meta stage: emit a statement-grid layout artifact for multi-statement UI composition.'
);

-- RESHAPE: scalar synth-sql operator (Phase 5). The model authors ONE PostgreSQL
-- expression over a text input `x` per distinct value-shape (digits->d, letters->a),
-- cached in rvbbit.synth_cache and applied natively. So reshaping 50M values of
-- ~50 formats costs ~50 model calls, then deterministic SQL.
SELECT rvbbit.create_operator(
    op_name        => 'reshape',
    op_arg_names   => ARRAY['value', 'intent'],
    op_arg_types   => ARRAY['text', 'text'],
    op_return_type => 'text',
    op_shape       => 'scalar',
    op_parser      => 'sql',
    op_system      => 'You write ONE PostgreSQL scalar expression that transforms a text input named x according to the request. Reference only x. Use standard PostgreSQL (regexp_replace, substring, ||, lower, upper, CASE, etc.); no subqueries, no semicolons, no function definitions. Return STRICT JSON {"sql": "<expression over x>"} and nothing else.',
    op_user        => E'REQUEST: {{ intent }}\nINPUT SHAPE (each d = a digit, a = a letter, other characters are literal): {{ shape }}\nA representative input of this shape: {{ example }}\n\nWrite the expression over x that performs the request for inputs of this exact shape.\nIf a previous attempt failed, fix this Postgres error (empty on the first try): {{ _last_sql_error }}\n\nReturn ONLY {"sql": "<expression over x>"}.',
    op_max_tokens  => 400,
    op_description => 'Scalar synth-sql: reshape/format a text value; the model writes one expression per value-shape, cached and reused.'
);

-- PARSE / NORMALIZE_VALUE: friendlier scalar synth-sql entry points for data
-- cleaning. These share the same shape-keyed compiler path as reshape: the
-- model writes one PostgreSQL expression over x per value format, and that
-- expression is cached/reused for every value with the same structural shape.
SELECT rvbbit.create_operator(
    op_name        => 'parse',
    op_arg_names   => ARRAY['value', 'instruction'],
    op_arg_types   => ARRAY['text', 'text'],
    op_return_type => 'text',
    op_shape       => 'scalar',
    op_parser      => 'sql',
    op_system      => 'You write ONE PostgreSQL scalar expression that parses a text input named x according to the instruction. Reference only x. Use standard PostgreSQL (regexp_replace, substring, ||, lower, upper, CASE, etc.); no subqueries, no semicolons, no function definitions. Return STRICT JSON {"sql": "<expression over x>"} and nothing else.',
    op_user        => E'INSTRUCTION: {{ instruction }}\nINPUT SHAPE (each d = a digit, a = a letter, other characters are literal): {{ shape }}\nA representative input of this shape: {{ example }}\n\nWrite the expression over x that parses/extracts the requested value for inputs of this exact shape.\nIf a previous attempt failed, fix this Postgres error (empty on the first try): {{ _last_sql_error }}\n\nReturn ONLY {"sql": "<expression over x>"}.',
    op_max_tokens  => 400,
    op_description => 'Scalar synth-sql: parse/extract a clean value from messy text; one cached SQL expression per value-shape.'
);

SELECT rvbbit.create_operator(
    op_name        => 'normalize_value',
    op_arg_names   => ARRAY['value', 'instruction'],
    op_arg_types   => ARRAY['text', 'text'],
    op_return_type => 'text',
    op_shape       => 'scalar',
    op_parser      => 'sql',
    op_system      => 'You write ONE PostgreSQL scalar expression that normalizes a text input named x according to the instruction. Reference only x. Use standard PostgreSQL (regexp_replace, substring, ||, lower, upper, CASE, etc.); no subqueries, no semicolons, no function definitions. Return STRICT JSON {"sql": "<expression over x>"} and nothing else.',
    op_user        => E'INSTRUCTION: {{ instruction }}\nINPUT SHAPE (each d = a digit, a = a letter, other characters are literal): {{ shape }}\nA representative input of this shape: {{ example }}\n\nWrite the expression over x that normalizes values of this exact shape.\nIf a previous attempt failed, fix this Postgres error (empty on the first try): {{ _last_sql_error }}\n\nReturn ONLY {"sql": "<expression over x>"}.',
    op_max_tokens  => 400,
    op_description => 'Scalar synth-sql: normalize a messy value; one cached SQL expression per value-shape.'
);

-- Query synth-sql helpers. These run inside plpgsql EXCEPTION blocks, which open a
-- REAL Postgres subtransaction (pgrx's PgTryBuilder does not) — so a caught error
-- is rolled back cleanly instead of leaving the surrounding transaction aborted.

-- Retrieve intent-relevant schema docs from the crawled catalog. The EXCEPTION
-- block isolates any data_search failure (returns '' so the caller falls back to
-- information_schema). The aggregation order has a node_id tiebreaker so the SAME
-- intent yields the SAME context string -> stable synth_cache key -> cache hits.
CREATE OR REPLACE FUNCTION rvbbit._synth_retrieve(p_intent text, p_k int)
RETURNS text LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_ctx text;
BEGIN
    BEGIN
        SELECT string_agg(doc, E'\n' ORDER BY score DESC NULLS LAST, node_id)
          INTO v_ctx
          FROM rvbbit.data_search(p_intent, greatest(coalesce(p_k, 16), 1),
                                  ARRAY['db_table', 'db_column'], 'db_catalog');
    EXCEPTION WHEN OTHERS THEN
        v_ctx := NULL;
    END;
    RETURN coalesce(v_ctx, '');
END $fn$;

-- Validate a generated statement. Returns NULL when it is a valid read-only
-- SELECT, else an error message. Two stages, both side-effect-free for the common
-- case:
--   1. PREPARE — parse + analyze ONLY (resolves tables/columns/types, raising on a
--      bad generation). PREPARE does not plan, so it never const-folds/executes a
--      function and avoids the EXPLAIN-of-WITH grammar quirks.
--   2. EXPLAIN (FORMAT JSON) EXECUTE — plan the prepared statement (clean grammar)
--      inside a subtransaction that is ALWAYS rolled back, and reject any
--      ModifyTable node so only read-only SELECTs pass (this covers data-modifying
--      CTEs, which a prefix check cannot). The rollback undoes any transactional
--      plan-time side effect; a deliberately mislabeled IMMUTABLE function's
--      non-transactional effects (e.g. nextval) are the only residual, closed by
--      Phase 2's parse-tree wrapper.
CREATE OR REPLACE FUNCTION rvbbit._synth_validate(p_sql text)
RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE
    v_plan   json;
    v_result text;
BEGIN
    -- Clear any prepared statement leaked by a prior crashed call.
    BEGIN EXECUTE 'DEALLOCATE _rvbbit_synth_chk'; EXCEPTION WHEN OTHERS THEN NULL; END;

    BEGIN
        EXECUTE 'PREPARE _rvbbit_synth_chk AS ' || p_sql;
    EXCEPTION WHEN OTHERS THEN
        RETURN SQLERRM;                              -- bad column/table/syntax
    END;

    BEGIN
        EXECUTE 'EXPLAIN (FORMAT JSON) EXECUTE _rvbbit_synth_chk' INTO v_plan;
        IF jsonb_path_exists(v_plan::jsonb, '$.** ? (@."Node Type" == "ModifyTable")')
        THEN v_result := 'not read-only: the statement writes data';
        ELSE v_result := NULL; END IF;
        RAISE EXCEPTION 'rvbbit_synth_validated' USING ERRCODE = 'RV000';
    EXCEPTION
        WHEN SQLSTATE 'RV000' THEN NULL;            -- planned OK; rolled back
        WHEN OTHERS          THEN v_result := SQLERRM;
    END;

    EXECUTE 'DEALLOCATE _rvbbit_synth_chk';
    RETURN v_result;
END $fn$;

-- Execute a validated synth SELECT with guard rails (Phase 2). The statement has
-- already been validated as a single read-only SELECT by rvbbit._synth_validate;
-- this is the execution-time defense-in-depth. The guards (a statement timeout, a
-- READ ONLY transaction so any write a mislabeled IMMUTABLE function attempts errors
-- out, and a hard row cap) are set inside a subtransaction that is ALWAYS rolled
-- back — which reverts the SET LOCALs (Postgres forbids turning transaction_read_only
-- back off after a query, so a rollback is the only way to restore it). The result
-- rows are captured into a plpgsql array, which survives the rollback, and returned.
-- Wrapping p_sql as a subquery also rejects data-modifying CTEs ("must be at top
-- level"). So `SELECT * FROM rvbbit.synth(...)` never leaves the caller read-only.
CREATE OR REPLACE FUNCTION rvbbit._synth_execute(p_sql text, p_max_rows int, p_timeout_ms int)
RETURNS SETOF jsonb LANGUAGE plpgsql AS $fn$
DECLARE
    v_rows jsonb[];
BEGIN
    BEGIN
        PERFORM set_config('statement_timeout', greatest(p_timeout_ms, 100)::text, true);
        PERFORM set_config('transaction_read_only', 'on', true);
        EXECUTE format(
            'SELECT array_agg(to_jsonb(q)) FROM (SELECT * FROM (%s) s LIMIT %s) q',
            p_sql, greatest(p_max_rows, 0)
        ) INTO v_rows;
        RAISE EXCEPTION 'rvbbit_synth_executed' USING ERRCODE = 'RV000';
    EXCEPTION
        WHEN SQLSTATE 'RV000' THEN NULL;       -- ran OK; the rollback reverted the guards
        WHEN OTHERS THEN
            RAISE WARNING 'rvbbit.synth: execution failed: %', SQLERRM;
            v_rows := NULL;
    END;
    RETURN QUERY SELECT unnest(coalesce(v_rows, ARRAY[]::jsonb[]));
END $fn$;

-- Capture the authoritative result shape of a generated SELECT — column names +
-- Postgres types — WITHOUT executing it. CREATE TEMP VIEW parse-analyzes the body
-- (resolving the output column list) but never plans/runs it, so there is no
-- const-fold side effect; pg_attribute over the view is the exact tuple descriptor.
-- This is the "compiler emits the schema" primitive: it is captured once at compile
-- time so the lens projection, flow head-unwrap, and base-SQL composition consume
-- one truth instead of re-inferring types from sampled rows. Returns
-- [{"name","type","oid"}, …], or [] if anything fails.
CREATE OR REPLACE FUNCTION rvbbit._synth_schema(p_sql text)
RETURNS jsonb LANGUAGE plpgsql
SET client_min_messages = warning   -- silence the first-call "pg_temp does not exist" NOTICE
AS $fn$
DECLARE v_schema jsonb;
BEGIN
    BEGIN EXECUTE 'DROP VIEW IF EXISTS pg_temp._rvbbit_synth_probe'; EXCEPTION WHEN OTHERS THEN NULL; END;
    BEGIN
        EXECUTE 'CREATE TEMP VIEW _rvbbit_synth_probe AS ' || p_sql;
        SELECT jsonb_agg(
                 jsonb_build_object('name', attname, 'type', atttypid::regtype::text, 'oid', atttypid::int)
                 ORDER BY attnum)
          INTO v_schema
          FROM pg_attribute
         WHERE attrelid = 'pg_temp._rvbbit_synth_probe'::regclass
           AND attnum > 0 AND NOT attisdropped;
        EXECUTE 'DROP VIEW pg_temp._rvbbit_synth_probe';
    EXCEPTION WHEN OTHERS THEN
        v_schema := NULL;
    END;
    RETURN coalesce(v_schema, '[]'::jsonb);
END $fn$;

-- SYNTH: query synth-sql operator (shape='query', parser='sql'). The widest scope
-- of the same model-as-compiler idea: a natural-language intent + the relevant
-- catalog metadata ({{ _schema_context }}, retrieved by rvbbit.data_search) -> ONE
-- read-only SELECT over the live database, cached in rvbbit.synth_cache. Invoked
-- via rvbbit.synth_sql(intent) (Phase 1: generates SQL, does not run it). Users can
-- create their own shape='query' operators with house-style prompts the same way.
SELECT rvbbit.create_operator(
    op_name        => 'synth',
    op_arg_names   => ARRAY['intent'],
    op_return_type => 'text',
    op_shape       => 'query',
    op_parser      => 'sql',
    op_system      => 'You translate a request into ONE read-only standard PostgreSQL SELECT against the user''s database, returning only SQL via JSON. You never invent tables or columns.',
    op_user        => E'REQUEST: {{ intent }}\n\nYou may use ONLY the tables and columns described below (exact schema-qualified names, types, example values, and foreign keys). Do not reference anything not listed.\n{{ _schema_context }}\n\nRules:\n- Write exactly ONE standard PostgreSQL SELECT (a single statement; WITH/CTEs are allowed).\n- Read-only only: no INSERT/UPDATE/DELETE/DDL, no semicolons, no multiple statements.\n- Schema-qualify table names and use only the columns listed above. Add a sensible LIMIT when the result could be large.\n- Return STRICT JSON and nothing else: {"sql": "<the SELECT statement>"}.\n\nIf a previous attempt failed, fix this Postgres error (empty on the first try):\n{{ _last_sql_error }}',
    op_max_tokens  => 1200,
    op_description => 'Query synth-sql: natural-language intent -> one read-only SELECT over the live DB, grounded by catalog retrieval. Invoked via rvbbit.synth_sql().'
);

-- ── Phase C: typed synth relations ──────────────────────────────────────────
-- Plain SQL can't reference dynamic columns from a SETOF jsonb function (the shape
-- isn't known at parse time). These surfaces use the compiler-captured schema
-- (synth_cache.result_schema / synth_schema) to expose REAL typed columns you can
-- CTE / JOIN / aggregate natively — materializing the late-bound shape into an
-- early-bound relation.

-- Format a result_schema array into a SQL column-definition list, e.g.
--   [{"name":"season","type":"text"},{"name":"n","type":"bigint"}] -> "season" text, "n" bigint
CREATE OR REPLACE FUNCTION rvbbit._synth_coldef_from_schema(p_schema jsonb)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT string_agg(quote_ident(c->>'name') || ' ' || (c->>'type'), ', ' ORDER BY ord)
    FROM jsonb_array_elements(coalesce(p_schema, '[]'::jsonb)) WITH ORDINALITY AS t(c, ord);
$fn$;

-- Execute a validated read-only SELECT and return its rows as records (the caller
-- supplies the column list via AS). Guard rails: statement timeout + a READ ONLY
-- transaction (so a mislabeled writer function can't write). Like synth(), this
-- leaves the surrounding transaction read-only (records can't be captured-and-rolled
-- back the way jsonb rows can) — use it as a standalone read / CTE source.
CREATE OR REPLACE FUNCTION rvbbit._synth_exec_record(p_sql text)
RETURNS SETOF record LANGUAGE plpgsql AS $fn$
BEGIN
    PERFORM set_config('statement_timeout', '10000', true);
    PERFORM set_config('transaction_read_only', 'on', true);
    RETURN QUERY EXECUTE p_sql;
END $fn$;

-- The column-definition list for synth_record's AS clause (from the cached schema).
CREATE OR REPLACE FUNCTION rvbbit.synth_coldef(intent text, operator text DEFAULT 'synth', opts jsonb DEFAULT '{}')
RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE v_schema jsonb;
BEGIN
    SELECT jsonb_agg(jsonb_build_object('name', column_name, 'type', data_type))
      INTO v_schema FROM rvbbit.synth_schema(intent, operator, opts);
    RETURN rvbbit._synth_coldef_from_schema(v_schema);
END $fn$;

-- Typed synth relation: SELECT * FROM rvbbit.synth_record('intent') AS t(col type, …)
-- Get the AS list from rvbbit.synth_coldef(intent). Gated behind rvbbit.synth_enabled.
CREATE OR REPLACE FUNCTION rvbbit.synth_record(intent text, operator text DEFAULT 'synth', opts jsonb DEFAULT '{}')
RETURNS SETOF record LANGUAGE plpgsql AS $fn$
DECLARE v_sql text;
BEGIN
    IF coalesce(current_setting('rvbbit.synth_enabled', true), 'off') NOT IN ('on','true','1','yes') THEN
        RAISE EXCEPTION 'rvbbit.synth_record is disabled; run: SET rvbbit.synth_enabled = on';
    END IF;
    v_sql := rvbbit.synth_sql(intent, operator, opts);
    IF v_sql IS NULL OR left(btrim(v_sql), 2) = '--' THEN
        RETURN;  -- generation failed / unvalidated
    END IF;
    PERFORM set_config('statement_timeout', '10000', true);
    PERFORM set_config('transaction_read_only', 'on', true);
    RETURN QUERY EXECUTE v_sql;
END $fn$;

-- Materialize a synth query as a typed VIEW you can query directly:
--   SELECT rvbbit.synth_view('sightings_by_region', 'number of sightings per region');
--   SELECT region, sum(...) FROM sightings_by_region GROUP BY region;   -- real columns
CREATE OR REPLACE FUNCTION rvbbit.synth_view(view_name text, intent text, operator text DEFAULT 'synth', opts jsonb DEFAULT '{}')
RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE v_coldef text;
BEGIN
    v_coldef := rvbbit.synth_coldef(intent, operator, opts);
    IF v_coldef IS NULL OR v_coldef = '' THEN
        RAISE EXCEPTION 'rvbbit.synth_view: no schema for intent (generation failed?)';
    END IF;
    EXECUTE format('CREATE OR REPLACE VIEW %I AS SELECT * FROM rvbbit.synth_record(%L, %L, %L::jsonb) AS _t(%s)',
                   view_name, intent, operator, opts, v_coldef);
    RETURN view_name;
END $fn$;
