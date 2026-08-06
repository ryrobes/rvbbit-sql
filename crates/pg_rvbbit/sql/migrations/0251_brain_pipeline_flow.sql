-- 0251_brain_pipeline_flow
--
-- Make the Document Brain a continuously draining pipeline instead of one
-- opaque, capped transaction:
--   * source updates and knowledge enrichment are separate durability stages;
--   * optional meeting summaries leave the source transaction and commit one
--     document at a time in the same worker;
--   * each enriched document commits independently;
--   * failed documents retry with backoff instead of poisoning a whole batch;
--   * sources are serviced fairly, with human/connector documents ahead of
--     high-volume query catalogs;
--   * query/MCP sources retain unseen rows unless they explicitly declare an
--     authoritative snapshot;
--   * the built-in Fireflies provider follows every API page in one update.
--
-- `brain_ingest` already makes a document searchable (chunk + embed).  The
-- worker below adds KG entities/relations asynchronously; it is not a second
-- indexing prerequisite.

ALTER TABLE rvbbit.brain_sources
    ADD COLUMN IF NOT EXISTS enrich_last_claimed_at timestamptz;

ALTER TABLE rvbbit.brain_documents
    ADD COLUMN IF NOT EXISTS enrich_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS enrich_last_attempt_at timestamptz,
    ADD COLUMN IF NOT EXISTS enrich_last_error text,
    ADD COLUMN IF NOT EXISTS enrich_error_hash text,
    ADD COLUMN IF NOT EXISTS enrich_retry_at timestamptz,
    ADD COLUMN IF NOT EXISTS summary_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS summary_last_attempt_at timestamptz,
    ADD COLUMN IF NOT EXISTS summary_last_error text,
    ADD COLUMN IF NOT EXISTS summary_error_hash text,
    ADD COLUMN IF NOT EXISTS summary_retry_at timestamptz;

CREATE INDEX IF NOT EXISTS brain_documents_enrichment_queue_idx
    ON rvbbit.brain_documents (source_id, enrich_retry_at, ingested_at DESC)
    WHERE deleted_at IS NULL AND body IS NOT NULL;

CREATE INDEX IF NOT EXISTS brain_documents_summary_queue_idx
    ON rvbbit.brain_documents (source_id, summary_retry_at, occurred_at DESC)
    WHERE deleted_at IS NULL AND body IS NOT NULL AND uri LIKE 'gmeet:%';

