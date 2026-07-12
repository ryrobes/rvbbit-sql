# Install tiers — additive by design

The docker-compose bootstrap ("from zero") is the turnkey path. But rvbbit
is **additive onto an existing PostgreSQL 18**, and the moving parts are
opt-in. Proven on bone-stock `postgres:18` (Debian trixie), 2026-07-12:
extension-only, no sidecar, no warren, no fleet.

## Tier 0 — Extension only (`CREATE EXTENSION pg_rvbbit`)

The whole install for an existing PG shop:

```bash
# 1. drop three files (install.sh does this from a release tarball)
cp pg_rvbbit.so   "$(pg_config --pkglibdir)/"
cp pg_rvbbit*.control pg_rvbbit*.sql "$(pg_config --sharedir)/extension/"
cp rvbbit-duck    /usr/local/bin/          # ships, but need not RUN at tier 0

# 2. one line of config
ALTER SYSTEM SET shared_preload_libraries = 'pg_rvbbit';   -- (+ pg_cron if wanted)
-- restart

# 3.
CREATE EXTENSION pg_rvbbit;
```

VERIFIED facts (not aspirations):
- **`pg_rvbbit.so` is self-contained** — `ldd` shows zero missing libs on a
  stock PG18 image. No apt dependencies to chase.
- **`shared_preload_libraries = 'pg_rvbbit'` is the only required setting**
  (the planner hooks need it; that's the one invasive-feeling bit, and it's
  standard for any serious PG extension — pg_stat_statements, timescaledb,
  citus all require it).
- **Acceleration works in-process, no sidecar.** `CREATE TABLE ... USING
  rvbbit` + `rvbbit.compact(rel)` builds the columnar layer; scans route to
  `rvbbit_native` (the in-process CustomScan) or embedded DataFusion. A
  20k-row table compacted and aggregated correctly with **zero rvbbit-duck
  processes running**.
- **Semantic SQL works against any REMOTE model provider.** Point a backend
  at a cloud endpoint — the managed Hutch (Clover/Gemma), OpenRouter, or a
  self-host — with `register_backend(name, url, transport, ..., KEY_ENV)`.
  `clover_sentiment(...)` and `outliers(..., 'embed')` returned correct
  results with **no local model of any kind**. The key lives in an env var
  on the PG host; nothing else.
- **Receipts, cache, operators, cubes, metrics** — all extension-resident,
  all work at tier 0.

So tier 0 alone = semantic SQL + columnar acceleration + receipts, using
zero self-hosted infrastructure (cloud models) or a single self-hosted
model box. For a dev/test kick-the-tires, this is the whole thing.

## Tier 1 — local duck sidecar (usually already on)

The duck engine needs NO install action beyond the binary being on disk —
which `install.sh` already drops at tier 0. Detection is presence-based:
`duck_binary()` finds `rvbbit-duck` at `/usr/local/bin`, on `$PATH`, or via
`RVBBIT_DUCK_BIN`; if found, the duck candidates enter the router's
availability set automatically. The extension **forks the binary as a child
process on demand** (no daemon to run, no registration) exactly when the
router picks a duck candidate — i.e. for **large** scans where in-process
native is emission-bound. Small scans stay in-process; you may see zero
`rvbbit-duck` processes until a big query triggers a fork. Absent binary =
the router simply never picks duck. Graceful degradation is a routing
property, not an error.

(The persistent `rvbbit-duck --serve-tcp` server is a *different* mode —
the standalone worker the read fleet connects to. That one you run
deliberately; see the fleet docs.)

## Tier 2 — local warren (needs only Docker)

Self-hosted models (a CPU reranker, the specialist zoo, an LLM). The only
host requirement is **Docker** — the warren agent's installer hard-checks
`command -v docker` and nothing else; the agent is a single Rust binary
(systemd unit or foreground) that needs a DSN to the brain and a work dir.

Deploying a capability to a local warren = the agent renders a compose
project and runs `docker compose up`. The models and their deps (torch,
transformers, …) live **inside the container image**, never installed on
the host:
- capability ships a **prebuilt image** → `docker pull` (fast, no build);
- otherwise → `docker build` locally from the capability's context (first
  time is slower; deps are still sealed in the Dockerfile).

Removes the cloud dependency for semantic ops — models answer on your own
metal. The "in the woods" path: you own the weights and the drift. Your
box stays clean; the only thing installed on it is Docker + the agent.

## Tier 3 — + fleet / Hutch / Hare

Distribution and managed capabilities: the read fleet (disposable workers
over published artifacts), the Hutch (managed Clover/Gemma via a
subscription key — tier 0's cloud models, but ours), Hare (serverless
query offload). All strictly additive; each is a catalog install + a key.

## The pitch this enables

An existing Postgres user does not adopt a platform — they add an
extension. `CREATE EXTENSION pg_rvbbit`, one preload line, and semantic
SQL + acceleration light up immediately using cloud models (a Clover
subscription = zero infra on their side). They pull local pieces in —
duck sidecar, warren, fleet — only when they want to cut the cloud
dependency or accelerate huge scans. Nothing is load-bearing that they
haven't opted into. The heavy compose stack is one way to run it, not the
price of entry.
