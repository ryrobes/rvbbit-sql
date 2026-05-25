-- Rvbbit semantic SQL demo on the BFRO bigfoot sightings dataset.
--
-- Prerequisites (only TWO steps):
--   make gpu-up        -- bring up embed/rerank/extract/nli GPU sidecars
--   make bigfoot-load  -- one-time: load ~5000 sightings
--   make bigfoot-demo  -- registers + wires specialists, then runs this file
--
-- `make bigfoot-demo` cats register-gpu-specialists.sql +
-- wire-operators-to-specialists.sql ahead of this file, so the demo is
-- immune to the specialist registrations being wiped by an extension
-- recreate (e.g. a concurrent benchmark). If you run this file alone,
-- register + wire the specialists first.
--
-- Every block below is a self-contained showcase of one capability.
-- Numbers in the comments are wall-clock ms from a clean run on the
-- 3090ti; your numbers will vary on first call (model loads + embed
-- materialization) but the SECOND run of any block is sub-100ms.

\timing on
\pset pager off

\echo
\echo ====================================================================
\echo 0. Sample 500 sightings into a working set + pre-warm embeddings
\echo ====================================================================

DROP TABLE IF EXISTS bf;
CREATE TABLE bf (
    bfroid   text PRIMARY KEY,
    state    text,
    county   text,
    title    text,
    observed text
) USING rvbbit;
INSERT INTO bf
    SELECT bfroid, state, county, title, observed
    FROM bigfoot_sightings
    WHERE observed IS NOT NULL AND length(observed) > 100
    ORDER BY bfroid
    LIMIT 500;

-- Flush the heap into a parquet row group so the per-group HLL +
-- bitmap_select sections below can find row-group metadata.
SELECT rvbbit.export_to_parquet('bf'::regclass) AS rows_compacted;

-- One BGE-M3 forward pass per distinct observed (batched in groups of
-- 64). After this, every semantic call below is a cache hit.
SELECT rvbbit.materialize_embeddings('bf'::regclass::oid, 'observed') AS new_embeddings;

\echo
\echo ====================================================================
\echo 1. knn_text — top-3 semantically-similar sightings to a query
\echo ====================================================================

SELECT
    substring(value, 1, 90) || E'…' AS sighting,
    round(score::numeric, 3) AS score
FROM rvbbit.knn_text(
    'bf'::regclass::oid,
    'observed',
    'large hairy creature crossing the road at night',
    3);

\echo
\echo ====================================================================
\echo 2. topics — k=5 themes across all sightings
\echo ====================================================================

SELECT
    cluster_id,
    count,
    substring(exemplar, 1, 90) || E'…' AS exemplar
FROM rvbbit.topics('SELECT observed FROM bf', 5);

\echo
\echo ====================================================================
\echo 3. outliers (isolation) — top-3 most-unusual sightings
\echo ====================================================================

SELECT
    substring(text, 1, 90) || E'…' AS sighting,
    round(score::numeric, 3) AS isolation
FROM rvbbit.outliers('SELECT observed FROM bf', 3);

\echo
\echo ====================================================================
\echo 4. outliers (criterion) — least-relevant to "vocalization or scream"
\echo ====================================================================

SELECT
    substring(text, 1, 90) || E'…' AS sighting,
    round(score::numeric, 3) AS irrelevance
FROM rvbbit.outliers(
    'SELECT observed FROM bf',
    3,
    'vocalization or scream'
);

\echo
\echo ====================================================================
\echo 5. dedupe_groups — near-duplicate sightings at 0.85 cosine threshold
\echo ====================================================================

SELECT
    group_id,
    size,
    substring(representative, 1, 70) || E'…' AS canonical
FROM rvbbit.dedupe_groups('SELECT observed FROM bf', 0.85)
WHERE size > 1
ORDER BY size DESC, group_id
LIMIT 5;

\echo
\echo ====================================================================
\echo 6. semantic_case — multi-branch classification (encounter type)
\echo ====================================================================

SELECT
    bfroid,
    rvbbit.semantic_case(
        observed,
        ARRAY[
            'visual sighting where they SAW the creature',
            'auditory experience where they HEARD vocalizations or noises',
            'physical evidence like footprints or hair samples',
            'they were followed or felt watched without seeing'
        ],
        ARRAY['visual', 'auditory', 'physical_evidence', 'paranoid'],
        'unclear'
    ) AS encounter_type
FROM bf
LIMIT 10;

\echo
\echo ====================================================================
\echo 7. diff — sightings in TX that have no semantic analog in WA
\echo ====================================================================

