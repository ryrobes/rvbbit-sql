-- pg_rvbbit 0.20.0 -> 0.21.0
-- Loop 20: specialists are node primitives, not a parallel class.
--
-- create_operator's op_system / op_user prompts now default to '' so a
-- steps-only operator (a pipeline of specialist / code nodes, with no
-- top-level LLM call) can be created with no prompt boilerplate. The
-- operator is the only callable object; a specialist endpoint is a node
-- primitive used inside op_steps, exactly like llm and code nodes.
--
-- Only the two defaults change; the signature is otherwise identical, so
-- CREATE OR REPLACE works without a DROP.

CREATE OR REPLACE FUNCTION rvbbit.create_operator(
    op_name        text,
    op_arg_names   text[],
    op_return_type text,
    op_system      text DEFAULT '',
    op_user        text DEFAULT '',
    op_shape       text DEFAULT 'scalar',         -- scalar | aggregate | dimension
    op_model       text DEFAULT 'openai/gpt-5.4-mini',
    op_parser      text DEFAULT NULL,            -- auto: yes_no/strip/score_0_1 by return_type
    op_max_tokens  int  DEFAULT 256,
    op_temperature real DEFAULT NULL,
    op_arg_types   text[] DEFAULT NULL,          -- defaults to all 'text'
    op_description text DEFAULT NULL,
    op_infix_symbol text DEFAULT NULL,           -- PG operator symbol (binary scalar ops only)
    op_infix_word   text DEFAULT NULL,           -- future: text-name rewriter
    op_tests        jsonb DEFAULT NULL,          -- [{sql, expect:{type, value|pattern}, ...}]
    op_steps        jsonb DEFAULT NULL           -- multi-step pipeline (NULL = single LLM call)
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    actual_parser    text;
    actual_arg_types text[];
    exec_fn          text;
    wrapper_args_with_opts text;
    wrapper_args_no_opts   text;
    wrapper_inputs   text;
    n_args           int;
BEGIN
    n_args := cardinality(op_arg_names);
    actual_arg_types := COALESCE(op_arg_types,
        ARRAY(SELECT 'text' FROM generate_series(1, n_args)));
    actual_parser := COALESCE(op_parser, CASE op_return_type
        WHEN 'bool'   THEN 'yes_no'
        WHEN 'float8' THEN 'score_0_1'
        WHEN 'jsonb'  THEN 'json'
        ELSE 'strip'
    END);

    INSERT INTO rvbbit.operators
        (name, shape, arg_names, arg_types, return_type, model, system_prompt, user_prompt,
         parser, max_tokens, temperature, description, infix_symbol, infix_word, tests, steps)
    VALUES
        (op_name, op_shape, op_arg_names, actual_arg_types, op_return_type, op_model,
         op_system, op_user, actual_parser, op_max_tokens, op_temperature, op_description,
         op_infix_symbol, op_infix_word, op_tests, op_steps)
    ON CONFLICT (name) DO UPDATE SET
        shape = EXCLUDED.shape,
        arg_names = EXCLUDED.arg_names,
        arg_types = EXCLUDED.arg_types,
        return_type = EXCLUDED.return_type,
        model = EXCLUDED.model,
        system_prompt = EXCLUDED.system_prompt,
        user_prompt = EXCLUDED.user_prompt,
        parser = EXCLUDED.parser,
        max_tokens = EXCLUDED.max_tokens,
        temperature = EXCLUDED.temperature,
        description = EXCLUDED.description,
        infix_symbol = EXCLUDED.infix_symbol,
        infix_word = EXCLUDED.infix_word,
        tests = EXCLUDED.tests,
        steps = EXCLUDED.steps;

    IF op_shape = 'dimension' THEN
        exec_fn := '_dim_exec_' || op_return_type;

        wrapper_inputs := 'jsonb_build_object(' || array_to_string(
            ARRAY(SELECT format('%L, $%s', a, i)
                  FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
            ', '
        ) || ')';

        wrapper_args_with_opts := array_to_string(
            ARRAY(SELECT format('%I %s', a, t)
                  FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
            ', '
        ) || ', opts jsonb DEFAULT ''{}''::jsonb';

        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS SETOF %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT * FROM rvbbit.%I(%L, %s, $%s) $wb$',
            op_name, wrapper_args_with_opts, op_return_type,
            exec_fn, op_name, wrapper_inputs, n_args + 1
        );
        RETURN;
    END IF;

    IF op_shape = 'aggregate' THEN
        wrapper_inputs := 'jsonb_build_object(' || array_to_string(
            ARRAY(SELECT format('%L, $%s', a, i + 1)
                  FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
            ', '
        ) || ')';
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb, %s, opts jsonb DEFAULT ''{}''::jsonb) RETURNS jsonb LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit._agg_append_state(state, %s) $wb$',
            '_agg_' || op_name || '_sfunc',
            array_to_string(
                ARRAY(SELECT format('%I %s', a, t)
                      FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
                ', '
            ),
            wrapper_inputs
        );

        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb) RETURNS %s LANGUAGE sql PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, state) $wb$',
            '_agg_' || op_name || '_ffunc',
            op_return_type,
            '_agg_run_op_' || op_return_type,
            op_name
        );

        EXECUTE format('DROP AGGREGATE IF EXISTS rvbbit.%I(%s, jsonb)',
            op_name,
            array_to_string(actual_arg_types, ', ')
        );
        EXECUTE format(
            'CREATE AGGREGATE rvbbit.%I(%s, jsonb) (SFUNC = rvbbit.%I, STYPE = jsonb, INITCOND = ''{}'', FINALFUNC = rvbbit.%I)',
            op_name,
            array_to_string(actual_arg_types, ', '),
            '_agg_' || op_name || '_sfunc',
            '_agg_' || op_name || '_ffunc'
        );

        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb, %s) RETURNS jsonb LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit._agg_append_state(state, %s) $wb$',
            '_agg_' || op_name || '_sfunc_no_opts',
            array_to_string(
                ARRAY(SELECT format('%I %s', a, t)
                      FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
                ', '
            ),
            wrapper_inputs
        );
        EXECUTE format('DROP AGGREGATE IF EXISTS rvbbit.%I(%s)',
            op_name,
            array_to_string(actual_arg_types, ', ')
        );
        EXECUTE format(
            'CREATE AGGREGATE rvbbit.%I(%s) (SFUNC = rvbbit.%I, STYPE = jsonb, INITCOND = ''{}'', FINALFUNC = rvbbit.%I)',
            op_name,
            array_to_string(actual_arg_types, ', '),
            '_agg_' || op_name || '_sfunc_no_opts',
            '_agg_' || op_name || '_ffunc'
        );
        RETURN;
    END IF;

    -- Default: scalar.
    exec_fn := '_exec_op_' || op_return_type;

    wrapper_inputs := 'jsonb_build_object(' || array_to_string(
        ARRAY(SELECT format('%L, $%s', a, i)
              FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
        ', '
    ) || ')';

    wrapper_args_with_opts := array_to_string(
        ARRAY(SELECT format('%I %s', a, t) FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
        ', '
    ) || ', opts jsonb DEFAULT ''{}''::jsonb';

    EXECUTE format(
        'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, %s, $%s) $wb$',
        op_name, wrapper_args_with_opts, op_return_type, exec_fn, op_name, wrapper_inputs, n_args + 1
    );

    IF n_args = 2 THEN
        wrapper_args_no_opts := array_to_string(
            ARRAY(SELECT format('%I %s', a, t)
                  FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
            ', '
        );
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, %s, ''{}''::jsonb) $wb$',
            '_op_' || op_name, wrapper_args_no_opts, op_return_type,
            exec_fn, op_name, wrapper_inputs
        );

        IF op_infix_symbol IS NOT NULL THEN
            EXECUTE format(
                'CREATE OPERATOR rvbbit.%s (LEFTARG = %s, RIGHTARG = %s, FUNCTION = rvbbit.%I)',
                op_infix_symbol, actual_arg_types[1], actual_arg_types[2],
                '_op_' || op_name
            );
        END IF;
    END IF;
END $$;
