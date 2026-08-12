# Business Topology evaluation harness

This package is the model-development boundary for RVBBIT Business Topology.
It turns bounded, privacy-safe source observations into review corpora, builds
candidate queues, applies private human labels, and measures a precision-first
baseline before any inferred edge can become a topology proposal.

It is deliberately **not** a customer ontology and **not** a table classifier.
The core code knows structural roles and relationship verdicts. Names such as
`Customer`, `Office`, `Policy`, or `Season` remain customer-specific labels
learned from evidence and confirmed through the proposal ledger.

## Architecture

```text
source adapter                 reusable evaluation core
--------------                 ------------------------
PostgreSQL profile packet ──┐
document mention packet  ───┼─> populations + motifs + correspondences
SaaS/MCP source packet   ────┘          │
                                        ├─ bounded candidate queue
private review overlay ─────────────────┤
                                        ├─ leakage-safe family split
                                        └─ baseline/model evaluation
```

PostgreSQL is the first adapter. Future document, ticket, SaaS, or MCP adapters
must emit the same `population.v1`, `source-motifs.v1`, and
`correspondence.v1` contracts. The evaluator does not branch on customer or
source-system names.

## Privacy boundary

Model-bound packets contain names, comments, types, bounded distribution
counts, value-shape histograms, and numeric overlap evidence. They contain:

- No raw sampled values.
- No installation-local fingerprints or hashes.
- No executable SQL in motif proposals.
- No database credentials.

Schema and field names can still be commercially sensitive. Real extracted
corpora and label overlays therefore belong in a protected customer workspace,
not Git. This directory ignores `private/`, `outputs/`, `*.local.json`,
`*.private.json`, and model checkpoint files.

## Quick regression

From the repository root:

```bash
PYTHONPATH=bench python -m business_topology synthetic \
  --output /tmp/topology-synthetic.json

PYTHONPATH=bench python -m business_topology validate \
  /tmp/topology-synthetic.json --require-reviewed

PYTHONPATH=bench python -m business_topology evaluate \
  /tmp/topology-synthetic.json
```

The synthetic corpus contains mirrored concepts across several systems, an
event table containing several distinct business objects, directional subset
keys, deceptive same-shape negatives, and an ambiguous pair that should cause
abstention.

## Rollback-only PostgreSQL extraction

Create a private scope following `scope.example.json`, then provide the DSN
through an environment variable so credentials do not appear in command
history or process arguments:

```bash
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology extract-postgres \
  --scope /secure/customer/topology-scope.private.json \
  --corpus-id customer-shadow-001 \
  --sample-rows 2048 \
  --output /secure/customer/topology-shadow.private.json
```

`business_topology_profile_packet()` uses temporary tables, so PostgreSQL
cannot execute it inside `BEGIN READ ONLY`. Shadow extraction instead uses a
normal transaction, invokes only that non-persisting packet function, and
unconditionally rolls the transaction back. It compares topology ledger counts
before and after and refuses the output if they differ.

For defense in depth, use a dedicated role with:

- `SELECT` only on explicitly scoped sources.
- `TEMP` on the database, because bounded profiling uses temporary tables.
- `EXECUTE` on the packet function.
- No `INSERT`, `UPDATE`, or `DELETE` privileges on source or RVBBIT schemas.

## Candidate review and private labels

Candidate generation is bounded by total pairs and per-population fanout. It
retains several evidence strata, including deceptive format-similar pairs for
hard-negative review:

```bash
PYTHONPATH=bench python -m business_topology candidates \
  /secure/customer/topology-shadow.private.json \
  --max-pairs 250 --max-fanout 12 \
  --output /secure/customer/topology-review.private.json

PYTHONPATH=bench python -m business_topology label-template \
  /secure/customer/topology-review.private.json \
  --output /secure/customer/topology-labels.private.json
```

Structural blocking is intentionally only one recall path. For mismatched
names and schemas, emit provider-neutral population text, run it through any
customer-approved embedding adapter, and turn the private vectors into bounded
cross-source neighbor evidence:

