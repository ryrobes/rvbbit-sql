-- sales foundation kit v0 — operator-owned scaffold. The third leg of
-- the trinity (scheduling / crm / sales). First shipped use of
-- requires.kits: sales composes crm (customer lookups) by declaration.
-- Functional plates are assistant-authored; switchboard ships here.
-- Idempotent; needs >= 0175 and the crm kit set up.

BEGIN;

-- ── 1. Setup DDL ─────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS sales;

CREATE TABLE IF NOT EXISTS sales.quotes (
    quote_id    text PRIMARY KEY
                DEFAULT 'q-' || substr(md5(clock_timestamp()::text || random()::text), 1, 8),
    customer_id text NOT NULL,
    title       text NOT NULL,
    amount      numeric(12,2) NOT NULL CHECK (amount >= 0),
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'sent', 'accepted', 'declined', 'expired')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz
);
CREATE INDEX IF NOT EXISTS quotes_customer ON sales.quotes (customer_id, created_at);
CREATE INDEX IF NOT EXISTS quotes_status ON sales.quotes (status);

CREATE TABLE IF NOT EXISTS sales.invoices (
    invoice_id  text PRIMARY KEY
                DEFAULT 'inv-' || substr(md5(clock_timestamp()::text || random()::text), 1, 8),
    customer_id text NOT NULL,
    quote_id    text,
    amount      numeric(12,2) NOT NULL CHECK (amount >= 0),
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'sent', 'paid', 'overdue', 'void')),
    issued_at   timestamptz NOT NULL DEFAULT now(),
    due_at      timestamptz NOT NULL,
    paid_at     timestamptz
);
CREATE INDEX IF NOT EXISTS invoices_customer ON sales.invoices (customer_id, issued_at);
CREATE INDEX IF NOT EXISTS invoices_status ON sales.invoices (status);

-- Config-as-rows (single-row): the domain-kit tweak surface.
CREATE TABLE IF NOT EXISTS sales.thresholds (
    one              boolean PRIMARY KEY DEFAULT true CHECK (one),
    big_deal_amount  numeric(12,2) NOT NULL DEFAULT 5000,
    stale_quote_days integer NOT NULL DEFAULT 14,
    default_net_days integer NOT NULL DEFAULT 30
);
INSERT INTO sales.thresholds (one) VALUES (true) ON CONFLICT DO NOTHING;

-- Fresh-shop self-fits (guarded).
DO $selffit$
BEGIN
    IF to_regclass('sales.v_quotes') IS NULL THEN
        CREATE VIEW sales.v_quotes AS
            SELECT quote_id, customer_id, title, amount, status, created_at, decided_at
            FROM sales.quotes;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'sales', 'sales.v_quotes',
           'SELECT quote_id, customer_id, title, amount, status, created_at, decided_at FROM sales.quotes',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'sales' AND target = 'sales.v_quotes');

    IF to_regclass('sales.v_invoices') IS NULL THEN
        CREATE VIEW sales.v_invoices AS
            SELECT invoice_id, customer_id, quote_id, amount, status, issued_at, due_at, paid_at
            FROM sales.invoices;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'sales', 'sales.v_invoices',
           'SELECT invoice_id, customer_id, quote_id, amount, status, issued_at, due_at, paid_at FROM sales.invoices',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'sales' AND target = 'sales.v_invoices');
END
$selffit$;

