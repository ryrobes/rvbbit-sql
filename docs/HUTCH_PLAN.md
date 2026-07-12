# The Hutch — managed-warren gateway (Phase 1)

*A hutch is where the domesticated rabbits live: enclosed, fed, someone else's
problem. This is the deployable GCP box that fronts every managed capability.*

## STATUS (2026-07-11)

**BUILT + PROVEN E2E (mock upstream):** `crates/rvbbit_hutch` (Rust, axum —
standalone crate like rvbbit_duck), `docker/Dockerfile.rvbbit-hutch` (93MB
image, boots OOTB with dev fixtures), `docker/hutch-compose.yml`. The full
trust spine verified live: key→tenant (Bearer + X-Rvbbit-Token), entitlement
(403 not_entitled), expiry (403 subscription_expired), lanes (3 lanes → 8
concurrent = exactly 3×200 + 5×429 with Retry-After), metering (SQLite WAL
ledger w/ would-be-cost + gated Prometheus /metrics), hot tenant reload
(POST /admin/reload-tenants — the Polar seam, exercised repeatedly).

**REAL-EXTENSION E2E PASSED:** stock pg_rvbbit (bench box) →
`rvbbit.register_backend('hutch_embed', 'http://<hutch>/b/embed/predict',
'rvbbit', ..., 'RVBBIT_ENGINE_TOKEN')` → `SELECT * FROM rvbbit.outliers(...,
'hutch_embed')` → dispatch batched 4 texts into ONE authenticated HTTP call,
ledger row `bench-dev|embed|4|200`. Expired-tenant flip surfaced the human
message verbatim in the SQL error. Zero extension changes.

**Phase 0 recon corrections (better than planned):**
- NO heartbeat problem exists: backends without a warren deployment are
  `serving_status='external'`, `callable=true` by definition in
  `warren_backend_status`. No warren_nodes row, no surrogate, nothing.
- The install surface is `rvbbit.register_backend()` (admin-gated,
  security-03) — not warren_nodes. Client install = one function call per
  backend + key in an env var (`auth_header_env` stores the var NAME).
- Wire contract (specialists/native.rs): POST endpoint_url, Bearer token,
  `{"inputs":[...]}` → `{"outputs":[...]}`; extra response fields ignored →
  hutch echoes model_version in-band + X-Hutch-Model-Version header.
- Embeddings flow via `{"text": ...}` inputs → bare float-array outputs
  (embeddings.rs parse_embedding_value); embedding_cache memoizes per
  (text_hash, specialist) so repeat calls never re-hit the hutch.

**PUBLIC E2E COMPLETE (2026-07-12):** hutch deployed ON the zoo box
(rabbit-zoo-core-template1, us-east5-b, g4-standard-48 Blackwell) via
docker-load + compose at /home/ryanr/hutch/ (network_mode host → reaches
zoo on localhost:8085). Firewall: ONLY tcp:8090 opened (rule
allow-hutch-8090, tag hutch-gateway); zoo 8085 + vllm 8000 stay
internal-only (verified unreachable externally). Stock bench PG18 →
public internet → hutch (auth/lanes/meter) → zoo → arctic-embed on GPU →
`rvbbit.outliers()` real semantic answer in 377ms wall (upstream 23.8ms
for 6 texts, one batched call; ledger row
`bench-dev|embed|6|200|clover-v1/arctic-embed-l-v2.0`).

**Adapters implemented** (config `adapter:` per backend; zoo shapes
captured live): `openai-embeddings` (/v1/embeddings → data[].embedding →
bare arrays), `zoo-sentiment` (/sentiment parallel scores[]/labels[] →
per-item {score,label}), `zoo-rerank` (/rerank; groups {query,text} pairs
by query, scatters scores to positions), `predict` passthrough. All three
verified against the live zoo.

**Zoo box facts:** compose at /home/ryanr/specialist_zoo/ (gateway
lars-specialist-gateway host-net :8085 — models: arctic-embed-l-v2.0,
bge-reranker-v2-m3, twitter-roberta sentiment, deberta NLI 2class+3class,
toxic-bert, xlm-r language, GLiNER extractor, + image_embedder,
transcriber; cuda/fp16). vllm: nvidia/Gemma-4-31B-IT-NVFP4,
--gpu-memory-utilization 0.66 (co-location already encoded!), 108K ctx,
96 seqs, tool parser gemma4; NOTE its entrypoint pip-installs transformers
on EVERY boot (minutes of cold start).

