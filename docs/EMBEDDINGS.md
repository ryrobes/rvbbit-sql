# Rvbbit Embeddings

This is the living guide for Rvbbit's embedding layer: local embeddings,
external embedding backends, cache/materialization, and retrieval patterns.

Rvbbit treats embeddings as a first-class SQL primitive. The default backend is
named `embed`, and all embedding SQL resolves through that backend unless you
pass a different specialist name.

## Quick Start

Fresh installs seed a local CPU embedding backend:

```sql
SELECT name, transport, endpoint_url, batch_size, transport_opts
FROM rvbbit.backends
WHERE name = 'embed';
```

Expected default:

```text
name      = embed
transport = local_embed
endpoint  = local://embed
opts      = {"model":"bge-small-en-v1.5"}
```

Embed text:

```sql
SELECT rvbbit.embed('refund request from angry customer');
```

Compare two strings:

```sql
SELECT rvbbit.similarity(
  'customer wants a refund',
  'billing dispute and duplicate charge'
);
```

Retrieve semantically similar rows:

```sql
SELECT *
FROM rvbbit.knn_text(
  'tickets'::regclass,
  'body',
  'late shipment refund request',
  10
);
```

All of the above use backend `embed` implicitly.

## Core Functions

| function | purpose |
|---|---|
| `rvbbit.embed(text [, specialist])` | Return a `real[]` embedding. |
| `rvbbit.similarity(a, b [, specialist])` | Cosine similarity between two texts. |
| `rvbbit.embed_distance(a, b [, specialist])` | `1 - similarity`, useful for ascending order. |
| `rvbbit.cosine_vec(a, b)` | Cosine similarity for already-materialized vectors. |
| `rvbbit.materialize_embeddings(rel, col [, specialist])` | Precompute embeddings for distinct values in a table column. |
| `rvbbit.knn_text(rel, col, query, k [, specialist])` | Top-k semantic retrieval over distinct text values. |
| `rvbbit.embedding_cache_stats()` | Cache observability. |
| `rvbbit.embedding_purge(specialist)` | Remove cached embeddings for one backend. |

## Recommended Pattern

For small or exploratory usage:

```sql
SELECT *
FROM rvbbit.knn_text('docs'::regclass, 'body', 'customer churn risk', 20);
```

For repeated workloads, prewarm first:

```sql
SELECT rvbbit.materialize_embeddings('docs'::regclass, 'body');

SELECT *
FROM rvbbit.knn_text('docs'::regclass, 'body', 'customer churn risk', 20);
```

For row-level enrichment, store the output or join back from `knn_text`:

```sql
WITH hits AS (
  SELECT value, score
  FROM rvbbit.knn_text('docs'::regclass, 'body', 'refund after damaged item', 25)
)
SELECT d.id, d.body, h.score
FROM hits h
JOIN docs d ON d.body = h.value
ORDER BY h.score DESC;
```

`knn_text` currently returns distinct text values. If a column has duplicates,
join back to the source table when you need row identities.

## Backend Selection

The default backend name is `embed`. It is only a row in
`rvbbit.backends`, so users can replace it without changing application SQL.

Use the local CPU default:

```sql
SELECT rvbbit.register_backend(
  backend_name        => 'embed',
  backend_endpoint    => 'local://embed',
  backend_transport   => 'local_embed',
  backend_batch_size  => 128,
  backend_max_concur  => 1,
  backend_timeout_ms  => 120000,
  backend_opts        => '{"model":"bge-small-en-v1.5"}'::jsonb,
  backend_description => 'Default local CPU text embedding backend'
);
SELECT rvbbit.reload_backends();
```

The same reset is available from the dev harness:

```bash
make restore-local-embed
```

Use OpenAI-compatible embeddings instead:

```sql
SELECT rvbbit.register_backend(
  backend_name      => 'embed',
  backend_endpoint  => 'https://api.openai.com/v1/embeddings',
  backend_transport => 'openai',
  backend_auth_env  => 'OPENAI_API_KEY',
  backend_opts      => '{"model":"text-embedding-3-small"}'::jsonb
);
SELECT rvbbit.reload_backends();
```

Use a named alternate backend without replacing the default:

