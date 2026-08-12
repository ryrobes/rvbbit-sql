# Business Topology

Business Topology is RVBBIT's living, evidence-backed map of the business
concepts represented across customer data. It is intentionally complementary
to both existing knowledge graphs:

| Layer | Answers | Example |
|---|---|---|
| Catalog KG | What physical data exists? | `crm.people.email` is a text field. |
| Business Topology | What business concepts and facets appear where? | Customer identity appears in CRM, billing, and support data. |
| Fact / semantic KG | What concrete claims have been observed? | Alice is enrolled in BIO-101. |

The promoted topology should remain compact enough to render as a hierarchy or
"company skeleton." Cross-links are retained, but the default product surface
must not become an undifferentiated network diagram.

## Status

Migration `0279_business_topology_foundation` ships the first core slice:

- Generic source and semantic-population registry.
- Bounded PostgreSQL relation profiling.
- Immutable profile snapshots and exact model-packet receipts.
- Installation-salted local value fingerprints.
- Inverted-index overlap candidate generation.
- Versioned inference queue and lease/complete worker contract.
- Proposal/evidence/review lifecycle.
- Human-promoted topology nodes, population bindings, and cross-links.
- A readable `rvbbit.business_topology_skeleton` view.

Migration `0282_business_topology_proposal_bundles` adds whole-skeleton shadow
receipts for the native DataRabbit debugger. Migration
`0284_business_topology_bundle_corrections` adds append-only human correction
overlays. Migration `0285_business_topology_workflows` adds the durable SQL
control plane for a complete, resumable excavation and the standing
Warehouse MCP executor that performs it. It does **not** yet ship a trained
topology specialist, a Calliope
company-skeleton projection, transactional bundle promotion, or a profiler for
accepted composite/slice/document populations. Those remain later layers.

## Population, not table

The inference atom is a **population**, not a table:

- `field`: an automatically profiled atomic field.
- `record_context`: source neighborhood supplied to a model; never itself
  asserted as a business object.
- `composite`: a related field bundle such as customer identity.
- `slice`: a declarative subset such as contacts associated with active accounts.
- `event_stream`: recurring business events.
- `mention_set`: mentions of a concept in unstructured sources.
- `query_projection`: a stable derived projection.

One relation can contribute to many concepts. One concept can bind to many
populations across many systems. Accepted slice/composite selectors are
declarative JSON and may not contain executable SQL.

## Privacy and bounded work

`rvbbit.business_topology_profile_packet()` and all queued model packets have:

```json
{
  "privacy": {
    "raw_values": false,
    "value_hashes": false,
    "bounded_sample": true
  }
}
```

The relation profiler:

- Requires the caller to have `SELECT` on the relation.
- Materializes at most the requested sample limit (32–50,000 rows).
- Uses deterministic page sampling for large analyzed heaps and a bounded
  `LIMIT` fallback for unsupported/custom relation types.
- Never runs a source `count(*)` and never sorts a source by `random()`.
- Never persists raw sampled values.
- Classifies value shapes, repetition, cardinality, null behavior, type family,
  and weak role/sensitivity hints.
- Derives bounded field-pair behavior—co-presence, shared missingness, equality,
  and approximate directional dependency—from at most 48 sampled fields, then
  discards the sampled cells.

Top sampled values are normalized and hashed with an installation-local salt.
Those hashes live only in `business_topology_value_fingerprints`. They are used
inside the customer database to compute numeric overlap/containment features;
neither hashes nor raw values enter a Clover packet.

This is a privacy boundary, not a claim that salted hashes are anonymous data.
Access to the private fingerprint table should remain at the same administrative
trust level as the warehouse itself.

## First run

Profile selected sources without model work:

```sql
SELECT rvbbit.business_topology_excavation_run(
    p_relations      => ARRAY[
        'crm.contacts',
        'billing.accounts',
        'support.requesters'
    ],
    p_sample_rows    => 2048,
    p_enqueue        => false
);
```

Or enumerate caller-readable base/materialized relations outside system and
configured catalog-exclusion schemas:

```sql
SELECT rvbbit.business_topology_excavation_run(
    p_schemas       => ARRAY['crm','billing','support'],
    p_sample_rows   => 2048,
    p_max_relations => 200,
    p_enqueue       => true
);
```

`p_enqueue => true` creates:

- One `population_embedding` job per changed field/context packet.
- One `source_motifs` job per changed relation context. This job is explicitly
  allowed to propose several composites/slices within a relation.
- `correspondence` jobs for plausible value-overlap candidates after the sweep.

