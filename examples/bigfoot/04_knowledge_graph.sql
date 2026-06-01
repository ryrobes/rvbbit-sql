\set ON_ERROR_STOP on
\pset pager off
\timing on

\if :{?kg_rows}
\else
\set kg_rows 250
\endif

\echo
\echo ====================================================================
\echo 04. Knowledge graph: deterministic graph from metadata + clue patterns
\echo ====================================================================
\echo KG source rows: :kg_rows

SELECT rvbbit.reset_query_id() AS kg_query_id;

DELETE FROM rvbbit.kg_nodes WHERE graph_id = 'bigfoot_notebook';

CREATE TEMP TABLE bf_kg_sample AS
SELECT bfroid, state, county, season, class, title, report_text
FROM bigfoot.sighting_docs
WHERE report_text IS NOT NULL
ORDER BY COALESCE(NULLIF(regexp_replace(bfroid, '[^0-9]', '', 'g'), '')::bigint, 0)
LIMIT :kg_rows;

CREATE TEMP TABLE bf_kg_facts AS
SELECT
    bfroid AS source_pk,
    'bf_report'::text AS subject_kind,
    'BFRO ' || bfroid AS subject,
    'observed_in_state'::text AS predicate,
    'bf_state'::text AS object_kind,
    state AS object,
    1.0::double precision AS confidence,
    substring(report_text, 1, 900) AS evidence,
    jsonb_build_object('state', state, 'county', county, 'season', season, 'class', class, 'title', title) AS properties
FROM bf_kg_sample
WHERE NULLIF(state, '') IS NOT NULL

UNION ALL

SELECT
    bfroid,
    'bf_report',
    'BFRO ' || bfroid,
    'observed_in_county',
    'bf_county',
    county || ', ' || state,
    1.0,
    substring(report_text, 1, 900),
    jsonb_build_object('state', state, 'county', county, 'season', season, 'class', class, 'title', title)
FROM bf_kg_sample
WHERE NULLIF(county, '') IS NOT NULL
  AND NULLIF(state, '') IS NOT NULL

UNION ALL

SELECT
    bfroid,
    'bf_report',
    'BFRO ' || bfroid,
    'reported_in_season',
    'bf_season',
    season,
    1.0,
    substring(report_text, 1, 900),
    jsonb_build_object('state', state, 'county', county, 'season', season, 'class', class, 'title', title)
FROM bf_kg_sample
WHERE NULLIF(season, '') IS NOT NULL

UNION ALL

SELECT
    s.bfroid,
    'bf_report',
    'BFRO ' || s.bfroid,
    'has_clue',
    'bf_clue',
    c.clue,
    c.confidence,
    substring(s.report_text, 1, 900),
    jsonb_build_object(
        'state', s.state,
        'county', s.county,
        'season', s.season,
        'class', s.class,
        'title', s.title,
        'pattern', c.pattern
    )
FROM bf_kg_sample s
CROSS JOIN LATERAL (
    VALUES
      ('road crossing', 0.88::double precision, 'road|highway|crossing|crossed', s.report_text ~* '(road|highway|crossing|crossed)'),
      ('night encounter', 0.82, 'night|dark|dusk|midnight|headlight', s.report_text ~* '(night|dark|dusk|midnight|headlight)'),
      ('red eyes', 0.86, 'red eyes|glowing eyes|eye shine|eyeshine', s.report_text ~* '(red eyes|glowing eyes|eye shine|eyeshine)'),
      ('vocalization', 0.84, 'scream|howl|whoop|vocal|yell|whistle|knock', s.report_text ~* '(scream|howl|whoop|vocal|yell|whistle|knock)'),
      ('tracks or footprints', 0.86, 'track|tracks|footprint|footprints|prints', s.report_text ~* '(track|tracks|footprint|footprints|prints)'),
      ('vehicle nearby', 0.80, 'car|truck|vehicle|headlight|headlights|four-wheeler|atv', s.report_text ~* '(car|truck|vehicle|headlight|headlights|four-wheeler|atv)'),
      ('multiple witnesses', 0.82, 'we saw|we heard|my friend|my wife|my husband|group|several|both saw', s.report_text ~* '(we saw|we heard|my friend|my wife|my husband|group|several|both saw)'),
      ('water nearby', 0.80, 'river|creek|lake|pond|stream|swamp|marsh', s.report_text ~* '(river|creek|lake|pond|stream|swamp|marsh)'),
      ('foul smell', 0.84, 'smell|odor|stink|musky', s.report_text ~* '(smell|odor|stink|musky)')
) AS c(clue, confidence, pattern, matched)
WHERE c.matched;

