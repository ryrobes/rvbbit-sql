-- The first switchboard: kit 'field-kit'. Its 'insights' module is gated by
-- a contract (needs >= 3 field notes). The switchboard shows contract state
-- AND carries the intake form that satisfies it — fixing the gate happens
-- ON the switchboard.

SELECT rvbbit.upsert_kit_contract(
  'field-kit', 'insights', 'has_enough_notes',
  $c$SELECT 'only ' || count(*) || ' field note(s) logged — need at least 3' AS violation
     FROM demo_kit.field_notes HAVING count(*) < 3$c$,
  'Insights need at least 3 field notes to be meaningful'
);

SELECT rvbbit.upsert_plate(
  'field-kit/switchboard',
  'Field Kit — Switchboard',
  $tpl$
<div class="plate-section">
  <h2>Field Kit</h2>
  <p>Modules unlock when their contracts go green. Fix them right here.</p>
  <div class="plate-cards">
    <div rv-each="contracts" class="plate-card {{ row.tone }}">
      <div class="plate-card-title">{{ row.module }} · {{ row.contract_id }}</div>
      <div class="plate-card-value">{{ row.state }}</div>
      <div class="plate-card-note">{{ row.detail }}</div>
    </div>
  </div>
</div>
<div class="plate-section">
  <h3>Intake — log field notes <span rv-each="stats">({{ row.n }} so far, need 3)</span></h3>
  <form rv-action="add_note" class="plate-form">
    <label>author<input name="author" type="text" placeholder="who saw it" required /></label>
    <label>note<input name="note" type="text" placeholder="what happened" required /></label>
    <button type="submit">Add note</button>
  </form>
  <table class="plate-table">
    <thead><tr><th>when</th><th>author</th><th>note</th></tr></thead>
    <tbody>
      <tr rv-each="notes">
        <td>{{ row.at }}</td><td><b>{{ row.author }}</b></td><td>{{ row.note }}</td>
      </tr>
    </tbody>
  </table>
</div>
$tpl$,
  jsonb_build_object(
    'contracts', jsonb_build_object('sql', $q$
      SELECT module, contract_id,
             CASE WHEN ok THEN 'GREEN' ELSE 'RED' END AS state,
             CASE WHEN ok THEN 'ok' ELSE 'bad' END AS tone,
             CASE WHEN ok THEN coalesce(description, 'satisfied') ELSE coalesce(sample, description, '') END AS detail
      FROM rvbbit.kit_contract_status('field-kit')
      ORDER BY module, contract_id
    $q$),
    'stats', jsonb_build_object('sql', 'SELECT count(*)::int AS n FROM demo_kit.field_notes'),
    'notes', jsonb_build_object('sql', $q$SELECT to_char(created_at, 'HH24:MI:SS') AS at, author, note FROM demo_kit.field_notes ORDER BY id DESC LIMIT 10$q$)
  ),
  jsonb_build_object(
    'add_note', jsonb_build_object(
      'sql', 'INSERT INTO demo_kit.field_notes (author, note) VALUES ({{author}}, {{note}})',
      'args', jsonb_build_array(
        jsonb_build_object('name', 'author', 'type', 'text', 'required', true),
        jsonb_build_object('name', 'note', 'type', 'text', 'required', true)
      ),
      'description', 'Insert a field note'
    )
  ),
  '[]'::jsonb,
  'field-kit', 'The kit''s gated entry: contract status + the intake that satisfies it'
);

-- The gated child: only renders when the insights module is green.
SELECT rvbbit.upsert_plate(
  'field-kit/insights',
  'Field Kit — Insights',
  $tpl$
<div class="plate-section">
  <h2>Field note insights</h2>
  <div class="plate-cards">
    <rv-metric query="totals" value="n" title="notes logged"></rv-metric>
    <rv-metric query="authors" value="n" title="distinct authors"></rv-metric>
  </div>
</div>
<div class="plate-section">
  <h3>Notes by author</h3>
  <rv-chart query="by_author" x="author" y="n" mark="bar"></rv-chart>
</div>
$tpl$,
  jsonb_build_object(
    'totals', jsonb_build_object('sql', 'SELECT count(*)::int AS n FROM demo_kit.field_notes'),
    'authors', jsonb_build_object('sql', 'SELECT count(DISTINCT author)::int AS n FROM demo_kit.field_notes'),
    'by_author', jsonb_build_object('sql', 'SELECT author, count(*)::int AS n FROM demo_kit.field_notes GROUP BY author ORDER BY n DESC')
  ),
  '{}'::jsonb, '[]'::jsonb,
  'field-kit', 'Gated by the insights module contract — unlocks at 3 notes'
);

UPDATE rvbbit.plates SET module = 'insights' WHERE plate_id = 'field-kit/insights';
SELECT plate_id, kit, module FROM rvbbit.plates ORDER BY plate_id;
