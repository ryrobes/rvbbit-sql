-- 0008_proposals — a durable proposal queue for agent-drafted cubes (and metrics later).
--
-- rvbbit.propose_cube returns an EPHEMERAL draft (the agent reasons out a good join once, then
-- it evaporates). This adds a durable, reviewable queue: the MCP propose_cube TOOL records each
-- draft here; a human reviews in the lens Cube Proposals inbox and blesses it (accept_proposal →
-- define_cube) or dismisses it (reject_proposal). The accept/reject signal is also the substrate
-- for the future "learning" loop. Generic over kind ('cube' now, 'metric' later). See
-- docs/CUBES_PLAN.md §6 (agent-drafted → human-blessed). Additive + idempotent.

CREATE TABLE IF NOT EXISTS rvbbit.proposals (
    proposal_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             text NOT NULL DEFAULT 'cube',     -- 'cube' | 'metric'
    status           text NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected | superseded
    name             text,                              -- suggested identifier
    subject          text,                              -- the natural-language prompt it was drafted from
    sql              text,
    grain            text,
    description      text,
    source_tables    jsonb NOT NULL DEFAULT '[]'::jsonb,
    fk_edges         jsonb NOT NULL DEFAULT '[]'::jsonb,
    candidate_tables jsonb NOT NULL DEFAULT '[]'::jsonb,
    join_rationale   text,
    confidence       real,
    proposed_by      text,                              -- 'mcp' | 'lens' | 'sql' | actor id
    proposed_via     text,
    result_name      text,                              -- the cube/metric created on accept
    notes            text,                              -- reviewer note (e.g. reject reason)
    created_at       timestamptz NOT NULL DEFAULT now(),
    reviewed_at      timestamptz
);
CREATE INDEX IF NOT EXISTS proposals_status_idx ON rvbbit.proposals (status, kind, created_at DESC);

-- record a draft (from a propose_* draft jsonb). Supersedes a prior PENDING proposal with the
-- same kind+name so the inbox doesn't fill with near-duplicate re-proposals. Returns the id.
CREATE OR REPLACE FUNCTION rvbbit.record_proposal(
    p_kind text, p_draft jsonb, p_proposed_by text DEFAULT 'agent', p_proposed_via text DEFAULT 'mcp'
) RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE v_id bigint; v_kind text := coalesce(nullif(btrim(p_kind), ''), 'cube'); v_name text;
BEGIN
    IF p_draft IS NULL OR jsonb_typeof(p_draft) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.record_proposal: draft must be a json object';
    END IF;
    v_name := nullif(btrim(p_draft->>'name'), '');
    IF v_name IS NOT NULL THEN
        UPDATE rvbbit.proposals SET status = 'superseded', reviewed_at = now()
         WHERE status = 'pending' AND kind = v_kind AND name = v_name;
    END IF;
    INSERT INTO rvbbit.proposals
        (kind, status, name, subject, sql, grain, description, source_tables, fk_edges,
         candidate_tables, join_rationale, confidence, proposed_by, proposed_via)
    VALUES (
        v_kind, 'pending', v_name,
        nullif(btrim(p_draft->>'subject'), ''),
        nullif(btrim(p_draft->>'sql'), ''),
        nullif(btrim(p_draft->>'grain'), ''),
        nullif(btrim(p_draft->>'description'), ''),
        CASE WHEN jsonb_typeof(p_draft->'source_tables') = 'array'    THEN p_draft->'source_tables'    ELSE '[]'::jsonb END,
        CASE WHEN jsonb_typeof(p_draft->'fk_edges') = 'array'         THEN p_draft->'fk_edges'         ELSE '[]'::jsonb END,
        CASE WHEN jsonb_typeof(p_draft->'candidate_tables') = 'array' THEN p_draft->'candidate_tables' ELSE '[]'::jsonb END,
        nullif(btrim(p_draft->>'join_rationale'), ''),
        CASE WHEN (p_draft->>'confidence') ~ '^[0-9.]+$' THEN (p_draft->>'confidence')::real ELSE NULL END,
        p_proposed_by, p_proposed_via)
    RETURNING proposal_id INTO v_id;
    RETURN v_id;
