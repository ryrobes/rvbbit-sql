-- 0200: The Hub — a front door for MCP-only users (docs/HUB_PLAN.md)
--
-- One URL (LENS/?hub, per-artifact deep link LENS/?hub&sel=<kind>:<slug>)
-- lands chat-first users in a Tableau-server-feel artifact browser that is
-- secretly a DataRabbit layout wall. Everything here is plates doctrine:
-- the gallery is a SELECT over rvbbit.artifact_index, the peek pane is a
-- single-row plate with a live rv-frame island, writes go through one
-- audited named action (toggle_pin).
--
--   * rvbbit.artifact_index — one row per consumable artifact: live apps /
--     dashboards (rvbbit.live_apps is a view over dashboards; kind derives
--     from app_kind) + latest metric/cube defs + alert rules with a
--     breaching status chip. Lineage rides along from dashboard_deps.
--   * rvbbit.hub_pins — box-wide favorites (per-user views arrive with
--     per-user MCP keys, WAREHOUSE_MCP_PLAN Phase 1).
--   * hub kit: hub/gallery + hub/peek plates and the 'hub' wall layout.
--
-- The lens side (same release): /?hub wall entry, rv-shot / rv-frame
-- artifact islands (handles, never URLs), /api/rvbbit/thumb proxy. The
-- warehouse-mcp side: auto-thumbnails on publish, /thumbs serving, and
-- hub_url in tool responses — distribution through the transcript.

-- artifact_index draft (freezes into 0200)
CREATE OR REPLACE VIEW rvbbit.artifact_index AS
WITH latest_dash_ver AS (
    SELECT DISTINCT ON (d.slug) d.slug, d.id, v.version
    FROM rvbbit.dashboards d
    JOIN rvbbit.dashboard_versions v ON v.dashboard_id = d.id
    ORDER BY d.slug, v.version DESC
),
dash_lineage AS (
    SELECT lv.slug,
           jsonb_agg(DISTINCT jsonb_build_object('kind', dp.kind, 'ref', dp.object_ref))
               FILTER (WHERE dp.object_ref IS NOT NULL) AS lineage
    FROM latest_dash_ver lv
    JOIN rvbbit.dashboard_deps dp ON dp.dashboard_id = lv.id AND dp.version = lv.version
    GROUP BY lv.slug
),
u AS (
    -- rvbbit.live_apps is a VIEW over rvbbit.dashboards (every published
    -- surface lives there); the artifact kind derives from app_kind.
    SELECT CASE WHEN a.app_kind = 'dashboard' THEN 'dashboard' ELSE 'app' END AS kind,
           a.slug AS ref, a.name AS title, a.description,
           a.owner_email AS owner, a.team,
           coalesce(nullif(a.status, ''), 'live') AS status,
           a.latest_version AS version, NULL::text AS native_category,
           a.updated_at, '/apps/' || a.slug AS path,
           coalesce(dl.lineage, '[]'::jsonb) AS lineage
    FROM rvbbit.live_apps a
    LEFT JOIN dash_lineage dl ON dl.slug = a.slug
  UNION ALL
    SELECT 'metric', m.name, m.name, m.description, m.owner,
           NULL, 'defined', m.version, NULL, m.created_at, NULL, '[]'::jsonb
    FROM (SELECT DISTINCT ON (name) * FROM rvbbit.metric_defs
          ORDER BY name, version DESC) m
  UNION ALL
    SELECT 'cube', c.name, c.name, c.description, c.owner,
           NULL, 'defined', c.version, nullif(c.category, ''),
           c.created_at, NULL, '[]'::jsonb
    FROM (SELECT DISTINCT ON (name) * FROM rvbbit.cube_defs
          ORDER BY name, version DESC) c
  UNION ALL
    SELECT 'alert', r.name, r.name, r.description, r.owner,
           NULL,
           CASE WHEN EXISTS (SELECT 1 FROM rvbbit.alert_state s
                             WHERE s.rule_name = r.name AND s.last_status = 'breach')
                THEN 'breaching' ELSE 'ok' END, r.version, NULL,
           r.created_at, NULL, '[]'::jsonb
    FROM (SELECT DISTINCT ON (name) * FROM rvbbit.alert_rules
          ORDER BY name, version DESC) r
)
SELECT u.kind, u.ref, u.title, u.description, u.owner, u.team, u.status,
       u.version, u.updated_at, u.path, u.lineage,
       coalesce(ec.category, u.native_category) AS category, ec.subcategory
FROM u
LEFT JOIN rvbbit.entity_categories ec
       ON ec.entity_kind = u.kind AND ec.entity_name = u.ref;

COMMENT ON VIEW rvbbit.artifact_index IS
    'One row per consumable artifact (app/dashboard/metric/cube/alert) — the Hub front page is a SELECT over this. docs/HUB_PLAN.md';

