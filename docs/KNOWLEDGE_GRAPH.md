# Rvbbit Knowledge Graph

Rvbbit's knowledge graph is a SQL-native semantic memory layer. It stores
entities, aliases, edges, and evidence in ordinary Rvbbit catalog tables, and
exposes graph writes/traversals as SQL functions.

The graph is intentionally SQL-first. Facts can be user-asserted, derived by
rules, or extracted by the editable `rvbbit.triples(...)` semantic operator and
then ingested as ordinary rows.

## Tables

| table | purpose |
|---|---|
| `rvbbit.kg_nodes` | Canonical entities/concepts. |
| `rvbbit.kg_aliases` | Alternate surface forms for a node. |
| `rvbbit.kg_edges` | Directed facts between nodes. |
| `rvbbit.kg_evidence` | Provenance/evidence for nodes or edges. |
| `rvbbit.kg_merge_candidates` | Review queue for possible duplicate entities. |
| `rvbbit.kg_node_merges` | Audit log for accepted node merges. |
| `rvbbit.kg_extraction_runs` | Audit table for bulk text-to-triples ingestion jobs. |
| `rvbbit.kg_extraction_errors` | Per-row extraction/ingestion failures for a run. |

All KG tables carry `graph_id`. The default graph is `default`, but most write
and traversal functions accept a final `graph` argument so multiple projects,
tenants, benchmark datasets, or experiments can reuse the same physical tables
without label collisions.

Every node has:

- `graph_id`: logical graph namespace.
- `kind`: normalized type/category, such as `customer`, `issue`, `product`.
- `label`: display label.
- `label_norm`: normalized lookup key.
- `properties`: JSONB attributes.
- `confidence`: confidence in `[0,1]`.

Every edge has:

- `graph_id`
- `subject_node_id`
- `predicate`
- `object_node_id`
- `properties`
- `confidence`

Every evidence row also records `query_id`, matching `rvbbit.receipts.query_id`
and MCP invocation `query_id` when facts are extracted inside a semantic query
pipeline. This is the execution-provenance spine for UI traces and debugging.

## Core Functions

| function | purpose |
|---|---|
| `rvbbit.kg_assert_node(kind, label, ...)` | Create or resolve a node and return `node_id`. |
| `rvbbit.kg_assert_alias(node_id, alias, ...)` | Attach an alternate label to a node. |
| `rvbbit.kg_resolve_node(kind, label, ...)` | Resolve exact alias or fuzzy embedding match. |
| `rvbbit.kg_assert_edge(...)` | Create/update a directed fact and optional evidence. |
| `rvbbit.kg_link_evidence(...)` | Attach provenance to an edge or node. |
| `rvbbit.kg_suggest_merges(...)` | Populate/retrieve pending duplicate-node candidates. |
| `rvbbit.kg_accept_merge(...)` | Accept a candidate and merge the two nodes. |
| `rvbbit.kg_reject_merge(...)` | Reject a candidate so it is not re-suggested. |
| `rvbbit.kg_merge_nodes(...)` | Directly merge two nodes, preserving aliases, edges, and evidence. |
| `rvbbit.kg_context(kind, label, ...)` | Return ranked graph context rows with paths and evidence. |
| `rvbbit.kg_neighbors(kind, label, ...)` | Traverse local graph neighborhood. |
| `rvbbit.kg_paths(...)` | Find short paths between two nodes. |
| `rvbbit.triples(text, focus, opts)` | Editable semantic operator returning strict JSON triples. |
| `rvbbit.triples_rows(text, focus, opts)` | SQL rowset wrapper over `rvbbit.triples`. |
| `rvbbit.kg_ingest_triples(query_sql, ...)` | Ingest any triple-shaped query into the KG. |
| `rvbbit.kg_ingest_table(regclass, pk_col, text_col, ...)` | Extract triples from a table column and record a run/error audit trail. |

## Basic Pattern

Assert nodes:

```sql
SELECT rvbbit.kg_assert_node('customer', 'Acme Corp');
SELECT rvbbit.kg_assert_node('issue', 'late shipment');
```

Use `graph` when you want isolation:

```sql
SELECT rvbbit.kg_assert_node(
  'customer',
  'Acme Corp',
  graph => 'support_demo'
);
```

Assert an edge with evidence:

```sql
SELECT rvbbit.kg_assert_edge(
  'customer',
  'Acme Corp',
  'reported',
  'issue',
  'late shipment',
  confidence => 0.92,
  evidence => '{"text":"Acme reported repeated late shipments in Q4.",
                "source":"support_ticket"}'::jsonb,
  properties => '{"channel":"support"}'::jsonb
);
```