END $fn$;

-- bless a pending proposal: materialize it (cube) + mark accepted + link the created object.
-- name/sql/grain/description overrides let the reviewer tweak before blessing.
CREATE OR REPLACE FUNCTION rvbbit.accept_proposal(
    p_id bigint, p_name text DEFAULT NULL, p_sql text DEFAULT NULL,
    p_grain text DEFAULT NULL, p_description text DEFAULT NULL, p_enrich boolean DEFAULT false
) RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE r rvbbit.proposals%ROWTYPE; v_name text; v_sql text; v_grain text; v_desc text; v_version int;
BEGIN
    SELECT * INTO r FROM rvbbit.proposals WHERE proposal_id = p_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'rvbbit.accept_proposal: proposal % not found', p_id; END IF;
    IF r.status <> 'pending' THEN
        RAISE EXCEPTION 'rvbbit.accept_proposal: proposal % is % (not pending)', p_id, r.status;
    END IF;
    IF r.kind <> 'cube' THEN
        RAISE EXCEPTION 'rvbbit.accept_proposal: kind % not supported yet (cubes only)', r.kind;
    END IF;
    v_name  := coalesce(nullif(btrim(p_name), ''), r.name);
    v_sql   := coalesce(nullif(btrim(p_sql), ''), r.sql);
    v_grain := coalesce(nullif(btrim(p_grain), ''), r.grain);
    v_desc  := coalesce(nullif(btrim(p_description), ''), r.description);
    IF v_name IS NULL OR v_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.accept_proposal: name and sql are required to accept';
    END IF;
    v_version := rvbbit.define_cube(v_name, v_sql, v_grain, v_desc,
                                    coalesce(nullif(btrim(r.proposed_by), ''), 'proposal'),
                                    NULL, 'proposed');
    IF p_enrich THEN
        BEGIN PERFORM rvbbit.enrich_cube(v_name);
        EXCEPTION WHEN OTHERS THEN NULL;   -- enrich is best-effort; the cube is already created
        END;
    END IF;
    UPDATE rvbbit.proposals
       SET status = 'accepted', result_name = v_name, reviewed_at = now()
     WHERE proposal_id = p_id;
    RETURN jsonb_build_object('status', 'accepted', 'cube', v_name, 'version', v_version);
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.reject_proposal(p_id bigint, p_note text DEFAULT NULL)
RETURNS void LANGUAGE sql AS $$
    UPDATE rvbbit.proposals
       SET status = 'rejected', notes = p_note, reviewed_at = now()
     WHERE proposal_id = p_id AND status = 'pending';
$$;

-- list proposals (pending first, newest first). The lens inbox + future learning read this.
CREATE OR REPLACE FUNCTION rvbbit.proposals(p_status text DEFAULT NULL, p_kind text DEFAULT NULL)
RETURNS TABLE (
    proposal_id bigint, kind text, status text, name text, subject text, sql text,
    grain text, description text, source_tables jsonb, fk_edges jsonb, join_rationale text,
    confidence real, proposed_by text, proposed_via text, result_name text, notes text,
    created_at timestamptz, reviewed_at timestamptz
) LANGUAGE sql STABLE AS $$
    SELECT proposal_id, kind, status, name, subject, sql, grain, description, source_tables,
           fk_edges, join_rationale, confidence, proposed_by, proposed_via, result_name, notes,
           created_at, reviewed_at
    FROM rvbbit.proposals
    WHERE (p_status IS NULL OR status = p_status)
      AND (p_kind   IS NULL OR kind   = p_kind)
    ORDER BY (status = 'pending') DESC, created_at DESC;
$$;