-- ── 2. Kit registration — composes crm by declaration ────────────────
SELECT rvbbit.upsert_kit(
    'sales',
    'Sales',
    'Foundation kit: quotes, pipeline, and invoices for trade shops. Requires the crm kit — quotes and invoices reference crm.v_customers.',
    $setup$
CREATE SCHEMA IF NOT EXISTS sales;
CREATE TABLE IF NOT EXISTS sales.quotes (
    quote_id    text PRIMARY KEY
                DEFAULT 'q-' || substr(md5(clock_timestamp()::text || random()::text), 1, 8),
    customer_id text NOT NULL,
    title       text NOT NULL,
    amount      numeric(12,2) NOT NULL CHECK (amount >= 0),
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'sent', 'accepted', 'declined', 'expired')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz
);
CREATE INDEX IF NOT EXISTS quotes_customer ON sales.quotes (customer_id, created_at);
CREATE INDEX IF NOT EXISTS quotes_status ON sales.quotes (status);
CREATE TABLE IF NOT EXISTS sales.invoices (
    invoice_id  text PRIMARY KEY
                DEFAULT 'inv-' || substr(md5(clock_timestamp()::text || random()::text), 1, 8),
    customer_id text NOT NULL,
    quote_id    text,
    amount      numeric(12,2) NOT NULL CHECK (amount >= 0),
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'sent', 'paid', 'overdue', 'void')),
    issued_at   timestamptz NOT NULL DEFAULT now(),
    due_at      timestamptz NOT NULL,
    paid_at     timestamptz
);
CREATE INDEX IF NOT EXISTS invoices_customer ON sales.invoices (customer_id, issued_at);
CREATE INDEX IF NOT EXISTS invoices_status ON sales.invoices (status);
CREATE TABLE IF NOT EXISTS sales.thresholds (
    one              boolean PRIMARY KEY DEFAULT true CHECK (one),
    big_deal_amount  numeric(12,2) NOT NULL DEFAULT 5000,
    stale_quote_days integer NOT NULL DEFAULT 14,
    default_net_days integer NOT NULL DEFAULT 30
);
INSERT INTO sales.thresholds (one) VALUES (true) ON CONFLICT DO NOTHING;
DO $selffit$
BEGIN
    IF to_regclass('sales.v_quotes') IS NULL THEN
        CREATE VIEW sales.v_quotes AS
            SELECT quote_id, customer_id, title, amount, status, created_at, decided_at
            FROM sales.quotes;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'sales', 'sales.v_quotes',
           'SELECT quote_id, customer_id, title, amount, status, created_at, decided_at FROM sales.quotes',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'sales' AND target = 'sales.v_quotes');
    IF to_regclass('sales.v_invoices') IS NULL THEN
        CREATE VIEW sales.v_invoices AS
            SELECT invoice_id, customer_id, quote_id, amount, status, issued_at, due_at, paid_at
            FROM sales.invoices;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'sales', 'sales.v_invoices',
           'SELECT invoice_id, customer_id, quote_id, amount, status, issued_at, due_at, paid_at FROM sales.invoices',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'sales' AND target = 'sales.v_invoices');
END
$selffit$;
$setup$,
    '0.1.0',
    '{"min_migration": "0175_kit_composition", "kits": ["crm"]}'::jsonb
);

-- ── 3. Targets ───────────────────────────────────────────────────────
SELECT rvbbit.upsert_kit_target(
    'sales', 'sales.v_quotes',
    'Canonical quote feed: one row per estimate/proposal',
    '[
      {"name": "quote_id",    "type": "text",        "required": true,  "description": "unique quote id"},
      {"name": "customer_id", "type": "text",        "required": true,  "description": "who it is for (crm.v_customers name-join in v0)"},
      {"name": "title",       "type": "text",        "required": true,  "description": "what is being quoted"},
      {"name": "amount",      "type": "numeric",     "required": true,  "description": "quoted amount"},
      {"name": "status",      "type": "text",        "required": true,  "description": "pipeline stage",
       "values": ["draft", "sent", "accepted", "declined", "expired"]},
      {"name": "created_at",  "type": "timestamptz", "required": true,  "description": "when drafted"},
      {"name": "decided_at",  "type": "timestamptz", "required": false, "description": "when accepted/declined"}
    ]'::jsonb
);
SELECT rvbbit.upsert_kit_target(
    'sales', 'sales.v_invoices',
    'Canonical invoice feed: one row per bill',
    '[
      {"name": "invoice_id",  "type": "text",        "required": true,  "description": "unique invoice id"},
      {"name": "customer_id", "type": "text",        "required": true,  "description": "who it bills (crm.v_customers name-join in v0)"},
      {"name": "quote_id",    "type": "text",        "required": false, "description": "originating quote, if any"},
      {"name": "amount",      "type": "numeric",     "required": true,  "description": "billed amount"},
      {"name": "status",      "type": "text",        "required": true,  "description": "billing state",
       "values": ["draft", "sent", "paid", "overdue", "void"]},
      {"name": "issued_at",   "type": "timestamptz", "required": true,  "description": "when issued"},
      {"name": "due_at",      "type": "timestamptz", "required": true,  "description": "when due"},
      {"name": "paid_at",     "type": "timestamptz", "required": false, "description": "when paid"}
    ]'::jsonb
);

