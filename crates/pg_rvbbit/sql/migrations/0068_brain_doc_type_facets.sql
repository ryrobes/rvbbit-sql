-- 0068_brain_doc_type_facets — tag every object with a TYPE + a facet-discovery surface for agents.
--
-- In this model SQL data, documents, and MCP artifacts are all "objects to introspect" — great, but an
-- agent mustn't conflate a terse ticket with a 40-page SOP. So tag type EXPLICITLY (server-side) rather
-- than make the agent infer it. Type lives on the SOURCE, defaulted from its PROVIDER:
--   • brain_doc_providers.doc_type  (linear-issues → 'ticket'; default 'document')
--   • resolved per source by rvbbit.brain_doc_type(config): explicit config.doc_type override →
--     provider's doc_type → 'document'. (Stored in config, never `kind` — kind gets clobbered to
--     'manual' by brain_ingest; config is durable, same reason we key query sources on config.provider.)
-- Surfaced as a column in brain_search/ask_brain, filterable via p_filter.type, and discoverable via
-- brain_facets(email) so an agent can ASK what's in the corpus (types/sources + counts) and then narrow —
-- instead of guessing, or us hardcoding. Small vocab, open text: document | ticket | table | message | …

-- ── type on the provider, default 'document'; Linear is tickets ───────────────
ALTER TABLE rvbbit.brain_doc_providers ADD COLUMN IF NOT EXISTS doc_type text NOT NULL DEFAULT 'document';
UPDATE rvbbit.brain_doc_providers SET doc_type = 'ticket' WHERE provider = 'linear-issues';

-- provider definition now carries doc_type (drop the 7-arg form; add an 8th defaulted param)
DROP FUNCTION IF EXISTS rvbbit.brain_define_provider(text, text, text, text, text, text, jsonb);
CREATE OR REPLACE FUNCTION rvbbit.brain_define_provider(
    p_provider text, p_label text, p_list_sql text,
    p_item_sql text DEFAULT NULL, p_icon text DEFAULT NULL, p_description text DEFAULT NULL,
    p_edge_map jsonb DEFAULT '[]'::jsonb, p_doc_type text DEFAULT 'document'
) RETURNS text LANGUAGE sql VOLATILE AS $fn$
    INSERT INTO rvbbit.brain_doc_providers (provider, label, list_sql, item_sql, icon, description, edge_map, doc_type)
    VALUES (p_provider, p_label, p_list_sql, nullif(btrim(p_item_sql),''), p_icon, p_description,
            coalesce(p_edge_map, '[]'::jsonb), coalesce(nullif(btrim(p_doc_type),''), 'document'))
    ON CONFLICT (provider) DO UPDATE SET
        label = excluded.label, list_sql = excluded.list_sql, item_sql = excluded.item_sql,
        icon = excluded.icon, description = excluded.description, edge_map = excluded.edge_map,
        doc_type = excluded.doc_type, updated_at = now()
    RETURNING provider;
$fn$;

-- resolve a source's type: explicit config override → its provider's doc_type → 'document'
CREATE OR REPLACE FUNCTION rvbbit.brain_doc_type(p_config jsonb)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT coalesce(
        nullif(btrim(p_config->>'doc_type'), ''),
        (SELECT doc_type FROM rvbbit.brain_doc_providers WHERE provider = p_config->>'provider'),
        'document');
$fn$;

-- ── facets: what types/sources can this identity see (+ counts)? agent discovery ──
CREATE OR REPLACE FUNCTION rvbbit.brain_facets(p_email text)
RETURNS TABLE(facet text, value text, docs bigint) LANGUAGE sql STABLE AS $fn$
    WITH d AS (
        SELECT d.doc_id, s.label AS source, s.config AS scfg
          FROM rvbbit.brain_documents d
          JOIN rvbbit.brain_visible_docs(p_email) v ON v.doc_id = d.doc_id
          JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
         WHERE d.deleted_at IS NULL)
    SELECT 'type'::text,   rvbbit.brain_doc_type(scfg), count(*) FROM d GROUP BY rvbbit.brain_doc_type(scfg)
    UNION ALL
    SELECT 'source'::text, source,                      count(*) FROM d GROUP BY source
    ORDER BY 1, 3 DESC;
$fn$;

-- ── brain_search: + doc_type column, + p_filter.type dimension ────────────────
DROP FUNCTION IF EXISTS rvbbit.ask_brain(text, text, integer, jsonb);
DROP FUNCTION IF EXISTS rvbbit.brain_search(text, text, integer, jsonb);