SELECT
    substring(text, 1, 90) || E'…' AS tx_unique_sighting,
    round(novelty::numeric, 3) AS novelty
FROM rvbbit.diff(
    'SELECT observed FROM bf WHERE state = ''Texas''',
    'SELECT observed FROM bf WHERE state = ''Washington''',
    5);

\echo
\echo ====================================================================
\echo 8. extract via GLiNER specialist — pull location names from sightings
\echo ====================================================================

SELECT
    bfroid,
    state,
    rvbbit.extract(observed, 'specific location or place name') AS place,
    rvbbit.extract(observed, 'time of day') AS when_at
FROM bf
WHERE state = 'Washington'
LIMIT 5;

\echo
\echo ====================================================================
\echo 9. about via Gradio rerank — relevance scoring (cross-encoder)
\echo ====================================================================

SELECT
    bfroid,
    substring(observed, 1, 70) || E'…' AS preview,
    round(rvbbit.about(observed, 'multiple witnesses present')::numeric, 3) AS witnesses_score,
    round(rvbbit.about(observed, 'creature aggression toward humans')::numeric, 3) AS aggression_score
FROM bf
LIMIT 8;

\echo
\echo ====================================================================
\echo 10. text_evidence — show WHY a sighting matched 'red eyes glowing'
\echo ====================================================================

SELECT
    bfroid,
    rvbbit.text_evidence(observed, 'red eyes glowing in dark', 2) AS evidence
FROM bf
WHERE rvbbit.similarity(observed, 'red eyes glowing in dark') > 0.5
LIMIT 5;

\echo
\echo ====================================================================
\echo 11a. sentiment via deberta NLI specialist — per-row classification
\echo ====================================================================

SELECT bfroid, state, rvbbit.sentiment(observed) AS feel
FROM bf
WHERE state IN ('Washington', 'Texas', 'Oregon')
LIMIT 10;

\echo
\echo ====================================================================
\echo 11b. classify via deberta NLI specialist — encounter shape
\echo ====================================================================

SELECT
    bfroid,
    state,
    rvbbit.classify(observed, 'visual sighting,audio only,physical evidence,unclear') AS shape
FROM bf
WHERE state = 'Washington'
LIMIT 10;

\echo
\echo ====================================================================
\echo 11c. contradicts via deberta NLI — find sightings that disagree
\echo ====================================================================

-- Spot reports whose own narrative undermines the "definitive bigfoot"
-- framing by contradicting it (typical examples: witness later sees
-- it was a bear, or describes something far too small).
SELECT bfroid,
       rvbbit.contradicts(observed, 'a large bigfoot creature was definitely seen') AS recants
FROM bf
WHERE state = 'Washington'
LIMIT 5;

\echo
\echo ====================================================================
\echo 12. explain_semantic — preview cost + cache state
\echo ====================================================================

SELECT line FROM rvbbit.explain_semantic(
    $q$ SELECT observed
        FROM bf
        WHERE rvbbit.about(observed, 'aggressive encounter') > 0.7
          AND rvbbit.semantic_case(observed,
                ARRAY['visual','audio'],
                ARRAY['saw','heard'], 'other') = 'saw'
    $q$
);

\echo
\echo ====================================================================
\echo 13a. approx_distinct — HLL cardinality from row-group sketches
\echo ====================================================================

SELECT rvbbit.approx_distinct('bf'::regclass::oid, 'observed') AS approx_observed,
       (SELECT count(DISTINCT observed) FROM bf) AS exact_observed,
       rvbbit.approx_distinct('bf'::regclass::oid, 'state') AS approx_state,
       (SELECT count(DISTINCT state) FROM bf) AS exact_state;

\echo
\echo ====================================================================
\echo 13b. semantic_mv — incremental materialized projection (cached row-wise)
\echo ====================================================================

-- The MV computes rvbbit.sentiment(observed) per row, caches the result,
-- and only re-runs for new rows on refresh. Joinable like a normal table.
SELECT rvbbit.semantic_mv_drop('bf_sentiments');  -- clean slate
SELECT rvbbit.semantic_mv_create(
    mv_name => 'bf_sentiments',
    source_rel => 'bf'::regclass::oid,
    pk_col => 'bfroid',
    projection_sql => 'rvbbit.sentiment(observed)',
    projection_col => 'feel',
    projection_type => 'text');

-- Join the MV back to the source table — every row in `bf` gets a `feel`.
SELECT bfroid, state, substring(observed, 1, 50) || E'…' AS preview, feel
FROM bf JOIN rvbbit.bf_sentiments USING (bfroid)
LIMIT 5;

