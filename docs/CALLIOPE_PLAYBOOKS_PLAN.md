# Calliope Playbooks

Status: Slices 0–3 implemented, including personal inventory and Dream drafting  
Decision: replace user-authored Workflow graphs with learned, versioned Playbooks

## The idea

A Playbook is Calliope's reusable procedural memory: a governed description of
how a person or company gets a useful kind of work done. It is extracted from
successful collaboration instead of assembled in a blank workflow builder.

It is deliberately fuzzy. A Playbook preserves the outcome, relevant context,
judgment, guardrails, expected delivery, and definition of success. It does not
replay a fixed sequence of tool calls. Calliope chooses the concrete execution
path at run time based on the tools, data, permissions, and situation available
then.

This completes a simple memory model:

| Primitive | Question it answers |
| --- | --- |
| Brain and knowledge graph | What does the company know? |
| Chats and run notebooks | What happened? |
| Playbooks | How do we do this kind of work? |
| Assignments | What should happen, for whom, and when? |
| Work Inbox | What needs human attention? |

The product promise is repeatability at the level that matters: repeatable
intent, reasoning, controls, and quality—not brittle reproduction of yesterday's
API calls.

## Product vocabulary

- **Capability** is anything Calliope can use to accomplish work.
- **Atomic capability** is a tool, SQL operator, MCP call, model, cube, metric,
  or other direct affordance.
- **Playbook capability** is a learned method that composes atomic capabilities
  with organizational judgment.
- **Assignment** is a durable commitment that may invoke a Playbook once or on a
  schedule. An Assignment may also remain open-ended and use no Playbook.
- **Run** is one evidence-bearing attempt.
- **Action** is a controlled mutation, with approval and authority independent
  of the Playbook that suggested it.

`Playbook` is the user-facing term. Internally it may behave similarly to an
agent skill, but using `Skill` in the UI would collide with provider and agent
framework terminology.

## Core principles

1. **Useful work comes first.** The normal authoring source is a successful chat,
   investigation, Brief, or Assignment run.
2. **Conversation is the editor.** Users refine Playbooks with Calliope rather
   than manipulating execution nodes and edges.
3. **Outcome over replay.** The method is stable; tool selection remains adaptive.
4. **Private by default.** A newly distilled Playbook belongs only to the human
   who created or accepted it until they share it.
5. **No silent learning.** Calliope may draft and suggest changes, but approval
   creates a usable version.
6. **Evidence remains attached.** Every draft and revision retains its source
   conversations and successful or failed runs.
7. **Permissions are evaluated twice.** Discovery is identity-filtered, and all
   referenced data/tools are authorized again when used.
8. **The visual is documentation.** A Sketch compresses the spirit of the method;
   it is not executable geometry.

## Non-goals

- A deterministic DAG scheduler or general-purpose orchestration engine
- A blank low-code workflow canvas
- Exact replay of model messages or tool calls
- Automatic mutation authority inherited from a Playbook
- Injecting every Playbook into every prompt
- Registering one physical MCP tool for every Playbook

## Canonical Playbook

The authoritative representation should be a compact semantic contract. The
exact schema can evolve, but each immutable version should retain:

### Identity and governance

- Stable Playbook UUID and capability identity
- Human-readable name and short synopsis
- Owner email
- Visibility: private, selected people, selected Teams, or Everyone
- Draft, approved, superseded, or archived lifecycle
- Version number and change summary
- Created/approved timestamps and actors

### Applicability

- Plain-language description of when the Playbook is useful
- Trigger phrases and concepts used for retrieval
- Intended outcome
- Explicit non-applicability or counterexamples
- Optional business area, entities, artifact types, or time horizon

### Method

- Context to gather or prefer
- Important questions to answer
- Decision heuristics and company-specific judgment
- Guardrails and approval boundaries
- Expected deliverable
- Validation and completion criteria
- Fallback behavior when a preferred capability or source is unavailable