```bash
PYTHONPATH=bench python -m business_topology embedding-inputs \
  /secure/customer/topology-shadow.private.json \
  --max-text-chars 12000 \
  --output /secure/customer/topology-embedding-inputs.private.jsonl

# The local adapter writes {"population_id": "...", "embedding": [...]} rows.

# When pg_rvbbit owns the approved embedding specialist, the bundled adapter
# batches by both item count and payload size, then rolls its cache writes back:
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology embed-postgres-shadow \
  /secure/customer/topology-shadow.private.json \
  --specialist embed \
  --channel focus --channel context \
  --output /secure/customer/topology-vectors.private.jsonl

PYTHONPATH=bench python -m business_topology embedding-evidence \
  /secure/customer/topology-shadow.private.json \
  /secure/customer/topology-vectors.private.jsonl \
  --top-k 32 \
  --output /secure/customer/topology-evidence.private.jsonl

# PostgreSQL can independently contribute local overlap aggregates. Sampled
# values and salted fingerprints never leave its rolled-back transaction.
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology overlap-postgres-shadow \
  /secure/customer/topology-shadow.private.json \
  --output /secure/customer/topology-overlap.private.jsonl

PYTHONPATH=bench python -m business_topology candidates \
  /secure/customer/topology-shadow.private.json \
  --evidence-stdin --max-pairs 250 --max-fanout 12 \
  --output /secure/customer/topology-review.private.json \
  < <(jq -c . \
      /secure/customer/topology-evidence.private.jsonl \
      /secure/customer/topology-overlap.private.jsonl)
```

Inputs are capped by character count and provider adapters should batch by
both item count and total payload size. The included neighbor search is exact
but blockwise, making it useful for evaluation without allocating a full
all-pairs matrix. Production-scale discovery can use Hutch or a vector index
while preserving the same numeric evidence contract.

The recommended baseline uses separate `focus` and `context` embeddings, then
combines their cosine scores. This keeps a wide record's field inventory from
drowning out the population being compared while still allowing two differently
named systems to reinforce a plausible match through their surroundings.

Large first-pass corpora can be staged with a generic structural-role filter,
for example `--role identity`. This is a compute policy, not a customer
ontology: the same fixed role vocabulary applies to every source adapter.

## Multi-domain source neighborhoods

A warehouse or imported reporting database is not assumed to represent one
business domain. It may contain unrelated applications, benchmark data,
replicas, experiments, and a few legitimate bridges between otherwise separate
areas. The source-neighborhood pass uses the privacy-safe source context to
propose a first map before object discovery:

```bash
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology embed-postgres-shadow \
  /secure/customer/topology-shadow.private.json \
  --specialist embed \
  --kind record_context --channel context \
  --output /secure/customer/topology-context-vectors.private.jsonl

PYTHONPATH=bench python -m business_topology source-neighborhoods \
  /secure/customer/topology-shadow.private.json \
  /secure/customer/topology-context-vectors.private.jsonl \
  --output /secure/customer/topology-neighborhoods.private.json
```

The inference is deliberately sparse and multi-resolution:

- Tight reciprocal affinities form unnamed candidate neighborhoods.
- A source with insufficient evidence remains an unassigned singleton.
- Weaker reciprocal affinities become bridges between neighborhoods; they do
  not collapse the neighborhoods into one connected component.
- Thresholds adapt to the installation's similarity distribution but retain
  absolute similarity and local-lift floors, so a junk drawer is not forced
  into a taxonomy merely because every source has a nearest neighbor.
- `record_context` is preferred, but non-relational adapters can supply any of
  the common population kinds; their source-level vectors are pooled without a
  PostgreSQL-specific branch.

Private extraction scopes may assign opaque source-family `split_group`
controls. Passing `--control-report ...` measures nearest-neighbor agreement,
tight-edge agreement, pairwise precision/recall, and control fragmentation.
Those controls are never read by inference. A cross-control edge is an audit
item rather than an automatic error: it may identify a replica, an adjacent
business area, or a coarse control that should be split.

Neighborhoods are not named business domains and are not topology truth. They
bound later motif, correspondence, and object discovery work and become review
proposals. Naming or merging them belongs to the learned/human proposal layer.

## Bounded neighborhood excavation

The next shadow stage compiles those neighborhoods into an auditable model-work
DAG. Optional field-level semantic and local-overlap evidence make the plan
precision-first without becoming prerequisites for source discovery:

