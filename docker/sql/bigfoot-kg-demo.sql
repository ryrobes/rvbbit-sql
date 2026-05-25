-- SQL-native knowledge graph demo on BFRO sightings.
--
-- Prerequisite:
--   make bigfoot-load
--
-- This intentionally avoids LLM calls. It derives a small, repeatable KG from
-- the long free-form `observed` field so graph traversal and evidence handling
-- can be tested without sidecars.

\timing on
\pset pager off

\echo
\echo ====================================================================
\echo 0. Build a deterministic Bigfoot KG from free-form observations
\echo ====================================================================

SELECT rvbbit.reset_query_id() AS kg_query_id;

CREATE TEMP TABLE bf_kg_sample AS
SELECT bfroid, state, county, title, observed
FROM bigfoot_sightings
WHERE observed IS NOT NULL
  AND length(observed) > 200
ORDER BY bfroid
LIMIT 250;

CREATE TEMP TABLE bf_kg_facts AS
SELECT
    bfroid AS source_pk,
    'bf_report'::text AS subject_kind,
    'BFRO ' || bfroid AS subject,
    'observed_in_state'::text AS predicate,
    'bf_state'::text AS object_kind,
    state AS object,
    1.0::double precision AS confidence,
    substring(observed, 1, 700) AS evidence,
    jsonb_build_object('source', 'bigfoot_kg_demo', 'state', state, 'county', county, 'title', title) AS properties
FROM bf_kg_sample
WHERE state IS NOT NULL AND btrim(state) <> ''

UNION ALL

SELECT
    bfroid,
    'bf_report',
    'BFRO ' || bfroid,
    'observed_in_county',
    'bf_county',
    county || ', ' || state,
    1.0,
    substring(observed, 1, 700),
    jsonb_build_object('source', 'bigfoot_kg_demo', 'state', state, 'county', county, 'title', title)
FROM bf_kg_sample
WHERE county IS NOT NULL AND btrim(county) <> ''
  AND state IS NOT NULL AND btrim(state) <> ''

UNION ALL

SELECT
    s.bfroid,
    'bf_report',
    'BFRO ' || s.bfroid,
    'has_clue',
    'bf_clue',
    c.clue,
    c.confidence,
    substring(s.observed, 1, 700),
    jsonb_build_object(
        'source', 'bigfoot_kg_demo',
        'state', s.state,
        'county', s.county,
        'title', s.title,
        'pattern', c.pattern
    )
FROM bf_kg_sample s
CROSS JOIN LATERAL (
    VALUES
      ('road crossing', 0.88::double precision, 'road|highway|crossing|crossed', s.observed ~* '(road|highway|crossing|crossed)'),
      ('night encounter', 0.82, 'night|dark|dusk|midnight', s.observed ~* '(night|dark|dusk|midnight)'),
      ('red eyes', 0.86, 'red eyes|glowing eyes|eye shine|eyeshine', s.observed ~* '(red eyes|glowing eyes|eye shine|eyeshine)'),
      ('vocalization', 0.84, 'scream|howl|whoop|vocal|yell|whistle', s.observed ~* '(scream|howl|whoop|vocal|yell|whistle)'),
      ('tracks or footprints', 0.86, 'track|tracks|footprint|footprints|prints', s.observed ~* '(track|tracks|footprint|footprints|prints)'),
      ('vehicle nearby', 0.80, 'car|truck|vehicle|headlight|headlights', s.observed ~* '(car|truck|vehicle|headlight|headlights)'),
      ('multiple witnesses', 0.82, 'we saw|we heard|my friend|my wife|my husband|group|several', s.observed ~* '(we saw|we heard|my friend|my wife|my husband|group|several)'),
      ('water nearby', 0.80, 'river|creek|lake|pond|stream|swamp', s.observed ~* '(river|creek|lake|pond|stream|swamp)'),
      ('foul smell', 0.84, 'smell|odor|stink|musky', s.observed ~* '(smell|odor|stink|musky)')
) AS c(clue, confidence, pattern, matched)
WHERE c.matched;

WITH asserted AS MATERIALIZED (
    SELECT
        f.*,
        rvbbit.kg_assert_edge(
            f.subject_kind,
            f.subject,
            f.predicate,
            f.object_kind,
            f.object,
            f.confidence,
            '{}'::jsonb,
            f.properties,
            '',
            0.0,
            'bigfoot_demo'
        ) AS edge_id
    FROM bf_kg_facts f
),
linked AS (
    SELECT rvbbit.kg_link_evidence(
        target_edge_id => a.edge_id,
        source_table => 'bigfoot_sightings'::regclass,
        source_pk => a.source_pk,
        source_column => 'observed',
        evidence_text => a.evidence,
        confidence => a.confidence,
        properties => a.properties,
        graph => 'bigfoot_demo'
    ) AS evidence_id
    FROM asserted a
    WHERE NOT EXISTS (
        SELECT 1
        FROM rvbbit.kg_evidence ev
        WHERE ev.graph_id = 'bigfoot_demo'
          AND ev.edge_id = a.edge_id
          AND ev.source_table = 'bigfoot_sightings'::regclass
          AND ev.source_pk = a.source_pk
          AND ev.source_column = 'observed'
    )
)
SELECT
    (SELECT count(*) FROM bf_kg_sample) AS sampled_reports,
    (SELECT count(*) FROM bf_kg_facts) AS derived_facts,
    (SELECT count(DISTINCT edge_id) FROM asserted) AS asserted_edges,
    (SELECT count(*) FROM linked) AS new_evidence_rows;