### Capability dependencies

- Required capabilities
- Preferred capabilities
- Optional enhancements
- Capability-search queries that may resolve changing implementations at run time
- Readiness state: ready, degraded, or blocked, with reasons

### Provenance and learning

- Source Calliope session and turn range
- Source Assignment or legacy Workflow run, when applicable
- Successful-run examples
- Known failure modes and corrections
- Last-used and success evidence
- Dismissed or rejected suggestion evidence, so Dreaming does not nag

### Documentation projection

- Current generated synopsis
- Current Sketch surface/reference
- Sketch revision from which a semantic change was last proposed

Private source content should not be copied into the searchable description.
The Playbook stores opaque references and a safe synopsis; access to source
documents, Sheets, sessions, and artifacts is checked again at run time.

## Authoring lifecycle

### 1. Work normally

The user and Calliope solve a problem in an ordinary chat or run notebook. No
special mode or up-front process design is required.

### 2. Distill

The user clicks **Save as Playbook**, or says something such as “remember how we
did this.” Calliope receives the authenticated session identity internally and
examines the relevant conversation, tool receipts, selected surfaces, outputs,
and corrections.

Calliope creates a private draft containing:

- what was accomplished;
- when the method appears useful;
- the durable reasoning and guardrails;
- requirements and validation;
- evidence supporting the draft; and
- a generated explanatory Sketch.

The extraction should omit accidental details such as transient IDs, exact tool
ordering, failed exploratory calls, and values that belong to one occurrence.

### 3. Review conversationally

The draft appears as a compact Playbook card and Sketch in the Stage. The user
can approve it directly or tell Calliope what is wrong. Calliope proposes the
corresponding semantic revision in plain language.

Freehand Sketch edits remain annotations until Calliope interprets them. For a
meaningful visual change she asks a confirmation such as:

> It looks like manager approval should happen before publication. Update the
> Playbook that way?

### 4. Approve

Approval creates an immutable usable version. Drafts are never returned as
approved capabilities unless the current owner is explicitly working on that
draft.

### 5. Reuse

An approved Playbook can be:

- found automatically during an ordinary chat;
- explicitly invoked by name;
- run once;
- attached to an Assignment;
- shared with a person or Team; or
- considered by Briefs and other agentic surfaces when relevant.

### 6. Learn without silently drifting

After a materially different run, Calliope may propose a new version:

> The source changed and I used a safer validation step. Update this Playbook?

The current version remains intact until accepted. Runs pin the exact version
they used so their evidence stays interpretable.

### 7. Find it again personally

Playbooks remain indexed in Library as capabilities, but Library is not the
ordinary user's filing cabinet. **My Playbooks** lives beside Work Inbox and
Assignments and separates methods that need review, methods ready to use, and
methods explicitly shared with the current person. Draft and approval changes
also update one deduplicated Work Inbox handoff; they do not create an endless
feed of receipts.

### 8. Dream a method worth keeping

Personal and Company Dreaming may propose `artifact_type = playbook` when its
grounded evidence reveals a recurring successful method, repeated
investigation, or useful way of working. The Dream must carry the same complete
typed semantic contract as an ordinary Playbook, and the Dream UI renders that
contract as a read-only Playbook review rather than prose sections.

Acceptance is an explicit human action. It creates one idempotent, approved,
private Playbook with an immutable source version, Dream evidence reference,
generated visual-method Sketch, capability projection, and personal Inbox
receipt. Acceptance never shares the method; Team or person access remains a
separate deliberate action. Malformed or incomplete model proposals are kept as
ordinary analysis Dreams and cannot enter the one-click acceptance path.

## Capability discovery

Playbooks belong in the existing capability graph as
`kind = 'cap_playbook'`. `capability_search()` already answers “what can this
Calliope do?” and Calliope calls it repeatedly during real work; a separate
Playbook-search ontology would duplicate that machinery.

A Playbook search document should safely include:

- title and short synopsis;
- applicability and trigger language;
- owner/visibility label appropriate for the caller;
- current approved version;
- required and preferred capability names;
- readiness state;
- safe success evidence; and
- the opaque reference used to load the full authorized contract.

Ranking should prefer a closely matching personal or Team Playbook over a bag of
lower-level tools, while still returning atomic capabilities when they are the
more direct answer. Playbooks must not swamp unrelated catalog results.

Discovery and loading are separate:

1. `capability_search()` finds an authorized `cap_playbook` result.
2. A generic capability-detail resolver loads the full version.
3. Calliope applies it as contextual method guidance.

There is no MCP tool per Playbook. A very small generic read/apply surface avoids
tool-list growth and lets permission checks stay centralized.

## Identity-aware capability access

Migration `0281_identity_scoped_capabilities` establishes the compatibility
model:

- No policy row means **legacy Everyone**. Existing operators, packs, MCP tools,
  syntax entries, metrics, cubes, and other current results remain discoverable.
- An explicit `everyone` policy means any verified human application subject.
- An explicit `restricted` policy means the owner, specifically granted people,
  or members of specifically granted flat Teams.
- The protected Everyone Team remains a dynamic verified-user wildcard.
- Archived Teams do not grant access.

The Warehouse MCP `capability_search` tool keeps its public signature of
`query`, `limit`, and `kinds`. Warehouse supplies the subject from the frozen
request authorization context; Calliope cannot pass or forge an email argument.

Trusted identity sources are:

- a direct signed OAuth human;
- a Google Chat sender delegated through the dedicated Hermes service credential
  and allowed Workspace audience;
- a Calliope browser owner joined from Warehouse's signed session ledger; and
- for discovery only, the owner of a managed Assignment or legacy Workflow cron
  joined from Warehouse's durable job ledger.

Legacy shared-key forwarding remains attribution only. Arbitrary cron metadata,
service callers, and model-provided emails do not become human subjects. A
managed cron owner recovered for discovery does not thereby receive mutation
authority.

Raw SQL has no trustworthy application identity. Its compatibility
`rvbbit.capability_search()` returns all ungoverned entries but fails closed for
explicitly governed entries. The Warehouse wrapper uses
`rvbbit.capability_search_for()` with its server-resolved subject.

The policy tables and `capability_can_use()` function are intended to become the
single decision primitive for both discovery and execution. Semantic
`capability_search`, `search_tools`, and `get_tool_help` now share that decision
and inherit the same server-resolved identity. Hidden names in exact tool-help
lookups are reported as missing rather than forbidden. Before the first MCP tool
is actually marked restricted, enforcement must still be added to raw MCP
`tools/list` results and the invocation boundary; filtering convenience
discovery alone is not a security boundary.

Playbook owners control their own Playbook audiences with optimistic revisions;
future platform-level MCP tool restrictions remain Admin-controlled. Both write
to the append-only `capability_access_events` ledger.

## Runtime application

When Calliope considers a Playbook she should:

1. Search under the current authenticated identity.
2. Load the selected immutable version under the same identity.
3. Resolve required and preferred capabilities available now.
4. Report degraded or blocked requirements instead of fabricating substitutes.
5. Gather only context the current caller may access.
6. Adapt the method to the present data and tools.
7. Honor independent action/approval policy.
8. Validate against the Playbook's completion criteria.
9. Record the Playbook ID/version, capabilities actually used, deviations,
   result, and validation evidence in the run.
10. Suggest—not silently apply—an improvement when warranted.

Calliope should briefly disclose meaningful reuse, for example “I’m using your
Weekly Enrollment Review Playbook,” without turning every response into process
ceremony.

## Assignments

Assignments own operational state: who requested the work, timing, notification
policy, overlap handling, run notebooks, and Work Inbox delivery. A Playbook owns
only the reusable method.

An Assignment may:

- remain open-ended with no Playbook;
- pin an exact Playbook version for controlled repeatability; or
- explicitly follow the latest approved version.

Following latest must be a visible choice. A scheduled job should not silently
change behavior merely because someone approved a new method version.

The existing `execution_kind = 'workflow'`, `workflow_id`, and
`workflow_version` work-order fields provide a compatibility seam. They can be
migrated or aliased to Playbook semantics without rebuilding the Assignment
lifecycle.

## Sketches

The Sketch is a compressed explanation of the Playbook's spirit. It may show:

- the business-language phases;
- important inputs and outputs;
- decision points;
- approval boundaries;
- alternate paths; and
- human annotations.

It should avoid pretending to be an exact orchestration graph. Labels such as
“Understand the current pipeline,” “Investigate meaningful gaps,” and “Escalate
only material changes” are preferable to literal MCP function nodes.

The canonical Playbook generates the Sketch. A Sketch edit can become evidence
for a proposed Playbook revision, but arrows and coordinates never execute
directly.

## User experience

There is no blank builder.

### In a chat or run

- A visible but quiet **Save as Playbook** action serves as reminder and entry
  point.
- Calliope can perform the same action conversationally.
- After substantial successful work she may unobtrusively ask whether the user
  wants to remember the method.

### In the Library

A Playbooks capability type provides a small management surface, not an authoring
studio:

- search and browse;
- inspect synopsis and Sketch;
- see owner, audience, readiness, and last use;
- view immutable versions and evidence;
- share or restrict;
- archive; and
- ask Calliope to revise, run, or assign it.

The old Workflow builder, graph controls, scheduling controls, and duplicated run
management should leave ordinary navigation. An Advanced compatibility inspector
may remain temporarily for old records and debugging, but it is not part of the
new creation journey.

## Dreaming as hands-off discovery

Personal Dreaming is a strong Playbook proposal source. It already synthesizes a
private dossier of what an active person asks, builds, investigates, corrects,
and repeats. A Dream pass can detect a recurring successful method that the user
has not thought to save.

A proposed personal Playbook must:

- be private to that user;
- cite the supporting chats, Assignments, or runs;
- explain why the pattern appears reusable;
- meet a recurrence or evidence threshold;
- deduplicate against existing Playbooks and prior dismissed suggestions;
- arrive as a Dream suggestion or private draft, never as approved capability;
- never schedule itself; and
- never broaden its own visibility.

Good suggestion:

> You have reviewed enrollment risk in a similar way three times, and the last
> two runs used the same validation checks. Draft a personal Playbook?

Bad suggestion:

> I inferred a company process from one conversation and published it.

Global/Admin Dreaming may propose company-level candidates, but publication and
audience selection still require an authorized human decision.

## Transition from Workflows

Do not destructively delete the existing Workflow tables, versions, runs, or
routes. They contain useful execution evidence and already implement several
needed mechanics.

Transition strategy:

1. Remove or hide the blank Workflow builder from the normal UI.
2. Introduce Playbook persistence and `cap_playbook` indexing alongside the old
   schema.
3. Convert a legacy Workflow version into a Playbook draft by distilling its
   goal, contexts, requirements, rules, outputs, and successful runs. Do not
   preserve graph shape merely for compatibility.
4. Preserve old run notebooks and source links as evidence.
5. Convert enabled Workflow schedules into Assignments pinned to the converted
   Playbook only after owner review.
6. Keep old API routes available during migration, then mark them compatibility
   only.
7. Retire the old user-facing Workflow label after all active schedules have an
   Assignment owner.

## Implementation slices

### Slice 0 — identity-aware discovery foundation

- Add optional capability policies and person/Team grants.
- Treat absent policy as legacy Everyone.
- Filter semantic capability search and both tool-discovery helpers using
  server-derived identity.
- Recover managed cron ownership for discovery without granting mutation power.
- Preserve the old tool signature and mixed-image rolling upgrades.
- Add append-only event storage for future audited policy mutations.

