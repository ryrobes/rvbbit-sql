# Hosted Appliance Bootstrap and Administration

Status: implementation contract, 2026-08-11

This document defines the hosted-first path from a newly deployed RVBBIT stack
to a useful company brain. The intended experience is not a conventional setup
wizard. It is a steered Calliope session with a visible setup state, secure
input controls, active probes, and durable receipts. The same mechanism becomes
the normal administration surface after first boot.

## Product boundaries

- Calliope is the customer UI and the setup guide. Datarabbit remains an
  advanced diagnostic surface, not a required administration path.
- The hosted operator supplies the LLM, Clover embeddings, Hermes, and Hermes'
  self-hosted Hindsight memory component before the customer begins setup.
- Hindsight is only Hermes memory. It does not use Hindsight's hosted data
  service: its state lives in the RVBBIT Postgres `hindsight` schema and its
  processing uses the configured Clover embedder plus hosted or zero-data-
  retention LLMs. It is not the company brain.
- The company brain is the separate RVBBIT/Calliope observation, knowledge-
  graph, semantic-search, and governed-agent-tool system.
- Mirrored company data is created with `USING rvbbit`, which registers it as
  an acceleration target and immediately normalizes physical storage back to
  ordinary Postgres heap. Optional Parquet materialization remains a later,
  independent policy.
- The appliance has one extension-enabled RVBBIT database. Company sources,
  company-brain data, control metadata, and receipts live in named schemas in
  that database; the mirror worker cannot target a second Postgres database.
- A hosted single-company appliance starts with one trusted reporting boundary.
  Hermes can read and reason over every non-secret company schema and RVBBIT
  metadata surface in its local RVBBIT database. Hermes does not connect to or
  introspect a production source system during normal use. Only the dlt mirror
  worker uses the remote read-only credential; onboarding asks the admin
  controller to run bounded discovery and returns a redacted catalog/receipt
  for Calliope to help interpret. Per-source user roles and RLS are optional
  later policy, not bootstrap-generated plumbing.
- First boot requires one configured human administrator, not organization-wide
  Google Workspace delegation. Google- or Postgres-backed sessions show
  provider verification. The interim hosted-password path is accepted only as
  an explicitly reported, exact one-email bootstrap boundary; it must not be
  presented as federated identity. Google Workspace and Meet are advanced
  projects.

## Provisioned first boot

The hosted appliance has one Compose entrypoint:
`docker/docker-compose.calliope.yml`. It includes Postgres/RVBBIT, Duck,
DataRabbit, Warren, the baseline Python/MCP/dlt runtimes, Warehouse/Calliope,
the unified origin, a pinned Hindsight 0.9.0 service, and a Calliope-configured
Hermes overlay on an immutable upstream image digest.

The host runtime environment is `/opt/rvbbit/.env` (owner-readable only, mode
`0600`). `docker/calliope.env.example` is the non-secret contract. The
provisioner generates every internal password, wrapping key, JWT, gateway
token, and Hermes bearer independently. The operator adds the hosted inference
credential only as:

```dotenv
RVBBIT_CLOVER_KEY=clv_...
```

The key is never baked into an image or Compose file. Postgres needs it for
semantic SQL, Hermes needs it for the `calliope` chat model, and the isolated
Hindsight process needs it for the same ZDR LLM and Clover embedder. Internal
session/auth secrets must not reuse it. Start or update the appliance with:

```bash
docker compose --env-file /opt/rvbbit/.env \
  -f docker/docker-compose.calliope.yml up -d
```

Hosted bootstrap fails closed if the key or an explicit RVBBIT product image
pin is missing. On fresh and existing volumes it applies the `managed/clover`
SQL embedded in the pinned Postgres image, then actively verifies the Clover
embedding backend and that the key can see the `calliope` model. It does not
download a mutable install script during appliance boot.