Read the local neighborhood:

```sql
SELECT *
FROM rvbbit.kg_neighbors('customer', 'Acme Corp', max_depth => 2);
```

Read ranked, evidence-bearing context:

```sql
SELECT context_rank,
       score,
       depth,
       predicate,
       to_kind,
       to_label,
       evidence
FROM rvbbit.kg_context(
  'customer',
  'Acme Corp',
  max_depth => 2,
  max_edges => 50,
  direction => 'both',
  include_evidence => true
);
```

Find a path:

```sql
SELECT *
FROM rvbbit.kg_paths(
  'customer', 'Acme Corp',
  'metric', 'retention risk',
  max_depth => 3
);
```

## Context Retrieval

`kg_context` is the main read primitive for graph-backed RAG and UI graph
exploration. It resolves the seed node, walks the graph up to `max_depth`, ranks
the best path to each edge, and aggregates edge/target-node evidence into JSONB.

The result is still regular SQL rows:

- `context_rank`, `score`, `depth`
- traversal-oriented `from_*` and `to_*` node fields
- `edge_direction`: `out` when traversing subject to object, `in` when traversing
  an incoming edge from the seed toward its source
- `edge_properties`, `path_node_ids`, `path_edge_ids`
- `evidence_count`, `evidence`

Use `include_evidence => false` when a UI only needs graph topology and will
fetch evidence lazily.

Ranking is deliberately small for now. `ranking => '{"depth_decay":0.6}'::jsonb`
controls how quickly longer paths lose score. The default is `0.85`.

## Alias And Resolution

Aliases are first-class and exact alias matches always win:

```sql
WITH node AS (
  SELECT rvbbit.kg_assert_node('company', 'OpenAI Incorporated') AS id
)
SELECT rvbbit.kg_assert_alias(id, 'Open AI')
FROM node;

SELECT *
FROM rvbbit.kg_resolve_node('company', 'open ai');
```

When no alias matches, `kg_resolve_node` can use embeddings:

```sql
SELECT *
FROM rvbbit.kg_resolve_node(
  'company',
  'OpenAI Inc.',
  specialist => 'embed',
  match_threshold => 0.92
);
```

`match_threshold => 0.0` disables fuzzy matching. This is useful for tests,
strict imports, and cases where accidental merging would be worse than
duplicates.

## Merge Review

Automated extraction can create near-duplicate entities. Rvbbit keeps identity
repair explicit: generate candidates, accept or reject them, and preserve an
audit trail.

Suggest likely duplicate nodes for one kind:

```sql
SELECT *
FROM rvbbit.kg_suggest_merges('customer', threshold => 0.86, limit_count => 100);
```

Limit review to one graph:

```sql
SELECT *
FROM rvbbit.kg_suggest_merges('customer', 0.86, 100, graph => 'support_demo');
```

Reject a bad candidate:

```sql
SELECT rvbbit.kg_reject_merge(42);
```

Accept a candidate and optionally choose the winner:

```sql
SELECT rvbbit.kg_accept_merge(
  target_candidate_id => 42,
  preferred_winner_node_id => 1001
);
```

Merging is conservative:

- aliases from the loser are moved to the winner;
- edges pointing at the loser are rewired to the winner;
- duplicate edges are collapsed and their evidence is moved to the surviving edge;
- node-level evidence is moved to the winner;
- `kg_node_merges` records the loser label/properties and `query_id`;
- rejected candidates are not re-suggested.

For controlled imports, merge two known nodes directly:

```sql
SELECT rvbbit.kg_merge_nodes(
  winner_node_id => 1001,
  loser_node_id => 1002,
  merge_properties => '{"reviewer":"data-steward"}'::jsonb
);
```

## Evidence

Evidence can be linked during edge assertion:

```sql
SELECT rvbbit.kg_assert_edge(
  'customer', 'Acme Corp',
  'uses',
  'product', 'Rvbbit',
  evidence => '{"text":"Acme deployed Rvbbit for analytics.",
                "source_table":"accounts",
                "source_pk":"42"}'::jsonb
);
```

Or attached directly:

```sql
SELECT rvbbit.kg_link_evidence(
  target_edge_id => 100,
  source_table => 'tickets'::regclass,
  source_pk => '123',
  source_column => 'body',
  evidence_text => 'Acme reported repeated late shipments in Q4.',
  confidence => 0.9
);
```

