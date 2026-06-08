-- KPI checks / thresholds verification.
--   * check_metric verdict (pass/fail, status, value/target)
--   * threshold-override sugar (caller param wins, no new version)
--   * bitemporal: def-time (threshold version) x data-time, independent
--   * preview_check_sql (draft); no-check => NULL; error surfaces
\set ON_ERROR_STOP on
\pset pager off

DELETE FROM rvbbit.metric_defs WHERE name IN ('daily_revenue','plain_metric');

-- ════════ data: two generations (gen1 total=100, gen2 total=200) ════════
DROP TABLE IF EXISTS kpi_demo;
CREATE TABLE kpi_demo (region text, amount int) USING rvbbit;
SELECT count(*) AS g1 FROM rvbbit.snapshot_load('kpi_demo',
  $q$SELECT * FROM (VALUES ('US',40),('EU',60)) v(region,amount)$q$);            -- gen1 total 100
SELECT pg_sleep(1.1);
SELECT count(*) AS g2 FROM rvbbit.snapshot_load('kpi_demo',
  $q$SELECT * FROM (VALUES ('US',40),('EU',60),('APAC',100)) v(region,amount)$q$); -- gen2 total 200

SELECT committed_at AS ts1 FROM rvbbit.generations WHERE table_oid='kpi_demo'::regclass AND generation=1 \gset
SELECT committed_at AS ts2 FROM rvbbit.generations WHERE table_oid='kpi_demo'::regclass AND generation=2 \gset

-- ════════ define a KPI: revenue total, target default 150 ════════
SELECT rvbbit.define_metric(
  'daily_revenue',
  'SELECT sum(amount) AS total FROM kpi_demo',
  '{"target": 150}'::jsonb,
  'all', 'Total revenue (KPI)', 'analytics', '{}'::jsonb,
  'SELECT total >= {target} AS ok, total AS value, {target}::numeric AS target FROM metric'
) AS v1;
SELECT now() AS d1 \gset
SELECT pg_sleep(1.1);

-- ════════ Part A: verdict across data-time (def = v1, target 150) ════════
SELECT 'A1 latest data=200 >= 150 (want ok=true, pass)' AS check,
       rvbbit.check_metric('daily_revenue')::text AS got;
SELECT 'A2 data=ts1 (100 >= 150 -> ok=false, fail)' AS check,
       rvbbit.check_metric('daily_revenue','{}'::jsonb, now(), :'ts1')::text AS got;

-- ════════ Part B: threshold-override sugar (same version, caller wins) ════════
SELECT 'B1 override target=250 latest (200 >= 250 -> false)' AS check,
       rvbbit.check_metric('daily_revenue','{"target":250}'::jsonb)::text AS got;
SELECT 'B2 override target=50 latest (200 >= 50 -> true)' AS check,
       rvbbit.check_metric('daily_revenue','{"target":50}'::jsonb)::text AS got;

-- ════════ Part C: bitemporal threshold — new version raises target to 300 ════════
SELECT rvbbit.define_metric(
  'daily_revenue',
  'SELECT sum(amount) AS total FROM kpi_demo',
  '{"target": 300}'::jsonb,
  'all', 'Total revenue (KPI, stricter)', 'analytics', '{}'::jsonb,
  'SELECT total >= {target} AS ok, total AS value, {target}::numeric AS target FROM metric'
) AS v2;

-- SAME data (latest, 200), DIFFERENT threshold version:
SELECT 'C1 def=v2 (target 300) data=latest (200 >= 300 -> false)' AS check,
       rvbbit.check_metric('daily_revenue')::text AS got;
SELECT 'C2 def=v1 (target 150) data=latest (200 >= 150 -> true)' AS check,
       rvbbit.check_metric('daily_revenue','{}'::jsonb, :'d1')::text AS got;

-- ════════ Part D: preview a DRAFT check (Creator) ════════
SELECT 'D1 preview draft (250 target on 200 -> false)' AS check,
       rvbbit.preview_check_sql(
         'SELECT sum(amount) AS total FROM kpi_demo',
         'SELECT total >= {target} AS ok FROM metric',
         '{"target":250}'::jsonb)::text AS got;

-- ════════ Part E: no check => NULL (not a KPI) ════════
SELECT rvbbit.define_metric('plain_metric', 'SELECT 1 AS x', '{}'::jsonb) AS pv;
SELECT 'E1 plain metric has no check (want <NULL>)' AS check,
       coalesce(rvbbit.check_metric('plain_metric')::text, '<NULL>') AS got;

-- ════════ Part F: error surfaces (must RAISE) ════════
\set ON_ERROR_STOP off
SELECT 'F1 expect missing-ok error below:' AS check;
SELECT rvbbit.preview_check_sql('SELECT 1 AS total', 'SELECT total FROM metric')::text;
SELECT 'F2 expect more-than-one-row error below:' AS check;
SELECT rvbbit.preview_check_sql(
  'SELECT * FROM (VALUES (1),(2)) v(total)',
  'SELECT total >= 1 AS ok FROM metric')::text;
\set ON_ERROR_STOP on

-- cleanup
DELETE FROM rvbbit.metric_defs WHERE name IN ('daily_revenue','plain_metric');
DROP TABLE kpi_demo;
SELECT 'checks-verify complete' AS done;
