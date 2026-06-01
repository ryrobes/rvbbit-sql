\set ON_ERROR_STOP on
\pset pager off
\timing on

\if :{?classify_rows}
\else
\set classify_rows 250
\endif

\if :{?extract_rows}
\else
\set extract_rows 12
\endif

\echo
\echo ====================================================================
\echo 03. Semantic map: classification, topics, outliers, diff, extraction
\echo ====================================================================
\echo Classification rows: :classify_rows
\echo Extraction rows: :extract_rows

DROP TABLE IF EXISTS bigfoot.encounter_map;
CREATE TABLE bigfoot.encounter_map AS
SELECT
    bfroid,
    state,
    county,
    season,
    class,
    title,
    rvbbit.semantic_case(
        report_text, -- text to classify
        ARRAY[
            'visual sighting where witnesses saw the creature',
            'auditory experience with screams howls knocks or vocalizations',
            'physical evidence such as tracks footprints hair or structures',
            'roadside or vehicle encounter with headlights cars trucks or roads',
            'camping hiking hunting or wilderness presence without clear sighting'
        ], -- semantic descriptions, in decision order
        ARRAY['visual', 'auditory', 'physical_evidence', 'road_vehicle', 'woods_presence'], -- returned labels
        'unclear', -- fallback label
        0.0        -- minimum score threshold
    ) AS encounter_type,
    rvbbit.semantic_case(
        report_text, -- text to classify
        ARRAY[
            'highly specific report with location sensory detail and witness context',
            'brief vague report with few concrete details',
            'second hand story or historical local legend'
        ], -- semantic descriptions, in decision order
        ARRAY['detailed', 'thin', 'second_hand'], -- returned labels
        'mixed', -- fallback label
        0.0      -- minimum score threshold
    ) AS report_detail
FROM bigfoot.sighting_docs
ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
LIMIT :classify_rows;

ANALYZE bigfoot.encounter_map;

\echo
\echo -- Encounter type distribution
SELECT encounter_type, count(*) AS reports
FROM bigfoot.encounter_map
GROUP BY encounter_type
ORDER BY reports DESC, encounter_type;

\echo
\echo -- Encounter types by state
SELECT state, encounter_type, count(*) AS reports
FROM bigfoot.encounter_map
WHERE state IS NOT NULL
GROUP BY state, encounter_type
ORDER BY reports DESC, state, encounter_type
LIMIT 20;

\echo
\echo -- Topic clusters over report_text
SELECT cluster_id, count, substring(exemplar, 1, 180) AS exemplar
FROM rvbbit.topics(
    'SELECT report_text FROM bigfoot.sighting_docs ORDER BY bfroid LIMIT 250', -- text-row SQL
    7                                                                         -- cluster count
)
ORDER BY cluster_id;

\echo
\echo -- Outliers: unusual reports in the notebook sample
SELECT substring(text, 1, 220) AS report_preview,
       round(score::numeric, 3) AS isolation
FROM rvbbit.outliers(
    'SELECT report_text FROM bigfoot.sighting_docs ORDER BY bfroid LIMIT 250', -- text-row SQL
    8                                                                         -- outliers to return
);

\echo
\echo -- Semantic diff: Washington-like reports that are unlike California reports
SELECT substring(text, 1, 220) AS washington_unique_report,
       round(novelty::numeric, 3) AS novelty
FROM rvbbit.diff(
    'SELECT report_text FROM bigfoot.sighting_docs WHERE state = ''Washington'' LIMIT 120', -- candidate set
    'SELECT report_text FROM bigfoot.sighting_docs WHERE state = ''California'' LIMIT 120', -- comparison set
    8                                                                                      -- novel rows to return
);

\echo
\echo -- Near-duplicate / similar reports
SELECT group_id,
       size,
       substring(representative, 1, 180) AS representative
FROM rvbbit.dedupe_groups(
    'SELECT concat_ws('' '', state, county, title) FROM bigfoot.sighting_docs ORDER BY bfroid LIMIT 500', -- text-row SQL
    0.84                                                                                                -- similarity threshold
)
WHERE size > 1
ORDER BY size DESC, group_id
LIMIT 10;

\echo
\echo -- Structured extraction over a small subset
DROP TABLE IF EXISTS bigfoot.extracted_facts;
CREATE TABLE bigfoot.extracted_facts AS
SELECT
    bfroid,
    state,
    county,
    rvbbit.extract(
        report_text,                      -- source text
        'specific location or place name' -- entity description
    ) AS place,
    rvbbit.extract(
        report_text,  -- source text
        'time of day' -- entity description
    ) AS time_of_day,
    rvbbit.extract(
        report_text,                 -- source text
        'animal color or hair color' -- entity description
    ) AS color_or_hair,
    rvbbit.extract(
        report_text,          -- source text
        'number of witnesses' -- entity description
    ) AS witness_count
FROM bigfoot.sighting_docs
WHERE state IN ('Washington', 'California', 'Oregon', 'Texas')
ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
LIMIT :extract_rows;

SELECT *
FROM bigfoot.extracted_facts
ORDER BY bfroid;