```bash
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology embed-postgres-shadow \
  topology-shadow.private.json \
  --specialist embed --kind field \
  --channel focus --channel context \
  --output topology-field-vectors.private.jsonl

PYTHONPATH=bench python -m business_topology embedding-evidence \
  topology-shadow.private.json topology-field-vectors.private.jsonl \
  --top-k 24 --output topology-semantic-evidence.private.jsonl

RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology overlap-postgres-shadow \
  topology-shadow.private.json \
  --probe-evidence topology-semantic-evidence.private.jsonl \
  --max-probe-pairs 1000 \
  --output topology-overlap-evidence.private.jsonl

PYTHONPATH=bench python -m business_topology excavation-plan \
  topology-shadow.private.json topology-neighborhoods.private.json \
  --evidence topology-semantic-evidence.private.jsonl \
  --evidence topology-overlap-evidence.private.jsonl \
  --maximum-populations-per-unit 48 \
  --output topology-excavation-plan.private.json
```

The broad overlap pass ignores fingerprints that occur in too many populations,
which prevents common booleans, statuses, and tiny integers from exploding the
candidate graph. That can also hide a legitimate pair of common regional or
category keys. `--probe-evidence` therefore gives the strongest independently
nominated semantic or usage pairs a bounded exact local overlap check. Usage
evidence is prioritized first, then embedding similarity; `--max-probe-pairs`
is a hard cap. This bypasses the global fanout filter only for those explicit
pairs and still emits aggregates rather than values or fingerprints.

The resulting stages are:

1. `source_motifs`: independently inspect every source and permit several
   composites, slices, references, or event populations from one source.
2. `correspondence`: score bounded cross-source population pairs inside an
   excavation unit. The production plan accepts semantic, overlap, and usage
   strata; hard-negative and diversity probes remain in the review/evaluation
   queue and do not consume production inference.
3. `neighborhood_synthesis`: compose candidate objects, facets, events,
   measures, bindings, and hierarchy from the prior receipts. A population may
   participate in several hypotheses and the model may leave it unbound.
4. `bridge_synthesis`: inspect only source-neighborhood bridges or graph cuts
   created by bounded sharding. A bridge result may propose a relationship or
   shared object, but it cannot silently merge the units.

Every work item carries a validated no-values/no-hashes packet, stable ID,
scope, dependencies, and a proposal-only output contract. The planner caps
sources and populations per unit, pairs per unit/link, population fanout, and
cross-neighborhood links. Population count is a separate model-work budget: a
few wide relations cannot evade the source cap and create a response too large
to complete. Graph-aware shards keep each source intact and preserve the
strongest cut affinities as explicit continuation work. An individually wide
source is retained as a visibly flagged exception instead of dropping fields.

This command writes a private plan only. It does not enqueue inference jobs or
materialize topology. The first live worker should execute the same DAG through
versioned Clover/Hutch checkpoints, validate result contracts, and submit nodes,
bindings, edges, or hierarchy to the existing proposal-review ledger.

Neighborhood and bridge synthesis results already have a strict validation
boundary:

```bash
PYTHONPATH=bench python -m business_topology validate-excavation-result \
  topology-excavation-plan.private.json \
  one-synthesis-result.private.json

# Bridge node references can additionally be checked against the exact prior
# neighborhood results they cite.
PYTHONPATH=bench python -m business_topology validate-excavation-result \
  topology-excavation-plan.private.json bridge-result.private.json \
  --prior-result left-neighborhood-result.private.json \
  --prior-result right-neighborhood-result.private.json
```

The validator requires every unit population to be either grounded in one or
more bindings or explicitly reported as unbound. Every proposed node must have
a binding and cite bounded prerequisite work; hierarchy must be acyclic; edges
must stay inside the result; executable SQL, raw values, local fingerprints, and
private controls are rejected. Bridge findings can only reference their two
synthesis units or nominated population probes and can never request an
automatic unit merge.

### Execute a bounded shadow DAG through Hutch

The first resumable executor is deliberately still a shadow worker. It writes
private result receipts, but it does not enqueue database jobs, submit proposal
rows, or materialize topology. Select one work item, excavation unit, boundary
link, or the explicitly gated full plan; the worker resolves the complete
dependency closure before making a call:

```bash
# Inspect the closure without touching the filesystem or network.
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology execute-excavation-plan \
  topology-excavation-plan.private.json \
  --unit-id excavation:... \
  --output-dir /secure/customer/topology-run.private \
  --dry-run

# Execute it through the tenant's registered clover_llm backend.
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology execute-excavation-plan \
  topology-excavation-plan.private.json \
  --unit-id excavation:... \
  --output-dir /secure/customer/topology-run.private \
  --max-work-items 32 \
  --max-llm-calls 8
```

Execution is split by what actually needs a generative model:

- `correspondence` uses the precision-first deterministic pair floor locally.
  It emits a complete score vector and abstains rather than spending an LLM
  call on every candidate pair.
