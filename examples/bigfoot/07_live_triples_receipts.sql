\set ON_ERROR_STOP on
\pset pager off
\timing on

\if :{?live_rows}
\else
\set live_rows 3
\endif

\echo
\echo ====================================================================
\echo 07. Optional live model section: triples_rows + receipts/costs
\echo ====================================================================
\echo Live rows: :live_rows

SELECT rvbbit.reset_query_id() AS live_query_id;

DROP TABLE IF EXISTS bigfoot.live_triples;
CREATE TABLE bigfoot.live_triples AS
WITH sample AS (
    SELECT bfroid, state, county, report_text
    FROM bigfoot.sighting_docs
    WHERE report_text IS NOT NULL
    ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
    LIMIT :live_rows
)
SELECT
    s.bfroid,
    s.state,
    s.county,
    tr.*
FROM sample s
CROSS JOIN LATERAL rvbbit.triples_rows(
    s.report_text, -- source text
    'wildlife field report with locations, witnesses, clues, dates, and observations', -- extraction focus
    '{}'::jsonb    -- options
) tr;

SELECT *
FROM bigfoot.live_triples
ORDER BY bfroid, subject_kind, subject, predicate
LIMIT 30;

SELECT rvbbit.kg_ingest_triples(
    $$
    SELECT
        subject_kind,
        subject,
        predicate,
        object_kind,
        object,
        confidence,
        evidence,
        properties,
        bfroid AS source_pk,
        'report_text'::text AS source_column
    FROM bigfoot.live_triples
    $$,
    source_table => 'bigfoot.sighting_docs'::regclass,
    graph => 'bigfoot_notebook_live'
) AS live_ingest;

SELECT kind, count(*) AS nodes
FROM rvbbit.kg_nodes
WHERE graph_id = 'bigfoot_notebook_live'
GROUP BY kind
ORDER BY kind;

SELECT
    receipt_id,
    operator,
    model,
    latency_ms,
    error,
    invocation_at
FROM rvbbit.receipts
ORDER BY invocation_at DESC
LIMIT 20;

-- No arguments; returns receipt and cost health JSON.
SELECT rvbbit.cost_audit_summary();
