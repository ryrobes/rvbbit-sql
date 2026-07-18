-- scheduling/edit — closes the dispatch loop: double-click a card on the
-- Dispatch boards -> the board emits appt_id to the desktop bus and
-- opens this plate; the from_bus param loads the appointment into the
-- same form shape as intake.
-- Idioms this plate is the reference for:
--   * from_bus param as an editor's record selector
--   * select PREFILL: form selects are query-driven (query/value/label
--     attrs) and the options query computes a boolean `selected` column
--     — NEVER interpolate the selected attribute itself (sanitize turns
--     selected="" into bare selected = on)
--   * NO nested rv-each: sibling rv-each blocks over the single-row
--     "appt" query per field group; option loops stand alone

BEGIN;

SELECT rvbbit.upsert_plate(
    'scheduling/edit',
    'Scheduling — Edit Appointment',
    $tpl$
<div class="plate-section">
  <div class="plate-banner">
    <div class="plate-banner-big">Edit Appointment</div>
    <div class="plate-banner-note">Double-click a card on Dispatch to load one</div>
  </div>
  <div class="plate-toolbar">
    <button type="button" rv-open="plate:scheduling/dispatch">Dispatch</button>
    <button type="button" rv-open="plate:scheduling/intake">New booking</button>
  </div>
</div>
<div rv-if="!appt.appt_id" class="plate-empty">nothing loaded &#8212; double-click an appointment on the Dispatch board</div>
<div rv-if="appt.appt_id" class="plate-section">
  <form rv-action="update_appointment" class="plate-form">
    <label rv-each="appt" class="plate-field">Customer<input name="customer_name" type="text" value="{{ row.customer_id }}" required /><input type="hidden" name="appt_id" value="{{ row.appt_id }}" /></label>
    <label class="plate-field">Job type<select name="job_type" required query="job_type_opts" value="v" label="l"></select></label>
    <label class="plate-field">Assignee<select name="assignee" required query="assignee_opts" value="v"></select></label>
    <label class="plate-field">Status<select name="status" required query="status_opts" value="v"></select></label>
    <div rv-each="appt" class="plate-field-inline">
      <label class="plate-field">Date<input name="d" type="date" value="{{ row.d }}" required /></label>
      <label class="plate-field">Time<input name="t" type="time" value="{{ row.t }}" required /></label>
    </div>
    <label rv-each="appt" class="plate-field">Address<input name="address" type="text" value="{{ row.address }}" /></label>
    <label rv-each="appt" class="plate-field">Notes<input name="notes" type="text" value="{{ row.notes }}" /></label>
    <button type="submit">Save changes</button>
  </form>
</div>
$tpl$,
    $q$
{
  "appt": {"sql": "SELECT appt_id, customer_id, to_char(starts_at, 'YYYY-MM-DD') AS d, to_char(starts_at, 'HH24:MI') AS t, coalesce(address, '') AS address, coalesce(notes, '') AS notes FROM scheduling.v_appointments WHERE appt_id = nullif({{ params.appt_id }}, '')"},
  "job_type_opts": {"sql": "SELECT j.name AS v, j.name || ' · ' || j.default_minutes || ' min' AS l, (j.name = a.job_type) AS selected FROM scheduling.job_types j LEFT JOIN scheduling.v_appointments a ON a.appt_id = nullif({{ params.appt_id }}, '') ORDER BY j.name"},
  "assignee_opts": {"sql": "SELECT s.name AS v, (s.name = a.assignee) AS selected FROM scheduling.assignees s LEFT JOIN scheduling.v_appointments a ON a.appt_id = nullif({{ params.appt_id }}, '') WHERE s.active OR s.name = a.assignee ORDER BY s.name"},
  "status_opts": {"sql": "SELECT u.s AS v, (u.s = a.status) AS selected FROM unnest(ARRAY['booked','confirmed','in_progress','done','cancelled','no_show']) WITH ORDINALITY AS u(s, ord) LEFT JOIN scheduling.v_appointments a ON a.appt_id = nullif({{ params.appt_id }}, '') ORDER BY u.ord"}
}
$q$::jsonb,
    $a$
{
  "update_appointment": {
    "sql": "UPDATE scheduling.appointments SET customer_id = {{customer_name}}, job_type = {{job_type}}, assignee = {{assignee}}, status = {{status}}, starts_at = nullif({{d}},'')::date + nullif({{t}},'')::time, ends_at = nullif({{d}},'')::date + nullif({{t}},'')::time + make_interval(mins => (SELECT default_minutes FROM scheduling.job_types WHERE name = {{job_type}})), address = nullif({{address}},''), notes = nullif({{notes}},'') WHERE appt_id = {{appt_id}}",
    "args": [
      {"name": "appt_id", "type": "text", "required": true},
      {"name": "customer_name", "type": "text", "required": true},
      {"name": "job_type", "type": "text", "required": true},
      {"name": "assignee", "type": "text", "required": true},
      {"name": "status", "type": "text", "required": true},
      {"name": "d", "type": "text", "required": true},
      {"name": "t", "type": "text", "required": true},
      {"name": "address", "type": "text", "required": false},
      {"name": "notes", "type": "text", "required": false}
    ],
    "confirm": false,
    "description": "Save changes to this appointment (duration re-derived from the job type)"
  }
}
$a$::jsonb,
    '[{"name": "appt_id", "type": "text", "default": "", "from_bus": true}]'::jsonb,
    'scheduling',
    'Edit one appointment; loaded by double-clicking a Dispatch card'
);

UPDATE rvbbit.plates SET module = 'operations' WHERE plate_id = 'scheduling/edit';

COMMIT;