CREATE TABLE IF NOT EXISTS rvbbit.brain_enrichment_runs (
    run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id bigint REFERENCES rvbbit.brain_documents(doc_id) ON DELETE SET NULL,
    source_id bigint REFERENCES rvbbit.brain_sources(source_id) ON DELETE SET NULL,
    trigger text NOT NULL DEFAULT 'worker',
    profile text NOT NULL DEFAULT 'standard',
    status text NOT NULL DEFAULT 'running',
    attempt integer NOT NULL DEFAULT 1,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    elapsed_sec numeric,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    CONSTRAINT brain_enrichment_runs_status_check
        CHECK (status IN ('running', 'succeeded', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS brain_enrichment_runs_source_idx
    ON rvbbit.brain_enrichment_runs (source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS brain_enrichment_runs_doc_idx
    ON rvbbit.brain_enrichment_runs (doc_id, started_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.brain_summary_runs (
    run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id bigint REFERENCES rvbbit.brain_documents(doc_id) ON DELETE SET NULL,
    source_id bigint REFERENCES rvbbit.brain_sources(source_id) ON DELETE SET NULL,
    trigger text NOT NULL DEFAULT 'worker',
    status text NOT NULL DEFAULT 'running',
    attempt integer NOT NULL DEFAULT 1,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    elapsed_sec numeric,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    CONSTRAINT brain_summary_runs_status_check
        CHECK (status IN ('running', 'succeeded', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS brain_summary_runs_source_idx
    ON rvbbit.brain_summary_runs (source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS brain_summary_runs_doc_idx
    ON rvbbit.brain_summary_runs (doc_id, started_at DESC);

-- A single truthful state summary powers both operators and lightweight UI.
-- "indexed" means chunks exist and the document is available to Brain search;
-- "knowledge_ready" means its KG pass matches the current content hash.
CREATE OR REPLACE FUNCTION rvbbit.brain_source_pipeline_status(
    p_source_id bigint DEFAULT NULL
) RETURNS jsonb LANGUAGE sql STABLE AS $fn$
WITH live AS (
    SELECT d.*
      FROM rvbbit.brain_documents d
     WHERE d.deleted_at IS NULL
       AND (p_source_id IS NULL OR d.source_id = p_source_id)
), counts AS (
    SELECT count(*)::bigint AS documents,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM rvbbit.brain_chunks c WHERE c.doc_id = live.doc_id
           ))::bigint AS indexed,
           count(*) FILTER (WHERE enriched_at IS NOT NULL
                              AND enrich_hash IS NOT DISTINCT FROM content_hash
                              AND enriched_at >= ingested_at)::bigint AS knowledge_ready,
           count(*) FILTER (WHERE body IS NOT NULL
                              AND NOT (enriched_at IS NOT NULL
                                       AND enrich_hash IS NOT DISTINCT FROM content_hash
                                       AND enriched_at >= ingested_at))::bigint AS pending,
           count(*) FILTER (WHERE body IS NOT NULL
                              AND NOT (enriched_at IS NOT NULL
                                       AND enrich_hash IS NOT DISTINCT FROM content_hash
                                       AND enriched_at >= ingested_at)
                              AND enrich_retry_at > now()
                              AND enrich_error_hash IS NOT DISTINCT FROM content_hash)::bigint AS retry_wait,
           count(*) FILTER (WHERE enrich_last_error IS NOT NULL
                              AND enrich_error_hash IS NOT DISTINCT FROM content_hash)::bigint AS errors,
           max(enriched_at) AS last_enriched_at
      FROM live
), summary_counts AS (
    SELECT count(*) FILTER (
               WHERE raw.uri LIKE 'gmeet:%'
                 AND lower(coalesce(s.config->>'summarize_meetings','true'))
                     NOT IN ('false','0','off','no')
                 AND (summary.doc_id IS NULL
                      OR summary.raw_meta->>'source_content_hash' IS DISTINCT FROM
                         coalesce(nullif(raw.content_hash,''),md5(raw.body)))
           )::bigint AS pending,
           count(*) FILTER (
               WHERE raw.uri LIKE 'gmeet:%'
                 AND raw.summary_last_error IS NOT NULL
                 AND raw.summary_error_hash IS NOT DISTINCT FROM
                     coalesce(nullif(raw.content_hash,''),md5(raw.body))
           )::bigint AS errors
      FROM live raw
      JOIN rvbbit.brain_sources s ON s.source_id=raw.source_id
      LEFT JOIN rvbbit.brain_documents summary
        ON summary.source_id=raw.source_id
       AND summary.uri='gmeet-summary:' || substr(raw.uri,length('gmeet:')+1)
       AND summary.deleted_at IS NULL
), last_failure AS (
    SELECT r.doc_id, r.started_at, r.error
      FROM rvbbit.brain_enrichment_runs r
     WHERE r.status = 'failed'
       AND (p_source_id IS NULL OR r.source_id = p_source_id)
     ORDER BY r.started_at DESC LIMIT 1
)
SELECT jsonb_build_object(
    'source_id', p_source_id,
    'documents', c.documents,
    'indexed', c.indexed,
    'knowledge_ready', c.knowledge_ready,
    'pending', c.pending,
    'ready_now', greatest(0, c.pending - c.retry_wait),
    'retry_wait', c.retry_wait,
    'errors', c.errors,
    'summaries_pending', sc.pending,
    'summary_errors', sc.errors,
    'summary_available', (
        to_regprocedure('rvbbit.clover_summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_summarize(text)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_llm_summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_llm_summarize(text)') IS NOT NULL OR
        to_regprocedure('rvbbit.summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.summarize(text)') IS NOT NULL
    ),
    'last_enriched_at', c.last_enriched_at,
    'last_failure', CASE WHEN f.started_at IS NULL THEN NULL ELSE jsonb_build_object(
        'doc_id', f.doc_id, 'at', f.started_at, 'error', f.error) END,
    'active_workers', (
        SELECT count(*) FROM pg_stat_activity a
         WHERE a.datname = current_database() AND a.state = 'active'
           AND a.pid <> pg_backend_pid()
           AND (a.query ILIKE '%brain_enrich_drain%'
                OR a.query ILIKE '%brain_enrich_next%')
    )
)
FROM counts c CROSS JOIN summary_counts sc LEFT JOIN last_failure f ON true;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_google_meet_summarizer_available()
RETURNS boolean LANGUAGE sql STABLE AS $fn$
    SELECT
        to_regprocedure('rvbbit.clover_summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_summarize(text)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_llm_summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_llm_summarize(text)') IS NOT NULL OR
        to_regprocedure('rvbbit.summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.summarize(text)') IS NOT NULL;
$fn$;

-- Migration 0250 called the model-backed summary batch from inside source
-- reconciliation. Keep the compatibility function, but make it a cheap queue
-- report: source updates must never wait for a batch of model calls. The
-- durable worker below owns the actual one-document-at-a-time processing.
CREATE OR REPLACE FUNCTION rvbbit.brain_summarize_google_meet_pending(
    p_source_id bigint, p_max_docs int DEFAULT 12
) RETURNS jsonb LANGUAGE plpgsql STABLE AS $fn$
DECLARE v_status jsonb;
BEGIN
    v_status := rvbbit.brain_source_pipeline_status(p_source_id);
    RETURN jsonb_build_object(
        'available', rvbbit.brain_google_meet_summarizer_available(),
        'queued', coalesce((v_status->>'summaries_pending')::bigint,0),
        'processed_inline', 0,
        'worker', 'CALL rvbbit.brain_enrich_drain()',
        'requested_batch', greatest(0,coalesce(p_max_docs,0))
    );
END $fn$;

-- Build at most one derived meeting brief. Summary generation has its own
-- retry state because a model outage must not make a canonical transcript
-- unsearchable or poison its later KG pass.
CREATE OR REPLACE FUNCTION rvbbit.brain_summarize_google_meet_next(
    p_source_id bigint DEFAULT NULL,
    p_trigger text DEFAULT 'worker'
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_source_id bigint;
    v_doc_id bigint;
    v_content_hash text;
    v_attempt integer;
    v_run_id bigint;
    v_started timestamptz := clock_timestamp();
    v_result jsonb;
    v_status text;
    v_error text;
    v_retry_at timestamptz;
BEGIN
    IF NOT rvbbit.brain_google_meet_summarizer_available() THEN
        RETURN jsonb_build_object('status','idle','reason','no_summarizer');
    END IF;

    SELECT s.source_id INTO v_source_id
      FROM rvbbit.brain_sources s
     WHERE s.enabled
       AND (p_source_id IS NULL OR s.source_id=p_source_id)
       AND lower(coalesce(s.config->>'summarize_meetings','true'))
           NOT IN ('false','0','off','no')
       AND EXISTS (
           SELECT 1
             FROM rvbbit.brain_documents raw
             LEFT JOIN rvbbit.brain_documents summary
               ON summary.source_id=raw.source_id
              AND summary.uri='gmeet-summary:' || substr(raw.uri,length('gmeet:')+1)
              AND summary.deleted_at IS NULL
            WHERE raw.source_id=s.source_id AND raw.deleted_at IS NULL
              AND raw.uri LIKE 'gmeet:%' AND raw.body IS NOT NULL
              AND (summary.doc_id IS NULL
                   OR summary.raw_meta->>'source_content_hash' IS DISTINCT FROM
                      coalesce(nullif(raw.content_hash,''),md5(raw.body)))
              AND (raw.summary_retry_at IS NULL OR raw.summary_retry_at <= now()
                   OR raw.summary_error_hash IS DISTINCT FROM
                      coalesce(nullif(raw.content_hash,''),md5(raw.body)))
       )
     ORDER BY s.enrich_last_claimed_at NULLS FIRST,s.source_id
     LIMIT 1 FOR UPDATE SKIP LOCKED;

    IF v_source_id IS NULL THEN
        RETURN jsonb_build_object('status','idle','source_id',p_source_id);
    END IF;

    SELECT raw.doc_id,coalesce(nullif(raw.content_hash,''),md5(raw.body))
      INTO v_doc_id,v_content_hash
      FROM rvbbit.brain_documents raw
      LEFT JOIN rvbbit.brain_documents summary
        ON summary.source_id=raw.source_id
       AND summary.uri='gmeet-summary:' || substr(raw.uri,length('gmeet:')+1)
       AND summary.deleted_at IS NULL
     WHERE raw.source_id=v_source_id AND raw.deleted_at IS NULL
       AND raw.uri LIKE 'gmeet:%' AND raw.body IS NOT NULL
       AND (summary.doc_id IS NULL
            OR summary.raw_meta->>'source_content_hash' IS DISTINCT FROM
               coalesce(nullif(raw.content_hash,''),md5(raw.body)))
       AND (raw.summary_retry_at IS NULL OR raw.summary_retry_at <= now()
            OR raw.summary_error_hash IS DISTINCT FROM
               coalesce(nullif(raw.content_hash,''),md5(raw.body)))
     ORDER BY raw.summary_last_attempt_at NULLS FIRST,
              raw.occurred_at DESC NULLS LAST,raw.doc_id DESC
     LIMIT 1 FOR UPDATE OF raw SKIP LOCKED;

    IF v_doc_id IS NULL THEN
        RETURN jsonb_build_object('status','contended','source_id',v_source_id);
    END IF;

    UPDATE rvbbit.brain_sources SET enrich_last_claimed_at=clock_timestamp()
     WHERE source_id=v_source_id;
    SELECT CASE WHEN summary_error_hash IS DISTINCT FROM
                          coalesce(nullif(content_hash,''),md5(body)) THEN 1
                       ELSE summary_attempts+1 END
      INTO v_attempt FROM rvbbit.brain_documents WHERE doc_id=v_doc_id;

    INSERT INTO rvbbit.brain_summary_runs
        (doc_id,source_id,trigger,status,attempt,started_at)
    VALUES
        (v_doc_id,v_source_id,coalesce(p_trigger,'worker'),'running',
         greatest(1,v_attempt),v_started)
    RETURNING run_id INTO v_run_id;

    BEGIN
        v_result := rvbbit.brain_summarize_google_meet_doc(v_doc_id);
        v_status := coalesce(v_result->>'status','skipped');
        IF v_status IN ('created','updated','current') THEN
            UPDATE rvbbit.brain_documents
               SET summary_attempts=0,summary_last_attempt_at=clock_timestamp(),
                   summary_last_error=NULL,summary_error_hash=NULL,summary_retry_at=NULL,
                   -- A newly available brief changes the raw transcript's
                   -- enrichment profile from deep to bounded evidence.
                   enriched_at=CASE WHEN v_status IN ('created','updated') THEN NULL ELSE enriched_at END,
                   enrich_hash=CASE WHEN v_status IN ('created','updated') THEN NULL ELSE enrich_hash END
             WHERE doc_id=v_doc_id;
            UPDATE rvbbit.brain_summary_runs
               SET status='succeeded',finished_at=clock_timestamp(),
                   elapsed_sec=extract(epoch FROM (clock_timestamp()-v_started)),
                   result=coalesce(v_result,'{}'::jsonb)
             WHERE run_id=v_run_id;
            RETURN jsonb_build_object(
                'status','succeeded','run_id',v_run_id,'source_id',v_source_id,
                'doc_id',v_doc_id,'task','meeting_summary','result',v_result);
        END IF;

        v_error := left(coalesce(v_result->>'reason','summary_skipped') ||
                        CASE WHEN v_result ? 'detail' THEN ': ' || (v_result->>'detail')
                             ELSE '' END,2000);
        v_retry_at := clock_timestamp() + make_interval(
            secs => least(21600,(30*power(2,least(greatest(1,v_attempt),9)))::integer));
        UPDATE rvbbit.brain_documents
           SET summary_attempts=greatest(1,v_attempt),
               summary_last_attempt_at=clock_timestamp(),summary_last_error=v_error,
               summary_error_hash=v_content_hash,summary_retry_at=v_retry_at
         WHERE doc_id=v_doc_id;
        UPDATE rvbbit.brain_summary_runs
           SET status='failed',finished_at=clock_timestamp(),
               elapsed_sec=extract(epoch FROM (clock_timestamp()-v_started)),
               result=coalesce(v_result,'{}'::jsonb),error=v_error
         WHERE run_id=v_run_id;
        RETURN jsonb_build_object(
            'status','failed','run_id',v_run_id,'source_id',v_source_id,
            'doc_id',v_doc_id,'task','meeting_summary','error',v_error,
            'retry_at',v_retry_at);
    EXCEPTION WHEN OTHERS THEN
        v_error := left(SQLERRM,2000);
        v_retry_at := clock_timestamp() + make_interval(
            secs => least(21600,(30*power(2,least(greatest(1,v_attempt),9)))::integer));
        UPDATE rvbbit.brain_documents
           SET summary_attempts=greatest(1,v_attempt),
               summary_last_attempt_at=clock_timestamp(),summary_last_error=v_error,
               summary_error_hash=v_content_hash,summary_retry_at=v_retry_at
         WHERE doc_id=v_doc_id;
        UPDATE rvbbit.brain_summary_runs
           SET status='failed',finished_at=clock_timestamp(),
               elapsed_sec=extract(epoch FROM (clock_timestamp()-v_started)),error=v_error
         WHERE run_id=v_run_id;
        RETURN jsonb_build_object(
            'status','failed','run_id',v_run_id,'source_id',v_source_id,
            'doc_id',v_doc_id,'task','meeting_summary','error',v_error,
            'retry_at',v_retry_at);
    END;
END $fn$;

-- Resolve an enrichment policy without baking source-specific behavior into
-- the worker. Sources may set enrichment_profile plus chunk/skip overrides.
-- Defaults:
--   deep       human-authored connector/file docs
--   standard   query/MCP docs (NER + structured edges, no LLM triples)
--   evidence   a raw document that has a derived summary (bounded NER only)
--   structured deterministic provider edges only
CREATE OR REPLACE FUNCTION rvbbit.brain_enrichment_profile(
    p_doc_id bigint, p_default_max_chunks integer DEFAULT 20
) RETURNS jsonb LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_config jsonb;
    v_props jsonb;
    v_provider text;
    v_connector text;
    v_profile text;
    v_is_query boolean;
    v_has_summary boolean;
    v_skip_triples boolean;
    v_skip_ner boolean;
    v_triple_chunks integer;
    v_ner_chunks integer;
BEGIN
    SELECT coalesce(s.config, '{}'::jsonb), coalesce(d.props, '{}'::jsonb),
           nullif(s.config->>'provider',''),
           coalesce(nullif(s.config->>'connector',''), nullif(s.config->>'endpoint',''))
      INTO v_config, v_props, v_provider, v_connector
      FROM rvbbit.brain_documents d
      JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
     WHERE d.doc_id = p_doc_id AND d.deleted_at IS NULL;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('profile','missing','skip_triples',true,'skip_ner',true,
                                  'triple_chunks',0,'ner_chunks',0);
    END IF;

    v_is_query := v_provider IS NOT NULL AND v_connector IS NULL;
    SELECT EXISTS (
        SELECT 1 FROM rvbbit.brain_documents child
         WHERE child.deleted_at IS NULL
           AND child.props->>'derivedFromDocId' = p_doc_id::text
    ) INTO v_has_summary;

    v_profile := lower(coalesce(
        nullif(v_props->>'enrichmentProfile',''),
        nullif(v_config->>'enrichment_profile',''),
        CASE
          WHEN v_provider = 'rvbbit-system-learning' THEN 'structured'
          WHEN v_has_summary THEN 'evidence'
          WHEN v_is_query THEN 'standard'
          ELSE 'deep'
        END
    ));
    IF v_profile NOT IN ('deep','standard','evidence','structured','search_only') THEN
        v_profile := 'standard';
    END IF;

    v_skip_triples := v_profile IN ('standard','evidence','structured','search_only');
    v_skip_ner := v_profile IN ('structured','search_only');
    IF v_config ? 'enrich_skip_triples' THEN
        v_skip_triples := lower(v_config->>'enrich_skip_triples') IN ('true','1','on','yes');
    END IF;
    IF v_config ? 'enrich_skip_ner' THEN
        v_skip_ner := lower(v_config->>'enrich_skip_ner') IN ('true','1','on','yes');
    END IF;

    BEGIN
        v_triple_chunks := coalesce(nullif(v_config->>'enrich_max_chunks','')::integer,
                                    p_default_max_chunks, 20);
    EXCEPTION WHEN OTHERS THEN v_triple_chunks := coalesce(p_default_max_chunks, 20); END;
    BEGIN
        v_ner_chunks := coalesce(nullif(v_config->>'enrich_ner_max_chunks','')::integer,
                                 CASE WHEN v_profile = 'evidence' THEN 40 ELSE 400 END);
    EXCEPTION WHEN OTHERS THEN
        v_ner_chunks := CASE WHEN v_profile = 'evidence' THEN 40 ELSE 400 END;
    END;

    v_triple_chunks := greatest(0, least(200, v_triple_chunks));
    v_ner_chunks := greatest(1, least(1000, v_ner_chunks));
    RETURN jsonb_build_object(
        'profile', v_profile,
        'skip_triples', v_skip_triples,
        'skip_ner', v_skip_ner,
        'triple_chunks', v_triple_chunks,
        'ner_chunks', v_ner_chunks,
        'query_source', v_is_query,
        'has_derived_summary', v_has_summary
    );
END $fn$;

-- Cheap deterministic enrichment for structured catalogs. This deliberately
-- avoids both model-backed passes while retaining provider edge maps and
-- explicit [[wikilinks]]. It also cleans model-derived mentions left by an old
-- profile so the result is reproducible.
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_structured_doc(p_doc_id bigint)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    g constant text := 'brain';
    v_doclabel text;
    v_docnode bigint;
    v_source_id bigint;
    v_body text;
    v_hash text;
    v_props jsonb;
    v_edge_map jsonb;
    es record;
    v_obj jsonb;
    v_lbl text;
    v_ekind text;
    v_men_edge bigint;
    v_struct int := 0;
    v_links int := 0;
    wl text;
    v_target bigint;
BEGIN
    SELECT d.source_id, d.body, d.content_hash, d.props, p.edge_map
      INTO v_source_id, v_body, v_hash, v_props, v_edge_map
      FROM rvbbit.brain_documents d
      LEFT JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
      LEFT JOIN rvbbit.brain_doc_providers p ON p.provider = s.config->>'provider'
     WHERE d.doc_id = p_doc_id AND d.deleted_at IS NULL;
    IF NOT FOUND THEN RETURN jsonb_build_object('skipped','not found or deleted'); END IF;

    v_docnode := rvbbit.brain_doc_node(p_doc_id);
    v_doclabel := rvbbit.brain_doc_label(p_doc_id);
    DELETE FROM rvbbit.kg_edges
     WHERE graph_id = g AND subject_node_id = v_docnode
       AND (predicate_norm IN ('mentions','links_to') OR properties->>'via' = 'structured');

    IF v_props IS NOT NULL AND jsonb_typeof(v_edge_map) = 'array' THEN
        FOR es IN SELECT * FROM jsonb_to_recordset(v_edge_map)
                     AS x(predicate text, kind text, path text)
        LOOP
            CONTINUE WHEN nullif(btrim(es.predicate),'') IS NULL
                       OR nullif(btrim(es.path),'') IS NULL;
            BEGIN
                FOR v_obj IN SELECT jsonb_path_query(v_props, es.path::jsonpath) LOOP
                    v_lbl := btrim(v_obj #>> '{}');
                    CONTINUE WHEN nullif(v_lbl,'') IS NULL
                                  OR rvbbit._brain_is_junk_entity(v_lbl);
                    v_ekind := coalesce(nullif(btrim(es.kind),''), 'entity');
                    IF lower(v_ekind) = 'document' THEN v_ekind := 'reference'; END IF;
                    PERFORM rvbbit.kg_assert_edge(
                        'document', v_doclabel, es.predicate, v_ekind, v_lbl,
                        1.0, '{}'::jsonb, jsonb_build_object('via','structured'), '', 0.0, g);
                    v_men_edge := rvbbit.kg_assert_edge(
                        'document', v_doclabel, 'mentions', v_ekind, v_lbl,
                        1.0, '{}'::jsonb, jsonb_build_object('via','structured'), '', 0.0, g);
                    PERFORM rvbbit.kg_link_evidence(
                        v_men_edge, NULL, 'rvbbit.brain_documents'::regclass,
                        p_doc_id::text, 'props', es.predicate || ': ' || v_lbl,
                        1.0, '{}'::jsonb, NULL, g);
                    v_struct := v_struct + 1;
                END LOOP;
            EXCEPTION WHEN OTHERS THEN NULL;
            END;
        END LOOP;
    END IF;

    FOR wl IN SELECT DISTINCT btrim((m)[1])
                FROM regexp_matches(coalesce(v_body,''), '\[\[([^\]]+)\]\]', 'g') m
    LOOP
        CONTINUE WHEN wl = '';
        SELECT doc_id INTO v_target FROM rvbbit.brain_documents
         WHERE source_id = v_source_id AND deleted_at IS NULL AND doc_id <> p_doc_id
           AND (lower(title) = lower(wl) OR uri = wl) LIMIT 1;
        IF v_target IS NOT NULL THEN
            PERFORM rvbbit.brain_doc_node(v_target);
            PERFORM rvbbit.kg_assert_edge(
                'document', v_doclabel, 'links_to', 'document',
                rvbbit.brain_doc_label(v_target), 1.0,
                '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
        ELSE
            PERFORM rvbbit.kg_assert_edge(
                'document', v_doclabel, 'links_to', 'document', wl || ' (unresolved)', 0.4,
                '{}'::jsonb, jsonb_build_object('unresolved',true), '', 0.0, g);
        END IF;
        v_links := v_links + 1;
    END LOOP;

    UPDATE rvbbit.brain_documents
       SET enriched_at = now(), enrich_hash = v_hash
     WHERE doc_id = p_doc_id;
    RETURN jsonb_build_object(
        'doc_id',p_doc_id,'structured',v_struct,'links',v_links,
        'ner',false,'triples',false);
END $fn$;

-- Process exactly one document. A scheduler or drain procedure supplies the
-- repetition, so every successful/failed document is its own transaction.
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_next(
    p_source_id bigint DEFAULT NULL,
    p_max_chunks integer DEFAULT 20,
    p_trigger text DEFAULT 'worker'
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_source_id bigint;
    v_doc_id bigint;
    v_profile jsonb;
    v_profile_name text;
    v_result jsonb;
    v_run_id bigint;
    v_started timestamptz := clock_timestamp();
    v_error text;
    v_attempt integer;
    v_retry_at timestamptz;
BEGIN
    -- Rotate fairly among sources. Human/file/connector sources drain before
    -- high-volume query catalogs, but no individual source can monopolize its
    -- class because last_claimed_at advances after every document.
    SELECT s.source_id INTO v_source_id
      FROM rvbbit.brain_sources s
     WHERE s.enabled
       AND (p_source_id IS NULL OR s.source_id = p_source_id)
       AND EXISTS (
           SELECT 1 FROM rvbbit.brain_documents d
            WHERE d.source_id = s.source_id AND d.deleted_at IS NULL AND d.body IS NOT NULL
              AND (d.enriched_at IS NULL OR d.enrich_hash IS DISTINCT FROM d.content_hash
                   OR d.enriched_at < d.ingested_at)
              AND (d.enrich_retry_at IS NULL OR d.enrich_retry_at <= now()
                   OR d.enrich_error_hash IS DISTINCT FROM d.content_hash)
       )
     ORDER BY
       CASE WHEN nullif(s.config->>'provider','') IS NOT NULL
                  AND nullif(s.config->>'connector','') IS NULL
                  AND nullif(s.config->>'endpoint','') IS NULL THEN 1 ELSE 0 END,
       s.enrich_last_claimed_at NULLS FIRST,
       s.source_id
     LIMIT 1 FOR UPDATE SKIP LOCKED;

    IF v_source_id IS NULL THEN
        RETURN jsonb_build_object(
            'status','idle','source_id',p_source_id,
            'pipeline',rvbbit.brain_source_pipeline_status(p_source_id));
    END IF;

    SELECT d.doc_id INTO v_doc_id
      FROM rvbbit.brain_documents d
     WHERE d.source_id = v_source_id AND d.deleted_at IS NULL AND d.body IS NOT NULL
       AND (d.enriched_at IS NULL OR d.enrich_hash IS DISTINCT FROM d.content_hash
            OR d.enriched_at < d.ingested_at)
       AND (d.enrich_retry_at IS NULL OR d.enrich_retry_at <= now()
            OR d.enrich_error_hash IS DISTINCT FROM d.content_hash)
     ORDER BY
       CASE WHEN d.props->>'docType' = 'meeting_summary' OR d.props->>'derived' = 'true'
            THEN 0 ELSE 1 END,
       d.enrich_last_attempt_at NULLS FIRST,
       d.ingested_at DESC,
       d.doc_id
     LIMIT 1 FOR UPDATE SKIP LOCKED;

    IF v_doc_id IS NULL THEN
        RETURN jsonb_build_object('status','contended','source_id',v_source_id);
    END IF;

    UPDATE rvbbit.brain_sources
       SET enrich_last_claimed_at = clock_timestamp()
     WHERE source_id = v_source_id;

    v_profile := rvbbit.brain_enrichment_profile(v_doc_id, p_max_chunks);
    v_profile_name := coalesce(v_profile->>'profile','standard');
    SELECT CASE
             WHEN enrich_error_hash IS DISTINCT FROM content_hash THEN 1
             ELSE enrich_attempts + 1
           END
      INTO v_attempt FROM rvbbit.brain_documents WHERE doc_id = v_doc_id;

    INSERT INTO rvbbit.brain_enrichment_runs
        (doc_id,source_id,trigger,profile,status,attempt,started_at)
    VALUES
        (v_doc_id,v_source_id,coalesce(p_trigger,'worker'),v_profile_name,
         'running',greatest(1,v_attempt),v_started)
    RETURNING run_id INTO v_run_id;

    BEGIN
        PERFORM set_config('rvbbit.brain_skip_triples',
                           coalesce(v_profile->>'skip_triples','false'), true);
        PERFORM set_config('rvbbit.brain_ner_max_chunks',
                           coalesce(v_profile->>'ner_chunks','400'), true);

        IF coalesce((v_profile->>'skip_ner')::boolean,false) THEN
            v_result := rvbbit.brain_enrich_structured_doc(v_doc_id);
        ELSE
            v_result := rvbbit.brain_enrich_doc(
                v_doc_id, greatest(0, coalesce((v_profile->>'triple_chunks')::integer,p_max_chunks,20)));
        END IF;

        UPDATE rvbbit.brain_documents
           SET enrich_attempts = 0,
               enrich_last_attempt_at = clock_timestamp(),
               enrich_last_error = NULL,
               enrich_error_hash = NULL,
               enrich_retry_at = NULL
         WHERE doc_id = v_doc_id;
        UPDATE rvbbit.brain_enrichment_runs
           SET status='succeeded', finished_at=clock_timestamp(),
               elapsed_sec=extract(epoch FROM (clock_timestamp()-v_started)),
               result=coalesce(v_result,'{}'::jsonb)
         WHERE run_id=v_run_id;
        RETURN jsonb_build_object(
            'status','succeeded','run_id',v_run_id,'source_id',v_source_id,
            'doc_id',v_doc_id,'profile',v_profile_name,'result',v_result);
    EXCEPTION WHEN OTHERS THEN
        v_error := left(SQLERRM,2000);
        v_retry_at := clock_timestamp() + make_interval(
            secs => least(21600, (30 * power(2,least(greatest(1,v_attempt),9)))::integer));
        UPDATE rvbbit.brain_documents
           SET enrich_attempts = greatest(1,v_attempt),
               enrich_last_attempt_at = clock_timestamp(),
               enrich_last_error = v_error,
               enrich_error_hash = content_hash,
               enrich_retry_at = v_retry_at
         WHERE doc_id = v_doc_id;
        UPDATE rvbbit.brain_enrichment_runs
           SET status='failed', finished_at=clock_timestamp(),
               elapsed_sec=extract(epoch FROM (clock_timestamp()-v_started)),
               error=v_error
         WHERE run_id=v_run_id;
        RETURN jsonb_build_object(
            'status','failed','run_id',v_run_id,'source_id',v_source_id,
            'doc_id',v_doc_id,'profile',v_profile_name,
            'error',v_error,'retry_at',v_retry_at);
    END;
END $fn$;

-- The durable worker. CALL must be top-level (pg_cron and Lens' detached runner
-- both satisfy that requirement) because the procedure commits after every
-- document. It alternates one derived meeting brief with one KG pass so neither
-- stage can starve the other. max_docs=0 means no document cap; max_seconds
-- merely yields the connection so a later invocation can continue the backlog.
CREATE OR REPLACE PROCEDURE rvbbit.brain_enrich_drain(
    p_source_id bigint DEFAULT NULL,
    p_max_chunks integer DEFAULT 20,
    p_max_docs integer DEFAULT 0,
    p_max_seconds integer DEFAULT 270,
    p_trigger text DEFAULT 'worker'
) LANGUAGE plpgsql AS $proc$
DECLARE
    v_started timestamptz := clock_timestamp();
    v_processed integer := 0;
    v_one jsonb;
    v_summary jsonb;
    v_did_work boolean;
BEGIN
    LOOP
        v_did_work := false;

        v_summary := rvbbit.brain_summarize_google_meet_next(
            p_source_id, coalesce(p_trigger,'worker'));
        IF coalesce(v_summary->>'status','idle') NOT IN ('idle','contended') THEN
            v_processed := v_processed + 1;
            v_did_work := true;
            COMMIT AND CHAIN;
            EXIT WHEN p_max_docs > 0 AND v_processed >= p_max_docs;
            EXIT WHEN p_max_seconds > 0
                      AND extract(epoch FROM (clock_timestamp()-v_started)) >= p_max_seconds;
        END IF;

        v_one := rvbbit.brain_enrich_next(
            p_source_id, p_max_chunks, coalesce(p_trigger,'worker'));
        IF coalesce(v_one->>'status','idle') NOT IN ('idle','contended') THEN
            v_processed := v_processed + 1;
            v_did_work := true;

            -- This is the key durability boundary: one document, one transaction.
            COMMIT AND CHAIN;

            EXIT WHEN p_max_docs > 0 AND v_processed >= p_max_docs;
            EXIT WHEN p_max_seconds > 0
                      AND extract(epoch FROM (clock_timestamp()-v_started)) >= p_max_seconds;
        END IF;

        EXIT WHEN NOT v_did_work;
    END LOOP;
END $proc$;

CREATE OR REPLACE FUNCTION rvbbit.brain_requeue_enrichment(
    p_source_id bigint DEFAULT NULL, p_doc_id bigint DEFAULT NULL
) RETURNS integer LANGUAGE sql VOLATILE AS $fn$
    WITH changed AS (
        UPDATE rvbbit.brain_documents
           SET enriched_at=NULL, enrich_hash=NULL, enrich_attempts=0,
               enrich_last_error=NULL, enrich_error_hash=NULL, enrich_retry_at=NULL
         WHERE deleted_at IS NULL
           AND (p_source_id IS NULL OR source_id=p_source_id)
           AND (p_doc_id IS NULL OR doc_id=p_doc_id)
        RETURNING 1
    ) SELECT count(*)::integer FROM changed;
$fn$;

-- One source update has one clear public meaning: fetch current source state,
-- apply ACL/content diffs, make changed documents searchable, and expose the
-- resulting knowledge backlog. Enrichment itself continues in the worker.
CREATE OR REPLACE FUNCTION rvbbit.brain_update_source(
    p_source_id bigint,
    p_trigger text DEFAULT 'manual',
    p_refresh_vector boolean DEFAULT true
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_sync jsonb;
    v_vec jsonb := '{}'::jsonb;
    v_status jsonb;
    v_started timestamptz := clock_timestamp();
    v_error text;
BEGIN
    BEGIN
        v_sync := rvbbit.brain_sync_dispatch(p_source_id,coalesce(p_trigger,'manual'));
    EXCEPTION WHEN OTHERS THEN
        v_error := left(SQLERRM,2000);
    END;
    IF v_error IS NULL AND nullif(v_sync->>'error','') IS NOT NULL THEN
        v_error := left(v_sync->>'error',2000);
    END IF;
    IF v_error IS NOT NULL THEN
        INSERT INTO rvbbit.brain_sync_runs
            (source_id,started_at,finished_at,trigger,errors,elapsed_sec,detail)
        VALUES
            (p_source_id,v_started,clock_timestamp(),coalesce(p_trigger,'manual'),1,
             extract(epoch FROM (clock_timestamp()-v_started)),
             jsonb_build_object('pipeline_error',v_error));
        v_status := rvbbit.brain_source_pipeline_status(p_source_id);
        RETURN coalesce(v_sync,'{}'::jsonb)
               || jsonb_build_object('source_id',p_source_id,'error',v_error,
                                     'pipeline',v_status,'vector','{}'::jsonb);
    END IF;
    IF p_refresh_vector THEN
        BEGIN
            v_vec := rvbbit.vector_refresh('brain_chunks');
        EXCEPTION WHEN OTHERS THEN
            v_vec := jsonb_build_object('ok',false,'reason',left(SQLERRM,500));
        END;
    END IF;
    v_status := rvbbit.brain_source_pipeline_status(p_source_id);
    RETURN coalesce(v_sync,'{}'::jsonb)
           || jsonb_build_object('pipeline',v_status,'vector',v_vec);
END $fn$;

-- Update every source with a commit boundary between sources, then refresh the
-- ANN mirror once. A failed connector gets a durable failure run and does not
-- roll back successful sources before it.
CREATE OR REPLACE PROCEDURE rvbbit.brain_update_drain(
    p_trigger text DEFAULT 'auto'
) LANGUAGE plpgsql AS $proc$
DECLARE
    v_ids bigint[];
    v_source_id bigint;
    v_one jsonb;
    v_started timestamptz;
BEGIN
    SELECT coalesce(array_agg(s.source_id ORDER BY s.source_id),'{}'::bigint[])
      INTO v_ids
      FROM rvbbit.brain_sources s
     WHERE s.enabled
       AND (
           nullif(s.config->>'provider','') IS NOT NULL
           OR nullif(s.config->>'connector','') IS NOT NULL
           OR nullif(s.config->>'endpoint','') IS NOT NULL
           OR (s.kind IN ('gdrive','google_drive','file_mirror','remote')
               AND EXISTS (SELECT 1 FROM rvbbit.backends b
                            WHERE b.name='gdrive_connector'))
       );

    FOREACH v_source_id IN ARRAY v_ids LOOP
        v_started := clock_timestamp();
        BEGIN
            v_one := rvbbit.brain_update_source(
                v_source_id, coalesce(p_trigger,'auto'), false);
        EXCEPTION WHEN OTHERS THEN
            INSERT INTO rvbbit.brain_sync_runs
                (source_id,started_at,finished_at,trigger,errors,elapsed_sec,detail)
            VALUES
                (v_source_id,v_started,clock_timestamp(),coalesce(p_trigger,'auto'),1,
                 extract(epoch FROM (clock_timestamp()-v_started)),
                 jsonb_build_object('pipeline_error',left(SQLERRM,2000)));
        END;
        COMMIT AND CHAIN;
    END LOOP;

    BEGIN
        PERFORM rvbbit.vector_refresh('brain_chunks');
    EXCEPTION WHEN OTHERS THEN
        NULL; -- source/index commits remain valid; lexical fallback still works
    END;
END $proc$;

-- Compatibility for already-installed `SELECT brain_nightly()` cron jobs. It
-- no longer performs a capped enrichment batch; the continuous drain owns that
-- stage. New schedules use CALL brain_update_drain() for per-source commits.
CREATE OR REPLACE FUNCTION rvbbit.brain_nightly(
    p_max_docs integer DEFAULT 100, p_max_chunks integer DEFAULT 20
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_sync jsonb; v_qsync jsonb; v_vec jsonb; v_status jsonb;
BEGIN
    v_sync := rvbbit.brain_sync_sources('auto');
    v_qsync := rvbbit.brain_sync_query_sources('auto');
    BEGIN v_vec := rvbbit.vector_refresh('brain_chunks');
    EXCEPTION WHEN OTHERS THEN v_vec := jsonb_build_object('ok',false,'reason',SQLERRM); END;
    v_status := rvbbit.brain_source_pipeline_status(NULL);
    RETURN jsonb_build_object(
        'sync',v_sync,'query_sync',v_qsync,'vector',v_vec,
        'enrichment',v_status,
        'worker','CALL rvbbit.brain_enrich_drain()');
END $fn$;

COMMENT ON FUNCTION rvbbit.brain_nightly(integer,integer) IS
    'Compatibility source update. Enrichment is continuously drained one committed document at a time by brain_enrich_drain().';

COMMENT ON FUNCTION rvbbit.brain_enrich_pending(integer,integer) IS
    'Legacy same-transaction batch helper. Automatic processing uses brain_enrich_next/brain_enrich_drain so each document commits independently.';

COMMENT ON FUNCTION rvbbit.brain_enrich_source(bigint,boolean,integer,boolean) IS
    'Explicit maintenance helper for a synchronous source-wide rebuild. Normal source updates queue changed docs for brain_enrich_drain.';

-- Query/MCP listings are not assumed authoritative. A partial page, transient
-- upstream limit, or ACL-scoped response must never make previously indexed
-- documents disappear. Providers that truly return a complete snapshot can
-- opt back in with {"tombstone_missing":true}.
UPDATE rvbbit.brain_sources
   SET config = jsonb_set(coalesce(config,'{}'::jsonb),'{tombstone_missing}','false'::jsonb,true)
 WHERE nullif(config->>'provider','') IS NOT NULL
   AND nullif(config->>'connector','') IS NULL
   AND nullif(config->>'endpoint','') IS NULL
   AND NOT (config ? 'tombstone_missing');

CREATE OR REPLACE FUNCTION rvbbit.brain_add_query_source(
    p_label text, p_provider text, p_config jsonb DEFAULT '{}'::jsonb,
    p_enabled boolean DEFAULT true
) RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_id bigint; v_config jsonb;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rvbbit.brain_doc_providers WHERE provider=p_provider) THEN
        RAISE EXCEPTION 'brain_add_query_source: provider "%" is not defined', p_provider;
    END IF;
    v_config := jsonb_build_object('tombstone_missing',false)
                || coalesce(p_config,'{}'::jsonb)
                || jsonb_build_object('provider',p_provider);
    v_id := rvbbit.brain_configure_source(
        p_label,'query',v_config,NULL,NULL,p_enabled);
    RETURN v_id;
END $fn$;

-- Fireflies exposes skip/limit pagination (limit <= 50). The old provider read
-- one page on each side of the current corpus, so repeated Index clicks appeared
-- to grow 50 -> 89 -> 125 and an incomplete listing could tombstone the rest.
-- Fetch every page in one update and retain old rows defensively if the upstream
-- account ever returns a partial view.
UPDATE rvbbit.brain_doc_providers
SET list_sql = $provider$
WITH RECURSIVE pages(skip_n, items, n) AS (
    SELECT 0,
           page.items,
           jsonb_array_length(page.items)
      FROM LATERAL (
          SELECT coalesce(jsonb_agg(r),'[]'::jsonb) AS items
            FROM rvbbit.mcp_rows(
                'fireflies','fireflies_get_transcripts',
                jsonb_strip_nulls(jsonb_build_object(
                    'limit',50,'skip',0,'format','json',
                    'fromDate',nullif($1->>'from_date',''),
                    'mine',CASE WHEN lower(coalesce($1->>'mine','')) IN ('true','1','yes','on')
                                THEN true ELSE NULL END
                ))) r
      ) page
    UNION ALL
    SELECT pages.skip_n + 50,
           page.items,
           jsonb_array_length(page.items)
      FROM pages
      CROSS JOIN LATERAL (
          SELECT coalesce(jsonb_agg(r),'[]'::jsonb) AS items
            FROM rvbbit.mcp_rows(
                'fireflies','fireflies_get_transcripts',
                jsonb_strip_nulls(jsonb_build_object(
                    'limit',50,'skip',pages.skip_n + 50,'format','json',
                    'fromDate',nullif($1->>'from_date',''),
                    'mine',CASE WHEN lower(coalesce($1->>'mine','')) IN ('true','1','yes','on')
                                THEN true ELSE NULL END
                ))) r
      ) page
     WHERE pages.n = 50
       AND pages.skip_n + 50 < greatest(50,least(50000,
           coalesce(nullif($1->>'sync_max_items','')::integer,10000)))
), raw AS (
    SELECT item AS r
      FROM pages CROSS JOIN LATERAL jsonb_array_elements(pages.items) item
), shaped AS (
    SELECT DISTINCT ON (r->>'id')
           'fireflies:'||(r->>'id') AS uri,
           concat_ws(' · ',nullif(r->>'title',''),
                     to_char(nullif(r->>'dateString','')::timestamptz,'YYYY-MM-DD')) AS title,
           -- Preserve the provider's established semantic hash so upgrading
           -- the pagination strategy does not make every historical meeting
           -- look changed and force a needless re-chunk/re-embed cycle.
           md5(coalesce(r#>>'{summary,short_summary}','') ||
               coalesce(r#>>'{summary,action_items}','')) AS content_hash,
           nullif(r->>'dateString','')::timestamptz AS occurred_at,
           concat_ws(E'\n\n',
             nullif(r->>'title',''),
             nullif('Date: '||(r->>'dateString'),'Date: '),
             nullif('Organizer: '||(r->>'organizerEmail'),'Organizer: '),
             CASE WHEN jsonb_typeof(r->'participants')='array'
                  THEN nullif('Participants: '||array_to_string(
                       ARRAY(SELECT jsonb_array_elements_text(r->'participants')),', '),'Participants: ')
                  ELSE NULL END,
             nullif('Summary: '||(r#>>'{summary,short_summary}'),'Summary: '),
             nullif('Action items: '||(r#>>'{summary,action_items}'),'Action items: '),
             CASE WHEN jsonb_typeof(r->'summary'->'keywords')='array'
                  THEN nullif('Keywords: '||array_to_string(
                       ARRAY(SELECT jsonb_array_elements_text(r->'summary'->'keywords')),', '),'Keywords: ')
                  ELSE NULL END
           ) AS body,
           r AS props
      FROM raw
     WHERE nullif(r->>'id','') IS NOT NULL
     ORDER BY r->>'id',nullif(r->>'dateString','')::timestamptz DESC NULLS LAST
)
SELECT uri,title,content_hash,occurred_at,body,props FROM shaped
$provider$,
    description = 'Fireflies meeting summaries with complete skip/limit pagination; partial upstream responses retain existing documents.',
    updated_at = now()
WHERE provider='fireflies-meetings';

UPDATE rvbbit.brain_sources
   SET config = coalesce(config,'{}'::jsonb)
                || jsonb_build_object('tombstone_missing',false)
 WHERE config->>'provider'='fireflies-meetings';

UPDATE rvbbit.brain_sources
   SET config = coalesce(config,'{}'::jsonb)
                || jsonb_build_object('enrichment_profile','structured')
 WHERE config->>'provider'='rvbbit-system-learning'
   AND NOT (config ? 'enrichment_profile');

-- summary_max_docs was the old inline-transaction safety valve. It has no
-- meaning once the worker drains every outstanding meeting one committed
-- document at a time, so stop seeding or retaining the misleading knob.
UPDATE rvbbit.brain_sources
   SET config = coalesce(config,'{}'::jsonb) - 'summary_max_docs'
 WHERE kind='google_meet' OR config->>'provider'='google-meet';

CREATE OR REPLACE FUNCTION rvbbit.brain_configure_google_meet_source(
    p_label text DEFAULT 'Google Meet',
    p_enabled boolean DEFAULT true,
    p_options jsonb DEFAULT '{}'::jsonb
) RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_id bigint; v_config jsonb;
BEGIN
    IF jsonb_typeof(coalesce(p_options,'{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'brain_configure_google_meet_source: p_options must be a JSON object';
    END IF;
    v_config := jsonb_build_object(
        'connector','gmeet_connector',
        'provider','google-meet',
        'doc_type','meeting',
        'source_kind','google_meet',
        'tombstone_missing',false,
        'lookback_days',29,
        'calendar_lookup',true,
        'auto_transcribe',false,
        'auto_transcribe_days',7,
        'drive_acl',true,
        'acl_mode','calendar_invitees_strict',
        'summarize_meetings',true
    ) || (coalesce(p_options,'{}'::jsonb) - ARRAY[
        'connector','provider','doc_type','source_kind','tombstone_missing',
        'summary_max_docs'
    ]::text[]);
    v_id := rvbbit.brain_configure_source(
        coalesce(nullif(btrim(p_label),''),'Google Meet'),
        'google_meet',v_config,'GMEET_CONNECTOR_TOKEN','/meetings',coalesce(p_enabled,true));
    UPDATE rvbbit.brain_sources SET kind='google_meet' WHERE source_id=v_id;
    RETURN v_id;
END $fn$;

COMMENT ON FUNCTION rvbbit.brain_configure_google_meet_source(text,boolean,jsonb) IS
    'Create/update an append-safe Google Meet transcript source. Optional summaries are queued for the durable per-document Brain worker; summary_max_docs is obsolete.';
