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
        ('filter', 'Filter a resultset to matching rows, same columns.')
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