Unchanged profile and model-input hashes do not create snapshots or jobs.

Inspect the local deterministic baseline:

```sql
SELECT *
FROM rvbbit.business_topology_overlap_candidates(
    p_min_shared => 2,
    p_max_fanout => 20,
    p_limit      => 1000
);
```

An overlap candidate means only "worth scoring." It does not mean joinable,
same concept, or same instance.

## Full appliance-native excavation

Migration `0285_business_topology_workflows` turns the complete shadow pipeline
into one durable SQL job. The shortest way to make a fresh topology proposal
set is:

```sql
SELECT rvbbit.business_topology_start_workflow();
```

That call snapshots up to 100 caller-readable non-system relations, queues one
run, and returns its UUID. Repeating it while a run is queued, running, or
cancelling returns the same UUID rather than creating duplicate work. To scope
the excavation explicitly:

```sql
SELECT rvbbit.business_topology_start_workflow(
    p_schemas        => ARRAY['crm','billing','support'],
    p_sample_rows    => 2048,
    p_max_relations  => 200,
    p_max_work_items => 800,
    p_max_llm_calls  => 160,
    p_run_name       => 'Customer operations excavation'
);
```

`p_relations => ARRAY['crm.contacts','billing.accounts']` may be used instead
of schema discovery. Relation names are resolved and snapshotted when the job
is created, so a later search-path or catalog change cannot silently alter its
scope.

The standing Warehouse MCP worker leases the job and persists progress after
every major phase:

```sql
SELECT run_id,run_name,status,phase,
       relation_count,population_count,excavation_unit_count,
       completed_work_items,work_item_count,llm_attempts,bundles_staged,
       any_worker_online,error,updated_at
FROM rvbbit.business_topology_workflow_status
ORDER BY requested_at DESC
LIMIT 1;
```

Closing DataRabbit, refreshing the page, or restarting Warehouse MCP does not
cancel the run. Private extraction, embedding, plan, and execution receipts are
resumed from Warehouse's durable data volume; PostgreSQL remains the workflow
control plane. A cancellation request is also SQL-native:

```sql
SELECT rvbbit.business_topology_cancel_workflow('<run uuid>'::uuid);
```

The worker stops after the current bounded operation and records `cancelled`.
On success it stages only deterministically validated proposal bundles in
`business_topology_proposal_bundles`; it never accepts or materializes governed
topology. DataRabbit exposes the same contract under **Knowledge → Business
Topology**: **New excavation** starts the SQL job, the status strip polls its
durable progress, terminal state refreshes the bundle debugger automatically,
and the adjacent cancel control requests a safe stop.

The older `business_topology_excavation_run()` examples above remain useful for
profiling and the lower-level inference queue. They do not run the complete
neighborhood/plan/synthesis/bundle workflow by themselves.

## Clover/Hutch contracts

The intended first deployment adds two checkpoint families, not a new model
zoo:

1. `semantic_population_v1`
   - Consumes `rvbbit.business-topology.population.v1` and
     `rvbbit.business-topology.source-motifs.v1` packets.
   - Produces a contextual embedding, multi-label role probabilities,
     uncertainty, and declarative motif proposals.
2. `semantic_correspondence_v1`
   - Consumes `rvbbit.business-topology.correspondence.v1` packets.
   - Scores distinct verdicts such as `same_concept`, `same_facet`,
     `same_instance_key`, `joinable`, `attribute_of`, `event_about`,
     `measurement_of`, `correlated`, `unrelated`, and `abstain`.

Hutch should expose both as batched, `unlaned: true` specialist backends. They
still retain normal tenant authentication, metering, model-version receipts,
and would-be-cost records. The worker should be stateless: tenant topology state
and profile caches remain in PostgreSQL.

The first executable shadow worker now lives in
`bench/business_topology/worker.py` and is exposed as
`execute-excavation-plan`. It uses the registered Hutch-backed `clover_llm`
only for motif and synthesis work, while the precision-first local
correspondence floor handles the bounded pair volume. Each successful call is
written as a private, resumable receipt with the exact Hutch model-version
header and is validated before storage. It intentionally submits no proposals
and performs no topology DML; this keeps mixed-corpus assumption tests on the
safe side of the review ledger.

Wide sources retain their complete plan scope but do not force every field into
one generative call. When a unit exceeds its binding budget, the worker selects
an evidence-led synthesis frontier from correspondence results, then balances
remaining capacity across source motifs and sources. Prompt-only identifiers
are compact aliases; they are expanded before validation and never persist in
the result. Populations outside the frontier are deterministically included in
the result's unbound set. Each model receipt records the frontier's selected and
total counts plus a stable ID hash, so review can distinguish deliberate
bounded abstention from missing input.