**LLM SURFACE LIVE (2026-07-12):** OpenAI-compatible `/v1/chat/completions`
(model-name routing: public id → entitlement → lane → rewrite to upstream
served name → vllm) + `GET /v1/models` (per-key entitled list). One surface
serves pg_rvbbit's openai_chat transport, agent()/flow llm steps, AND raw
OpenAI SDKs (direct sub-key use is fine — we host open-weights, not
frontier). Token metering from usage block (prompt/completion columns added
to ledger, idempotent ALTER); stream passthrough works, tokens unmetered on
streams (v2: inject stream_options.include_usage, parse tail frame).

E2E proven: `rvbbit.register_backend('hutch_llm',
'http://<hutch>/v1/chat/completions', 'openai_chat', ...)` +
`create_operator(..., op_steps := '[{"kind":"llm","provider":"hutch_llm",
"model":"gemma4","user":"{{q}}"}]')` → `SELECT rvbbit.hutch_ask(...)` →
"Rabbit" in 215ms wall / 65ms on-box. DOUBLE-ENTRY: client receipt
(hutch_ask|gemma4|27/2 tok|hutch_llm) matches vendor meter
(bench-dev|llm:gemma4|27/2 tok|2µUSD) exactly. GOTCHAS: step templates are
`{{arg}}` (double-brace — `{q}` goes through literally); single-LLM
operators use the default provider, pin via one-node steps pipeline.

**Calliope play (banked):** vLLM serves LoRA adapters natively
(--enable-lora), so a RVBBIT-tuned "calliope-1" = Gemma + adapter in the
SAME vllm instance — cheap to serve, versioned as adapter files. Contract
rule: brand + version explicitly (calliope-1 → calliope-2 opt-in with
regression gate + thumbed-row upgrade preview); silent in-place swaps are
what breaks the mental contract, not the branding. Keep Gemma lineage note
in docs (license terms pass-through), brand in marketing.

**POLAR MONEY LOOP CLOSED (2026-07-12):** sandbox org `data-rabbit`
(ee327001-…), products Clover-ML $99/mo (a044d570) + Gemma Lanes x3
$199/mo (ff4df14b), each with a license_keys benefit (`rvb_` prefix;
clover d221c4fb / gemma 18023de8). Test purchase driven headlessly via
playwright (Stripe 4242 card; Radix comboboxes need DOM-click — option
refs sit outside viewport, typeahead buffer too slow across tool calls).
Hutch polar.rs = **validate-on-first-sight**: unknown `rvb_`-prefixed key
→ one org-token call to /v1/license-keys/validate (org token: NEVER send
organization_id in bodies; trailing-slash 307s — use `curl -L`) →
benefit_id mapped to {entitlements, lanes} via config benefit_map ("the
benefit IS the SKU") → in-memory cache w/ revalidate TTL (60s sandbox /
900s default); Polar-down = serve stale; restart = revalidate on first
sight (no persistence). PROVEN E2E over public internet: purchased key →
gemma4 answer billed 35/14 tok / 7µUSD to tenant
rvbbit.testbuyer@gmail.com; same key correctly 403 not_entitled on
clover backends; subscription revoked via API → key dead within TTL
(revoked keys 404 on validate → Expired tombstone keeps the renewal
message + avoids re-asking Polar; fix landed post-test, ships next
deploy). Static tenants.yaml remains for dev fixtures; box compose reads
polar.env (env_file) for POLAR_SANDBOX_TOKEN.

**CLOVER v1 CONTENTS PROVEN (2026-07-12):** the install script IS the
package payload (scratchpad `clover_v1_install.sql`, becomes the catalog
entry verbatim). Core move: **canonical-name rebinds** —
`register_backend('embed', <hutch>/b/embed/predict, ...)` makes every
built-in composite (outliers/dedupe_groups/knn_text/cluster) hutch-backed
with ZERO operator changes, because `resolve_specialist("")` defaults to
the name 'embed'. RULE: rebinding a canonical specialist name is a CACHE
INVALIDATION EVENT (`DELETE FROM rvbbit.embedding_cache WHERE
specialist='embed'` — old local-model vectors share the key, not the
vector space). Wrapper ops: clover_sentiment (specialist step, inputs
{"text":"{{t}}"}), clover_relevance (rerank pair {"query","text"} →
float8). GOTCHA: operator return types are CHECK'd to
bool|text|float8|jsonb ('real' rejected). Verified: relevance 1.000 vs
0.046 discrimination; outliers('' specialist) → GPU; meter attribution
per backend (embed/rerank/sentiment/llm:gemma4) all under one tenant.
CURATION FINDING: twitter-roberta scored "astonishingly fast" NEGATIVE —
sentiment model needs harness vetting before canon (this is what the
acceptance harness is FOR).

