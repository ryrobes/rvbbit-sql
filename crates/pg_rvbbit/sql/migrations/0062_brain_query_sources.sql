-- 0062_brain_query_sources — index MCP-queryable artifacts (Linear/JIRA/GitHub/…) as first-class docs.
--
-- A brain source is "something that yields a set of {uri,title,body,occurred_at} items." A Drive folder
-- is one way; an MCP tool that lists tickets is another. This migration adds a second source STRATEGY —
-- kind='query' — whose locator is a SQL expression instead of a folder id. Everything downstream
-- (manifest diff, embed, KG/NER enrichment, brain_search/brain_related/ask_brain) is the existing
-- pipeline, unchanged: a Linear issue becomes a real brain_document and flows through identical machinery
-- (semantic + graph), so it's searchable and cross-links with the file corpus by shared entities.
--
-- A PROVIDER is the reusable "document type" (pseudo-DDL): a named list_sql (+ optional per-artifact
-- item_sql for list→get APIs). A SOURCE binds a provider. rvbbit.mcp_rows(server,tool,args) is the bridge
-- to any MCP server, so providers stay generic. Example Linear provider (single-phase):
--
--   SELECT rvbbit.brain_define_provider('linear-issues','Linear Issues', $list$
--     SELECT 'linear:'||(r->>'id')                              AS uri,
--            (r->>'identifier')||' · '||(r->>'title')           AS title,
--            (r->>'updatedAt')                                  AS content_hash,
--            (r->>'updatedAt')::timestamptz                     AS occurred_at,
--            concat_ws(E'\n\n', r->>'title', r->>'description',
--                      'Status: '||(r->>'state'), 'Assignee: '||(r->>'assignee')) AS body
--       FROM rvbbit.mcp_rows('Linear','list_issues','{"team":"ENG"}'::jsonb) r
--   $list$);
--   SELECT rvbbit.brain_add_query_source('Linear · ENG', 'linear-issues');
--   SELECT rvbbit.brain_sync_query_source((SELECT source_id FROM rvbbit.brain_sources WHERE label='Linear · ENG'));
--
-- Two-phase (list returns ids → get per id): provider also supplies item_sql with $1=uri returning
-- exactly (body text, title text, occurred_at timestamptz); only NEW/CHANGED uris are fetched.
--
-- ACL: MCP org-data has no clean per-user permission mapping (unlike Drive's folder shares), so these
-- docs are GLOBAL — visible to any authenticated caller. Implemented as an is_public synthetic role, so
-- the single security predicate (brain_visible_docs) still governs everything. (Per-row ACL: future.)

-- ── provider registry: a document type whose "scrape" is SQL ──────────────────
CREATE TABLE IF NOT EXISTS rvbbit.brain_doc_providers (
    provider    text PRIMARY KEY,          -- 'linear-issues'
    label       text NOT NULL,             -- 'Linear Issues'
    list_sql    text NOT NULL,             -- SELECT → (uri,title,content_hash,occurred_at[,body]); may ref $1 = source config
    item_sql    text,                      -- NULL = single-phase; else $1=uri → (body,title,occurred_at)
    icon        text,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ── global visibility: a role everyone (authenticated) sees ───────────────────
ALTER TABLE rvbbit.brain_roles ADD COLUMN IF NOT EXISTS is_public boolean NOT NULL DEFAULT false;

-- security predicate: visible if a public role OR a role the caller is in, minus excludes (still default-deny)
CREATE OR REPLACE FUNCTION rvbbit.brain_visible_docs(p_email text)
RETURNS TABLE(doc_id bigint) LANGUAGE sql STABLE AS $fn$
    SELECT d.doc_id FROM rvbbit.brain_documents d
    WHERE p_email IS NOT NULL
      AND d.deleted_at IS NULL
      AND ( EXISTS (SELECT 1 FROM rvbbit.brain_doc_roles dr
                    JOIN rvbbit.brain_roles r ON r.role = dr.role
                    WHERE dr.doc_id = d.doc_id AND r.is_public)
         OR EXISTS (SELECT 1 FROM rvbbit.brain_doc_roles dr
                    JOIN rvbbit.brain_role_members rm ON rm.role = dr.role
                    WHERE dr.doc_id = d.doc_id AND lower(rm.principal) = lower(p_email)) )
      AND NOT EXISTS (SELECT 1 FROM rvbbit.brain_doc_exclude ex
                      WHERE ex.doc_id = d.doc_id AND lower(ex.principal) = lower(p_email));
$fn$;

-- ── define a provider (pseudo-DDL) ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rvbbit.brain_define_provider(
    p_provider text, p_label text, p_list_sql text,
    p_item_sql text DEFAULT NULL, p_icon text DEFAULT NULL, p_description text DEFAULT NULL
) RETURNS text LANGUAGE sql VOLATILE AS $fn$
    INSERT INTO rvbbit.brain_doc_providers (provider, label, list_sql, item_sql, icon, description)
    VALUES (p_provider, p_label, p_list_sql, nullif(btrim(p_item_sql),''), p_icon, p_description)
    ON CONFLICT (provider) DO UPDATE SET
        label = excluded.label, list_sql = excluded.list_sql, item_sql = excluded.item_sql,
        icon = excluded.icon, description = excluded.description, updated_at = now()
    RETURNING provider;
$fn$;

-- ── create a query source bound to a provider ─────────────────────────────────
CREATE OR REPLACE FUNCTION rvbbit.brain_add_query_source(
    p_label text, p_provider text, p_config jsonb DEFAULT '{}'::jsonb, p_enabled boolean DEFAULT true
) RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_id bigint;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rvbbit.brain_doc_providers WHERE provider = p_provider) THEN
        RAISE EXCEPTION 'brain_add_query_source: provider "%" is not defined', p_provider;
    END IF;
    v_id := rvbbit.brain_configure_source(
        p_label, 'query',
        coalesce(p_config, '{}'::jsonb) || jsonb_build_object('provider', p_provider),
        NULL, NULL, p_enabled);
    RETURN v_id;
END $fn$;

-- ── sync ONE query source: run the provider query → manifest → reconcile ──────
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_query_source(p_source_id bigint, p_trigger text DEFAULT 'manual')
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_provider text; v_list text; v_item text; v_two boolean;
    v_config jsonb; v_sel text; v_role text;
    rec record;
    v_body text; v_title2 text; v_occ2 timestamptz;
    v_fetched int := 0; v_apply jsonb;
BEGIN
    -- Discriminate on the provider binding in config, NOT kind: brain_ingest → brain_define_source
    -- resets kind to its 'manual' default on every apply, but never touches config.
    SELECT nullif(s.config->>'provider',''), coalesce(s.config,'{}'::jsonb)
      INTO v_provider, v_config
      FROM rvbbit.brain_sources s WHERE s.source_id = p_source_id;
    IF v_provider IS NULL THEN
        RAISE EXCEPTION 'brain_sync_query_source: source % has no provider (not a query source)', p_source_id;
    END IF;
    SELECT list_sql, item_sql INTO v_list, v_item FROM rvbbit.brain_doc_providers WHERE provider = v_provider;
    IF v_list IS NULL THEN
        RAISE EXCEPTION 'brain_sync_query_source: provider "%" not defined (source %)', v_provider, p_source_id;
    END IF;
    v_two := nullif(btrim(coalesce(v_item,'')),'') IS NOT NULL;

    -- (1) Replace the manifest from the provider's list query (authoritative current set).
    -- $1 (= source config) is always bound; the wrapper references it so inner SQL may use it freely.
    DELETE FROM rvbbit.brain_sync_manifest WHERE source_id = p_source_id;
    v_sel := format(
        'SELECT q.uri::text AS uri, q.title::text AS title, q.content_hash::text AS content_hash, '
        'q.occurred_at::timestamptz AS occurred_at, %s AS body '
        'FROM (%s) q WHERE ($1 IS NOT NULL OR $1 IS NULL)',
        CASE WHEN v_two THEN 'NULL::text' ELSE 'q.body::text' END, v_list);

    FOR rec IN EXECUTE v_sel USING v_config LOOP
        CONTINUE WHEN nullif(btrim(rec.uri),'') IS NULL;
        INSERT INTO rvbbit.brain_sync_manifest
            (source_id, uri, title, rel_path, folder_id, mime, modified_at, content_hash, permissions, staged_path, body)
        VALUES (p_source_id, rec.uri, rec.title, '/', NULL, 'text/markdown', rec.occurred_at,
                CASE WHEN v_two THEN rec.content_hash
                     ELSE coalesce(rec.content_hash, md5(coalesce(rec.body,''))) END,
                '{}', NULL, rec.body)
        ON CONFLICT (source_id, uri) DO UPDATE SET
            title = excluded.title, modified_at = excluded.modified_at,
            content_hash = excluded.content_hash, body = excluded.body;
    END LOOP;

    -- (2) Two-phase: fetch body for NEW/CHANGED uris only (list→get pattern; one MCP get per changed item).
    IF v_two THEN
        FOR rec IN
            SELECT m.uri FROM rvbbit.brain_sync_manifest m
             WHERE m.source_id = p_source_id
               AND ( m.content_hash IS NULL
                  OR NOT EXISTS (SELECT 1 FROM rvbbit.brain_documents d
                                 WHERE d.source_id = p_source_id AND d.uri = m.uri AND d.deleted_at IS NULL
                                   AND d.content_hash IS NOT DISTINCT FROM m.content_hash) )
        LOOP
            BEGIN
                EXECUTE v_item INTO v_body, v_title2, v_occ2 USING rec.uri;
            EXCEPTION WHEN OTHERS THEN v_body := NULL; v_title2 := NULL; v_occ2 := NULL; END;
            UPDATE rvbbit.brain_sync_manifest m
               SET body = v_body,
                   title = coalesce(nullif(btrim(v_title2),''), m.title),
                   modified_at = coalesce(v_occ2, m.modified_at)
             WHERE m.source_id = p_source_id AND m.uri = rec.uri;
            IF v_body IS NOT NULL THEN v_fetched := v_fetched + 1; END IF;
        END LOOP;
    END IF;

    -- (3) Global visibility: this source's synthetic root role is public (any authenticated caller).
    v_role := rvbbit.brain_folder_role(p_source_id, NULL);
    INSERT INTO rvbbit.brain_roles (role, origin, is_public, label)
    VALUES (v_role, 'sync', true, 'auto: query source (global)')
    ON CONFLICT (role) DO UPDATE SET is_public = true, origin = 'sync';

    -- (4) Reconcile with the existing engine (ingest new/changed, tombstone gone). brain_sync_acl runs
    -- inside it and only updates origin on conflict, so is_public survives; re-assert after for safety.
    v_apply := rvbbit.brain_sync_apply_manifest(p_source_id, coalesce(p_trigger, 'manual'));
    UPDATE rvbbit.brain_roles SET is_public = true WHERE role = v_role;
    -- apply's ingest reset kind to 'manual'; restore it so admin/UI listing reads true (config is the
    -- source of truth either way).
    UPDATE rvbbit.brain_sources SET kind = 'query', last_synced_at = now() WHERE source_id = p_source_id;

    RETURN coalesce(v_apply, '{}'::jsonb)
           || jsonb_build_object('provider', v_provider, 'two_phase', v_two, 'fetched', v_fetched);
