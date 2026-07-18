-- Report Finder: the control-primitives demo. Every basic input is a param
-- emitter riding the same rv-emit verb — search box, query-driven dropdown,
-- static dropdown, slider, datepicker, checkbox — and every piece of
-- selection state is a column the SQL computes from its own params.
SELECT rvbbit.upsert_plate(
  'demo/report-finder',
  'Report Finder',
  $tpl$
<div class="plate-toolbar">
  <label class="plate-field">search titles
    <input type="search" rv-emit="q" value="{{ params.q }}" placeholder="wood knocks, howls…">
  </label>
  <label class="plate-field">state
    <select rv-emit="state" query="state_opts" value="state" label="label" placeholder="All states"></select>
  </label>
  <label class="plate-field">season
    <select rv-emit="season">
      <option value="">All seasons</option>
      <option>Summer</option>
      <option>Fall</option>
      <option>Winter</option>
      <option>Spring</option>
    </select>
  </label>
  <label class="plate-field">year &#8805; {{ params.min_year }}
    <input type="range" rv-emit="min_year" min="1900" max="2025" step="5" value="{{ params.min_year }}">
  </label>
  <label class="plate-field">submitted since
    <input type="date" rv-emit="since" value="{{ params.since }}">
  </label>
  <label class="plate-field plate-field-inline">
    <input type="checkbox" rv-emit="class_a" rv-value="Class A"> Class A only
  </label>
</div>

<div class="plate-cards">
  <div rv-each="summary" class="plate-card ok">
    <div class="plate-card-title">matching reports</div>
    <div class="plate-card-value">{{ row.n }}</div>
    <div class="plate-card-note">{{ row.scope }}</div>
  </div>
</div>

<table class="plate-table">
  <thead><tr><th>year</th><th>state</th><th>county</th><th>season</th><th>title</th></tr></thead>
  <tbody>
    <tr rv-each="results">
      <td>{{ row.year }}</td>
      <td>{{ row.state }}</td>
      <td>{{ row.county }}</td>
      <td>{{ row.season }}</td>
      <td>{{ row.title }}</td>
    </tr>
  </tbody>
</table>
$tpl$,
  jsonb_build_object(
    'state_opts', jsonb_build_object('sql', $q$
SELECT state, state || ' (' || count(*) || ')' AS label
FROM public.bigfoot_sightings_locations WHERE state IS NOT NULL
GROUP BY state ORDER BY count(*) DESC
    $q$),
    'summary', jsonb_build_object('sql', $q$
SELECT count(*)::int AS n,
       trim(both ' · ' FROM
         coalesce(nullif({{ params.state }}, ''), 'all states')
         || ' · ' || coalesce(nullif({{ params.season }}, ''), 'all seasons')
         || CASE WHEN {{ params.min_year }} > 1900 THEN ' · ' || {{ params.min_year }} || '+' ELSE '' END
         || CASE WHEN nullif({{ params.since }}, '') IS NOT NULL THEN ' · since ' || {{ params.since }} ELSE '' END
         || CASE WHEN nullif({{ params.class_a }}, '') IS NOT NULL THEN ' · class A' ELSE '' END
       ) AS scope
FROM public.bigfoot_sightings_locations
WHERE title IS NOT NULL
  AND (nullif({{ params.q }}, '') IS NULL OR title ILIKE '%' || {{ params.q }} || '%')
  AND (nullif({{ params.state }}, '') IS NULL OR state = {{ params.state }})
  AND (nullif({{ params.season }}, '') IS NULL OR season = {{ params.season }})
  AND ({{ params.min_year }} <= 1900 OR substring(fixed_year from '\d{4}')::int >= {{ params.min_year }})
  AND (nullif({{ params.since }}, '') IS NULL OR submitted_date >= nullif({{ params.since }}, '')::date)
  AND (nullif({{ params.class_a }}, '') IS NULL OR class = {{ params.class_a }})
    $q$),
    'results', jsonb_build_object('sql', $q$
SELECT coalesce(substring(fixed_year from '\d{4}'), '—') AS year, state, county, season, left(title, 80) AS title
FROM public.bigfoot_sightings_locations
WHERE title IS NOT NULL
  AND (nullif({{ params.q }}, '') IS NULL OR title ILIKE '%' || {{ params.q }} || '%')
  AND (nullif({{ params.state }}, '') IS NULL OR state = {{ params.state }})
  AND (nullif({{ params.season }}, '') IS NULL OR season = {{ params.season }})
  AND ({{ params.min_year }} <= 1900 OR substring(fixed_year from '\d{4}')::int >= {{ params.min_year }})
  AND (nullif({{ params.since }}, '') IS NULL OR submitted_date >= nullif({{ params.since }}, '')::date)
  AND (nullif({{ params.class_a }}, '') IS NULL OR class = {{ params.class_a }})
ORDER BY submitted_date DESC NULLS LAST LIMIT 40
    $q$)
  ),
  '{}'::jsonb,
  '[{"name": "q", "default": ""},
    {"name": "state", "default": "", "from_bus": true},
    {"name": "season", "default": "", "from_bus": true},
    {"name": "min_year", "default": 1900},
    {"name": "since", "default": ""},
    {"name": "class_a", "default": ""}]'::jsonb,
  NULL,
  'Control primitives: search, query-driven + static dropdowns, slider, datepicker, checkbox — all rv-emit param emitters'
);
SELECT 'report finder seeded';
