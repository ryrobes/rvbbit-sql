-- 0201: Hub open ergonomics — shorter breadcrumbs, richer peek
--
-- Field feedback on 0200: opening an artifact should be one gesture, and
-- the peek should show what the artifact is MADE of.
--
--   * Cards carry rv-open-dbl="app:live?slug=…&kind=…" — double-click
--     opens the artifact's native DataRabbit window (apps/dashboards get
--     their standalone dashboard-app window; other kinds pop the peek) and
--     leaves the wall. Single click still previews (delayed one beat so
--     the pair can be told apart; the plate refetch on emit unmounts DOM,
--     so the lens detects the pair by attribute value, not dblclick).
--   * The peek grows: an explicit "Open in DataRabbit" button, a Queries
--     section — each crawled dep query with a LIVE first-row sample via
--     rvbbit._hub_query_preview() and a built-not-run open button — and
--     table lineage chips that open as SELECT … LIMIT 500.
--   * rv-frame gains a hover "open ↗" pill in the lens (the artifact's
--     external URL, the same link chat hands out) — no SQL change.

-- Peek-pane query previews: run a crawled dep query (SELECT-shaped only,
-- LIMIT-wrapped) and return a one-line sample — "col=val · col=val · N cols".
-- Errors and non-SELECTs degrade to text, never break the render. The plate
-- render path runs read-only, so this can't be a write vector; cost is
-- bounded by the LIMIT wrap plus the peek's own per-query instrumentation.
CREATE OR REPLACE FUNCTION rvbbit._hub_query_preview(p_sql text, p_rows int DEFAULT 1)
RETURNS text LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_sql  text := rtrim(btrim(coalesce(p_sql, '')), ';');
    v_head text := upper(left(ltrim(v_sql), 6));
    v_row  jsonb;
    v_cols int;
    v_out  text := '';
    kv     record;
BEGIN
    IF v_sql = '' OR (v_head NOT LIKE 'SELECT%' AND v_head NOT LIKE 'WITH%') THEN
        RETURN NULL;
    END IF;
    BEGIN
        EXECUTE format('SELECT to_jsonb(__q) FROM (%s) __q LIMIT 1', v_sql) INTO v_row;
    EXCEPTION WHEN OTHERS THEN
        RETURN '⚠ ' || left(SQLERRM, 120);
    END;
    IF v_row IS NULL THEN
        RETURN '(no rows)';
    END IF;
    SELECT count(*) INTO v_cols FROM jsonb_object_keys(v_row);
    FOR kv IN SELECT key, value FROM jsonb_each_text(v_row) ORDER BY key LIMIT 4 LOOP
        v_out := v_out || CASE WHEN v_out = '' THEN '' ELSE ' · ' END
                 || kv.key || '=' || coalesce(left(kv.value, 28), '∅');
    END LOOP;
    IF v_cols > 4 THEN
        v_out := v_out || ' · +' || (v_cols - 4) || ' cols';
    END IF;
    RETURN left(v_out, 200);
END $fn$;

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
            rv-open-dbl="app:live?slug={{ row.ref }}&kind={{ row.kind }}" title="Click to preview · double-click to open in DataRabbit"
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
  <div rv-if="row.is_frame" style="display:flex; align-items:center; gap:8px">
    <button rv-open="app:live?slug={{ row.ref }}&kind={{ row.kind }}"
            style="font-size:11px; padding:5px 12px; border-radius:8px; border:1px solid rgba(245,180,70,0.55); background:rgba(245,180,70,0.12); cursor:pointer">Open in DataRabbit</button>
  </div>
  <div rv-if="row.is_frame" style="flex:1">
    <rv-frame kind="{{ row.kind }}" slug="{{ row.ref }}" title="{{ row.title }}" height="470"></rv-frame>
  </div>
</div>
<div rv-if="lineage.lref" style="padding:0 18px 10px">
  <div class="plate-muted" style="font-size:9px; text-transform:uppercase; letter-spacing:0.09em; margin-bottom:6px">Built from</div>
  <div style="display:flex; gap:6px; flex-wrap:wrap">
    <span rv-each="lineage" style="display:inline-flex">
      <button rv-if="row.open_sql" rv-open-sql="{{ row.open_sql }}" rv-open-sql-title="{{ row.lref }}"
              style="font-size:10px; padding:2px 8px; border-radius:99px; border:1px solid rgba(128,128,128,0.30); background:rgba(128,128,128,0.14); cursor:pointer" title="Open this table in DataRabbit">{{ row.lkind }} · {{ row.lref }} ↗</button>
      <span rv-if="!row.open_sql" style="font-size:10px; padding:2px 8px; border-radius:99px; background:rgba(128,128,128,0.14)">{{ row.lkind }} · {{ row.lref }}</span>
    </span>
  </div>
</div>
<div rv-if="qpreviews.qname" style="padding:0 18px 10px">
  <div class="plate-muted" style="font-size:9px; text-transform:uppercase; letter-spacing:0.09em; margin-bottom:6px">Queries</div>
  <div style="display:flex; flex-direction:column; gap:6px">
    <div rv-each="qpreviews" style="display:flex; align-items:center; gap:10px; padding:6px 10px; border-radius:8px; background:rgba(128,128,128,0.08)">
      <div style="flex:1; min-width:0">
        <div style="font-size:11px; font-weight:600">{{ row.qname }}</div>
        <div class="plate-muted" style="font-size:10px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">{{ row.preview }}</div>
      </div>
      <button rv-open-sql="{{ row.base_sql }}" rv-open-sql-title="{{ row.qname }}"
              style="font-size:10px; padding:3px 10px; border-radius:99px; border:1px solid rgba(128,128,128,0.35); background:transparent; cursor:pointer; white-space:nowrap" title="Open this query in DataRabbit">open ↗</button>
    </div>
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
      SELECT initcap(j->>'kind') AS lkind, j->>'ref' AS lref,
             CASE WHEN j->>'kind' = 'table' AND to_regclass(j->>'ref') IS NOT NULL
                  THEN 'SELECT * FROM ' || (to_regclass(j->>'ref'))::text || ' LIMIT 500'
                  ELSE '' END AS open_sql
      FROM rvbbit.artifact_index a, jsonb_array_elements(a.lineage) AS j
      WHERE a.kind || ':' || a.ref = nullif({{ params.sel }}, '')
      ORDER BY 1, 2
    $q$),
    'qpreviews', jsonb_build_object('sql', $q$
      SELECT dp.object_ref AS qname, dp.base_sql,
             coalesce(rvbbit._hub_query_preview(dp.base_sql), '') AS preview
      FROM rvbbit.dashboards d
      JOIN rvbbit.dashboard_deps dp
        ON dp.dashboard_id = d.id AND dp.version = d.latest_version
      WHERE (CASE WHEN d.app_kind = 'dashboard' THEN 'dashboard' ELSE 'app' END)
              || ':' || d.slug = nullif({{ params.sel }}, '')
        AND dp.kind = 'query' AND coalesce(dp.base_sql, '') <> ''
      ORDER BY dp.object_ref
      LIMIT 8
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