Evidence is intentionally queryable instead of buried in edge properties:

```sql
SELECT e.edge_id,
       ev.query_id,
       ev.source_table,
       ev.source_pk,
       ev.evidence_text
FROM rvbbit.kg_edges e
JOIN rvbbit.kg_evidence ev ON ev.edge_id = e.edge_id
ORDER BY ev.created_at DESC;
```

To begin a new explicit trace group in an interactive session:

```sql
SELECT rvbbit.reset_query_id();
```

Then any semantic receipts, MCP calls, and KG evidence created in that session
will share `rvbbit.current_query_id()` unless a row supplies its own
`query_id`.

## RAG Pattern

Vectors answer "what text is similar?" Graphs answer "what facts are connected,
and why?" A useful retrieval shape combines both:

```sql
WITH semantic_hits AS (
  SELECT value, score
  FROM rvbbit.knn_text('tickets'::regclass, 'body', 'renewal risk after shipping failures', 20)
),
accounts AS (
  SELECT DISTINCT t.account_name
  FROM semantic_hits h
  JOIN tickets t ON t.body = h.value
)
SELECT a.account_name,
       n.*
FROM accounts a
CROSS JOIN LATERAL rvbbit.kg_context('customer', a.account_name, 2) n;
```

That gives the model both nearby text and structured graph context, with
evidence rows available for attribution.

## Triple Extraction

`rvbbit.triples` is seeded as a semantic operator, not hardcoded. Users can
change its prompt, model, retry plan, or convert it to a specialist/backend
pipeline later.

Call the rowset wrapper for SQL-native use:

```sql
SELECT *
FROM rvbbit.triples_rows(
  'Acme reported repeated late shipments and renewal risk is rising.',
  'customers'
);
```

The row shape is:

```text
subject_kind text
subject text
predicate text
object_kind text
object text
confidence float8
evidence text
properties jsonb
```

Ingest extracted triples into the graph:

```sql
SELECT rvbbit.kg_ingest_triples($$
  SELECT *
  FROM rvbbit.triples_rows(
    'Acme reported repeated late shipments and renewal risk is rising.',
    'customers'
  )
$$);
```

For bulk ingestion, provide any SQL query that returns the triple row shape.
Optional `source_pk`, `source_table`, and `source_column` columns are preserved
as evidence metadata:

```sql
SELECT rvbbit.kg_ingest_triples($$
  SELECT tr.*, t.id::text AS source_pk, 'body'::text AS source_column
  FROM tickets t
  CROSS JOIN LATERAL rvbbit.triples_rows(t.body, 'customers') tr
$$, source_table => 'tickets'::regclass);
```

For a table-oriented ingestion job with audit rows:

```sql
SELECT *
FROM rvbbit.kg_ingest_table(
  source_rel => 'tickets'::regclass,
  pk_col => 'id',
  text_col => 'body',
  focus => 'customer issues, products, dates, and risks',
  graph => 'support_demo',
  limit_rows => 100
);
```

Inspect the run and any row-level failures:

```sql
SELECT *
FROM rvbbit.kg_extraction_runs
WHERE graph_id = 'support_demo'
ORDER BY created_at DESC
LIMIT 5;

SELECT source_pk, error, input_text
FROM rvbbit.kg_extraction_errors
WHERE graph_id = 'support_demo'
ORDER BY created_at DESC;
```

`where_sql` on `kg_ingest_table` is a trusted SQL fragment. It is intended for
admin/batch use, not for passing through raw end-user input.

## UI Builder Guide

This section is the practical contract for an optional KG UI. The UI should
treat the KG as SQL-native data, not as an opaque service. Everything below can
be queried directly through Postgres.

### What The KG Can Power

A useful first UI can include:

- Graph selector: choose `graph_id`, show node/edge/evidence counts.
- Entity search: search nodes by label/kind and open an entity detail page.
- Graph explorer: render nodes/edges around a selected node using
  `kg_context` or `kg_neighbors`.
- Evidence drawer: show source text, `source_table`, `source_pk`,
  `source_column`, `query_id`, confidence, and properties for a node or edge.
- Path finder: ask whether two entities are connected and display the path.
- Extraction runs dashboard: show table ingestion jobs, error rows, and
  inserted triple counts.
- Merge review queue: review likely duplicate nodes and accept/reject merges.
- RAG context preview: show exactly what graph context would be sent to a model.

The UI does not need to know anything about sidecars or LLM providers for graph
navigation. Extraction through `rvbbit.triples` may call an LLM depending on the
operator config; reading the graph is ordinary SQL.

