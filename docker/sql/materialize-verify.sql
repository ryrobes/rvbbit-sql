-- Metric materialization verification.
--   * define auto-derives table deps + a default (compaction-on) policy
--   * a new generation enqueues; materialize_tick drains -> one observation/gen
--   * observation captures value + verdict (as-decided) + bitemporal coords
--   * manual backfill of an older generation's data with the current def
--   * metric_history reader = the durable series
\set ON_ERROR_STOP on
\pset pager off

DELETE FROM rvbbit.metric_defs WHERE name IN ('mat_kpi','mat_rowset');
DELETE FROM rvbbit.metric_observations WHERE metric_name IN ('mat_kpi','mat_rowset');
DELETE FROM rvbbit.metric_materialize WHERE metric_name IN ('mat_kpi','mat_rowset');
DELETE FROM rvbbit.metric_dependencies WHERE metric_name IN ('mat_kpi','mat_rowset');
DROP TABLE IF EXISTS mat_demo;

CREATE TABLE mat_demo (region text, amount int) USING rvbbit;
SELECT count(*) AS g1 FROM rvbbit.snapshot_load('mat_demo',
  $q$SELECT * FROM (VALUES ('US',40),('EU',60)) v(region,amount)$q$);            -- gen1 total 100

-- define a KPI (target 150) → auto deps + default policy
SELECT rvbbit.define_metric('mat_kpi',
  'SELECT sum(amount) AS total FROM mat_demo', '{"target":150}'::jsonb,
  'all','Total revenue KPI','analytics','{}'::jsonb,
  'SELECT total >= {target} AS ok, total AS value, {target}::numeric AS target FROM metric') AS v1;

SELECT 'deps (want mat_demo)'          AS check, string_agg(table_name, ',') AS got
  FROM rvbbit.metric_dependencies WHERE metric_name='mat_kpi';
SELECT 'policy on_compaction (want t)' AS check, on_compaction::text AS got
  FROM rvbbit.metric_materialize WHERE metric_name='mat_kpi';

-- a NEW generation fires the trigger → enqueue → drain
SELECT pg_sleep(1.1);
SELECT count(*) AS g2 FROM rvbbit.snapshot_load('mat_demo',
  $q$SELECT * FROM (VALUES ('US',140),('EU',60),('APAC',50)) v(region,amount)$q$); -- gen2 total 250

SELECT 'queue after gen2 (want 1)'  AS check, count(*)::text AS got FROM rvbbit.materialize_queue;
SELECT 'tick (want 1 materialized)' AS check, rvbbit.materialize_tick()::text AS got;
SELECT 'queue after tick (want 0)'  AS check, count(*)::text AS got FROM rvbbit.materialize_queue;

-- backfill gen1's DATA with the CURRENT def (metric did not exist at gen1's time)
SELECT rvbbit.materialize_metric('mat_kpi','{}'::jsonb, now(),
   (SELECT committed_at FROM rvbbit.generations WHERE table_oid='mat_demo'::regclass AND generation=1),
   1, 'backfill') AS backfilled;

-- the durable series: gen1 fail (100<150), gen2 pass (250>=150)
SELECT 'history' AS check, data_generation AS gen, (value->>'value') AS total,
       status, trigger
FROM rvbbit.metric_history('mat_kpi') ORDER BY data_generation;

-- A rowset is fine as SQL, but not as a persisted metric observation.
SELECT rvbbit.define_metric('mat_rowset', 'SELECT region, amount FROM mat_demo') AS rowset_v;
\set ON_ERROR_STOP off
SELECT 'rowset materialize should fail scalar contract' AS check;
SELECT rvbbit.materialize_metric('mat_rowset');
\set ON_ERROR_STOP on

-- cleanup
DELETE FROM rvbbit.metric_defs WHERE name IN ('mat_kpi','mat_rowset');
DELETE FROM rvbbit.metric_observations WHERE metric_name IN ('mat_kpi','mat_rowset');
DELETE FROM rvbbit.metric_materialize WHERE metric_name IN ('mat_kpi','mat_rowset');
DELETE FROM rvbbit.metric_dependencies WHERE metric_name IN ('mat_kpi','mat_rowset');
DROP TABLE mat_demo;
SELECT 'materialize-verify complete' AS done;
