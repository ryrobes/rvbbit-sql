-- 0018_proposal_quality — observability + one-click promote for the recommendations surface.
--
-- proposal_quality: accept-rate by kind + proposer (the loop's KPI — "Claude cubes 85% / warehouse
-- 60%"); a regressed propose prompt shows up here immediately. propose_discovery: turn one mined
-- discovery_candidates() table-set into a proposal on demand (the lens Recommendations "Propose this"
-- button), without waiting for the cron sweeper. P4 backend. Additive + idempotent.

-- accept-rate (among DECIDED proposals) by kind + proposer.
CREATE OR REPLACE VIEW rvbbit.proposal_quality AS
SELECT kind,
       coalesce(proposed_by, '?') AS proposed_by,
       count(*)                                          AS total,
       count(*) FILTER (WHERE status = 'accepted')       AS accepted,
       count(*) FILTER (WHERE status = 'rejected')       AS rejected,
       count(*) FILTER (WHERE status = 'pending')        AS pending,
       count(*) FILTER (WHERE status = 'withdrawn')      AS withdrawn,
       round( (count(*) FILTER (WHERE status = 'accepted'))::numeric
              / nullif(count(*) FILTER (WHERE status IN ('accepted', 'rejected')), 0), 3) AS accept_rate,
       max(created_at)  AS last_proposed,
       max(reviewed_at) AS last_reviewed
FROM rvbbit.proposals
GROUP BY kind, coalesce(proposed_by, '?');

-- propose a cube for one discovery candidate (a recurring table-set) on demand.
CREATE OR REPLACE FUNCTION rvbbit.propose_discovery(p_tables text[], p_proposed_by text DEFAULT 'lens')
RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE v_subject text; v_draft jsonb; v_pid bigint;
BEGIN
    IF p_tables IS NULL OR cardinality(p_tables) < 1 THEN
        RAISE EXCEPTION 'rvbbit.propose_discovery: at least one table is required';
    END IF;
    v_subject := 'analysis joining ' || array_to_string(p_tables, ', ') || ' (from observed usage)';
    v_draft := rvbbit.propose_cube(v_subject, p_tables);
    v_draft := v_draft || jsonb_build_object('subject', v_subject);
    v_pid := rvbbit.record_proposal('cube', v_draft,
                coalesce(nullif(btrim(p_proposed_by), ''), 'lens'), 'discovery_ui');
    RETURN v_draft || jsonb_build_object('proposal_id', v_pid);
END $fn$;