### Stable Identifiers

Use these as stable UI IDs:

- Node: `kg_nodes.node_id`
- Edge: `kg_edges.edge_id`
- Evidence: `kg_evidence.evidence_id`
- Merge candidate: `kg_merge_candidates.candidate_id`
- Extraction run: `kg_extraction_runs.run_id`
- Logical graph: `graph_id`

Display labels should use `label` and `predicate`. The `_norm` columns are
lookup keys and should usually stay hidden.

`properties` columns are arbitrary JSONB. Render them as expandable metadata;
do not hardcode specific keys unless the app owns a graph-specific convention.

### Graph Overview

List available graphs:

```sql
WITH graphs AS (
  SELECT graph_id FROM rvbbit.kg_nodes
  UNION
  SELECT graph_id FROM rvbbit.kg_edges
  UNION
  SELECT graph_id FROM rvbbit.kg_evidence
)
SELECT g.graph_id,
       COALESCE(n.nodes, 0) AS nodes,
       COALESCE(e.edges, 0) AS edges,
       COALESCE(ev.evidence_rows, 0) AS evidence_rows,
       GREATEST(n.updated_at, e.updated_at, ev.updated_at) AS last_activity
FROM graphs g
LEFT JOIN (
  SELECT graph_id, count(*) AS nodes, max(updated_at) AS updated_at
  FROM rvbbit.kg_nodes
  GROUP BY graph_id
) n USING (graph_id)
LEFT JOIN (
  SELECT graph_id, count(*) AS edges, max(updated_at) AS updated_at
  FROM rvbbit.kg_edges
  GROUP BY graph_id
) e USING (graph_id)
LEFT JOIN (
  SELECT graph_id, count(*) AS evidence_rows, max(created_at) AS updated_at
  FROM rvbbit.kg_evidence
  GROUP BY graph_id
) ev USING (graph_id)
ORDER BY last_activity DESC NULLS LAST, g.graph_id;
```

Show shape of one graph:

```sql
SELECT kind, count(*) AS nodes
FROM rvbbit.kg_nodes
WHERE graph_id = $1
GROUP BY kind
ORDER BY nodes DESC, kind;

SELECT predicate, count(*) AS edges, avg(confidence) AS avg_confidence
FROM rvbbit.kg_edges
WHERE graph_id = $1
GROUP BY predicate
ORDER BY edges DESC, predicate;
```

### Entity Search And Detail

Search nodes:

```sql
SELECT node_id, graph_id, kind, label, confidence, properties, updated_at
FROM rvbbit.kg_nodes
WHERE graph_id = $1
  AND ($2::text IS NULL OR kind = rvbbit.kg_normalize_label($2))
  AND ($3::text IS NULL OR label ILIKE '%' || $3 || '%')
ORDER BY
  CASE WHEN label ILIKE $3 || '%' THEN 0 ELSE 1 END,
  confidence DESC,
  updated_at DESC
LIMIT COALESCE($4, 50);
```

Load an entity detail card:

```sql
SELECT n.node_id,
       n.graph_id,
       n.kind,
       n.label,
       n.confidence,
       n.properties,
       n.created_at,
       n.updated_at,
       COALESCE(a.aliases, '[]'::jsonb) AS aliases,
       COALESCE(ev.evidence_count, 0) AS node_evidence_count
FROM rvbbit.kg_nodes n
LEFT JOIN LATERAL (
  SELECT jsonb_agg(
           jsonb_build_object(
             'alias_id', alias_id,
             'alias', alias,
             'confidence', confidence,
             'properties', properties
           )
           ORDER BY confidence DESC, alias
         ) AS aliases
  FROM rvbbit.kg_aliases
  WHERE graph_id = n.graph_id
    AND node_id = n.node_id
) a ON true
LEFT JOIN LATERAL (
  SELECT count(*) AS evidence_count
  FROM rvbbit.kg_evidence
  WHERE graph_id = n.graph_id
    AND node_id = n.node_id
) ev ON true
WHERE n.node_id = $1;
```

### Graph Explorer Payload

For a force-directed graph, `kg_context` is usually the best API because it
already ranks edges and can include evidence. Start with `max_depth = 2`,
`max_edges = 100`, and `include_evidence = false`; fetch evidence lazily when
the user clicks an edge.

