# Rvbbit Diagnostics

Rvbbit exposes SQL-first diagnostic surfaces for install checks, provider
configuration, and UI health panels.

## Quick Checks

```sql
SELECT * FROM rvbbit.doctor(false);
```

Rows use this shape:

| Column | Meaning |
|---|---|
| `area` | Subsystem, such as `core`, `storage`, `routing`, `provider`, `costs`, `mcp`, or `warren`. |
| `name` | Specific check inside the subsystem. |
| `status` | `ok`, `warn`, or `error`. |
| `detail` | JSONB payload with counts, config, and reasons. |

Use `live => true` to allow active backend probes:

```sql
SELECT * FROM rvbbit.doctor(true);
SELECT * FROM rvbbit.provider_doctor(true);
```

Live mode can make provider/model calls. Use it intentionally in setup flows,
release checks, and support sessions.

## Provider Checks

```sql
SELECT * FROM rvbbit.provider_doctor(false);
```

Provider diagnostics validate:

- the SQL default provider exists
- chat backends have auth env vars present when configured
- model/cost coverage exists where Rvbbit can infer a default model
- optional live probes succeed for probeable providers

For OpenAI-compatible local endpoints, set a default model in `backend_opts` so
live probes know which model to call:

```sql
SELECT rvbbit.register_backend(
  backend_name => 'local-vllm',
  backend_endpoint => 'http://vllm:8000/v1/chat/completions',
  backend_transport => 'openai_chat',
  backend_max_concur => 2,
  backend_opts => '{"model":"nvidia/Gemma-4-31B-IT-NVFP4"}'::jsonb
);
```

Then register catalog/cost metadata:

```sql
SELECT rvbbit.register_self_hosted_model(
  provider => 'local-vllm',
  model => 'nvidia/Gemma-4-31B-IT-NVFP4',
  backend_name => 'local-vllm',
  cost_policy => 'free'
);
```

For paid private endpoints, use `cost_policy => 'model_rate'` with rates.

## Environment Presence

```sql
SELECT rvbbit.env_present('OPENROUTER_API_KEY');
```

This returns only a boolean. It never exposes the secret value.

## UI Notes

Recommended panels:

- Overview: grouped `rvbbit.doctor(false)` rows.
- Provider setup: `rvbbit.provider_doctor(false)` with an optional live-probe
  action that runs `rvbbit.provider_doctor(true)`.
- Cost audit: link `costs/receipt_cost_audit` warnings to
  `rvbbit.cost_audit_gaps`.
- Routing: render the `routing/route_status` detail JSON.
- Warren/MCP: render counts from the matching `doctor` rows, then drill into
  `rvbbit.warren_inventory` and `rvbbit.mcp_health`.