All hosted text embeddings use Clover's TLS-protected OpenAI endpoint,
`https://clover.rvbb.it/v1/embeddings`. The RVBBIT backend named `embed`
omits `model`, allowing Clover to select its canonical `embed` alias. Hindsight
sends the explicit alias because its OpenAI SDK requires a model value. Both
paths resolve to the same 1,024-dimensional onboard embedder; image and time
series embeddings remain separate modality-specific vector spaces.

Calliope spoken-response rewriting also stays on the managed Clover path. Warehouse
uses `rvbbit.clover_llm_apply` before sending the resulting short script to ElevenLabs;
it does not use the generic `rvbbit.summarize` operator or require an appliance-owned
OpenRouter credential. A Clover failure degrades to a deterministic local digest.

Hindsight retains an independent healthcheck and restart policy, but Hermes
only waits for the memory service to start. A temporary embedding-provider
outage therefore appears as degraded Hermes memory without taking the Calliope
setup/admin surface offline.

### Advanced Burrow diagnostic surface

DataRabbit ships as an authenticated diagnostic console in every hosted stack.
Compose pins Lens to `RVBBIT_MODE=burrow` and publishes the unified origin a
second time at `RVBBIT_BURROW_PORT` (default `127.0.0.1:3001`). This is an alias
of the same Caddy target used by Calliope, not a second web or identity service:
GET `/login` is rendered by Lens, while login submission, Google OAuth,
`/auth/whoami`, and MCP OAuth endpoints all go to the one Warehouse process and
its durable auth state. Lens holds no JWT or OAuth client secret; it validates
the browser's `wh_session` with Warehouse. Because browser cookies are scoped
to a host rather than a port, the normal Calliope session is also the Burrow
session when both entrypoints use the same hostname.

The debug port is intentionally loopback-only. Reach it without widening the
customer network surface:

```bash
ssh -L 3001:127.0.0.1:3001 appliance-host
# then open http://127.0.0.1:3001/
```

The hosted default `WAREHOUSE_AUTH=shared` treats the authenticated email as an
audit identity, not a PostgreSQL role, and executes through the appliance's
pinned database connection. If an installation later selects
`WAREHOUSE_AUTH=pg`, the same Lens build preserves classic Burrow behavior and
uses the mapped session subject with `SET LOCAL ROLE` for GRANT/RLS enforcement.
The raw Lens listener (default `127.0.0.1:3000`) is not the supported entrypoint
because it cannot route login and OAuth actions to Warehouse.

## The setup workspace

`/calliope/setup` is a special first-boot presentation of the real Calliope
notebook. It uses the same conversation, turns, stage surfaces, composer, and
artifact/query renderers; it is not a second chat implementation. The ordinary
session rail and global navigation are removed from this presentation.

1. The left progress rail replaces the session list and acts as the table of
   contents: deployment health, administrator, company profile, databases,
   documents, services, first brain evidence, and launch review. Each item is
   `current`, `upcoming`, `ready`, or `optional`. Active service and controller
   panels carry the finer testing/blocked state and remediation detail.
2. The center stage continues to show query results and artifacts, and also
   renders bounded table pickers, approvals, connection tests, and secret-entry
   forms requested by Calliope. Secret values are posted directly to the
   responsible administration endpoint, cleared from the browser, and replaced
   with a credential reference plus a redacted test receipt. The value is never
   appended to the chat message or Hermes request.
3. The right side remains the real Calliope conversation. Selecting a progress
   item steers the composer; Calliope explains, validates, troubleshoots, and
   chooses the next appropriate stage control.

The conversation explains requirements, asks business-level questions,
interprets failures, and proposes the next action. A user can later ask the
ordinary Calliope chat, for example, "connect Linear", "change the ERP refresh",
or "why did last night's mirror fail" and enter the same governed flow. The
special setup route teaches the interaction model and handles first boot; it
does not become a permanent second admin application.

Every mutating administration action follows this lifecycle:

```text
discover -> propose -> review -> apply -> probe -> verify -> receipt
                         ^                    |
                         +------ repair <-----+
```

