-- 0015_proposal_exemplars — close the learning loop: accepted/rejected proposals become few-shot
-- exemplars that steer the next draft.
--
-- Until now every human accept/reject evaporated — the drafter (propose_cube/propose_metric) drew
-- in isolation each time. This captures each decision as an exemplar (subject + sql + GOOD/BAD
-- label + an embedding of the subject), and injects the most-similar past exemplars into the
-- propose_*_draft context. The drafter emulates blessed designs and avoids rejected ones. Reuses
-- rvbbit.embed; capture is a trigger on rvbbit.proposals (so accept_proposal/reject_proposal are
-- untouched). Additive + idempotent. P1 of the proposals learning loop.

-- SQL cosine over two equal-length real[] embeddings (no SQL cosine is exposed; data_search's is
-- Rust-internal). NULL-safe; used only for the small exemplar KNN.
CREATE OR REPLACE FUNCTION rvbbit._cosine_arr(a real[], b real[])
RETURNS double precision LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN a IS NULL OR b IS NULL OR array_length(a, 1) IS NULL OR array_length(b, 1) IS NULL THEN NULL
        ELSE (SELECT sum(xa::float8 * xb::float8)
                   / nullif(sqrt(sum(xa::float8 * xa::float8)) * sqrt(sum(xb::float8 * xb::float8)), 0)
              FROM unnest(a, b) AS u(xa, xb))
    END;
$$;

