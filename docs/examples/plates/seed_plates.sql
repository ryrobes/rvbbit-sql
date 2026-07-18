-- Sample plates: three template styles proving the primitives compose.

-- ── 1. Health-style: status cards + heavy-table list + build-not-run remedies ──
SELECT rvbbit.upsert_plate(
  'demo/health-mini',
  'Health — Mini',
  $tpl$
<div class="plate-section">
  <h2>Engine vitals</h2>
  <div class="plate-cards">
    <div rv-each="status" class="plate-card {{ row.tone }}">
      <div class="plate-card-title">{{ row.name }}</div>
      <div class="plate-card-value">{{ row.value }}</div>
      <div class="plate-card-note">{{ row.note }}</div>
    </div>
  </div>
</div>
<div class="plate-section">
  <h3>Heaviest rvbbit relations</h3>
  <table class="plate-table">
    <thead><tr><th>relation</th><th>size</th><th></th></tr></thead>
    <tbody>
      <tr rv-each="heavy">
        <td><code>{{ row.rel }}</code></td>
        <td>{{ row.size_pretty }}</td>
        <td><button type="button" rv-open-sql="{{ row.remedy_sql }}" rv-open-sql-title="Vacuum {{ row.rel }}">Review VACUUM SQL</button></td>
      </tr>
    </tbody>
  </table>
  <p>Remedies open as SQL windows — built, never run for you.</p>
</div>
$tpl$,
  jsonb_build_object(
    'status', jsonb_build_object('sql', $q$
      SELECT * FROM (
        SELECT 1 AS ord, 'tombstones' AS name, count(*)::text AS value,
               CASE WHEN count(*) < 100000 THEN 'ok' WHEN count(*) < 1000000 THEN 'warn' ELSE 'bad' END AS tone,
               'rows in delete_log' AS note
        FROM rvbbit.delete_log
        UNION ALL
        SELECT 2, 'orphaned files', count(*)::text,
               CASE WHEN count(*) = 0 THEN 'ok' WHEN count(*) < 500 THEN 'warn' ELSE 'bad' END,
               'awaiting the grace reaper'
        FROM rvbbit.orphaned_files
        UNION ALL
        SELECT 3, 'semantic calls · 24h', count(*)::text, 'ok', 'receipts written'
        FROM rvbbit.receipts WHERE invocation_at > now() - interval '24 hours'
        UNION ALL
        SELECT 4, 'operators', count(*)::text, 'ok', 'installed semantic operators'
        FROM rvbbit.operators
      ) s ORDER BY ord
    $q$),
    'heavy', jsonb_build_object('sql', $q$
      SELECT c.relname AS rel,
             pg_size_pretty(pg_total_relation_size(c.oid)) AS size_pretty,
             'VACUUM (VERBOSE, ANALYZE) rvbbit.' || quote_ident(c.relname) || ';' AS remedy_sql
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'rvbbit' AND c.relkind = 'r'
      ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 5
    $q$)
  ),
  '{}'::jsonb, '[]'::jsonb,
  NULL, 'Health-system style: status cards, heavy relations, build-not-run remedies'
);

