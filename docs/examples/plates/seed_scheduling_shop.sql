-- Synthetic shop for the scheduling foundation kit: Beacon Hill Heating
-- & Air (HVAC, Boston). Demo/bench data only — never ships in the kit.
-- Deterministic (setseed) apart from now()-relative status assignment.
-- Re-runnable: wipes only its own appt-bhh-* rows + config.

BEGIN;

DELETE FROM scheduling.appointments WHERE appt_id LIKE 'appt-bhh-%';
DELETE FROM scheduling.job_types;
DELETE FROM scheduling.assignees;
DELETE FROM scheduling.hours;

INSERT INTO scheduling.job_types (name, default_minutes, buffer_minutes, tone) VALUES
    ('install',     240, 30, 'info'),
    ('repair',       90, 15, 'warn'),
    ('maintenance',  60, 15, 'ok'),
    ('inspection',   45,  0, 'ok'),
    ('emergency',   120,  0, 'bad');

INSERT INTO scheduling.assignees (name, skills, active) VALUES
    ('Marcus Webb',  '{install,repair,emergency}',                        true),
    ('Dana Okafor',  '{repair,maintenance,inspection,emergency}',         true),
    ('Chris Tran',   '{maintenance,inspection}',                          true),
    ('Sofia Reyes',  '{install,repair,maintenance,inspection,emergency}', true),
    ('Pete Gillis',  '{install}',                                         true),
    ('Ray Boudreau', '{repair,maintenance}',                              false);

-- Mon-Fri 8-5, Sat 9-1, closed Sunday (no dow 0 row).
INSERT INTO scheduling.hours (dow, open_at, close_at) VALUES
    (1, '08:00', '17:00'), (2, '08:00', '17:00'), (3, '08:00', '17:00'),
    (4, '08:00', '17:00'), (5, '08:00', '17:00'), (6, '09:00', '13:00');

-- ~170 appointments across four weeks around today, Mon-Fri only, with
-- start hours that let each job finish inside business hours. Assignees
-- only get jobs they are skilled for; every rule violation the demo
-- shows is PLANTED below, not generator noise.
SELECT setseed(0.42);
INSERT INTO scheduling.appointments
    (appt_id, customer_id, assignee, job_type, starts_at, ends_at, status, address, notes)
SELECT
    'appt-bhh-' || lpad(g.gs::text, 4, '0'),
    g.cust, asg.name, g.job, g.starts_at,
    g.starts_at + make_interval(mins => jt.default_minutes),
    CASE
      WHEN g.starts_at < now() - interval '4 hours' THEN
        (ARRAY['done','done','done','done','done','done','done','done',
               'cancelled','no_show'])[1 + floor(random() * 10)::int]
      WHEN g.starts_at < now() THEN 'in_progress'
      WHEN g.starts_at < now() + interval '3 days' THEN
        (ARRAY['confirmed','confirmed','booked'])[1 + floor(random() * 3)::int]
      ELSE (ARRAY['booked','booked','confirmed'])[1 + floor(random() * 3)::int]
    END,
    g.addr, NULL
FROM (
    SELECT d.gs, d.job, d.cust, d.addr,
           d.wd + make_interval(
               hours => CASE WHEN d.job IN ('install', 'emergency')
                             THEN 8 + floor(random() * 5)::int    -- ends by 17:00
                             ELSE 8 + floor(random() * 8)::int END,
               mins  => (ARRAY[0, 30])[1 + floor(random() * 2)::int]) AS starts_at
    FROM (
        -- weekday in [-14d, +13d]: Sun -> Mon, Sat -> following Mon.
        -- d0 must be a per-row SELECT-list expression, NOT a scalar
        -- subquery — the planner runs an uncorrelated subquery ONCE
        -- (InitPlan) and every row lands on the same day.
        SELECT raw.gs, raw.job, raw.cust, raw.addr,
               raw.d0 + (CASE extract(dow FROM raw.d0)
                         WHEN 0 THEN 1 WHEN 6 THEN 2 ELSE 0 END)::int AS wd
        FROM (
            SELECT gs,
                current_date + (floor(random() * 28)::int - 14) AS d0,
                (ARRAY['maintenance','maintenance','maintenance','repair','repair','repair',
                       'inspection','inspection','install','emergency'])[1 + floor(random() * 10)::int] AS job,
                (ARRAY['Alice Fontaine','Bob Kowalski','Carmen Diaz','Dev Patel','Elena Rossi',
                       'Frank OMeara','Grace Chen','Hank Sullivan','Iris Nakamura','Joe Bishop',
                       'Karen Doyle','Leo Martins','Mia Torres','Ned Flaherty','Olga Petrov',
                       'Paul Nguyen','Quinn Murphy','Rosa Alvarez','Sam Whitaker','Tess Gallagher'])[1 + floor(random() * 20)::int] AS cust,
                (floor(random() * 180)::int + 1) || ' ' ||
                (ARRAY['Charles St','Beacon St','Pinckney St','Mt Vernon St','Revere St',
                       'Myrtle St','Joy St','Chestnut St','Cambridge St','Hancock St'])[1 + floor(random() * 10)::int] AS addr
            FROM generate_series(1, 170) gs
        ) raw
    ) d
) g
CROSS JOIN LATERAL (
    SELECT name FROM scheduling.assignees
    WHERE active AND g.job = ANY (skills)
    ORDER BY md5(g.gs::text || name) LIMIT 1
) asg
JOIN scheduling.job_types jt ON jt.name = g.job;

