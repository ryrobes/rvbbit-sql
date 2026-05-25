# Provider Catalogs and Maintenance

Rvbbit keeps a local provider catalog so SQL, costs, and UI tooling can answer
two questions without guessing:

- Which provider models are available for the configured credentials?
- Which models have rates good enough for cost estimation?

The catalog is intentionally separate from the existing `rvbbit.model_rates`
compatibility table. `rvbbit.model_rates` remains the simple source used by
cost estimation. The richer tables give UIs and operators more context.

## Tables and Views

`rvbbit.provider_catalog`

One row per provider refresh target. It records `auth_state`, `status`,
`last_refresh`, row counts, and the last error.

`auth_state` values:

- `configured`: usable credentials were detected.
- `public`: provider has a public catalog path and no key was required.
- `missing`: no relevant credentials were detected.
- `unknown`: provider name was not recognized.

`status` values:

- `ok`: refresh completed.
- `skipped`: refresh was skipped because credentials were absent.
- `error`: refresh attempted and failed.
- `never`: provider has not been refreshed yet.

`rvbbit.provider_models`

One row per provider/model pair. Important columns for UIs:

- `provider`
- `model`
- `display_name`
- `family`
- `capabilities`
- `context_window`
- `output_token_limit`
- `available`
- `source`
- `raw`

`rvbbit.model_rate_cards`

Provider-specific rate data. This supports multiple rate kinds later, while
the current first pass primarily writes `rate_kind = 'standard'`.

Important columns:

- `provider`
- `model`
- `rate_kind`
- `input_per_mtok`
- `output_per_mtok`
- `cached_input_per_mtok`
- `cache_write_per_mtok`
- `currency`
- `source`
- `confidence`

`confidence` values:

- `provider`: fetched directly from a provider/model API.
- `seeded`: copied from Rvbbit's bundled rate seeds.
- `manual`: user-supplied override.
- `actual`: actual settled provider bill.
- `unknown`: present but not trusted for automatic costing.

`rvbbit.provider_model_catalog`

Convenience view joining model metadata to rate cards.

## Refresh

Refresh all known providers:

```sql
SELECT * FROM rvbbit.refresh_provider_catalogs();
```

Refresh a subset:

```sql
SELECT * FROM rvbbit.refresh_provider_catalogs('openrouter,gemini');
```

Register a self-hosted model that has no provider model-list API:

```sql
SELECT rvbbit.register_self_hosted_model(
  provider       => 'local-vllm',
  model          => 'nvidia/Gemma-4-31B-IT-NVFP4',
  backend_name   => 'local-vllm',
  display_name   => 'Gemma 4 31B on local vLLM',
  family         => 'gemma',
  capabilities   => '["chat"]'::jsonb,
  context_window => 32768,
  cost_policy    => 'free'
);
```

`cost_policy` is optional and attaches to `backend_name` when supplied:

- `free`: local/internal call, zero provider bill.
- `model_rate`: estimate from supplied rates or `rvbbit.model_rates`. Pass
  `input_per_mtok` and `output_per_mtok` when the model is not already in the
  rate table.
- `unknown`: intentionally unpriced but still visible in audit views.

The backend's own `max_concurrent` remains the per-endpoint concurrency cap
for chat calls. Use it to match a local vLLM worker count or a hosted API rate
limit; `RVBBIT_PROVIDER_MAX_CONCURRENT` is an additional process-wide cap.

To make a registered chat backend the default for single-LLM operators:

```sql
SELECT rvbbit.set_default_provider('local-vllm');
SELECT rvbbit.default_provider();
```

`RVBBIT_DEFAULT_PROVIDER` still overrides the SQL setting when it is set in
the Postgres process environment.

Current provider behavior:

| Provider | Availability source | Rate source |
|---|---|---|
| `openrouter` | `https://openrouter.ai/api/v1/models` | same endpoint, mirrored into `rvbbit.model_rates` |
| `openai` | `https://api.openai.com/v1/models` | bundled/current Rvbbit rate seeds when model ids match |
| `anthropic` | `https://api.anthropic.com/v1/models` | bundled/current Rvbbit rate seeds when model ids match |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/models` | bundled/current Rvbbit rate seeds when model ids match |

Missing provider keys do not raise. They produce `status = 'skipped'`, which
lets setup UIs show exactly what remains unconfigured.

Credentials detected:

- `OPENROUTER_API_KEY`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `GOOGLE_APPLICATION_CREDENTIALS`

Gemini uses `GEMINI_API_KEY` when present. If it is absent and
`GOOGLE_APPLICATION_CREDENTIALS` is present, Rvbbit requests a service-account
OAuth token and uses the Gemini model-list endpoint with bearer auth.

## Summary

```sql
SELECT rvbbit.provider_catalog_summary();
```

This returns provider status, total model counts, rate-card counts, and the
number of available models without a standard rate card.

## Maintenance Tick

```sql
SELECT rvbbit.maintain();
```

This performs bounded, idempotent maintenance:

- flush queued receipts
- backfill cost events
- reconcile delayed OpenRouter costs
- refresh provider catalogs

By default it does not compact storage. To include storage maintenance:

```sql
SELECT rvbbit.maintain(storage_tables => 2);
```

That calls `rvbbit.maintain_storage(...)`, which compacts dirty Rvbbit shadow
heaps and refreshes stale layout variants for a small number of tables.

## pg_cron

Core Rvbbit does not require `pg_cron`. If `pg_cron` is installed and preloaded,
schedule the default jobs with:

```sql
SELECT rvbbit.install_maintenance_jobs();
```

Defaults:

- `rvbbit-maintain`: every 15 minutes
- `rvbbit-storage-maintain`: hourly, up to 2 tables

Custom schedules:

```sql
SELECT rvbbit.install_maintenance_jobs(
    maintenance_schedule => '*/10 * * * *',
    storage_schedule => '15 * * * *',
    storage_tables => 4
);
```

If `pg_cron` is unavailable, the function returns a JSON object with install
hints and the manual fallback:

```sql
SELECT rvbbit.maintain();
```
