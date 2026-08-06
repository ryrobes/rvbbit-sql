-- 0250: Google Meet transcripts as governed, append-safe Brain documents.
--
-- Meet's structured conference/transcript resources disappear after 30 days.
-- A connector therefore cannot honestly return an all-time authoritative
-- manifest.  This migration makes "tombstone_missing" a source policy (true by
-- default for Drive and existing connectors), enriches the connector manifest
-- with author/occurred_at/structured props, and registers the reusable Meet
-- provider + compose endpoint.  Google Meet sources set tombstone_missing=false:
-- the latest window is reconciled while older evidence stays in the Brain.

ALTER TABLE rvbbit.brain_sync_manifest
    ADD COLUMN IF NOT EXISTS author text,
    ADD COLUMN IF NOT EXISTS occurred_at timestamptz,
    ADD COLUMN IF NOT EXISTS props jsonb;

-- Connector routing stays server-owned. Only non-secret source options cross
-- the HTTP boundary; the service-account JSON remains in the sidecar and the
-- optional bearer value remains an env-var reference on the backend row.
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_request(p_source_id bigint)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    SELECT jsonb_build_object(
        'endpoint', coalesce(s.config->>'endpoint', b.endpoint_url),
        'auth_env', coalesce(s.config->>'auth_env', b.auth_header_env),
        'payload', jsonb_build_object(
            'source_id', s.source_id,
            'folders',   coalesce(s.config->'folders', '[]'::jsonb),
            'cursor',    s.sync_cursor,
            'known',     coalesce((SELECT jsonb_object_agg(d.uri, d.content_hash)
                                   FROM rvbbit.brain_documents d
                                   WHERE d.source_id = s.source_id AND d.deleted_at IS NULL
                                     AND d.uri IS NOT NULL AND d.content_hash IS NOT NULL), '{}'::jsonb),
            'options', jsonb_strip_nulls(jsonb_build_object(
                'subjects',        s.config->'subjects',
                'admin_subject',   s.config->>'admin_subject',
                'domain',          s.config->>'domain',
                'lookback_days',   s.config->'lookback_days',
                'max_subjects',    s.config->'max_subjects',
                'discover_users',  s.config->'discover_users',
                'calendar_lookup', s.config->'calendar_lookup',
                'auto_transcribe', s.config->'auto_transcribe',
                'auto_transcribe_days', s.config->'auto_transcribe_days',
                'drive_acl',       s.config->'drive_acl',
                'acl_mode',        s.config->>'acl_mode'
            ))
        )
    )
    FROM rvbbit.brain_sources s
    LEFT JOIN rvbbit.backends b
      ON b.name = coalesce(s.config->>'connector', 'gdrive_connector')
    WHERE s.source_id = p_source_id;
$fn$;

-- Land the connector's full current WINDOW. The apply policy below decides
-- whether absence means deletion (ordinary mirrors) or retention (Meet).
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_write_manifest(
    p_source_id bigint, p_files jsonb, p_pending jsonb DEFAULT '[]'::jsonb, p_cursor text DEFAULT NULL
) RETURNS int LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE n int := 0;
BEGIN
    DELETE FROM rvbbit.brain_sync_manifest WHERE source_id = p_source_id;
    INSERT INTO rvbbit.brain_sync_manifest
        (source_id, uri, title, rel_path, folder_id, mime, modified_at,
         occurred_at, author, content_hash, permissions, staged_path, body, props)
    SELECT p_source_id,
           f->>'uri',
           f->>'title',
           coalesce(f->>'rel_path', '/'),
           f->>'folder_id',
           f->>'mime',
           nullif(f->>'modified_at','')::timestamptz,
           nullif(f->>'occurred_at','')::timestamptz,
           nullif(f->>'author',''),
           f->>'content_hash',
           coalesce((SELECT array_agg(p) FROM jsonb_array_elements_text(
               CASE WHEN jsonb_typeof(f->'permissions') = 'array' THEN f->'permissions' ELSE '[]'::jsonb END
           ) p), '{}'),
           f->>'staged_path',
           f->>'body',
           CASE WHEN jsonb_typeof(f->'props') = 'object' THEN f->'props' ELSE NULL END
    FROM jsonb_array_elements(coalesce(p_files, '[]'::jsonb)) f
    WHERE nullif(f->>'uri','') IS NOT NULL;
    GET DIAGNOSTICS n = ROW_COUNT;

    DELETE FROM rvbbit.brain_pending_grants WHERE source_id = p_source_id AND NOT approved;
    INSERT INTO rvbbit.brain_pending_grants (source_id, folder_id, grant_kind, grant_value)
    SELECT p_source_id, coalesce(g->>'folder_id',''), g->>'grant_kind', coalesce(g->>'grant_value','')
    FROM jsonb_array_elements(coalesce(p_pending, '[]'::jsonb)) g
    WHERE nullif(g->>'grant_kind','') IS NOT NULL
    ON CONFLICT DO NOTHING;

    UPDATE rvbbit.brain_sources
       SET sync_cursor = coalesce(p_cursor, sync_cursor)
     WHERE source_id = p_source_id;
    RETURN n;