A complete response that exceeds only the binding budget is projected back to
that budget with a deterministic precision-first policy. The projection first
checks all binding references, retains at least one strongest binding per node,
then ranks remaining candidates by confidence and structural role; omitted
populations become explicitly unbound. Its before/after counts are stored in
the execution receipt. Invalid references, malformed evidence, and impossible
node coverage are never repaired by this projection.

No new named Hutch backend is warranted for this shadow phase. A prompt routed
to the general Clover service is not a trained specialist. The first
`semantic_population_v1` or `semantic_correspondence_v1` deployment should be
an unlaned, versioned checkpoint that has cleared the held-out-family and
hard-negative gates below.

Claim work transactionally:

```sql
SELECT *
FROM rvbbit.business_topology_claim_inference_jobs(
    p_worker       => 'clover-topology-1',
    p_task_kinds   => ARRAY['population_embedding','source_motifs'],
    p_limit        => 64,
    p_lease_seconds=> 900
);
```

Complete with an exact model receipt:

```sql
SELECT rvbbit.business_topology_complete_inference_job(
    p_job_id       => '<job uuid>',
    p_succeeded    => true,
    p_model_name   => 'semantic-population',
    p_model_version=> 'semantic-population-v1.0.0',
    p_result       => '{"embedding_ref":"...","roles":{},"uncertainty":0.1}'
);
```

Expired worker leases are reclaimable. A profile change marks unconsumed work
for the prior profile stale, so an old verdict cannot silently become current.

## Governance

Inference results become proposals, not topology truth:

```sql
SELECT rvbbit.business_topology_propose(
    p_proposal_kind => 'node',
    p_payload        => '{
      "node_kind":"object",
      "name":"Customer",
      "description":"A customer known across operating systems"
    }',
    p_confidence     => 0.94,
    p_inference_kind => 'semantic_population_v1',
    p_source_job_id  => '<job uuid>',
    p_model_name     => 'semantic-population',
    p_model_version  => 'semantic-population-v1.0.0'
);
```

Evidence is attached independently with
`business_topology_add_proposal_evidence()`. A reviewer then calls
`business_topology_review_proposal()`. Accepted `population`, `node`, `binding`,
`edge`, `hierarchy`, and `authority` proposals materialize the governed map.
Identity-rule proposals remain ledger-only until the instance-linking contract
lands; acceptance must not silently invent a join.

## Training and evaluation pre-work

The reusable shadow-extraction, review-overlay, candidate-generation, and
baseline evaluation tooling lives in
[`bench/business_topology`](../bench/business_topology/README.md). It is
schema-agnostic after the source-adapter boundary: customer-specific concepts
and review decisions stay in private corpus overlays, while the public suite
contains only contracts and synthetic multi-system regressions.

The baseline has two independent recall paths: dual-channel semantic neighbors
(`focus` plus `context`) and installation-local value overlap. Their numeric
evidence merges before correspondence scoring; neither path asserts an edge.
PostgreSQL embedding and overlap adapters run in transactions that are
unconditionally rolled back, including temporary embedding-cache writes.

The overlap adapter has two bounded modes. Its broad inverted-index pass drops
fingerprints with excessive population fanout so ubiquitous flags and tiny
codes do not create a graph explosion. Independently nominated semantic or
usage pairs may then receive an exact pair-local overlap probe. This second pass
bypasses the global fanout filter only for the strongest capped nominations;
raw values and installation-local fingerprints still never cross the adapter.

The harness also proposes adaptive source neighborhoods from source-context
embeddings. This is a coarse search boundary, not a table-as-object assumption:
one neighborhood can contain several systems, one source can later yield many
business objects, and weak links are retained as non-merging bridges. Sources
that do not clear both absolute and installation-relative affinity gates remain
singletons. The same implementation accepts relational and non-relational
population packets and never reads private source-family controls during
inference.

Those neighborhoods can now be compiled into a bounded shadow excavation DAG.
Each source first receives independent multi-object motif work; cross-source
population comparisons are limited to its excavation unit; an internal
synthesis step proposes the unit's object skeleton; and only nominated bridge
or shard-boundary evidence reaches the final cross-unit synthesis stage.
Oversized connected components are graph-sharded by both source count and
non-context population count rather than allowed to create an unbounded model
prompt or response. This catches neighborhoods made from only a few very wide
relations. Sources remain intact, individually wide sources are flagged, and
cut affinities remain explicit work instead of disappearing.

