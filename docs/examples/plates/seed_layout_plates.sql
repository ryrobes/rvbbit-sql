-- Bespoke-arrangement demos: the layout vocabulary (plate-split / rail / kv /
-- toolbar / feed / banner / columns) composes into master-detail and feed
-- surfaces with zero custom CSS. Also the reactivity pair: demo/notes-wall
-- shares kit 'demo' with demo/field-notes, so mutating notes in one
-- live-refreshes the other (rvbbit:plate-data-changed, same-browser).

-- ── 1. Feed style: notes wall (reader for demo/field-notes' table) ──
SELECT rvbbit.upsert_plate(
  'demo/notes-wall',
  'Notes Wall',
  $tpl$
<div rv-each="stats" class="plate-banner">
  <span class="plate-banner-big">{{ row.n }}</span>
  <span>field notes</span>
  <span class="plate-banner-note" rv-if="row.latest_author">latest from {{ row.latest_author }} at {{ row.latest_at }}</span>
  <span class="plate-banner-note" rv-if="!row.latest_author">nothing logged yet</span>
</div>

<div class="plate-toolbar">
  <button type="button" rv-emit="author" rv-value="">All authors</button>
  <button rv-each="authors" type="button" rv-emit="author" rv-value="{{ row.author }}">{{ row.author }} · {{ row.n }}</button>
</div>

<div class="plate-feed">
  <div rv-each="notes" class="plate-feed-item">
    <div class="plate-feed-meta">{{ row.author }} · {{ row.at }}</div>
    <div>{{ row.note }}</div>
  </div>
</div>
$tpl$,
  jsonb_build_object(
    'stats', jsonb_build_object('sql', $q$
SELECT count(*)::int AS n,
       (SELECT author FROM demo_kit.field_notes ORDER BY id DESC LIMIT 1) AS latest_author,
       (SELECT to_char(created_at, 'HH24:MI:SS') FROM demo_kit.field_notes ORDER BY id DESC LIMIT 1) AS latest_at
FROM demo_kit.field_notes
WHERE (nullif({{ params.author }}, '') IS NULL OR author = {{ params.author }})
    $q$),
    'authors', jsonb_build_object('sql', $q$
SELECT author, count(*)::int AS n FROM demo_kit.field_notes GROUP BY author ORDER BY n DESC, author LIMIT 12
    $q$),
    'notes', jsonb_build_object('sql', $q$
SELECT author, note, to_char(created_at, 'HH24:MI:SS') AS at
FROM demo_kit.field_notes
WHERE (nullif({{ params.author }}, '') IS NULL OR author = {{ params.author }})
ORDER BY id DESC LIMIT 50
    $q$)
  ),
  '{}'::jsonb,
  '[{"name": "author", "default": ""}]'::jsonb,
  'demo',
  'Feed style: banner + author filter chips (param loop-back) + timeline feed; live-refreshes when demo/field-notes mutates'
);

-- ── 2. Master-detail split: rail nav + kv panel + detail table ──
SELECT rvbbit.upsert_plate(
  'demo/sightings-console',
  'Sightings Console',
  $tpl$
<div class="plate-split">
  <div class="plate-rail">
    <h3>States</h3>
    <button rv-each="states" type="button" rv-emit="state" rv-value="{{ row.state }}">{{ row.state }}<small>{{ row.n }}</small></button>
  </div>
  <div>
    <h2>{{ params.state }}</h2>
    <dl rv-each="summary" class="plate-kv">
      <dt>total sightings</dt><dd>{{ row.total }}</dd>
      <dt>counties</dt><dd>{{ row.counties }}</dd>
      <dt>busiest county</dt><dd>{{ row.top_county }} ({{ row.top_n }})</dd>
    </dl>
    <h3>Recent reports</h3>
    <table class="plate-table">
      <thead><tr><th>title</th><th>county</th></tr></thead>
      <tbody>
        <tr rv-each="recent">
          <td>{{ row.title }}</td>
          <td>{{ row.county }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</div>
$tpl$,
  jsonb_build_object(
    'states', jsonb_build_object('sql', $q$
SELECT state, count(*)::int AS n FROM public.bigfoot_sightings
WHERE state IS NOT NULL GROUP BY state ORDER BY n DESC LIMIT 12
    $q$),
    'summary', jsonb_build_object('sql', $q$
WITH s AS (SELECT * FROM public.bigfoot_sightings WHERE state = {{ params.state }})
SELECT (SELECT count(*) FROM s) AS total,
       (SELECT count(DISTINCT county) FROM s) AS counties,
       (SELECT county FROM s GROUP BY county ORDER BY count(*) DESC NULLS LAST LIMIT 1) AS top_county,
       (SELECT count(*) FROM s GROUP BY county ORDER BY count(*) DESC NULLS LAST LIMIT 1) AS top_n
    $q$),
    'recent', jsonb_build_object('sql', $q$
SELECT left(title, 90) AS title, county FROM public.bigfoot_sightings
WHERE state = {{ params.state }} ORDER BY bfroid DESC LIMIT 15
    $q$)
  ),
  '{}'::jsonb,
  '[{"name": "state", "default": "Washington"}]'::jsonb,
  NULL,
  'Master-detail split: state rail (rv-emit loop-back) + kv summary + detail table — no islands, all server-rendered'
);

SELECT 'layout plates seeded';