**CLOVER v1 FULL SUITE SHIPPED (2026-07-12, scratchpad
`clover_v1_full.sql` — 19 statements = the package):** 9 canonical
backends (embed/sentiment/rerank/nli/nli3/classify/toxicity/language/
extract) + 10 operators: clover_means (rerank≥0.5 bool — LarSQL
semantic_matches heritage incl. its test cases), clover_relevance
(float8), clover_entails (NLI2 entailment≥0.5), clover_contradicts (NLI3
contradiction≥0.5), clover_sentiment (jsonish text), clover_sentiment_
score (float8 [-1,1]), clover_classify (zero-shot top label, csv
labels), clover_toxic (bool), clover_language (ISO code), clover_extract
(GLiNER entities as jsonb). Battery: 8/8 booleans correct (pos+neg
cases), classify/language/extract exact, sentiment ±. GLUE ANSWER
(settled): wire-shape glue = hutch adapters (Rust, 5 new: zoo-nli/
classify/toxicity/language/extract + upstream_params passthrough for
server-side tunables); SEMANTIC glue = code steps in operator defs
(number_gte/json_get/string_eq registry) — shipped as pack DATA. No
python service on the GPU box; the LarSQL "ugly python" evaporated into
those two layers. GOTCHAS: float8 auto-parser is score_0_1 and CLAMPS —
ops with scores outside [0,1] need op_parser:='strip'; operator
re-CREATE may not invalidate its receipt/semantic cache (pack upgrades
must handle — OPEN QUESTION whether input_hash covers steps changes);
admin_token YAML placement (config append ordering bit us once).
Curation flags for the harness: twitter-roberta "astonishingly fast"→
negative; classify margins are thin (returns 0.47 vs 0.17 flat others). baked
seed stays as the offline base; `rvbbit.catalog_refresh()` (explicit,
user-initiated — apt-update semantics) fetches a static hash-pinned
`catalog.json` from rvbbit.ai (existing CDN, no new server) and upserts
`source='remote'` capability rows; entries carry version +
min_extension_version; installs stay local acts; pin-by-default upgrades.
Air-gap = vendor the JSON file. Decouples product releases from extension
releases in both directions. The remote index is the first
antivirus-definitions feed — packs require it anyway.
`catalog_refresh()` = extension work, next release train.

**WAVE A + BAKE-OFF + SEMANTIC TESTS (2026-07-12 pm):**
- Wave A ops shipped: clover_pii (GLiNER PII preset — 4/4 on smoke),
  clover_similar (dual embed + cosine_similarity code fn), clover_moderate
  (toxicity category scores). Suite now 16 operators.
- Semantic Tests data layer (scratchpad clover_wave_a.sql, migration
  candidate): rvbbit.operator_test_runs (append-only, backend_tag regime
  stamp) + rvbbit.run_tests_log(tag). Engine already HAD op_tests +
  run_tests + run_all_tests. 56-test battery populated (49 new via
  clover_tests.sql). Baseline 54/56.