-- ── 2. Dashboard-esque: metric + chart + grid islands + emit chips ──
SELECT rvbbit.upsert_plate(
  'demo/bigfoot-dashboard',
  'Bigfoot — Field Dashboard',
  $tpl$
<div rv-each="scope" class="plate-toolbar">
  <button class="{{ row.all_sel }}" type="button" rv-emit="state" rv-value="">All states</button>
  <button class="{{ row.season_sel }}" type="button" rv-emit="season" rv-value="">All seasons</button>
  <span class="plate-banner-note">scope: {{ row.scope }}</span>
</div>
<div class="plate-section">
  <div class="plate-cards">
    <rv-metric query="total" value="n" title="total sightings"></rv-metric>
    <rv-metric query="states" value="n" title="states reporting"></rv-metric>
    <rv-metric query="peak" value="season" title="peak season"></rv-metric>
  </div>
</div>
<div class="plate-section">
  <h3>Sightings by season — click a bar to scope, click again to clear</h3>
  <rv-chart query="by_season" x="season" y="n" mark="bar" rv-emit="season"></rv-chart>
</div>
<div class="plate-section">
  <h3>Top states <span rv-each="top_states"><button class="{{ row.sel }}" type="button" rv-emit="state" rv-value="{{ row.state }}">{{ row.state }} ({{ row.n }})</button></span></h3>
  <rv-grid query="recent"></rv-grid>
</div>
$tpl$,
  jsonb_build_object(
    'scope', jsonb_build_object('sql', $q$SELECT coalesce(nullif({{ params.state }}, ''), 'all states') || ' · ' || coalesce(nullif({{ params.season }}, ''), 'all seasons') AS scope, CASE WHEN nullif({{ params.state }}, '') IS NULL THEN 'active' ELSE '' END AS all_sel, CASE WHEN nullif({{ params.season }}, '') IS NULL THEN 'active' ELSE '' END AS season_sel$q$),
    'total', jsonb_build_object('sql', $q$SELECT count(*)::int AS n FROM public.bigfoot_sightings_locations WHERE (nullif({{ params.state }}, '') IS NULL OR state = {{ params.state }}) AND (nullif({{ params.season }}, '') IS NULL OR season = {{ params.season }})$q$),
    'states', jsonb_build_object('sql', $q$SELECT count(DISTINCT state)::int AS n FROM public.bigfoot_sightings_locations WHERE state IS NOT NULL AND (nullif({{ params.state }}, '') IS NULL OR state = {{ params.state }}) AND (nullif({{ params.season }}, '') IS NULL OR season = {{ params.season }})$q$),
    'peak', jsonb_build_object('sql', $q$SELECT season FROM public.bigfoot_sightings_locations WHERE season IS NOT NULL AND season <> 'Unknown' AND (nullif({{ params.state }}, '') IS NULL OR state = {{ params.state }}) GROUP BY season ORDER BY count(*) DESC LIMIT 1$q$),
    'by_season', jsonb_build_object('sql', $q$SELECT season, count(*)::int AS n, CASE WHEN season = {{ params.season }} THEN 'active' ELSE '' END AS sel FROM public.bigfoot_sightings_locations WHERE season IS NOT NULL AND season <> 'Unknown' AND (nullif({{ params.state }}, '') IS NULL OR state = {{ params.state }}) GROUP BY season ORDER BY n DESC$q$),
    'top_states', jsonb_build_object('sql', $q$SELECT state, count(*)::int AS n, CASE WHEN state = {{ params.state }} THEN 'active' ELSE '' END AS sel FROM public.bigfoot_sightings_locations WHERE state IS NOT NULL AND (nullif({{ params.season }}, '') IS NULL OR season = {{ params.season }}) GROUP BY state ORDER BY n DESC LIMIT 6$q$),
    'recent', jsonb_build_object('sql', $q$SELECT bfroid, title, state, county, season, fixed_year AS year FROM public.bigfoot_sightings_locations WHERE title IS NOT NULL AND (nullif({{ params.state }}, '') IS NULL OR state = {{ params.state }}) AND (nullif({{ params.season }}, '') IS NULL OR season = {{ params.season }}) ORDER BY submitted_date DESC NULLS LAST LIMIT 50$q$)
  ),
  '{}'::jsonb, '[{"name": "state", "default": "", "from_bus": true}, {"name": "season", "default": "", "from_bus": true}]'::jsonb,
  NULL, 'Dashboard style: metric/chart/grid islands + param-emitting chips; state is from_bus — drive it from any plate or window'
);

-- ── 3. Form-esque: kit-owned table + actions with confirm ──
CREATE SCHEMA IF NOT EXISTS demo_kit;
CREATE TABLE IF NOT EXISTS demo_kit.field_notes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    author text NOT NULL,
    note text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

SELECT rvbbit.upsert_plate(
  'demo/field-notes',
  'Field Notes — Intake',
  $tpl$
<div class="plate-section">
  <h2>Log a field note</h2>
  <form rv-action="add_note" class="plate-form">
    <label>author<input name="author" type="text" placeholder="who saw it" required /></label>
    <label>note<input name="note" type="text" placeholder="what happened" required /></label>
    <button type="submit">Add note</button>
  </form>
</div>
<div class="plate-section">
  <h3>Recent notes <span rv-each="stats">({{ row.n }} total)</span></h3>
  <table class="plate-table">
    <thead><tr><th>when</th><th>author</th><th>note</th></tr></thead>
    <tbody>
      <tr rv-each="notes">
        <td>{{ row.at }}</td>
        <td><b>{{ row.author }}</b></td>
        <td>{{ row.note }}</td>
      </tr>
    </tbody>
  </table>
</div>
<div class="plate-section">
  <form rv-action="clear_notes"><button type="submit">Clear all notes</button></form>
</div>
$tpl$,
  jsonb_build_object(
    'notes', jsonb_build_object('sql', $q$SELECT to_char(created_at, 'HH24:MI:SS') AS at, author, note FROM demo_kit.field_notes ORDER BY id DESC LIMIT 20$q$),
    'stats', jsonb_build_object('sql', 'SELECT count(*)::int AS n FROM demo_kit.field_notes')
  ),
  jsonb_build_object(
    'add_note', jsonb_build_object(
      'sql', 'INSERT INTO demo_kit.field_notes (author, note) VALUES ({{author}}, {{note}})',
      'args', jsonb_build_array(
        jsonb_build_object('name', 'author', 'type', 'text', 'required', true),
        jsonb_build_object('name', 'note', 'type', 'text', 'required', true)
      ),
      'description', 'Insert a field note'
    ),
    'clear_notes', jsonb_build_object(
      'sql', 'DELETE FROM demo_kit.field_notes',
      'args', '[]'::jsonb,
      'confirm', true,
      'description', 'Delete ALL field notes — sure?'
    )
  ),
  '[]'::jsonb,
  'demo', 'Form style: kit-owned table, validated actions, confirm-gated destructive action'
);

SELECT plate_id, title FROM rvbbit.plates ORDER BY plate_id;
