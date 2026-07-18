-- Casebook: tabs + pagination + radio group + live search + rv-open.
-- Tabs are a param whose sections are query-driven rv-ifs; page math is
-- computed by SQL (prev/next/pageno are columns); the radio group and the
-- live search ride the same rv-emit verb as everything else.
SELECT rvbbit.upsert_plate(
  'demo/casebook',
  'Bigfoot Casebook',
  $tpl$
<div rv-each="tabs" class="plate-tabs">
  <button type="button" class="{{ row.browse_sel }}" rv-emit="tab" rv-value="browse">Browse</button>
  <button type="button" class="{{ row.stats_sel }}" rv-emit="tab" rv-value="stats">Stats</button>
</div>

<div rv-if="tabs.show_browse">
  <div class="plate-toolbar">
    <label class="plate-field">search as you type
      <input type="search" rv-emit="q" rv-live value="{{ params.q }}" placeholder="howls, tracks, knocks…">
    </label>
    <label class="plate-field plate-field-inline"><input type="radio" rv-emit="class" value=""> All</label>
    <label class="plate-field plate-field-inline"><input type="radio" rv-emit="class" value="Class A"> Class A</label>
    <label class="plate-field plate-field-inline"><input type="radio" rv-emit="class" value="Class B"> Class B</label>
  </div>
  <table class="plate-table">
    <thead><tr><th>state</th><th>county</th><th>class</th><th>title</th></tr></thead>
    <tbody>
      <tr rv-each="reports">
        <td>{{ row.state }}</td>
        <td>{{ row.county }}</td>
        <td>{{ row.class }}</td>
        <td>{{ row.title }}</td>
      </tr>
    </tbody>
  </table>
  <div rv-each="pager" class="plate-pager">
    <button type="button" rv-if="row.has_prev" rv-emit="page" rv-value="{{ row.prev }}">&#8592; Prev</button>
    <span>page {{ row.pageno }} of {{ row.pages }} &#183; {{ row.total }} reports</span>
    <button type="button" rv-if="row.has_next" rv-emit="page" rv-value="{{ row.next }}">Next &#8594;</button>
  </div>
  <p><button type="button" rv-open="plate:demo/report-finder" rv-open-title="Report Finder">Open the full Report Finder &#8594;</button></p>
</div>

<div rv-if="tabs.show_stats">
  <div class="plate-cards">
    <rv-metric query="stat_total" value="n" title="reports in scope"></rv-metric>
    <rv-metric query="stat_states" value="n" title="states"></rv-metric>
  </div>
  <h3>Reports by decade</h3>
  <rv-chart query="by_decade" x="decade" y="n" mark="bar"></rv-chart>
</div>
$tpl$,
  jsonb_build_object(
    'tabs', jsonb_build_object('sql', $q$
SELECT CASE WHEN {{ params.tab }} = 'browse' THEN 'active' ELSE '' END AS browse_sel,
       CASE WHEN {{ params.tab }} = 'stats' THEN 'active' ELSE '' END AS stats_sel,
       {{ params.tab }} = 'browse' AS show_browse,
       {{ params.tab }} = 'stats' AS show_stats
    $q$),
    'reports', jsonb_build_object('sql', $q$
SELECT state, county, class, left(title, 70) AS title
FROM public.bigfoot_sightings_locations
WHERE title IS NOT NULL
  AND (nullif({{ params.q }}, '') IS NULL OR title ILIKE '%' || {{ params.q }} || '%')
  AND (nullif({{ params.class }}, '') IS NULL OR class = {{ params.class }})
ORDER BY submitted_date DESC NULLS LAST
LIMIT 12 OFFSET {{ params.page }} * 12
    $q$),
    'pager', jsonb_build_object('sql', $q$
WITH t AS (
  SELECT count(*)::int AS total FROM public.bigfoot_sightings_locations
  WHERE title IS NOT NULL
    AND (nullif({{ params.q }}, '') IS NULL OR title ILIKE '%' || {{ params.q }} || '%')
    AND (nullif({{ params.class }}, '') IS NULL OR class = {{ params.class }})
)
SELECT total,
       greatest(ceil(total / 12.0), 1)::int AS pages,
       {{ params.page }} + 1 AS pageno,
       greatest({{ params.page }} - 1, 0) AS prev,
       {{ params.page }} + 1 AS next,
       {{ params.page }} > 0 AS has_prev,
       {{ params.page }} + 1 < ceil(total / 12.0) AS has_next
FROM t
    $q$),
    'stat_total', jsonb_build_object('sql', $q$
SELECT count(*)::int AS n FROM public.bigfoot_sightings_locations WHERE title IS NOT NULL
  AND (nullif({{ params.q }}, '') IS NULL OR title ILIKE '%' || {{ params.q }} || '%')
  AND (nullif({{ params.class }}, '') IS NULL OR class = {{ params.class }})
    $q$),
    'stat_states', jsonb_build_object('sql', $q$
SELECT count(DISTINCT state)::int AS n FROM public.bigfoot_sightings_locations WHERE title IS NOT NULL
  AND (nullif({{ params.q }}, '') IS NULL OR title ILIKE '%' || {{ params.q }} || '%')
  AND (nullif({{ params.class }}, '') IS NULL OR class = {{ params.class }})
    $q$),
    'by_decade', jsonb_build_object('sql', $q$
SELECT (substring(fixed_year from '\d{4}')::int / 10 * 10)::text || 's' AS decade, count(*)::int AS n
FROM public.bigfoot_sightings_locations
WHERE substring(fixed_year from '\d{4}') IS NOT NULL
  AND substring(fixed_year from '\d{4}')::int BETWEEN 1900 AND 2030
  AND (nullif({{ params.q }}, '') IS NULL OR title ILIKE '%' || {{ params.q }} || '%')
  AND (nullif({{ params.class }}, '') IS NULL OR class = {{ params.class }})
GROUP BY 1 ORDER BY 1
    $q$)
  ),
  '{}'::jsonb,
  '[{"name": "tab", "default": "browse"},
    {"name": "page", "default": 0, "type": "number"},
    {"name": "q", "default": ""},
    {"name": "class", "default": ""}]'::jsonb,
  NULL,
  'Tabs (param + query-driven rv-if) + SQL-computed pagination + radio group + live search + rv-open navigation'
);
SELECT 'casebook seeded';
