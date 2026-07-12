-- 0144_operator_test_runs.sql — Semantic Tests persistence + drift timeline.
--
-- Operators can carry embedded test cases (rvbbit.operators.tests) run via
-- rvbbit.run_tests / run_all_tests. Those are stateless. This adds an
-- append-only results ledger so a battery can be LOGGED and its pass-rate
-- tracked over time, each run stamped with a free-form backend_tag naming the
-- model/version regime that answered — so a pass-rate change attributes to
-- exactly one regime change (verdict drift). The lens Semantic Tests window
-- renders this table.

CREATE SEQUENCE IF NOT EXISTS rvbbit.operator_test_run_seq;

CREATE TABLE IF NOT EXISTS rvbbit.operator_test_runs (
    run_id      bigint      NOT NULL,
    ts          timestamptz NOT NULL DEFAULT clock_timestamp(),
    operator    text        NOT NULL,
    test_name   text,
    passed      boolean,
    actual      text,
    expected    text,
    description text,
    error       text,
    -- which model/version answered this run (drift attribution)
    backend_tag text
);

CREATE INDEX IF NOT EXISTS operator_test_runs_op_ts
    ON rvbbit.operator_test_runs (operator, ts);
CREATE INDEX IF NOT EXISTS operator_test_runs_run
    ON rvbbit.operator_test_runs (run_id);

-- Run every operator's embedded battery, LOG the results under one run_id
-- stamped with `tag`, and return a per-run summary.
CREATE OR REPLACE FUNCTION rvbbit.run_tests_log(tag text DEFAULT NULL)
RETURNS TABLE (run_id bigint, operators bigint, tests bigint, passed bigint)
LANGUAGE plpgsql
VOLATILE
AS $rtl$
DECLARE
    rid bigint;
BEGIN
    rid := nextval('rvbbit.operator_test_run_seq');
    INSERT INTO rvbbit.operator_test_runs
        (run_id, operator, test_name, passed, actual, expected, description, error, backend_tag)
    SELECT rid, t.operator, t.test_name, t.passed, t.actual, t.expected, t.description, t.error, tag
    FROM rvbbit.run_all_tests() t;
    RETURN QUERY
        SELECT rid,
               count(DISTINCT r.operator),
               count(*),
               count(*) FILTER (WHERE r.passed)
        FROM rvbbit.operator_test_runs r
        WHERE r.run_id = rid;
END
$rtl$;
