-- 0202: Hub peek — metric callouts + cube data grids
--
-- Selecting a metric in the Hub now shows its CURRENT VALUE as a callout
-- (rvbbit._hub_metric_callout resolves through the metrics layer;
-- anything unresolvable — params required, broken def — simply shows no
-- callout). Selecting a materialized cube shows its actual rows in a data
-- grid via the new sql-from grid capability (the lens runs SQL held in
-- another query's first row: SELECT-shaped only, read-only, hard-capped —
-- how a grid renders a table whose name is only known per-selection).
-- Thumbnails also became fully self-healing in this release: the
-- warehouse /thumbs route enqueues a capture on miss OR staleness
-- (throttled, deduped), so pre-Hub artifacts get thumbnails just by
-- being looked at. No SQL for that part — it lives in warehouse-mcp.

-- Metric callout for the Hub peek: resolve the metric's CURRENT value via
-- the metrics layer (rvbbit.metric returns SETOF jsonb, {"value": N}).
-- Anything that can't resolve — params required, broken def, non-numeric —
-- returns no rows and the peek simply doesn't show a callout.
CREATE OR REPLACE FUNCTION rvbbit._hub_metric_callout(p_sel text)
RETURNS TABLE(show int, value double precision, label text)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_name  text;
    v_row   jsonb;
    v_num   double precision;
    v_grain text;
BEGIN
    IF p_sel IS NULL OR p_sel NOT LIKE 'metric:%' THEN
        RETURN;
    END IF;
    v_name := substr(p_sel, 8);
    BEGIN
        SELECT m INTO v_row FROM rvbbit.metric(v_name) m LIMIT 1;
        v_num := (v_row ->> 'value')::double precision;
    EXCEPTION WHEN OTHERS THEN
        RETURN;
    END;
    IF v_num IS NULL THEN
        RETURN;
    END IF;
    SELECT g.grain INTO v_grain
    FROM (SELECT DISTINCT ON (name) grain FROM rvbbit.metric_defs
          WHERE name = v_name ORDER BY name, version DESC) g;
    RETURN QUERY SELECT 1, v_num,
        'current value' || coalesce(' · ' || nullif(v_grain, '') || ' grain', '');
END $fn$;

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
<div rv-if="mval.show" style="padding:6px 18px 2px">
  <rv-metric query="mval" value="value" label="label"></rv-metric>
</div>
<div rv-if="gridsql.show" style="padding:2px 18px 10px">
  <div class="plate-muted" style="font-size:9px; text-transform:uppercase; letter-spacing:0.09em; margin-bottom:6px">Data</div>
  <div style="max-height:320px; overflow:auto; border:1px solid rgba(128,128,128,0.22); border-radius:8px">
    <rv-grid sql-from="gridsql.q" limit="50"></rv-grid>
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
    'mval', jsonb_build_object('sql', $q$
      SELECT show, value, label
      FROM rvbbit._hub_metric_callout(nullif({{ params.sel }}, ''))
    $q$),
    'gridsql', jsonb_build_object('sql', $q$
      SELECT 1 AS show,
             'SELECT * FROM cubes.' || quote_ident(split_part({{ params.sel }}, ':', 2)) || ' LIMIT 50' AS q
      WHERE {{ params.sel }} LIKE 'cube:%'
        AND to_regclass('cubes.' || quote_ident(split_part({{ params.sel }}, ':', 2))) IS NOT NULL
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
