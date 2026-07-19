-- scheduling foundation kit v0 — the operator-owned scaffold.
-- Canon: docs/FOUNDATION_KITS_PLAN.md. Functional plates (today board /
-- week list / intake) are assistant-authored and NOT in this file; the
-- switchboard is infrastructure, so it ships here.
-- Idempotent; needs migrations >= 0175. Safe to re-run.

BEGIN;

-- ── 1. Setup DDL (also stored as the kit's setup_sql below) ──────────
CREATE SCHEMA IF NOT EXISTS scheduling;

CREATE TABLE IF NOT EXISTS scheduling.appointments (
    appt_id     text PRIMARY KEY
                DEFAULT 'appt-' || substr(md5(clock_timestamp()::text || random()::text), 1, 10),
    customer_id text NOT NULL,
    assignee    text NOT NULL,
    job_type    text NOT NULL,
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    status      text NOT NULL DEFAULT 'booked'
                CHECK (status IN ('booked','confirmed','in_progress','done','cancelled','no_show')),
    address     text,
    notes       text,
    lat         double precision,
    lon         double precision,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS appointments_assignee_time ON scheduling.appointments (assignee, starts_at);
CREATE INDEX IF NOT EXISTS appointments_time ON scheduling.appointments (starts_at);

-- Config-as-rows: the tweak surface domain kits SEED (never fork).
CREATE TABLE IF NOT EXISTS scheduling.job_types (
    name            text PRIMARY KEY,
    default_minutes integer NOT NULL DEFAULT 60,
    buffer_minutes  integer NOT NULL DEFAULT 0,
    tone            text NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS scheduling.assignees (
    name   text PRIMARY KEY,
    skills text[] NOT NULL DEFAULT '{}',   -- empty = generalist
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS scheduling.hours (
    dow      integer PRIMARY KEY CHECK (dow BETWEEN 0 AND 6),  -- 0 = Sunday
    open_at  time,
    close_at time                                              -- NULL = closed
);

-- Conventions live in the catalog (0184): the assistant reads column
-- comments before assuming an encoding, so every non-obvious convention
-- gets one here.
COMMENT ON COLUMN scheduling.hours.dow IS 'PostgreSQL extract(dow) numbering: 0=Sunday .. 6=Saturday (NOT isodow)';
COMMENT ON COLUMN scheduling.hours.close_at IS 'NULL open_at/close_at = closed that day';
COMMENT ON COLUMN scheduling.appointments.status IS 'booked -> confirmed -> in_progress -> done; cancelled and no_show are terminal';
COMMENT ON COLUMN scheduling.assignees.skills IS 'empty array = generalist (matches any job_type)';
COMMENT ON COLUMN scheduling.job_types.buffer_minutes IS 'travel/cleanup padding after default_minutes when proposing slots';

-- Fresh-shop SELF-FIT: the kit's own table IS the canon. Guarded twice
-- so a customer's accepted fitting over their existing data is never
-- clobbered by setup re-runs/upgrades.
DO $selffit$
BEGIN
    IF to_regclass('scheduling.v_appointments') IS NULL THEN
        CREATE VIEW scheduling.v_appointments AS
            SELECT appt_id, customer_id, assignee, job_type, starts_at,
                   ends_at, status, address, notes, lat, lon
            FROM scheduling.appointments;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'scheduling', 'scheduling.v_appointments',
           'SELECT appt_id, customer_id, assignee, job_type, starts_at, ends_at, status, address, notes, lat, lon FROM scheduling.appointments',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'scheduling' AND target = 'scheduling.v_appointments');
END
$selffit$;

-- ── 2. Kit registration ──────────────────────────────────────────────
SELECT rvbbit.upsert_kit(
    'scheduling',
    'Scheduling',
    'Foundation kit: appointments, crew, and business hours for trade shops. Domain kits compose it via requires.kits and seed the config tables.',
    $setup$
CREATE SCHEMA IF NOT EXISTS scheduling;
CREATE TABLE IF NOT EXISTS scheduling.appointments (
    appt_id     text PRIMARY KEY
                DEFAULT 'appt-' || substr(md5(clock_timestamp()::text || random()::text), 1, 10),
    customer_id text NOT NULL,
    assignee    text NOT NULL,
    job_type    text NOT NULL,
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    status      text NOT NULL DEFAULT 'booked'
                CHECK (status IN ('booked','confirmed','in_progress','done','cancelled','no_show')),
    address     text,
    notes       text,
    lat         double precision,
    lon         double precision,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS appointments_assignee_time ON scheduling.appointments (assignee, starts_at);
CREATE INDEX IF NOT EXISTS appointments_time ON scheduling.appointments (starts_at);
CREATE TABLE IF NOT EXISTS scheduling.job_types (
    name            text PRIMARY KEY,
    default_minutes integer NOT NULL DEFAULT 60,
    buffer_minutes  integer NOT NULL DEFAULT 0,
    tone            text NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS scheduling.assignees (
    name   text PRIMARY KEY,
    skills text[] NOT NULL DEFAULT '{}',
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS scheduling.hours (
    dow      integer PRIMARY KEY CHECK (dow BETWEEN 0 AND 6),
    open_at  time,
    close_at time
);
COMMENT ON COLUMN scheduling.hours.dow IS 'PostgreSQL extract(dow) numbering: 0=Sunday .. 6=Saturday (NOT isodow)';
COMMENT ON COLUMN scheduling.hours.close_at IS 'NULL open_at/close_at = closed that day';
COMMENT ON COLUMN scheduling.appointments.status IS 'booked -> confirmed -> in_progress -> done; cancelled and no_show are terminal';
COMMENT ON COLUMN scheduling.assignees.skills IS 'empty array = generalist (matches any job_type)';
COMMENT ON COLUMN scheduling.job_types.buffer_minutes IS 'travel/cleanup padding after default_minutes when proposing slots';
DO $selffit$
BEGIN
    IF to_regclass('scheduling.v_appointments') IS NULL THEN
        CREATE VIEW scheduling.v_appointments AS
            SELECT appt_id, customer_id, assignee, job_type, starts_at,
                   ends_at, status, address, notes, lat, lon
            FROM scheduling.appointments;
    END IF;
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, accepted_by, proposal)
    SELECT 'scheduling', 'scheduling.v_appointments',
           'SELECT appt_id, customer_id, assignee, job_type, starts_at, ends_at, status, address, notes, lat, lon FROM scheduling.appointments',
           'setup (self-fit)', '{"drafted_by": "self-fit"}'::jsonb
    WHERE NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings
                      WHERE kit = 'scheduling' AND target = 'scheduling.v_appointments');
END
$selffit$;
$setup$,
    '0.1.0',
    '{"min_migration": "0175_kit_composition"}'::jsonb
);

-- ── 3. Target (the fittable spine; values = closed vocabulary) ───────
SELECT rvbbit.upsert_kit_target(
    'scheduling', 'scheduling.v_appointments',
    'Canonical appointments feed: one row per scheduled visit',
    '[
      {"name": "appt_id",     "type": "text",        "required": true,  "description": "unique appointment id"},
      {"name": "customer_id", "type": "text",        "required": true,  "description": "customer identifier (free-form name/phone is fine until the crm kit composes in)"},
      {"name": "assignee",    "type": "text",        "required": true,  "description": "tech / crew / room the visit is assigned to"},
      {"name": "job_type",    "type": "text",        "required": true,  "description": "kind of work; should match scheduling.job_types"},
      {"name": "starts_at",   "type": "timestamptz", "required": true,  "description": "scheduled start"},
      {"name": "ends_at",     "type": "timestamptz", "required": true,  "description": "scheduled end"},
      {"name": "status",      "type": "text",        "required": true,  "description": "lifecycle state",
       "values": ["booked", "confirmed", "in_progress", "done", "cancelled", "no_show"]},
      {"name": "address",     "type": "text",        "required": false, "description": "service address"},
      {"name": "notes",       "type": "text",        "required": false, "description": "free-text notes"},
      {"name": "lat",         "type": "double precision", "required": false, "description": "service location latitude"},
      {"name": "lon",         "type": "double precision", "required": false, "description": "service location longitude"}
    ]'::jsonb
);

-- ── 4. Contracts on module 'operations' ──────────────────────────────
SELECT rvbbit.upsert_kit_contract(
    'scheduling', 'operations', 'targets_fitted',
    'SELECT target, problem FROM rvbbit.fitting_violations(''scheduling'')',
    'Every kit target has an accepted fitting'
);
SELECT rvbbit.upsert_kit_contract(
    'scheduling', 'operations', 'config_seeded',
    $c$
    SELECT 'no job types configured — seed scheduling.job_types' AS problem
    WHERE NOT EXISTS (SELECT 1 FROM scheduling.job_types)
    UNION ALL
    SELECT 'no active crew — seed scheduling.assignees'
    WHERE NOT EXISTS (SELECT 1 FROM scheduling.assignees WHERE active)
    UNION ALL
    SELECT 'business hours not set — seed scheduling.hours'
    WHERE NOT EXISTS (SELECT 1 FROM scheduling.hours WHERE open_at IS NOT NULL)
    $c$,
    'Job types, active crew, and business hours are configured'
);

-- ── 5. Rules: the day_check decision table ───────────────────────────
SELECT rvbbit.upsert_kit_rule(
    'scheduling', 'day_check', 'double_booked',
    $r$EXISTS (SELECT 1 FROM scheduling.v_appointments o
               WHERE o.assignee = subject->>'assignee'
                 AND o.appt_id <> subject->>'appt_id'
                 AND o.status NOT IN ('cancelled', 'no_show')
                 AND tstzrange(o.starts_at, o.ends_at)
                     && tstzrange((subject->>'starts_at')::timestamptz,
                                  (subject->>'ends_at')::timestamptz))$r$,
    '{"label": "double-booked", "tone": "bad"}',
    10, 'Overlaps another live appointment for the same assignee'
);
SELECT rvbbit.upsert_kit_rule(
    'scheduling', 'day_check', 'outside_hours',
    $r$NOT EXISTS (SELECT 1 FROM scheduling.hours h
                   WHERE h.dow = extract(dow FROM (subject->>'starts_at')::timestamptz)::int
                     AND h.open_at IS NOT NULL
                     AND ((subject->>'starts_at')::timestamptz)::time >= h.open_at
                     AND ((subject->>'ends_at')::timestamptz)::time <= h.close_at)$r$,
    '{"label": "outside hours", "tone": "warn"}',
    20, 'Falls outside configured business hours'
);
SELECT rvbbit.upsert_kit_rule(
    'scheduling', 'day_check', 'skill_mismatch',
    $r$EXISTS (SELECT 1 FROM scheduling.assignees a
               WHERE a.name = subject->>'assignee'
                 AND cardinality(a.skills) > 0
                 AND NOT (subject->>'job_type' = ANY (a.skills)))$r$,
    '{"label": "skill mismatch", "tone": "warn"}',
    30, 'Assignee does not list this job type as a skill (empty skills = generalist)'
);
SELECT rvbbit.upsert_kit_rule(
    'scheduling', 'day_check', 'ok',
    'true', '{"label": "on track", "tone": "ok"}',
    999, 'Default verdict'
);
SELECT rvbbit.upsert_kit_rule_set(
    'scheduling', 'day_check',
    $s$SELECT appt_id, customer_id, assignee, job_type, starts_at, ends_at, status
       FROM scheduling.v_appointments
       WHERE starts_at >= now() - interval '1 day'
         AND starts_at <  now() + interval '14 days'
         AND status NOT IN ('cancelled')$s$,
    'Upcoming appointments checked for double-booking, hours, and skills'
);

-- ── 6. Switchboard (infrastructure plate; no module → always renders) ─
SELECT rvbbit.upsert_plate(
    'scheduling/switchboard',
    'Scheduling — Switchboard',
    $tpl$
<div class="plate-section">
  <h2>Scheduling</h2>
  <p>Foundation kit. Modules unlock when the contracts below go green.</p>
  <div class="plate-toolbar">
    <button type="button" rv-open="app:fitting?kit=scheduling">Open the Fitting Room &#8594;</button>
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
  <h3>Shop configuration</h3>
  <div class="plate-cards">
    <div rv-each="config" class="plate-card">
      <div class="plate-card-title">{{ row.what }}</div>
      <div class="plate-card-value">{{ row.n }}</div>
      <div class="plate-card-note">{{ row.note }}</div>
    </div>
  </div>
</div>
<div class="plate-section">
  <h3>Needs attention (next 14 days)</h3>
  <table class="plate-table">
    <thead><tr><th>when</th><th>customer</th><th>assignee</th><th>job</th><th>flag</th></tr></thead>
    <tbody>
      <tr rv-each="flags">
        <td>{{ row.at_txt }}</td><td>{{ row.customer_id }}</td>
        <td><b>{{ row.assignee }}</b></td><td>{{ row.job_type }}</td>
        <td><span class="plate-chip {{ row.tone }}" title="rule: {{ row.rule_id }}">{{ row.label }}</span></td>
      </tr>
    </tbody>
  </table>
</div>
$tpl$,
    $q$
{
  "contracts": {"sql": "SELECT module, contract_id, CASE WHEN ok THEN 'GREEN' ELSE 'RED' END AS state, CASE WHEN ok THEN 'ok' ELSE 'bad' END AS tone, CASE WHEN ok THEN coalesce(description, 'satisfied') ELSE coalesce(sample, description, '') END AS detail FROM rvbbit.kit_contract_status('scheduling') ORDER BY module, contract_id"},
  "config": {"sql": "SELECT 'job types' AS what, count(*)::int AS n, string_agg(name, ', ' ORDER BY name) AS note FROM scheduling.job_types UNION ALL SELECT 'active crew', count(*)::int, string_agg(name, ', ' ORDER BY name) FROM scheduling.assignees WHERE active UNION ALL SELECT 'open days / week', count(*)::int, min(open_at)::text || ' - ' || max(close_at)::text FROM scheduling.hours WHERE open_at IS NOT NULL UNION ALL SELECT 'appointments today', count(*)::int, NULL FROM scheduling.v_appointments WHERE starts_at::date = current_date"},
  "flags": {"sql": "SELECT to_char(v.starts_at, 'Dy MM-DD HH24:MI') AS at_txt, v.customer_id, v.assignee, v.job_type, r.rule_id, r.verdict->>'label' AS label, coalesce(r.verdict->>'tone', 'warn') AS tone FROM scheduling.v_appointments v CROSS JOIN LATERAL rvbbit.rule_verdict('scheduling', 'day_check', to_jsonb(v)) r WHERE v.starts_at >= now() - interval '1 day' AND v.starts_at < now() + interval '14 days' AND v.status NOT IN ('cancelled', 'done') AND coalesce(r.verdict->>'tone', 'ok') <> 'ok' ORDER BY v.starts_at LIMIT 12"}
}
$q$::jsonb,
    '{}'::jsonb,
    '[]'::jsonb,
    'scheduling',
    'Scheduling kit status: contracts, shop configuration, and rule flags'
);

COMMIT;