Calliope may perform discovery and validation. It may prepare a change, but an
administrator approves the concrete plan before credentials, schedules, or
connectors are changed. Retrying a step is idempotent and resumes the same
setup item rather than creating a second configuration. Its hidden setup
notebook is durable and separate from the user's ordinary session list.

## Hosted first-boot path

The smallest useful bootstrap is intentionally short.

### 1. Deployment preflight

Verify Postgres/pg_rvbbit, Warehouse MCP, Calliope, Hermes and its self-hosted
Hindsight memory component, Clover, the MCP gateway, the dlt mirror worker, and
the configured hosted/ZDR LLM.
Failures are shown as operator-owned or customer-actionable. A customer should
not be asked to diagnose a missing managed provider key.

### 2. Confirm one administrator

`WAREHOUSE_TEAM_BOOTSTRAP_ADMINS` idempotently places the configured address in
the protected Admins Team, and the existing Warehouse allowlist controls who
can sign in. The setup Stage reports the actual assurance: Google-verified
email, Postgres identity, exact one-email hosted bootstrap, or the weaker
shared-password fallback. It does not claim OAuth verification when none
occurred. The hosted deployment should keep both bootstrap and allowed-email
lists to the same single address until federated identity is enabled.

Additional people, groups, Google sign-in, SSO, and domain enforcement are
post-bootstrap administration. This keeps the first useful session independent
of GCP project work.

### 3. Describe the company

Collect the company name, preferred timezone, reporting calendar, terminology,
and a short statement of what the company does. These are ordinary reviewed
configuration, not secrets. Calliope can turn the answers into a proposed
company profile and show exactly what will be saved.

### 4. Connect the first evidence source

The current launch gate is one successful read-only database mirror. Documents
and high-value MCP services such as Linear, meeting notes, and task systems are
visible follow-up projects, but they do not hold first boot hostage.

For a database, the user names the connection and enters a read-only SQLAlchemy
DSN in the secure rail. The dlt worker stores it under a canonical reference,
probes connectivity, discovers schemas/tables without returning the DSN, and
lets Calliope propose a bounded mirror plan. The administrator reviews tables,
destination names, load mode, and cadence before the first run.

### 5. Verify the company brain

Ask one useful question against the newly local data and select the resulting
query surface. It qualifies only when Warehouse recorded non-empty rows, SQL,
and direct `warehouse_objects` lineage to a table with a successful dlt
table-run receipt. The administrator records what they checked. The proof
receipt stores the question, review note, hashes, row/column counts, and local
relation names—not the result rows or source credential.

### 6. Launch review

Show what is connected, what runs in the background, who can administer it,
which optional projects were deferred, and how to reopen each item. Setup mode
then closes. Future administration starts from normal Calliope chat using the
same tools and stage controls, after the user has learned the interaction model.

## Canonical credential contract

Migration `0286_canonical_credentials.sql` introduces one reference-addressed
credential store for backend, MCP, mirror, and future connector secrets.

- Postgres stores only AES-256-GCM envelopes and non-secret metadata. The
  immutable credential reference is authenticated with the ciphertext, so an
  envelope cannot be moved to another reference and still decrypt.
- The root wrapping material is supplied outside the database through a
  mounted key file or deployment secret. It must be generated independently of
  JWT, database, and gateway tokens. `RVBBIT_CREDENTIAL_KEYS[_FILE]` supports a
  primary key followed by old decryption keys during rotation. With `NEW,OLD`
  configured, `rvbbit.rewrap_credentials()` re-encrypts every active envelope
  under `NEW`; after its receipts are verified, `OLD` can be removed.
- References are stable handles such as `mcp/linear/LINEAR_API_KEY` and
  `mirror/erp_core/SOURCE_DSN`. Configuration and prompts use the handle, never
  the value.
- Create, rotate, resolve, migrate, and revoke operations produce metadata-only
  audit events. MCP revocation keeps a versioned, ciphertext-free tombstone so
  a same-named gateway environment fallback cannot silently reactivate after a
  restart. Error responses and worker logs redact connection strings.