- `source_motifs`, `neighborhood_synthesis`, and `bridge_synthesis` use the
  registered OpenAI-compatible Clover backend through Hutch.

Hutch calls record the exact `x-hutch-model-version`, provider request ID,
usage, prompt/output hashes, transport retries, worker version, and prompt
contract version. The worker reads the registered endpoint and secret from
PostgreSQL; tokens are never written to its execution directory. Every result
is checked against its exact bounded work item before being stored. Invalid
model text is not persisted.

JSON recovery accepts only a complete root object. It never scans a truncated
response for a valid-looking inner node or binding. Harmless generic `result`
wrappers and non-contract outer status words are normalized only when the
proposal collection makes the canonical status unambiguous; all semantic
content must still pass the deterministic result validator.

Synthesis prompts replace repeat-heavy population and dependency identifiers
with per-call compact aliases. A value-free legend retains each population's
field label; the executor expands only contract ID slots back to the exact plan
IDs before normalization and validation. Stored results therefore keep stable
identities while avoiding output spent repeating opaque identifiers. If a
synthesis unit is wider than its binding budget, the worker also constructs a
bounded synthesis frontier: independently supported correspondences are
considered first, then source motifs and remaining fields are sampled
round-robin so one relation or motif cannot consume the prompt merely because
it sorts first. Only that frontier is exposed to the model. The exact original
scope remains the validation boundary, and every population outside the
frontier is deterministically recorded as unbound. The execution receipt stores
the selected/total counts and a hash of the selected population IDs, making the
selection auditable without duplicating private packets.

If an otherwise bounded neighborhood response reuses populations and exceeds
the binding cap, the worker applies a deterministic precision-first projection
instead of expanding the contract or discarding the entire skeleton. It first
verifies every binding stays within the exact population/evidence contract,
retains the strongest binding for every node, and fills the remaining budget by
confidence and structural role. The receipt records the input, output, and
removed counts. Any malformed or out-of-scope binding still fails normal
validation and enters the bounded repair path.

The execution directory and all receipts are mode `0700`/`0600`. Re-running the
same command resumes completed dependencies. A different plan hash, worker
version, or prompt contract is rejected instead of silently mixing regimes.
Transient network failures and Hutch `429`/`5xx` responses receive bounded,
receipt-visible retries. `--max-work-items` and `--max-llm-calls` are hard
preflight gates; `--all` does not bypass them.

The four result contracts are now versioned:

- `source-motifs-result.v1` accounts for every source field as assigned or
  explicitly unassigned and permits multiple overlapping motifs.
- `correspondence-result.v1` binds the exact pair to a complete verdict score
  vector, confidence, uncertainty, and explicit abstention state.
- `neighborhood-skeleton-result.v1` gives each proposed tree a short canonical
  business name, grounds every node, and accounts for every scoped population.
- `bridge-result.v1` can propose only bounded cross-unit findings and can never
  merge excavation units.

### Stage whole skeletons for review

Once synthesis results validate, they can be copied from the private execution
directory into the database as internally consistent proposal bundles:

```bash
# Re-read every receipt and reproduce its validation without writing.
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology stage-proposal-bundles \
  topology-excavation-plan.private.json \
  /secure/customer/topology-run.private \
  --dry-run

# Stage completed neighborhood skeletons for DataRabbit inspection.
RVBBIT_DSN='postgresql://...' PYTHONPATH=bench \
python -m business_topology stage-proposal-bundles \
  topology-excavation-plan.private.json \
  /secure/customer/topology-run.private
```

The staging command checks the execution manifest against the canonical plan
hash, reloads only completed synthesis receipts, reproduces their deterministic
validation, and inserts all bundles in one transaction. The context projection
contains readable source and population labels but no raw values, local hashes,
or profile packets.

`business_topology_proposal_bundles` is deliberately not the governed topology.
Its states are `proposed`, `needs_revision`, `rejected`, and `superseded`;
there is no accepted state. The companion summary view exposes the canonical
tree name, coverage, confidence, cost, model versions, validation, and complete
result to the WIP DataRabbit debugger. Transactional bundle promotion will be a
separate contract after local node keys can be resolved together into durable
UUIDs. The debugger is available from DataRabbit's **Knowledge → Business
Topology** launcher. Migration `0284_business_topology_bundle_corrections`
gives it a versioned correction overlay without weakening that boundary:
reviewers can rename, reparent, suppress, or reclassify receipt nodes; adjust
or suppress bindings; record split/merge suggestions; and save a review note.
Each save is append-only and optimistic-concurrency protected. A `complete`
review is still shadow state and never accepts or materializes topology.