-- Planted anomalies for the day_check rules, anchored to NEXT week
-- (Mon..Fri + the Sunday after) so they are always upcoming and inside
-- the 14-day rule-set window: two double-bookings, a Sunday call, an
-- after-hours call, two skill mismatches (Pete on inspection, Chris on
-- install).
WITH mon AS (
    SELECT date_trunc('week', current_date::timestamp + interval '7 days')::date AS d
)
INSERT INTO scheduling.appointments
    (appt_id, customer_id, assignee, job_type, starts_at, ends_at, status, address, notes)
SELECT x.appt_id, x.cust, x.assignee, x.job,
       mon.d + x.day_off * interval '1 day' + x.start_h,
       mon.d + x.day_off * interval '1 day' + x.end_h,
       x.status, x.addr, x.note
FROM mon, (VALUES
    ('appt-bhh-x001', 'Grace Chen',   'Marcus Webb', 'repair',      0,
     interval '10 hours',   interval '11.5 hours',  'confirmed', '44 Beacon St',    'planted: overlaps x002'),
    ('appt-bhh-x002', 'Joe Bishop',   'Marcus Webb', 'repair',      0,
     interval '11 hours',   interval '12.5 hours',  'booked',    '17 Joy St',       'planted: overlaps x001'),
    ('appt-bhh-x003', 'Karen Doyle',  'Sofia Reyes', 'install',     1,
     interval '9 hours',    interval '13 hours',    'confirmed', '102 Charles St',  'planted: overlaps x004'),
    ('appt-bhh-x004', 'Leo Martins',  'Sofia Reyes', 'inspection',  1,
     interval '12 hours',   interval '12.75 hours', 'booked',    '9 Myrtle St',     'planted: overlaps x003'),
    ('appt-bhh-x005', 'Ned Flaherty', 'Dana Okafor', 'emergency',   6,
     interval '14 hours',   interval '16 hours',    'booked',    '61 Revere St',    'planted: Sunday'),
    ('appt-bhh-x006', 'Olga Petrov',  'Dana Okafor', 'repair',      2,
     interval '19 hours',   interval '20.5 hours',  'booked',    '88 Cambridge St', 'planted: after hours'),
    ('appt-bhh-x007', 'Paul Nguyen',  'Pete Gillis', 'inspection',  3,
     interval '10 hours',   interval '10.75 hours', 'booked',    '31 Hancock St',   'planted: skill mismatch'),
    ('appt-bhh-x008', 'Rosa Alvarez', 'Chris Tran',  'install',     4,
     interval '8 hours',    interval '12 hours',    'booked',    '140 Chestnut St', 'planted: skill mismatch')
) AS x(appt_id, cust, assignee, job, day_off, start_h, end_h, status, addr, note);

-- The generator does not avoid same-assignee collisions, so prune
-- generated rows that overlap an earlier generated row OR any planted
-- row (greedy; slight over-delete on chains is fine for a demo). After
-- this, every surviving rule violation is a planted one.
DELETE FROM scheduling.appointments a
USING scheduling.appointments b
WHERE a.appt_id ~ '^appt-bhh-[0-9]+$'
  AND a.assignee = b.assignee
  AND a.appt_id <> b.appt_id
  AND tstzrange(a.starts_at, a.ends_at) && tstzrange(b.starts_at, b.ends_at)
  AND (b.appt_id LIKE 'appt-bhh-x%' OR b.appt_id < a.appt_id);

COMMIT;