\echo
\echo ====================================================================
\echo 13c. bitmap_select — JOIN-filter via a cached predicate bitmap
\echo ====================================================================

-- Populate a bitmap of "sightings that mention vehicles" via cheap ILIKE
-- (replace with rvbbit.means for an LLM-driven version).
SELECT rvbbit.bitmap_populate(
    'bf'::regclass::oid, 'vehicle_sighting', 'lexical-v1',
    $$ observed ILIKE '%car%' OR observed ILIKE '%truck%' OR observed ILIKE '%vehicle%' $$);

-- Now query the matching rows in one JOIN — the bitmap pre-filters
-- without re-evaluating the predicate per row.
SELECT t.bfroid, t.state, substring(t.observed, 1, 70) || E'…' AS preview
FROM bf t
JOIN rvbbit.bitmap_select_text('bf'::regclass::oid, 'bfroid',
                               'vehicle_sighting', 'lexical-v1') AS m(bfroid)
     USING (bfroid)
LIMIT 5;

\echo
\echo ====================================================================
\echo 14. cache stats — every semantic call above shares one cache
\echo ====================================================================

SELECT specialist, n_entries, dim, total_bytes
FROM rvbbit.embedding_cache_stats();

SELECT op_name, n_invocations, n_unique_inputs, total_latency_ms
FROM rvbbit.judgment_stats('about')
UNION ALL
SELECT op_name, n_invocations, n_unique_inputs, total_latency_ms
FROM rvbbit.judgment_stats('extract')
UNION ALL
SELECT op_name, n_invocations, n_unique_inputs, total_latency_ms
FROM rvbbit.judgment_stats('classify')
UNION ALL
SELECT op_name, n_invocations, n_unique_inputs, total_latency_ms
FROM rvbbit.judgment_stats('sentiment')
UNION ALL
SELECT op_name, n_invocations, n_unique_inputs, total_latency_ms
FROM rvbbit.judgment_stats('contradicts')
ORDER BY op_name;

\echo
\echo ====================================================================
\echo 15. semantic flow operators — retry / wards / takes on real rows
\echo ====================================================================

-- A small 6-row working set: these operators do 1-4 model calls per row,
-- so the section stays snappy and observable.
DROP TABLE IF EXISTS bf_flow;
CREATE TABLE bf_flow AS
    SELECT bfroid, state, observed
    FROM bigfoot_sightings
    WHERE observed IS NOT NULL AND length(observed) > 200
    ORDER BY bfroid
    LIMIT 6;

\echo
\echo -- clean_year: retry-validated 4-digit year extraction --
SELECT bfroid, state, rvbbit.clean_year(observed) AS year
FROM bf_flow ORDER BY bfroid;

\echo
\echo -- redact: PII stripped; a blocking post-ward rejects any leaked email --
SELECT bfroid,
       substring(rvbbit.redact(observed), 1, 130) || E'…' AS redacted
FROM bf_flow ORDER BY bfroid LIMIT 3;

\echo
\echo -- headline: 3 takes, an LLM evaluator picks the punchiest --
SELECT bfroid, rvbbit.headline(observed) AS headline
FROM bf_flow ORDER BY bfroid;

\echo
\echo -- flow audit: one headline call == 3 takes + 1 evaluator sub-call --
SELECT operator,
       jsonb_array_length(sub_calls) AS flow_steps,
       (SELECT count(*) FROM jsonb_array_elements(sub_calls) s
        WHERE s->>'step' = 'evaluator') AS evaluator_calls
FROM rvbbit.receipts
WHERE operator = 'headline'
ORDER BY invocation_at DESC
LIMIT 1;

\echo
\echo ====================================================================
\echo 16. specialist endpoints as operator node primitives
\echo ====================================================================

-- A specialist endpoint (embed / extract / rerank / nli ...) is a node
-- primitive, not a separate kind of callable. Wrap one in a plain operator
-- (no prompts needed) and it is first-class: callable, cached, composable.

-- vectorize: the embed backend exposed as a one-node operator.
SELECT rvbbit.create_operator(
    op_name => 'vectorize',
    op_arg_names => ARRAY['text'],
    op_return_type => 'jsonb',
    op_steps => $j$[{"name":"e","kind":"specialist","specialist":"embed",
                     "inputs":{"text":"{{ inputs.text }}"}}]$j$::jsonb);

\echo
\echo -- embeddings as a node primitive: rvbbit.vectorize(text) --
SELECT bfroid, jsonb_array_length(rvbbit.vectorize(observed)) AS vector_dims
FROM bf_flow ORDER BY bfroid LIMIT 3;