-- ── 4. Contract on module 'sales' ────────────────────────────────────
SELECT rvbbit.upsert_kit_contract(
    'sales', 'sales', 'targets_fitted',
    'SELECT target, problem FROM rvbbit.fitting_violations(''sales'')',
    'Every kit target has an accepted fitting'
);

-- ── 5. Rules: deal_watch (quotes) + ar_watch (invoices) ──────────────
SELECT rvbbit.upsert_kit_rule(
    'sales', 'deal_watch', 'stale_quote',
    $r$subject->>'status' = 'sent' AND (subject->>'created_at')::timestamptz
        < now() - make_interval(days => (SELECT stale_quote_days FROM sales.thresholds))$r$,
    '{"label": "stale — chase it", "tone": "warn"}',
    10, 'Sent quote older than the configured staleness window'
);
SELECT rvbbit.upsert_kit_rule(
    'sales', 'deal_watch', 'big_deal',
    $r$subject->>'status' IN ('draft', 'sent') AND (subject->>'amount')::numeric
        >= (SELECT big_deal_amount FROM sales.thresholds)$r$,
    '{"label": "big deal", "tone": "ok"}',
    20, 'Open quote at or above the big-deal threshold'
);
SELECT rvbbit.upsert_kit_rule(
    'sales', 'deal_watch', 'ok',
    'true', '{"label": "in play", "tone": ""}',
    999, 'Default verdict'
);
SELECT rvbbit.upsert_kit_rule_set(
    'sales', 'deal_watch',
    $s$SELECT quote_id, customer_id, title, amount, status, created_at
       FROM sales.v_quotes WHERE status IN ('draft', 'sent')$s$,
    'Open quotes checked for staleness and size'
);

SELECT rvbbit.upsert_kit_rule(
    'sales', 'ar_watch', 'overdue',
    $r$subject->>'status' IN ('sent', 'overdue') AND (subject->>'due_at')::timestamptz < now()$r$,
    '{"label": "overdue", "tone": "bad"}',
    10, 'Unpaid invoice past its due date'
);
SELECT rvbbit.upsert_kit_rule(
    'sales', 'ar_watch', 'due_soon',
    $r$subject->>'status' = 'sent' AND (subject->>'due_at')::timestamptz < now() + interval '7 days'$r$,
    '{"label": "due this week", "tone": "warn"}',
    20, 'Unpaid invoice due within 7 days'
);
SELECT rvbbit.upsert_kit_rule(
    'sales', 'ar_watch', 'ok',
    'true', '{"label": "current", "tone": ""}',
    999, 'Default verdict'
);
SELECT rvbbit.upsert_kit_rule_set(
    'sales', 'ar_watch',
    $s$SELECT invoice_id, customer_id, amount, status, issued_at, due_at
       FROM sales.v_invoices WHERE status NOT IN ('paid', 'void', 'draft')$s$,
    'Outstanding invoices checked for collection urgency'
);