END $fn$;

-- Meeting briefs are an optional derived layer, never the canonical evidence.
-- Try Clover first, then the ordinary summarizer. Every invocation is isolated
-- so a missing/misconfigured semantic backend cannot abort transcript sync.
CREATE OR REPLACE FUNCTION rvbbit._brain_google_meet_summary(p_text text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_out text;
    v_seen boolean := false;
    v_errors jsonb := '[]'::jsonb;
BEGIN
    IF to_regprocedure('rvbbit.clover_summarize(text,jsonb)') IS NOT NULL THEN
        v_seen := true;
        BEGIN
            EXECUTE 'SELECT rvbbit.clover_summarize($1,$2)::text'
               INTO v_out USING p_text, '{}'::jsonb;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors || jsonb_build_array(jsonb_build_object(
                'operator', 'clover_summarize', 'error', left(SQLERRM, 300)));
        END;
        IF nullif(btrim(v_out), '') IS NOT NULL AND lower(btrim(v_out)) NOT IN ('null','""','{}','[]') THEN
            RETURN jsonb_build_object('ok', true, 'operator', 'clover_summarize', 'summary', v_out);
        END IF;
    END IF;

    IF to_regprocedure('rvbbit.clover_summarize(text)') IS NOT NULL THEN
        v_seen := true; v_out := NULL;
        BEGIN
            EXECUTE 'SELECT rvbbit.clover_summarize($1)::text' INTO v_out USING p_text;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors || jsonb_build_array(jsonb_build_object(
                'operator', 'clover_summarize', 'error', left(SQLERRM, 300)));
        END;
        IF nullif(btrim(v_out), '') IS NOT NULL AND lower(btrim(v_out)) NOT IN ('null','""','{}','[]') THEN
            RETURN jsonb_build_object('ok', true, 'operator', 'clover_summarize', 'summary', v_out);
        END IF;
    END IF;

    -- Older Clover catalogs exposed this spelling. Keep it ahead of the
    -- generic operator so upgraded installations retain Clover priority.
    IF to_regprocedure('rvbbit.clover_llm_summarize(text,jsonb)') IS NOT NULL THEN
        v_seen := true; v_out := NULL;
        BEGIN
            EXECUTE 'SELECT rvbbit.clover_llm_summarize($1,$2)::text'
               INTO v_out USING p_text, '{}'::jsonb;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors || jsonb_build_array(jsonb_build_object(
                'operator', 'clover_llm_summarize', 'error', left(SQLERRM, 300)));
        END;
        IF nullif(btrim(v_out), '') IS NOT NULL AND lower(btrim(v_out)) NOT IN ('null','""','{}','[]') THEN
            RETURN jsonb_build_object('ok', true, 'operator', 'clover_llm_summarize', 'summary', v_out);
        END IF;
    END IF;

    IF to_regprocedure('rvbbit.clover_llm_summarize(text)') IS NOT NULL THEN
        v_seen := true; v_out := NULL;
        BEGIN
            EXECUTE 'SELECT rvbbit.clover_llm_summarize($1)::text' INTO v_out USING p_text;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors || jsonb_build_array(jsonb_build_object(
                'operator', 'clover_llm_summarize', 'error', left(SQLERRM, 300)));
        END;
        IF nullif(btrim(v_out), '') IS NOT NULL AND lower(btrim(v_out)) NOT IN ('null','""','{}','[]') THEN
            RETURN jsonb_build_object('ok', true, 'operator', 'clover_llm_summarize', 'summary', v_out);
        END IF;
    END IF;

    IF to_regprocedure('rvbbit.summarize(text,jsonb)') IS NOT NULL THEN
        v_seen := true; v_out := NULL;
        BEGIN
            EXECUTE 'SELECT rvbbit.summarize($1,$2)::text'
               INTO v_out USING p_text, '{}'::jsonb;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors || jsonb_build_array(jsonb_build_object(
                'operator', 'summarize', 'error', left(SQLERRM, 300)));
        END;
        IF nullif(btrim(v_out), '') IS NOT NULL AND lower(btrim(v_out)) NOT IN ('null','""','{}','[]') THEN
            RETURN jsonb_build_object('ok', true, 'operator', 'summarize', 'summary', v_out);
        END IF;
    END IF;

    IF to_regprocedure('rvbbit.summarize(text)') IS NOT NULL THEN
        v_seen := true; v_out := NULL;
        BEGIN
            EXECUTE 'SELECT rvbbit.summarize($1)::text' INTO v_out USING p_text;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors || jsonb_build_array(jsonb_build_object(
                'operator', 'summarize', 'error', left(SQLERRM, 300)));
        END;
        IF nullif(btrim(v_out), '') IS NOT NULL AND lower(btrim(v_out)) NOT IN ('null','""','{}','[]') THEN
            RETURN jsonb_build_object('ok', true, 'operator', 'summarize', 'summary', v_out);
        END IF;
    END IF;

    RETURN jsonb_build_object(
        'ok', false,
        'reason', CASE WHEN v_seen THEN 'summarizer_failed' ELSE 'no_summarizer' END,
        'errors', v_errors
    );
EXCEPTION WHEN OTHERS THEN
    -- This helper is deliberately fail-soft: it is called from the sync path.
    RETURN jsonb_build_object('ok', false, 'reason', 'summary_dispatch_failed',
                              'errors', jsonb_build_array(left(SQLERRM, 300)));
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_summarize_google_meet_doc(
    p_doc_id bigint, p_max_chars int DEFAULT 120000
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    rec record;
    v_roles text[];
    v_uri text;
    v_source_hash text;
    v_existing bigint;
    v_existing_hash text;
    v_transcript text;
    v_prompt text;
    v_result jsonb;
    v_summary text;
    v_operator text;
    v_body text;
    v_summary_doc bigint;
BEGIN
    SELECT d.doc_id, d.source_id, d.uri, d.title, d.author, d.folder_path,
           d.body, d.occurred_at, d.content_hash, d.props,
           s.label AS source_label, s.kind AS source_kind, s.config AS source_config,
           s.default_roles AS source_default_roles, s.folder_prefix AS source_folder_prefix
      INTO rec
      FROM rvbbit.brain_documents d
      JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
     WHERE d.doc_id = p_doc_id AND d.deleted_at IS NULL
       AND d.uri LIKE 'gmeet:%' AND d.body IS NOT NULL;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'skipped', 'reason', 'not_google_meet_transcript',
                                  'doc_id', p_doc_id);
    END IF;

    v_source_hash := coalesce(nullif(rec.content_hash, ''), md5(rec.body));
    v_uri := 'gmeet-summary:' || substr(rec.uri, length('gmeet:') + 1);
    SELECT d.doc_id, d.raw_meta->>'source_content_hash'
      INTO v_existing, v_existing_hash
      FROM rvbbit.brain_documents d
     WHERE d.source_id = rec.source_id AND d.uri = v_uri;
    IF v_existing IS NOT NULL AND v_existing_hash = v_source_hash
       AND EXISTS (SELECT 1 FROM rvbbit.brain_documents d
                    WHERE d.doc_id = v_existing AND d.deleted_at IS NULL) THEN
        RETURN jsonb_build_object('status', 'current', 'doc_id', p_doc_id,
                                  'summary_doc_id', v_existing);
    END IF;

    v_transcript := rec.body;
    IF length(v_transcript) > greatest(12000, p_max_chars) THEN
        v_transcript := left(v_transcript, greatest(6000, p_max_chars * 2 / 3))
            || E'\n\n[... middle of transcript omitted for summary context budget ...]\n\n'
            || right(v_transcript, greatest(6000, p_max_chars / 3));
    END IF;
    v_prompt :=
        'Create a durable internal meeting brief from the transcript below. '
        || 'Return concise Markdown only. Include: Summary; Decisions; Action Items '
        || '(owner and due date when stated); Open Questions and Risks; and Referenced '
        || 'Projects, Tickets, Documents, or Metrics. Do not invent facts, owners, or dates. '
        || 'If a section has no evidence, say None identified. Preserve useful names and '
        || 'include transcript timestamps beside consequential claims when available.'
        || E'\n\nMEETING TRANSCRIPT:\n' || v_transcript;

    v_result := rvbbit._brain_google_meet_summary(v_prompt);
    IF NOT coalesce((v_result->>'ok')::boolean, false) THEN
        RETURN jsonb_build_object('status', 'skipped', 'doc_id', p_doc_id,
                                  'reason', coalesce(v_result->>'reason', 'summary_unavailable'),
                                  'detail', coalesce(v_result->'errors', '[]'::jsonb));
    END IF;
    v_summary := left(btrim(v_result->>'summary'), 30000);
    v_operator := v_result->>'operator';
    IF nullif(v_summary, '') IS NULL THEN
        RETURN jsonb_build_object('status', 'skipped', 'doc_id', p_doc_id,
                                  'reason', 'empty_summary');
    END IF;
    v_body := '# ' || rec.title || E'\n\n## Derived meeting brief\n\n' || v_summary
        || E'\n\n---\n\n_Source transcript retained as canonical evidence._\n';

    SELECT coalesce(array_agg(role ORDER BY role), '{}')
      INTO v_roles FROM rvbbit.brain_doc_roles WHERE doc_id = p_doc_id;
    v_summary_doc := rvbbit.brain_ingest(
        rec.source_label,
        'Brief · ' || rec.title,
        v_body,
        v_roles,
        rec.folder_path,
        v_uri,
        rec.author,
        rec.occurred_at,
        jsonb_build_object(
            'derived_from_doc_id', p_doc_id,
            'derived_from_uri', rec.uri,
            'source_content_hash', v_source_hash,
            'summary_operator', v_operator
        )
    );
    -- brain_ingest's ergonomic source upsert must not turn a connector source
    -- back into a manual source.
    UPDATE rvbbit.brain_sources
       SET kind = rec.source_kind, config = rec.source_config,
           default_roles = rec.source_default_roles,
           folder_prefix = rec.source_folder_prefix
     WHERE source_id = rec.source_id;

    UPDATE rvbbit.brain_documents
       SET content_hash = md5(v_body), mime = 'text/markdown', deleted_at = NULL,
           props = coalesce(rec.props, '{}'::jsonb) || jsonb_build_object(
               'provider', 'google_meet',
               'docType', 'meeting_summary',
               'derived', true,
               'derivedFromDocId', p_doc_id,
               'derivedFromUri', rec.uri,
               'sourceContentHash', v_source_hash,
               'summaryOperator', v_operator
           ),
           enriched_at = NULL, enrich_hash = NULL
     WHERE doc_id = v_summary_doc;

    -- The exclusion belt is document-specific, so copy it in addition to the
    -- shared role. This keeps the derived doc's effective ACL identical.
    DELETE FROM rvbbit.brain_doc_exclude WHERE doc_id = v_summary_doc;
    INSERT INTO rvbbit.brain_doc_exclude (doc_id, principal, reason)
    SELECT v_summary_doc, principal, reason
      FROM rvbbit.brain_doc_exclude WHERE doc_id = p_doc_id
    ON CONFLICT (doc_id, principal) DO UPDATE SET reason = excluded.reason;

    BEGIN
        PERFORM rvbbit.brain_doc_node(v_summary_doc);
        PERFORM rvbbit.brain_doc_node(p_doc_id);
        PERFORM rvbbit.kg_assert_edge(
            'document', rvbbit.brain_doc_label(v_summary_doc), 'derived_from',
            'document', rvbbit.brain_doc_label(p_doc_id), 1.0,
            '{}'::jsonb, jsonb_build_object('via', 'google_meet_summary'), '', 0.0, 'brain'
        );
    EXCEPTION WHEN OTHERS THEN
        -- The explicit raw_meta linkage is sufficient for correctness; a KG
        -- edge is an enhancement and must not discard an otherwise valid brief.
        NULL;
    END;
    RETURN jsonb_build_object('status', CASE WHEN v_existing IS NULL THEN 'created' ELSE 'updated' END,
                              'doc_id', p_doc_id, 'summary_doc_id', v_summary_doc,
                              'operator', v_operator);
EXCEPTION WHEN OTHERS THEN
    -- Summary generation is additive. Raw transcript ingestion must remain
    -- successful even when a semantic operator or embedder is unhealthy.
    RETURN jsonb_build_object('status', 'skipped', 'doc_id', p_doc_id,
                              'reason', 'summary_write_failed', 'detail', left(SQLERRM, 300));
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_summarize_google_meet_pending(
    p_source_id bigint, p_max_docs int DEFAULT 12
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    rec record;
    v_result jsonb;
    v_operator_available boolean;
    n_created int := 0; n_updated int := 0; n_current int := 0;
    n_skipped int := 0; v_details jsonb := '[]'::jsonb; v_halted text;
BEGIN
    v_operator_available :=
        to_regprocedure('rvbbit.clover_summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_summarize(text)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_llm_summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.clover_llm_summarize(text)') IS NOT NULL OR
        to_regprocedure('rvbbit.summarize(text,jsonb)') IS NOT NULL OR
        to_regprocedure('rvbbit.summarize(text)') IS NOT NULL;
    IF NOT v_operator_available THEN
        RETURN jsonb_build_object('available', false, 'reason', 'no_summarizer',
                                  'created', 0, 'updated', 0, 'skipped', 0);
    END IF;

    FOR rec IN
        SELECT raw.doc_id
          FROM rvbbit.brain_documents raw
          LEFT JOIN rvbbit.brain_documents summary
            ON summary.source_id = raw.source_id
           AND summary.uri = 'gmeet-summary:' || substr(raw.uri, length('gmeet:') + 1)
           AND summary.deleted_at IS NULL
         WHERE raw.source_id = p_source_id AND raw.deleted_at IS NULL
           AND raw.uri LIKE 'gmeet:%' AND raw.body IS NOT NULL
           AND (summary.doc_id IS NULL OR summary.raw_meta->>'source_content_hash'
                IS DISTINCT FROM coalesce(nullif(raw.content_hash, ''), md5(raw.body)))
         ORDER BY raw.occurred_at DESC NULLS LAST, raw.doc_id DESC
         LIMIT greatest(0, p_max_docs)
    LOOP
        v_result := rvbbit.brain_summarize_google_meet_doc(rec.doc_id);
        CASE v_result->>'status'
            WHEN 'created' THEN n_created := n_created + 1;
            WHEN 'updated' THEN n_updated := n_updated + 1;
            WHEN 'current' THEN n_current := n_current + 1;
            ELSE
                n_skipped := n_skipped + 1;
                IF jsonb_array_length(v_details) < 10 THEN v_details := v_details || v_result; END IF;
                IF v_result->>'reason' IN ('summarizer_failed','summary_dispatch_failed','summary_write_failed') THEN
                    v_halted := v_result->>'reason';
                    EXIT; -- one systemic failure is enough; do not fan it across the backlog
                END IF;
        END CASE;
    END LOOP;
    RETURN jsonb_build_object('available', true, 'created', n_created, 'updated', n_updated,
                              'current', n_current, 'skipped', n_skipped, 'halted', v_halted,
                              'details', v_details);
EXCEPTION WHEN OTHERS THEN
    RETURN jsonb_build_object('available', true, 'created', n_created, 'updated', n_updated,
                              'current', n_current, 'skipped', n_skipped,
                              'reason', 'summary_batch_failed', 'detail', left(SQLERRM, 300));
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_sync_apply_manifest(
    p_source_id bigint, p_trigger text DEFAULT 'manual'
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_label text;
    v_run bigint;
    v_added int := 0; v_changed int := 0; v_removed int := 0;
    v_skipped int := 0; v_errors int := 0;
    rec record;
    v_doc bigint;
    v_role text;
    v_existing rvbbit.brain_documents%ROWTYPE;
    v_t0 timestamptz := clock_timestamp();
    v_config jsonb;
    v_kind text;
    v_default_roles text[];
    v_folder_prefix text;
    v_tombstone_missing boolean;
    v_meeting_summaries jsonb := '{}'::jsonb;
BEGIN
    SELECT label, coalesce(config, '{}'::jsonb), kind, default_roles, folder_prefix
      INTO v_label, v_config, v_kind, v_default_roles, v_folder_prefix
      FROM rvbbit.brain_sources WHERE source_id = p_source_id;
    IF v_label IS NULL THEN
        RAISE EXCEPTION 'brain_sync_apply_manifest: source % not found', p_source_id;
    END IF;
    v_tombstone_missing := lower(coalesce(v_config->>'tombstone_missing', 'true'))
                           NOT IN ('false','0','off','no');

    INSERT INTO rvbbit.brain_sync_runs (source_id, trigger)
    VALUES (p_source_id, coalesce(p_trigger, 'manual'))
    RETURNING run_id INTO v_run;

    PERFORM rvbbit.brain_sync_acl(p_source_id);

    FOR rec IN SELECT * FROM rvbbit.brain_sync_manifest WHERE source_id = p_source_id LOOP
        v_role := rvbbit.brain_folder_role(p_source_id, rec.folder_id);
        SELECT * INTO v_existing FROM rvbbit.brain_documents
         WHERE source_id = p_source_id AND uri = rec.uri;

        IF v_existing.doc_id IS NOT NULL
           AND v_existing.deleted_at IS NULL
           AND v_existing.content_hash IS NOT DISTINCT FROM rec.content_hash
           AND rec.content_hash IS NOT NULL THEN
            -- Metadata and ACLs can change without body bytes changing. Keep the
            -- document current without paying the re-chunk/re-embed cost.
            UPDATE rvbbit.brain_documents
               SET title = coalesce(nullif(rec.title,''), title),
                   author = coalesce(rec.author, author),
                   occurred_at = coalesce(rec.occurred_at, rec.modified_at, occurred_at),
                   mime = coalesce(rec.mime, mime),
                   props = coalesce(rec.props, props),
                   raw_meta = raw_meta || jsonb_strip_nulls(jsonb_build_object(
                       'sync_uri', rec.uri, 'folder_id', rec.folder_id,
                       'mime', rec.mime, 'staged_path', rec.staged_path,
                       'content_hash', rec.content_hash)),
                   deleted_at = NULL
             WHERE doc_id = v_existing.doc_id;
            DELETE FROM rvbbit.brain_doc_roles dr
             USING rvbbit.brain_roles r
             WHERE dr.doc_id = v_existing.doc_id AND r.role = dr.role
               AND r.origin = 'sync' AND r.role LIKE format('sync/%s/%%', p_source_id);
            INSERT INTO rvbbit.brain_doc_roles (doc_id, role)
            VALUES (v_existing.doc_id, v_role) ON CONFLICT DO NOTHING;
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;

        IF rec.body IS NULL THEN
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;

        BEGIN
            v_doc := rvbbit.brain_ingest(
                v_label,
                coalesce(rec.title, rec.uri),
                rec.body,
                ARRAY[v_role],
                coalesce(rec.rel_path, '/'),
                rec.uri,
                rec.author,
                coalesce(rec.occurred_at, rec.modified_at),
                jsonb_strip_nulls(jsonb_build_object(
                    'sync_uri', rec.uri, 'folder_id', rec.folder_id,
                    'mime', rec.mime, 'staged_path', rec.staged_path,
                    'content_hash', rec.content_hash
                ))
            );
            UPDATE rvbbit.brain_documents
               SET content_hash = rec.content_hash,
                   mime = coalesce(rec.mime, mime),
                   props = coalesce(rec.props, props),
                   deleted_at = NULL
             WHERE doc_id = v_doc;
            IF v_existing.doc_id IS NULL THEN
                v_added := v_added + 1;
            ELSE
                v_changed := v_changed + 1;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors + 1;
        END;
    END LOOP;

    IF v_tombstone_missing THEN
        WITH gone AS (
            UPDATE rvbbit.brain_documents d SET deleted_at = now()
             WHERE d.source_id = p_source_id AND d.deleted_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM rvbbit.brain_sync_manifest m
                               WHERE m.source_id = p_source_id AND m.uri = d.uri)
            RETURNING d.doc_id
        )
        SELECT count(*) INTO v_removed FROM gone;
        DELETE FROM rvbbit.brain_doc_roles dr
         USING rvbbit.brain_documents d
         WHERE dr.doc_id = d.doc_id AND d.source_id = p_source_id AND d.deleted_at IS NOT NULL;
    END IF;

    IF v_kind = 'google_meet'
       AND lower(coalesce(v_config->>'summarize_meetings', 'true')) NOT IN ('false','0','off','no') THEN
        BEGIN
            v_meeting_summaries := rvbbit.brain_summarize_google_meet_pending(
                p_source_id,
                greatest(0, least(100, coalesce(nullif(v_config->>'summary_max_docs','')::int, 12)))
            );
        EXCEPTION WHEN OTHERS THEN
            v_meeting_summaries := jsonb_build_object(
                'available', true, 'reason', 'summary_batch_failed', 'detail', left(SQLERRM, 300));
        END;
    ELSE
        v_meeting_summaries := jsonb_build_object('available', false, 'reason', 'disabled');
    END IF;

    -- brain_ingest's ergonomic source upsert uses manual defaults. Restore the
    -- actual connector identity and source settings after the batch.
    UPDATE rvbbit.brain_sources
       SET kind = coalesce(nullif(v_config->>'source_kind',''), v_kind),
           config = v_config,
           default_roles = v_default_roles,
           folder_prefix = v_folder_prefix,
           last_synced_at = now()
     WHERE source_id = p_source_id;

    UPDATE rvbbit.brain_sync_runs
       SET finished_at = clock_timestamp(),
           added = v_added, changed = v_changed, removed = v_removed,
           skipped = v_skipped, errors = v_errors,
           elapsed_sec = EXTRACT(EPOCH FROM (clock_timestamp() - v_t0)),
           detail = detail || jsonb_build_object(
               'tombstone_missing', v_tombstone_missing,
               'retained_missing', NOT v_tombstone_missing,
               'meeting_summaries', v_meeting_summaries
           )
     WHERE run_id = v_run;

    RETURN jsonb_build_object(
        'run_id', v_run, 'source_id', p_source_id,
        'added', v_added, 'changed', v_changed, 'removed', v_removed,
        'skipped', v_skipped, 'errors', v_errors,
        'tombstone_missing', v_tombstone_missing,
        'meeting_summaries', v_meeting_summaries
    );
END $fn$;

-- Connector-backed sources may still bind a provider for doc type, Brief
-- projection, and deterministic KG edges. Provider alone means query source;
-- connector/endpoint wins when both are present.
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_query_sources(p_trigger text DEFAULT 'auto')
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; v_results jsonb := '[]'::jsonb; v_one jsonb;
BEGIN
    FOR rec IN
        SELECT source_id FROM rvbbit.brain_sources
         WHERE enabled
           AND nullif(config->>'provider','') IS NOT NULL
           AND nullif(config->>'connector','') IS NULL
           AND nullif(config->>'endpoint','') IS NULL
         ORDER BY source_id
    LOOP
        BEGIN
            v_one := rvbbit.brain_sync_query_source(rec.source_id, p_trigger);
        EXCEPTION WHEN OTHERS THEN
            v_one := jsonb_build_object('source_id', rec.source_id, 'error', SQLERRM);
        END;
        v_results := v_results || v_one;
    END LOOP;
    RETURN jsonb_build_object('sources', jsonb_array_length(v_results), 'results', v_results);
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_sync_dispatch(
    p_source_id bigint, p_trigger text DEFAULT 'manual'
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_provider text; v_connector text; v_exists boolean;
BEGIN
    SELECT nullif(config->>'provider',''),
           coalesce(nullif(config->>'connector',''), nullif(config->>'endpoint',''))
      INTO v_provider, v_connector
      FROM rvbbit.brain_sources WHERE source_id = p_source_id;
    SELECT EXISTS (SELECT 1 FROM rvbbit.brain_sources WHERE source_id = p_source_id)
      INTO v_exists;
    IF NOT v_exists THEN
        RAISE EXCEPTION 'brain_sync_dispatch: source % not found', p_source_id;
    END IF;
    IF v_connector IS NOT NULL THEN
        RETURN rvbbit.brain_sync_source(p_source_id, p_trigger);
    ELSIF v_provider IS NOT NULL THEN
        RETURN rvbbit.brain_sync_query_source(p_source_id, p_trigger);
    ELSE
        RETURN rvbbit.brain_sync_source(p_source_id, p_trigger);
    END IF;
END $fn$;

-- A provider describes document shape and deterministic edges; it no longer
-- necessarily means that the source itself is query-backed. Connector-backed
-- providers (Meet is the first) retain full document/triple enrichment.
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_pending(
    p_max_docs int DEFAULT 25, p_max_chunks int DEFAULT 20
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; n_docs int := 0; n_err int := 0;
BEGIN
    FOR rec IN
        SELECT bd.doc_id,
               (nullif(bs.config->>'provider','') IS NOT NULL
                AND nullif(bs.config->>'connector','') IS NULL
                AND nullif(bs.config->>'endpoint','') IS NULL) AS is_query
          FROM rvbbit.brain_documents bd
          JOIN rvbbit.brain_sources bs ON bs.source_id = bd.source_id
         WHERE bd.deleted_at IS NULL AND bd.body IS NOT NULL
           AND (bd.enriched_at IS NULL OR bd.enrich_hash IS DISTINCT FROM bd.content_hash
                OR bd.enriched_at < bd.ingested_at)
         ORDER BY (nullif(bs.config->>'provider','') IS NOT NULL
                   AND nullif(bs.config->>'connector','') IS NULL
                   AND nullif(bs.config->>'endpoint','') IS NULL),
                  bd.ingested_at DESC
         LIMIT greatest(1, p_max_docs)
    LOOP
        BEGIN
            PERFORM set_config('rvbbit.brain_skip_triples', rec.is_query::text, true);
            PERFORM rvbbit.brain_enrich_doc(rec.doc_id, p_max_chunks);
            n_docs := n_docs + 1;
        EXCEPTION WHEN OTHERS THEN
            n_err := n_err + 1;
        END;
    END LOOP;
    PERFORM rvbbit.brain_refresh_node_norm();
    RETURN jsonb_build_object('enriched_docs', n_docs, 'errors', n_err);
END $fn$;

-- Keep one-click source enrichment consistent with the nightly path: a
-- provider is query-shaped only when it has no connector. Meet documents and
-- their derived briefs therefore retain the full relation-triple pass.
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_source(
    p_source_id bigint, p_force boolean DEFAULT false,
    p_max_chunks int DEFAULT 20, p_skip_triples boolean DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; n_docs int := 0; n_err int := 0; v_skip boolean;
BEGIN
    v_skip := coalesce(p_skip_triples,
        EXISTS (SELECT 1 FROM rvbbit.brain_sources
                 WHERE source_id = p_source_id
                   AND nullif(config->>'provider','') IS NOT NULL
                   AND nullif(config->>'connector','') IS NULL
                   AND nullif(config->>'endpoint','') IS NULL));
    PERFORM set_config('rvbbit.brain_skip_triples', v_skip::text, true);

    FOR rec IN SELECT doc_id FROM rvbbit.brain_documents
                WHERE source_id = p_source_id AND deleted_at IS NULL AND body IS NOT NULL
                  AND (p_force OR enriched_at IS NULL
                       OR enrich_hash IS DISTINCT FROM content_hash OR enriched_at < ingested_at)
                ORDER BY ingested_at DESC LOOP
        BEGIN
            PERFORM rvbbit.brain_enrich_doc(rec.doc_id, p_max_chunks);
            n_docs := n_docs + 1;
        EXCEPTION WHEN OTHERS THEN n_err := n_err + 1;
        END;
    END LOOP;

    PERFORM rvbbit.brain_refresh_node_norm();
    RETURN jsonb_build_object('source_id', p_source_id, 'enriched_docs', n_docs, 'errors', n_err,
                              'skip_triples', v_skip, 'forced', p_force);
END $fn$;

-- The connector is a packaged product sidecar, just like gdrive_connector.
DO $do$
BEGIN
    PERFORM rvbbit.register_backend(
        backend_name       => 'gmeet_connector',
        backend_endpoint   => coalesce(
            nullif(current_setting('rvbbit.gmeet_connector_endpoint', true), ''),
            'http://rvbbit-gmeet-connector:8080/sync'),
        backend_transport  => 'rvbbit',
        backend_batch_size => 1,
        backend_max_concur => 1,
        backend_timeout_ms => 900000,
        backend_auth_env   => 'GMEET_CONNECTOR_TOKEN',
        backend_description => 'Google Meet transcript -> governed Brain connector. Uses Workspace domain-wide delegation; ships behind the gmeet compose profile.'
    );
END $do$;

-- Provider metadata is used by facets, Briefs, and deterministic KG edges.
-- Its list SQL is intentionally inert: this provider is populated through the
-- connector contract, not through an MCP/query source.
SELECT rvbbit.brain_define_provider(
    'google-meet',
    'Google Meet',
    $list$
      SELECT NULL::text AS uri,
             NULL::text AS title,
             NULL::text AS content_hash,
             NULL::timestamptz AS occurred_at,
             NULL::text AS body,
             NULL::jsonb AS props
       WHERE false
    $list$,
    NULL,
    'meeting',
    'Generated Google Meet transcripts and optional derived briefs with Workspace identities, strict invitee ACLs, and Calendar context.',
    '[{"predicate":"organized_by","kind":"person","path":"$.organizerEmail"},
      {"predicate":"attended_by","kind":"person","path":"$.attendees[*].email"},
      {"predicate":"has_participant","kind":"person","path":"$.participants[*].email"},
      {"predicate":"held_in","kind":"place","path":"$.meetingCode"}]'::jsonb,
    'meeting',
    '{"starts_at":["$.startTime"],
      "ends_at":["$.endTime"],
      "url":["$.meetingUri","$.transcriptUri","$.calendarUrl"],
      "participants":["$.participants[*].email","$.attendees[*].email","$.organizerEmail"],
      "authors":["$.organizerEmail"]}'::jsonb
);

CREATE OR REPLACE FUNCTION rvbbit.brain_configure_google_meet_source(
    p_label text DEFAULT 'Google Meet',
    p_enabled boolean DEFAULT true,
    p_options jsonb DEFAULT '{}'::jsonb
) RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_id bigint; v_config jsonb;
BEGIN
    IF jsonb_typeof(coalesce(p_options, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'brain_configure_google_meet_source: p_options must be a JSON object';
    END IF;
    v_config := jsonb_build_object(
        'connector', 'gmeet_connector',
        'provider', 'google-meet',
        'doc_type', 'meeting',
        'source_kind', 'google_meet',
        'tombstone_missing', false,
        'lookback_days', 29,
        'calendar_lookup', true,
        'auto_transcribe', false,
        'auto_transcribe_days', 7,
        'drive_acl', true,
        'acl_mode', 'calendar_invitees_strict',
        'summarize_meetings', true,
        'summary_max_docs', 12
    ) || (coalesce(p_options, '{}'::jsonb) - ARRAY[
        'connector','provider','doc_type','source_kind','tombstone_missing'
    ]::text[]);
    v_id := rvbbit.brain_configure_source(
        coalesce(nullif(btrim(p_label),''), 'Google Meet'),
        'google_meet',
        v_config,
        'GMEET_CONNECTOR_TOKEN',
        '/meetings',
        coalesce(p_enabled, true)
    );
    UPDATE rvbbit.brain_sources SET kind = 'google_meet' WHERE source_id = v_id;
    RETURN v_id;
END $fn$;

COMMENT ON FUNCTION rvbbit.brain_configure_google_meet_source(text,boolean,jsonb) IS
    'Create/update an append-safe Google Meet transcript Brain source. Credentials stay in the gmeet connector sidecar; p_options contains only non-secret discovery/policy settings.';
