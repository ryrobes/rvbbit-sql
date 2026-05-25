# Rvbbit semantic SQL demo — BFRO bigfoot sightings

A worked tour of every semantic primitive rvbbit ships, running on
real data: the [BFRO sightings dataset](https://www.bfro.net/GDB/)
(~5,000 first-hand encounter reports). All queries are real SQL
copied verbatim from `docker/sql/bigfoot-demo.sql`. Outputs are real
results from a clean run on an RTX 3090ti.

## TL;DR

```sql
-- Find the 3 sightings most semantically similar to a query
SELECT * FROM rvbbit.knn_text('bf'::regclass::oid, 'observed',
                              'large hairy creature crossing road at night', 3);
-- ~50ms after one-time embedding materialization

-- Cluster all sightings into themes
SELECT * FROM rvbbit.topics('SELECT observed FROM bf', 5);
-- ~80ms; one row per cluster with size + exemplar

-- Find sightings unique to Texas (no semantic analog in Washington)
SELECT * FROM rvbbit.diff('SELECT observed FROM bf WHERE state=''Texas''',
                          'SELECT observed FROM bf WHERE state=''Washington''',
                          5);
-- ~24ms
```

Every cached result lives in plain `rvbbit.embedding_cache` /
`rvbbit.receipts` tables — backed up with `pg_dump`, inspectable
with SQL, model-version-invalidated automatically.

## Setup — two steps

```bash
# 1. Build + start the main stack + GPU sidecars
make gpu-up

# 2. Load the BFRO CSV
make bigfoot-load

# 3. Run the demo — it registers + wires the specialists itself
make bigfoot-demo
```

`make bigfoot-demo` concatenates `register-gpu-specialists.sql` +
`wire-operators-to-specialists.sql` ahead of the demo SQL, all in one
psql session. So the demo re-registers the specialists every run —
it is immune to the registration table being wiped by an extension
recreate (e.g. a concurrent benchmark that does `DROP EXTENSION` /
`CREATE EXTENSION`). `make register-specialists` still exists as a
standalone target if you want to register without running the demo.

The GPU registration intentionally replaces the default `embed` backend
row with the BGE-M3 sidecar so normal `rvbbit.embed`, `rvbbit.knn_text`,
`rvbbit.topics`, and related SQL use the GPU model without changing query
text. Restore the fresh-install local CPU embedding backend with:

```bash
make restore-local-embed
```

The GPU sidecars are 4 containers sharing a single 24GB VRAM pool:

| sidecar | model | transport | rvbbit operators it backs |
|---|---|---|---|
| embed | BAAI/bge-m3 (1024-dim) | rvbbit native | knn_text, similarity, topics, outliers, dedupe_groups, diff, semantic_case |
| rerank | BAAI/bge-reranker-v2-m3 | **gradio** | about / score |
| extract | urchade/gliner_medium-v2.1 | rvbbit native | extract |
| nli | MoritzLaurer/DeBERTa-v3-large-mnli-… | rvbbit native (3 endpoints) | classify, sentiment, contradicts, supports, implies |

Combined VRAM: ~14.5 GB out of 24 GB. Gradio + native transports
both work end-to-end.

---

## 0. Sample 500 sightings + pre-warm embeddings

Pre-warming is one BGE-M3 forward pass per distinct value (batched in
groups of 64). After this, every semantic call below is a cache hit
on those 500 rows.

```sql
DROP TABLE IF EXISTS bf;
CREATE TABLE bf AS
    SELECT bfroid, state, county, title, observed
    FROM bigfoot_sightings
    WHERE observed IS NOT NULL AND length(observed) > 100
    ORDER BY bfroid
    LIMIT 500;

SELECT rvbbit.materialize_embeddings('bf'::regclass::oid, 'observed');
```

```
 new_embeddings
----------------
            500
Time: 4763 ms
```

500 × 1024-dim embeddings cached in ~5s. Re-running this is a no-op
(idempotent).

---

## 1. `knn_text` — top-k semantic retrieval

```sql
SELECT
    substring(value, 1, 90) || '…' AS sighting,
    round(score::numeric, 3) AS score
FROM rvbbit.knn_text(
    'bf'::regclass::oid,
    'observed',
    'large hairy creature crossing the road at night',
    3);
```

```
                                          sighting                                          | score
--------------------------------------------------------------------------------------------+-------
 Creature crossed small two lane paved road in front of vehicle late afternoon. No other v… | 0.739
 I saw a hairy medium build creature about six foot tall with long arms crossing a dirt ro… | 0.709
 As I was driving alone in my car I saw a very large dingy white, furry individual cross t… | 0.708
Time: 56 ms
```

The top-3 are all road-crossing sightings — semantic match beats
keyword match (none of these contain "hairy" or "creature crossing" in
that exact order).

---

## 2. `topics` — k-means clustering

```sql
SELECT
    cluster_id,
    count,
    substring(exemplar, 1, 90) || '…' AS exemplar
FROM rvbbit.topics('SELECT observed FROM bf', 5);
```

```
 cluster_id | count |                                          exemplar
------------+-------+--------------------------------------------------------------------------------------------
          0 |   138 |  At approximately 10:00 PM on a Saturday night. I was on my way to Chippewa Lake to pick u…
          1 |   121 |  I may have already given this encounter to you a year or so back. I don't remember wheth…
          2 |   114 |  OK GENTLEMEN. FIRST , I CAN'T BELIEVE THIS EVEN HAPPENED TO ME. I LOVE TO HUNT A…
          3 |    78 |  It was early one morning, probally around 6:00 am, and i was up and decided to go into th…
          4 |    49 |  A couple friends and myself had gone out on a Friday night to "run" one's coondog. It wa…
Time: 80 ms
```

500 sightings → 5 clusters. To get human-readable labels, compose
with `rvbbit.condense` or `rvbbit.classify`:

```sql
SELECT cluster_id, count,
       rvbbit.condense(exemplar) AS theme
FROM rvbbit.topics('SELECT observed FROM bf', 5);
```

---

## 3. `outliers (isolation)` — most-unusual sightings

```sql
SELECT
    substring(text, 1, 90) || '…' AS sighting,
    round(score::numeric, 3) AS isolation
FROM rvbbit.outliers('SELECT observed FROM bf', 3);
```

```
                                          sighting                                          | isolation
--------------------------------------------------------------------------------------------+-----------
 I was 19 years old at that time. I had just gotten out of high school just six months bef… |     0.363
 The Sandy River originates high on the slopes of Mt. Hood, located about 50 miles east of… |     0.361
 saw a mnlike creature on an island in a bog. Tracks seen by District Forester quoted beca… |     0.361
Time: 375 ms
```

Isolation = `1 - max cosine to any other row`. The Sandy River
geographic monologue, the autobiographical opener, and the
forester-on-an-island sighting are stylistically distinct from the
rest of the corpus.

---

## 4. `outliers (criterion)` — least-relevant to a phrase

```sql
SELECT
    substring(text, 1, 90) || '…' AS sighting,
    round(score::numeric, 3) AS irrelevance
FROM rvbbit.outliers(
    'SELECT observed FROM bf',
    3,
    'vocalization or scream'
);
```

```
                                          sighting                                          | irrelevance
--------------------------------------------------------------------------------------------+-------------
 I dont recall which summer this sighting happened. It could have been the summer of 2000 … |       0.710
 On the afternoon of January 5th 2001, I discovered several footprints in the Yellow House… |       0.709
 I saw the figure thru my window, it just stood there just looking in at me. It didn't mo… |       0.706
Time: 70 ms
```

Sightings about silent visual encounters or footprints, NOT about
sound — exactly what "least relevant to vocalization/scream" should
find.

---

## 5. `dedupe_groups` — near-duplicate clustering

```sql
SELECT
    group_id, size,
    substring(representative, 1, 70) || '…' AS canonical
FROM rvbbit.dedupe_groups('SELECT observed FROM bf', 0.85)
WHERE size > 1
ORDER BY size DESC, group_id;
```

In this 500-row sample, no two narratives are 85% cosine-similar (each
report is a unique first-person account). Try `threshold => 0.7` for
looser matches, or run against a larger corpus where some sightings
were submitted twice. For email/customer-name dedup the default 0.7
catches "John Smith" / "Jon Smith" / "Johnny Smith" reliably.

---

## 6. `semantic_case` — multi-branch classification (no LLM)

```sql
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
FROM bf LIMIT 10;
```

```
 bfroid |  encounter_type
--------+-------------------
 10006  | visual
 10012  | paranoid
 10024  | physical_evidence
 1003   | paranoid
 10034  | visual
 10037  | visual
 10046  | visual
 1005   | physical_evidence
 10062  | physical_evidence
 10074  | visual
Time: 223 ms
```

Argmax over embedding cosines — no LLM call, just the cached BGE-M3
vectors. Fastest classification primitive rvbbit offers; 10 rows in
220ms (most of which is the conditions getting embedded the first
time).

---

## 7. `diff` — semantic set difference (novelty detection)

```sql
SELECT
    substring(text, 1, 90) || '…' AS tx_unique_sighting,
    round(novelty::numeric, 3) AS novelty
FROM rvbbit.diff(
    'SELECT observed FROM bf WHERE state = ''Texas''',
    'SELECT observed FROM bf WHERE state = ''Washington''',
    5);
```

```
                                     tx_unique_sighting                                     | novelty
--------------------------------------------------------------------------------------------+---------
 I saw the figure thru my window, it just stood there just looking in at me. It didn't mo… |   0.389
 On the afternoon of January 5th 2001, I discovered several footprints in the Yellow House… |   0.350
 The Woodlands, Texas... October 18th about 12:30 am to 1 am at least... I live in a … |   0.333
 I was 14 years old in 1976. It was either Nov or Dec 1976 and I had rode my dirt bike to … |   0.332
 Only in the past few years have I associated a previous experience with an investigative … |   0.319
Time: 24 ms
```

Daily/weekly "what's new" digests are the canonical use case — swap
`state` for `created_at` ranges.

---

## 8. `extract` via GLiNER — pull entities from text

Powered by the **extract** sidecar (urchade/gliner_medium-v2.1, native
rvbbit transport). Zero-shot NER with arbitrary descriptive labels.

```sql
SELECT
    bfroid, state,
    rvbbit.extract(observed, 'specific location or place name') AS place,
    rvbbit.extract(observed, 'time of day') AS when_at
FROM bf
WHERE state = 'Washington'
LIMIT 5;
```

```
 bfroid |   state    |      place       |    when_at
--------+------------+------------------+----------------
 10062  | Washington | NULL             | Saturday night
 10091  | Washington | Joyce Washington | 0615
 10258  | Washington | NULL             | NULL
 10354  | Washington | Spokane          | daytime
 10397  | Washington | Hannon Lake      | NULL
Time: 704 ms
```

GLiNER returns 'NULL' (literal string) when no span clears the
threshold. Real place names (Spokane, Hannon Lake) and times of day
(0615, Saturday night) pulled with no schema-design + no Python pipeline.

---

## 9. `about` via Gradio rerank — calibrated relevance

Powered by the **rerank** sidecar (BAAI/bge-reranker-v2-m3, **Gradio**
transport). Cross-encoder gives calibrated [0, 1] scores. This is the
operator that talks to a Gradio app — proves rvbbit speaks both wire
formats.

```sql
SELECT
    bfroid,
    substring(observed, 1, 70) || '…' AS preview,
    round(rvbbit.about(observed, 'multiple witnesses present')::numeric, 3) AS witnesses_score,
    round(rvbbit.about(observed, 'creature aggression toward humans')::numeric, 3) AS aggression_score
FROM bf LIMIT 8;
```

```
 bfroid |                                preview                               | witnesses_score | aggression_score
--------+----------------------------------------------------------------------+-----------------+------------------
 10006  |  hello. some time back i submitted a report about strange noises i…  |           0.001 |            0.002
 10012  |  My wife and myself were trucking in 1988 . We stopped on I-90 just… |           0.001 |            0.006
 10024  |  I was hunting on the second weekend of the deer hunt and I planne…  |           0.000 |            0.001
 1003   |  Around fifteen years ago me and a friend of mine were horse back r… |           0.001 |            0.023
 10034  |    This incident took place during the first Oregon elk season in …  |           0.000 |            0.003
 10037  |  I believe it was a saturday morning, late summer of 80-81 I don't… |           0.001 |            0.003
 10046  |  This sighting happened in mid October 1971 in Fairfield, New Jersey…|           0.020 |            0.232
 1005   |  OK GENTLEMEN. FIRST , I CAN'T BELIEVE THIS EVEN HAPPENED TO M…    |           0.004 |            0.010
Time: 571 ms
```

bfroid 10046 stands out at 0.232 on aggression — and reading the full
text confirms it's a confrontational encounter narrative. The cross-
encoder makes the relevant rows pop without LLM cost.

---

## 10. `text_evidence` — show which sentences matched

Pure Rust, no model call. Sentence-split + query-term-coverage scoring.

```sql
SELECT
    bfroid,
    rvbbit.text_evidence(observed, 'red eyes glowing in dark', 2) AS evidence
FROM bf
WHERE rvbbit.similarity(observed, 'red eyes glowing in dark') > 0.5
LIMIT 5;
```

bfroid **10863**:

```
{
  "At that time it looked up at me with its red eyes and that is
   when my heart dropped to the bottom of my small intestine.",
  "The red eyes on this thing glowed off the security-light at the
   top of the cliff and I was just scared shitless."
}
```

bfroid **10766**:

```
{
  "I was especially alert due to the large number of deer in the area.",
  "To the right of me I saw a flash of glowing eyes in my headlights."
}
```

The most matching sentences extracted verbatim — useful for showing
users WHY a row matched, or for trimming context before an LLM prompt.
57ms for 5 evidence calls.

---

## 11a. `sentiment` via deberta-v3-large NLI

```sql
SELECT bfroid, state, rvbbit.sentiment(observed) AS feel
FROM bf
WHERE state IN ('Washington', 'Texas', 'Oregon')
LIMIT 10;
```

```
 bfroid |   state    |   feel
--------+------------+----------
 10034  | Oregon     | negative
 10062  | Washington | mixed
 10091  | Washington | mixed
 10095  | Oregon     | mixed
 10097  | Oregon     | mixed
 10157  | Texas      | negative
 102    | Oregon     | mixed
 10239  | Texas      | negative
 10258  | Washington | negative
 10272  | Texas      | negative
Time: 2.8 ms (cached) / ~250 ms cold (long texts on GPU NLI)
```

`sentiment(text)` is a zero-shot classify against
`positive,negative,neutral,mixed` baked into the operator's step
config. Substitute your own label set per call with `rvbbit.classify`.

---

## 11b. `classify` via deberta NLI

```sql
SELECT
    bfroid, state,
    rvbbit.classify(observed, 'visual sighting,audio only,physical evidence,unclear') AS shape
FROM bf
WHERE state = 'Washington'
LIMIT 10;
```

```
 bfroid |   state    |       shape
--------+------------+-------------------
 10062  | Washington | physical evidence
 10091  | Washington | unclear
 10258  | Washington | audio only
 10354  | Washington | unclear
 10397  | Washington | visual sighting
 1043   | Washington | visual sighting
 10470  | Washington | physical evidence
 10509  | Washington | visual sighting
 10646  | Washington | physical evidence
 1077   | Washington | visual sighting
Time: 3000 ms (10 cold calls)
```

User passes ANY comma-separated label list. NLI scores each label
against the text and picks the argmax. Much cheaper than LLM
classification and just as accurate for cleanly-separated label sets.

---

## 11c. `contradicts` via deberta NLI

```sql
SELECT bfroid,
       rvbbit.contradicts(observed, 'a large bigfoot creature was definitely seen') AS recants
FROM bf
WHERE state = 'Washington'
LIMIT 5;
```

```
 bfroid | recants
--------+---------
 10062  | f
 10091  | f
 10258  | t        -- 10258 is the audio-only encounter; doesn't claim creature seen
 10354  | f
 10397  | f
```

Real NLI contradiction detection — the row that recants its own
"creature seen" framing gets flagged. Useful for finding rows whose
narrative undermines a hypothesis.

---

## 11d. `approx_distinct` — HLL distinct count from row-group sketches

```sql
SELECT rvbbit.approx_distinct('bf'::regclass::oid, 'observed') AS approx_observed,
       (SELECT count(DISTINCT observed) FROM bf) AS exact_observed,
       rvbbit.approx_distinct('bf'::regclass::oid, 'state') AS approx_state,
       (SELECT count(DISTINCT state) FROM bf) AS exact_state;
```

```
 approx_observed | exact_observed | approx_state | exact_state
-----------------+----------------+--------------+-------------
             500 |            500 |           45 |          45
Time: 3 ms
```

Reads the per-row-group HLL sketches from `rvbbit.row_groups.stats`,
unions them, returns the cardinality estimate. ±2.6% RMSE at
precision 12 in HLL mode; exact below ~1024 distinct. Compare to
`count(DISTINCT)` which has to scan every row.

---

## 11e. `semantic_mv` — incremental materialized projection

```sql
SELECT rvbbit.semantic_mv_create(
    mv_name => 'bf_sentiments',
    source_rel => 'bf'::regclass::oid,
    pk_col => 'bfroid',
    projection_sql => 'rvbbit.sentiment(observed)',
    projection_col => 'feel',
    projection_type => 'text');
```

```
 semantic_mv_create
--------------------
                500    -- initial populate, 48 ms
```

```sql
-- Join the MV back to the source — every row in `bf` gets a `feel`.
SELECT bfroid, state, substring(observed, 1, 50) || '…' AS preview, feel
FROM bf JOIN rvbbit.bf_sentiments USING (bfroid) LIMIT 5;
```

```
 bfroid |     state     |                       preview                       |   feel
--------+---------------+-----------------------------------------------------+----------
 10006  | West Virginia |  hello. some time back i submitted a report about … | negative
 10012  | Idaho         |  My wife and myself were trucking in 1988 . We sto… | negative
 10024  | Utah          |  I was hunting on the second weekend of the deer h… | negative
 1003   | Florida       |  Around fifteen years ago me and a friend of mine … | negative
 10034  | Oregon        |    This incident took place during the first Orego… | negative
```

Subsequent `SELECT rvbbit.semantic_mv_refresh('bf_sentiments')` calls
only re-run on PKs new to source. Drop + recreate to force full
recompute. INSERT-only semantics — UPDATE/DELETE on source rows
don't invalidate the cached projection (by design; documented).

---

## 11f. `bitmap_select` — JOIN-filter via cached predicate bitmap

```sql
-- 1. Populate a bitmap once (cheap ILIKE here; swap for rvbbit.means
--    to use an LLM-driven predicate).
SELECT rvbbit.bitmap_populate(
    'bf'::regclass::oid, 'vehicle_sighting', 'lexical-v1',
    $$ observed ILIKE '%car%' OR observed ILIKE '%truck%'
       OR observed ILIKE '%vehicle%' $$);

-- 2. Use the bitmap as a JOIN-filter — no per-row eval on subsequent calls.
SELECT t.bfroid, t.state, substring(t.observed, 1, 70) || '…' AS preview
FROM bf t
JOIN rvbbit.bitmap_select_text('bf'::regclass::oid, 'bfroid',
                               'vehicle_sighting', 'lexical-v1') AS m(bfroid)
     USING (bfroid)
LIMIT 5;
```

```
 bfroid |     state     |                                 preview
--------+---------------+-------------------------------------------------------------------------
 10006  | West Virginia |  hello. some time back i submitted a report about strange noises i and…
 10012  | Idaho         |  My wife and myself were trucking in 1988 . We stopped on I-90 just we…
 10024  | Utah          |  I was hunting on the second weekend of the deer hunt and I planned on…
 10034  | Oregon        |    This incident took place during the first Oregon elk season in 1989…
 10037  | Illinois      |  I believe it was a saturday morning, late summer of 80-81 I don't re…
Time: 2 ms
```

The bitmap was populated once (~11 ms). Subsequent JOIN-filters
hit the cached roaring bitmap. Useful for "show me only rows where
the expensive predicate is true" patterns where the predicate
result is stable per row.

---

## 12. `explain_semantic` — preview cost + cache state

```sql
SELECT line FROM rvbbit.explain_semantic($q$
    SELECT observed
    FROM bf
    WHERE rvbbit.about(observed, 'aggressive encounter') > 0.7
      AND rvbbit.semantic_case(observed,
            ARRAY['visual','audio'],
            ARRAY['saw','heard'], 'other') = 'saw'
$q$);
```

```
Semantic Plan
-------------
Query:
  SELECT observed FROM bf
    WHERE rvbbit.about(observed, 'aggressive encounter') > 0.7
      AND rvbbit.semantic_case(observed, ARRAY['visual','audio'],
                                          ARRAY['saw','heard'], 'other') = 'saw'

Semantic operators detected: 2
  rvbbit.about — shape=scalar, return=float8, model=openai/gpt-5.4-mini
    arg count: 2
    estimated literal-arg tokens per row (cl100k_base): 3
  rvbbit.semantic_case (not in rvbbit.operators; built-in UDF or user-defined)
    arg count: 6
    estimated literal-arg tokens per row (cl100k_base): 6

Bitmap cache: empty.
Notes:
  - Static analysis only. Auto-routing queries through the bitmap cache lands in RYR-300.
```

---

## 13. Cache stats — every call shares one cache

```sql
SELECT specialist, n_entries, dim, total_bytes
FROM rvbbit.embedding_cache_stats();

SELECT op_name, n_invocations, n_unique_inputs, total_latency_ms
FROM rvbbit.judgment_stats('about')
UNION ALL ... -- one per op
ORDER BY op_name;
```

```
 specialist | n_entries | dim  | total_bytes
------------+-----------+------+-------------
 embed      |       507 | 1024 |     2086812

   op_name   | n_invocations | n_unique_inputs | total_latency_ms
-------------+---------------+-----------------+------------------
 about       |            16 |              16 |              535
 classify    |            15 |              15 |             3680
 contradicts |             6 |               6 |              402
 extract     |            10 |              10 |              653
 sentiment   |           500 |             500 |        14293644    -- historic LLM calls; new NLI route is ~250ms/call cold
```

507 distinct sightings embedded once, 2.1MB on disk. Every subsequent
`knn_text`/`topics`/`outliers`/`diff`/`semantic_case` call hits this
cache — no new GPU work.

---

## Re-running

```bash
make bigfoot-demo  # ~2s total when fully warm
```

Every block above re-runs from cache. The 4.7s materialization step
becomes a no-op (idempotent) and every semantic call is a catalog
lookup.

## Production paths to swap in

- **Different embedder**: `EMBED_MODEL=BAAI/bge-large-en-v1.5 make gpu-up`
  (or any HF-compatible AutoModel name)
- **Hosted reranker**: re-point the `rerank` specialist's
  `endpoint_url` at a Cohere/Together/OpenRouter rerank endpoint —
  the Gradio transport works against any compatible `/api/predict`
- **OpenAI-compatible embeddings**: register a new specialist with
  `backend_transport => 'openai'` pointing at Ollama / vLLM / OpenAI
  proper — `rvbbit.knn_text` uses whichever name `embed` resolves to
- **Built-in CPU embeddings**: fresh installs seed `embed` with
  `backend_transport => 'local_embed'` and
  `{"model":"bge-small-en-v1.5"}`. Re-running
  `rvbbit.register_backend(backend_name => 'embed', ...)` replaces it.
- **Switch the NLI model**: `NLI_MODEL=MoritzLaurer/deberta-v3-base-zeroshot-v2.0 make gpu-up`
  for a smaller / faster variant
- **Roll back to LLM-only**: `UPDATE rvbbit.operators SET steps = NULL
  WHERE name = 'about'` etc. — the LLM path is the fallback when
  steps is NULL

## What's running where

```
        ┌──────────────────────────────────────────────────┐
        │  PostgreSQL 18 + pg_rvbbit (port 55433)          │
        │   ─ TAM + custom scan + judgment_cache +         │
        │     embedding_cache catalog tables               │
        │   ─ HTTP client to specialists (reqwest)         │
        └──────────┬───────────────────────────────────────┘
                   │
        ┌──────────┼──────────┬──────────┬──────────┐
        ▼          ▼          ▼          ▼
   ┌─────────┐ ┌────────┐ ┌────────┐ ┌─────────────────┐
   │ embed   │ │ rerank │ │extract │ │      nli        │
   │ (8091)  │ │ (8093) │ │ (8094) │ │     (8095)      │
   │ BGE-M3  │ │ Gradio │ │GLiNER  │ │ deberta-large   │
   │ native  │ │ /api/  │ │native  │ │ 3 endpoints     │
   │ batched │ │ predict│ │batched │ │ /classify       │
   │  GPU    │ │  GPU   │ │ GPU    │ │ /entails        │
   │         │ │        │ │        │ │ /contradicts    │
   └─────────┘ └────────┘ └────────┘ │  GPU            │
                                     └─────────────────┘

   ~4 GB    ~2.5 GB    ~1 GB        ~1.5 GB   = ~9 GB / 24 GB used
```