END $fn$;

-- ── sync ALL enabled query sources ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_query_sources(p_trigger text DEFAULT 'auto')
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; v_results jsonb := '[]'::jsonb; v_one jsonb;
BEGIN
    FOR rec IN SELECT source_id FROM rvbbit.brain_sources
                WHERE enabled AND nullif(config->>'provider','') IS NOT NULL ORDER BY source_id LOOP
        BEGIN
            v_one := rvbbit.brain_sync_query_source(rec.source_id, p_trigger);
        EXCEPTION WHEN OTHERS THEN
            v_one := jsonb_build_object('source_id', rec.source_id, 'error', SQLERRM);
        END;
        v_results := v_results || v_one;
    END LOOP;
    RETURN jsonb_build_object('sources', jsonb_array_length(v_results), 'results', v_results);
END $fn$;

-- ── unified sync entry point: route by source kind (UI + nightly call this) ────
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_dispatch(p_source_id bigint, p_trigger text DEFAULT 'manual')
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_provider text; v_exists boolean;
BEGIN
    SELECT nullif(config->>'provider','') INTO v_provider FROM rvbbit.brain_sources WHERE source_id = p_source_id;
    SELECT EXISTS (SELECT 1 FROM rvbbit.brain_sources WHERE source_id = p_source_id) INTO v_exists;
    IF NOT v_exists THEN RAISE EXCEPTION 'brain_sync_dispatch: source % not found', p_source_id; END IF;
    IF v_provider IS NOT NULL THEN
        RETURN rvbbit.brain_sync_query_source(p_source_id, p_trigger);
    ELSE
        RETURN rvbbit.brain_sync_source(p_source_id, p_trigger);   -- connector path (C / HTTP)
    END IF;
END $fn$;

-- ── nightly: connector sources + query sources, then enrich the backlog ───────
CREATE OR REPLACE FUNCTION rvbbit.brain_nightly(p_max_docs int DEFAULT 25, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_sync jsonb; v_qsync jsonb; v_enrich jsonb;
BEGIN
    v_sync   := rvbbit.brain_sync_sources('auto');        -- connector (Drive/S3/…) sources
    v_qsync  := rvbbit.brain_sync_query_sources('auto');  -- MCP/query sources
    v_enrich := rvbbit.brain_enrich_pending(p_max_docs, p_max_chunks);
    RETURN jsonb_build_object('sync', v_sync, 'query_sync', v_qsync, 'enrich', v_enrich);
END $fn$;
