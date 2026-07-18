-- crm foundation kit v0 — operator-owned scaffold (canon:
-- docs/FOUNDATION_KITS_PLAN.md). Functional plates are assistant-
-- authored; the switchboard ships here. Idempotent; needs >= 0175.

BEGIN;

-- ── 1. Setup DDL ─────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS crm;

CREATE TABLE IF NOT EXISTS crm.customers (
    customer_id text PRIMARY KEY
                DEFAULT 'cust-' || substr(md5(clock_timestamp()::text || random()::text), 1, 8),
    name        text NOT NULL,
    phone       text,
    email       text,
    address     text,
    status      text NOT NULL DEFAULT 'lead'
                CHECK (status IN ('lead', 'active', 'lapsed')),
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS customers_name ON crm.customers (name);

CREATE TABLE IF NOT EXISTS crm.interactions (
    interaction_id text PRIMARY KEY
                   DEFAULT 'int-' || substr(md5(clock_timestamp()::text || random()::text), 1, 10),
    customer_id    text NOT NULL,
    at             timestamptz NOT NULL DEFAULT now(),
    channel        text NOT NULL
                   CHECK (channel IN ('call', 'text', 'email', 'visit', 'job', 'note')),
    summary        text NOT NULL,
    outcome        text
);
CREATE INDEX IF NOT EXISTS interactions_customer ON crm.interactions (customer_id, at);

-- Fresh-shop self-fits (guarded; never clobber an accepted fitting).
DO $selffit$
BEGIN
    IF to_regclass('crm.v_customers') IS NULL THEN
        CREATE VIEW crm.v_customers AS
            SELECT customer_id, name, phone, email, address, status,
                   first_seen, last_seen
            FROM crm.customers;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'crm', 'crm.v_customers',
           'SELECT customer_id, name, phone, email, address, status, first_seen, last_seen FROM crm.customers',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'crm' AND target = 'crm.v_customers');

    IF to_regclass('crm.v_interactions') IS NULL THEN
        CREATE VIEW crm.v_interactions AS
            SELECT interaction_id, customer_id, at, channel, summary, outcome
            FROM crm.interactions;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'crm', 'crm.v_interactions',
           'SELECT interaction_id, customer_id, at, channel, summary, outcome FROM crm.interactions',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'crm' AND target = 'crm.v_interactions');
END
$selffit$;

-- ── 2. Kit registration ──────────────────────────────────────────────
SELECT rvbbit.upsert_kit(
    'crm',
    'CRM',
    'Foundation kit: customers and interactions for trade shops. Domain kits compose it via requires.kits; scheduling surfaces look customers up in crm.v_customers.',
    $setup$
CREATE SCHEMA IF NOT EXISTS crm;
CREATE TABLE IF NOT EXISTS crm.customers (
    customer_id text PRIMARY KEY
                DEFAULT 'cust-' || substr(md5(clock_timestamp()::text || random()::text), 1, 8),
    name        text NOT NULL,
    phone       text,
    email       text,
    address     text,
    status      text NOT NULL DEFAULT 'lead'
                CHECK (status IN ('lead', 'active', 'lapsed')),
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS customers_name ON crm.customers (name);
CREATE TABLE IF NOT EXISTS crm.interactions (
    interaction_id text PRIMARY KEY
                   DEFAULT 'int-' || substr(md5(clock_timestamp()::text || random()::text), 1, 10),
    customer_id    text NOT NULL,
    at             timestamptz NOT NULL DEFAULT now(),
    channel        text NOT NULL
                   CHECK (channel IN ('call', 'text', 'email', 'visit', 'job', 'note')),
    summary        text NOT NULL,
    outcome        text
);
CREATE INDEX IF NOT EXISTS interactions_customer ON crm.interactions (customer_id, at);
DO $selffit$
BEGIN
    IF to_regclass('crm.v_customers') IS NULL THEN
        CREATE VIEW crm.v_customers AS
            SELECT customer_id, name, phone, email, address, status,
                   first_seen, last_seen
            FROM crm.customers;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'crm', 'crm.v_customers',
           'SELECT customer_id, name, phone, email, address, status, first_seen, last_seen FROM crm.customers',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'crm' AND target = 'crm.v_customers');
    IF to_regclass('crm.v_interactions') IS NULL THEN
        CREATE VIEW crm.v_interactions AS
            SELECT interaction_id, customer_id, at, channel, summary, outcome
            FROM crm.interactions;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'crm', 'crm.v_interactions',
           'SELECT interaction_id, customer_id, at, channel, summary, outcome FROM crm.interactions',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'crm' AND target = 'crm.v_interactions');
