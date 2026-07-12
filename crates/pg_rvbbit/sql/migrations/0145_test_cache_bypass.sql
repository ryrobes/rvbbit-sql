-- 0145_test_cache_bypass.sql — Semantic Tests bypass the operator result cache.
--
-- run_tests re-ran the test SQL through normal operator execution, which
-- MEMOIZES (cache_policy default). So a second battery run re-served cached
-- verdicts instead of re-calling the model — fast, but it tested the cache,
-- not the model, which silently defeats drift detection. This CREATE OR
-- REPLACE adds a txn-local `rvbbit.cache_bypass` GUC (honored by the operator
-- exec: READ + WRITE bypass) so every test genuinely re-exercises the model.
-- Production caching / cache_policy are untouched.

CREATE OR REPLACE FUNCTION rvbbit.run_tests(operator_name text)
RETURNS TABLE (
    test_name   text,
    passed      boolean,
    actual      text,
    expected    text,
    description text,
    error       text
) LANGUAGE plpgsql AS $$
DECLARE
    op_row    record;
    test_case jsonb;
    actual_text text;
    expect_type text;
    expect_val  text;
    expect_pat  text;
    test_ok   boolean;
    err_msg   text;
BEGIN
    -- Force the operator result cache to bypass for the duration of this
    -- transaction, so every test genuinely re-runs the model instead of
    -- re-serving a cached verdict. Txn-local; production caching is untouched.
    PERFORM set_config('rvbbit.cache_bypass', 'on', true);
    SELECT * INTO op_row FROM rvbbit.operators WHERE name = operator_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.run_tests: operator % not found', operator_name;
    END IF;
    IF op_row.tests IS NULL OR jsonb_array_length(op_row.tests) = 0 THEN
        RETURN;
    END IF;

    FOR test_case IN SELECT jsonb_array_elements(op_row.tests) LOOP
        test_name   := COALESCE(test_case->>'name', '<unnamed>');
        description := test_case->>'description';
        expect_type := test_case->'expect'->>'type';
        expect_val  := test_case->'expect'->>'value';
        expect_pat  := test_case->'expect'->>'pattern';
        actual_text := NULL;
        test_ok     := false;
        error       := NULL;
        expected    := COALESCE(expect_val, expect_pat, expect_type);

        BEGIN
            EXECUTE test_case->>'sql' INTO actual_text;
        EXCEPTION WHEN OTHERS THEN
            actual := NULL;
            passed := false;
            error  := SQLERRM;
            RETURN NEXT;
            CONTINUE;
        END;
        actual := actual_text;

        test_ok := CASE expect_type
            WHEN 'exact'     THEN actual_text IS NOT DISTINCT FROM expect_val
            WHEN 'contains'  THEN actual_text IS NOT NULL AND position(expect_val IN actual_text) > 0
            WHEN 'regex'     THEN actual_text IS NOT NULL AND actual_text ~ expect_pat
            WHEN 'min'       THEN actual_text IS NOT NULL AND actual_text::numeric >= expect_val::numeric
            WHEN 'max'       THEN actual_text IS NOT NULL AND actual_text::numeric <= expect_val::numeric
            WHEN 'not_empty' THEN actual_text IS NOT NULL AND length(actual_text) > 0
            ELSE false
        END;
        passed := test_ok;
        RETURN NEXT;
    END LOOP;
END $$;
