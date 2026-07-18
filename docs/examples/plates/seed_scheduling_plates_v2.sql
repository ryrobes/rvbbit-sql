-- scheduling/today + scheduling/week, rebuilt on the tier-1 palette and
-- rv-group — the reference examples for placement-as-data:
--   week:  plate-cal calendar grid; SQL computes each chip's day column
--          (c1..c7) and each day's capacity bar width (w0..w100). TWO
--          queries replace the fourteen copy-pasted per-day ones.
--   today: rv-group="board:assignee" — crew columns come from ROWS
--          (new hire appears, deactivated tech disappears, zero
--          re-authoring), idle techs get an honest plate-empty.
-- Replaces the assistant's v1 rows; actions unchanged in spirit.

BEGIN;

SELECT rvbbit.upsert_plate(
    'scheduling/week',
    'Scheduling — Next 7 Days',
    $tpl$
<div class="plate-section">
  <div class="plate-banner">
    <div class="plate-banner-big">Next 7 Days</div>
    <div class="plate-banner-note">Capacity and bookings, one column per day</div>
  </div>
  <div class="plate-toolbar">
    <button type="button" rv-open="plate:scheduling/today">Today</button>
    <button type="button" rv-open="plate:scheduling/intake">Book appointment</button>
  </div>
</div>
<div class="plate-cal">
  <div rv-each="days" class="plate-cal-head {{ row.cell }}">
    <b>{{ row.day_label }}</b>
    <div class="plate-bar"><div class="{{ row.wclass }} {{ row.bar_tone }}"></div></div>
    <span>{{ row.cap_note }}</span>
  </div>
  <div rv-each="appts" class="plate-card plate-cal-chip {{ row.cell }} {{ row.tone }}">
    <div class="plate-card-title">{{ row.start_time }} <span class="plate-avatar" title="{{ row.assignee }}">{{ row.initials }}</span></div>
    <div class="plate-card-value">{{ row.customer }}</div>
    <div class="plate-card-note">{{ row.job_type }} &#183; {{ row.status }}</div>
  </div>
</div>
$tpl$,
    $q$
{
  "days": {"sql": "WITH d AS (SELECT current_date + o AS day, o + 1 AS pos FROM generate_series(0, 6) o), crew AS (SELECT count(*)::int AS n FROM scheduling.assignees WHERE active), cap AS (SELECT d.day, d.pos, coalesce(round(extract(epoch FROM (h.close_at - h.open_at)) / 60)::int, 0) * crew.n AS avail FROM d CROSS JOIN crew LEFT JOIN scheduling.hours h ON h.dow = extract(dow FROM d.day)::int AND h.open_at IS NOT NULL), b AS (SELECT starts_at::date AS day, round(sum(extract(epoch FROM (ends_at - starts_at)) / 60))::int AS booked FROM scheduling.v_appointments WHERE status NOT IN ('cancelled', 'no_show') AND starts_at >= current_date AND starts_at < current_date + 7 GROUP BY 1) SELECT to_char(cap.day, 'Dy FMDD Mon') AS day_label, 'c' || cap.pos AS cell, CASE WHEN cap.avail = 0 THEN 'w100' ELSE 'w' || (least(100, round(coalesce(b.booked, 0) * 100.0 / cap.avail / 5) * 5))::int END AS wclass, CASE WHEN cap.avail = 0 THEN 'bad' WHEN coalesce(b.booked, 0) >= cap.avail THEN 'bad' WHEN coalesce(b.booked, 0) >= cap.avail * 0.8 THEN 'warn' ELSE 'ok' END AS bar_tone, CASE WHEN cap.avail = 0 THEN 'closed' ELSE coalesce(b.booked, 0) || ' / ' || cap.avail || ' min' END AS cap_note FROM cap LEFT JOIN b ON b.day = cap.day ORDER BY cap.pos"},
  "appts": {"sql": "SELECT 'c' || (starts_at::date - current_date + 1) AS cell, to_char(starts_at, 'HH24:MI') AS start_time, customer_id AS customer, assignee, upper(left(split_part(assignee, ' ', 1), 1) || left(split_part(assignee, ' ', 2), 1)) AS initials, job_type, status, CASE status WHEN 'done' THEN 'ok' WHEN 'confirmed' THEN 'ok' WHEN 'cancelled' THEN 'bad' WHEN 'no_show' THEN 'bad' WHEN 'in_progress' THEN 'warn' ELSE '' END AS tone FROM scheduling.v_appointments WHERE starts_at >= current_date AND starts_at < current_date + 7 ORDER BY starts_at"}
}
$q$::jsonb,
    '{}'::jsonb,
    '[]'::jsonb,
    'scheduling',
    'Week calendar: capacity bars per day, appointment chips in day columns'
);

