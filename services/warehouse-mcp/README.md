# rvbbit Warehouse MCP — Phase 0 prototype

A governed, semantic, time-travel data interface for Claude (Cowork & Code).
Design: [`docs/WAREHOUSE_MCP_PLAN.md`](../../docs/WAREHOUSE_MCP_PLAN.md) ·
tool spec: [`docs/WAREHOUSE_MCP_PHASE0.md`](../../docs/WAREHOUSE_MCP_PHASE0.md).

Standalone for now (foldable into `rvbbit-mcp-gateway` later). **Phase 0 uses one
read-only connection** — per-user role scoping is Phase 1.

## Tools
| tool | what | backing |
|---|---|---|
| `search_data(query, limit?, schema?)` | semantic search → ranked tables/cols, each grounded with **live samples + per-column stats + freshness/drift** | `data_search` + `pg_stats` + `accel_freshness` |
| `describe_table(table)` | columns + samples + per-column stats + freshness | information_schema + `pg_stats` + `accel_freshness` |
| `list_metrics(category?, search?)` / `get_metric(name)` | the blessed metric catalog | `metric_defs` |
| `metric(name, params?, as_of?, def_as_of?)` | a governed scalar number (bitemporal) | `rvbbit.metric_scalar()` |
| `cube_pivot(cube, rows?, cols?, measures?)` | direct grouped table or crosstab; accepts multiple dimensions and aggregated numeric values without requiring a metric definition | validated, read-only `cubes.<name>` aggregation |
| `validate_sql(sql, as_of?)` | plan, **don't execute** (self-correct loop) | `route_explain` |
| `run_sql(sql, as_of?, limit?)` | **read-only** execute (validate → safe_select gate → run) | engine |

`as_of` (data-time) flows in as the engine's `-- rvbbit: as_of <ts>` directive; the
read-only guard rejects anything that isn't a `safe_select`.

## What's exposed — databases & schemas
The warehouse and rvbbit's own internals live in **one database, different schemas**
(the Temporal Mirror syncs external sources into dest schemas right next to the
`rvbbit.*` catalog). So scoping is by **schema**, not database: `search_data` and
`describe_table` always hide `rvbbit` / `pg_*` / `information_schema`, surfacing only
the data schemas. Set `WAREHOUSE_SCHEMAS` (CSV) to restrict to an explicit allowlist
(e.g. `mirror_sales,mirror_ops,analytics`). To expose data that lives in a *separate*
database, mirror it in (Temporal Mirror) — then it's covered, time-travel and all.
The hard backstop is still the DB role: don't grant `warehouse_reader` SELECT on the
`rvbbit` schema and the internals are unreadable even via `run_sql`.

