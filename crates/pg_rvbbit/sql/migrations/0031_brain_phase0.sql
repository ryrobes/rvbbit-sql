-- 0031_brain_phase0 — Document Intelligence ("the brain"): role-gated, semantically-searchable docs.
--
-- Docs become ROWS; intelligence (embeddings, later: classify/entities) becomes COLUMNS; access
-- control is plain rows you can reshape. The security model is EXPLICIT, not inferred: an ingester
-- assigns role(s) to a source/doc, principals (emails) hold roles, and retrieval filters to the
-- caller's visible docs BEFORE the vector search — so a restricted doc never enters the KNN set and
-- can't be paraphrased into an answer. Default-deny: a doc with no role grant is visible to no one.
--
-- Phase 0 = the corpus + ACL + ACL-filtered cosine retrieval (the "Joe can't see the meeting about
-- firing Joe" case). Enrichment beyond embeddings (classify/sensitivity/entities->KG) and semantic
-- auto-foldering layer on later as additive operators. All in the rvbbit schema, idempotent.

-- ── corpus ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rvbbit.brain_sources (
    source_id     bigserial PRIMARY KEY,
    label         text UNIQUE NOT NULL,
    kind          text NOT NULL DEFAULT 'manual',   -- manual | mcp | file_mirror | ...
    default_roles text[] NOT NULL DEFAULT '{}',     -- roles inherited by docs from this source
    folder_prefix text,                             -- default folder root for the explorer
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rvbbit.brain_documents (
    doc_id       bigserial PRIMARY KEY,
    source_id    bigint NOT NULL REFERENCES rvbbit.brain_sources(source_id) ON DELETE CASCADE,
    uri          text,                              -- natural id within the source (for re-ingest)
    title        text NOT NULL,
    author       text,
    folder_path  text NOT NULL DEFAULT '/',         -- the file-explorer path (explicit or derived)
    mime         text NOT NULL DEFAULT 'text/markdown',
    body         text,
    occurred_at  timestamptz,                       -- when the thing happened (meeting date, etc.)
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    raw_meta     jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_id, uri)
);
CREATE INDEX IF NOT EXISTS brain_documents_folder_idx ON rvbbit.brain_documents (folder_path);

CREATE TABLE IF NOT EXISTS rvbbit.brain_chunks (
    chunk_id   bigserial PRIMARY KEY,
    doc_id     bigint NOT NULL REFERENCES rvbbit.brain_documents(doc_id) ON DELETE CASCADE,
    idx        int NOT NULL,
    text       text NOT NULL,
    embedding  real[],                              -- rvbbit.embed(text); ACL-filtered cosine ranks these
    token_est  int,
    UNIQUE (doc_id, idx)
);
CREATE INDEX IF NOT EXISTS brain_chunks_doc_idx ON rvbbit.brain_chunks (doc_id);

-- ── access control (just rows: reshape freely) ────────────────────────────────
CREATE TABLE IF NOT EXISTS rvbbit.brain_roles (
    role        text PRIMARY KEY,
    label       text,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rvbbit.brain_role_members (   -- principal (email) -> role
    role       text NOT NULL,
    principal  text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (role, principal)
);
CREATE INDEX IF NOT EXISTS brain_role_members_principal_idx ON rvbbit.brain_role_members (lower(principal));

CREATE TABLE IF NOT EXISTS rvbbit.brain_doc_roles (     -- doc -> allowed role
    doc_id bigint NOT NULL REFERENCES rvbbit.brain_documents(doc_id) ON DELETE CASCADE,
    role   text NOT NULL,
    PRIMARY KEY (doc_id, role)
);

CREATE TABLE IF NOT EXISTS rvbbit.brain_doc_exclude (   -- the subject-exclusion belt: can't see a doc about you
    doc_id    bigint NOT NULL REFERENCES rvbbit.brain_documents(doc_id) ON DELETE CASCADE,
    principal text NOT NULL,
    reason    text,
    PRIMARY KEY (doc_id, principal)
);

-- ── chunker (paragraph-packed to ~max chars) ──────────────────────────────────
CREATE OR REPLACE FUNCTION rvbbit._brain_chunks(p_body text, p_max int DEFAULT 1200)
RETURNS TABLE(idx int, chunk text) LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE para text; buf text := ''; i int := 0;
BEGIN
    IF p_body IS NULL OR btrim(p_body) = '' THEN RETURN; END IF;
    FOREACH para IN ARRAY regexp_split_to_array(p_body, E'\\n\\s*\\n') LOOP
        para := btrim(para);
        IF para = '' THEN CONTINUE; END IF;
        IF buf <> '' AND length(buf) + length(para) + 2 > p_max THEN
            idx := i; chunk := buf; RETURN NEXT; i := i + 1; buf := '';
        END IF;
        buf := CASE WHEN buf = '' THEN para ELSE buf || E'\n\n' || para END;
        WHILE length(buf) > p_max LOOP            -- hard-split an over-long paragraph
            idx := i; chunk := left(buf, p_max); RETURN NEXT; i := i + 1;
            buf := substr(buf, p_max + 1);
        END LOOP;
    END LOOP;
    IF btrim(buf) <> '' THEN idx := i; chunk := buf; RETURN NEXT; END IF;
END $fn$;

-- ── ingest: upsert doc, (re)chunk, embed, assign roles ────────────────────────
CREATE OR REPLACE FUNCTION rvbbit.brain_define_source(
    p_label text, p_kind text DEFAULT 'manual',
    p_default_roles text[] DEFAULT '{}', p_folder_prefix text DEFAULT NULL
) RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_id bigint;
BEGIN
    INSERT INTO rvbbit.brain_sources (label, kind, default_roles, folder_prefix)
    VALUES (p_label, p_kind, coalesce(p_default_roles, '{}'), p_folder_prefix)
    ON CONFLICT (label) DO UPDATE SET kind = excluded.kind,
        default_roles = excluded.default_roles, folder_prefix = coalesce(excluded.folder_prefix, rvbbit.brain_sources.folder_prefix)
    RETURNING source_id INTO v_id;
    RETURN v_id;
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_ingest(
    p_source text, p_title text, p_body text,
    p_roles text[] DEFAULT NULL, p_folder text DEFAULT NULL, p_uri text DEFAULT NULL,
    p_author text DEFAULT NULL, p_occurred_at timestamptz DEFAULT NULL, p_meta jsonb DEFAULT '{}'::jsonb
) RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_src rvbbit.brain_sources%ROWTYPE; v_doc bigint; v_roles text[]; v_folder text; v_uri text;
BEGIN
    IF p_title IS NULL OR btrim(p_title) = '' THEN RAISE EXCEPTION 'brain_ingest: title required'; END IF;
    -- auto-create the source by label (ergonomic ingest)
    PERFORM rvbbit.brain_define_source(p_source);
    SELECT * INTO v_src FROM rvbbit.brain_sources WHERE label = p_source;

    v_roles  := coalesce(p_roles, v_src.default_roles, '{}');
    v_folder := coalesce(p_folder, v_src.folder_prefix, '/' || p_source);
    v_uri    := coalesce(p_uri, md5(p_title || coalesce(p_body, '')));   -- stable id for re-ingest

    INSERT INTO rvbbit.brain_documents (source_id, uri, title, author, folder_path, body, occurred_at, raw_meta)
    VALUES (v_src.source_id, v_uri, p_title, p_author, v_folder, p_body, p_occurred_at, coalesce(p_meta, '{}'::jsonb))
    ON CONFLICT (source_id, uri) DO UPDATE SET
        title = excluded.title, author = excluded.author, folder_path = excluded.folder_path,
        body = excluded.body, occurred_at = excluded.occurred_at, raw_meta = excluded.raw_meta,
        ingested_at = now()
    RETURNING doc_id INTO v_doc;

    -- re-chunk + re-embed
    DELETE FROM rvbbit.brain_chunks WHERE doc_id = v_doc;
    INSERT INTO rvbbit.brain_chunks (doc_id, idx, text, embedding, token_est)
    SELECT v_doc, ch.idx, ch.chunk, rvbbit.embed(ch.chunk), (length(ch.chunk) / 4)
    FROM rvbbit._brain_chunks(p_body) ch;

    -- (re)assign roles (register unknown roles so the catalog stays complete)
    DELETE FROM rvbbit.brain_doc_roles WHERE doc_id = v_doc;
    IF array_length(v_roles, 1) IS NOT NULL THEN
        INSERT INTO rvbbit.brain_roles (role) SELECT unnest(v_roles) ON CONFLICT DO NOTHING;
        INSERT INTO rvbbit.brain_doc_roles (doc_id, role) SELECT v_doc, unnest(v_roles) ON CONFLICT DO NOTHING;
    END IF;
    RETURN v_doc;
END $fn$;

-- ── ACL primitives ────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rvbbit.brain_grant(p_role text, p_principal text)
RETURNS void LANGUAGE sql VOLATILE AS $fn$
    INSERT INTO rvbbit.brain_roles (role) VALUES (p_role) ON CONFLICT DO NOTHING;
    INSERT INTO rvbbit.brain_role_members (role, principal) VALUES (p_role, p_principal) ON CONFLICT DO NOTHING;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_revoke(p_role text, p_principal text)
RETURNS void LANGUAGE sql VOLATILE AS $fn$
    DELETE FROM rvbbit.brain_role_members WHERE role = p_role AND lower(principal) = lower(p_principal);
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_exclude(p_doc bigint, p_principal text, p_reason text DEFAULT NULL)
RETURNS void LANGUAGE sql VOLATILE AS $fn$
    INSERT INTO rvbbit.brain_doc_exclude (doc_id, principal, reason) VALUES (p_doc, p_principal, p_reason)
    ON CONFLICT (doc_id, principal) DO UPDATE SET reason = excluded.reason;
$fn$;

-- visible_docs(email): the one place the security predicate lives. EXISTS a role the caller holds,
-- AND not on the doc's exclusion belt. NULL/role-less caller -> empty set (default-deny).
CREATE OR REPLACE FUNCTION rvbbit.brain_visible_docs(p_email text)
RETURNS TABLE(doc_id bigint) LANGUAGE sql STABLE AS $fn$
    SELECT d.doc_id FROM rvbbit.brain_documents d
    WHERE p_email IS NOT NULL
      AND EXISTS (SELECT 1 FROM rvbbit.brain_doc_roles dr
                  JOIN rvbbit.brain_role_members rm ON rm.role = dr.role
                  WHERE dr.doc_id = d.doc_id AND lower(rm.principal) = lower(p_email))
      AND NOT EXISTS (SELECT 1 FROM rvbbit.brain_doc_exclude ex
                      WHERE ex.doc_id = d.doc_id AND lower(ex.principal) = lower(p_email));
$fn$;

-- ── retrieval: filter to visible docs, THEN cosine-rank that subset ────────────
-- Restricted chunks never enter the ranked set → cannot be paraphrased into an answer.
CREATE OR REPLACE FUNCTION rvbbit.ask_brain(p_email text, p_query text, p_k int DEFAULT 8)
RETURNS TABLE(doc_id bigint, title text, folder_path text, source text,
              occurred_at timestamptz, chunk text, score double precision)
LANGUAGE sql STABLE AS $fn$
    WITH q AS (SELECT rvbbit.embed(p_query) AS v),
         vis AS (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
    SELECT d.doc_id, d.title, d.folder_path, s.label, d.occurred_at,
           c.text, rvbbit.cosine_vec(c.embedding, (SELECT v FROM q)) AS score
    FROM rvbbit.brain_chunks c
    JOIN vis ON vis.doc_id = c.doc_id
    JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
    JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
    WHERE c.embedding IS NOT NULL AND nullif(btrim(coalesce(p_query, '')), '') IS NOT NULL
    ORDER BY score DESC
    LIMIT greatest(1, least(coalesce(p_k, 8), 50));
$fn$;

-- ── file-explorer feed: the visible folder/doc tree for a caller ───────────────
CREATE OR REPLACE FUNCTION rvbbit.brain_tree(p_email text)
RETURNS TABLE(folder_path text, doc_id bigint, title text, source text, mime text,
              author text, occurred_at timestamptz, ingested_at timestamptz, chunks bigint)
LANGUAGE sql STABLE AS $fn$
    SELECT d.folder_path, d.doc_id, d.title, s.label, d.mime, d.author, d.occurred_at, d.ingested_at,
           (SELECT count(*) FROM rvbbit.brain_chunks c WHERE c.doc_id = d.doc_id) AS chunks
    FROM rvbbit.brain_documents d
    JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
    JOIN rvbbit.brain_visible_docs(p_email) vis ON vis.doc_id = d.doc_id
    ORDER BY d.folder_path, d.title;
$fn$;

-- one doc's full body + metadata, only if the caller may see it
CREATE OR REPLACE FUNCTION rvbbit.brain_get_doc(p_email text, p_doc bigint)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    SELECT to_jsonb(x) FROM (
        SELECT d.doc_id, d.title, d.folder_path, s.label AS source, d.author, d.mime,
               d.occurred_at, d.ingested_at, d.body, d.raw_meta,
               (SELECT array_agg(role ORDER BY role) FROM rvbbit.brain_doc_roles WHERE doc_id = d.doc_id) AS roles
        FROM rvbbit.brain_documents d
        JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
        WHERE d.doc_id = p_doc
          AND EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = d.doc_id)
    ) x;
$fn$;