The production planner intentionally excludes hard-negative and diversity
review probes. It accepts only semantic-neighbor, installation-local overlap,
or usage-supported correspondence candidates. All work remains proposal-only:
the shadow planner neither enqueues database jobs nor materializes nodes,
bindings, hierarchy, or edges.

Versioned neighborhood and bridge result validators enforce the other side of
that boundary. A neighborhood result must account for every scoped population
as bound or explicitly unbound, ground every node in at least one population,
give the entire proposed tree a short business-readable canonical name, cite
only prerequisite work, preserve an acyclic hierarchy, and contain no SQL, raw
values, fingerprints, or private controls. Bridge results may reference
only their bounded synthesis/node or population endpoints and are forbidden
from requesting an automatic unit merge.

Validated synthesis receipts are persisted whole in
`business_topology_proposal_bundles` for the DataRabbit review surface. A bundle
preserves its local node keys, bindings, hierarchy, edges, exact plan/worker/
prompt/model receipts, source context, and validation as one proposal. It is
explicitly not governed topology: the ledger supports revision, rejection, and
supersession, but has no acceptance/materialization state yet. Transactional
promotion will resolve all local node keys to durable UUIDs together in a later
contract rather than accepting dependent atomic proposals out of order.
The native DataRabbit WIP debugger lives under **Knowledge → Business
Topology** and exposes these bundles, their semantic hierarchy, unbound
populations, evidence, and execution receipts without offering a promotion
action. Canonical business names lead the bundle rail and tree header; source
table names remain visible only as provenance evidence.

Review corrections do not rewrite those receipts. Each save appends one full
overlay snapshot in `business_topology_bundle_corrections`, protected by an
expected-revision check. The overlay can rename and reparent receipt nodes,
change their kind or description, suppress an entire branch or one binding,
adjust binding role/authority, and record split/merge suggestions plus a human
note. DataRabbit projects only the latest revision over the immutable result.
Both `draft` and `complete` remain shadow states: `complete` means the reviewer
finished that correction pass, not that RVBBIT accepted or materialized it.

The PostgreSQL shadow adapter calls only
`business_topology_profile_packet()`, rolls its transaction back, and verifies
that topology ledger counts did not change. Because the bounded profiler uses
transaction-local temporary tables, a hardened shadow role needs `SELECT`,
`TEMP`, and packet-function `EXECUTE`, but no persistent DML privileges.

The first evaluation corpus should come from a non-persisting reference-deployment
shadow extraction, not from assumed schemas. Label a compact set of:

- Population role assignments.
- Same-concept and same-facet pairs.
- Joinable-but-not-same pairs.
- Same-instance-key candidates.
- Hard negatives with deceptively similar formats.
- Multi-object source motifs and accepted field bundles.

Useful weak supervision already exists in RVBBIT:

- Successful joins from actual Calliope SQL and route-shape history.
- Reused dashboards, artifacts, cubes, and promoted metrics.
- Exact/high-confidence local value overlap.
- Document entity links and accepted source mappings.
- Human accept/reject decisions from this proposal ledger.

Train precision-first with calibrated abstention. False semantic edges compound
more dangerously than unknown edges. Frontier LLM work should be limited to
cluster naming, explanations, and a small ambiguous frontier; it should not be
the bulk pair scorer.

One deployment is an assumption detector, not a universal training set. No
checkpoint may be promoted merely because it recalls one organization's known
bridges. Before deployment, repeat candidate-recall and held-out-family tests
against at least one structurally different corpus, including a non-relational
population adapter. Customer nouns, source names, and private labels must remain
outside the reusable feature and checkpoint contracts.

## Next implementation milestones

1. Review source-neighborhood bridges, excavation plans, private reference
   controls, multi-object motifs, and hard negatives.
2. Repeat the generalization gate on additional structurally different corpora.
3. Review and correct receipt-backed proposal bundles in the WIP DataRabbit
   debugger; use the append-only correction history as explicit error signal.
   Train and deploy the population/motif and correspondence specialists through
   Hutch only after their held-out promotion gates pass.
4. Add profile executors for accepted composite/slice/mention populations.
5. Feed successful Calliope joins and promotion decisions back as evidence.
6. Design transactional bundle promotion separately from correction editing,
   then add the Calliope company-skeleton projection.
7. Bind Living Pages, briefs, metrics, and query planning to accepted topology
   nodes while preserving their underlying source receipts.

`rvbbit.data_crawl_run()` remains a separate selective fact-extraction path. It
must not be scheduled as the topology profiler and should not become a source of
raw-row profile evidence.
