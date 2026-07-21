# Burrow mode — one database, one door

*(Working name: a warren is many holes; a burrow is one home. Naming open.)*

**STATUS: BUILT (P0–P4), browser-verified end to end 2026-07-21** as a
real PG user through the unified origin: `marketing` (a plain Postgres
account, enrolled via `rvbbit.burrow_enroll`) signed in on the
DataRabbit login page → Hub wall rendered under her grants →
`SELECT current_user, session_user` returned `marketing / postgres`
(SET LOCAL ROLE live) → the pin action was REFUSED with `permission
denied for table hub_pins` (the GRANT wall enforcing a write) → the app
data bridge (`rvbbitQuery`) also ran as `marketing`. Stale
shared-mode sessions are rejected by whoami in pg mode and bounce to
re-login. As-built map: 0203 (`rvbbit_users`/`rvbbit_admin` +
`burrow_enroll`, superuser-enrollment refused); warehouse
`WAREHOUSE_AUTH=pg` verifier + `/auth/whoami` + `/auth/logout` +
`WAREHOUSE_LOGIN_UI=lens` PRG; ingress at `docker/origin/Caddyfile`
(`origin` compose profile, GET /login → lens, POST → warehouse,
flush_interval -1 for /mcp); lens `/login` page, whoami-introspecting
middleware (fails OPEN if the warehouse is down — SQL still refuses),
`/api/lens/mode`, picker hidden in burrow, `executeQuery {role}` +
plates render/actions threaded (audit logs `__role`), warehouse
`_conn(role=…)` on run_sql/multi + the app bridge (contextvar carries
session identity so tool schemas stay clean). v1 boundary, documented:
system/maintenance surfaces and metadata reads still run as service;
multi-statement SQL that COMMITs mid-script escapes SET LOCAL ROLE to
the (least-privilege!) service role — §7's non-superuser service rule
is the backstop.

## §1 The problem

DataRabbit-the-SQL-client is multi-database by design, and stays that
way. But there is a second, extremely common shape: **one Postgres, used
by a team, with DataRabbit + the MCP as the shared access layer on top.**
Today that deployment is held together piecemeal — Ryan's client box runs
caddy auth for some pages, the warehouse's own OAuth AS with a shared
password for MCP, lens with no auth at all behind the proxy, and env
config stitching them together. Three permission systems, none of them
the one that matters: **the database already has users, passwords,
GRANTs, and RLS.**

Burrow mode makes Postgres the identity provider and the authorization
engine for everything — lens, MCP, plates, brain — and pins DataRabbit to
one preconfigured database. Fewer moving parts than any piecemeal setup,
not more. Whatever auth the customer's Postgres already speaks (scram
users, managed-PG IAM users that present as roles), we inherit.

## §2 Why this is doctrine, not a bolt-on

The plates doctrine already says it: *"the GRANT wall is the real
enforcement; `requires_role` is affordance-gating."* Document Brain ACLs
ARE PG roles (`brain_grant`, view-as). Kit roles, action audit, metric
ownership — everything is role-shaped. But every query runs as one
service account, so the role system is a costume over a single identity.
Burrow mode is the moment the costume becomes the body:

- Session identity = a real PG role.
- Every query — lens windows, plate queries, plate ACTIONS, MCP run_sql,
  brain search — executes under `SET LOCAL ROLE <session role>`.
- GRANTs and RLS the customer's DBA already maintains become the app's
  permission system, with zero new machinery. `requires_role` and
  `pg_has_role` checks become true statements about the viewer.

This is the PostgREST / Supabase pattern (authenticate once against the
DB, mint app tokens, execute via `SET LOCAL ROLE` on pooled service
connections) — proven at enormous scale, not exotic.

## §3 What already exists (the hinges)

| Need | Hinge |
|---|---|
| OAuth AS for MCP (Claude Desktop native flow) | `auth.py` — full AS; credential check is ONE function (`_creds_ok(email, password)`: shared password + allowlist today) |
| Login flow that separates pixels from plumbing | `/login` POST contract: `{txn}` (OAuth continuation) or `{next}` (browser session) + credentials; `complete_login(txn, sub)` / `set_session(resp, sub)` |
| Session read | `read_session(request)` → subject string (email today, role tomorrow) |
| Pinned database | `RVBBIT_LENS_SEED_DSN` (first-boot seeded connection) |
| Role affordances | plates `requires_role` + `pg_has_role`, brain role ACLs, kit roles, `plate_action_log` |
| One-origin routing | the ingress from the Hub auth discussion (not yet built — P0 here) |
| Txn-scoped GUC hygiene | the `is_local=true` lesson (query_id bug) — same discipline for `SET LOCAL ROLE` |

## §4 Architecture

**Identity flow** (both UI and MCP):

1. User hits any protected surface → redirected to `/login` (ONE page,
   lens-rendered — §7).
2. POST credentials → warehouse AS verifies **against Postgres**: attempt
   a real connection as `role`/`password` (scram). Success = identity.
   The PG password is exchanged for rvbbit's own JWTs on the spot —
   never stored, never re-sent.
3. Browser flows get the `wh_session` cookie (now first-party everywhere
   thanks to the unified origin); OAuth flows continue the txn and mint
   tokens with `sub = role`.