The deterministic correspondence floor is not being disguised as a trained
Hutch specialist. Promote `semantic_population_v1` and
`semantic_correspondence_v1` as new unlaned Hutch backends only after reviewed,
held-out-family checkpoints beat this floor. The executor seam is intentionally
small so those deployments replace a model implementation without moving
tenant state or weakening the result validators.

Candidate input is a union of independent recall paths. If multiple adapters
nominate the same pair, their distinct numeric evidence is merged and repeated
signals keep the strongest value. A name match, embedding neighbor, or value
overlap remains a review candidate—not an asserted business relationship.

Reviewers fill only the private overlay. Applying it cannot change packet
features or identifiers. They should also give semantically related examples
across systems the same opaque `split_group`; this keeps an entire reviewed
concept family on one side of a train/test split:

```bash
PYTHONPATH=bench python -m business_topology apply-labels \
  /secure/customer/topology-review.private.json \
  /secure/customer/topology-labels.private.json \
  --output /secure/customer/topology-reviewed.private.json
```

The overlay may also add `correspondence_controls`: reviewed anchor pairs that
did not happen to land in a bounded review sample. This keeps customer-specific
expectations private while allowing the reusable harness to measure whether the
full evidence pool can rediscover them:

```bash
jq -c . topology-evidence.private.jsonl topology-overlap.private.jsonl | \
PYTHONPATH=bench python -m business_topology candidate-recall \
  topology-reviewed-controls.private.json --evidence-stdin
```

Candidate recall is measured before human-review sampling. A small diverse
review queue and a broad high-recall inference pool are different artifacts;
conflating them makes queue quotas look like model failures.

Correspondence labels are multi-label because a pair may simultaneously be the
same concept, the same facet, a same-instance key, and joinable. `abstain`
always stands alone. A matching value format or an overlap candidate is never
itself a semantic edge.

## Evaluation discipline

Splits operate on complete `split_group` families rather than random pairs.
Extraction starts with conservative source-family groups; private review can
replace those with opaque cross-system concept-family groups without altering
the model packet. This prevents one identifier or mirrored concept from
leaking near-duplicates into both training and evaluation.

The deterministic baseline is intentionally conservative and explainable. It
is the floor for future Clover checkpoints, not a production inference engine.
The important report fields are:

- Precision and recall by verdict.
- Coverage after abstention.
- Hard-negative false-edge rate.
- Held-out family behavior.
- Exact model and packet receipts.

Initial promotion gates should emphasize precision: target at least 0.98
precision for automatically proposed same-instance keys, preserve abstention on
ambiguous evidence, and require human review before materialization. Recall can
improve as proposal decisions and successful Calliope joins accumulate.

The harness can also train a small portable one-vs-rest checkpoint. This is an
explainable learned floor and a test of the complete dataset path, not the final
specialist:

```bash
PYTHONPATH=bench python -m business_topology train-linear \
  /secure/customer/topology-reviewed.private.json \
  --task correspondence \
  --checkpoint /secure/customer/correspondence.checkpoint.json \
  --report /secure/customer/correspondence-report.private.json
```

The checkpoint receipts its feature order, thresholds, corpus ID, seed, and
disjoint train/test groups. Poor held-out results are a useful outcome: they
show which assumptions require better features or additional reviewed families
before Clover deployment.

A single customer corpus is useful for falsifying assumptions, not for proving
generalization. Promotion requires the same candidate-recall and held-out-family
gates on at least one structurally different private corpus. The public suite
also exercises document `mention_set` and external `event_stream` populations
so the core contract cannot quietly collapse back into a PostgreSQL table model.

## Clover/Hutch boundary

The first learned implementation remains the two checkpoint families in
`docs/BUSINESS_TOPOLOGY.md`:

- `semantic_population_v1`: existing text embedding plus calibrated
  multi-label role and source-motif heads.
- `semantic_correspondence_v1`: deterministic pair features plus learned
  correspondence scores and calibrated abstention.

They should be batched, unlaned Hutch specialists. PostgreSQL retains tenant
state, local fingerprints, review decisions, and exact packet receipts; Clover
receives only validated model packets. A trained model must beat this harness
on held-out families before the first persistent customer excavation is queued.
