# Hare — serverless query offload (query capsules)

**Name.** Warrens are burrows: persistent, stateful, homes for deployable
capabilities. Hares don't burrow — no den, no state, just fast and gone. A
*hare* is a stateless query sprinter: it materializes (scale-from-zero),
answers exactly one capsule, and vanishes. It deliberately does NOT have the
warren's deployable-capability surface; it is its own thing that borrows the
duck engine, not a warren variant.

**Thesis.** The read fleet already proved the shape: whole queries routed to
disposable workers over published immutable artifacts, fail-open, never
wrong. Fleet workers are *pets on a leash* — long-running processes holding a
read-only DSN back to the brain and their own object-store credentials. A
hare removes both tethers. The brain ships a **query capsule**: the vetted
SQL plus everything the worker would otherwise have to ask the catalog for —
table manifests, row-group URLs (presigned), column types, generation pins,
limits. The worker needs **zero credentials, zero callbacks, zero state**.
That is what makes scale-to-zero platforms (Cloud Run first) viable: nothing
to warm up but the binary, nothing to configure but a shared token.

## The capsule

Produced by `rvbbit.capsule(sql, ttl_secs, presign)` on the brain. JSON:

```json
{
  "capsule": 1,
  "sql": "SELECT ...",
  "engine": "duck",
  "max_rows": 10000,
  "timeout_s": 60,
  "expires_s": 900,
  "published_only": true,
  "tables": [
    {
      "schema": "public",
      "relname": "hits",
      "columns": [["UserID", "bigint"], ["EventDate", "date"]],
      "paths": ["https://storage.googleapis.com/...signed..."],
      "row_group_rows": 1000000,
      "row_group_bytes": 52428800,
      "generation": 7
    }
  ]
}
```

Design notes:

- The manifest is exactly the engine's internal `RvbbitDuckTable` shape — the
  same catalog slice `rvbbit_row_group_catalog()` fetches over the DSN today,
  precomputed by the brain. Columns carry **PG type names**; the worker
  applies its existing `supported_pg_type` gate and casts, so type policy
  lives in one place (the engine) and stays symmetric with fleet mode.
- **Presigned GETs** (object_store `Signer`, S3 + GCS both implement it; GCS
  also works through the S3-interop path we already use) mean the hare holds
  no store credentials. `file://` paths pass through un-signed — that is the
  local-loopback dev mode, not a production path.
- **Freshness gate (v1):** capsule minting enforces the SAME predicate the
  sidecar's DSN catalog uses to drop a table from its view — pending
  `delete_log` rows OR a retained-but-dirty shadow heap both mean the parquet
  no longer equals the heap; the error names the table and the fix
  (`compact` / `rebuild_acceleration`). Queried LIVE at mint time, not from
  the router's memoized table metrics, which can miss a just-executed DELETE
  (found the hard way: on AM tables a DELETE marks the shadow heap dirty
  rather than writing an immediate tombstone — compaction is the diff
  engine). Tombstone bitmaps ride in a later capsule version; correctness
  first.
- **Generation pins** ride along for observability and future AS-OF capsules;
  v1 workers trust the manifest (the URLs *are* the pin — immutable
  artifacts).
- Expiry is advisory in v1 (presigned URLs enforce real expiry themselves).

## The worker

`rvbbit-duck --serve-http [host:port]` — a plain HTTP/1.1 server (Cloud Run
speaks HTTP to the container; honors `$PORT` when the flag gives no port):

- `POST /capsule` with `Authorization: Bearer $RVBBIT_ENGINE_TOKEN`
  (fail-closed at startup, same contract as `--serve-tcp`). Body = capsule.
  Response = the standard `QuerySummary` JSON (rows forced to JSON transport,
  same reasoning as fleet: an arrow-ipc *path* is worker-local and useless).
- `GET /healthz` → `200 ok` (Cloud Run health checks).
- Execution path: `guarded_safe_select` (defense in depth — the brain already
  vetted it), manifest → `RvbbitDuckTable` map, `run_duck_once`. No DSN
  connect, no catalog SQL, no authoritative-visibility check (the brain
  asserted it at build time — that's the trust model: the brain signs work,
  the hare does work).
- `--pgdata-prefix` / `--visible-pgdata-prefix` remaps apply to `file://`
  manifests so local loopback testing works without a bucket.

## The image

`docker/Dockerfile.rvbbit-hare` — multi-stage: `rust:1-bookworm` builds the
engine, runtime is `debian:bookworm-slim` + `ca-certificates` + the binary
(≈150MB vs the 1.2GB full image). One process, one port, no extension, no
Postgres. This image doubles as the slim fleet-worker image later.

## v1 scope (Cloud Run)

```
gcloud run deploy rvbbit-hare --image ghcr.io/ryrobes/rvbbit-hare:X \
  --set-env-vars RVBBIT_ENGINE_TOKEN=... --concurrency 4 --min-instances 0
```

- Brain-side dispatch is **not** wired into the router in v1 — pre-work only.
  Testing drives it manually: `rvbbit.capsule(...)` → POST → compare rows.
  Router integration (a `hare_endpoint` sibling to `duck_fleet_endpoint`,
  probe = wake, latency-aware demotion) comes after the experiment proves
  cold-start + fetch economics.
- Cloud Run first because gRPC/HTTP2 + scale-to-zero + per-request billing;
  Azure Container Apps has the same primitives (later); AWS needs
  capsule-over-Lambda gymnastics (much later, maybe never).

## v1 experiment results (2026-07-10, us-east4)

Full chain proven end-to-end: brain1 (GCE VM, 3.2.0 + 0139) minted presigned
capsules against the published GCS artifacts; the hare image
(`gcr.io/rabbitize/rvbbit-hare:exp2`, 295MB) ran on Cloud Run
(min-instances 0, 2 CPU / 2GiB, concurrency 4); a laptop POSTed capsules to
the public URL. `traffic_violations`, 1.83M rows across 2 published row
groups, `GROUP BY` + `ORDER BY` + `LIMIT`:

- **Correctness**: rows byte-identical to the brain's heap answer, 7/7 runs.
- **Warm**: 0.56–0.71s end-to-end from the laptop; 454–546ms server-side
  (dominated by per-request re-fetch of the parquet — no cross-request cache
  in v1).
- **Cold** (fresh revision, first request): 1.21s end-to-end, 1.07s
  server-side. Scale-to-zero is genuinely viable for BI-sized queries.
- **Expiry works as designed**: a capsule minted 25 minutes earlier (900s
  TTL) was correctly refused by the object store — capsules go stale, by
  contract.

Platform gotchas (all encoded in code/image now):

1. **Cloud Run's frontend intercepts ANY `Authorization` header** and tries
   to verify it as a Google IAM token — 401 before the container sees the
   request, even with allUsers invoker. Hence `X-Rvbbit-Token` as the
   Cloud Run spelling of the same secret (Bearer still works for direct /
   VPC callers).
2. **`/healthz` is reserved by Google's frontend** and never forwarded;
   `GET /` is the health endpoint that works everywhere.
3. Debian's `protobuf-compiler` alone can't build the engine
   (substrait needs the well-known-type protos) — `libprotobuf-dev` rides
   in the build stage.

## Open questions for the experiment

1. Cold-start economics: binary start (~ms) + httpfs range-GETs against
   parquet — how much of a 500ms budget survives? (Vortex layouts later.)
2. Result transport: JSON is fine for BI-sized results; arrow-ipc-in-body is
   the v2 lever.
3. Warm reuse: same instance serving N capsules can keep a duck + page cache
   — free win, measure it.
4. Concurrency setting vs duck threads: 1 big or 4 small per instance.