SELECT rvbbit.upsert_plate(
    'scheduling/today',
    'Scheduling — Today',
    $tpl$
<div class="plate-section">
  <div class="plate-banner">
    <div class="plate-banner-big">Today&#8217;s Board</div>
    <div class="plate-banner-note">Active crew and today&#8217;s appointments</div>
  </div>
  <div class="plate-toolbar">
    <button type="button" rv-open="plate:scheduling/week">Next 7 days</button>
    <button type="button" rv-open="plate:scheduling/intake">Book appointment</button>
  </div>
</div>
<div class="plate-columns">
  <section rv-group="board:assignee" class="plate-section">
    <h3>{{ group.key }}</h3>
    <div rv-each="group">
      <div rv-if="row.appt_id" class="plate-card {{ row.tone }}">
        <div class="plate-card-title">{{ row.time_range }} &#183; {{ row.customer }}</div>
        <div class="plate-card-value">{{ row.job_type }}</div>
        <div class="plate-card-note">{{ row.status }}</div>
        <div class="plate-toolbar">
          <form rv-action="mark_done"><button type="submit" name="appt_id" value="{{ row.appt_id }}">Done</button></form>
          <form rv-action="cancel_appointment"><button type="submit" name="appt_id" value="{{ row.appt_id }}">Cancel</button></form>
        </div>
      </div>
      <div rv-if="!row.appt_id" class="plate-empty">no visits today</div>
    </div>
  </section>
</div>
$tpl$,
    $q$
{
  "board": {"sql": "SELECT a.name AS assignee, ap.appt_id, to_char(ap.starts_at, 'HH24:MI') || '–' || to_char(ap.ends_at, 'HH24:MI') AS time_range, ap.customer_id AS customer, ap.job_type, ap.status, CASE ap.status WHEN 'done' THEN 'ok' WHEN 'confirmed' THEN 'ok' WHEN 'cancelled' THEN 'bad' WHEN 'no_show' THEN 'bad' WHEN 'in_progress' THEN 'warn' ELSE '' END AS tone FROM scheduling.assignees a LEFT JOIN scheduling.v_appointments ap ON ap.assignee = a.name AND ap.starts_at >= current_date AND ap.starts_at < current_date + 1 WHERE a.active ORDER BY a.name, ap.starts_at"}
}
$q$::jsonb,
    $a$
{
  "mark_done": {
    "sql": "UPDATE scheduling.appointments SET status = 'done' WHERE appt_id = {{appt_id}}",
    "args": [{"name": "appt_id", "type": "text", "required": true}],
    "confirm": false,
    "description": "Mark this appointment done"
  },
  "cancel_appointment": {
    "sql": "UPDATE scheduling.appointments SET status = 'cancelled' WHERE appt_id = {{appt_id}}",
    "args": [{"name": "appt_id", "type": "text", "required": true}],
    "confirm": true,
    "description": "Cancel this appointment"
  }
}
$a$::jsonb,
    '[]'::jsonb,
    'scheduling',
    'Today board: one column per active crew member, grouped from rows'
);

UPDATE rvbbit.plates SET module = 'operations'
WHERE plate_id IN ('scheduling/today', 'scheduling/week');

COMMIT;
