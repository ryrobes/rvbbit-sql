\set ON_ERROR_STOP on
\pset pager off
\timing on

\echo
\echo ====================================================================
\echo 02. Semantic retrieval: materialize embeddings, KNN, evidence snippets
\echo ====================================================================

SELECT rvbbit.materialize_embeddings(
    'bigfoot.sighting_docs'::regclass::oid, -- table to pre-embed
    'report_text'                           -- text column to embed
) AS new_embeddings;

\echo
\echo -- Query A: road crossing at night
WITH hits AS (
    SELECT value, score
    FROM rvbbit.knn_text(
        'bigfoot.sighting_docs'::regclass::oid,             -- table to search
        'report_text',                                      -- text column
        'large hairy creature crossing a road at night in headlights', -- query text
        8                                                   -- top-k matches
    )
)
SELECT
    d.bfroid,
    d.state,
    d.county,
    round(h.score::numeric, 3) AS score,
    d.title,
    rvbbit.text_evidence(
        d.report_text,                          -- source text
        'road crossing at night in headlights', -- evidence target
        2                                       -- snippets to return
    ) AS evidence
FROM hits h
JOIN bigfoot.sighting_docs d ON d.report_text = h.value
ORDER BY h.score DESC;

\echo
\echo -- Query B: vocalization or screams
WITH hits AS (
    SELECT value, score
    FROM rvbbit.knn_text(
        'bigfoot.sighting_docs'::regclass::oid,        -- table to search
        'report_text',                                 -- text column
        'loud screams whoops vocalizations heard in the forest', -- query text
        8                                              -- top-k matches
    )
)
SELECT
    d.bfroid,
    d.state,
    d.county,
    round(h.score::numeric, 3) AS score,
    d.title,
    rvbbit.text_evidence(
        d.report_text,                                  -- source text
        'screams whoops vocalizations heard in forest', -- evidence target
        2                                               -- snippets to return
    ) AS evidence
FROM hits h
JOIN bigfoot.sighting_docs d ON d.report_text = h.value
ORDER BY h.score DESC;

\echo
\echo -- Query C: red eyes / eye shine
WITH hits AS (
    SELECT value, score
    FROM rvbbit.knn_text(
        'bigfoot.sighting_docs'::regclass::oid, -- table to search
        'report_text',                          -- text column
        'red glowing eyes or eye shine in the dark', -- query text
        8                                       -- top-k matches
    )
)
SELECT
    d.bfroid,
    d.state,
    d.county,
    round(h.score::numeric, 3) AS score,
    d.title,
    rvbbit.text_evidence(
        d.report_text,              -- source text
        'red glowing eyes in dark', -- evidence target
        2                           -- snippets to return
    ) AS evidence
FROM hits h
JOIN bigfoot.sighting_docs d ON d.report_text = h.value
ORDER BY h.score DESC;

SELECT specialist, n_entries, dim, total_bytes
FROM rvbbit.embedding_cache_stats()
ORDER BY specialist;