### Slice 1 — Playbook persistence and capability indexing

- [x] Add Playbook and immutable version tables.
- [x] Add person/Team view grants and lifecycle receipts.
- [x] Define the semantic contract JSON schema.
- [x] Crawl approved visible Playbooks into the capability graph as `cap_playbook`.
- [x] Remove stale/superseded search documents without deleting version history.
- [x] Add an authorized generic Playbook detail resolver and compact Library/Stage views.

### Slice 2 — distill a successful conversation

- [x] Add a quiet **Save as Playbook** affordance to substantial completed chats.
- [x] Build an immutable evidence packet from the bounded turn range, tool
  receipt outputs, selected Stage surfaces, artifact versions, and the exact
  current Sketch revision when used.
- [x] Let Calliope distill the method into a private draft without giving the
  model authority to approve or share it.
- [x] Review the native Stage card with direct human approval, conversational
  revision/use actions, readiness needs, and links to source evidence.
- [x] Keep source prose out of capability search; non-owners receive provenance
  only when they can independently view the source notebook.

The first browser slice intentionally starts from a completed chat response.
Assignment-run and Brief entry points can reuse the same server-owned evidence
pin contract later instead of inventing another distillation path.

Earlier checklist wording retained for the remaining visual enhancement:

- Build the evidence packet from turns, tool receipts, selected surfaces, and
  outcomes.
- Generate or update an explanatory Sketch when a visual genuinely improves the
  Playbook documentation; never mutate the shared canvas reflexively.

### Slice 3 — runtime reuse

- [x] Teach Calliope to load a relevant approved Playbook returned by
  identity-aware capability search, disclose its title/version briefly, and
  adapt it rather than replaying a tool graph.
- [x] Record the exact loaded Playbook ID/version in the durable response receipt.
- Resolve readiness against current atomic capabilities.
- Record material deviations and validation evidence on each use.
- Add revision suggestions after material divergence.

### Slice 4 — Assignment convergence

- Let an Assignment pin or follow an approved Playbook.
- Move all timing, notification, overlap, and run state out of legacy Workflows.
- Offer **Assign this Playbook** from chat and Library.
- Migrate owner-approved legacy schedules.

### Slice 5 — Dreaming proposals

- Detect recurring successful personal methods from dossier evidence.
- Propose private Playbook drafts with citations and confidence.
- Honor dismissals and suppress duplicates.
- Add Admin-only global candidates without automatic publication.

### Slice 6 — capability execution gates

- Add Admin UI and agent tools for capability audiences.
- Enforce `capability_can_use()` at MCP `tools/list` and invocation; semantic
  discovery, `search_tools`, and `get_tool_help` are already filtered.
- Return a non-enumerating denial that does not disclose hidden capability
  details.
- Audit every policy change and denied attempt as appropriate.
- Add end-to-end tests for direct OAuth, Calliope web, Google Chat, managed cron,
  legacy shared key, Team membership changes, and archived Teams.

## Acceptance criteria

- Existing installations return the same capability results before any policy is
  explicitly created.
- Calliope never supplies an email argument to capability search.
- Two users with different Team memberships receive different governed results
  from the same query.
- Hidden Playbook titles, summaries, Sketches, and source references do not leak
  through search, counts, readiness, or errors.
- A saved chat becomes a useful private Playbook without opening a builder.
- The same Playbook can guide a manual chat and a scheduled Assignment while
  using different concrete tools.
- Every run records the exact Playbook version and observable deviations.
- Sketch changes do nothing operational until translated and approved as a
  semantic revision.
- Dreaming can propose a cited private draft but cannot approve, share, or
  schedule it.
- Legacy Workflow records remain inspectable throughout migration.

## Product shorthand

The feature should be explainable in one sentence:

> Work with Calliope once, save what worked, and let her use that method whenever
> it is useful again—manually or automatically.