-- place_tags: a chained graph — a GLiNER specialist node feeds a code node.
SELECT rvbbit.create_operator(
    op_name => 'place_tags',
    op_arg_names => ARRAY['text'],
    op_return_type => 'text',
    op_steps => $j$[
       {"name":"x","kind":"specialist","specialist":"extract",
        "inputs":{"text":"{{ inputs.text }}","what":"place names"}},
       {"name":"u","kind":"code","fn":"uppercase",
        "inputs":{"text":"{{ steps.x.output }}"}}
    ]$j$::jsonb);

\echo
\echo -- a chained operator: GLiNER extract node -> code node, one call --
SELECT bfroid, rvbbit.place_tags(observed) AS places
FROM bf_flow ORDER BY bfroid LIMIT 3;

\echo
\echo ====================================================================
\echo 17. heterogeneous takes — an ensemble across different engines
\echo ====================================================================

-- encounter_kind: classify a sighting two ways at once — an LLM node AND
-- the NLI classifier specialist node — then an LLM evaluator picks. One
-- operator, three engines, one cached SQL call.
SELECT rvbbit.create_operator(
    op_name => 'encounter_kind',
    op_arg_names => ARRAY['text'],
    op_return_type => 'text');
SELECT rvbbit.set_operator_takes('encounter_kind', $j${
  "nodes": [
    {"name":"llm","kind":"llm","model":"openai/gpt-5.4-mini",
     "system":"Classify the bigfoot encounter. Reply with ONE word: visual, auditory, physical_evidence, or other.",
     "user":"{{ inputs.text }}"},
    {"name":"nli","kind":"specialist","specialist":"nli_classify",
     "inputs":{"text":"{{ inputs.text }}","candidate_labels":"visual,auditory,physical_evidence,other"}}
  ],
  "reduce":"evaluator",
  "evaluator":{"instructions":"Pick the most accurate encounter type for the text. Reply with only the number."}
}$j$::jsonb);

\echo
\echo -- one operator, two engines (LLM + NLI specialist), evaluator picks --
SELECT bfroid, rvbbit.encounter_kind(observed) AS encounter
FROM bf_flow ORDER BY bfroid LIMIT 4;

\echo
\echo -- flow audit: the ensemble is heterogeneous — llm + nli + evaluator --
SELECT operator,
       jsonb_array_length(sub_calls) AS take_calls,
       (SELECT string_agg(s->>'step', ', ' ORDER BY ord)
        FROM jsonb_array_elements(sub_calls) WITH ORDINALITY AS e(s, ord)) AS engines
FROM rvbbit.receipts
WHERE operator = 'encounter_kind'
ORDER BY invocation_at DESC
LIMIT 1;

\echo
\echo ====================================================================
\echo 18. sql node — an operator that looks up its own data
\echo ====================================================================

-- sighting_brief(bfroid): you pass only an id. A sql node fetches the
-- narrative from bigfoot_sightings; an llm node summarizes it. The whole
-- workflow is the operator — tiny payload in, summary out.
SELECT rvbbit.create_operator(
    op_name => 'sighting_brief',
    op_arg_names => ARRAY['bfroid'],
    op_return_type => 'text',
    op_steps => $j$[
       {"name":"lookup","kind":"sql",
        "sql":"SELECT observed, state, county FROM bigfoot_sightings WHERE bfroid = $1",
        "params":["{{ inputs.bfroid }}"]},
       {"name":"brief","kind":"llm","model":"openai/gpt-5.4-mini",
        "system":"Summarize the bigfoot sighting in ONE vivid sentence. No preamble.",
        "user":"County: {{ steps.lookup.output.county }}, {{ steps.lookup.output.state }}\n\nReport: {{ steps.lookup.output.observed }}"}
    ]$j$::jsonb);

\echo
\echo -- pass only a bfroid; the operator fetches the narrative + summarizes --
SELECT bfroid, rvbbit.sighting_brief(bfroid) AS brief
FROM bf_flow ORDER BY bfroid LIMIT 4;

\echo
\echo -- flow audit: the operator pipeline is sql -> llm --
SELECT operator,
       (SELECT string_agg(s->>'kind', ' -> ' ORDER BY ord)
        FROM jsonb_array_elements(sub_calls) WITH ORDINALITY AS e(s, ord)) AS pipeline
FROM rvbbit.receipts
WHERE operator = 'sighting_brief'
ORDER BY invocation_at DESC
LIMIT 1;

\echo
\echo ====================================================================
\echo End of demo. Re-run "make bigfoot-demo" — all queries are sub-second
\echo from the cache.
\echo ====================================================================
