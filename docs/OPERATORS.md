# Rvbbit Semantic Operators — Complete Reference

This document is everything you need to **read, create, mutate, test, and
observe** rvbbit semantic operators — including the flow-control features
(validators, retry, wards, takes). It assumes no prior knowledge of rvbbit.

Everything here is plain SQL. There is no separate API, config file, or
service — operators live in a Postgres table and you manage them with
`SELECT` / `UPDATE` / DDL-helper functions. A UI for building operator
flows is a visual editor over the JSON described below.

---

## Table of contents

1. [What an operator is](#1-what-an-operator-is)
2. [Enumerating operators](#2-enumerating-operators)
3. [Creating an operator](#3-creating-an-operator)
4. [Calling an operator](#4-calling-an-operator)
5. [Mutating an operator](#5-mutating-an-operator)
6. [Deleting an operator](#6-deleting-an-operator)
7. [Flow control: the pipeline](#7-flow-control-the-pipeline)
8. [Validators — the shared primitive](#8-validators--the-shared-primitive)
9. [Retry](#9-retry)
10. [Wards](#10-wards)
11. [Takes](#11-takes)
12. [Templating reference](#12-templating-reference)
13. [Testing operators](#13-testing-operators)
14. [Observability — receipts & stats](#14-observability--receipts--stats)
15. [Caching & invalidation](#15-caching--invalidation)
16. [Node kinds & model backends](#16-node-kinds--model-backends)
17. [Worked example: build a flow operator end to end](#17-worked-example)
18. [Cheat sheet](#18-cheat-sheet)

---

## 1. What an operator is

A **semantic operator** is a named SQL function, backed by an LLM (or a
specialist model), that you call like any other function:

```sql
SELECT rvbbit.sentiment('I love this product!');     -- => 'positive'
SELECT rvbbit.classify('a poem about the sea', 'tech,art,sports');  -- => 'art'
```

Every operator is one row in the table **`rvbbit.operators`**. The row holds
the prompts, the model, the return type, and optional **flow control** (retry
/ wards / takes). Creating an operator also auto-generates the typed SQL
wrapper function (`rvbbit.<name>(...)`) so you can call it.

Operators are **fully user-space**: you create and edit them at runtime with
SQL. Nothing is compiled in. Editing an operator takes effect on the next
call.

A call flows through this pipeline (each stage optional):

```
inputs ─▶ pre-wards ─▶ execute (1 call, or an N-take ensemble) ─▶ retry ─▶ post-wards ─▶ result
```

The `execute` stage is itself a pipeline of one or more **nodes** — each a
primitive of kind `llm`, `specialist`, `python`, or `code`. A plain operator is one
`llm` node; specialists and chained workflows are covered in §16.

---

## 2. Enumerating operators

**List every operator** (use this for a UI list view):

```sql
SELECT name,
       shape,
       return_type,
       description,
       arg_names,
       arg_types,
       retry IS NOT NULL  AS has_retry,
       wards IS NOT NULL  AS has_wards,
       takes IS NOT NULL  AS has_takes,
       steps IS NOT NULL  AS is_multi_step
FROM rvbbit.operators
ORDER BY name;
```

**Read one operator in full** (use this for an edit view):

```sql
SELECT * FROM rvbbit.operators WHERE name = 'classify';
```

### The `rvbbit.operators` columns

| column | type | meaning |
|---|---|---|
| `name` | text (PK) | operator name; also the SQL function name `rvbbit.<name>` |
| `shape` | text | `scalar` (one call per row — the usual case), `aggregate` (one call sees a whole group), `dimension` (one call returns per-input assignments) |
| `arg_names` | text[] | the argument names, e.g. `{text,criterion}`. These become the wrapper's parameters, the keys of the `inputs` JSON, and the template variables |
| `arg_types` | text[] | SQL types of the args, e.g. `{text,text}`. Same length as `arg_names` |
| `return_type` | text | `bool` \| `text` \| `float8` \| `jsonb` |
| `model` | text | default model, e.g. `openai/gpt-5.4-mini` |
| `system_prompt` | text | system message (templated) |
| `user_prompt` | text | user message (templated) |
| `parser` | text | how the raw model text becomes the typed value — see below |
| `max_tokens` | int | output token cap |
| `temperature` | real | nullable; sampling temperature (NULL = provider default) |
| `steps` | jsonb | NULL = single LLM call. Non-NULL = a multi-step pipeline (§16) |
| `retry` | jsonb | NULL = none. Retry plan (§9) |
| `wards` | jsonb | NULL = none. Pre/post validator gates (§10) |
| `takes` | jsonb | NULL = none. Multi-take ensemble plan (§11) |
| `description` | text | human-readable docs |
| `tests` | jsonb | embedded self-tests (§13) |
| `infix_symbol` | text | optional SQL operator symbol for 2-arg ops |
| `created_at` / `updated_at` | timestamptz | `updated_at` auto-bumps on every UPDATE |

**`parser`** turns the model's raw text into the typed return value:

| parser | used for | behavior |
|---|---|---|
| `yes_no` | `bool` | `YES`/`TRUE`/`1` → true, else false |
| `score_0_1` | `float8` | first number found, clamped to `[0,1]` |
| `strip` | `text` | trims whitespace |
| `raw_text` | `text` | passes through unchanged |
| `json` | `jsonb` | parses the output as JSON |

If you don't pass a parser at creation, it is auto-chosen from `return_type`
(`bool`→`yes_no`, `float8`→`score_0_1`, `jsonb`→`json`, `text`→`strip`).

---

## 3. Creating an operator

Use the DDL helper **`rvbbit.create_operator(...)`**. It inserts the catalog
row **and** generates the typed SQL wrapper. Calling it again with the same
`op_name` **upserts** (overwrites) the operator.

```sql
SELECT rvbbit.create_operator(
    op_name        => 'tone',
    op_arg_names   => ARRAY['text'],
    op_return_type => 'text',
    op_system      => 'You classify the tone of a message. Reply with ONE '
                   || 'lowercase word: formal, casual, or hostile.',
    op_user        => E'MESSAGE: {{ text }}\n\nTone:',
    op_shape       => 'scalar',                       -- default
    op_model       => 'openai/gpt-5.4-mini',   -- default
    op_max_tokens  => 8,
    op_temperature => 0.0,
    op_description => 'Classify message tone: formal | casual | hostile.'
);
```

### `create_operator` parameters

| parameter | required | default | notes |
|---|---|---|---|
| `op_name` | yes | — | operator + function name |
| `op_arg_names` | yes | — | `text[]` of argument names |
| `op_return_type` | yes | — | `bool`/`text`/`float8`/`jsonb` |
| `op_system` | no | `''` | system prompt (templated). Required in practice for a single-LLM operator; leave unset for a steps-only operator |
| `op_user` | no | `''` | user prompt (templated). Same — unused when `op_steps` is set |
| `op_shape` | no | `'scalar'` | `scalar`/`aggregate`/`dimension` |
| `op_model` | no | `'openai/gpt-5.4-mini'` | any OpenRouter model id |
| `op_parser` | no | auto from return type | see parser table |
| `op_max_tokens` | no | `256` | output cap |
| `op_temperature` | no | NULL | sampling temperature |
| `op_arg_types` | no | all `text` | `text[]`, same length as `op_arg_names` |
| `op_description` | no | NULL | docs string |
| `op_infix_symbol` | no | NULL | SQL operator symbol (2-arg ops) |
| `op_tests` | no | NULL | embedded tests (§13) |
| `op_steps` | no | NULL | multi-step pipeline (§16) |

> **Flow control is NOT set here.** `create_operator` has no retry/wards/takes
> parameters. Create the operator first, then attach flow control with the
> `set_operator_*` helpers (§9–§11).

---

## 4. Calling an operator

`create_operator` generates a typed wrapper. For `op_arg_names => {text}` and
`return_type => text` you get:

```sql
rvbbit.tone(text, opts jsonb DEFAULT '{}')  RETURNS text
```

So you call it positionally:

```sql
SELECT rvbbit.tone('Please submit the report by EOD.');         -- => 'formal'
SELECT rvbbit.tone(message_body) FROM support_tickets;          -- per row
```

### The `opts` argument

The last parameter is always an optional `opts jsonb`. Recognized keys:

| key | effect |
|---|---|
| `model` | override the model for this call |
| `temperature` | override the temperature for this call |

```sql
SELECT rvbbit.tone('hi there', '{"model":"openai/gpt-4o-mini"}'::jsonb);
```

### What `inputs` means

When you call `rvbbit.classify('a poem', 'tech,art,sports')`, rvbbit builds an
**`inputs`** JSON object keyed by `arg_names`:

```json
{ "text": "a poem", "categories": "tech,art,sports" }
```

`inputs` is what templates and validators see. Remember this — it is central
to flow control.

---

## 5. Mutating an operator

Operators are just table rows. Edit them with `UPDATE`; the change applies on
the next call.

```sql
-- change a prompt
UPDATE rvbbit.operators
SET system_prompt = 'You are a stricter tone classifier. ...'
WHERE name = 'tone';

-- change the default model
UPDATE rvbbit.operators SET model = 'anthropic/claude-sonnet-4.6'
WHERE name = 'tone';
```

> **Cache note:** editing `system_prompt`, `user_prompt`, `model`, or `steps`
> **auto-invalidates** the cache (the cache key includes them). Editing
> `retry`, `wards`, or `takes` does **NOT** — see §15.

You can re-run `create_operator` with the same `op_name` to overwrite an
operator wholesale (it re-generates the wrapper too).

---

## 6. Deleting an operator

```sql
DELETE FROM rvbbit.operators WHERE name = 'tone';
DROP FUNCTION IF EXISTS rvbbit.tone(text, jsonb);   -- drop the wrapper too
```

The wrapper signature is `rvbbit.<name>(<arg_types...>, jsonb)`. For a 2-arg
text operator it is `rvbbit.<name>(text, text, jsonb)`.

---

## 7. Flow control: the pipeline

Flow control wraps the operator's model call. The full per-call pipeline:

```
   ┌────────────┐   ┌─────────────────────────┐   ┌─────────┐   ┌─────────────┐
   │ pre-wards  │──▶│ execute                 │──▶│ retry   │──▶│ post-wards  │──▶ result
   │ gate input │   │ 1 LLM call, or an       │   │ loop    │   │ gate output │
   └────────────┘   │ N-take ensemble (takes) │   └─────────┘   └─────────────┘
                    └─────────────────────────┘
```

- **pre-wards** — validate the inputs before anything runs.
- **execute** — the model call. If `takes` is set, this is an N-attempt
  ensemble reduced to one answer.
- **retry** — if the output fails the retry validator, re-run with feedback.
- **post-wards** — validate the final output.

All four are **optional** and stored as JSON on the operator row. They are
set with three helper functions and all share one primitive: the
**validator**.

---

## 8. Validators — the shared primitive

A **validator** answers one yes/no question about a value. It appears in
retry (`until`), wards (`validator`), and takes (`filter`). It is one of
three shapes:

### 8a. Inline SQL — `{"sql": "<boolean expression>"}`

A Postgres boolean expression. Two variables are bound:

| variable | type | value |
|---|---|---|
| `$output` | text | the operator's raw output string |
| `$inputs` | jsonb | the named inputs object (see §4) |

```json
{ "sql": "$output = ANY(string_to_array($inputs->>'categories', ','))" }
```
```json
{ "sql": "length(btrim($output)) > 0" }
```
```json
{ "sql": "btrim($output) ~ '^[0-9]{4}$'" }
```

This is the normal case. It needs no setup — rvbbit is inside Postgres, so
SQL **is** the validator language. The full deterministic toolbox (regex,
`jsonb` operators, `string_to_array`, math, ranges, …) is available.

### 8b. A Postgres function — `{"function": "schema.fn"}`

For logic that doesn't fit one expression, reference a function:

```json
{ "function": "myschema.check_label" }
```

The function signature must be:

```sql
CREATE FUNCTION myschema.check_label(output text, inputs jsonb)
RETURNS boolean LANGUAGE sql AS $$ SELECT ... $$;
```

It can be written in **any** procedural language Postgres has installed —
`plpgsql`, `plpython3u`, `plv8`, `plperl`. This is the escape hatch: want
arbitrary Python in a validator? `CREATE FUNCTION ... LANGUAGE plpython3u`
and reference it by name. (Same trust boundary as any SQL the user runs.)

### 8c. Shorthand — `"fn_name"`

A bare string is shorthand for `{"function": "fn_name"}`.

### Validator semantics

- Returns **true** → the value passes.
- Returns **false** → the value fails.
- If the validator itself errors (bad SQL, missing function) rvbbit logs a
  warning and treats the value as **passing** — a typo never silently eats
  every result.

### Testing a validator by itself

There is no separate "eval validator" function — a SQL validator is just an
expression, so test it directly by substituting a sample value:

```sql
-- test: {"sql": "$output = ANY(string_to_array($inputs->>'cats', ','))"}
SELECT ('art' = ANY(string_to_array('tech,art,sports', ',')));   -- => true
```

---

## 9. Retry

A retry plan loops the operator: run → check the output against a validator
→ if it fails, re-run with feedback appended to the prompt → repeat, up to
`max_attempts`. The first valid output wins; if every attempt fails, the
last one is returned anyway.

### Set / clear

```sql
SELECT rvbbit.set_operator_retry('clean_year', jsonb_build_object(
    'until',        jsonb_build_object('sql',
        'btrim($output) ~ ''^[0-9]{4}$'''),
    'max_attempts', 3,
    'instructions', 'Respond with ONLY a 4-digit year, nothing else.'
));

SELECT rvbbit.set_operator_retry('clean_year', NULL);   -- remove the plan
```

(You can also `UPDATE rvbbit.operators SET retry = '...'::jsonb` directly;
the helper just adds validation and is friendlier for a UI.)

### The `retry` JSON shape

```json
{
  "until":        { "sql": "btrim($output) ~ '^[0-9]{4}$'" },
  "max_attempts": 3,
  "instructions": "Respond with ONLY a 4-digit year, nothing else."
}
```

| field | required | default | meaning |
|---|---|---|---|
| `until` | yes | — | a [validator](#8-validators--the-shared-primitive). The output must satisfy it |
| `max_attempts` | no | `3` | total attempts, clamped to `1..10` |
| `instructions` | no | — | feedback text appended to the prompt on each retry |

### `instructions` templating

The `instructions` string is rendered (see §12) with extra variables:

- `{{ output }}` — the rejected previous output
- `{{ attempt }}` — the attempt number
- `{{ inputs.<argname> }}` — the operator inputs

```json
"instructions": "Your answer {{ output }} was not one of {{ inputs.categories }}. Pick exactly one."
```

Retry feedback is injected into single-LLM operators. Multi-step operators
re-run without feedback injection.

---

## 10. Wards

A **ward** places a validator as a gate **before** (`pre`) or **after**
(`post`) the operator runs. Unlike retry (which loops), a ward simply
passes, warns, or fails.

### Set / clear

```sql
SELECT rvbbit.set_operator_wards('redact', jsonb_build_object(
    'pre',  jsonb_build_array(
        jsonb_build_object(
            'validator', jsonb_build_object('sql', 'length(btrim($inputs->>''text'')) > 0'),
            'mode',      'blocking')),
    'post', jsonb_build_array(
        jsonb_build_object(
            'validator', jsonb_build_object('sql', '$output !~ ''@'''),
            'mode',      'blocking'))
));

SELECT rvbbit.set_operator_wards('redact', NULL);   -- remove all wards
```

### The `wards` JSON shape

```json
{
  "pre":  [ { "validator": <validator>, "mode": "blocking" } ],
  "post": [ { "validator": <validator>, "mode": "advisory" } ]
}
```

| field | meaning |
|---|---|
| `pre` | array of wards run **before** execute; validate the inputs |
| `post` | array of wards run **after** execute; validate the final output |

Each ward:

| field | required | default | meaning |
|---|---|---|---|
| `validator` | yes | — | a [validator](#8-validators--the-shared-primitive) |
| `mode` | no | `"blocking"` | `blocking` or `advisory` |

### What a ward sees

- **pre-ward** — validates `$inputs`. `$output` is empty (nothing has run).
  Use it for "is the input non-empty / well-formed".
- **post-ward** — validates `$output` (and `$inputs`). Use it for "the
  output must satisfy this contract".

### Modes

| mode | on failure |
|---|---|
| `blocking` | the operator call **fails** — returns the type default and writes an error receipt |
| `advisory` | logs a warning and **continues** — the value is used anyway |

A failed call (blocking ward, all-takes-failed, provider error) returns the
type default (`false` / `''` / `0` / `NULL`) and is **not cached** — so it is
retried on the next call.

---

## 11. Takes

A **takes** plan turns one operator call into an **ensemble** — produce N
candidate answers, then reduce them to one. Two modes:

- **homogeneous** — run the operator `factor` times, optionally across a
  pool of models;
- **heterogeneous** — run an explicit list of `nodes`, each a different
  engine (llm / specialist / python / code). See "Heterogeneous takes" below.

### Set / clear

```sql
SELECT rvbbit.set_operator_takes('headline', jsonb_build_object(
    'factor', 3,
    'reduce', 'evaluator',
    'evaluator', jsonb_build_object(
        'instructions', 'Pick the most vivid, accurate headline.')
));

SELECT rvbbit.set_operator_takes('headline', NULL);   -- remove the plan
```

### The `takes` JSON shape

```json
{
  "factor":  3,
  "models":  ["openai/gpt-5.4-mini", "openai/gpt-4o-mini"],
  "reduce":  "evaluator",
  "filter":  { "sql": "$output <> ''" },
  "evaluator": {
    "model":        "anthropic/claude-sonnet-4.6",
    "instructions": "Pick the best answer. Reply with only its number."
  }
}
```

| field | required | default | meaning |
|---|---|---|---|
| `factor` | homogeneous mode | — | number of attempts, clamped to `1..12` |
| `models` | no | operator's model | (homogeneous) model pool, round-robined across the takes |
| `nodes` | heterogeneous mode | — | a list of node specs — each one is a take; see below |
| `reduce` | no | `"vote"` | how to collapse N answers → 1 (see below) |
| `filter` | no | — | a [validator](#8-validators--the-shared-primitive); takes that fail it are dropped before the reduce. If it would drop all, all are kept |
| `evaluator` | only for `reduce: evaluator` | — | `{model, instructions}` for the LLM judge |

Supply **`factor`** (homogeneous) or **`nodes`** (heterogeneous) — `nodes`
takes precedence if both are present.

### Reducers

| `reduce` | how it picks the winner | cost |
|---|---|---|
| `vote` | majority — the most common output string wins (ties → earliest) | no extra call. Best for classification |
| `first_valid` | the first take that passed the `filter` | no extra call. Cheapest |
| `evaluator` | an LLM judge sees the inputs + all candidates and picks one | +1 model call |

### Heterogeneous takes

A take does not have to be a re-run of the operator. With `nodes`, each
take is its **own engine** — and they can be different kinds. Each node has
the same shape as a `steps` node (§16):

```json
{
  "nodes": [
    { "name": "gliner", "kind": "specialist", "specialist": "extract",
      "inputs": { "text": "{{ inputs.text }}", "what": "place names" } },
    { "name": "llm", "kind": "llm", "model": "openai/gpt-5.4-mini",
      "system": "List the place names in the text.", "user": "{{ inputs.text }}" }
  ],
  "reduce": "evaluator",
  "evaluator": { "instructions": "Pick the most complete, accurate list." }
}
```

This runs a GLiNER specialist **and** an LLM on the same input, and an
evaluator picks the best — an ensemble across model *types*. Use `vote`
when the engines emit a shared normalized label set; use `evaluator` (or
`first_valid` + a `filter`) for free-form outputs that won't string-match.
Each node's call is audited in the receipt's `sub_calls` (§14), so you can
see exactly which engines ran.

### Notes

- For takes to be *useful*, the attempts must differ — use a `models` pool
  **or** set a non-zero `temperature` on the operator. Otherwise all takes
  are identical and the reduce is trivial.
- `factor` × (and `+1` for the evaluator) is the per-row model-call count.
  Takes is for *important* judgments, not casual bulk columns.
- The whole ensemble is one cached result and one receipt; every take shows
  up in the receipt's `sub_calls` audit (§14).

---

## 12. Templating reference

Prompts (`system_prompt`, `user_prompt`, step prompts) and retry
`instructions` are rendered with a small `{{ }}` substitution language.

| reference | resolves to |
|---|---|
| `{{ inputs.text }}` | the `text` argument |
| `{{ text }}` | shorthand — bare names resolve against `inputs` |
| `{{ opts.model }}` | a key from the per-call `opts` |
| `{{ steps.embed.output }}` | output of an earlier step (multi-step only) |
| `{{ output }}` | (retry `instructions` only) the rejected output |
| `{{ attempt }}` | (retry `instructions` only) the attempt number |

This is plain substitution — there are **no** `{% if %}` / `{% for %}`
constructs. Control flow lives in the declarative JSON, not in templates.

---

## 13. Testing operators

### 13a. Just call it

The simplest test is a `SELECT`:

```sql
SELECT rvbbit.tone('Submit the report by 5pm.');     -- expect: formal
```

### 13b. Embedded self-tests

An operator can carry a `tests` JSON array. Each test is a SQL snippet plus
an expectation. Run them with `rvbbit.run_tests(...)`.

```sql
UPDATE rvbbit.operators SET tests = '[
  {"name": "formal_case",
   "sql": "SELECT rvbbit.tone(''Please find the attached invoice.'')",
   "expect": {"type": "exact", "value": "formal"},
   "description": "polite business message"}
]'::jsonb
WHERE name = 'tone';

SELECT * FROM rvbbit.run_tests('tone');     -- per-test pass/fail
SELECT * FROM rvbbit.run_all_tests();       -- every operator that has tests
```

`run_tests` returns `(test_name, passed, actual, expected, description, error)`.

**`expect.type` values:**

| type | passes when |
|---|---|
| `exact` | `actual` equals `expect.value` |
| `contains` | `actual` contains the substring `expect.value` |
| `regex` | `actual` matches `expect.pattern` |
| `min` | `actual::numeric >= expect.value` |
| `max` | `actual::numeric <= expect.value` |
| `not_empty` | `actual` is non-null and non-empty |

---

## 14. Observability — receipts & stats

Every operator call writes one row to **`rvbbit.receipts`** (unless it was a
cache hit). This is the audit trail and the persistent cache in one table.

```sql
SELECT operator, output, error, latency_ms, n_tokens_in, n_tokens_out,
       sub_calls, invocation_at
FROM rvbbit.receipts
WHERE operator = 'headline'
ORDER BY invocation_at DESC
LIMIT 5;
```

### Key `receipts` columns

| column | meaning |
|---|---|
| `operator` | operator name |
| `inputs` | the inputs JSON |
| `output` | final output (NULL if the call errored) |
| `error` | error text, or NULL on success |
| `n_tokens_in` / `n_tokens_out` | totals across all sub-calls |
| `latency_ms` | total wall time |
| `sub_calls` | **JSON array — one entry per underlying model call** |
| `query_id` | groups all receipts from one SQL query |
| `invocation_at` | timestamp |

### `sub_calls` — the flow audit

`sub_calls` is where flow control becomes observable. One `headline` call
with `takes.factor = 3` and `reduce: evaluator` produces **4** sub-calls:

```sql
SELECT jsonb_array_length(sub_calls) AS flow_steps,
       (SELECT count(*) FROM jsonb_array_elements(sub_calls) s
        WHERE s->>'step' = 'evaluator') AS evaluator_calls
FROM rvbbit.receipts
WHERE operator = 'headline'
ORDER BY invocation_at DESC LIMIT 1;
-- flow_steps = 4, evaluator_calls = 1   (3 takes + 1 evaluator)
```

Each `sub_calls` entry has `step`, `kind` (`llm`/`code`/`python`/`specialist`),
`model`, `backend`, `transport`, token counts, latency, error, and provider
ids/cost metadata when the backend exposes them. A retry appends each attempt's
sub-calls, so `sub_calls` length tells you how hard the flow worked.

Costs are append-only in `rvbbit.cost_events`, with convenience views
`rvbbit.cost_latest`, `rvbbit.query_costs`, and `rvbbit.receipt_costs`.
OpenRouter costs can settle after the call via
`rvbbit.reconcile_openrouter_costs()`. See
[`COSTS_AND_RECEIPTS.md`](./COSTS_AND_RECEIPTS.md).

Provider model availability and richer rate-card metadata are maintained by:

```sql
SELECT * FROM rvbbit.refresh_provider_catalogs();
SELECT rvbbit.provider_catalog_summary();
```

See [`PROVIDER_CATALOGS.md`](./PROVIDER_CATALOGS.md).

### Aggregate stats

```sql
SELECT * FROM rvbbit.judgment_stats('headline');
-- (op_name, n_invocations, n_unique_inputs, total_tokens_in,
--  total_tokens_out, total_cost_usd, total_latency_ms, first_at, last_at)
```

---

## 15. Caching & invalidation

Operator results are cached, content-addressed, in three tiers:

1. **L1** — in-memory LRU, per database backend (~microseconds).
2. **L2** — `rvbbit.receipts` table, shared across backends (~1–3 ms).
3. **miss** — the actual model call.

The cache key is a hash of: **operator name + model + `system_prompt` +
`user_prompt` + `steps` + inputs**.

### What invalidates the cache

| you change… | cache invalidates automatically? |
|---|---|
| `system_prompt`, `user_prompt`, `model`, `steps` | **yes** — they are in the key |
| `retry`, `wards`, `takes` | **no** — not in the key |
| inputs (calling with a different value) | yes — different key |

> **Important:** after changing `retry` / `wards` / `takes`, a previously
> seen input still returns its **old cached result**. To force the new flow
> to take effect, purge the operator's cache:
>
> ```sql
> SELECT rvbbit.judgment_purge('headline');   -- deletes L2 receipts + flushes L1
> ```

Other cache controls:

```sql
SELECT rvbbit.flush_cache();        -- clear L1 (in-memory) only
SELECT rvbbit.judgment_purge('op');  -- delete an operator's receipts + flush L1
```

Errored calls (blocking ward, provider failure) are **never cached** — they
re-run every time until they succeed.

---

## 16. Node kinds & model backends

An operator's body is a **pipeline of nodes**. Each node is a primitive of
one `kind`, and the six kinds are peers — each is `inputs → output`:

| `kind` | what it runs | node fields |
|---|---|---|
| `llm` | a language-model call | `model`, `system`, `user`, `max_tokens`, `temperature`, `provider` |
| `specialist` | a call to a registered model backend — embedder, reranker, classifier, extractor, NLI… | `specialist`, `inputs` |
| `python` | a managed CPython handler in a sidecar-created venv | `env`, `handler`, `inputs`, `timeout_ms` |
| `code` | a built-in deterministic function | `fn`, `inputs` |
| `sql` | a parameterized SELECT against the database — lookups, reference data | `sql`, `params` |
| `mcp` | a tool on a registered MCP server (Model Context Protocol) | `server`, `tool`, `inputs` |

A plain "single-LLM operator" (§3) is just the degenerate case: one `llm`
node, expressed through the `system_prompt`/`user_prompt` columns. The
moment you set `steps`, the operator is an explicit pipeline of nodes:

```json
[
  { "name": "draft", "kind": "llm",
    "model": "openai/gpt-5.4-mini",
    "system": "...", "user": "Summarize: {{ inputs.text }}" },
  { "name": "clean", "kind": "code", "fn": "trim",
    "inputs": { "text": "{{ steps.draft.output }}" } }
]
```

A later node reads an earlier one via `{{ steps.<name>.output }}` (§12);
the operator's output is the last node's output. The whole pipeline is
wrapped by flow control (pre-wards → … → post-wards) like any operator.

### A `python` node

A `python` node runs a named handler from `rvbbit.python_handlers` inside a
managed environment from `rvbbit.python_envs`. Users define both with SQL;
the sidecar creates the venv from the package list and executes the handler
over JSON inputs.

```sql
SELECT rvbbit.create_python_env(
  env_name => 'analytics',
  python_version => '3.12',
  requirements => ARRAY['rapidfuzz==3.9.7'],
  runtime_name => 'python_default',
  timeout_ms => 1000
);

SELECT rvbbit.create_python_handler(
  handler_name => 'ticket_score',
  env_name => 'analytics',
  code => $py$
def run(inputs):
    text = inputs["text"].lower()
    return {"is_outage": "outage" in text or "down" in text}
$py$
);
```

Then use it in an operator pipeline:

```json
{ "name": "score", "kind": "python", "env": "analytics",
  "handler": "ticket_score",
  "inputs": { "text": "{{ inputs.body }}" },
  "timeout_ms": 1000 }
```

- **Input** — the rendered `inputs` object is passed to `run(inputs)`.
- **Output** — JSON-serializable Python return values become
  `{{ steps.score.output }}` for downstream nodes.
- **Packages** — package lists live in SQL env rows; no manual server venv
  setup is required. Env/handler hashes are folded into the operator cache
  key, so changing code or packages invalidates cached results.
- **Runtime** — `runtime_name` points at a registered execution runtime such
  as a Warren-deployed row in `rvbbit.python_runtimes`. Direct `endpoint_url`
  overrides still work for local tests, but named runtimes are the preferred
  operational path. Locally, `make python-runtime-up` deploys the built-in
  Python runtime catalog item through Warren and registers `python_default`.
- **Scope** — this is a workflow primitive, not a general PL/Python
  replacement. Python code runs in the sidecar, not in the Postgres backend.

### The three layers

rvbbit has exactly three kinds of object — keep them straight:

| layer | what it is | callable from SQL? |
|---|---|---|
| **Backend** — row in `rvbbit.backends` | *infrastructure*: where a model is served (URL, transport, batching, auth). Registered once, shared. Like a connection / foreign-server registry. Covers **both** specialist endpoints **and** LLM providers. | no — it is plumbing |
| **Node primitive** — a `kind` (`llm` / `specialist` / `python` / `code` / `sql` / `mcp`) | an invocable unit inside an operator's `steps`; an `llm` node names a provider backend, a `specialist` node names a specialist backend, and a `python` node names managed handler code | only inside an operator |
| **Operator** — row in `rvbbit.operators` | a flow over a pipeline of nodes | **yes — the one callable thing** |

A specialist endpoint — and an LLM provider — is **not** its own class of
callable. Each is a backend (layer 1) reached through a node (layer 2). To
call one from SQL you wrap it in an ordinary operator (layer 3).

### A `specialist` node

```json
{ "name": "rr", "kind": "specialist", "specialist": "rerank",
  "inputs": { "text": "{{ inputs.text }}", "criterion": "{{ inputs.query }}" } }
```

| field | meaning |
|---|---|
| `specialist` | the backend name — a row in `rvbbit.backends` |
| `inputs` | a template object mapping operator args → the backend's wire-format input keys |

`inputs` is rendered (§12) per row, sent to the backend; the backend's
response becomes the node's output.

### A `sql` node

A `sql` node runs a **parameterized SELECT** against the database — a
lookup, a reference-data fetch, an enrichment. Its value: the caller
passes a small key, and the operator fetches the rest itself.

```json
{ "name": "lookup", "kind": "sql",
  "sql": "SELECT name, tier, region FROM customers WHERE id = $1",
  "params": ["{{ inputs.customer_id }}"] }
```

| field | meaning |
|---|---|
| `sql` | a SELECT. `$1..$N` are placeholders filled from `params` |
| `params` | a list of templates (§12); each rendered value fills `$1, $2, …` |

- **Output** — the **first row, as a `{column: value}` jsonb object** —
  addressed downstream as `{{ steps.lookup.output.tier }}`. Zero rows →
  null. Need all rows? `SELECT jsonb_agg(...) AS rows FROM …`.
- **Safety** — `params` are bound as quoted literals, so a value from an
  LLM step or user input **cannot inject**. Cast in the query if needed
  (`WHERE id = $1::int`).
- **Read-only** — a `sql` node must be a `SELECT`.
- **Runs on the leader** — SQL needs a Postgres backend, so an operator
  containing a `sql` node executes on the leader, not the worker pool.
  For interactive calls (`SELECT rvbbit.op(id)`) and heterogeneous takes
  this is free. In **bulk** (`… FROM big_table`), such an operator runs
  row-sequentially — for high-volume work, do the lookup as a `JOIN` in
  the outer query instead; the `sql` node is the single-call convenience.

### An `mcp` node

An `mcp` node calls a tool on a registered MCP (Model Context Protocol)
server — the same servers you talk to with `rvbbit.mcp_call(...)`, just
inside an operator pipeline. Composition is the point: classify intent,
fetch via MCP, summarize with an `llm` node, gate with a post-ward.

```json
{ "name": "fetch", "kind": "mcp",
  "server": "github",
  "tool": "search_repositories",
  "inputs": { "query": "{{ inputs.topic }}", "perPage": 5 } }
```

| field | meaning |
|---|---|
| `server` | the registered MCP server name (a row in `rvbbit.mcp_servers`) |
| `tool` | the tool name (a row in `rvbbit.mcp_tools` under that server) |
| `inputs` | a template object — rendered (§12) per row and sent as the tool's arguments |

- **Output** — the tool's text content, **parsed as JSON if possible**.
  So if a tool returns `{"items":[…]}`, downstream nodes read
  `{{ steps.fetch.output.items }}`; if it returns plain text,
  `{{ steps.fetch.output }}` is that string.
- **Tool errors** (the MCP server returned `isError=true`) become a step
  error — the operator's flow control (retry, wards) sees them like any
  other step error.
- **Transport errors** (gateway down, server crashed) bubble up as a
  step error too. In bulk via the warm path, per-call MCP audit rows are
  skipped (pool threads can't SPI); the operator's own `sub_calls`
  receipt still captures every call.
- **Need rows, not a blob?** Use `rvbbit.mcp_rows(server, tool, args)` at
  the SQL level — it auto-unwraps `items` / `results` / `data` arrays and
  returns `SETOF jsonb` so you can `JOIN` / `WHERE` against the result.

### Model backends — `rvbbit.backends`

A backend is a model-serving endpoint rvbbit reaches over HTTP. Register
one — once — with `rvbbit.register_backend`:

```sql
SELECT rvbbit.register_backend(
    backend_name        => 'rerank',
    backend_endpoint    => 'http://rerank:7860/api/predict',
    backend_transport   => 'gradio',        -- rvbbit|gradio|openai|local_embed|stub|openai_chat
    backend_batch_size  => 1,
    backend_max_concur  => 8,
    backend_timeout_ms  => 60000,
    backend_opts        => '{"model":"BAAI/bge-reranker-v2-m3"}'::jsonb,
    backend_description => 'cross-encoder reranker'
);
```

`register_backend` only records the endpoint — it creates nothing
callable. `transport` is `rvbbit` (native batched `POST /predict`),
`gradio`, `openai`, `local_embed` (in-process CPU text embeddings),
`stub` (in-process deterministic, for tests), or `openai_chat` (an LLM
provider — see below).
List backends with `SELECT name, transport, endpoint_url FROM rvbbit.backends`.

Fresh installs seed `embed` as a local CPU embedding backend:

```sql
SELECT name, transport, transport_opts
FROM rvbbit.backends
WHERE name = 'embed';
```

That row is not special. Re-register it when you want OpenAI-compatible
embeddings, a sidecar, or another local model:

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

### LLM providers are backends too

An **LLM provider** — OpenRouter, a local vLLM or Ollama, OpenAI — is just
a backend with a *chat* transport. It lives in the **same**
`rvbbit.backends` registry; there is no separate "providers" table.

rvbbit ships one pre-registered: **`openrouter`**, the default. A fresh
install calls models with zero setup — auth comes from the
`OPENROUTER_API_KEY` env var.

Register another with a **chat transport** — one of three:

| transport | speaks | covers |
|---|---|---|
| `openai_chat` | OpenAI chat-completions | OpenRouter, a local vLLM/Ollama, OpenAI, Together, Groq, Fireworks |
| `anthropic` | Anthropic Messages API | Anthropic direct |
| `gemini` | Google generativelanguage | Gemini direct |

```sql
-- an OpenAI-compatible endpoint (a local vLLM)
SELECT rvbbit.register_backend(
    backend_name      => 'local-vllm',
    backend_endpoint  => 'http://vllm:8000/v1/chat/completions',
    backend_transport => 'openai_chat',
    backend_auth_env  => 'VLLM_API_KEY',      -- optional env var name, not the token
    backend_max_concur => 2,                   -- per-backend in-flight cap
    backend_opts      => '{"model":"nvidia/Gemma-4-31B-IT-NVFP4"}'::jsonb);

-- If no API key is required, omit backend_auth_env. If Rvbbit runs in Docker,
-- do not use localhost unless vLLM is in the Postgres container itself; use a
-- Compose service name such as http://vllm:8000, host.docker.internal, or an
-- internal network address reachable from pg-rvbbit.

-- Optional but recommended: add the self-hosted model to the provider/model
-- catalog and mark the backend as internally free for cost audit purposes.
SELECT rvbbit.register_self_hosted_model(
    provider      => 'local-vllm',
    model         => 'nvidia/Gemma-4-31B-IT-NVFP4',
    backend_name  => 'local-vllm',
    display_name  => 'Gemma 4 31B on local vLLM',
    family        => 'gemma',
    capabilities  => '["chat"]'::jsonb,
    cost_policy   => 'free'
);

-- For a paid/private endpoint, use model_rate instead of free:
SELECT rvbbit.register_self_hosted_model(
    provider        => 'private-openai-compatible',
    model           => 'vendor/custom-chat',
    backend_name    => 'private-chat',
    input_per_mtok  => 2.0,
    output_per_mtok => 4.0,
    cost_policy     => 'model_rate'
);

-- Make it the SQL-configured default for single-LLM operators. The
-- RVBBIT_DEFAULT_PROVIDER environment variable still wins if it is set.
SELECT rvbbit.set_default_provider('local-vllm');
SELECT rvbbit.default_provider();

-- OpenAI direct. GPT-5/o-series chat models expect max_completion_tokens;
-- set the option once on the backend and normal operator max_tokens will be
-- translated by the transport.
SELECT rvbbit.register_backend(
    backend_name      => 'openai',
    backend_endpoint  => 'https://api.openai.com/v1/chat/completions',
    backend_transport => 'openai_chat',
    backend_auth_env  => 'OPENAI_API_KEY',
    backend_opts      => '{"max_tokens_field":"max_completion_tokens"}'::jsonb);

-- Anthropic direct
SELECT rvbbit.register_backend(
    backend_name      => 'anthropic',
    backend_endpoint  => 'https://api.anthropic.com/v1/messages',
    backend_transport => 'anthropic',
    backend_auth_env  => 'ANTHROPIC_API_KEY');

-- Gemini direct with an AI Studio/Gemini API key. The endpoint is a {model}
-- template because Gemini puts the model in the URL path.
SELECT rvbbit.register_backend(
    backend_name      => 'gemini',
    backend_endpoint  => 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
    backend_transport => 'gemini',
    backend_auth_env  => 'GEMINI_API_KEY');

-- Gemini direct with Google ADC / service-account JSON. GOOGLE_APPLICATION_CREDENTIALS
-- may be a normal credentials path visible inside the Postgres container, or
-- compact service-account JSON in local/dev Compose setups.
SELECT rvbbit.register_backend(
    backend_name      => 'gemini-adc',
    backend_endpoint  => 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
    backend_transport => 'gemini',
    backend_auth_env  => 'GOOGLE_APPLICATION_CREDENTIALS',
    backend_opts      => '{"auth_mode":"google_adc"}'::jsonb);
```

The `gemini` transport defaults ADC/OAuth to the
`https://www.googleapis.com/auth/generative-language` scope. Override with
`backend_opts => '{"auth_mode":"google_adc","scope":"..."}'::jsonb` if a
specific Google environment requires a different scope.

An `llm` node picks a provider with the optional **`provider`** field:

```json
{ "kind": "llm", "provider": "local-vllm",
  "model": "nvidia/Gemma-4-31B-IT-NVFP4",
  "system": "...", "user": "..." }
```

- Omit `provider` and the node uses the **default** — `openrouter`, or
  whatever `RVBBIT_DEFAULT_PROVIDER` names.
- `model` is just a parameter; its namespace is the provider's
  (`openai/gpt-5.4-mini` on OpenRouter, a bare model id on vLLM).
- For `backend_probe('local-vllm')`, set `backend_opts.model`; probe uses it
  as the sample chat model. Normal operators can still override `model` per
  step.
- A plain single-LLM operator always uses the default provider. To pin one
  to a specific provider, express it as a one-node `steps` pipeline.
- Mix freely: a heterogeneous-takes ensemble (§11) can run one prompt
  across `openrouter`, Anthropic direct, Gemini, and a local model at once.

> `model` is provider-namespaced: `openai/gpt-5.4-mini` on
> OpenRouter, but `claude-haiku-4-5-20251001` on the `anthropic`
> transport and `gemini-2.5-flash-lite` on `gemini`. Use the id the chosen
> provider expects.

### Making a specialist callable — wrap it in an operator

There is no `create_specialist_operator`. Use the ordinary
`create_operator`; prompts default to `''`, so a steps-only operator needs
no prompt boilerplate:

```sql
SELECT rvbbit.create_operator(
    op_name        => 'rerank',
    op_arg_names   => ARRAY['text', 'query'],
    op_return_type => 'float8',
    op_steps       => '[{"name":"rr","kind":"specialist","specialist":"rerank",
        "inputs":{"text":"{{ inputs.text }}","criterion":"{{ inputs.query }}"}}]'::jsonb
);

SELECT rvbbit.rerank('a bigfoot crossed the road', 'vehicle encounter');
```

`rvbbit.rerank` is now a normal operator — callable, cached, and wrappable
with wards / retry / takes exactly like an LLM operator.

> Flow features compose with specialist operators, but pick what fits: a
> **post-ward** ("the classifier's label is in my allowed set") is very
> useful; `retry` / `takes` add little to a *deterministic* specialist
> (re-running gives the same answer) — they shine on `llm` nodes.

### Embeddings as a node

The embedding model is just a backend; any operator can embed text as a
node:

```sql
SELECT rvbbit.create_operator(
    op_name => 'vectorize', op_arg_names => ARRAY['text'],
    op_return_type => 'jsonb',
    op_steps => '[{"name":"e","kind":"specialist","specialist":"embed",
                   "inputs":{"text":"{{ inputs.text }}"}}]'::jsonb);

SELECT rvbbit.vectorize('some text');     -- => a vector (jsonb array)
```

(rvbbit's vector-search functions — `knn_text`, `topics`, … — keep a
dedicated fast path over the embedding cache; the `specialist` node is for
using embeddings as a building block inside your *own* flows.)

### Chained pipelines — the operator graph

Because nodes can be any kind and each reads earlier nodes, an operator
*is* a workflow graph. Mix `specialist` + `llm` + `code` freely:

```json
[
  { "name": "ents", "kind": "specialist", "specialist": "extract",
    "inputs": { "text": "{{ inputs.text }}", "what": "place names" } },
  { "name": "tidy", "kind": "code", "fn": "uppercase",
    "inputs": { "text": "{{ steps.ents.output }}" } }
]
```

Wrapped in `create_operator(... op_steps => …)`, that whole pipeline — a
GLiNER extraction feeding a code transform — is one cached, flow-wrapped
SQL function. Add `set_operator_wards` / `set_operator_retry` and it is a
governed business-logic workflow callable as `rvbbit.<name>(...)`.

### Built-in `code` functions

`trim`, `lowercase`, `uppercase`, `first_non_empty_line`, `extract_int`,
`validate_one_of`, `char_count`, `json_parse`.

---

## 17. Worked example

Build `urgency(text)` — classify a support message as
`low | medium | high | urgent` — using **every** flow feature.

```sql
-- 1. Create the operator (a plain single-LLM scalar operator).
SELECT rvbbit.create_operator(
    op_name        => 'urgency',
    op_arg_names   => ARRAY['text'],
    op_return_type => 'text',
    op_system      => 'You triage support messages. Reply with ONE lowercase '
                   || 'word: low, medium, high, or urgent.',
    op_user        => E'MESSAGE: {{ text }}\n\nUrgency:',
    op_max_tokens  => 8,
    op_temperature => 0.7,          -- >0 so the 3 takes differ
    op_description => 'Triage a support message into a 4-level urgency.'
);

-- 2. pre-ward: reject empty input before spending any model calls.
SELECT rvbbit.set_operator_wards('urgency', jsonb_build_object(
    'pre', jsonb_build_array(jsonb_build_object(
        'validator', jsonb_build_object('sql',
            'length(btrim($inputs->>''text'')) > 0'),
        'mode', 'blocking'))
));

-- 3. takes: run it 3x and take the majority vote (consensus = reliability).
SELECT rvbbit.set_operator_takes('urgency', jsonb_build_object(
    'factor', 3,
    'reduce', 'vote'
));

-- 4. retry: the answer MUST be one of the four labels.
SELECT rvbbit.set_operator_retry('urgency', jsonb_build_object(
    'until', jsonb_build_object('sql',
        'btrim(lower($output)) IN (''low'',''medium'',''high'',''urgent'')'),
    'max_attempts', 3,
    'instructions',
        'Your answer {{ output }} was not valid. Reply with exactly one of: '
     || 'low, medium, high, urgent.'
));

-- 5. Call it.
SELECT rvbbit.urgency('the production database is down, customers affected');
-- => 'urgent'

-- 6. Observe the flow: one call == 3 takes (vote needs no evaluator).
SELECT jsonb_array_length(sub_calls) AS flow_steps
FROM rvbbit.receipts WHERE operator = 'urgency'
ORDER BY invocation_at DESC LIMIT 1;          -- => 3

-- 7. Changed a flow setting? Purge so it takes effect on seen inputs.
SELECT rvbbit.judgment_purge('urgency');
```

Pipeline for each call: **pre-ward** (non-empty) → **takes** (3× → vote) →
**retry** (must be a valid label) → done.

---

## 18. Cheat sheet

```sql
-- ENUMERATE
SELECT name, shape, return_type, retry IS NOT NULL AS has_retry,
       wards IS NOT NULL AS has_wards, takes IS NOT NULL AS has_takes
FROM rvbbit.operators ORDER BY name;

-- READ ONE
SELECT * FROM rvbbit.operators WHERE name = 'X';

-- CREATE (LLM operator)
SELECT rvbbit.create_operator(op_name=>'X', op_arg_names=>ARRAY['text'],
    op_return_type=>'text', op_system=>'...', op_user=>'{{ text }}');

-- CREATE (specialist operator — wrap a backend as a node, no prompts)
SELECT rvbbit.register_backend(backend_name=>'B', backend_endpoint=>'http://...');
SELECT rvbbit.create_operator(op_name=>'X', op_arg_names=>ARRAY['text'],
    op_return_type=>'jsonb',
    op_steps=>'[{"name":"n","kind":"specialist","specialist":"B",
                 "inputs":{"text":"{{ inputs.text }}"}}]'::jsonb);

-- MUTATE PROMPT (auto-invalidates cache)
UPDATE rvbbit.operators SET system_prompt = '...' WHERE name = 'X';

-- FLOW CONTROL (does NOT auto-invalidate — purge after)
SELECT rvbbit.set_operator_retry('X', '{"until":{"sql":"..."},"max_attempts":3}'::jsonb);
SELECT rvbbit.set_operator_wards('X', '{"post":[{"validator":{"sql":"..."}}]}'::jsonb);
SELECT rvbbit.set_operator_takes('X', '{"factor":3,"reduce":"vote"}'::jsonb);
SELECT rvbbit.set_operator_retry('X', NULL);          -- clear
SELECT rvbbit.judgment_purge('X');                    -- make flow change live

-- CALL
SELECT rvbbit.X('some input');
SELECT rvbbit.X('some input', '{"model":"openai/gpt-4o-mini"}'::jsonb);

-- TEST
SELECT * FROM rvbbit.run_tests('X');

-- OBSERVE
SELECT output, error, sub_calls FROM rvbbit.receipts
WHERE operator = 'X' ORDER BY invocation_at DESC LIMIT 5;
SELECT * FROM rvbbit.judgment_stats('X');

-- DELETE
DELETE FROM rvbbit.operators WHERE name = 'X';
DROP FUNCTION IF EXISTS rvbbit.X(text, jsonb);
```

**The one rule to remember:** editing prompts invalidates the cache
automatically; editing `retry` / `wards` / `takes` does not — follow those
with `SELECT rvbbit.judgment_purge('<operator>')`.