END
$selffit$;
$setup$,
    '0.1.0',
    '{"min_migration": "0175_kit_composition"}'::jsonb
);

-- ── 3. Targets (values = closed vocabularies) ────────────────────────
SELECT rvbbit.upsert_kit_target(
    'crm', 'crm.v_customers',
    'Canonical customer directory: one row per customer',
    '[
      {"name": "customer_id", "type": "text",        "required": true,  "description": "stable customer id"},
      {"name": "name",        "type": "text",        "required": true,  "description": "customer display name"},
      {"name": "phone",       "type": "text",        "required": false, "description": "phone number"},
      {"name": "email",       "type": "text",        "required": false, "description": "email address"},
      {"name": "address",     "type": "text",        "required": false, "description": "service/billing address"},
      {"name": "status",      "type": "text",        "required": true,  "description": "lifecycle state",
       "values": ["lead", "active", "lapsed"]},
      {"name": "first_seen",  "type": "timestamptz", "required": true,  "description": "first contact"},
      {"name": "last_seen",   "type": "timestamptz", "required": true,  "description": "most recent contact"}
    ]'::jsonb
);
SELECT rvbbit.upsert_kit_target(
    'crm', 'crm.v_interactions',
    'Canonical interaction feed: one row per touchpoint with a customer',
    '[
      {"name": "interaction_id", "type": "text",        "required": true,  "description": "unique interaction id"},
      {"name": "customer_id",    "type": "text",        "required": true,  "description": "who it was with (crm.v_customers.customer_id)"},
      {"name": "at",             "type": "timestamptz", "required": true,  "description": "when it happened"},
      {"name": "channel",        "type": "text",        "required": true,  "description": "touchpoint kind",
       "values": ["call", "text", "email", "visit", "job", "note"]},
      {"name": "summary",        "type": "text",        "required": true,  "description": "what happened"},
      {"name": "outcome",        "type": "text",        "required": false, "description": "result / next step"}
    ]'::jsonb
);

-- ── 4. Contract on module 'customers' ────────────────────────────────
SELECT rvbbit.upsert_kit_contract(
    'crm', 'customers', 'targets_fitted',
    'SELECT target, problem FROM rvbbit.fitting_violations(''crm'')',
    'Every kit target has an accepted fitting'
);

-- ── 5. Rules: the follow_up decision table ───────────────────────────
SELECT rvbbit.upsert_kit_rule(
    'crm', 'follow_up', 'gone_quiet',
    $r$subject->>'status' = 'active' AND NOT EXISTS (
        SELECT 1 FROM crm.v_interactions i
        WHERE i.customer_id = subject->>'customer_id'
          AND i.at > now() - interval '30 days')$r$,
    '{"label": "overdue follow-up", "tone": "warn"}',
    10, 'Active customer with no touchpoint in 30 days'
);
SELECT rvbbit.upsert_kit_rule(
    'crm', 'follow_up', 'hot_lead',
    $r$subject->>'status' = 'lead' AND EXISTS (
        SELECT 1 FROM crm.v_interactions i
        WHERE i.customer_id = subject->>'customer_id'
          AND i.at > now() - interval '7 days')$r$,
    '{"label": "hot lead", "tone": "ok"}',
    20, 'Lead with a touchpoint in the last 7 days — strike now'
);
SELECT rvbbit.upsert_kit_rule(
    'crm', 'follow_up', 'cold_lead',
    $r$subject->>'status' = 'lead' AND NOT EXISTS (
        SELECT 1 FROM crm.v_interactions i
        WHERE i.customer_id = subject->>'customer_id'
          AND i.at > now() - interval '21 days')$r$,
    '{"label": "cold lead", "tone": "warn"}',
    30, 'Lead untouched for 3 weeks'
);
SELECT rvbbit.upsert_kit_rule(
    'crm', 'follow_up', 'ok',
    'true', '{"label": "current", "tone": "ok"}',
    999, 'Default verdict'
);
SELECT rvbbit.upsert_kit_rule_set(
    'crm', 'follow_up',
    'SELECT customer_id, name, status FROM crm.v_customers',
    'Customers checked for follow-up urgency'
);

