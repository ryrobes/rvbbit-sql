# Costs And Receipts

Rvbbit keeps semantic calls auditable in two layers:

- `rvbbit.receipts`: one operator invocation, its input hash, output, total
  token/latency counts, `query_id`, and the full `sub_calls` JSON audit.
- `rvbbit.cost_events`: append-only cost facts for the external calls inside
  receipts. A call can start as `pending` or `estimated` and later receive a
  `settled` event without mutating the original receipt.

For UI implementation details, see
[COSTS_UI_CONTRACT.md](COSTS_UI_CONTRACT.md).

## Cost Ledger

Use the latest cost state per call:

```sql
SELECT *
FROM rvbbit.cost_latest
ORDER BY created_at DESC;
```

Roll up by query:

```sql
SELECT *
FROM rvbbit.query_costs
ORDER BY last_event_at DESC;
```

Roll up by receipt:

```sql
SELECT r.operator, c.*
FROM rvbbit.receipt_costs c
JOIN rvbbit.receipts r USING (receipt_id)
ORDER BY c.last_event_at DESC;
```

Backfill ledger rows for older receipts, or repair a transient cost-event
write failure:

```sql
SELECT rvbbit.backfill_cost_events_from_receipts(10000);
```

Audit coverage:

```sql
SELECT rvbbit.cost_audit_summary();

SELECT *
FROM rvbbit.cost_audit_gaps
ORDER BY invocation_at DESC;
```

`rvbbit.receipt_cost_audit` compares each receipt's chargeable `sub_calls`
(`llm`, `specialist`, `mcp`) with the latest ledger rows. `audit_status`
is one of `ok`, `no_chargeable_sub_calls`, `missing_cost_events`, `pending`,
`stale_pending`, `uncosted`, or `errors`. `stale_pending` means a pending
cost has been waiting more than 15 minutes and should usually trigger a
reconcile job check.

Statuses:

- `pending`: provider has a request/generation id, but final cost has not been
  fetched yet.
- `settled`: provider returned actual cost.
- `estimated`: cost was computed from `rvbbit.model_rates`.
- `uncosted`: Rvbbit saw the call, but no actual or estimated pricing is
  available.
- `free`: reserved for explicit free/local policies.
- `error`: the underlying call errored.

## Cost Policies

Provider calls that return actual cost should use that actual cost. Everything
else can be governed by `rvbbit.cost_policies`.

Specificity order:

1. `mcp_tool`, with target name `server.tool`
2. `backend`, with target name from `rvbbit.backends.name`
3. `model`, with target name from the model id

Policies:

- `free`: records a zero-cost event with status `free`.
- `fixed`: records `fixed_cost_usd` as an estimated cost.
- `model_rate`: estimates from `input_per_mtok` / `output_per_mtok`, or from
  `rvbbit.model_rates` for the supplied `model`.
- `provider_settled`: records `pending` when a provider generation id exists,
  then expects a later reconcile function to append the settled event.
- `unknown`: records `uncosted`.

Examples:

```sql
-- Local sidecar classifier is free from Rvbbit's cost accounting view.
SELECT rvbbit.set_cost_policy(
  target_kind => 'backend',
  target_name => 'sentiment_local',
  policy => 'free',
  notes => 'Runs on local Warren GPU'
);

-- A paid MCP tool with a fixed per-call charge.
SELECT rvbbit.set_cost_policy(
  target_kind => 'mcp_tool',
  target_name => 'vendor.extract_entities',
  policy => 'fixed',
  fixed_cost_usd => 0.0025
);

-- A model/provider without actual-cost settlement.
SELECT rvbbit.set_cost_policy(
  target_kind => 'model',
  target_name => 'example/model-v1',
  policy => 'model_rate',
  input_per_mtok => 0.50,
  output_per_mtok => 1.50
);
```

Direct OpenAI providers use `model_rate` estimation by default. Rvbbit seeds
current OpenAI text model rates such as `gpt-5.4-mini`, `gpt-5.4`, and
`gpt-4o-mini`; users can override them with `rvbbit.set_model_rate(...)` if
their account, region, or service tier differs.

Direct Anthropic providers also use `model_rate` estimation. Rvbbit seeds
current first-party Claude rates such as `claude-haiku-4-5-20251001`,
`claude-sonnet-4-6`, and `claude-opus-4-7`. These rows cover base input and
output token pricing. Prompt-cache read/write pricing, fast mode, and data
residency multipliers should be modeled with explicit cost policies when used.

Direct Gemini providers use the same `model_rate` estimation. Rvbbit seeds
current text rates such as `gemini-2.5-flash-lite`, `gemini-2.5-flash`,
`gemini-2.5-pro`, and current Gemini 3 text models. These rows cover standard
text input and output pricing. Long-context tiers, cache read/write, batch,
flex, priority, image/audio, grounding, and tool-specific SKUs should be
modeled with explicit cost policies when used.

## OpenRouter Settlement

OpenRouter generation costs are settled after the completion. Rvbbit stores
the generation id in `cost_events.provider_generation_id` and can reconcile
later:

```sql
SELECT rvbbit.reconcile_openrouter_costs(100);
```

The function uses the `auth_header_env` from the `openrouter` backend row,
normally `OPENROUTER_API_KEY`, and appends `settled` cost events for pending
generations.

## Delayed Receipt Queue

Postgres does not allow receipt `INSERT`s in all execution contexts, especially
parallel workers. Instead of dropping those receipts, Rvbbit writes them to a
small JSON queue and flushes them from the next safe backend context.

Inspect and flush manually:

```sql
SELECT rvbbit.receipt_queue_pending();
SELECT rvbbit.flush_receipt_queue(1000);
```

Queue location:

- `RVBBIT_AUDIT_QUEUE_DIR`, if set.
- `$PGDATA/rvbbit_audit_queue`, if `PGDATA` is available.
- `/tmp/rvbbit_audit_queue` as a fallback.

Automatic draining is on by default. Disable it with:

```bash
RVBBIT_RECEIPT_QUEUE_AUTODRAIN=0
```

Queued receipts may have `query_id = NULL` if they originated in a context
where Rvbbit could not safely read or create the session query id. They still
land with full input hash, operator, output, token counts, latency, error, and
`sub_calls`.

## Operational Pattern

For a production-ish install, schedule both of these:

```sql
SELECT rvbbit.maintain_cost_audit();
```

The broader maintenance entry point also refreshes provider model/rate
catalogs, which keeps model-picker UIs and cost estimation metadata current:

```sql
SELECT rvbbit.maintain();
```

The expanded form is useful when you want different limits per phase:

```sql
SELECT rvbbit.flush_receipt_queue(10000);
SELECT rvbbit.backfill_cost_events_from_receipts(10000);
SELECT rvbbit.reconcile_openrouter_costs(1000);
```

`pg_cron` or an external job runner is enough. These functions are idempotent
for normal operation: flushing removes successfully inserted queue files,
backfill only targets receipts without cost rows, and OpenRouter
reconciliation only reads currently pending cost requests.

Provider model/rate catalogs live in `rvbbit.provider_catalog`,
`rvbbit.provider_models`, and `rvbbit.model_rate_cards`. See
[PROVIDER_CATALOGS.md](PROVIDER_CATALOGS.md).