```sql
SELECT rvbbit.register_backend(
  backend_name      => 'embed_large',
  backend_endpoint  => 'local://embed',
  backend_transport => 'local_embed',
  backend_opts      => '{"model":"bge-m3"}'::jsonb
);
SELECT rvbbit.reload_backends();

SELECT rvbbit.embed('hello', 'embed_large');
SELECT *
FROM rvbbit.knn_text('docs'::regclass, 'body', 'contract renewal risk', 10, 'embed_large');
```

## Local Embed Options

`local_embed` runs text embeddings in-process through FastEmbed/ONNX Runtime.

`transport_opts`:

| option | meaning |
|---|---|
| `model` | FastEmbed model name or common alias. Default: `bge-small-en-v1.5`. |
| `cache_dir` | Model cache directory. Defaults to FastEmbed's normal cache path. |
| `max_length` | Token length cap, clamped to `8..8192`. |

Useful model aliases:

- `bge-small-en-v1.5`
- `bge-small-en-v1.5-q`
- `all-MiniLM-L6-v2`
- `all-MiniLM-L6-v2-q`
- `bge-m3`
- `nomic-embed-text-v1.5`

Environment fallbacks:

- `RVBBIT_LOCAL_EMBED_MODEL`
- `RVBBIT_LOCAL_EMBED_CACHE`

The first local call may download model files into the cache directory unless
the Docker image or host has pre-populated that cache.

## Cache Semantics

Embeddings are content-addressed by:

```text
specialist name + text
```

That means:

- Same text through the same backend hits cache.
- Same text through two backend names stores two independent embeddings.
- If you change the model behind an existing backend name, purge its cache.

Inspect cache:

```sql
SELECT *
FROM rvbbit.embedding_cache_stats()
ORDER BY specialist, model;
```

Purge cache for one backend:

```sql
SELECT rvbbit.embedding_purge('embed');
```

## SQL Patterns

Semantic ranking:

```sql
SELECT id,
       body,
       rvbbit.similarity(body, 'contract cancellation risk') AS score
FROM tickets
ORDER BY score DESC
LIMIT 20;
```

For larger tables, prefer `knn_text` plus join-back instead of per-row
`similarity(...)`:

```sql
WITH hits AS (
  SELECT value, score
  FROM rvbbit.knn_text('tickets'::regclass, 'body', 'contract cancellation risk', 20)
)
SELECT t.*, hits.score
FROM hits
JOIN tickets t ON t.body = hits.value
ORDER BY hits.score DESC;
```

Semantic branch/classification without an LLM:

```sql
SELECT id,
       rvbbit.semantic_case(
         body,
         ARRAY['billing problem', 'shipping delay', 'product bug'],
         ARRAY['billing', 'shipping', 'bug'],
         'other',
         0.0
       ) AS bucket
FROM tickets;
```

Find near-duplicate groups:

```sql
SELECT *
FROM rvbbit.dedupe_groups(
  'SELECT body FROM tickets',
  0.82
);
```

Find novelty:

```sql
SELECT *
FROM rvbbit.diff(
  'SELECT body FROM tickets WHERE created_at >= now() - interval ''1 day''',
  'SELECT body FROM tickets WHERE created_at < now() - interval ''1 day''
                         AND created_at >= now() - interval ''8 days''',
  20
);
```

## Operational Notes

- Local embeddings are CPU-only today.
- `local_embed` is in-process. Every Postgres backend can load its own model
  instance. That is good for simple installs, but a future managed local
  embedding worker may be better for high connection counts.
- The default `embed` backend is intentionally replaceable with
  `rvbbit.register_backend(...)`.
- `rvbbit.reload_backends()` refreshes the backend cache in the current
  backend after catalog changes.
- Keep model choice stable for a backend name, or purge/rebuild cached
  embeddings.

## Future Direction

Likely next layers:

- Derived ANN index over `rvbbit.embedding_cache` or table materializations.
- Optional Turbovec-backed vector accelerator.
- Hybrid retrieval: SQL filters -> vector allowlist -> rerank.
- Graph context: vector seed search -> entity/edge expansion -> evidence rows.
- `TRIPLES()`/graph ingestion operators that use embeddings for entity
  resolution but keep extraction LLM-powered.