```sql
WITH ctx AS (
  SELECT *
  FROM rvbbit.kg_context(
    node_kind => $1,
    node_label => $2,
    max_depth => COALESCE($3, 2),
    max_edges => COALESCE($4, 100),
    direction => COALESCE($5, 'both'),
    include_evidence => false,
    specialist => '',
    match_threshold => 0.0,
    graph => $6,
    ranking => COALESCE($7, '{}'::jsonb)
  )
),
nodes AS (
  SELECT from_node_id AS node_id, from_kind AS kind, from_label AS label
  FROM ctx
  UNION
  SELECT to_node_id, to_kind, to_label
  FROM ctx
),
edges AS (
  SELECT edge_id,
         from_node_id AS source,
         to_node_id AS target,
         predicate,
         edge_direction,
         edge_confidence,
         score,
         depth,
         edge_properties
  FROM ctx
)
SELECT jsonb_build_object(
  'nodes',
  COALESCE((
    SELECT jsonb_agg(
      jsonb_build_object(
        'id', node_id,
        'kind', kind,
        'label', label
      )
      ORDER BY kind, label
    )
    FROM nodes
  ), '[]'::jsonb),
  'edges',
  COALESCE((
    SELECT jsonb_agg(
      jsonb_build_object(
        'id', edge_id,
        'source', source,
        'target', target,
        'predicate', predicate,
        'direction', edge_direction,
        'confidence', edge_confidence,
        'score', score,
        'depth', depth,
        'properties', edge_properties
      )
      ORDER BY score DESC, depth, edge_id
    )
    FROM edges
  ), '[]'::jsonb)
) AS graph;
```

For a simpler table/list view around a node:

```sql
SELECT *
FROM rvbbit.kg_neighbors(
  node_kind => $1,
  node_label => $2,
  max_depth => 1,
  direction => 'both',
  specialist => '',
  match_threshold => 0.0,
  graph => $3
);
```

### Evidence Drawer

Load evidence for an edge:

```sql
SELECT evidence_id,
       query_id,
       source_table::text AS source_table,
       source_pk,
       source_column,
       evidence_text,
       span,
       confidence,
       properties,
       created_at
FROM rvbbit.kg_evidence
WHERE graph_id = $1
  AND edge_id = $2
ORDER BY confidence DESC, evidence_id;
```

Load evidence for a node:

```sql
SELECT evidence_id,
       query_id,
       source_table::text AS source_table,
       source_pk,
       source_column,
       evidence_text,
       span,
       confidence,
       properties,
       created_at
FROM rvbbit.kg_evidence
WHERE graph_id = $1
  AND node_id = $2
ORDER BY confidence DESC, evidence_id;
```

When `source_table` and `source_pk` are present, the UI can offer a “source row”
link. The source row query is application-specific because the primary-key
column name is not stored in evidence; `source_pk` is the preserved value.

### Path Finder

Use `kg_paths` when the user asks how two entities are connected:

```sql
SELECT p.length,
       p.node_ids,
       p.labels,
       (
         SELECT jsonb_agg(
           jsonb_build_object(
             'edge_id', e.edge_id,
             'predicate', e.predicate,
             'confidence', e.confidence
           )
           ORDER BY ord
         )
         FROM unnest(p.edge_ids) WITH ORDINALITY AS path_edges(edge_id, ord)
         JOIN rvbbit.kg_edges e ON e.edge_id = path_edges.edge_id
       ) AS edges
FROM rvbbit.kg_paths(
  subject_kind => $1,
  subject_label => $2,
  object_kind => $3,
  object_label => $4,
  max_depth => COALESCE($5, 3),
  direction => COALESCE($6, 'both'),
  specialist => '',
  match_threshold => 0.0,
  graph => $7
) p;
```

### Extraction Runs Dashboard

List recent ingestion runs:

```sql
SELECT run_id,
       graph_id,
       query_id,
       source_table::text AS source_table,
       source_column,
       focus,
       status,
       rows_seen,
       triples_inserted,
       errors,
       properties,
       created_at,
       finished_at
FROM rvbbit.kg_extraction_runs
WHERE ($1::text IS NULL OR graph_id = $1)
ORDER BY created_at DESC
LIMIT COALESCE($2, 100);
```

Load run errors:

```sql
SELECT error_id,
       source_table::text AS source_table,
       source_pk,
       source_column,
       left(input_text, 500) AS input_preview,
       error,
       properties,
       created_at
FROM rvbbit.kg_extraction_errors
WHERE run_id = $1
ORDER BY error_id;
```

Start an ingestion run from a UI admin action:

