-- 0013_proposal_lifecycle — refine + withdraw a pending proposal.
--
-- The agent (and the lens) can draft and a human can accept/reject, but a PENDING proposal can't be
-- edited in place or withdrawn — so iterating means re-proposing (a new row each time). This adds:
--   * refine_proposal — update a pending draft's fields in place (the agent re-drafts the SAME
--     proposal after seeing feedback, instead of spawning duplicates; the lens persists edits even
--     before accepting).
--   * withdraw_proposal — the proposer retracts a pending draft (status='withdrawn'); the inbox
--     already filters on status, so it drops out of the pending view for free.
-- Part of P0 of the proposals learning loop. Additive + idempotent (CREATE OR REPLACE).

-- update a PENDING proposal in place (every arg is optional; NULL leaves that field unchanged).
CREATE OR REPLACE FUNCTION rvbbit.refine_proposal(
    p_id             bigint,
    p_name           text  DEFAULT NULL,
    p_sql            text  DEFAULT NULL,
    p_grain          text  DEFAULT NULL,
    p_description    text  DEFAULT NULL,
    p_params         jsonb DEFAULT NULL,
    p_check_sql      text  DEFAULT NULL,
    p_join_rationale text  DEFAULT NULL,
    p_confidence     real  DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE r rvbbit.proposals%ROWTYPE;
BEGIN
    SELECT * INTO r FROM rvbbit.proposals WHERE proposal_id = p_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.refine_proposal: proposal % not found', p_id;
    END IF;
    IF r.status <> 'pending' THEN
        RAISE EXCEPTION 'rvbbit.refine_proposal: proposal % is % (only pending proposals can be refined)', p_id, r.status;
    END IF;
    UPDATE rvbbit.proposals SET
        name           = coalesce(nullif(btrim(p_name), ''), name),
        sql            = coalesce(nullif(btrim(p_sql), ''), sql),
        grain          = coalesce(nullif(btrim(p_grain), ''), grain),
        description    = coalesce(nullif(btrim(p_description), ''), description),
        params         = coalesce(p_params, params),
        check_sql      = coalesce(nullif(btrim(p_check_sql), ''), check_sql),
        join_rationale = coalesce(nullif(btrim(p_join_rationale), ''), join_rationale),
        confidence     = coalesce(p_confidence, confidence)
    WHERE proposal_id = p_id;
    RETURN jsonb_build_object('status', 'refined', 'proposal_id', p_id);
END $fn$;

-- the proposer retracts a pending draft.
CREATE OR REPLACE FUNCTION rvbbit.withdraw_proposal(p_id bigint, p_reason text DEFAULT NULL)
RETURNS void LANGUAGE sql AS $$
    UPDATE rvbbit.proposals
       SET status = 'withdrawn',
           notes = coalesce(nullif(btrim(p_reason), ''), notes),
           reviewed_at = now()
     WHERE proposal_id = p_id AND status = 'pending';
$$;