- Hosted deployments set `RVBBIT_REQUIRE_CANONICAL_CREDENTIALS=true`; missing
  key material or database access fails closed.

The MCP gateway now treats the canonical store as authoritative. At startup it
can copy missing values from its older encrypted file without overwriting a
newer canonical value. The old file remains only as a rollback artifact until
an administrator verifies the migration and deliberately removes it. Legacy
`rvbbit.secrets` rows have a separate explicit migration function that also
preserves an existing canonical value; migration is never hidden in an
extension upgrade.

### Default appliance trust model

The initial hosted appliance is a trusted company reporting system, not a
multi-tenant database. It does not generate a role per source, schema, or
connector. Hermes and Warehouse need broad access to the company data or the
company brain is artificially blind.

Secret values are the narrow exception because they are infrastructure, not
reporting data. Secret fields bypass the conversation, saved values are never
returned by an admin API, and resolver functions are not advertised as Hermes
tools. The migration's `PUBLIC` revokes are defense in depth and do not require
the rest of the database to adopt a role lattice.

If Warehouse and Hermes deliberately connect as a database superuser, Postgres
cannot provide a cryptographic guarantee that the same process can never call a
resolver. That is an explicit trusted-appliance boundary, not something the UI
should pretend roles have solved. When a customer introduces real users, OAuth,
or differentiated access, the administrator may add a small service-role split
and user/RLS policy based on actual requirements. Hermes should still retain
read access to every schema that forms the company brain.

## dlt mirror contract

Migration `0287_dlt_mirror_control_plane.sql` and the `data/dlt-mirror`
capability provide a pinned worker plus Postgres-owned configuration, queueing,
heartbeats, retries, and table-level receipts.

Initial packaged database families are PostgreSQL, MySQL/MariaDB, SQL Server,
Oracle, and DB2. Each source account should be read-only, preferably pointed at
a read replica, restricted to selected schemas/tables, and require transport
encryption appropriate to that database.

### Canonical data shape

- Control metadata and all destination schemas live in the same current RVBBIT
  database where `pg_rvbbit` is installed. There is no second warehouse DB.
- Every job owns one short, source-oriented schema such as `erp`, `salesforce`,
  `netsuite`, or `erp_finance`. A forced `mirror_*` prefix adds no lineage and
  is not used. Destination schema names are lower snake case and at most 48
  characters so technical names cannot be silently truncated by Postgres.
- Selected source tables retain useful names inside that schema, for example
  `erp.orders` and `salesforce.opportunity`. The exact remote identifier remains
  in `source_table`; the local relation uses dlt's deterministic lowercase
  Postgres spelling (for example, `OrderLines` becomes `order_lines`) so lineage
  always names the relation that was physically created.
- `rvbbit.mirror_lineage` records the authoritative connection, dialect, source
  schema/table, destination schema/table, load mode, physical access method,
  size, and latest run/table receipt. Names stay pleasant while lineage remains
  explicit and queryable.
- Nested values stay JSON rather than being expanded into child-table trees.
- dlt's `_dlt_*` load/state tables are technical receipts and should be hidden
  from normal company-data browsing.
- There is no user-visible raw/staging/cleaned layer. dlt may create an isolated
  technical staging schema for merge/upsert and truncates it after the load;
  default snapshot mirrors do not create business staging tables.
- Every mirrored user table is created with `USING rvbbit` and therefore has a
  live `rvbbit.tables` registry row from first materialization. Physical
  storage remains heap; Parquet is still optional and created only later when
  the table benefits from acceleration.

DataRabbit's Postgres Admin surface includes a read-only Mirrors debugger over
`rvbbit.mirror_lineage` and `rvbbit.mirror_run_status`. It is for table lineage,
worker/run state, row/load receipts, heap-plus-registry verification, and redacted failures;
it is not another dlt configuration UI.

### Load modes

