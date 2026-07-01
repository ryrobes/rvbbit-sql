-- 0047_brain_sync_orchestration — the glue between a connector sidecar and the 0046 sync engine.
--
-- 0046 gave us the manifest contract + reconciler (pure SQL). This adds the orchestration surface:
--   • brain_sync_request(source_id)      → the JSON a connector /sync call needs (endpoint, auth env,
--                                           configured Drive folder/doc locations, cursor, and the known uri→hash map so the
--                                           connector only re-downloads CHANGED files).
--   • brain_sync_write_manifest(...)      → land the connector's returned listing (full current set)
--                                           + pending grants + cursor. One writer: rvbbit.
--   • brain_sync_extract_bodies(source_id)→ fill body for new/changed files via the extract_doc
--                                           operator (universal file→markdown sidecar). Guarded: a
--                                           no-op if extract_doc isn't installed (text files carry
--                                           inline body from the connector and ingest regardless).
--   • extract_doc operator + backend      → registered here with a default compose endpoint so it's
--                                           turnkey; installing the extraction capability upserts the
--                                           real deployed endpoint over it.
--   • C bindings for brain_sync_source / brain_sync_sources (the Rust HTTP orchestrator) — created
--     here so the ownership-drift manual-binding step isn't needed (symbol resolves post-rebuild).

-- ── what a connector /sync call needs (computed server-side) ──────────────────
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
                                     AND d.uri IS NOT NULL AND d.content_hash IS NOT NULL), '{}'::jsonb)
        )
    )
    FROM rvbbit.brain_sources s
    LEFT JOIN rvbbit.backends b ON b.name = coalesce(s.config->>'connector', 'gdrive_connector')
    WHERE s.source_id = p_source_id;
$fn$;

-- ── land the connector's listing (the connector is authoritative for the current set) ──
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_write_manifest(
    p_source_id bigint, p_files jsonb, p_pending jsonb DEFAULT '[]'::jsonb, p_cursor text DEFAULT NULL
) RETURNS int LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE n int := 0;
BEGIN
    -- Replace the whole manifest: the connector returns ALL current files (metadata), staging bytes
    -- only for changed ones. Full replace is what makes the apply step's tombstone logic correct.
    DELETE FROM rvbbit.brain_sync_manifest WHERE source_id = p_source_id;
    INSERT INTO rvbbit.brain_sync_manifest
        (source_id, uri, title, rel_path, folder_id, mime, modified_at, content_hash, permissions, staged_path, body)
    SELECT p_source_id, f->>'uri', f->>'title', coalesce(f->>'rel_path', '/'), f->>'folder_id', f->>'mime',
           nullif(f->>'modified_at','')::timestamptz, f->>'content_hash',
           coalesce((SELECT array_agg(p) FROM jsonb_array_elements_text(f->'permissions') p), '{}'),
           f->>'staged_path', f->>'body'
    FROM jsonb_array_elements(coalesce(p_files, '[]'::jsonb)) f
    WHERE nullif(f->>'uri','') IS NOT NULL;
    GET DIAGNOSTICS n = ROW_COUNT;

    -- Non-individual shares (group/domain/anyone) → admin-approval queue (strict mode).
    DELETE FROM rvbbit.brain_pending_grants WHERE source_id = p_source_id AND NOT approved;
    INSERT INTO rvbbit.brain_pending_grants (source_id, folder_id, grant_kind, grant_value)
    SELECT p_source_id, coalesce(g->>'folder_id',''), g->>'grant_kind', coalesce(g->>'grant_value','')
    FROM jsonb_array_elements(coalesce(p_pending, '[]'::jsonb)) g
    WHERE nullif(g->>'grant_kind','') IS NOT NULL
    ON CONFLICT DO NOTHING;

    UPDATE rvbbit.brain_sources SET sync_cursor = coalesce(p_cursor, sync_cursor) WHERE source_id = p_source_id;
    RETURN n;
END $fn$;

-- ── extract markdown for new/changed binary files (guarded; skips unchanged) ──
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_extract_bodies(p_source_id bigint)
RETURNS int LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE n int := 0;
BEGIN
    -- Check existence by NAME (not a fixed signature): create_operator defines
    -- extract_doc(text,text,jsonb) — a (text,text) regprocedure lookup would miss it
    -- and wrongly conclude the capability is absent, skipping ALL binary extraction.
    IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                   WHERE n.nspname = 'rvbbit' AND p.proname = 'extract_doc') THEN
        RETURN 0;   -- extraction capability not installed; text files still ingest via inline body
    END IF;
    WITH upd AS (
        UPDATE rvbbit.brain_sync_manifest m
           SET body = nullif(rvbbit.extract_doc(m.staged_path, m.mime), '')  -- failed extract → NULL → skipped, not an empty doc
         WHERE m.source_id = p_source_id AND m.body IS NULL AND nullif(m.staged_path,'') IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM rvbbit.brain_documents d
                           WHERE d.source_id = p_source_id AND d.uri = m.uri AND d.deleted_at IS NULL
                             AND d.content_hash IS NOT DISTINCT FROM m.content_hash)
        RETURNING 1)
    SELECT count(*) INTO n FROM upd;
    RETURN n;
END $fn$;

-- ── extract_doc: universal file→markdown operator (specialist sidecar) ────────
-- Default endpoint is the compose service name; override with the GUC
-- rvbbit.extract_endpoint, or install the extraction capability (upserts the deployed URL).
DO $do$
BEGIN
    PERFORM rvbbit.register_backend(
        backend_name      => 'extract_doc',
        backend_endpoint  => coalesce(nullif(current_setting('rvbbit.extract_endpoint', true), ''),
                                      'http://rvbbit-doc-extract:8080/predict'),
        backend_transport => 'rvbbit',
        backend_batch_size=> 4,
        backend_max_concur=> 2,
        backend_timeout_ms=> 180000,
        backend_description => 'Universal document→markdown extraction (pdf/docx/xlsx/pptx/html/images/…)');

    PERFORM rvbbit.create_operator(
        op_name        => 'extract_doc',
        op_arg_names   => ARRAY['staged_path','mime'],
        op_return_type => 'text',
        op_parser      => 'strip',
        op_description => 'Extract a staged file (by shared-volume path) to markdown via the extraction sidecar.',
        op_steps       => '[{"name":"x","kind":"specialist","specialist":"extract_doc",
                             "inputs":{"staged_path":"{{ inputs.staged_path }}","mime":"{{ inputs.mime }}"}}]'::jsonb);
END $do$;

-- ── C bindings for the Rust HTTP orchestrator (drift-proof, like cold_put) ────
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_source(p_source_id bigint, p_trigger text DEFAULT 'manual')
RETURNS jsonb LANGUAGE c AS '$libdir/pg_rvbbit', 'brain_sync_source_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.brain_sync_sources(p_trigger text DEFAULT 'auto')
RETURNS jsonb LANGUAGE c AS '$libdir/pg_rvbbit', 'brain_sync_sources_wrapper';