CREATE TABLE IF NOT EXISTS rvbbit.hub_pins (
    kind      text NOT NULL,
    ref       text NOT NULL,
    pinned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, ref)
);
COMMENT ON TABLE rvbbit.hub_pins IS
    'Hub gallery pins — box-wide favorites until per-user MCP identity lands (docs/HUB_PLAN.md §5 P3)';

SELECT rvbbit.upsert_kit('hub', 'The Hub',
  'The front door for chat-first users: a browsable index of every app, dashboard, metric, cube and alert made through the MCP. docs/HUB_PLAN.md');

SELECT rvbbit.upsert_plate(
  'hub/gallery',
  'Hub — Gallery',
  $tpl$
<div style="display:flex; flex-direction:column; gap:16px; padding:18px 20px; min-height:100%">
  <div style="display:flex; align-items:flex-end; gap:12px; flex-wrap:wrap">
    <div style="flex:1; min-width:220px">
      <h1 style="margin:0; font-size:20px; font-weight:700; letter-spacing:-0.01em">Your data, live</h1>
      <p class="plate-muted" style="margin:4px 0 0; font-size:12px">Apps, dashboards, metrics, cubes and alerts — everything made through chat, in one place.</p>
    </div>
    <input type="search" rv-emit="q" value="{{ params.q }}" placeholder="Search…"
           style="min-width:240px; padding:8px 12px; border-radius:8px; border:1px solid rgba(128,128,128,0.35); background:rgba(128,128,128,0.08); font-size:13px">
    <select rv-emit="kind" query="kinds" value="v" label="l" placeholder="All kinds"></select>
    <select rv-emit="owner" query="owners" value="v" label="l" placeholder="Everyone"></select>
  </div>
  <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(225px, 1fr)); gap:14px">
    <button rv-each="arts" rv-emit="sel" rv-value="{{ row.kind }}:{{ row.ref }}" rv-open="plate:hub/peek@peek"
            style="display:flex; flex-direction:column; gap:8px; padding:10px; border-radius:10px; text-align:left; cursor:pointer; border:{{ row.card_border }}; background:rgba(128,128,128,0.06)">
      <div rv-if="row.has_shot" style="height:118px; overflow:hidden; border-radius:6px">
        <rv-shot kind="{{ row.kind }}" slug="{{ row.ref }}" title="{{ row.title }}"></rv-shot>
      </div>
      <div rv-if="!row.has_shot" style="height:118px; display:flex; align-items:center; justify-content:center; border-radius:6px; background:rgba(128,128,128,0.10); font-size:30px; opacity:0.55">{{ row.glyph }}</div>
      <div style="display:flex; align-items:center; gap:7px; flex-wrap:wrap">
        <span style="font-size:9px; text-transform:uppercase; letter-spacing:0.09em; padding:2px 7px; border-radius:99px; background:rgba(128,128,128,0.20)">{{ row.kind }}</span>
        <span rv-if="row.breaching" style="font-size:9px; text-transform:uppercase; letter-spacing:0.09em; padding:2px 7px; border-radius:99px; background:rgba(220,70,60,0.30)">breaching</span>
        <span rv-if="row.pinned" style="font-size:10px; color:rgba(245,180,70,0.9)">★</span>
        <span class="plate-muted" style="font-size:10px">{{ row.updated }}</span>
      </div>
      <div style="font-weight:600; font-size:13px; line-height:1.3">{{ row.title }}</div>
      <div class="plate-muted" style="font-size:11px; line-height:1.4; max-height:46px; overflow:hidden">{{ row.description }}</div>
      <div class="plate-muted" style="font-size:10px; margin-top:auto">{{ row.owner }}</div>
    </button>
  </div>
  <p rv-if="empty.yes" class="plate-muted" style="text-align:center; padding:26px 0 8px">Nothing matches — clear the search or filters.</p>