- SENTIMENT BAKE-OFF (runs 1-3 in the table): twitter-roberta-base 15/16
  sentiment (astonish_fast −0.439); clapAI/modernBERT-large-multilingual
  16/16, margins avg .948/min .774 — QUALITY WINNER but needs C compiler
  in zoo container (torch inductor; gcc exec'd in live container =
  ephemeral) → v1.1 canon candidate GATED on image bake;
  cardiffnlp/twitter-xlm-roberta-base-sentiment 16/16, margins .815/.556,
  zero friction → SHIPPED as clover-v1.0 sentiment (persisted in
  specialist_zoo/.env, hutch model_version bumped, cache_policy restored
  to memoize). Known red test: clover_entails subset+ (all→some) —
  deberta-zeroshot NLI weakness, try 3class model for entails (curation
  flag #2). Red tests stay red: they are information.
- LENS "Semantic Tests" window (rvbbit-lens, uncommitted): lib/rvbbit/
  semantic-tests.ts + semantic-tests-window.tsx + shell/types wiring;
  per-operator pass-rate trend bars, runs list w/ regime tags, failures
  drill, Run-battery button w/ tag input. DOM-verified live on dev
  against the real bake-off runs.
- BANKED: roll specialist_zoo repo + hutch into ONE modernized deploy
  (image incl. build-essential or reference_compile=false for
  ModernBERT; GCP disk image as the usual deploy) — end-of-tinkering.

**STOREFRONT LIVE (2026-07-12 night):** the catalog IS the marketing
surface. `rvbbit-docs/public/catalog.json` (canonical; deploys to
https://rvbbit.ai/catalog.json with the next site deploy) carries 3
managed entries in the EXISTING CatalogDoc interchange format: clover-ml
(35 install stmts incl. tests, key_env RVBBIT_CLOVER_KEY, verified 55/56
block), gemma-lanes (RVBBIT_GEMMA_KEY), hare-slots (coming_soon).
Imported through the EXISTING lens import-catalog route →
`rvbbit.upsert_capability_catalog_entry` accepted kind='managed'
unchanged — the package manager was already built. Lens gold treatment
(uncommitted): CapabilityTypeKey 'managed' + --cap-type-managed gold var
(both themes) + card: gold rail, Sparkles icon, verified-battery chip
(tooltip = regime+date+note), price pill → opens checkout, coming-soon
chip. Kind='managed' classify is strict (kind, not tags). VERIFIED live:
48 packs, sources facet shows 'rvbbit.ai 3', and the Clover card shows
REGISTERED·USED + live call stats because the install-state join
recognized the real backends — the store card displays the product
WORKING. GOTCHA: capabilities window doesn't auto-refetch after import
from outside its own import UI — reopen/reload. Hare banked as
capability: gaps = duck sandbox hardening (non-negotiable), router
auto-offload (pressure gate + size floor), tmpfs artifact cache
(latency AND egress); economics ≈ $0.00004/query compute, risk = data
gravity (bucket region vs pool region).

**REMAINS:** caddy/TLS + hostname; Polar webhook for instant revocation
(TTL covers it meanwhile); per-entitlement lane pools (Clover unlaned vs
LLM laned — currently one per-tenant pool); stream usage metering; lens
surfacing of hutch model_version + would-be-cost; rotate bench-token dev
tenant to issued keys; central meter sink for multi-box LB topology;
re-purchase test subs post-revocation (both products) for standing demo
keys; production Polar org when ready (swap api_base + token env).

## Thesis

Sell **managed capabilities** through the existing catalog. A managed
capability is a catalog entry whose backend is our endpoint instead of the
customer's hardware — installed as metadata only (a `warren_nodes` row +
operator defs + optionally seeded cache rows), spoken to over the existing
warren protocol. The gateway is a warren-shaped front — it satisfies the API
contract, therefore it *is* a warren — with auth / entitlement / lanes /
metering middleware in the middle and the resurrected specialist zoo behind
it.

Two structural rules, enforced from day one:

1. **Money and product never touch.** Polar owns the payment + key lifecycle
   (later); the gateway validates keys against a *local* tenant store synced
   by webhook. Never validate against Polar on the hot path. Phase 1 the
   store is a static file — the lookup is one function, swapped later.
2. **Nothing a customer made ever persists on our side.** Metering metadata
   only; no payload logging. This sentence is the sales pitch for the
   compliance-shaped buyers — keep it structurally true, not policy-true.

Product context this unlocks (see memory `managed_capabilities_saas.md`):
Clover-ML (bundled specialist operators, flat sub), the Gemma generalist tier
(lane-priced), the Zoo menu (per-model subs, power users), hare lanes
(concurrent capsule slots), domain packs (seeded-cache + operators, versioned
like antivirus definitions). All of them are backends behind this one front.

## Phase 0 — recon (first hour)

- **Extract the exact warren call contract** from pg_rvbbit: the
  `specialists/mod.rs` call path (request/response JSON per specialist op),
  the `warren_backend_status` view's `callable` predicate (what freshness /
  status gates node selection), and the `auth_config` jsonb shape on
  `warren_nodes` (how the key is presented per-request).
- **Heartbeat strategy.** Warrens PUSH heartbeats via
  `rvbbit.warren_heartbeat(name, status, labels, capacity, inventory,
  version)` over a PG DSN (catalog.rs:4664). A managed warren cannot have
  inbound PG to customer brains. Candidates, pick the simplest that needs no
  extension change: (a) client-side heartbeat surrogate — the install ships a
  pg_cron job that probes the hutch over HTTP (fleet_probe-style) and calls
  `warren_heartbeat` locally on success; (b) an existing warren probe
  function if one exists; (c) a `managed=true` label that relaxes the
  freshness gate (extension change → banked for next release train).