`snapshot` is the default. It replaces the destination from the current source
table, propagates hard deletes, needs no source cursor, and is easiest to reason
about for modest tables. Its cadence and table size must be bounded so it does
not become an accidental source-system load test.

`incremental_upsert` is opt-in for larger tables. It requires a real unique key
and a monotonic update cursor. It applies inserts and updates, but it cannot
infer a hard delete that leaves no source row. Such jobs need source tombstones,
a periodic reconciliation snapshot, or an explicit delete-detection design.

This is mirroring, not an ETL product. Renaming a destination table and choosing
included columns are allowed; business transformations belong in later RVBBIT
models, views, metrics, and company-brain observations.

### Administration tools Calliope needs

The admin controller should expose narrow, receipt-returning operations rather
than arbitrary SQL:

- read setup state and health;
- request a secure credential input control for a declared reference;
- list credential references/status and rotate or revoke one;
- create, probe, and discover a database connection;
- propose and apply an explicit table mirror plan;
- queue a run and inspect its per-table receipt;
- change/pause a schedule and retry a failed run;
- register/probe an MCP service using the existing gateway; and
- ingest/probe a document source and verify company-brain evidence.

Secret-bearing endpoints are UI/admin-controller operations, not Hermes tools.
Calliope can request that the stage render one and can receive its redacted
result, but it cannot supply or retrieve the value.

### Implemented first database slice

The database item on `/calliope/setup` now opens a native control above the
normal Stage ledger. It supports the complete first-source path:

1. an Admins Team member submits a labeled, read-only SQLAlchemy connection URL
   to the same-origin Warehouse controller;
2. Warehouse keeps only host/database lineage metadata and forwards the
   transient value to the authenticated dlt worker, which writes the canonical
   encrypted `mirror/<connection>/SOURCE_DSN` reference;
3. probe and catalog discovery happen inside the dlt worker, never in Hermes;
4. the administrator chooses source tables, recognizable destination table
   names, snapshot or PK/cursor-backed incremental behavior, destination
   schema, and cadence;
5. Warehouse signs the exact credential-free review plan, applies only that
   plan, and queues its first durable mirror run; and
6. credential-free connection, probe, discovery, and applied-plan receipts are
   appended to the hidden setup notebook as `setup_control` Stage strata.

The password field is cleared immediately after `fetch` begins. The DSN is not
placed in Calliope state, local/session storage, a chat turn, a Stage payload,
or an API response. Mutations require both Admins Team membership and a
setup-session-bound request token returned only to the authenticated setup
page. `RVBBIT_MIRROR_TOKEN` must be set on both Warehouse and the dlt worker;
the setup controller fails closed when it or the canonical credential wrapping
key is missing. The worker's HTTP administration endpoints also fail closed
when its token is absent, while its health receipt reports the missing setting
so Calliope can distinguish Warehouse-side and worker-side configuration.
Warren registers `data_mover` sidecars in the shared runtime
inventory, so the controller normally resolves `dlt_mirror` without a hardcoded
URL. `WAREHOUSE_DLT_MIRROR_URL` is an explicit external-worker override.

## Google is a set of advanced projects

"Google integration" should not be one bootstrap checkbox. It contains
different trust and configuration boundaries:

1. Google sign-in proves a human identity through an OAuth web client. It does
   not require a service account, but domain restrictions, redirect URIs, and
   consent-screen policy still need deliberate configuration.
2. Per-user Calendar/Drive/Docs access uses user consent and narrowly scoped
   refresh tokens. It should be added when that person wants the private
   feature, not demanded at appliance claim time.
3. Organization-wide background access generally requires a company GCP
   project, service-account JSON, Workspace domain-wide delegation, approved
   scopes, and a Workspace administrator who selects the impersonated account.
4. Meet transcripts add recording/transcription policy, storage/location,
   organizer permissions, discovery, and ingestion details on top. They deserve
   their own guided project and validation checklist.