WITH asserted AS MATERIALIZED (
    SELECT
        f.*,
        rvbbit.kg_assert_edge(
            f.subject_kind,     -- subject node kind
            f.subject,          -- subject label
            f.predicate,        -- edge label
            f.object_kind,      -- object node kind
            f.object,           -- object label
            f.confidence,       -- 0..1 edge confidence
            '{}'::jsonb,        -- subject properties
            f.properties,       -- edge/object properties
            '',                 -- embedding specialist; blank = default
            0.0,                -- fuzzy match threshold
            'bigfoot_notebook'  -- graph id
        ) AS edge_id
    FROM bf_kg_facts f
),
linked AS (
    SELECT rvbbit.kg_link_evidence(
        target_edge_id => a.edge_id,
        source_table => 'bigfoot.sighting_docs'::regclass,
        source_pk => a.source_pk,
        source_column => 'report_text',
        evidence_text => a.evidence,
        confidence => a.confidence,
        properties => a.properties,
        graph => 'bigfoot_notebook'
    ) AS evidence_id
    FROM asserted a
)
SELECT
    (SELECT count(*) FROM bf_kg_sample) AS sampled_reports,
    (SELECT count(*) FROM bf_kg_facts) AS derived_facts,
    (SELECT count(DISTINCT edge_id) FROM asserted) AS asserted_edges,
    (SELECT count(*) FROM linked) AS evidence_rows;

\echo
\echo -- Graph size by node kind
SELECT kind, count(*) AS nodes
FROM rvbbit.kg_nodes
WHERE graph_id = 'bigfoot_notebook'
GROUP BY kind
ORDER BY kind;

\echo
\echo -- Highest-signal clue nodes
SELECT clue.label AS clue,
       count(DISTINCT report.node_id) AS reports
FROM rvbbit.kg_edges e
JOIN rvbbit.kg_nodes report ON report.node_id = e.subject_node_id
JOIN rvbbit.kg_nodes clue ON clue.node_id = e.object_node_id
WHERE e.graph_id = 'bigfoot_notebook'
  AND e.predicate_norm = 'has_clue'
GROUP BY clue.label
ORDER BY reports DESC, clue.label;

\echo
\echo -- Clues by state
WITH report_states AS (
    SELECT e.subject_node_id AS report_id, state.label AS state
    FROM rvbbit.kg_edges e
    JOIN rvbbit.kg_nodes state ON state.node_id = e.object_node_id
    WHERE e.graph_id = 'bigfoot_notebook'
      AND e.predicate_norm = 'observed_in_state'
),
report_clues AS (
    SELECT e.subject_node_id AS report_id, clue.label AS clue
    FROM rvbbit.kg_edges e
    JOIN rvbbit.kg_nodes clue ON clue.node_id = e.object_node_id
    WHERE e.graph_id = 'bigfoot_notebook'
      AND e.predicate_norm = 'has_clue'
)
SELECT state, clue, count(*) AS reports
FROM report_states s
JOIN report_clues c USING (report_id)
GROUP BY state, clue
ORDER BY reports DESC, state, clue
LIMIT 20;

\echo
\echo -- Evidence-bearing graph context around "red eyes"
SELECT context_rank,
       depth,
       predicate,
       from_kind,
       from_label,
       to_kind,
       to_label,
       evidence_count,
       substring(COALESCE(evidence->0->>'evidence_text', ''), 1, 130) AS evidence_preview
FROM rvbbit.kg_context(
    'bf_clue',                  -- start node kind
    'red eyes',                 -- start node label
    2,                          -- max traversal depth
    15,                         -- max context rows
    'both',                     -- edge direction
    true,                       -- include evidence
    '',                         -- embedding specialist; blank = default
    0.0,                        -- fuzzy match threshold
    'bigfoot_notebook',         -- graph id
    '{"depth_decay":0.6}'::jsonb -- ranking options
);