```sql
SELECT *
FROM rvbbit.kg_ingest_table(
  source_rel => $1::regclass,
  pk_col => $2,
  text_col => $3,
  focus => COALESCE($4, 'all'),
  graph => COALESCE($5, 'default'),
  limit_rows => $6,
  where_sql => $7,
  opts => COALESCE($8, '{}'::jsonb),
  specialist => '',
  match_threshold => 0.92
);
```

Only expose `where_sql` to trusted users. It is intentionally raw SQL so batch
admins can target a subset of rows.

### Merge Review UI

Generate/list pending candidates:

```sql
SELECT *
FROM rvbbit.kg_suggest_merges(
  node_kind => $1,
  threshold => COALESCE($2, 0.86),
  limit_count => COALESCE($3, 100),
  graph => $4
);
```

Show candidate detail:

```sql
SELECT c.candidate_id,
       c.graph_id,
       c.kind,
       c.score,
       c.method,
       c.reason,
       c.status,
       left_n.node_id AS left_node_id,
       left_n.label AS left_label,
       left_n.properties AS left_properties,
       right_n.node_id AS right_node_id,
       right_n.label AS right_label,
       right_n.properties AS right_properties
FROM rvbbit.kg_merge_candidates c
JOIN rvbbit.kg_nodes left_n ON left_n.node_id = c.left_node_id
JOIN rvbbit.kg_nodes right_n ON right_n.node_id = c.right_node_id
WHERE c.candidate_id = $1;
```

Accept/reject actions:

```sql
SELECT rvbbit.kg_accept_merge(
  target_candidate_id => $1,
  preferred_winner_node_id => $2
);

SELECT rvbbit.kg_reject_merge($1);
```

After accepting a merge, refresh any open graph explorer payload because node
IDs and edges may have been rewired.

### RAG Context Preview

For “what would the model see?” panels, call `kg_context` with evidence:

```sql
SELECT context_rank,
       score,
       depth,
       predicate,
       from_kind,
       from_label,
       to_kind,
       to_label,
       evidence_count,
       evidence,
       path_node_ids,
       path_edge_ids
FROM rvbbit.kg_context(
  node_kind => $1,
  node_label => $2,
  max_depth => COALESCE($3, 2),
  max_edges => COALESCE($4, 50),
  direction => COALESCE($5, 'both'),
  include_evidence => true,
  specialist => '',
  match_threshold => 0.0,
  graph => $6,
  ranking => COALESCE($7, '{}'::jsonb)
);
```

The `evidence` column is a JSONB array. Each item has:

- `evidence_id`
- `target`: `edge`, `to_node`, or `from_node`
- `query_id`
- `source_table`, `source_pk`, `source_column`
- `evidence_text`
- `confidence`
- `properties`

### UI Assumptions To Avoid

- Do not assume one global graph. Always pass and store `graph_id`.
- Do not assume node labels are unique without graph and kind.
- Do not use `label_norm` or `predicate_norm` as display text.
- Do not assume every edge has evidence. User-asserted facts can be evidence-free.
- Do not assume `properties` has a fixed shape.
- Do not run fuzzy resolution from keystroke search by default. Use direct table
  search first; reserve `kg_resolve_node(... match_threshold > 0)` for explicit
  “resolve” actions because it may call embeddings.
- Do not call `DROP EXTENSION ... CASCADE` from UI/admin flows unless the user
  explicitly asks to destroy extension-owned data.

## Bigfoot KG Demo

The deterministic demo builds a graph from `bigfoot_sightings.observed` without
LLM calls:

```sh
make bigfoot-load
make bigfoot-kg-demo
```

It creates facts such as:

- `bf_report -> observed_in_state -> bf_state`
- `bf_report -> observed_in_county -> bf_county`
- `bf_report -> has_clue -> bf_clue`

The clue edges are derived from the long free-form report text and linked back
to `bigfoot_sightings` evidence rows. The demo then shows clue rollups by state,
short paths such as `state -> report -> red eyes`, and
`kg_context(... include_evidence => true)` output suitable for a UI or RAG
context pack.

## Extension Reloads

Use the non-destructive path when iterating on the extension:

```sh
make reload-extension
```

That runs `CREATE EXTENSION IF NOT EXISTS` followed by `ALTER EXTENSION UPDATE`,
so KG, route profile, cache, and benchmark data stay in place. Avoid
`DROP EXTENSION ... CASCADE` unless you intentionally want to remove
extension-owned tables and their contents.

## Future Direction

The next graph slices should build on these primitives:

- Hybrid graph/vector retrieval helpers.
- Optional graph-aware materialized context views for RAG.