## Run on the uber stack (Docker)
The image ships in the release set (`ghcr.io/<ns>/rvbbit-warehouse-mcp`) and is wired
into `docker-compose.uber.yml` behind an **opt-in `warehouse` profile** — so a plain
`make release-uber-up` / `docker compose up -d` does **not** start it (by design: it's
an internet-facing endpoint). Bring it up explicitly:
```bash
export WAREHOUSE_MCP_KEY="$(openssl rand -hex 24)"   # required — endpoint won't start without it
make warehouse-up RELEASE_VERSION=<the version you pushed>   # pulls the image, starts MCP + tunnel
make warehouse-url                                          # the public https://<…>.trycloudflare.com URL
make warehouse-down                                         # stop just these two
```
Equivalently, raw compose: `… --profile warehouse up -d` (the `--profile` flag is the
thing that's easy to forget — without it the two services are silently skipped).

## Run standalone (no Docker)
```bash
pip install -r requirements.txt
export WAREHOUSE_DSN="host=... port=5432 dbname=... user=warehouse_reader password=..."

# remote (Cowork + Code): streamable-HTTP, single shared key
export WAREHOUSE_MCP_KEY="$(openssl rand -hex 24)"   # share this with users
python server.py --http        # serves http://0.0.0.0:8765/mcp  (/health is open)

python server.py --selftest    # exercise every tool against the warehouse
python server.py               # stdio (local Claude Code only)
```

### Make it remotely reachable (no open ports, no exposed Postgres)
Run `--http` **next to the warehouse** (DB over localhost) and expose only the MCP
endpoint via a tunnel:
```bash
cloudflared tunnel --url http://localhost:8765      # → https://<random>.trycloudflare.com
# (or a named Cloudflare Tunnel / Tailscale for a stable URL)
```

## Two auth modes
The server picks the mode from `WAREHOUSE_PUBLIC_URL`:

**OAuth (recommended — Claude Desktop/Cowork's native connector).** A self-contained
OAuth 2.1 AS (`auth.py`): the SDK mounts `/authorize`/`/token`/`/register` + the
`.well-known` metadata and verifies PKCE; we supply the `/login` page (a shared
`WAREHOUSE_LOGIN_PASSWORD` + optional `WAREHOUSE_ALLOWED_EMAILS`) and HS256 JWTs. Users
just **paste the URL → log in → Allow** — no header to configure. Needs a **stable
HTTPS URL** (OAuth redirects), so terminate TLS at a proxy.
```bash
export WAREHOUSE_PUBLIC_URL="https://dwmcp.example.com"   # your stable domain
export WAREHOUSE_LOGIN_PASSWORD="$(openssl rand -hex 16)" # the shared login password
export WAREHOUSE_JWT_SECRET="$(openssl rand -hex 32)"     # MUST differ from WAREHOUSE_MCP_KEY
export WAREHOUSE_ALLOWED_EMAILS="a@co.com,b@co.com"       # optional allowlist
python server.py --http     # serves :8765; behind your proxy at WAREHOUSE_PUBLIC_URL
```
> **Security:** `WAREHOUSE_JWT_SECRET` must be independent of `WAREHOUSE_MCP_KEY` — that
> key is handed to users, and reusing it to sign would let any holder forge a token for
> any email. The server **refuses to start** if they match or if either secret/password
> is missing. Login is rate-limited (per-IP lockout + serialized checks).

**Shared key (Claude Code / scripts).** No `WAREHOUSE_PUBLIC_URL`; gate on a static
bearer. Still accepted in OAuth mode too, so Code keeps working alongside the UI flow.
Because a shared bearer contains no user identity, its activity caller defaults to
`static-key`. Set `WAREHOUSE_MCP_STATIC_CALLER` to the legacy service email/name when
one shared integration should retain its old attribution. The audit `client_id` remains
`static-key`, and a verified OAuth token's email always takes precedence over this fallback.

**First-party Hermes delegation.** Give Hermes its own bearer instead of reusing the
legacy/script key:

```bash
export WAREHOUSE_HERMES_MCP_KEY="$(openssl rand -hex 32)"
export WAREHOUSE_HERMES_MCP_CALLER="calliope@example.com" # service actor, not the human
```

An access token presented with this key has `client_id=hermes-service`. Only that
principal can turn Hermes' bounded Google Chat sender envelope, or an opaque Calliope
API session that Warehouse resolves against its own signed session ledger, into an
application authorization `subject`. Chat senders must also pass this Warehouse's
`WAREHOUSE_ALLOWED_EMAILS` and Workspace-domain audience; an adapter-verified external
sender is still not automatically a company user. A legacy `WAREHOUSE_MCP_KEY` holder may still
produce backwards-compatible attribution, but never a delegated authorization subject.
Direct OAuth remains authoritative and ignores any forwarded envelope. This is an
application-layer identity decision only: it does not issue the user's Google token,
run `SET ROLE`, or change warehouse grants.

### Google Sign-In (optional, recommended)
Adds a **Sign in with Google** button to the login page. We stay the OAuth *server*
for Claude and additionally become an OAuth *client* to Google; both paths converge on
one place (`_finish_login`), so a Google-verified address reaches the access token's
`sub` — and therefore `mcp_activity.caller` — exactly like a typed one. It gates the
MCP endpoint **and** everything served behind the same session (`/`, `/d/<slug>`,
`/apps/<slug>`).

This is an identity upgrade, not just convenience: with a shared password the email is
*self-asserted*, so any password-holder can claim any name in your audit log. Google's
`email_verified` claim ends that.

```bash
export WAREHOUSE_GOOGLE_CLIENT_ID="....apps.googleusercontent.com"
export WAREHOUSE_GOOGLE_CLIENT_SECRET="..."
export WAREHOUSE_GOOGLE_HD="acme.com"    # restrict to one Workspace domain
# export WAREHOUSE_GOOGLE_ONLY=1         # later: retire the shared password
```
In the Google Cloud console create an **OAuth client ID → Web application** and register
the redirect URI **exactly**: `<WAREHOUSE_PUBLIC_URL>/auth/google/callback`. (That's a
*web* client — not a service-account JSON, which is a different credential with no human
sign-in. It needs a stable origin, so the ephemeral `warehouse-tunnel-up` quick-tunnel
URL can't be used with Google.)

> **Restricting to one domain — read this.** Passing `hd=` on the authorization request
> is only an account-chooser *hint*; a user can edit it out of the URL, so it is **not**
> a security boundary. The gate is the signed **`hd` claim** on the returned ID token,
> which `WAREHOUSE_GOOGLE_HD` verifies server-side. It's strictly stronger than matching
> the email's suffix — a consumer Google account cannot present an `hd` at all.
> `WAREHOUSE_ALLOWED_EMAILS` still applies on top (exact addresses or `@domain`).
> The server **refuses to start** with Google enabled and *neither* `WAREHOUSE_GOOGLE_HD`
> nor `WAREHOUSE_ALLOWED_EMAILS` set — that combination would let any Google account on
> earth sign in.

The shared password keeps working alongside it (existing users are unaffected) until you
set `WAREHOUSE_GOOGLE_ONLY=1`, which stops `POST /login` from accepting a password at all
— not merely hiding the form. ID tokens are verified, never just decoded: RS256 against
Google's JWKS, audience, issuer, expiry, and a server-planted single-use `nonce`.

When Google Sign-In and Calliope are both enabled, Personal Briefs also expose an
optional **Connect Calendar** control. It uses a separate incremental consent grant for
`calendar.events.owned.readonly`; adding Calendar never silently broadens the sign-in
scope. The same registered callback URI is reused, so no second redirect URI is needed.
The returned Google account must exactly match the signed-in Warehouse identity.
Calliope syncs a bounded window from that user's primary calendar, stores only normalized
event fields, and keeps them in an owner-keyed private overlay rather than shared Brain or
Hermes memory. Google access tokens are never stored. The offline refresh token is Fernet
encrypted with `WAREHOUSE_GOOGLE_TOKEN_KEY` when set, or a key derived from
`WAREHOUSE_JWT_SECRET`; private events are reduced to title `Private event` plus timing
before storage. Disconnect revokes the grant best-effort and deletes the local cache.
On installations without Google Sign-In, the Calendar control, config flag, consent
route, and Calendar API routes are absent/invisible.

Query surfaces also expose a contextual **Sheet** action. Its first use requests a
separate incremental Google Workspace grant with only `drive.file`; sign-in and
Calendar consent still do not imply file access. Calliope can create and update files
it created for that user, but cannot enumerate the rest of their Drive. The same action
resumes automatically after consent, exports the exact frozen result shown on the Stage,
freezes and formats the header, and saves an owner-scoped receipt and link.

With Google Picker configured, the Stage also exposes **Bring in Sheet**. Picker lets the
user explicitly choose one workbook without granting Calliope broad Drive listing access.
Calliope then shows its grid tabs and a bounded row preview; the user can choose an A1
range and whether the first row contains field names. Confirming the import re-reads that
selection, freezes up to 1,000 rows / 50,000 cells in the private notebook, and records workbook, tab,
range, source link, owner, and content hash. It is deliberately a snapshot rather than a
silent sync. The active grid has an explicit **Refresh** action: an unchanged check advances
its freshness timestamp without adding another turn, while changed content supersedes the active
receipt and creates a linked immutable Stage revision that is selected automatically. Selecting
that Stage grid attaches its exact workbook, tab, range, schema, and a bounded row preview to
the current turn. Calliope can page through the rest of that owner-checked immutable snapshot
with `calliope_sheet_snapshot`; it does not silently re-read the live Google file. For analysis,
`calliope_sheet_query` exposes that exact selected snapshot inside one governed read-only statement
as a typed `selected_sheet` relation. Calliope can join it to ordinary warehouse tables, aggregate
it, and place the result back on the Stage without creating a persistent database object. Imported
headers receive stable SQL-safe aliases (`sql_name`), and every result retains the source surface,
snapshot hash/time, original workbook/tab/range, query, and resolved warehouse relations as lineage.
If the user explicitly asks for current/latest/live values, `read_mode=live` performs one bounded
Google API read for that query and records both the saved and observed hashes; it never mutates the
Stage snapshot. Snapshot mode remains the default, and there is no background polling.

**Bring in Doc** uses the same Picker grant for Google Docs. Calliope re-reads the selected
document on confirmation, extracts its current tab-aware text representation, and indexes
up to 250,000 characters as a normal Brain document. A deterministic private role is granted
only to the authenticated uploader, so Brain search, Trails, `brain_get_doc`, and future
Personal Briefs can use it for that owner without placing its prose in company-visible
knowledge. Re-importing the same file refreshes that owner's indexed copy and appends a
linked receipt on the Stage. The active receipt exposes two deliberately explicit lifecycle
actions: **Refresh** checks the live Doc and creates a new linked revision only when its
extracted content changed; **Forget** removes the indexed body, chunks, ACL attachment, and
document KG node while leaving the original Google Drive file untouched. Earlier Stage
receipts remain as lineage but cannot be mistaken for current Brain context. There is no
silent background synchronization in this slice, and sharing or promoting private documents
is intentionally deferred to the broader permissions pass.

Enable the **Google Picker API** plus the surface APIs used by the installation: **Google
Sheets API** for Sheet import/export and **Google Docs API** for private Doc import. Create a
browser API key, restrict its HTTP referrers to the exact public Warehouse origin, restrict
its API access to Google Picker, and set:

```bash
export WAREHOUSE_GOOGLE_PICKER_API_KEY="..."       # public browser key; origin-restrict it
# Optional: GCP project number. Normally derived from the numeric OAuth client-ID prefix.
export WAREHOUSE_GOOGLE_PICKER_APP_ID="123456789012"
```

The Picker key is intentionally sent to the authenticated browser and therefore is not a
secret; its referrer and API restrictions are the security boundary. Short-lived Google
OAuth access tokens are returned only by an authenticated, non-cacheable POST and are
never persisted by Calliope or put in browser storage.

The MCP tool `export_to_google_sheets` uses the same connection and receipt ledger while
running its SQL through the normal governed read-only path. Enable the **Google Sheets
API** in the OAuth client's Google Cloud project; exports need no Picker key or service
account. Refresh tokens use the same configured encryption secret as Calendar, with a
purpose-separated encryption context.

### Burrow + Google: one door, Postgres still decides
With `WAREHOUSE_AUTH=pg`, Google proves **who** you are and Postgres still decides **what**
you may touch. A verified identity is resolved to a role by `rvbbit.resolve_identity()`
(migration 0221), in this order:

1. an enabled `rvbbit.identity_map` row (`identity` → `role_name`) — the DBA's escape hatch;
2. **the email IS a role** — Azure Entra and Cloud SQL IAM both name database roles after the
   principal, so `CREATE ROLE "ryan@acme.com" LOGIN` needs no mapping at all;
3. nothing matches → **`rvbbit_guest`**.

That third case is a state neither system expresses alone: *OAuth says yes, the database says
it can't place this user.* It isn't an error — it's someone who needs provisioning. They get
the artifact index's **access-pending** page (no artifacts, no titles, no DataRabbit link — an
unmapped session would fail every query it made), and they're recorded in
**`rvbbit.identity_pending`** so there's a queue to work from:

```sql
SELECT * FROM rvbbit.identity_pending;                 -- who's waiting (role_now_exists = ready)
SELECT rvbbit.burrow_enroll('ryan@acme.com');          -- once their role exists
INSERT INTO rvbbit.identity_map(identity, role_name)   -- or map to a differently-named role
     VALUES ('ryan@acme.com', 'analyst_ryan');
```

`rvbbit_guest` is created `NOLOGIN` **with no grants at all** — it cannot be connected as, and
it can read nothing. (`NOLOGIN` costs nothing: `SET ROLE` into a NOLOGIN role works, and guest
is only ever reached that way.) To give it a real read-only tier, that's a deliberate act:
`SELECT rvbbit.burrow_grant_guest('analytics');`

> Identities longer than **63 bytes** can't be role names — Postgres truncates identifiers at
> that length with only a `NOTICE`, so two long addresses sharing a prefix would silently
> collide into one account. Resolution refuses them; they need an explicit `identity_map` row.

**Non-Burrow installs are untouched by all of this.** `session_subject()` returns before any
resolution when `WAREHOUSE_AUTH` isn't `pg`, and 0221's only footprint on such a box is one
inert row in `pg_authid`.

`GET /auth/config` reports `{mode, google, password}` so a sibling-rendered login page
(`WAREHOUSE_LOGIN_UI=lens`) draws the right buttons without duplicating this config.

### nginx (terminate TLS, forward all paths to `127.0.0.1:8765`)
```nginx
server {
  listen 443 ssl;
  server_name dwmcp.example.com;
  # ssl_certificate ... (e.g. certbot)
  location / {                       # /mcp, /authorize, /token, /register, /.well-known/*, /login
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;     # the server rate-limits per this IP
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;             # streamable-HTTP / SSE
    proxy_read_timeout 3600s;
  }
}
```

> **Connector gotcha:** in the "Add custom connector" dialog, leave **OAuth Client ID**
> and **OAuth Client Secret** EMPTY — those are for pre-registered clients and bypass
> auto-registration. If you put your email there you'll get "Client ID not found." Your
> email + password go on the *login page* that appears after, not in the dialog.

## Activity log (audit + usage-learning)
Every tool call is recorded to **`rvbbit.mcp_activity`** (auto-created on startup):
`caller` (the backwards-compatible attributed user), `client_id`, `actor` (the
credential principal), `subject` (the human application request may authorize as),
`auth_mode`, `delegated`, `channel`, `client_app`,
`session_ref`, sanitized `provenance`, `tool`, `args` (incl. the SQL/search query),
`ok`/`error`, `objects` (schema.tables touched), `rows`, `engine`, `elapsed_ms`, `as_of`,
and `result_summary`. `channel` distinguishes `google_chat`, `web`, `direct_mcp`,
`automation`, and explicitly ambiguous legacy traffic. `client_app` further identifies
Calliope/Gallery or the MCP handshake/OAuth registration's self-declared software name
(for example Codex or Claude Code); it is useful provenance, not an authorization claim.
For a direct OAuth request, actor and subject are the same person. For Calliope or Google
Chat through the dedicated Hermes credential, actor is the Calliope service and subject
is the verified human. Legacy shared-key forwarding can still populate `caller` for old
reports while leaving `subject` null, making the security distinction queryable.
DataRabbit's **MCP Incoming** surface deliberately shows distinct people separately from
MCP request volume: one dashboard open or Calliope turn can fan out into many logged SQL
and tool requests. Historical rows and generic shared-key clients that cannot be resolved
remain in **Other** instead of being guessed into a first-party surface.

Three rollup views ship with it: `rvbbit.mcp_activity_summary` (per tool/caller),
`rvbbit.mcp_activity_channel_summary` (per channel/client/tool), and
`rvbbit.mcp_popular_objects` (most-touched tables — the seed for "the catalog learns from
usage"). Existing rows remain readable as `legacy_unknown` in the channel rollup. It's in
the `rvbbit` schema, so it's hidden from `search_data`. Logging is best-effort; with a
read-only data role, `GRANT INSERT ON rvbbit.mcp_activity` (and the table's privileges) so
writes succeed.

## Application Teams

Calliope's Library → **Teams** contains a focused flat Team directory backed by
`rvbbit.application_principals`, `rvbbit.teams`, and `rvbbit.team_members`. A person enters
the candidate directory only when Warehouse has a trusted application authorization
`subject`; a service actor, rejected sender, or legacy attribution never becomes a Team
candidate. Observation itself grants no access.

The protected **Admins** Team is created automatically and is the only capability that
permits `team_create` or `team_update`. Admins can edit every Team, including Admins
membership, but Admins cannot be renamed, archived, or left empty. Team changes are
revision-checked, idempotent, and recorded in the append-only `rvbbit.team_events` ledger
with both credential actor and authorized human subject.

The protected **Everyone** Team is the organization-wide wildcard. It stores no
`team_members` rows: membership is evaluated at request time against Warehouse's verified
application `subject`, so a newly signed-in user matches immediately. Anonymous requests,
service identities, rejected senders, and legacy attribution-only callers never match it.
Everyone cannot be renamed, archived, deleted, or manually populated. Its stable
`system_key='everyone'` is the grant target for artifacts visible to all authenticated
users.

Bootstrap the first administrator from operator-controlled configuration:

```bash
export WAREHOUSE_TEAM_BOOTSTRAP_ADMINS="admin@example.com"
```

The value is a comma-separated list and is a one-way, idempotent enrollment path. After an
administrator adds durable replacements through Calliope, remove the environment value
before removing that bootstrap member; otherwise the next service restart will restore it.

## Artifact access and lifecycle

Newly published artifacts are private to their verified human owner. Existing artifacts
receive a one-time **Everyone** grant during migration so upgrades preserve their prior
visibility. An owner can use the Gallery card's **Access** action, or the
`artifact_access_get` / `artifact_access_update` MCP tools, to replace the exact viewer list
with any combination of active Teams and observed people. The owner is always implicit and
cannot accidentally remove themselves. Adding Everyone requires explicit confirmation.
Grants govern current and historical versions, thumbnails, Gallery discovery, Calliope
evidence and Trail references, semantic Home replay, and mutating artifact tools. Unauthorized
and archived routes return the same not-found response rather than disclosing metadata.

Only the owner can change sharing or lifecycle, including when that owner is also an Admin;
Admins do not gain a hidden artifact override. **Archive** is reversible and takes the
artifact out of normal discovery and viewing without deleting versions, grants, lineage,
events, or pins. The owner can select **Archived** in Gallery and restore it with its prior
viewer list intact. `artifact_archive` and `artifact_restore` expose the same flow to Calliope.
Access writes use optimistic revisions and append actor, authorized human, before/after, and
reason records to `rvbbit.artifact_access_events`. There is intentionally no hard-delete UI.

### Connect Claude
- **Claude Desktop / Cowork (OAuth):** Settings → Connectors → **Add custom connector** →
  URL `https://dwmcp.example.com/mcp` → it opens the login page → enter email + the shared
  password → **Allow**. No header.
- **Claude Code (either mode):** `claude mcp add --transport http rvbbit-warehouse <url>/mcp --header "Authorization: Bearer $WAREHOUSE_MCP_KEY"`

## The landing page (`/`)
`GET /` is this server's own index of every published artifact — thumbnail, kind,
description, dependency counts, search and kind filters — behind the same session
cookie that already gates `/d/<slug>`, so a cold link bounces through `/login` and
comes back. It's for the install shape where nobody opens DataRabbit at all: people
talk to the warehouse through Claude, artifacts get published here, and without an
index "find last week's dashboard" means scrolling a transcript.

Also served at **`/gallery`** — on a warehouse-only box `/` is free and you get it at
the root; behind the unified origin (`docker/origin/Caddyfile`) DataRabbit owns `/`,
so the ingress routes `/gallery` here instead. Same page either way.

It lists the active `rvbbit.live_apps` each signed-in person may view, plus the owner's
archived recovery view (DataRabbit *plates* live in `rvbbit.plates` and never appear).
Cards open in a new tab: the index is somewhere you come back to. Routes only exist in
OAuth mode (same as `/d/<slug>`).

The adjacent **Metrics** view is explicit opt-in and stays out of artifact search by
default. It reads governed definitions plus already-materialized
`rvbbit.metric_observations`; browsing never evaluates every metric. Cards show the
latest value and bounded trend, while **Metric Lens** exposes the inspectable timeline,
definition versions, parameters, source freshness, and dependent artifacts. Metric
handles include canonical JSON parameters, so two slices of the same definition remain
distinct everywhere they are followed, pinned, or discussed.

**Thumbnails.** Captures are stored on disk under
`$WAREHOUSE_LIVE_APP_CAPTURE_DIR/thumbs/<kind>/<slug>.jpg` (compose points that at the
durable volume) and are **never regenerated per visit** — a warm request is a ~7ms
file read. Three things keep a cold gallery from looking broken:
- **Warming happens at page render**, not at image request. The browser only fetches
  thumbnails for cards it decides to load, so leaving generation to `/thumbs` meant an
  artifact below the fold never started rendering until somebody scrolled to it.
- **The page retries** a missing shot with backoff (5 tries), so a cold gallery fills
  in while you watch instead of after a manual refresh.
- **`ETag` + `304`**, so a reload revalidates for ~0 bytes instead of re-transferring
  every image.

Captures are JPEG at 800×500/q72 (~28KB each, down from ~190KB PNGs). The URL keeps its
`.png` spelling because lens's `rv-shot` proxy hardcodes it — the `content-type` header
is what the browser reads, and the proxy forwards ours. Pre-existing `.png` captures are
still served until the artifact is republished. The `capture_live_app` MCP tool is
unaffected and still produces full-resolution lossless PNGs.

## Warehouse appearance
The palette icon in every first-party Warehouse header opens the shared image-theme
picker. It is available before authentication too, so a saved room follows the browser
across the login page, artifact hub, access-pending page, and Calliope. The bundled
library is a self-contained, web-sized copy of DataRabbit's Lens wallpaper set: 74
thumbnail/full WebP pairs rather than a runtime dependency on the Lens checkout.

Color selection uses the same pipeline as DataRabbit: `node-vibrant` role swatches are
normalized into an image palette, then paired dark/light derivations generate interface,
accent, semantic, and chart tokens around the image's dominant hue. The sun/moon control
beside the palette icon switches the active derivation and saves that choice in the browser;
it applies before first paint and follows the user across every first-party Warehouse page.
Library selections
store their stable source reference, palette, and generated tokens in `localStorage`.
Uploaded images store the actual Blob in IndexedDB and the compact palette/tokens in
`localStorage`, so colors can restore during `<head>` and the image follows immediately
after browser storage opens. A selected theme can use either its wallpaper or a
user-selected solid background color; switching to solid keeps the derived interface
and chart palette intact. Calliope uses a darkened scene behind dark mode and a warm paper
wash behind light mode so its translucent notebook panes remain legible without losing the scene.
**Use default** deletes both records and exposes the page's existing hand-tuned colors
and randomized background again.

The boundary is deliberate: first-party chrome and native system objects (including
Calliope query charts) inherit the generated tokens. Ordinary published HTML/JavaScript
apps, dashboards, and decks do not load the appearance bundle, and Calliope artifact
iframes remain separate CSS trees, so authored artifacts keep their own visual identity.
The built-in **Adaptive Calliope** Design Profile is the explicit opt-in exception. Its
artifact manifest asks a small, allowlisted runtime for the viewer snapshot: direct pages
read the same browser-local state, while sandboxed Stage frames request it through the
parent's validated `postMessage` bridge. Uploaded wallpaper blobs stay in IndexedDB and
never enter artifact HTML. Theme changes update open adaptive artifacts live; captures,
thumbnails, and exports use the profile's deterministic editorial fallback. The uber-origin
Caddy route proxies `/theme/*` to the Warehouse service.

## Calliope (optional Hermes-backed notebook)
Set both `WAREHOUSE_HERMES_URL` and `WAREHOUSE_HERMES_API_KEY` to add **Calliope** to
the gallery as a floating workspace portal. If either is blank, no Calliope launcher
or route is registered.

The gallery keeps its instant, browser-local artifact filter. Once a query has at least
two characters it also offers an explicit semantic escalation: clicking it (or pressing
Enter in the filter) resolves company evidence, creates a fresh email-owned Hermes and
Calliope session, saves the result as the first evidence bundle, and then opens that
bundle in the full Calliope workspace. The search is sent in the authenticated POST body,
not placed in the URL, and never becomes a synthetic agent chat turn.

Each gallery card also has an explicit **Ask** action. It always creates a fresh notebook
and pins the card's exact published artifact version to the stage; the immutable version
URL is used for rendering even if the gallery alias advances later. **Pin** remains a
separate action for adding the artifact to the user's Semantic Home.

Named business-object Home pins open that exact stored dashboard/app version at
`/d/<slug>/versions/<n>` or `/apps/<slug>/versions/<n>`; they never fall through to a
newer live-app runner. A scalar pin can also be **Promoted to metric**. Warehouse
rehydrates and executes the immutable semantic definition under the mapped user, freezes
its resolved context, carries its meaning/formula/unit/display/source metadata into an
append-versioned `metric_defs` row, materializes the first observation, and replaces the
source pin with the resulting governed metric. The browser can edit only the catalog name
and description—not the SQL or provenance—and an unrelated existing metric name is never
overwritten.

Governed metrics use the same three-intent split. **Brief me** quietly contributes recent
durable observations to that user's Personal Brief without creating an alert. **Pin**
tracks the latest observation on Semantic Home. **Ask Calliope** freezes either the
current observation or a selected Metric Lens range onto a fresh Stage, preserving the
observation ids, definition version, parameters, and lineage while the conversation
continues.

Calliope is a three-column business-user surface: the signed-in user's private session
rail, a newest-first living artifact record, and chat. Hermes owns the agent run and uses
its current/default profile and shared company memory; the warehouse stores only the
email-owned UI index, mirrored turn prose, image attachments, and immutable projections
of MCP results. Queries, KPI timelines, dashboards, apps, and downloadable documents
appear as stage surfaces linked back to their source message. Visual-validation captures
are retained as exact-version companions to their live artifact rather than duplicated
as a second Stage card. The artifact's **Markup** action reuses that companion—or renders
one on demand—so pen, arrow, box, and optional selected-element targets annotate the same
pinned version. Selecting a surface adds it to chat context; selecting it again clears
that context. The chat edge is draggable (and keyboard adjustable) within
viewport-derived bounds. Updating a published artifact creates a new exact-version
surface linked to the older one. Visual builds get a bounded
capture-and-review continuation: Calliope receives the real screenshot as vision input
before finishing. Any standalone image surface can also be opened in the pen/arrow/box markup
editor; the flattened annotation is sent with the next message while its separately
stored overlay becomes a toggleable, lineage-linked stage surface.

A new notebook may be created without a title. After its first substantive completed
turn, Warehouse calls the receipted `rvbbit.summarize` operator over a bounded transcript
and replaces only the provisional title; an explicit human rename always wins. Hermes
terminal usage is mirrored into the RVBBIT receipt and semantic cost ledgers with the
signed caller. Explicit upstream charges are settled, known model rates are estimated,
and subscription/OAuth or otherwise unreported runs are honestly recorded as
`0.00 / uncosted` rather than pretending they were free.

The stage header also contains a company evidence resolver. One query federates the
caller's ACL-filtered Document Brain, published artifact metadata and enriched semantic
objects, and the warehouse semantic catalog. Results are saved as a newest-first evidence
bundle in the notebook rather than pasted into chat. Published artifact matches reuse compact
gallery thumbnails. Users explicitly select any combination
of documents, dashboards, dashboard values, tables, columns, cubes, or metrics and then
ask Calliope with that context. With no individual selection, **Ask Calliope** attaches the
whole search as one compact index containing its query, corpus counts, and bounded result
gists/provenance handles—not the full documents. The browser sends only opaque handles;
Warehouse rehydrates the selected records or search set from the authenticated session
before giving bounded evidence to Hermes. Evidence searches are durable scratchpad history
but are not Hermes chat turns, while the selected evidence is recorded on the eventual chat turn for later
resumption. A failed corpus is shown as unavailable without discarding results from the
other resolvers. No additional environment variables are required.

**Personal Briefs** are the user/time-scoped front door to that same evidence system.
The **Brief** control in both the gallery rail and Calliope opens one private notebook per
OAuth email and local calendar day. Its first surface is resolved deterministically—no
LLM call—from ACL-visible Brain sources, the user's Work Inbox and semantic watches,
their private Semantic Home pins, recent private Calliope notebooks, and recently authored
artifacts. Whole-artifact Home pins follow latest while named business-object pins retain
their exact versioned handle. Explicit refreshes
append timestamped snapshots to the same lineage; simply reopening today's Brief reuses
the latest snapshot. Each refresh compares stable observation state with the immediately
preceding snapshot: new and materially changed facts are explicit, while unchanged generic
activity is not mislabeled as “changed.” Sections distinguish the user's pinned focus,
work that needs attention, upcoming events, exact changes, moved watch values, resumable
work, and possible identity matches. Source facts are labeled **observed**,
confirmed/candidate identity joins are
**resolved**, and agent **interpretation** only begins after the user attaches selected
items or the compact whole-Brief index and chooses Ask Calliope. Card actions attach the
exact item and prepare a reviewable investigation/preparation/resumption prompt; they never
auto-send or mutate a source. “That's me,” “Relevant,” and “Not mine” corrections are
private and durable; they never rewrite source documents or split the shared
Hermes/Hindsight company memory.

Each daily Brief also has an optional append-only **Daily notes** thread. The
CodeMirror editor supports Obsidian-style `[[…]]` completion against ACL-visible
Brain objects projected as people, places, things, projects, and tickets. Choosing
an object records an explicit private `mentions` edge to its canonical KG node;
ordinary prose creates no inferred edge. Note bodies stay out of the shared Brain
graph and shared Hermes memory. Instead, the owner-keyed graph overlay and recent
prior notes feed later Personal Briefs under **From your notes**, labeled **You
noted** so user context is never presented as independently observed fact.

An optionally connected Google Calendar follows the same private-overlay contract.
Upcoming and recently completed events enter Briefs as directly **observed** Calendar
facts; normalized attendee, organizer, and location fields produce owner-keyed
person/place edges for later personal context. Exact email, display-name, and location
matches may point at canonical Brain objects only when those objects are backed by a
document visible to the signed-in user. The event remains private: no Calendar prose or
edge is written into the organization graph, fuzzy identity matches are not attempted,
and every **Trail** hop rechecks both the Calendar owner and Brain ACL. A resolved event
trail can surface its canonical people/places and semantically relevant company documents,
which gives **Prepare** grounded history without presenting a private Calendar fact as a
company assertion. Brief refreshes sync Calendar first when the local cache is stale,
while a Google API outage leaves the last private cache and every other Brief resolver
intact.

Brain resolution is sampled fairly per visible source before applying the overall bound,
so a high-volume fresh corpus cannot starve another source. System-learning records remain
outside the personal resolver rather than pretending to be person-addressed work; they are
better suited to a separate operator-oriented Brief. Coverage distinguishes resolvers
checked, contributing sources, available records, person mapping, and records omitted from
the visible bounded snapshot.

Providers can project arbitrary source JSON into this observed layer with an optional
`observation_map` on `rvbbit.brain_doc_providers`, or a source-specific override at
`rvbbit.brain_sources.config.observation_map`. The deliberately small JSON-path subset
supports fields such as `status`, `due_at`, `starts_at`, `url`, `assignee_emails`,
`assignee_names`, `participants`, `authors`, and `viewer_scope`. This keeps Linear,
calendar, meeting, ticket, and future connector names out of Calliope itself. Sources
already synchronized under the bundled Linear and Fireflies providers receive matching
defaults, including Fireflies attendee objects, participant-email strings, organizer,
meeting time, and meeting link fields. Sources
without a usable person projection remain visible in Brief coverage as **not
person-mapped**, which gives a future setup wizard a concrete recommendation instead of
silently pretending the integration is personalized. No additional environment
variables are required.

The header's **Design Profiles** library turns uploaded reference images, a frozen
URL viewport/extraction, an existing selected capture, and optional written direction
into a reusable dashboard style contract. Profiles are company-visible and creator-
editable. Markdown remains directly editable; every save creates an immutable version.
A profile may be pinned to the notebook or only the next turn, and selecting an older
artifact carries its exact pinned profile forward. Calliope injects that version into
the Hermes authoring and screenshot-review prompts; the reference assets and compiled
profile never become runtime CSS for unrelated custom artifacts.

Every install also seeds the immutable, system-owned **Adaptive Calliope** profile. It
combines a compact 12-column editorial composition, Newsreader display type, IBM Plex Sans
body type, IBM Plex Mono metadata, explicit KPI/table/chart defaults, responsive rules,
and strong avoid rules for generic card walls. Structure and information-design behavior
stay consistent, while each viewer's light/dark palette, chart series, wallpaper, and glass
materials resolve at render time. The artifact listens for `rvbbit:adaptive-theme` when a
JavaScript chart needs to reread CSS colors. Users may duplicate the profile to make a
static or customized variant, but cannot revise or archive the shipped original.

The adjacent **Instruments** library turns a repeated workflow into a small interface
that people can reuse without learning prompts. Calliope co-designs a bounded form
(`text`, `textarea`, `number`, `select`, `boolean`, and `date`) plus a transparent prompt
contract, then saves it through `draft_calliope_instrument`. Agent-created revisions are
immutable private drafts: only the human creator can publish one privately or advance
the company-visible publication pointer. A newer private draft is not exposed to company
readers until that explicit approval. Running an Instrument validates its inputs and
opens a fresh email-owned notebook with the exact Instrument version, choices, and form
values frozen as evidence; revising one starts a separate evidence-backed conversation.
The interface never executes Instrument-authored HTML, JavaScript, or SQL directly, and
the resulting Calliope run still uses the signed-in person's normal governed tools and
warehouse permissions. No additional environment variables are required.

The adjacent **Workflows** library is the headless counterpart to Instruments. A readable,
versioned graph has one manual or schedule trigger, up to eight governed context nodes
(`artifact`, `semantic_object`, `evidence`, `knowledge`, or `instruction`), one agent goal
with decision rules, and stage, Work Inbox, or artifact outputs. It deliberately does not
store arbitrary SQL, JavaScript, shell, or low-level tool DAGs: Hermes chooses concrete
governed tools at run time. Agent-authored revisions remain private until the owner moves
the publication pointer. Every invocation creates a fresh notebook, freezes the approved
graph and resolved context, and requires a paired begin/finish lifecycle call; completion
adds durable result surfaces and a deduplicated Work Inbox result or blocker. These fresh
execution notebooks join Instrument executions under the **Runs** tab in the session rail.
The compact **Chats**, **Briefs**, **Runs**, and **Actions** tabs keep generated notebooks
out of the ordinary conversation list while Work Inbox remains continuously visible. The
browser remembers the active tab, the last notebook in each tab, and the last notebook
overall; a valid `?session=` deep link always takes precedence over remembered state.

**New** opens a native starter-based builder and creates a private draft without invoking
an LLM. **Design one with Calliope** is the separate conversational path for people who
want the agent to co-design the graph. This distinction remains visible in the builder so
a missing model provider cannot prevent someone from authoring a Workflow.

**Run now** opens that notebook and auto-submits only the Workflow launch instruction;
ordinary prepared Calliope handoffs remain review-before-send. If the streamed agent turn
ends without the required finish call—including provider/authentication failure—the
warehouse fails the run closed, commits a visible result surface, and publishes an unread
Work Inbox blocker instead of leaving the Workflow permanently running.

Before Run now or schedule enablement, **Test readiness** resolves the exact frozen
contexts and checks Hermes model/configuration health plus declared requirements such as
personal context, project/ticket sources, MCP servers, Brain providers, or installed
capabilities. The check is side-effect free: it creates no session, run, Inbox item, model
call, or schedule change. Explicit missing requirements block execution; advisory
requirements inferred from older graphs remain warnings that require acknowledgement.
The graph, approval, automation, readiness, and latest-run states stay visible together in
one lifecycle strip.

The frozen launch contract also exposes an identity-scoped personal-context capability
when a goal needs the owner's latest Daily Brief, private notes, or Work Inbox. The opaque
run ID resolves ownership server-side; the agent cannot provide or enumerate an email
address, and the bounded result remains subject to the same owner checks as the UI.

Each run keeps a bounded, user-visible diagnostic timeline of tool start, completion, and
failure events, plus any explicitly reported action/outcome steps from a scheduled agent.
The UI groups those events under four readable phases—prepare, gather governed context,
analyze and decide, and commit the result—while leaving the exact technical events
expandable underneath. The timeline redacts credential-shaped text and never stores hidden
reasoning, raw tool arguments, or raw tool results. **Revise Workflow from this run** opens
a fresh revision notebook with the exact graph plus a bounded outcome snapshot (status,
summary, structured details, phase summaries, artifact refs, and source notebook pointer),
never a transcript, prompt, or raw tool payload. The Workflow rail also includes a
lightweight Hermes operations summary for visible cron jobs and queue health; prompts,
delivery targets, and provider credentials are deliberately excluded.

Published schedule graphs require a second explicit **Enable schedule** action. The
schedule pins that approved version and runs through Hermes cron using the Hermes
installation's timezone and configured cron/default model provider. Drafting or revising
never silently advances the live schedule. The Workflow library mirrors Hermes job state,
including provider failures that occur before a Calliope run can begin, so unattended work
cannot fail invisibly. Pausing, resuming, running now, disabling, unpublishing, or archiving
also updates or removes the corresponding Hermes job. No additional environment variables
are required beyond the normal Calliope/Hermes setup.

The header's **Library** control is an outcome-oriented front door to the SQL-first
administration and capability catalog. People can search for goals such as “use Linear in
my Brief” instead of knowing an MCP server, Brain provider, or catalog capability by name.
Each result exposes what it unlocks, its current requirements, and one of two paths:
guided actions seed a fresh Calliope notebook with a structured contract, while typed
actions use a bounded native form. Typed changes always produce an immutable plan first;
**Apply change** is a separate human approval and records progress through apply, probe,
verify, and receipt steps. Receipts retain redacted inputs and rollback guidance, and can
be reopened from both the Library and the Calliope stage. Guided notebooks are collected
under the **Actions** tab in the session rail. Grouping is derived from their durable
Library handoff surface, so ordinary conversations that merely use an action remain under
**Chats**.

Workflow readiness links missing declared requirements directly to matching Library
actions. A successful repair returns to the blocked Workflow and reruns its side-effect-free
readiness check. In this first version, authenticated organization members are trusted to
use the Library; the eventual unified role and policy system is intentionally not modeled
piecemeal here.

MCP credentials never enter a Calliope prompt or PostgreSQL. Secret fields are password
controls whose values are sent only with the explicit apply request, immediately cleared
in the browser, and forwarded to the MCP gateway's encrypted secret route. Existing saved
secret names may be shown, but values are never returned. Set the same
`RVBBIT_GATEWAY_TOKEN` on PostgreSQL, Warehouse, Warren, and the MCP gateway in deployed
environments; `MCP_GATEWAY_URL` can override endpoint discovery when needed. An absent
required credential or an unavailable gateway fails the action closed before activation.

The Library's **Connect a custom MCP server** action exposes the same generic RVBBIT
connection path for servers that are not in `capability_catalog`. It supports streamable
HTTP (no authentication or one Bearer token) and stdio executables that run on the MCP
gateway host/container. Stdio arguments are an explicit array rather than a shell command;
non-secret environment values stay in the registration while `${NAME}` values resolve from
the gateway's secret store. Apply registers the reviewed transport, calls
`refresh_mcp_server` to introspect live tools and resources, generates both per-tool typed
functions such as `server_name.tool_name(...)` and optional RVBBIT operators, then runs an
active probe and records the resulting counts. The universal fallback remains
`rvbbit.mcp_call(server, tool, args_jsonb)`. Wrapper generation refuses reserved names or
an unrelated existing SQL schema so its recreate-on-drift behavior cannot erase another
application surface. Older SSE and interactive OAuth transports are not implied by the
form; those require a stdio bridge or a future gateway transport.

Cube schema surfaces are also direct interactive analysis tables. Add one or more
dimensions to Rows and one or more numeric aggregations to Values; this produces a
normal grouped table with named columns. Columns is optional—adding dimensions there
turns the same view into a cross-tab. Every shelf change recalculates automatically,
without asking Hermes or requiring a governed metric. The backend validates every
field against the materialized cube, quotes identifiers, and rejects results above
the configured cell cap.

The header's **Dreams** control opens Calliope's bounded company-reflection loop. Once
per company-local day, a background worker compares the incremental activity window with
a rolling 90-day horizon. Separate no-tools Hermes observer passes rotate several lenses
across notebook intent, MCP usage, governed objects, artifacts, document/KG shapes,
metric history, structured work, sync/run health, and available capabilities. Private
Calendar and Daily Note overlays contribute only k-anonymous rhythm counts (three or more
owners), never titles, prose, attendees, labels, or identities. The editor may retain up
to twelve evidence-linked candidates but promotes only three; runners-up remain quietly
inspectable under **In the wings**. Small ideas become inspectable native prototypes,
larger or externally mutating ideas remain project plans, and uncertain ideas become
explicit questions. Similar ideas version and deepen one durable Dream rather than
stacking copies. Raw conversation text is used only inside the bounded observer call and
is not stored in the Dream tables or receipts.

The Evidence Lab sits between observation and ideation. Hermes may propose a small set
of falsifiable SQL or Clover experiments, but it never receives a general tool loop.
Warehouse admits only parsed, single-statement SELECTs over recently observed schema
targets, runs data extraction in a read-only transaction with row/time limits, and invokes
only a small allowlist of Clover operators itself. Raw Clover inputs are ephemeral; Dreams
retain compact aggregate receipts under **What Calliope tested**. The worker discovers
operators from the running database rather than assuming a catalog entry is installed,
and equivalent tests may reuse a receipt for 24 hours. Set
`WAREHOUSE_CALLIOPE_DREAM_EVIDENCE_LAB=0` to turn this phase off. Deployments may also set
`WAREHOUSE_CALLIOPE_DREAM_SQL_ROLE` to an existing least-privilege read role; the
application-level target and query guards remain active either way.

Dream feedback is personal until someone adopts or explores an idea. **Sleep on it** and
**Not useful** hide a Dream only for that viewer. **Explore with Calliope** creates a normal
user-owned notebook, pins the versioned Dream and its de-identified evidence on the Stage,
and prepares a review-first continuation prompt; it never schedules, publishes, or mutates
an external system by itself. **Dream deeper** deliberately revisits a rolling 30 days
against the 90-day horizon and cannot consume the nightly incremental cursor; it refuses
overlapping cycles. Failed and stale nightly cycles remain visible and can be retried safely.

The chat composer and private Daily Notes editor expose **Dictate** only when a
server-side speech provider is configured and the browser supports microphone capture.
With OpenAI Realtime enabled, a provisional transcript appears while the user speaks;
stopping the microphone finalizes it and inserts only the final text at the captured
cursor. Dictation remains deliberately review-first: it never sends a chat turn or
appends a note automatically. Warehouse authenticates the user, adds the bounded
transcription configuration, and exchanges the browser's WebRTC offer without exposing
the provider key. The live audio then flows directly between the browser and the
provider. Recognition hints are bounded to configured organization terms plus current
notebook titles, identity names, and recent private linked-object labels already visible
to that user. A simultaneous browser recording supplies an automatic file-transcription
fallback if live setup, connectivity, or finalization fails. Warehouse validates that
fallback audio and does not persist it.

Spoken responses are an independent, opt-in browser preference in the shared
**Appearance** dialog. **Off** is the default. **Fast** rewrites each newly completed
answer into a factual two- or three-sentence spoken digest and streams it through
ElevenLabs Flash without performance tags. **Expressive** uses the same bounded semantic
rewrite, permits at most two validated v3 audio tags, and synthesizes with ElevenLabs v3.
The full assistant answer remains canonical and unchanged in the turn record. While voice
is enabled, the conversation displays the concise spoken cut and the small Voice control
opens the complete answer; switching voice off restores the complete answer directly in
chat. The latest derived script and non-secret render metadata live under that turn's
response receipt for reload-safe replay and debugging. The optional speaking-personality
text lives only in browser storage. Warehouse sends it transiently as untrusted tone
guidance and retains only its hash. Provider credentials remain server-side. The timed
ElevenLabs stream supplies per-character alignment alongside PCM, so the browser decorates
each word as it is spoken; a cadence estimate is used only when alignment is absent. Audio
begins playing while the HTTP response is still arriving, completed audio and its timing
manifest are cached in the owner-gated Calliope store, and starting another turn, changing
notebooks, switching voice off, or pressing Stop interrupts playback.

```bash
export WAREHOUSE_HERMES_URL="http://127.0.0.1:8642"
export WAREHOUSE_HERMES_API_KEY="..."       # Hermes API_SERVER_KEY
# Optional: one shared company memory scope, never an email-derived scope
export WAREHOUSE_HERMES_MEMORY_KEY="company"
export WAREHOUSE_CALLIOPE_DIR="/var/lib/warehouse/calliope"
# Optional, trusted networks only: permit URL references on private/local hosts
export WAREHOUSE_CALLIOPE_STYLE_ALLOW_PRIVATE_URLS="false"
# Optional but required for browser downloads created by external Hermes:
export WAREHOUSE_CALLIOPE_EXPORT_ROOTS="/var/lib/hermes/exports"
export WAREHOUSE_CALLIOPE_MAX_EXPORT_BYTES="134217728" # 128 MiB; ceiling 512 MiB
# Optional company Dream schedule; enabled with Calliope by default
export WAREHOUSE_CALLIOPE_DREAMS="1"                  # set 0 to disable
export WAREHOUSE_CALLIOPE_DREAM_EVIDENCE_LAB="1"      # bounded SQL/Clover tests
export WAREHOUSE_CALLIOPE_DREAM_TIMEZONE="America/New_York"
export WAREHOUSE_CALLIOPE_DREAM_HOUR="3"              # local hour, 0-23
# Private notebook rail synopses; debounce resets whenever the thread changes
export WAREHOUSE_CALLIOPE_SESSION_SYNOPSES="1"          # set 0 to disable
export WAREHOUSE_CALLIOPE_SYNOPSIS_DEBOUNCE_SECONDS="90"
# Optional dictation; WAREHOUSE_CALLIOPE_STT_KEY overrides OPENAI_API_KEY
export WAREHOUSE_CALLIOPE_STT_PROVIDER="openai"         # set off to disable
export WAREHOUSE_CALLIOPE_STT_MODEL="gpt-transcribe"
export WAREHOUSE_CALLIOPE_STT_REALTIME_MODEL="gpt-live-transcribe" # set off for batch-only
# Optional literal recognition hints and expected language codes for live dictation
export WAREHOUSE_CALLIOPE_STT_KEYWORDS="RVBBIT,Calliope,Linear,ENG-42"
export WAREHOUSE_CALLIOPE_STT_LANGUAGES="en"
export WAREHOUSE_CALLIOPE_MAX_AUDIO_SECONDS="120"
# Optional spoken responses; both values are required to expose the feature
export ELEVENLABS_API_KEY="..."
export ELEVENLABS_VOICE_ID="..."
# Optional adapters/defaults shown here for explicit deployment control
export WAREHOUSE_CALLIOPE_TTS_FAST_MODEL="eleven_flash_v2_5"
export WAREHOUSE_CALLIOPE_TTS_EXPRESSIVE_MODEL="eleven_v3"
export WAREHOUSE_CALLIOPE_TTS_SAMPLE_RATE="24000"
export WAREHOUSE_CALLIOPE_TTS_PREPARE_TIMEOUT_SECONDS="30"
```

For the uber Compose stack with Hermes running directly on the Docker host,
use `WAREHOUSE_HERMES_URL=http://host.docker.internal:8642`; the service
includes the Linux `host-gateway` mapping. A Hermes container on the same
Compose network can use its service name instead. Compose also mounts
`WAREHOUSE_CALLIOPE_EXPORT_DIR` (default `/tmp/rvbbit-hermes-exports`) read-only
at the same absolute path inside Warehouse and advertises it to Calliope's agent
instructions. Hermes must be able to write that directory; a containerized Hermes
needs the same bind mount. Warehouse accepts only a bounded document/data extension
allowlist beneath configured roots, rejects sensitive config/credential paths, hashes
and copies valid files into `WAREHOUSE_CALLIOPE_DIR`, and serves them through
email-owner-gated download URLs. The originating host path is never sent to the browser.

Configure the Hermes default profile's `mcp_servers` entry to use this warehouse's
`/mcp` URL and the server-side `WAREHOUSE_HERMES_MCP_KEY`. Calliope does not create or select a
Hermes profile. Set `forward_session_identity: true` only on this trusted first-party
Warehouse entry. Hermes then forwards bounded platform/session provenance for Google
Chat, Calliope's API-server sessions, and cron runs. Google Chat additionally carries the
adapter-verified sender email; API and cron envelopes carry no asserted human identity.
Warehouse resolves Calliope session IDs against its own tables, which lets it label web
chat and scheduled Workflows while retaining the trusted local owner. Publication tools
also resolve that local owner before persisting a new dashboard or app, so the shared
Hermes service credential never becomes the artifact owner. Post-turn reconciliation and
startup backfill repair artifacts produced by older Hermes clients, including deployments
that name the shared principal with `WAREHOUSE_MCP_STATIC_CALLER`. Keep the flag on
the individual Warehouse MCP entry—not under `platforms.google_chat`, where it is ignored
so provenance cannot accidentally be forwarded to every configured MCP server:

```yaml
mcp_servers:
  Datamarket:
    url: https://warehouse.example.com/mcp
    headers:
      Authorization: Bearer ${WAREHOUSE_HERMES_MCP_KEY}
    forward_session_identity: true
```

### Hermes update compatibility

The current upstream Hermes client does not yet implement RVBBIT's opted-in
identity envelope. The config key above is therefore inert unless the compatibility
patch shipped in this repository is present in the Hermes checkout. `hermes update`
may replace locally modified core files, so check and reapply the patch after every
Hermes update:

```bash
HERMES_AGENT_ROOT="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
RVBBIT_SQL_ROOT=/path/to/rvbbit-sql
cd "$HERMES_AGENT_ROOT"

# A successful reverse check means the patch is already installed.
if git apply --reverse --check \
  "$RVBBIT_SQL_ROOT/services/warehouse-mcp/hermes-forward-session-identity.patch"; then
  echo "RVBBIT Hermes identity forwarding is already installed"
else
  git apply --check \
    "$RVBBIT_SQL_ROOT/services/warehouse-mcp/hermes-forward-session-identity.patch"
  git apply \
    "$RVBBIT_SQL_ROOT/services/warehouse-mcp/hermes-forward-session-identity.patch"
fi

scripts/run_tests.sh tests/tools/test_mcp_session_identity.py -q
systemctl --user restart hermes-gateway
```

If the forward check fails after an upstream refactor, do not force-apply the patch.
Rebase its two integration points in `tools/mcp_tool.py`: capture the gateway
ContextVars before crossing to the MCP event loop, then send the bounded envelope as
MCP request `_meta`. Warehouse should subsequently record a Google Chat call with the
shared Calliope principal as `actor`, the sender email as `subject`,
`auth_mode=google_chat_delegation`, `delegated=true`, `channel=google_chat`, and a
non-empty `session_ref`.

Google Chat's working marker is separately configurable in upstream Hermes and does
not require a source edit. Keep this beside the other platform settings so it survives
updates:

```yaml
platforms:
  google_chat:
    typing_status_text: "Calliope is thinking…"
```

If Hermes uses a terminal-side MCP helper to parse a large result, that helper must
also send the metadata envelope. Calling `server.session.call_tool(...)` directly
bypasses the native handler and records the shared service principal instead of the
Google Chat sender. Install the supplied helper rather than maintaining an ad-hoc copy:

```bash
install -m 0755 \
  "$RVBBIT_SQL_ROOT/services/warehouse-mcp/hermes-dmcp.py" \
  "$HOME/dmcp.py"
```

The helper reads the session ContextVars bridged into its subprocess environment and
uses the same `_forwarded_session_metadata` / `_call_tool_with_metadata` path as native
Hermes. Outside a recognized Hermes session it sends no human identity and remains a
normal service call.

Forwarded metadata is not a tool argument or PG role. It becomes an application-layer
human subject only when paired with the dedicated `hermes-service` credential; with the
legacy shared key it remains attribution-only. A direct
shared-key client that declares a specific MCP `clientInfo` is labeled from that handshake;
an old generic client and an old Hermes connection without this flag remain deliberately
`unknown` rather than being guessed. Enable that same profile's API server and pin its
advertised model to the real provider-routable model ID:

```bash
hermes config set platforms.api_server.enabled true
hermes config set platforms.api_server.extra.host 127.0.0.1
hermes config set platforms.api_server.extra.port 8642
hermes config set platforms.api_server.extra.model_name openai/gpt-5.6-sol
hermes config set platforms.api_server.extra.key "$WAREHOUSE_HERMES_API_KEY"
hermes gateway run --accept-hooks
```

The explicit model avoids the default API alias (`hermes-agent`) being sent to a
provider that does not recognize it. Browser users never receive either secret.
Full contracts and the temporal interaction model are in
[`docs/CALLIOPE_PLAN.md`](../../docs/CALLIOPE_PLAN.md).

## Dashboards (artifacts that live + work outside Claude)
**Start from `dashboard_template`** — the proven boilerplate (see [`DASHBOARD_TEMPLATE.md`](DASHBOARD_TEMPLATE.md)).

For an explicit, non-default visualization experiment, `tanstack_chart_template` returns a
complete framework-free dashboard using the pinned TanStack Charts 0.3.1 SVG runtime. Its
`mountRvbbitChart()` adapter adds exact mark/query/row metadata and semantic bindings while
leaving the surrounding artifact as unconstrained HTML/CSS/JS. Existing Chart.js artifacts and
the ordinary template are unchanged. Publish the returned HTML and manifest with
`create_live_app`, then run `capture_live_app` to validate it.
Its dual-mode data bridge means the *same* artifact runs live in **two places, no login**:
- **In a Cowork artifact** — `window.cowork.callMcpTool('mcp__<id>__run_sql', {sql})`, authed by
  the connector OAuth the user already granted (the sandbox blocks `fetch`, so this is the path).
- **Hosted** — `publish_dashboard(name, html, …)` stores it versioned in `rvbbit.dashboards`,
  serves it at `<WAREHOUSE_PUBLIC_URL>/d/<slug>` behind the login cookie, and injects
  `rvbbitQuery()` (→ `/api/d/<slug>/q`, read-only on the mirror, logged to `mcp_activity`).

Key rule: one **flat** query per data concern in the `composePayload` parts map — the framework
batches them into **one** `run_sql_multi` round trip (each bridge call has ~1.5s overhead), but
each query stays flat on the wire: routable by the accelerated engines, visible to the catalog,
and individually promotable. Never hand-write a `json_build_object` payload query, and never
bake data in — that's a 'dead tree'.

### Versioned semantic maps

Every published HTML dashboard is queued for a **non-blocking semantic compiler pass**.
Publication succeeds immediately; a background worker renders that immutable version,
captures its live read-only queries and visible value-bearing DOM, and calls
`rvbbit.artifact_semantic_enrich(...)`. The agent proposes meanings and replay SQL, but its
output is only a candidate: Warehouse replaces model-supplied selectors with selectors from
the captured DOM, validates each query as a safe `SELECT`, executes it, and accepts an object
only when its replayed value matches the rendered value. A bad candidate is rejected on its
own without rejecting the artifact or other good candidates.

The verified result is stored in `rvbbit.artifact_semantic_enrichments`, keyed to the exact
artifact id + version. It is merged into the manifest when that version is served; the
original authored manifest is never rewritten. The Artifact Lens polls while compilation is
pending, so newly verified objects appear without republishing or reloading the dashboard.

This map is deliberately lighter than a governed metric layer: each business-significant
number says what it means, where it appears in the DOM, which dashboard filter values
produced it, and the exact read-only SQL that independently recreates it. The evaluator may
use arbitrary SQL and may repeat a client-side aggregation in SQL; it does not require a cube
or metric object.

Builders normally do not need to create this map. They can optionally add a precise authored
`manifest.semantic_map` and runtime bindings for important or unusual values; authored ids
and DOM bindings always win, while the compiler fills uncovered values. For a manual binding,
use a stable selector and bind the raw value plus live context after every render:

Use a stable selector and bind the raw value plus live context after every render:

```js
bindBusinessObject(
  'regional_revenue',
  '#regional-revenue',
  payload.kpi.revenue,
  { region: selectedRegion }
);
```

The matching authored manifest object declares `meaning`, `parameters`, `bindings`, and an
`evaluator` whose scalar result is exposed as `value`. Authored evaluators are normalized,
validated, and executed before committing the version. If optional authored semantic
metadata is malformed or cannot replay, Warehouse quarantines that map, returns a
`SEMANTIC_MAP_QUARANTINED` warning, and still publishes the artifact; on update it retains a
previous valid authored map when possible. Generated evaluators use the same contract, but
are attached asynchronously after deterministic replay verification. The hosted runtime
resolves the DOM binding from that effective version; the Artifact Lens presents meaning and
filter context first, recreates the value from warehouse data, and keeps SQL collapsed as
technical evidence. Older or not-yet-enriched artifacts still use best-effort query-trace
inference.

The v1 contract is intentionally value-shaped (`scalar`, `cell`, or `status`, with one
result row). Whole charts and tables stay normal dashboard UI; their individual visible
values use repeated parameterized bindings. This keeps recreation deterministic instead
of smuggling a second analytics framework into the manifest.

One definition may bind many rendered elements. Call `bindBusinessObject` once per
table cell, SVG mark, or chart label with that element's raw value and dimension context;
the runtime keeps context per DOM element. This maps every visible member of a repeated
series without manufacturing a separate manifest object for every region, customer, or date.

The dependency crawler indexes verified definitions as `semantic` edges (separate from the
dashboard's data-fetch `query` edges), resolves their source tables, and reports the current
version's semantic-object count in the gallery. Lens replays are tagged separately from
dashboard runtime queries so inspecting a value does not rewrite the artifact's dependency map.

`semantic_enrichment_status(slug, version?)` reports queue, coverage, and verification
status. `enrich_live_app(slug, version?, force?)` queues or requeues an existing HTML
artifact, which is useful for backfilling versions published before the compiler existed.

Tools: `dashboard_template` / `publish_dashboard` / `update_dashboard` / `list_dashboards` /
`get_dashboard`. Tables auto-create on startup (no migration). Design: [`docs/DASHBOARDS_PLAN.md`](../../docs/DASHBOARDS_PLAN.md).

**Phase 1 — catalog-linked inspection.** `dashboard_crawl(slug)` extracts each dashboard's
data dependencies — parses literal `rvbbitQuery(...)` calls, **SQL-shaped string literals
anywhere in the artifact** (catches SQL Claude assigns to a variable and passes as
`client(sql)`; `EXPLAIN` validates them so junk like `"select … from the menu"` is dropped),
reconciles the queries it actually ran (from `mcp_activity`), and an OpenRouter LLM fallback
(`OPENROUTER_API_KEY`) — then resolves every query to its tables via `EXPLAIN` (catches
plain heap tables, not just rvbbit-managed). Stored in `rvbbit.dashboard_deps` (a derived,
regenerable index; re-run on publish/update). `get_dashboard` returns the `sources` (the
lens "open base SQL" list); `dashboard_dependents(object)` is impact analysis ("what breaks
if I change this table"); views `rvbbit.dashboard_sources` / `rvbbit.dashboard_dependents`.
No `rvbbitQuery`/metric found ⇒ flagged `materialized` (a "dead tree" — nudge against).

## Config (env)
`WAREHOUSE_DSN` · `RVBBIT_CATALOG_GRAPH` (default `db_catalog`) ·
`WAREHOUSE_SCHEMAS` (CSV allowlist; default = all but rvbbit/pg_*) ·
`WAREHOUSE_ROW_CAP` (1000) · `WAREHOUSE_STMT_TIMEOUT_MS` (30000) ·
`WAREHOUSE_CUBE_PIVOT_CELL_CAP` (2500) ·
`WAREHOUSE_MCP_HOST` (0.0.0.0) · `WAREHOUSE_MCP_PORT` (8765)
**OAuth mode:** `WAREHOUSE_PUBLIC_URL` (enables it) · `WAREHOUSE_LOGIN_PASSWORD` (req) ·
`WAREHOUSE_JWT_SECRET` (req, ≠ MCP_KEY) · `WAREHOUSE_ALLOWED_EMAILS` (opt) ·
`WAREHOUSE_ACCESS_TTL` (3600) · `WAREHOUSE_REFRESH_TTL` (30d) ·
`WAREHOUSE_STATE_FILE` (persist registered clients + refresh tokens across restarts —
put it on a volume, else a restart strands connectors with "client_id not found").
**Google Sign-In / Calendar / Workspace:** `WAREHOUSE_GOOGLE_CLIENT_ID` +
`WAREHOUSE_GOOGLE_CLIENT_SECRET` · `WAREHOUSE_GOOGLE_HD` and/or
`WAREHOUSE_ALLOWED_EMAILS` (audience gate) · `WAREHOUSE_GOOGLE_ONLY` (optional) ·
`WAREHOUSE_GOOGLE_TOKEN_KEY` (optional dedicated Google grant-token encryption secret;
otherwise derives from `WAREHOUSE_JWT_SECRET`) · `WAREHOUSE_GOOGLE_PICKER_API_KEY`
(optional origin-restricted browser key enabling Sheet and Google Doc import) ·
`WAREHOUSE_GOOGLE_PICKER_APP_ID` (optional GCP project number; normally derived from the
OAuth client ID).
**Calliope:** `WAREHOUSE_HERMES_URL` + `WAREHOUSE_HERMES_API_KEY` (both required) ·
`WAREHOUSE_HERMES_MEMORY_KEY` (optional shared company scope) ·
`WAREHOUSE_CALLIOPE_DIR` (attachment storage) ·
`WAREHOUSE_CALLIOPE_MAX_IMAGE_BYTES` (8 MiB default) ·
`WAREHOUSE_CALLIOPE_STYLE_ALLOW_PRIVATE_URLS` (false by default; trusted-network
opt-in for private/local Design Profile URL references) ·
`WAREHOUSE_CALLIOPE_EXPORT_ROOTS` (OS-path-separated allowed Hermes output roots) ·
`WAREHOUSE_CALLIOPE_MAX_EXPORT_BYTES` (128 MiB default, 512 MiB ceiling; in uber
Compose set the single shared host path with `WAREHOUSE_CALLIOPE_EXPORT_DIR`) ·
`WAREHOUSE_CALLIOPE_DREAMS` (`1` by default) ·
`WAREHOUSE_CALLIOPE_DREAM_EVIDENCE_LAB` (`1` by default) ·
`WAREHOUSE_CALLIOPE_DREAM_SQL_ROLE` (optional least-privilege PostgreSQL role) ·
`WAREHOUSE_CALLIOPE_DREAM_TIMEZONE` (`TZ`, then `UTC`, by default) ·
`WAREHOUSE_CALLIOPE_DREAM_HOUR` (3, local time) ·
`WAREHOUSE_CALLIOPE_DREAM_TICK_SECONDS` (900; worker wake interval) ·
`WAREHOUSE_CALLIOPE_SESSION_SYNOPSES` (`1` by default) ·
`WAREHOUSE_CALLIOPE_SYNOPSIS_DEBOUNCE_SECONDS` (90; bounded to 30–900) ·
`WAREHOUSE_CALLIOPE_SYNOPSIS_MAX_ATTEMPTS` (3) ·
`WAREHOUSE_CALLIOPE_STT_PROVIDER` (`openai`, or `off`) ·
`WAREHOUSE_CALLIOPE_STT_KEY` (optional override for `OPENAI_API_KEY`) ·
`WAREHOUSE_CALLIOPE_STT_BASE_URL` · `WAREHOUSE_CALLIOPE_STT_MODEL`
(`gpt-transcribe`, batch fallback) · `WAREHOUSE_CALLIOPE_STT_REALTIME_MODEL`
(`gpt-live-transcribe`; `off` keeps batch-only dictation) ·
`WAREHOUSE_CALLIOPE_STT_KEYWORDS` (optional comma-separated literal hints) ·
`WAREHOUSE_CALLIOPE_STT_LANGUAGES` (optional comma-separated expected language codes) ·
`WAREHOUSE_CALLIOPE_MAX_AUDIO_BYTES` (12 MiB default,
25 MiB ceiling) · `WAREHOUSE_CALLIOPE_MAX_AUDIO_SECONDS` (120 default, 600 ceiling) ·
`ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` (both required for spoken responses;
dedicated overrides are `WAREHOUSE_CALLIOPE_TTS_KEY` and
`WAREHOUSE_CALLIOPE_TTS_VOICE_ID`) · `WAREHOUSE_CALLIOPE_TTS_BASE_URL` ·
`WAREHOUSE_CALLIOPE_TTS_FAST_MODEL` (`eleven_flash_v2_5`) ·
`WAREHOUSE_CALLIOPE_TTS_EXPRESSIVE_MODEL` (`eleven_v3`) ·
`WAREHOUSE_CALLIOPE_TTS_SAMPLE_RATE` (24000; supported PCM rates only) ·
`WAREHOUSE_CALLIOPE_TTS_PREPARE_TIMEOUT_SECONDS` (30 default; one rewrite-to-first-audio budget) ·
`WAREHOUSE_CALLIOPE_TTS_MAX_AUDIO_BYTES` (16 MiB default, 64 MiB ceiling).
**Artifact semantic compiler:** `WAREHOUSE_SEMANTIC_ENRICHMENT` (default `1`; set `0` to
disable queueing and the worker) · `WAREHOUSE_SEMANTIC_ENRICH_MODEL` (default
`openai/gpt-5.6-sol`) · `WAREHOUSE_SEMANTIC_ENRICH_MAX_ATTEMPTS` (default `3`) ·
`WAREHOUSE_SEMANTIC_ENRICH_RENDER_WAIT_MS` (default `3000`) ·
`WAREHOUSE_SEMANTIC_ENRICH_SOURCE_CHARS` (default `180000`). The database process—not
Warehouse MCP—executes the agent operator, so its configured backend must have the matching
provider key (for the default model, `OPENROUTER_API_KEY`).
**Artifact catalog enrichment:** `WAREHOUSE_ARTIFACT_CATALOG_ENRICHMENT` (`1` by
default; deterministic dashboard-link extraction and legacy crawl backfill still run when
classification is disabled) · `WAREHOUSE_ARTIFACT_CATALOG_MAX_ATTEMPTS` (3). Areas come
from the controlled `rvbbit.artifact_areas` vocabulary; automatic classification never
creates free-form categories, and `set_artifact_area` provides a durable manual override.
**Shared-key mode:** `WAREHOUSE_MCP_KEY` (bearer; unset = auth OFF, dev only) ·
`WAREHOUSE_MCP_STATIC_CALLER` (optional legacy caller label/email; auth `client_id`
stays `static-key`, default caller is `static-key`) · `WAREHOUSE_HERMES_MCP_KEY`
(distinct first-party Hermes bearer; required for delegated application identity) ·
`WAREHOUSE_HERMES_MCP_CALLER` (service actor label; defaults to the static caller).

## Deferred to Phase 1+
Application subjects are now explicit and auditable, but database-enforced per-user
identity → scoped role (tools run as the *caller's* PG scope), PII masking in
samples, `ask` (compose text-to-SQL), per-role cost caps, receipts table,
`define_metric`/`get_connection` (promote + scoped runtime DSN).