The initial administrator therefore uses verified email. The company can later
open "Set up Google Workspace", with Calliope tracking GCP and Workspace steps,
collecting the JSON only through a secure stage form, probing every scope, and
leaving a durable receipt. A meeting-notes MCP such as the pilot's existing
provider can supply value earlier without blocking the rest of the appliance.

## Image and release strategy

The hosted release should be a versioned appliance manifest, not a collection
of floating image tags.

- Pin RVBBIT, Warehouse/Calliope, MCP gateway, dlt worker, Hermes, and Hindsight
  by immutable image digest and record upstream source revisions.
- Package small Hermes compatibility changes as a reproducible patch/overlay on
  a pinned upstream revision. Use a maintained fork only when the overlay can no
  longer be applied and tested reliably; either path gets a scheduled upstream
  rebase test.
- Package Hindsight as a separate pinned local service configured for the
  RVBBIT `hindsight` schema, Clover embeddings, and hosted/ZDR processing. Do
  not substitute Hindsight's hosted service or an in-process library call.
- Build images in CI, emit an SBOM/provenance record, scan them, run an empty-
  appliance bootstrap smoke test, and promote the exact manifest through
  environments.

The OSS bootstrap can reuse the same setup-state and secure-input machinery
later, but starts with an additional provider bootstrap before Calliope/Hermes
can guide the remaining work.

## Delivery sequence

### Foundation and first database slice: now implemented in this branch

- canonical encrypted credential envelopes, metadata, audit receipts, rotation
  key support, and explicit legacy backend migration;
- MCP gateway canonical-store bridge with encrypted-file compatibility and
  restrictive key-file permissions;
- dlt mirror schemas, schedules, queue/claim/heartbeat/retry lifecycle, and
  table receipts;
- pinned dlt sidecar capability with probe, discovery, secure credential,
  queue, and status APIs;
- a `/calliope/setup` shell that reuses the production Calliope chat/stage,
  keeps one durable hidden setup notebook per administrator, replaces the
  session rail with a receipt-derived checklist, and keeps setup notebooks out
  of the ordinary rail;
- the native secure database Stage controller described above, including
  admin/session-bound mutations, worker-side fail-closed authentication,
  connection probe, remote catalog discovery, table selection, signed plan
  review, durable mirror queueing, run polling, and credential-free receipts;
- Warren registration and health probing for `data_mover` runtimes; and
- successful disposable-source tests of snapshot delete propagation,
  incremental upsert, JSON preservation, and RVBBIT-registered heap
  destinations.

### First useful boot: now implemented in this branch

- an active managed-service preflight for Postgres/pg_rvbbit, Warehouse,
  Calliope, the canonical credential store, Hermes/model/gateway readiness,
  Hermes' local Hindsight memory component, Clover, MCP gateway, and dlt;
- an administrator assurance panel that reuses the protected Admins Team and
  reports the actual Google, Postgres, exact one-email, or shared-password
  trust state;
- a typed singleton company profile with signed review/apply, versioning,
  timezone validation, reporting calendar, terminology, and initial business
  questions;
- a source-backed proof selector restricted to non-empty query surfaces with
  lineage to successfully mirrored, RVBBIT-registered local heap tables,
  followed by a human approval and bounded receipt; and
- a signed final launch review backed by a durable singleton launch state. It
  cannot launch until preflight, administrator, profile, successful mirror, and
  evidence proof gates pass. The hidden setup notebook remains reopenable.

Migration `0291_calliope_first_boot.sql` owns the two typed singleton tables.
Conversation turns and redacted Stage surfaces remain the detailed audit trail.
Company/profile fields and proof-form drafts survive background status polling;
secret values remain confined to their dedicated controller fields.

### Ongoing mirror and MCP credential administration: now implemented

- Database mirrors appear in the ordinary Calliope Library inventory with
  cadence, table count, next run, latest dlt run status, row/table receipts,
  and redacted failure detail.