4. Every SQL-executing surface begins its transaction with
   `SET LOCAL ROLE <sub>`. Postgres does the rest.

**Enrollment**: `SET ROLE` requires the service account to be a member of
each user role. One SECURITY DEFINER helper:
`rvbbit.burrow_enroll(p_role text)` → `GRANT p_role TO <service role>`,
plus an optional marker role (`rvbbit_users`) acting as the allowlist —
"who may log in" = "who is IN rvbbit_users", managed with plain GRANT.
Admin surfaces (System Health, accel, maintenance) gate on
`pg_has_role(sub, 'rvbbit_admin')` — the same idiom everywhere.

**Mode flag**: `RVBBIT_MODE=burrow` + `RVBBIT_BURROW_DSN` (the one
database). Exactly three narrowings, all at existing seams:
(a) auth source → PG verifier; (b) connection picker → hidden, pinned;
(c) execution → `SET LOCAL ROLE`. Default mode untouched; no fork. If a
burrow-only feature ever wants to exist, that is the fork smell — argue
here first.

## §5 Phases

**P0 — one origin (standalone value, do first).**
A ~20-line caddy/nginx service in the uber compose: `/mcp`, `/apps`,
`/d`, `/thumbs`, `/login`, `/auth/*` → warehouse-mcp; everything else →
lens. `WAREHOUSE_PUBLIC_URL` = `LENS_PUBLIC_URL` = the origin; the
Hub's env lattice collapses; iframes/cookies become first-party.
COST: the OAuth issuer changes → existing Claude connectors re-register
once. Do this BEFORE the biz-worker cohort scales.

**P1 — the verifier swap.**
`WAREHOUSE_AUTH=pg` (default `shared` = today's behavior, full
back-compat): `_creds_ok` attempts `psycopg.connect(dsn, user, password)`
(+ optional `pg_has_role(user, 'rvbbit_users')` gate). `sub` becomes the
role name; rate-limiting/lockout already exists. MCP identity is now a
PG account. The login form's "email" field becomes "username" in pg mode.

**P2 — lens joins the session.**
Warehouse exposes `GET /auth/whoami` (cookie → `{sub}`; `read_session`
already does this — no secret sharing with lens). In burrow mode, lens
server-side gates on it: no session → redirect `/login?next=…`;
connection picker pinned to `RVBBIT_BURROW_DSN`. `owner_email`/callers
throughout become the role. Hub "Mine vs Team", brain identity, receipts
attribution all light up for free.

**P3 — execution identity (the deep one, last).**
`executeQuery` grows `role?: string`, threaded from the session in
burrow mode; implementation is `SET LOCAL ROLE` inside the transaction
(txn-scoped, pooled-connection-safe, auto-reverts on commit/abort — the
is_local discipline). Same in warehouse `_conn()`/run_sql per-request.
`rvbbit.burrow_enroll()` + docs for the marker/admin roles. Plate
actions now execute as the viewer — `plate_action_log` finally logs a
meaningful principal.

**P4 — the login page becomes DataRabbit.**
Kill the one-off Python HTML for interactive use: `/login` GETs are
served by a lens page (warm-ink, the real design system, the assistant
mark) that POSTs to the same warehouse endpoint — the txn/next
continuation contract is already pixels-agnostic, so the AS keeps 100%
of the flow logic and lens owns 100% of the look. "Authorize Claude to
access your warehouse" becomes a branded moment instead of a beige form.
The Python page stays as fallback for MCP-only installs (no lens
container to render anything). Rejected: login-as-a-plate — plates
render through the DB connection and the login page must exist BEFORE
identity; chicken-egg, keep it a plain lens route.

## §6 What this dissolves

- WAREHOUSE_MCP_PLAN Phase 1 (per-user keys) — per-user IS per-role here.
- The Hub's shared-key attribution caveat; Mine/Team views.
- The brain write-side ACL gap (pre-release audit): a real principal to gate on.
- The client box's caddy-auth + shared-password + no-lens-auth patchwork
  → one door, one session, one permission system (the DBA's).

## §7 Open questions

- **Name.** Burrow is the working name; the env var surface
  (`RVBBIT_MODE`) should be settled before P2 lands.
- **Verifier transport**: connection-attempt covers scram (and managed-PG
  password auth). Cert/LDAP/Kerberos delegated setups can't be verified
  this way — those installs keep `WAREHOUSE_AUTH=shared` or front their
  own IdP; document, don't chase.
- **Session lifetime/refresh** for browser cookies (today's wh_session
  TTL is tuned for dashboard viewing; a working session in lens wants
  sliding renewal).
- **GUC bleed audit**: `SET LOCAL` reverts at txn end, but any surface
  that runs multi-statement work outside one txn needs a look (the
  detached-run path, cron-launched work — those run as service on
  purpose, but write it down).
- **Semantic op spend per role**: receipts get a principal — do
  per-role budgets/laning come with it, or later? (Later; note only.)
- **Superuser hygiene**: the service role should NOT be superuser in
  burrow mode (SET ROLE from superuser reaches anything). Document a
  least-privilege service role as part of install.
