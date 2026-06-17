-- 0046_brain_remote_sources — remote file-source sync substrate for the document brain.
--
-- Phase 1 of "admin points the brain at Google Drive (or any remote/local store)". This migration
-- is the SPINE: the schema + the pure-SQL diff/ingest/ACL-sync/tombstone engine that runs against a
-- MANIFEST a connector sidecar writes. No HTTP here — a connector (gdrive/s3/nfs/local) lists files +
-- folder permissions and writes rvbbit.brain_sync_manifest rows; the Rust orchestrator fills each
-- row's `body` (text files read directly; pdf/xlsx/docx/etc. via the extraction sidecar); then
-- brain_sync_apply_manifest() reconciles the corpus. Everything is testable now by hand-writing a
-- manifest row with a body and calling apply.
--
-- ACL model (the elegant bit): each synced folder maps to a SYNTHETIC role `sync/<source>/<folderId>`
-- whose members are the folder's individually-shared emails. Docs from the folder carry that role.
-- brain_visible_docs joins role membership at QUERY time, so re-syncing the folder's membership
-- auto-propagates to every doc under it — folder-permission inheritance with zero per-doc churn,
-- using the existing ACL tables unchanged. Group/domain/"anyone" shares are NOT auto-granted (strict
-- default-deny); they land in brain_pending_grants for admin approval.

-- ── source config: a source can now describe a remote/local store ─────────────
ALTER TABLE rvbbit.brain_sources ADD COLUMN IF NOT EXISTS config        jsonb NOT NULL DEFAULT '{}'::jsonb; -- folder ids, connector endpoint, staging path
ALTER TABLE rvbbit.brain_sources ADD COLUMN IF NOT EXISTS creds_ref     text;                               -- env-var NAME holding the credential (never the secret)
ALTER TABLE rvbbit.brain_sources ADD COLUMN IF NOT EXISTS enabled       boolean NOT NULL DEFAULT true;
ALTER TABLE rvbbit.brain_sources ADD COLUMN IF NOT EXISTS sync_cursor   text;                               -- connector incremental cursor (e.g. Drive changes pageToken)
ALTER TABLE rvbbit.brain_sources ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;

-- ── corpus: change detection + soft delete ────────────────────────────────────
ALTER TABLE rvbbit.brain_documents ADD COLUMN IF NOT EXISTS content_hash text;        -- skip re-extract/re-embed when unchanged
ALTER TABLE rvbbit.brain_documents ADD COLUMN IF NOT EXISTS deleted_at   timestamptz; -- tombstone (file removed / unshared)
CREATE INDEX IF NOT EXISTS brain_documents_live_idx ON rvbbit.brain_documents (source_id) WHERE deleted_at IS NULL;

-- ── roles: distinguish auto-managed (folder) roles from hand-made ones ─────────
ALTER TABLE rvbbit.brain_roles ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'manual'; -- 'manual' | 'sync'

-- ── the manifest: a connector's view of a source's current files ──────────────
CREATE TABLE IF NOT EXISTS rvbbit.brain_sync_manifest (
    source_id    bigint NOT NULL REFERENCES rvbbit.brain_sources(source_id) ON DELETE CASCADE,
    uri          text   NOT NULL,            -- connector's stable file id (e.g. Drive fileId)
    title        text,
    rel_path     text   NOT NULL DEFAULT '/',-- folder path within the source (explorer hierarchy)
    folder_id    text,                       -- the folder this file lives in (drives the synthetic role)
    mime         text,
    modified_at  timestamptz,
    content_hash text,                       -- connector's hash (Drive md5Checksum, etc.)
    permissions  text[] NOT NULL DEFAULT '{}', -- folder-effective INDIVIDUAL emails (strict ACL)
    staged_path  text,                       -- where the connector put the raw bytes (shared volume)
    body         text,                       -- extracted text/markdown; NULL = awaiting extraction
    seen_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, uri)
);
CREATE INDEX IF NOT EXISTS brain_sync_manifest_folder_idx ON rvbbit.brain_sync_manifest (source_id, folder_id);

