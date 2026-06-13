-- 0019_metric_check_resilience — a broken KPI check must not nuke the metric value.
--
-- Symptom: materialize_all_metrics reported "rvbbit check failed: syntax error at or near {" for
-- every metric whose check_sql contained a {target} placeholder. Root cause is NOT bad metric SQL —
-- it's that propose_metric drafted a KPI check like `(SELECT value FROM metric) >= {target}` WITHOUT
-- a default for `target` in params, so at bulk-evaluation time (params = {}) the {target} token is
-- left literal and the check SQL is a syntax error. check_metric RAISED that error, which aborted
-- materialize_metric BEFORE the value was recorded — so a bad check threw away a perfectly good value.
--
-- Two fixes:
--   (1) check_metric now isolates the check: a failed/unresolved check returns an ERROR verdict
--       ({ok:null, status:'error'}) instead of raising. The value still materializes; the Inspector
--       and board show an error verdict instead of crashing. (Authoring-time errors still surface via
--       preview_check_sql, which is unchanged.)
--   (2) propose_metric_draft is told to use CONCRETE thresholds in check_sql (or to give every
--       {param} a default), so new proposals don't carry unbound placeholders.
-- CREATE OR REPLACE / idempotent UPDATE → hot-applyable. Existing metrics heal immediately (their
-- values materialize); to make their {target} checks actually evaluate, edit them to a concrete
-- threshold (or add a default target) in the Metric Creator.

CREATE OR REPLACE FUNCTION rvbbit.check_metric(
    p_name       text,
    p_params     jsonb DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_check    text;
    v_defaults jsonb;
    v_eff      jsonb;
    v_msql     text;
    v_csql     text;
BEGIN
    SELECT check_sql, coalesce(params, '{}'::jsonb)
      INTO v_check, v_defaults
    FROM rvbbit.metric_defs
    WHERE name = p_name AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC
    LIMIT 1;

    IF v_check IS NULL OR btrim(v_check) = '' THEN
        RETURN NULL;
    END IF;

    -- threshold defaults live in the metric def's params; merge the caller's overrides on top.
    v_eff := v_defaults || coalesce(p_params, '{}'::jsonb);

    -- Isolate the whole resolve+run: a broken or unresolved check (e.g. an unbound {param}) must
    -- not crash a runtime evaluation (materialization / board / Inspector) — surface it as an error
    -- verdict and let the metric VALUE record.
    BEGIN
        v_msql := rvbbit.metric_sql(p_name, v_eff, p_def_as_of);
        v_csql := rvbbit.preview_metric_sql(v_check, v_eff, p_def_as_of);
        v_csql := rvbbit._resolve_relative_refs(v_csql, v_msql, v_eff, p_def_as_of, p_data_as_of);
        v_msql := rvbbit._resolve_relative_refs(v_msql, v_msql, v_eff, p_def_as_of, p_data_as_of);
        RETURN rvbbit._run_check(v_msql, v_csql, p_data_as_of);
    EXCEPTION WHEN OTHERS THEN
        RETURN jsonb_build_object('ok', NULL, 'status', 'error', 'error', left(SQLERRM, 400));
    END;
END;
$fn$;

-- prevent: stop the drafter emitting unbound placeholders in check_sql (idempotent append).
UPDATE rvbbit.operators
   SET system_prompt = system_prompt ||
       E'\nIMPORTANT for check_sql: use a CONCRETE numeric threshold (e.g. ">= 0.7" or "<= 30"), ' ||
       'NOT a {target} or other {param} placeholder — bulk/scheduled evaluation supplies no params, ' ||
       'so an unbound placeholder makes the whole check a syntax error. If you genuinely need a ' ||
       'parameter, you MUST also give it a default value in "params".'
 WHERE name = 'propose_metric_draft'
   AND position('CONCRETE numeric threshold' IN system_prompt) = 0;