- **Inventory the old zoo.** `../rabbit-lars/specialist_zoo/` —
  docker-compose.yml, bring-up.sh, gateway/main.py (171KB: DynamicBatcher,
  tenant-labeled Prometheus metrics `{route, tenant, cascade_id, status}`,
  request-log store — the multi-tenant metering spine already exists).
  Decide model set v0 = what Clover's operators need: embeddings
  (means/about), cross-encoder rerank, NLI, zero-shot classify, sentiment,
  extract. Refresh model choices once (shop for current best, Blackwell-tuned
  if available), then **pin as canon** — SOTA-as-of-now, frozen; upgrades
  ship as new immutable versions, never in-place swaps (verdict stability).
  Hardware validated previously: full specialist zoo runs on L4 (and mostly
  T4); Gemma does NOT — L4 boxes are zoo-only, Blackwells reserved for LLMs.

## Phase 1 — gateway service (boring FastAPI, on purpose)

Lives at `services/hutch/` (precedent: hare ships from this repo too).
Separate proxy in front of the zoo container — do NOT rewrite zoo main.py;
resist the gravity, strip dead routes later if ever.

Middleware chain, in order:

1. **Key extraction** — `X-Rvbbit-Token` or `Bearer` (GFE lesson from hare:
   support both, prefer X-Rvbbit-Token).
2. **Tenant lookup** — SHA-256 key hash → `tenants.yaml` (Phase 1 static):
   `{tenant_id, key_hash, lanes, entitlements[], status}`. This function is
   the Polar-webhook seam.
3. **Entitlement check** — capability allowlist per tenant.
4. **Lane semaphore** — per-tenant `asyncio.Semaphore(lanes)`; over-cap =
   429 + Retry-After + a JSON error body matching the operator
   graceful-degradation shape (same contract as a down fleet node: query
   still answers client-side, receipt says why).
5. **Proxy to zoo route** — batch passthrough; zoo's DynamicBatcher does the
   fusing.
6. **Meter** — append-only ledger row (SQLite WAL or the zoo's request-log
   store): tenant, capability, route, model+version, units, duration_ms,
   status, would-be à la carte cost (the Claude-Max receipt trick). Plus
   Prometheus counters `{tenant, route, status}` — keep the old zoo metric
   spine.

Every response echoes the **backend model version** — the verdict-stability
breadcrumb that lands in client receipts ("means() answered by clover-v1").

Failure paths are first-class: expired key and saturated lanes are the two
scenarios customers actually experience — both must degrade, not error.

## Phase 2 — the box

- docker-compose stack: `hutch-gateway` + `zoo` (NGC PyTorch base — runs on
  L4 and Blackwell alike) + `caddy` (auto-TLS once a hostname exists, e.g.
  hutch.rvbbit.ai; plain token-over-VPC is fine for the first e2e) +
  prometheus/grafana (configs exist from the LarSQL era boxes).
- GCP: one **L4 spot instance** (g2-standard-8), debian + compose (brain1
  pattern — multi-container, so not create-with-container). Firewall: 443 +
  health only. NVIDIA driver bootstrap: copy the existing warren GPU box
  recipe. Remember the warren GPU harness lesson: verify models actually
  land on GPU (the 13x CPU-silent-fallback bug).
- Cost anchor for later pricing: this box IS the "naked hardware cost of
  doing it worse" — price Clover below it.

## Phase 3 — end-to-end proof (the trust spine)

1. `tenants.yaml` with two test tenants: different lane counts, one expired.
2. Stock rvbbit (bench box or brain1): run the install SQL — `warren_nodes`
   row (base_url = hutch, auth_config carries the key) + heartbeat surrogate
   — then run real semantic ops (`means`/`about`/`classify`) over a real
   table.
3. Verify: correct answers; meter rows carry tenant; per-tenant Prometheus
   series; parallel load (fleet_stress-style) saturates lanes → 429 →
   graceful degradation + receipt reason; expired key → same degradation
   path; model version echo lands in receipts.
4. Measure the marketing number: batched-GPU-via-hutch vs CPU-sidecar-on-the-
   PG-box for a table-scale semantic op.

## Explicitly out of scope (slots exist, nothing built)

Polar webhook sync + signup portal; catalog listing UI for paid entries;
Gemma/vLLM backend (second backend behind the same front); Zoo menu SKUs;
domain packs; any model training (self-hosted only, forever — see rule 2).

## Open questions

- Heartbeat mechanism (Phase 0 decides; extension change banked if needed).
- Public TLS story: hostname + caddy vs Cloud LB.
- Whether the zoo image gets its model set refreshed before or after the
  first e2e (recommend after — prove the pipe with the old models, then
  re-canonize once).
