# Read Fleet — the upside-down lakehouse

**Thesis**: distribute the *acceleration layer*, never Postgres. One brain (the PG
primary: heap truth + catalog + router), N disposable muscles (warrens running
engine servers over shared immutable artifacts). Offloading, not "distributed":
the brain approves every query, warrens execute vetted SQL against frozen bytes,
and a dead warren is a slow query, never a wrong one. The lakehouse started with
immutable columnar and had to bolt on a mutable brain; we start with the brain
and publish the immutable layer outward. Cake first, icing second.

**The one commandment**: route WHOLE queries, never shard one. A query whose
tables span placements runs on the brain — slow, never wrong. No distributed
join executor, ever. Query routing, not query sharding.

Why it pays even with zero GPUs in the fleet:
- **Concurrency**: every offloaded scan is a PG backend, work_mem slab, and
  parallel-worker slot the brain keeps.
- **Isolation**: analytical scans can't evict the OLTP working set from the
  brain's buffer cache or saturate its NVMe. The fleet is a protection racket
  for the heap.
- **Elasticity without rebalancing**: readers pull immutable artifacts from
  shared storage on demand. Nothing is relocated (contrast Elastic shard
  moves). Spin up 10 duck warrens for the Monday stampede, kill them at noon.

## POC topology

- **Brain**: dev machine (local PG18 + rvbbit).
- **Muscles**: GCP GPU box (`rvbbit-sql-test1`, GQE + duck-server) + GCP CPU box
  (`rvbbit-cpu-test1`, duck-server only).
- **Shared storage**: GCS. Use the **S3-interop XML API with HMAC keys** first
  (zero new code if the s3 path works — endpoint `storage.googleapis.com`);
  native `gs://` via the object_store crate is the cheap follow-up. duckdb
  reads both via httpfs. Support matrix ships as: s3, s3-compatible (MinIO/R2/
  GCS-interop), gcs-native. That covers everyone who isn't Azure, and
  s3-compatible covers half of Azure users anyway (via gateways).
- **POC network**: **tailscale**, not mTLS. Flat private mesh in ten minutes,
  solves brain-behind-home-NAT for free, and engine ports never face the
  public internet (these boxes have been hammered before). Shared-token auth
  on the engine endpoints as a second factor. mTLS-with-brain-as-CA is the
  productization step, not the POC step — don't build the CA ceremony to
  prove the routing works.

**POC gotcha (the one that bites)**: publication runs brain→GCS, and the POC
brain is on residential upload bandwidth. Publishing a 750k-row bench table
from home will dominate the demo timeline. Mitigate: seed the bucket from a
GCP box (or publish a modest table). In production the brain sits on real
uplink and this disappears — but don't let it masquerade as a design flaw
during the POC.

## What exists / what's new

Already built (the "existing weapons, sharpened"):
- Warren: inventory, labels, health, deployments, capability manifests.
- GQE: already a Flight/gRPC server at a URL — remote ≈ config + shared files.
- ObjectStore tiering: cold_url + migrate_to_cold + df.rs reader — the
  publication mechanism wants dual-presence, not migration.
- Router: candidates, availability gates, learned latency curves,
  route_decisions breadcrumbs, per-table policy (accel_policy deny-sets),
  fail-open fallback.
- AS-OF generations: remote staleness is *declared* ("AS OF generation N"),
  not mystery replica lag.

New artifacts (the actual work):
1. **duck-server**: the existing sidecar behind a network listener (Flight for
   results; token auth; generation-pinned requests). One container image,
   deployable as a warren capability.
2. **Publication**: `publish-on-compact` — compact() also uploads the new
   generation's artifacts to the configured store, KEEPING local files
   (dual-presence). Registry records published_generation per table.