CREATE OR REPLACE FUNCTION rvbbit.brain_search(
    p_email text, p_query text, p_k int DEFAULT 8, p_filter jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE(doc_id bigint, chunk_id bigint, chunk_idx int, title text, folder_path text,
                source text, doc_type text, occurred_at timestamptz, chunk text,
                score double precision, entities text[])
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_q real[]; v_k int; v_of int;
    v_sources text[]; v_types text[]; v_folder text; v_since timestamptz; v_until timestamptz;
    v_ids bigint[]; v_scores float8[];
BEGIN
    IF nullif(btrim(coalesce(p_query, '')), '') IS NULL THEN RETURN; END IF;
    v_q := rvbbit.embed(p_query);
    v_k := greatest(1, least(coalesce(p_k, 8), 50));
    v_of := greatest(v_k * 8, 100);

    v_sources := CASE WHEN p_filter ? 'source' THEN
        CASE jsonb_typeof(p_filter->'source') WHEN 'array' THEN ARRAY(SELECT jsonb_array_elements_text(p_filter->'source'))
             ELSE ARRAY[p_filter->>'source'] END ELSE NULL END;
    v_types := CASE WHEN p_filter ? 'type' THEN
        CASE jsonb_typeof(p_filter->'type') WHEN 'array' THEN ARRAY(SELECT jsonb_array_elements_text(p_filter->'type'))
             ELSE ARRAY[p_filter->>'type'] END ELSE NULL END;
    v_folder := nullif(p_filter->>'folder', '');
    v_since  := nullif(p_filter->>'since', '')::timestamptz;
    v_until  := nullif(p_filter->>'until', '')::timestamptz;

    IF rvbbit.vector_ready('brain_chunks', array_length(v_q, 1)) THEN
        SELECT array_agg(t.id ORDER BY t.score DESC, t.id), array_agg(t.score ORDER BY t.score DESC, t.id)
          INTO v_ids, v_scores
          FROM (
            SELECT a.id, a.score
              FROM rvbbit.vector_ann('brain_chunks', v_q, v_of) a
              JOIN rvbbit.brain_chunks c    ON c.chunk_id = a.id
              JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
              JOIN rvbbit.brain_sources s   ON s.source_id = d.source_id
             WHERE c.doc_id IN (SELECT bv.doc_id FROM rvbbit.brain_visible_docs(p_email) bv)
               AND (v_sources IS NULL OR s.label = ANY(v_sources))
               AND (v_types   IS NULL OR rvbbit.brain_doc_type(s.config) = ANY(v_types))
               AND (v_folder  IS NULL OR d.folder_path = v_folder OR d.folder_path LIKE v_folder || '%')
               AND (v_since   IS NULL OR d.occurred_at >= v_since)
               AND (v_until   IS NULL OR d.occurred_at <= v_until)
             ORDER BY a.score DESC LIMIT v_k
          ) t;
    END IF;

    IF v_ids IS NULL OR array_length(v_ids, 1) < v_k THEN
        SELECT array_agg(t.chunk_id ORDER BY t.sc DESC, t.chunk_id), array_agg(t.sc ORDER BY t.sc DESC, t.chunk_id)
          INTO v_ids, v_scores
          FROM (
            SELECT c.chunk_id, rvbbit.cosine_vec(c.embedding, v_q) AS sc
              FROM rvbbit.brain_chunks c
              JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
              JOIN rvbbit.brain_sources s   ON s.source_id = d.source_id
             WHERE c.embedding IS NOT NULL
               AND c.doc_id IN (SELECT bv.doc_id FROM rvbbit.brain_visible_docs(p_email) bv)
               AND (v_sources IS NULL OR s.label = ANY(v_sources))
               AND (v_types   IS NULL OR rvbbit.brain_doc_type(s.config) = ANY(v_types))
               AND (v_folder  IS NULL OR d.folder_path = v_folder OR d.folder_path LIKE v_folder || '%')
               AND (v_since   IS NULL OR d.occurred_at >= v_since)
               AND (v_until   IS NULL OR d.occurred_at <= v_until)
             ORDER BY sc DESC LIMIT v_k
          ) t;
    END IF;

    IF v_ids IS NULL THEN RETURN; END IF;

    RETURN QUERY
        SELECT d.doc_id, c.chunk_id, c.idx, d.title, d.folder_path, s.label,
               rvbbit.brain_doc_type(s.config), d.occurred_at, c.text,
               v_scores[arr.ord]::double precision AS score,
               coalesce((SELECT array_agg(lbl ORDER BY prio, lbl) FROM (
                    SELECT max(ob.label) AS lbl,
                           min(CASE WHEN ob.kind IN ('location','state','place','organization','person',
                                                     'metric','event','product','program') THEN 0 ELSE 1 END) AS prio
                      FROM rvbbit.kg_evidence ev
                      JOIN rvbbit.kg_edges me ON me.edge_id = ev.edge_id AND me.predicate_norm = 'mentions'
                      JOIN rvbbit.kg_nodes ob ON ob.node_id = me.object_node_id
                     WHERE ev.graph_id = 'brain' AND ev.source_table = 'rvbbit.brain_chunks'::regclass
                       AND ev.source_pk = c.chunk_id::text
                       AND NOT rvbbit._brain_is_junk_entity(ob.label)
                     GROUP BY rvbbit._brain_norm_key(ob.label)
                     ORDER BY min(CASE WHEN ob.kind IN ('location','state','place','organization','person',
                                                        'metric','event','product','program') THEN 0 ELSE 1 END),
                              max(lower(ob.label))
                     LIMIT 12) z), '{}') AS entities
          FROM unnest(v_ids) WITH ORDINALITY AS arr(cid, ord)
          JOIN rvbbit.brain_chunks c    ON c.chunk_id = arr.cid
          JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
          JOIN rvbbit.brain_sources s   ON s.source_id = d.source_id
         ORDER BY arr.ord;
END $fn$;

-- ask_brain: + doc_type (delegates; the agent gets type per hit)
CREATE OR REPLACE FUNCTION rvbbit.ask_brain(
    p_email text, p_query text, p_k int DEFAULT 8, p_filter jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE(doc_id bigint, title text, folder_path text, source text, doc_type text,
                occurred_at timestamptz, chunk text, score double precision)
LANGUAGE sql STABLE AS $fn$
    SELECT doc_id, title, folder_path, source, doc_type, occurred_at, chunk, score
      FROM rvbbit.brain_search(p_email, p_query, p_k, p_filter);
$fn$;