-- ── 6. Switchboard ───────────────────────────────────────────────────
SELECT rvbbit.upsert_plate(
    'sales/switchboard',
    'Sales — Switchboard',
    $tpl$
<div class="plate-section">
  <div class="flex items-start justify-between gap-4">
    <div>
      <div class="text-xs font-semibold uppercase tracking-wide text-primary">Sales</div>
      <div class="text-2xl font-bold text-foreground">Switchboard</div>
      <div class="text-sm text-muted-foreground">Foundation kit &#8212; composes the crm kit. Modules unlock when contracts go green.</div>
    </div>
    <div class="plate-toolbar">
      <button type="button" rv-open="app:fitting?kit=sales">Fitting Room &#8594;</button>
      <button type="button" rv-open="plate:system/rules">Rules &#8594;</button>
    </div>
  </div>
</div>
<div class="plate-section">
  <div class="plate-cards">
    <div rv-each="contracts" class="plate-card {{ row.tone }}">
      <div class="plate-card-title">{{ row.module }} &#183; {{ row.contract_id }}</div>
      <div class="plate-card-value">{{ row.state }}</div>
      <div class="plate-card-note">{{ row.detail }}</div>
    </div>
  </div>
</div>
<div class="plate-section">
  <h3>The book right now</h3>
  <div class="plate-cards">
    <div rv-each="pulse" class="plate-card">
      <div class="plate-card-title">{{ row.what }}</div>
      <div class="plate-card-value">{{ row.n }}</div>
      <div class="plate-card-note">{{ row.note }}</div>
    </div>
  </div>
</div>
<div class="plate-section">
  <h3>Needs attention</h3>
  <table class="plate-table">
    <thead><tr><th>what</th><th>customer</th><th class="text-right">amount</th><th>age</th><th>flag</th></tr></thead>
    <tbody>
      <tr rv-each="flags">
        <td class="font-medium">{{ row.what }}</td><td>{{ row.customer_id }}</td>
        <td class="text-right tabular-nums">{{ row.amount_txt }}</td><td class="text-muted-foreground">{{ row.age }}</td>
        <td><span class="plate-chip {{ row.tone }}" title="rule: {{ row.rule_id }}">{{ row.label }}</span></td>
      </tr>
    </tbody>
  </table>
</div>
$tpl$,
    $q$
{
  "contracts": {"sql": "SELECT module, contract_id, CASE WHEN ok THEN 'GREEN' ELSE 'RED' END AS state, CASE WHEN ok THEN 'ok' ELSE 'bad' END AS tone, CASE WHEN ok THEN coalesce(description, 'satisfied') ELSE coalesce(sample, description, '') END AS detail FROM rvbbit.kit_contract_status('sales') ORDER BY module, contract_id"},
  "pulse": {"sql": "SELECT 'open pipeline' AS what, to_char(coalesce(sum(amount), 0), 'FM$999,999,999') AS n, count(*) || ' open quotes' AS note FROM sales.v_quotes WHERE status IN ('draft', 'sent') UNION ALL SELECT 'won (90d)', to_char(coalesce(sum(amount), 0), 'FM$999,999,999'), count(*) || ' accepted' FROM sales.v_quotes WHERE status = 'accepted' AND decided_at > now() - interval '90 days' UNION ALL SELECT 'win rate (90d)', coalesce(round(100.0 * count(*) FILTER (WHERE status = 'accepted') / nullif(count(*) FILTER (WHERE status IN ('accepted', 'declined')), 0)) || '%', 'n/a'), count(*) FILTER (WHERE status IN ('accepted', 'declined')) || ' decided' FROM sales.v_quotes WHERE decided_at > now() - interval '90 days' UNION ALL SELECT 'outstanding AR', to_char(coalesce(sum(amount), 0), 'FM$999,999,999'), count(*) || ' unpaid invoices' FROM sales.v_invoices WHERE status IN ('sent', 'overdue')"},
  "flags": {"sql": "SELECT 'quote: ' || q.title AS what, q.customer_id, to_char(q.amount, 'FM$999,999') AS amount_txt, (now()::date - q.created_at::date) || 'd' AS age, r.rule_id, r.verdict->>'label' AS label, coalesce(r.verdict->>'tone', 'warn') AS tone FROM sales.v_quotes q CROSS JOIN LATERAL rvbbit.rule_verdict('sales', 'deal_watch', to_jsonb(q)) r WHERE q.status IN ('draft', 'sent') AND coalesce(r.verdict->>'tone', '') <> '' UNION ALL SELECT 'invoice ' || i.invoice_id, i.customer_id, to_char(i.amount, 'FM$999,999'), (now()::date - i.issued_at::date) || 'd', r.rule_id, r.verdict->>'label', coalesce(r.verdict->>'tone', 'warn') FROM sales.v_invoices i CROSS JOIN LATERAL rvbbit.rule_verdict('sales', 'ar_watch', to_jsonb(i)) r WHERE i.status NOT IN ('paid', 'void', 'draft') AND coalesce(r.verdict->>'tone', '') <> '' ORDER BY 5, 4 DESC LIMIT 15"}
}
$q$::jsonb,
    '{}'::jsonb,
    '[]'::jsonb,
    'sales',
    'Sales kit status: contracts, pipeline pulse, and collection flags'
);

COMMIT;
