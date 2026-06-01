\set ON_ERROR_STOP on
\pset pager off
\timing on

\if :{?capability_entity_rows}
\else
\set capability_entity_rows 8
\endif

\if :{?capability_rerank_candidates}
\else
\set capability_rerank_candidates 24
\endif

\if :{?capability_classify_rows}
\else
\set capability_classify_rows 8
\endif

\if :{?capability_rerank_query}
\else
\set capability_rerank_query 'road crossing at night with headlights'
\endif

\echo
\echo ====================================================================
\echo 06. Warren capability operators: GLiNER, rerank, classify, emotion
\echo ====================================================================
\echo GLiNER rows: :capability_entity_rows
\echo Rerank candidates: :capability_rerank_candidates
\echo Classification rows: :capability_classify_rows
\echo Rerank query: :capability_rerank_query

\echo
\echo -- Capability-backed operators used by this section
SELECT o.name,
       o.return_type,
       o.arg_names,
       h.name AS backend,
       h.n_calls,
       h.n_errors,
       h.avg_latency_ms
FROM rvbbit.operators o
LEFT JOIN rvbbit.backend_health h
  ON o.steps::text LIKE '%' || h.name || '%'
WHERE o.name IN (
    'extract_entities',
    'contains_entity',
    'has_pii',
    'semantic_score',
    'about',
    'classify',
    'emotion',
    'sentiment'
)
ORDER BY o.name, h.name;

\echo
\echo -- GLiNER entity spans: arbitrary labels over report text
DROP TABLE IF EXISTS bigfoot.capability_entity_spans;
CREATE TABLE bigfoot.capability_entity_spans AS
WITH sample AS (
    SELECT
        bfroid,
        state,
        county,
        title,
        report_text
    FROM bigfoot.sighting_docs
    WHERE report_text IS NOT NULL
    ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
    LIMIT :capability_entity_rows
),
spans AS (
    SELECT
        s.bfroid,
        s.state,
        s.county,
        s.title,
        span
    FROM sample s
    CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(
            rvbbit.extract_entities(
                s.report_text, -- source text
                'location,time of day,animal color,witness,animal,water body' -- labels
            ),
            '[]'::jsonb
        )
    ) AS e(span)
)
SELECT
    bfroid,
    state,
    county,
    title,
    span->>'label' AS label,
    span->>'text' AS value,
    COALESCE((span->>'score')::double precision, 0.0) AS score,
    COALESCE((span->>'start')::integer, -1) AS start_offset,
    COALESCE((span->>'end')::integer, -1) AS end_offset
FROM spans
WHERE COALESCE((span->>'score')::double precision, 0.0) >= 0.35;

ANALYZE bigfoot.capability_entity_spans;

SELECT label,
       count(*) AS spans,
       round(avg(score)::numeric, 3) AS avg_score
FROM bigfoot.capability_entity_spans
GROUP BY label
ORDER BY spans DESC, label;

SELECT bfroid,
       state,
       substring(title, 1, 64) AS title,
       label,
       value,
       round(score::numeric, 3) AS score
FROM bigfoot.capability_entity_spans
ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0),
         label,
         score DESC
LIMIT 24;

\echo
\echo -- Boolean entity operators over the same narrative rows
WITH sample AS (
    SELECT bfroid, state, title, report_text
    FROM bigfoot.sighting_docs
    WHERE report_text IS NOT NULL
    ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
    LIMIT LEAST(:capability_entity_rows, 6)
)
SELECT bfroid,
       state,
       substring(title, 1, 64) AS title,
       rvbbit.contains_entity(
           report_text,  -- source text
           'water body'  -- label to detect
       ) AS has_water_body,
       rvbbit.contains_entity(
           report_text,   -- source text
           'time of day'  -- label to detect
       ) AS has_time_marker,
       rvbbit.has_pii(
           report_text    -- source text
       ) AS has_pii
FROM sample;

\echo
\echo -- Vector recall plus reranker scoring
DROP TABLE IF EXISTS bigfoot.capability_reranked_hits;
CREATE TABLE bigfoot.capability_reranked_hits AS
WITH candidates AS MATERIALIZED (
    SELECT
        row_number() OVER (ORDER BY score DESC) AS knn_rank,
        value,
        score AS knn_score
    FROM rvbbit.knn_text(
        'bigfoot.sighting_docs'::regclass::oid, -- table to search
        'report_text',                          -- text column
        :'capability_rerank_query',             -- recall query text
        :capability_rerank_candidates           -- candidate count
    )
),
scored AS (
    SELECT
        c.knn_rank,
        c.knn_score,
        d.bfroid,
        d.state,
        d.county,
        d.title,
        rvbbit.semantic_score(
            d.report_text,              -- source text
            :'capability_rerank_query'  -- semantic criterion
        ) AS rerank_score,
        rvbbit.text_evidence(
            d.report_text,              -- source text
            :'capability_rerank_query', -- evidence target
            1                           -- snippets to return
        ) AS evidence
    FROM candidates c
    JOIN bigfoot.sighting_docs d ON d.report_text = c.value
)
SELECT
    row_number() OVER (ORDER BY rerank_score DESC, knn_score DESC, knn_rank) AS rerank_rank,
    *
FROM scored;

ANALYZE bigfoot.capability_reranked_hits;

SELECT knn_rank,
       rerank_rank,
       bfroid,
       state,
       substring(title, 1, 68) AS title,
       round(knn_score::numeric, 3) AS knn_score,
       round(rerank_score::numeric, 3) AS rerank_score,
       rerank_score >= 0.02 AS passes_demo_threshold,
       substring(array_to_string(evidence, ' | '), 1, 120) AS evidence
FROM bigfoot.capability_reranked_hits
ORDER BY rerank_rank
LIMIT 10;

\echo
\echo -- Capability-backed classification plus emotion/sentiment rollups
DROP TABLE IF EXISTS bigfoot.capability_report_labels;
CREATE TABLE bigfoot.capability_report_labels AS
WITH sample AS (
    SELECT
        bfroid,
        state,
        county,
        title,
        substring(report_text, 1, 1200) AS operator_text
    FROM bigfoot.sighting_docs
    WHERE report_text IS NOT NULL
    ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
    LIMIT :capability_classify_rows
)
SELECT
    bfroid,
    state,
    county,
    title,
    rvbbit.classify(
        operator_text, -- bounded text to classify
        'road encounter,vocalization,tracks or footprints,water encounter,woods sighting,other' -- labels
    ) AS capability_class,
    rvbbit.emotion(
        operator_text -- bounded text to classify
    ) AS emotion,
    rvbbit.sentiment(
        operator_text -- bounded text to classify
    ) AS sentiment
FROM sample;

ANALYZE bigfoot.capability_report_labels;

SELECT capability_class,
       emotion,
       sentiment,
       count(*) AS reports
FROM bigfoot.capability_report_labels
GROUP BY capability_class, emotion, sentiment
ORDER BY reports DESC, capability_class, emotion, sentiment;

SELECT bfroid,
       state,
       substring(title, 1, 68) AS title,
       capability_class,
       emotion,
       sentiment
FROM bigfoot.capability_report_labels
ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
LIMIT 12;
