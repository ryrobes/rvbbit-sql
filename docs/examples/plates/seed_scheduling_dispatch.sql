-- scheduling/dispatch — the rv-board reference plate (tier 3, island 1).
-- One island, two semantics:
--   board by ASSIGNEE: drag a card to another tech -> reassign {id, to}
--   board by DAY:      drag a card to another day  -> reschedule {id, to}
-- Empty columns come from LEFT JOINs (rows with NULL appt_id are
-- placeholders), so idle techs and open days are still drop targets.

BEGIN;

SELECT rvbbit.upsert_plate(
    'scheduling/dispatch',
    'Scheduling — Dispatch',
    $tpl$
<div class="plate-section">
  <div class="plate-banner">
    <div class="plate-banner-big">Dispatch</div>
    <div class="plate-banner-note">Drag between columns: crew board reassigns, week board reschedules</div>
  </div>
  <div class="plate-toolbar">
    <button type="button" rv-open="plate:scheduling/today">Today</button>
    <button type="button" rv-open="plate:scheduling/week">Next 7 days</button>
    <button type="button" rv-open="plate:scheduling/intake">Book appointment</button>
  </div>
</div>
<div class="plate-section">
  <h3>Today&#8217;s crew &#8212; drag to reassign</h3>
  <rv-board query="crew_board" group-by="assignee" id="appt_id"
            title="time_range" value="customer" note="job_note" tone="tone"
            action="reassign"></rv-board>
</div>
<div class="plate-section">
  <h3>This week &#8212; drag to another day to reschedule</h3>
  <rv-board query="day_board" group-by="day" group-label="day_label" id="appt_id"
            title="start_time" value="customer" note="job_note" tone="tone"
            action="reschedule"></rv-board>
</div>
$tpl$,
    $q$
{
  "crew_board": {"sql": "SELECT a.name AS assignee, ap.appt_id, to_char(ap.starts_at, 'HH24:MI') || '–' || to_char(ap.ends_at, 'HH24:MI') AS time_range, ap.customer_id AS customer, ap.job_type || ' · ' || ap.status AS job_note, CASE ap.status WHEN 'done' THEN 'ok' WHEN 'confirmed' THEN 'ok' WHEN 'in_progress' THEN 'warn' ELSE '' END AS tone FROM scheduling.assignees a LEFT JOIN scheduling.v_appointments ap ON ap.assignee = a.name AND ap.starts_at >= current_date AND ap.starts_at < current_date + 1 AND ap.status NOT IN ('cancelled', 'no_show') WHERE a.active ORDER BY a.name, ap.starts_at"},
  "day_board": {"sql": "SELECT to_char(d.day, 'YYYY-MM-DD') AS day, to_char(d.day, 'Dy FMDD Mon') AS day_label, ap.appt_id, to_char(ap.starts_at, 'HH24:MI') AS start_time, ap.customer_id AS customer, ap.job_type || ' · ' || ap.assignee AS job_note, CASE ap.status WHEN 'done' THEN 'ok' WHEN 'confirmed' THEN 'ok' WHEN 'in_progress' THEN 'warn' ELSE '' END AS tone FROM (SELECT current_date + o AS day FROM generate_series(0, 6) o) d LEFT JOIN scheduling.v_appointments ap ON ap.starts_at::date = d.day AND ap.status IN ('booked', 'confirmed') ORDER BY d.day, ap.starts_at"}
}
$q$::jsonb,
    $a$
{
  "reassign": {
    "sql": "UPDATE scheduling.appointments SET assignee = {{to}} WHERE appt_id = {{id}}",
    "args": [{"name": "id", "type": "text", "required": true}, {"name": "to", "type": "text", "required": true}],
    "confirm": false,
    "description": "Reassign this appointment to another crew member"
  },
  "reschedule": {
    "sql": "UPDATE scheduling.appointments SET starts_at = nullif({{to}},'')::date + starts_at::time, ends_at = nullif({{to}},'')::date + ends_at::time WHERE appt_id = {{id}}",
    "args": [{"name": "id", "type": "text", "required": true}, {"name": "to", "type": "text", "required": true}],
    "confirm": false,
    "description": "Move this appointment to another day (same time)"
  }
}
$a$::jsonb,
    '[]'::jsonb,
    'scheduling',
    'Drag-and-drop dispatch: reassign across crew, reschedule across days'
);

UPDATE rvbbit.plates SET module = 'operations' WHERE plate_id = 'scheduling/dispatch';

COMMIT;
