# Future Callable Surfaces

Rvbbit currently has several ways to call external or semi-external work from
SQL:

- model backends in `rvbbit.backends`
- semantic operators in `rvbbit.operators`
- MCP tools/resources in `rvbbit.mcp_*`
- plain SQL functions

That split is intentional. The future UX should make them feel like one
callable surface without forcing every execution path through one transport.

## Design Principle

One callable UX, multiple execution harnesses.

The database should expose a unified catalog of callable things while keeping
the optimized runtime paths underneath:

| harness | best at | why it should stay separate |
|---|---|---|
| backend | high-throughput model/data-plane calls | ordered N-in/N-out batching, local Rust transports, direct HTTP, embeddings, rerankers, classifiers |
| MCP | typed tools/resources/actions | discovery, schema-rich tool args, external integrations, side-effectful actions |
| operator | composition | prompt/step control, retries, validators, takes, audit receipts |
| SQL function | native database primitive | planner-visible, joinable, easy to call from normal SQL |

MCP should be one kind of callable, not the universal transport. A local
embedding model or a reranker should not have to pretend to be an MCP server
just to be first-class.

## Unified Catalog

A future `rvbbit.callables` view should normalize the user-facing shape:

```sql
SELECT *
FROM rvbbit.callables
ORDER BY callable_type, name;
```

Possible columns:

| column | meaning |
|---|---|
| `name` | stable callable name, e.g. `embed`, `triples`, `github.search_repositories` |
| `callable_type` | `backend`, `operator`, `mcp_tool`, `sql_function` |
| `description` | human-facing description |
| `input_schema` | JSON Schema or best-effort inferred schema |
| `output_schema` | JSON Schema or row-shape metadata |
| `batchable` | safe/effective to call with N inputs |
| `cacheable` | eligible for content-addressed caching |
| `side_effecting` | may mutate external state |
| `throughput_class` | `hot_path`, `interactive`, `slow`, `unknown` |
| `latency_class` | rough latency hint |
| `target_ref` | catalog pointer to the concrete implementation |

This lets a UI, operator author, or SQL helper discover the whole system
without learning every underlying table first.

## Generic Call Step

Operator flows can later grow a generic `call` step:

```json
{
  "kind": "call",
  "target": "embed",
  "inputs": {"text": "{{ inputs.text }}"}
}
```

The resolver dispatches to the correct harness:

- backend target -> `specialists::predict_*`
- operator target -> `_exec_op_*`
- MCP target -> `mcp_call`
- SQL target -> SPI call

This should be ergonomic sugar over the existing explicit node kinds, not a
replacement. The explicit `llm`, `specialist`, `mcp`, `sql`, and `code` nodes
are still useful when authors need control.

## SQL Sugar

Once the catalog exists, add generic SQL functions:

```sql
SELECT rvbbit.call('embed', '{"text":"hello"}'::jsonb);

SELECT *
FROM rvbbit.call_rows(
  'github.search_repositories',
  '{"query":"rust postgres extension"}'::jsonb
);
```

This is enough for a UI builder and for power users. Parser-level syntax such
as `CALL github.search_repositories(...)` can come later, if it is worth the
rewriter complexity.

## Operational Semantics

The unified surface must preserve important differences:

- Batchable model backends can be prewarmed and parallelized aggressively.
- MCP stdio servers are often serialized behind a gateway and should not be
used as the default embedding/reranking hot path.
- Side-effecting callables should not be retried blindly or run in parallel by
default.
- Cache behavior must be explicit because some calls are pure and some are
actions.
- Every call path should remain auditable in its native log table, with a
future unified observability view on top.

The short version: make the callable model simpler for users, but keep the
runtime honest about performance, purity, and side effects.