CREATE TABLE IF NOT EXISTS rvbbit.proposal_exemplars (
    exemplar_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind               text NOT NULL,             -- 'cube' | 'metric'
    subject            text NOT NULL,
    name               text,
    sql                text,
    grain              text,
    decision           text NOT NULL,             -- 'accepted' (GOOD) | 'rejected' (BAD)
    reason             text,                       -- the reject note, when present
    subject_embedding  real[],
    source_proposal_id bigint,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS proposal_exemplars_kind_idx
    ON rvbbit.proposal_exemplars (kind, decision, created_at DESC);

-- capture one decision as an exemplar (embeds the subject best-effort).
CREATE OR REPLACE FUNCTION rvbbit.record_proposal_exemplar(p_id bigint, p_decision text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE r rvbbit.proposals%ROWTYPE; v_subj text; v_emb real[];
BEGIN
    SELECT * INTO r FROM rvbbit.proposals WHERE proposal_id = p_id;
    IF NOT FOUND THEN RETURN; END IF;
    v_subj := nullif(btrim(coalesce(r.subject, r.name, '')), '');
    IF v_subj IS NULL THEN RETURN; END IF;
    BEGIN
        v_emb := rvbbit.embed(v_subj, '', 'document');
    EXCEPTION WHEN OTHERS THEN
        v_emb := NULL;          -- no embedder → still capture (excluded from KNN, kept for recency)
    END;
    INSERT INTO rvbbit.proposal_exemplars
        (kind, subject, name, sql, grain, decision, reason, subject_embedding, source_proposal_id)
    VALUES (r.kind, v_subj, r.name, r.sql, r.grain, p_decision, r.notes, v_emb, p_id);
END $fn$;

-- capture on the accept/reject status transition (so the accept/reject fns stay untouched).
CREATE OR REPLACE FUNCTION rvbbit._proposal_exemplar_capture()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.status IN ('accepted', 'rejected') AND NEW.status IS DISTINCT FROM OLD.status THEN
        BEGIN
            PERFORM rvbbit.record_proposal_exemplar(NEW.proposal_id, NEW.status);
        EXCEPTION WHEN OTHERS THEN
            NULL;               -- never block the accept/reject on exemplar capture
        END;
    END IF;
    RETURN NEW;
END $fn$;

DROP TRIGGER IF EXISTS proposal_exemplar_capture ON rvbbit.proposals;
CREATE TRIGGER proposal_exemplar_capture
    AFTER UPDATE OF status ON rvbbit.proposals
    FOR EACH ROW EXECUTE FUNCTION rvbbit._proposal_exemplar_capture();

-- top-k most-similar exemplars per decision (GOOD + BAD) for a subject → a jsonb array.
CREATE OR REPLACE FUNCTION rvbbit.get_proposal_exemplars(p_kind text, p_subject text, p_k int DEFAULT 4)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $fn$
DECLARE v_q real[]; v_out jsonb;
BEGIN
    IF p_subject IS NULL OR btrim(p_subject) = '' THEN RETURN '[]'::jsonb; END IF;
    BEGIN
        v_q := rvbbit.embed(p_subject, '', 'document');
    EXCEPTION WHEN OTHERS THEN
        v_q := NULL;            -- no embedder → fall back to most-recent within each decision
    END;
    SELECT jsonb_agg(e ORDER BY decision, sim DESC)
      INTO v_out
      FROM (
        SELECT jsonb_build_object(
                   'decision', decision, 'subject', subject, 'name', name,
                   'grain', grain, 'sql', sql, 'reason', reason,
                   'similarity', round(coalesce(rvbbit._cosine_arr(subject_embedding, v_q), 0)::numeric, 3)) AS e,
               decision,
               coalesce(rvbbit._cosine_arr(subject_embedding, v_q), 0) AS sim,
               row_number() OVER (
                   PARTITION BY decision
                   ORDER BY coalesce(rvbbit._cosine_arr(subject_embedding, v_q), 0) DESC, created_at DESC) AS rn
        FROM rvbbit.proposal_exemplars
        WHERE kind = p_kind
      ) ranked
     WHERE rn <= greatest(coalesce(p_k, 4), 1);
    RETURN coalesce(v_out, '[]'::jsonb);
END $fn$;

-- teach the drafters to use exemplars (idempotent one-time append to the system prompt).
UPDATE rvbbit.operators
   SET system_prompt = system_prompt ||
       E'\nYou may also receive "exemplars": past proposals for similar subjects, each labeled ' ||
       'decision="accepted" (GOOD — that design was blessed) or "rejected" (BAD — avoid its pattern; ' ||
       'see its reason). Emulate the GOOD exemplars'' join/column/aggregation choices and steer clear ' ||
       'of the BAD ones.'
 WHERE name IN ('propose_cube_draft', 'propose_metric_draft')
   AND position('"exemplars": past proposals' IN system_prompt) = 0;

-- ── re-create propose_cube / propose_metric with the exemplar injection ─────
-- (identical to 0006/0010 except v_ctx now carries 'exemplars').
CREATE OR REPLACE FUNCTION rvbbit.propose_cube(
    p_subject     text,
    p_seed_tables text[] DEFAULT NULL,
    p_schema      text   DEFAULT NULL,
    p_max_tables  int    DEFAULT 8
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_cands text[]; v_oids oid[]; v_fk jsonb;
    v_cols jsonb := '{}'::jsonb; v_docs jsonb := '{}'::jsonb;
    v_ctx text; v_out jsonb; s text; sch text; tbl text;
BEGIN
    IF p_subject IS NULL OR btrim(p_subject) = '' THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: subject is required';
    END IF;

    IF p_seed_tables IS NOT NULL AND cardinality(p_seed_tables) > 0 THEN
        v_cands := p_seed_tables;
    ELSE
        SELECT array_agg(DISTINCT schema_name || '.' || rel_name)
          INTO v_cands
          FROM rvbbit.data_search(p_subject, p_max_tables, ARRAY['db_table'], 'db_catalog')
         WHERE rel_name IS NOT NULL
           AND (p_schema IS NULL OR schema_name = p_schema);
        IF v_cands IS NULL OR cardinality(v_cands) = 0 THEN
            SELECT array_agg(t) INTO v_cands FROM (
                SELECT table_schema || '.' || table_name AS t
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema NOT IN ('pg_catalog', 'information_schema', 'rvbbit', 'cubes')
                  AND (p_schema IS NULL OR table_schema = p_schema)
                ORDER BY table_schema, table_name
                LIMIT p_max_tables) z;
        END IF;
    END IF;
    IF v_cands IS NULL OR cardinality(v_cands) = 0 THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: no candidate tables for subject % (pass p_seed_tables or run catalog_crawl)', p_subject;
    END IF;

    SELECT array_agg(o) INTO v_oids FROM (
        SELECT to_regclass(t)::oid AS o FROM unnest(v_cands) t
    ) z WHERE o IS NOT NULL;

    SELECT jsonb_agg(jsonb_build_object(
             'from_table', pfn.nspname || '.' || pf.relname, 'from_column', a.attname,
             'to_table',   pcn.nspname || '.' || pc.relname, 'to_column',   a2.attname))
      INTO v_fk
      FROM pg_constraint con
      JOIN pg_class pf      ON pf.oid  = con.conrelid
      JOIN pg_namespace pfn ON pfn.oid = pf.relnamespace
      JOIN pg_class pc      ON pc.oid  = con.confrelid
      JOIN pg_namespace pcn ON pcn.oid = pc.relnamespace
      JOIN pg_attribute a   ON a.attrelid  = con.conrelid  AND a.attnum  = con.conkey[1]
      JOIN pg_attribute a2  ON a2.attrelid = con.confrelid AND a2.attnum = con.confkey[1]
     WHERE con.contype = 'f' AND pf.oid = ANY(v_oids) AND pc.oid = ANY(v_oids);

    FOREACH s IN ARRAY v_cands LOOP
        IF to_regclass(s) IS NULL THEN CONTINUE; END IF;
        sch := split_part(s, '.', 1); tbl := split_part(s, '.', 2);
        IF tbl = '' THEN tbl := sch; sch := NULL; END IF;
        v_cols := v_cols || jsonb_build_object(s, (
            SELECT jsonb_agg(jsonb_build_object('name', column_name, 'type', data_type)
                             ORDER BY ordinal_position)
              FROM information_schema.columns
             WHERE table_name = tbl AND (sch IS NULL OR table_schema = sch)));
        v_docs := v_docs || jsonb_build_object(s, (
            SELECT left(doc, 500) FROM rvbbit.catalog_docs
              WHERE rel_name = tbl AND col_name IS NULL AND (sch IS NULL OR schema_name = sch)
              ORDER BY updated_at DESC NULLS LAST LIMIT 1));
    END LOOP;

    v_ctx := jsonb_build_object(
        'subject', p_subject,
        'candidate_tables', to_jsonb(v_cands),
        'fk_edges', coalesce(v_fk, '[]'::jsonb),
        'column_samples', v_cols,
        'source_docs', v_docs,
        'exemplars', rvbbit.get_proposal_exemplars('cube', p_subject, 4))::text;

    v_out := rvbbit.propose_cube_draft(v_ctx);
    IF v_out IS NULL
       OR nullif(btrim(v_out->>'name'), '') IS NULL
       OR nullif(btrim(v_out->>'sql'), '')  IS NULL THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: draft generation failed for subject %', p_subject;
    END IF;

    RETURN v_out || jsonb_build_object(
        'candidate_tables', to_jsonb(v_cands),
        'fk_edges', coalesce(v_fk, '[]'::jsonb));
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.propose_metric(
    p_subject      text,
    p_seed_sources text[] DEFAULT NULL,
    p_schema       text   DEFAULT NULL,
    p_max_sources  int    DEFAULT 8
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_srcs text[]; v_cols jsonb := '{}'::jsonb; v_docs jsonb := '{}'::jsonb; v_metrics jsonb;
    v_ctx text; v_out jsonb; s text; sch text; tbl text;
BEGIN
    IF p_subject IS NULL OR btrim(p_subject) = '' THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: subject is required';
    END IF;

    IF p_seed_sources IS NOT NULL AND cardinality(p_seed_sources) > 0 THEN
        v_srcs := p_seed_sources;
    ELSE
        SELECT array_agg(DISTINCT schema_name || '.' || rel_name)
          INTO v_srcs
          FROM rvbbit.data_search(p_subject, p_max_sources, ARRAY['cube', 'db_table'], 'db_catalog')
         WHERE rel_name IS NOT NULL
           AND (p_schema IS NULL OR schema_name = p_schema OR schema_name = 'cubes');
        IF v_srcs IS NULL OR cardinality(v_srcs) = 0 THEN
            SELECT array_agg(t) INTO v_srcs FROM (
                SELECT 'cubes.' || name AS t FROM rvbbit.cubes()
                UNION ALL
                SELECT table_schema || '.' || table_name
                  FROM information_schema.tables
                 WHERE table_type = 'BASE TABLE'
                   AND table_schema NOT IN ('pg_catalog', 'information_schema', 'rvbbit', 'cubes')
                   AND (p_schema IS NULL OR table_schema = p_schema)
                LIMIT p_max_sources) z;
        END IF;
    END IF;
    IF v_srcs IS NULL OR cardinality(v_srcs) = 0 THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: no candidate sources for subject % (pass p_seed_sources or build cubes/crawl)', p_subject;
    END IF;

    FOREACH s IN ARRAY v_srcs LOOP
        IF to_regclass(s) IS NULL THEN CONTINUE; END IF;
        sch := split_part(s, '.', 1); tbl := split_part(s, '.', 2);
        IF tbl = '' THEN tbl := sch; sch := NULL; END IF;
        v_cols := v_cols || jsonb_build_object(s, (
            SELECT jsonb_agg(jsonb_build_object('name', column_name, 'type', data_type)
                             ORDER BY ordinal_position)
              FROM information_schema.columns
             WHERE table_name = tbl AND (sch IS NULL OR table_schema = sch)));
        v_docs := v_docs || jsonb_build_object(s, (
            SELECT left(doc, 500) FROM rvbbit.catalog_docs
              WHERE rel_name = tbl AND col_name IS NULL AND (sch IS NULL OR schema_name = sch)
              ORDER BY updated_at DESC NULLS LAST LIMIT 1));
    END LOOP;

    SELECT jsonb_agg(jsonb_build_object('name', name, 'grain', grain, 'description', description))
      INTO v_metrics
      FROM (SELECT name, grain, description FROM rvbbit.metric_catalog ORDER BY name LIMIT 20) m;

    v_ctx := jsonb_build_object(
        'subject', p_subject,
        'candidate_sources', to_jsonb(v_srcs),
        'columns', v_cols,
        'source_docs', v_docs,
        'existing_metrics', coalesce(v_metrics, '[]'::jsonb),
        'exemplars', rvbbit.get_proposal_exemplars('metric', p_subject, 4))::text;

    v_out := rvbbit.propose_metric_draft(v_ctx);
    IF v_out IS NULL
       OR nullif(btrim(v_out->>'name'), '') IS NULL
       OR nullif(btrim(v_out->>'sql'), '')  IS NULL THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: draft generation failed for subject %', p_subject;
    END IF;

    RETURN v_out || jsonb_build_object('candidate_sources', to_jsonb(v_srcs));
END $fn$;