-- ── sync run history (mirrors accel_tick_runs / route_optimize_runs) ──────────
CREATE TABLE IF NOT EXISTS rvbbit.brain_sync_runs (
    run_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   bigint REFERENCES rvbbit.brain_sources(source_id) ON DELETE SET NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    trigger     text NOT NULL DEFAULT 'manual',  -- auto | manual
    added       int NOT NULL DEFAULT 0,
    changed     int NOT NULL DEFAULT 0,
    removed     int NOT NULL DEFAULT 0,
    skipped     int NOT NULL DEFAULT 0,           -- unchanged, or awaiting extraction (body NULL)
    errors      int NOT NULL DEFAULT 0,
    elapsed_sec numeric,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS brain_sync_runs_source_idx ON rvbbit.brain_sync_runs (source_id, started_at DESC);

-- ── pending grants: non-individual Drive shares awaiting admin approval ───────
CREATE TABLE IF NOT EXISTS rvbbit.brain_pending_grants (
    source_id   bigint NOT NULL REFERENCES rvbbit.brain_sources(source_id) ON DELETE CASCADE,
    folder_id   text   NOT NULL DEFAULT '',
    grant_kind  text   NOT NULL,             -- group | domain | anyone
    grant_value text   NOT NULL DEFAULT '',  -- group email / domain / 'anyone'
    first_seen  timestamptz NOT NULL DEFAULT now(),
    approved    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (source_id, folder_id, grant_kind, grant_value)
);

-- ── synthetic folder role name (deterministic, namespaced, collision-free) ────
CREATE OR REPLACE FUNCTION rvbbit.brain_folder_role(p_source_id bigint, p_folder_id text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT format('sync/%s/%s', p_source_id, coalesce(nullif(btrim(p_folder_id), ''), 'root'));
$fn$;

-- ── security predicate: now also excludes tombstoned docs ─────────────────────
CREATE OR REPLACE FUNCTION rvbbit.brain_visible_docs(p_email text)
RETURNS TABLE(doc_id bigint) LANGUAGE sql STABLE AS $fn$
    SELECT d.doc_id FROM rvbbit.brain_documents d
    WHERE p_email IS NOT NULL
      AND d.deleted_at IS NULL
      AND EXISTS (SELECT 1 FROM rvbbit.brain_doc_roles dr
                  JOIN rvbbit.brain_role_members rm ON rm.role = dr.role
                  WHERE dr.doc_id = d.doc_id AND lower(rm.principal) = lower(p_email))
      AND NOT EXISTS (SELECT 1 FROM rvbbit.brain_doc_exclude ex
                      WHERE ex.doc_id = d.doc_id AND lower(ex.principal) = lower(p_email));
$fn$;

-- admin/unfiltered listing: skip tombstones (they're not live docs to triage)
CREATE OR REPLACE FUNCTION rvbbit.brain_all_docs()
RETURNS TABLE(folder_path text, doc_id bigint, title text, source text, mime text, author text,
              occurred_at timestamptz, ingested_at timestamptz, chunks bigint, roles text[], unassigned boolean)
LANGUAGE sql STABLE AS $fn$
    SELECT d.folder_path, d.doc_id, d.title, s.label, d.mime, d.author, d.occurred_at, d.ingested_at,
           (SELECT count(*) FROM rvbbit.brain_chunks c WHERE c.doc_id = d.doc_id) AS chunks,
           coalesce((SELECT array_agg(dr.role ORDER BY dr.role) FROM rvbbit.brain_doc_roles dr
                     WHERE dr.doc_id = d.doc_id), '{}') AS roles,
           NOT EXISTS (SELECT 1 FROM rvbbit.brain_doc_roles dr WHERE dr.doc_id = d.doc_id) AS unassigned
    FROM rvbbit.brain_documents d
    JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
    WHERE d.deleted_at IS NULL
    ORDER BY d.folder_path, d.title;
$fn$;

-- ── configure a remote source (convenience over brain_define_source) ──────────
CREATE OR REPLACE FUNCTION rvbbit.brain_configure_source(
    p_label text, p_kind text, p_config jsonb DEFAULT '{}'::jsonb,
    p_creds_ref text DEFAULT NULL, p_folder_prefix text DEFAULT NULL, p_enabled boolean DEFAULT true
) RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_id bigint;
BEGIN
    v_id := rvbbit.brain_define_source(p_label, p_kind, '{}', p_folder_prefix);
    UPDATE rvbbit.brain_sources
       SET config = coalesce(p_config, '{}'::jsonb), creds_ref = p_creds_ref, enabled = coalesce(p_enabled, true)
     WHERE source_id = v_id;
    RETURN v_id;
END $fn$;

-- ── ACL sync: synthetic folder roles ← manifest folder permissions ────────────
-- Idempotent; only ever rewrites roles we own (origin='sync'). Members = the union of
-- individual emails the connector reported for files in that folder.
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_acl(p_source_id bigint)
RETURNS void LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; v_role text;
BEGIN
    FOR rec IN
        SELECT m.folder_id,
               array_agg(DISTINCT p) FILTER (WHERE p IS NOT NULL AND btrim(p) <> '') AS principals
        FROM rvbbit.brain_sync_manifest m
        LEFT JOIN LATERAL unnest(m.permissions) AS p ON true
        WHERE m.source_id = p_source_id
        GROUP BY m.folder_id
    LOOP
        v_role := rvbbit.brain_folder_role(p_source_id, rec.folder_id);
        INSERT INTO rvbbit.brain_roles (role, origin, label)
        VALUES (v_role, 'sync', 'auto: synced folder')
        ON CONFLICT (role) DO UPDATE SET origin = 'sync';
        -- replace membership (only for the role we own)
        DELETE FROM rvbbit.brain_role_members WHERE role = v_role;
        IF rec.principals IS NOT NULL THEN
            INSERT INTO rvbbit.brain_role_members (role, principal)
                SELECT v_role, unnest(rec.principals) ON CONFLICT DO NOTHING;
        END IF;
    END LOOP;
END $fn$;

-- ── reconcile the corpus to the manifest: ingest new/changed, tombstone gone ──
-- Rows with body IS NULL are awaiting extraction (counted as skipped, not ingested).
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_apply_manifest(p_source_id bigint, p_trigger text DEFAULT 'manual')
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_label   text;
    v_run     bigint;
    v_added   int := 0; v_changed int := 0; v_removed int := 0; v_skipped int := 0; v_errors int := 0;
    rec       record;
    v_doc     bigint;
    v_role    text;
    v_existing rvbbit.brain_documents%ROWTYPE;
    v_t0      timestamptz := clock_timestamp();
BEGIN
    SELECT label INTO v_label FROM rvbbit.brain_sources WHERE source_id = p_source_id;
    IF v_label IS NULL THEN RAISE EXCEPTION 'brain_sync_apply_manifest: source % not found', p_source_id; END IF;

    INSERT INTO rvbbit.brain_sync_runs (source_id, trigger) VALUES (p_source_id, coalesce(p_trigger, 'manual'))
    RETURNING run_id INTO v_run;

    -- ACL first so the synthetic role exists before ingest stamps it on the doc.
    PERFORM rvbbit.brain_sync_acl(p_source_id);

    FOR rec IN SELECT * FROM rvbbit.brain_sync_manifest WHERE source_id = p_source_id LOOP
        v_role := rvbbit.brain_folder_role(p_source_id, rec.folder_id);
        SELECT * INTO v_existing FROM rvbbit.brain_documents
         WHERE source_id = p_source_id AND uri = rec.uri;

        -- Unchanged (same hash, live): leave content, just guarantee the folder role is attached.
        IF v_existing.doc_id IS NOT NULL
           AND v_existing.deleted_at IS NULL
           AND v_existing.content_hash IS NOT DISTINCT FROM rec.content_hash
           AND rec.content_hash IS NOT NULL THEN
            INSERT INTO rvbbit.brain_doc_roles (doc_id, role) VALUES (v_existing.doc_id, v_role)
                ON CONFLICT DO NOTHING;
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;

        -- Awaiting extraction: connector staged it but no text yet. Skip; the Rust pass fills body.
        IF rec.body IS NULL THEN
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;

        BEGIN
            v_doc := rvbbit.brain_ingest(
                v_label, coalesce(rec.title, rec.uri), rec.body,
                ARRAY[v_role], coalesce(rec.rel_path, '/'), rec.uri, NULL, rec.modified_at,
                jsonb_build_object('sync_uri', rec.uri, 'folder_id', rec.folder_id,
                                   'mime', rec.mime, 'staged_path', rec.staged_path,
                                   'content_hash', rec.content_hash)
            );
            UPDATE rvbbit.brain_documents
               SET content_hash = rec.content_hash,
                   mime = coalesce(rec.mime, mime),
                   deleted_at = NULL
             WHERE doc_id = v_doc;
            IF v_existing.doc_id IS NULL THEN v_added := v_added + 1; ELSE v_changed := v_changed + 1; END IF;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors + 1;
        END;
    END LOOP;

    -- Tombstone: live docs in this source whose uri is no longer in the manifest.
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

    UPDATE rvbbit.brain_sources SET last_synced_at = now() WHERE source_id = p_source_id;
    UPDATE rvbbit.brain_sync_runs
       SET finished_at = clock_timestamp(),
           added = v_added, changed = v_changed, removed = v_removed, skipped = v_skipped, errors = v_errors,
           elapsed_sec = EXTRACT(EPOCH FROM (clock_timestamp() - v_t0))
     WHERE run_id = v_run;

    RETURN jsonb_build_object('run_id', v_run, 'source_id', p_source_id, 'added', v_added,
                              'changed', v_changed, 'removed', v_removed, 'skipped', v_skipped, 'errors', v_errors);
END $fn$;

-- ── approve a pending (group/domain/anyone) grant: add emails to the folder role ──
CREATE OR REPLACE FUNCTION rvbbit.brain_approve_pending_grant(
    p_source_id bigint, p_folder_id text, p_grant_kind text, p_grant_value text, p_emails text[]
) RETURNS void LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_role text := rvbbit.brain_folder_role(p_source_id, p_folder_id);
BEGIN
    INSERT INTO rvbbit.brain_roles (role, origin) VALUES (v_role, 'sync') ON CONFLICT (role) DO UPDATE SET origin = 'sync';
    INSERT INTO rvbbit.brain_role_members (role, principal)
        SELECT v_role, unnest(coalesce(p_emails, '{}')) ON CONFLICT DO NOTHING;
    UPDATE rvbbit.brain_pending_grants SET approved = true
     WHERE source_id = p_source_id AND folder_id = p_folder_id
       AND grant_kind = p_grant_kind AND grant_value = p_grant_value;
END $fn$;