3. **Fleet metadata → routing gate**: warren heartbeats populate health/
   capability tables; candidate_availability consults them (cached, ~5s TTL —
   no per-query SPI storm) plus artifact coverage ("is every table in this
   query published at a generation this warren can see?").
4. **candidate×endpoint identity**: the latency model and route_decisions key
   on (candidate, node). The model learns WAN penalties from observation —
   nobody tells it the GCP box is 40ms away; it notices.

## UX doctrine (user's framing — keep it)

No "assign this table to cold/warm storage" ceremony. **Object store is a base
primitive**: configure it once; from then on, any table in the accel registry
publishes automatically (local files stay — the brain never depends on the
network for its own reads). Per-table opt-out lives in accel_policy (same
pattern as engine deny-sets). Sync state is a *freshness watermark*
(published_generation vs local generation) in accel_freshness — the existing
value-vs-cost policy plane grows a WHERE dimension. UI sugar (fleet tray à la
GQE tray, per-table publish state in Finder vitals) comes after the machinery
proves out.

## The four under-weighted items (not QoL — correctness/economics)

1. **Generation GC vs in-flight remote reads** — the one real distributed
   problem inherited. Brain reaps generation N while a warren is mid-scan on
   it. POC version: orphaned_files deferred-unlink grace window ≥ max remote
   query timeout (one constant). Production version: generation leases.
   Must be in Sprint 1, not polish.
2. **Result-set economics** — offloading a scan that returns 50M rows over the
   WAN un-wins the win. The router's shape features already estimate output
   cardinality; offload should favor aggregating shapes (small out, big in).
   The learned model will discover this, but add a guardrail prior so the POC
   doesn't learn it the expensive way.
3. **Cancellation propagation** — user cancels in lens → brain cancels the
   Flight call (native in gRPC/tonic). Without it, runaway scans keep burning
   the GPU box after the user gave up. Cheap; day one.
4. **Cache affinity** — warrens are stateless for correctness, cache-warm for
   performance. First read of a cold artifact pulls GBs from GCS. POC: accept
   it (intra-GCP GCS reads are fast). Production: prefer-warm-warren scheduling
   preference + pre-warm on deploy from placement policy.

## Sprints

**Sprint 1 — one remote muscle** (prove the spine)
- duck-server image: sidecar + listener + token auth + generation pin.
- Manual fleet row (skip the keeper; INSERT the endpoint by hand).
- publish-on-compact for one table to GCS (dual-presence).
- Router: remote candidate behind `route_force_candidate` override;
  route_decisions grows `node`.
- GC grace window constant.
- **Accept**: same query forced local vs remote returns identical results;
  receipts show node; kill the warren mid-query → error → fail-open heap
  retry serves it.

**Sprint 2 — the fleet is real** (remove the hands)
- Warren capability kind `engine`; heartbeat → health/capability tables;
  agent probes "can I reach the bucket" not just "am I up".
- Availability gate reads fleet metadata (cached) + artifact coverage.
- Latency model keyed candidate×node; un-force the router.
- Cancellation propagation; GQE box joins as second warren (its existing
  Flight server + published artifacts).
- **Accept**: router chooses remote for big aggregating scans and local for
  small ones ON ITS OWN (that's the demo); dead warren detected ≤ heartbeat
  interval and traffic re-routes with zero failed queries.

**Sprint 3 — honest and visible**
- Freshness: published_generation watermark in accel_freshness; remote
  results carry "AS OF generation N" declaration.
- Placement/opt-out policy rows; presigned-URL mode (credential-less warrens)
  behind a flag.
- Lens fleet tray (GQE-tray generalization: nodes, health, what's routed
  where, cache warmth).
- **The headline bench**: N concurrent clients, brain-only vs brain+2 warrens.
  Metrics: p95 latency, brain buffer-cache hit rate, OLTP write latency under
  analytical load. Concurrency + isolation is the story, not single-query
  speed.

## POC exit criteria ("we're basically there" test)

Local brain + 2 GCP warrens; a bench table published; 20 concurrent analytical
clients + a write workload. Pass = OLTP write p95 unmoved with the fleet on,
analytical p95 improved vs brain-only, zero wrong results, every decision
visible in route_decisions with node identity, and yanking a warren's plug
mid-run is a non-event in the results (visible only as a latency blip and a
breadcrumb). Then it's QoL from there — agreed.