</div>
$tpl$,
  jsonb_build_object(
    'arts', jsonb_build_object('sql', $q$
      SELECT a.kind, a.ref, a.title,
             coalesce(a.description, '') AS description,
             coalesce(a.owner, '') AS owner,
             to_char(a.updated_at, 'Mon DD') AS updated,
             (a.kind IN ('app','dashboard'))::int AS has_shot,
             (a.status = 'breaching')::int AS breaching,
             (p.ref IS NOT NULL)::int AS pinned,
             CASE a.kind WHEN 'metric' THEN '∑' WHEN 'cube' THEN '⬡' WHEN 'alert' THEN '⚠' ELSE '▦' END AS glyph,
             CASE WHEN a.kind || ':' || a.ref = {{ params.sel }}
                  THEN '1px solid rgba(245,180,70,0.85)'
                  ELSE '1px solid rgba(128,128,128,0.25)' END AS card_border
      FROM rvbbit.artifact_index a
      LEFT JOIN rvbbit.hub_pins p ON p.kind = a.kind AND p.ref = a.ref
      WHERE (nullif({{ params.q }}, '') IS NULL
             OR a.title ILIKE '%' || {{ params.q }} || '%'
             OR coalesce(a.description, '') ILIKE '%' || {{ params.q }} || '%'
             OR a.ref ILIKE '%' || {{ params.q }} || '%')
        AND (nullif({{ params.kind }}, '') IS NULL OR a.kind = {{ params.kind }})
        AND (nullif({{ params.owner }}, '') IS NULL OR coalesce(a.owner, '') = {{ params.owner }})
      ORDER BY (p.ref IS NOT NULL) DESC, a.updated_at DESC
    $q$),
    'kinds', jsonb_build_object('sql', $q$
      SELECT kind AS v, kind || 's · ' || count(*) AS l
      FROM rvbbit.artifact_index GROUP BY kind ORDER BY kind
    $q$),
    'owners', jsonb_build_object('sql', $q$
      SELECT DISTINCT owner AS v, owner AS l FROM rvbbit.artifact_index
      WHERE coalesce(owner, '') <> '' ORDER BY 1
    $q$),
    'empty', jsonb_build_object('sql', $q$
      SELECT (count(*) = 0)::int AS yes
      FROM rvbbit.artifact_index
      WHERE (nullif({{ params.q }}, '') IS NULL
             OR title ILIKE '%' || {{ params.q }} || '%'
             OR coalesce(description, '') ILIKE '%' || {{ params.q }} || '%'
             OR ref ILIKE '%' || {{ params.q }} || '%')
        AND (nullif({{ params.kind }}, '') IS NULL OR kind = {{ params.kind }})
        AND (nullif({{ params.owner }}, '') IS NULL OR coalesce(owner, '') = {{ params.owner }})
    $q$)
  ),
  '{}'::jsonb,
  jsonb_build_array(
    jsonb_build_object('name', 'q', 'default', ''),
    jsonb_build_object('name', 'kind', 'default', ''),
    jsonb_build_object('name', 'owner', 'default', ''),
    jsonb_build_object('name', 'sel', 'default', '', 'from_bus', true)
  ),
  'hub',
  'The Hub front page: searchable, faceted card gallery over rvbbit.artifact_index. Cards emit sel and open hub/peek in the peek slot.'
);

