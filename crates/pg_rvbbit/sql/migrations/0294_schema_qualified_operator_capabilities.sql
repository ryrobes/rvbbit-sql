-- 0294: capability discovery must return executable operator signatures.
--
-- rvbbit.operators stores an unqualified catalog identity (for example,
-- clover_forecast), but generated SQL functions live in the rvbbit schema.
-- Advertising clover_forecast(...) made agents emit a call that depended on
-- search_path and failed in Warehouse. Keep the identity unchanged while
-- qualifying the callable surface in every generated capability document.

CREATE OR REPLACE FUNCTION rvbbit.capability_doc(
    p_kind text, p_name text, p_signature text, p_description text,
    p_example text, p_cost text, p_tags text[])
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT 'capability ' || p_name
        || E'\nkind: ' || p_kind
        || E'\nsignature: '
        || CASE
             WHEN p_kind = 'cap_operator'
              AND coalesce(btrim(p_signature), '') <> ''
              AND btrim(p_signature) !~* '^rvbbit\.'
             THEN 'rvbbit.' || p_signature
             ELSE coalesce(p_signature, '')
           END
        || E'\ncost: ' || coalesce(p_cost, 'unknown')
        || CASE WHEN cardinality(p_tags) > 0
                THEN E'\ntags: ' || array_to_string(p_tags, ', ') ELSE '' END
        || E'\n' || coalesce(p_description, '')
        || CASE WHEN coalesce(p_example, '') <> ''
                THEN E'\nexample: ' || p_example ELSE '' END;
$fn$;

-- Repair the live search documents without discarding their compatible
-- embeddings. The schema prefix does not change semantic meaning, and the next
-- normal crawl will regenerate the same qualified text through capability_doc.
UPDATE rvbbit.catalog_docs
SET doc = replace(
        doc,
        E'\nsignature: ' || rel_name || '(',
        E'\nsignature: rvbbit.' || rel_name || '('
    ),
    updated_at = clock_timestamp()
WHERE graph_id = 'rvbbit_capabilities'
  AND kind = 'cap_operator'
  AND rel_name IS NOT NULL
  AND doc LIKE '%' || E'\nsignature: ' || rel_name || '(%'
  AND doc NOT LIKE '%' || E'\nsignature: rvbbit.' || rel_name || '(%';

UPDATE rvbbit.kg_nodes
SET properties = jsonb_set(
        jsonb_set(
            properties,
            '{signature}',
            to_jsonb('rvbbit.' || (properties->>'signature')),
            true
        ),
        '{search_doc}',
        to_jsonb(replace(
            properties->>'search_doc',
            E'\nsignature: ' || label || '(',
            E'\nsignature: rvbbit.' || label || '('
        )),
        true
    ),
    updated_at = clock_timestamp()
WHERE graph_id = 'rvbbit_capabilities'
  AND kind = 'cap_operator'
  AND coalesce(properties->>'signature', '') LIKE label || '(%'
  AND coalesce(properties->>'search_doc', '') LIKE
      '%' || E'\nsignature: ' || label || '(%';

COMMENT ON FUNCTION rvbbit.capability_doc(text,text,text,text,text,text,text[]) IS
    'Canonical searchable capability document; cap_operator signatures are schema-qualified as rvbbit.<name>(...).';