\echo
\echo ====================================================================
\echo 1. Graph size by node kind
\echo ====================================================================

SELECT kind, count(*) AS nodes
FROM rvbbit.kg_nodes
WHERE graph_id = 'bigfoot_demo'
GROUP BY kind
ORDER BY kind;

\echo
\echo ====================================================================
\echo 2. Highest-signal clue nodes extracted from observations
\echo ====================================================================

SELECT clue.label AS clue,
       count(DISTINCT report.node_id) AS reports
FROM rvbbit.kg_edges e
JOIN rvbbit.kg_nodes report ON report.node_id = e.subject_node_id
JOIN rvbbit.kg_nodes clue ON clue.node_id = e.object_node_id
WHERE e.graph_id = 'bigfoot_demo'
  AND e.predicate_norm = 'has_clue'
GROUP BY clue.label
ORDER BY reports DESC, clue.label;

\echo
\echo ====================================================================
\echo 3. Clues by state: graph join over report -> state and report -> clue
\echo ====================================================================

WITH report_states AS (
    SELECT e.subject_node_id AS report_id, state.label AS state
    FROM rvbbit.kg_edges e
    JOIN rvbbit.kg_nodes state ON state.node_id = e.object_node_id
    WHERE e.graph_id = 'bigfoot_demo'
      AND e.predicate_norm = 'observed_in_state'
),
report_clues AS (
    SELECT e.subject_node_id AS report_id, clue.label AS clue
    FROM rvbbit.kg_edges e
    JOIN rvbbit.kg_nodes clue ON clue.node_id = e.object_node_id
    WHERE e.graph_id = 'bigfoot_demo'
      AND e.predicate_norm = 'has_clue'
)
SELECT state,
       clue,
       count(*) AS reports
FROM report_states s
JOIN report_clues c USING (report_id)
GROUP BY state, clue
ORDER BY reports DESC, state, clue
LIMIT 20;

\echo
\echo ====================================================================
\echo 4. Path search: a state connected to red eyes through reports
\echo ====================================================================

\echo -- Pick a state that actually has a red-eyes report in this sample.
WITH red_eye_reports AS (
    SELECT e.subject_node_id AS report_id
    FROM rvbbit.kg_edges e
    JOIN rvbbit.kg_nodes clue ON clue.node_id = e.object_node_id
    WHERE e.graph_id = 'bigfoot_demo'
      AND e.predicate_norm = 'has_clue'
      AND clue.kind = 'bf_clue'
      AND clue.label = 'red eyes'
),
candidate_state AS (
    SELECT state.label AS state
    FROM red_eye_reports r
    JOIN rvbbit.kg_edges e ON e.subject_node_id = r.report_id
    JOIN rvbbit.kg_nodes state ON state.node_id = e.object_node_id
    WHERE e.graph_id = 'bigfoot_demo'
      AND e.predicate_norm = 'observed_in_state'
    GROUP BY state.label
    ORDER BY count(*) DESC, state.label
    LIMIT 1
)
SELECT s.state,
       p.length,
       p.labels,
       (
         SELECT array_agg(e.predicate ORDER BY ord)
         FROM unnest(p.edge_ids) WITH ORDINALITY AS path_edges(edge_id, ord)
         JOIN rvbbit.kg_edges e ON e.edge_id = path_edges.edge_id
       ) AS predicates
FROM candidate_state s
CROSS JOIN LATERAL rvbbit.kg_paths(
    'bf_state', s.state,
    'bf_clue', 'red eyes',
    3,
    'both',
    '',
    0.0,
    'bigfoot_demo'
) p
LIMIT 10;

\echo
\echo ====================================================================
\echo 5. Evidence-bearing graph context around a clue
\echo ====================================================================

SELECT context_rank,
       depth,
       predicate,
       from_kind,
       from_label,
       to_kind,
       to_label,
       evidence_count,
       substring(COALESCE(evidence->0->>'evidence_text', ''), 1, 130) || '...' AS evidence_preview
FROM rvbbit.kg_context(
    'bf_clue',
    'red eyes',
    2,
    15,
    'both',
    true,
    '',
    0.0,
    'bigfoot_demo',
    '{"depth_decay":0.6}'::jsonb
);

\echo
\echo ====================================================================
\echo End of Bigfoot KG demo.
\echo ====================================================================