SELECT rvbbit.upsert_plate(
  'hub/peek',
  'Hub — Preview',
  $tpl$
<div rv-if="!has.yes" style="display:flex; height:100%; min-height:300px; align-items:center; justify-content:center; padding:28px">
  <p class="plate-muted" style="text-align:center; font-size:12px">Select an artifact to preview it here.</p>
</div>
<div rv-each="art" style="display:flex; flex-direction:column; gap:10px; padding:16px 18px; min-height:100%">
  <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
    <span style="font-size:9px; text-transform:uppercase; letter-spacing:0.09em; padding:2px 8px; border-radius:99px; background:rgba(128,128,128,0.20)">{{ row.kind_label }}</span>
    <span rv-if="row.breaching" style="font-size:9px; text-transform:uppercase; letter-spacing:0.09em; padding:2px 8px; border-radius:99px; background:rgba(220,70,60,0.30)">breaching</span>
    <span class="plate-muted" style="font-size:11px">{{ row.updated }}</span>
  </div>
  <h2 style="margin:0; font-size:17px; font-weight:700; letter-spacing:-0.01em">{{ row.title }}</h2>
  <p class="plate-muted" style="margin:0; font-size:12px; line-height:1.45">{{ row.description }}</p>
  <div style="display:flex; align-items:center; gap:10px">
    <span class="plate-muted" style="font-size:11px">{{ row.owner }} {{ row.team }}</span>
    <form rv-action="toggle_pin" style="display:inline">
      <input type="hidden" name="kind" value="{{ row.kind }}">
      <input type="hidden" name="ref" value="{{ row.ref }}">
      <button rv-if="!row.pinned" type="submit" style="font-size:10px; padding:2px 9px; border-radius:99px; border:1px solid rgba(128,128,128,0.35); background:transparent; cursor:pointer">☆ Pin to top</button>
      <button rv-if="row.pinned" type="submit" style="font-size:10px; padding:2px 9px; border-radius:99px; border:1px solid rgba(245,180,70,0.55); background:rgba(245,180,70,0.12); cursor:pointer">★ Pinned</button>
    </form>
  </div>
  <div rv-if="row.is_frame" style="flex:1">
    <rv-frame kind="{{ row.kind }}" slug="{{ row.ref }}" title="{{ row.title }}" height="470"></rv-frame>
  </div>
</div>
<div rv-if="lineage.lref" style="padding:0 18px 10px">
  <div class="plate-muted" style="font-size:9px; text-transform:uppercase; letter-spacing:0.09em; margin-bottom:6px">Built from</div>
  <div style="display:flex; gap:6px; flex-wrap:wrap">
    <span rv-each="lineage" style="font-size:10px; padding:2px 8px; border-radius:99px; background:rgba(128,128,128,0.14)">{{ row.lkind }} · {{ row.lref }}</span>
  </div>
</div>
<div rv-if="def.def_sql" style="padding:0 18px 16px">
  <button rv-each="def" rv-open-sql="{{ row.def_sql }}" rv-open-sql-title="Definition SQL"
          style="font-size:11px; padding:6px 12px; border-radius:8px; border:1px solid rgba(128,128,128,0.35); background:rgba(128,128,128,0.10); cursor:pointer">View definition SQL</button>
</div>
$tpl$,
  jsonb_build_object(
    'art', jsonb_build_object('sql', $q$
      SELECT kind, ref, title,
             coalesce(description, '') AS description,
             coalesce(owner, '—') AS owner, coalesce(team, '') AS team,
             to_char(updated_at, 'Mon DD YYYY, HH24:MI') AS updated,
             (kind IN ('app','dashboard'))::int AS is_frame,
             (status = 'breaching')::int AS breaching,
             (EXISTS (SELECT 1 FROM rvbbit.hub_pins p
                      WHERE p.kind = artifact_index.kind AND p.ref = artifact_index.ref))::int AS pinned,
             CASE kind WHEN 'app' THEN 'Live app' WHEN 'dashboard' THEN 'Dashboard'
                       WHEN 'metric' THEN 'Metric' WHEN 'cube' THEN 'Cube' ELSE 'Alert' END AS kind_label
      FROM rvbbit.artifact_index
      WHERE kind || ':' || ref = nullif({{ params.sel }}, '')
    $q$),
    'has', jsonb_build_object('sql', $q$
      SELECT (count(*) > 0)::int AS yes FROM rvbbit.artifact_index
      WHERE kind || ':' || ref = nullif({{ params.sel }}, '')
    $q$),
    'lineage', jsonb_build_object('sql', $q$
      SELECT initcap(j->>'kind') AS lkind, j->>'ref' AS lref
      FROM rvbbit.artifact_index a, jsonb_array_elements(a.lineage) AS j
      WHERE a.kind || ':' || a.ref = nullif({{ params.sel }}, '')
      ORDER BY 1, 2
    $q$),
    'def', jsonb_build_object('sql', $q$
      SELECT def_sql FROM (
        SELECT 'metric:' || name AS k, sql AS def_sql
        FROM (SELECT DISTINCT ON (name) name, sql FROM rvbbit.metric_defs ORDER BY name, version DESC) m
        UNION ALL
        SELECT 'cube:' || name, sql
        FROM (SELECT DISTINCT ON (name) name, sql FROM rvbbit.cube_defs ORDER BY name, version DESC) c
      ) d WHERE k = nullif({{ params.sel }}, '')
    $q$)
  ),
  jsonb_build_object(
    'toggle_pin', jsonb_build_object(
      'sql', $q$
        WITH del AS (
          DELETE FROM rvbbit.hub_pins WHERE kind = {{kind}} AND ref = {{ref}} RETURNING 1
        )
        INSERT INTO rvbbit.hub_pins (kind, ref)
        SELECT {{kind}}, {{ref}} WHERE NOT EXISTS (SELECT 1 FROM del)
      $q$,
      'args', jsonb_build_array(
        jsonb_build_object('name', 'kind', 'type', 'text', 'required', true),
        jsonb_build_object('name', 'ref', 'type', 'text', 'required', true)
      ),
      'description', 'Pin/unpin this artifact to the top of the Hub gallery (box-wide)'
    )
  ),
  jsonb_build_array(
    jsonb_build_object('name', 'sel', 'default', '', 'from_bus', true)
  ),
  'hub',
  'The Hub preview pane: live iframe for apps/dashboards (rv-frame), lineage strip, and a built-not-run definition-SQL breadcrumb for metrics/cubes.'
);

SELECT rvbbit.upsert_layout(
  'hub',
  'The Hub',
  '{"width": 1600, "height": 900}'::jsonb,
  jsonb_build_array(
    jsonb_build_object('id', 'cards', 'plate', 'hub/gallery', 'x', 0, 'y', 0, 'w', 0.63, 'h', 1, 'z', 0),
    jsonb_build_object('id', 'peek', 'plate', 'hub/peek', 'x', 0.63, 'y', 0, 'w', 0.37, 'h', 1, 'z', 0, 'slot', true, 'title', 'Preview')
  ),
  'hub',
  'The Hub wall: gallery left, preview slot right. Entry URL: /?hub'
);