-- ── 6. Switchboard ───────────────────────────────────────────────────
SELECT rvbbit.upsert_plate(
    'crm/switchboard',
    'CRM — Switchboard',
    $tpl$
<div class="plate-section">
  <h2>CRM</h2>
  <p>Foundation kit. Modules unlock when the contracts below go green.</p>
  <div class="plate-toolbar">
    <button type="button" rv-open="app:fitting?kit=crm">Open the Fitting Room &#8594;</button>
    <button type="button" rv-open="plate:system/rules">Rule observability &#8594;</button>
  </div>
  <div class="plate-cards">
    <div rv-each="contracts" class="plate-card {{ row.tone }}">
      <div class="plate-card-title">{{ row.module }} &#183; {{ row.contract_id }}</div>
      <div class="plate-card-value">{{ row.state }}</div>
      <div class="plate-card-note">{{ row.detail }}</div>
    </div>
  </div>
</div>
<div class="plate-section">
  <h3>Book of business</h3>
  <div class="plate-cards">
    <div rv-each="counts" class="plate-card">
      <div class="plate-card-title">{{ row.what }}</div>
      <div class="plate-card-value">{{ row.n }}</div>
      <div class="plate-card-note">{{ row.note }}</div>
    </div>
  </div>
</div>
<div class="plate-section">
  <h3>Follow-up queue</h3>
  <table class="plate-table">
    <thead><tr><th>customer</th><th>status</th><th>last touch</th><th>flag</th></tr></thead>
    <tbody>
      <tr rv-each="queue">
        <td><b>{{ row.name }}</b></td><td>{{ row.status }}</td><td>{{ row.last_touch }}</td>
        <td><span class="plate-chip {{ row.tone }}" title="rule: {{ row.rule_id }}">{{ row.label }}</span></td>
      </tr>
    </tbody>
  </table>
</div>
$tpl$,
    $q$
{
  "contracts": {"sql": "SELECT module, contract_id, CASE WHEN ok THEN 'GREEN' ELSE 'RED' END AS state, CASE WHEN ok THEN 'ok' ELSE 'bad' END AS tone, CASE WHEN ok THEN coalesce(description, 'satisfied') ELSE coalesce(sample, description, '') END AS detail FROM rvbbit.kit_contract_status('crm') ORDER BY module, contract_id"},
  "counts": {"sql": "SELECT initcap(status) AS what, count(*)::int AS n, NULL::text AS note FROM crm.v_customers GROUP BY status UNION ALL SELECT 'interactions (30d)', count(*)::int, NULL FROM crm.v_interactions WHERE at > now() - interval '30 days' ORDER BY 1"},
  "queue": {"sql": "SELECT c.name, c.status, coalesce(to_char((SELECT max(i.at) FROM crm.v_interactions i WHERE i.customer_id = c.customer_id), 'MM-DD HH24:MI'), 'never') AS last_touch, r.rule_id, r.verdict->>'label' AS label, coalesce(r.verdict->>'tone', 'warn') AS tone FROM crm.v_customers c CROSS JOIN LATERAL rvbbit.rule_verdict('crm', 'follow_up', to_jsonb(c)) r WHERE coalesce(r.verdict->>'tone', 'ok') <> 'ok' ORDER BY c.last_seen LIMIT 15"}
}
$q$::jsonb,
    '{}'::jsonb,
    '[]'::jsonb,
    'crm',
    'CRM kit status: contracts, book of business, and the follow-up queue'
);

COMMIT;