- Admins can ask normal Calliope chat to run a mirror, retry the exact latest
  failed/partial/cancelled run, pause future runs, resume it, or change its
  cadence. Calliope searches the same Action Library, creates a durable redacted
  receipt, applies immediately against the frozen job/latest-run revision,
  and verifies the local control plane. Pausing does not pretend to cancel a
  load that is already queued or running.
- Each registered MCP server has an admin-only credential action. Calliope can
  reason over names, versions, status, and timestamps, but add/rotation values
  are accepted only by the native Library password control. Revocation needs no
  value. The gateway requires canonical mode, applies an atomic expected-version
  check, evicts its runtime cache, and returns only metadata. Revocation verifies
  a ciphertext-free tombstone; rotation also runs an active MCP probe.
- These actions recheck protected Admins Team membership at execution time.
  Their existing `calliope_action_runs` records are the durable audit,
  progress, verification, failure, and rollback receipts; no parallel admin
  database or new role lattice is introduced.
- Hermes and direct assistant paths operate only on local mirror control rows.
  They never receive the source DSN or contact the remote database.
- Admin Calliope sessions have a local RVBBIT DBA fallback for SQL-native
  administration not covered by a typed action. `SELECT`/`WITH` calls—including
  RVBBIT settings and catalog functions—execute directly on the local writable
  connection. Explicit DDL/DML is frozen and requires one approval before the
  exact receipt executes. Host/instance escapes, credentials, foreign servers,
  and dblink remain outside this lane; source access remains dlt-only.

### Next: remaining administration surfaces

1. Replace remaining direct `rvbbit.set_secret(...)` and gateway-only secret
   entry paths with canonical reference-based controller calls.
2. Add MCP service and document-source controls, beginning with task systems
   and meeting notes rather than making Google Workspace a launch dependency.
3. Before hosted release, add endpoint rate limits and explicit request-log
   redaction tests, then run the empty-appliance browser/bootstrap smoke suite.
4. Keep one broad trusted reporting/data boundary. Offer service/user roles and
   RLS later when a customer's identity and access model makes them useful.

### Then: reproducible appliance images

Inventory the pilot's exact Hermes/Hindsight source revisions and patches,
codify the images, add compatibility tests, and produce the immutable appliance
manifest. After that, extract the remaining valuable pilot connectors one at a
time instead of copying the host wholesale.

### Later: advanced identity and sources

Add Google sign-in conversion, per-user Workspace grants, organization-wide
delegation, and Meet transcript ingestion as separate guided projects. The OSS
provider bootstrap follows once the hosted experience is stable.

## Release gates

A hosted appliance is not ready until:

- a clean deployment can reach its first cited company-brain answer with one
  explicitly assured admin and one evidence source, without Datarabbit or
  shell access; hosted-password bootstrap must remain an exact one-email gate
  until provider-verified identity replaces it;
- credential values never enter a Calliope/Hermes prompt, tool result, browser
  storage, database metadata, receipt, trace, or log, and Postgres stores only
  ciphertext at rest;
- missing wrapping keys/tokens fail closed and key rotation is exercised;
- snapshot failure/recovery, incremental retry, hard-delete expectations,
  schema drift, large tables, and each packaged database driver have tests;
- setup can resume safely after browser, worker, or container interruption;
- every applied configuration has a redacted receipt and a supported rollback
  or disable action; and
- Google Workspace and Meet can remain deferred without reducing the core
  appliance to an unhealthy state.

## Immediate pilot remediation

Read-only pilot inspection confirmed that Datarabbit's MCP secret UI forwards
values to the MCP gateway and that the gateway's encrypted file was the active
store. It also confirmed that the pilot's Hindsight service uses the RVBBIT
Postgres `hindsight` schema. No pilot state was changed.

During that inspection, a multiline Google service-account credential was
accidentally emitted into the private tool transcript while enumerating
environment variable names. Treat that service-account key as compromised:
revoke it in GCP, create a replacement only if the integration is still needed,
update the pilot through its secret path, and verify old-key rejection. Do not
reuse the exposed key in the appliance image or canonical migration.
