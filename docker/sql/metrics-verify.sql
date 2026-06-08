-- Metrics / BI layer verification.
--   * the rvbbit.as_of_timestamp GUC (data-time, reaches nested EXECUTE)
--   * define_metric / metric_sql / metric
--   * bitemporal 2x2 matrix (def-time x data-time, independent axes)
--   * {metric:NAME} composition + {param} substitution
--   * cycle detection + error surfaces
\set ON_ERROR_STOP on
\pset pager off

DELETE FROM rvbbit.metric_defs
 WHERE name IN ('total_sales','base_rows','big_sales','cyc_a','cyc_b');

-- ════════ data table: two data-time generations (snapshot mode) ════════
DROP TABLE IF EXISTS mt_sales;
CREATE TABLE mt_sales (id int, amount int) USING rvbbit;
SELECT count(*) AS gen1_rows
  FROM rvbbit.snapshot_load('mt_sales',
       $q$SELECT * FROM (VALUES (1,40),(2,60)) v(id,amount)$q$);          -- gen1 sum=100
SELECT pg_sleep(1.1);
SELECT count(*) AS gen2_rows
  FROM rvbbit.snapshot_load('mt_sales',
       $q$SELECT * FROM (VALUES (1,40),(2,60),(3,100)) v(id,amount)$q$);   -- gen2 sum=200

SELECT committed_at AS ts1 FROM rvbbit.generations
 WHERE table_oid='mt_sales'::regclass AND generation=1 \gset
SELECT committed_at AS ts2 FROM rvbbit.generations
 WHERE table_oid='mt_sales'::regclass AND generation=2 \gset

-- ════════ Part A: rvbbit.as_of_timestamp GUC (no metrics) ════════
SELECT 'A0 latest sum (want 200)'      AS check, sum(amount)::text AS got FROM mt_sales;
SET rvbbit.as_of_timestamp = :'ts1';
SELECT 'A1 guc asof ts1 (want 100)'    AS check, sum(amount)::text AS got FROM mt_sales;
SET rvbbit.as_of_timestamp = :'ts2';
SELECT 'A2 guc asof ts2 (want 200)'    AS check, sum(amount)::text AS got FROM mt_sales;
RESET rvbbit.as_of_timestamp;
SELECT 'A3 after reset (want 200)'     AS check, sum(amount)::text AS got FROM mt_sales;

-- ════════ Part B: two def-time versions ════════
SELECT rvbbit.define_metric('total_sales',
       'SELECT sum(amount) AS total FROM mt_sales') AS v1_version;
SELECT now() AS d1 \gset
SELECT pg_sleep(1.1);
SELECT rvbbit.define_metric('total_sales',
       'SELECT sum(amount) AS total, count(*) AS n FROM mt_sales') AS v2_version;

SELECT 'B1 versions (want 2)'          AS check, count(*)::text AS got
  FROM rvbbit.metric_versions('total_sales');
SELECT 'B2 catalog current (want 2)'   AS check, version::text AS got
  FROM rvbbit.metric_catalog WHERE name='total_sales';

-- ════════ Part C: bitemporal 2x2 matrix ════════
-- def v1 (as of d1): only `total`.   def v2 (now): adds `n`.
-- data ts1 = gen1 (sum 100).         data ts2 = gen2 (sum 200).
SELECT 'C1 def=v1 data=ts1 (want total=100, no n)' AS check,
       rvbbit.metric('total_sales','{}'::jsonb, :'d1', :'ts1')::text AS got;
SELECT 'C2 def=v1 data=ts2 (want total=200, no n)' AS check,
       rvbbit.metric('total_sales','{}'::jsonb, :'d1', :'ts2')::text AS got;
SELECT 'C3 def=v2 data=ts1 (want total=100, n=2)' AS check,
       rvbbit.metric('total_sales','{}'::jsonb, now(), :'ts1')::text AS got;
SELECT 'C4 def=v2 data=ts2 (want total=200, n=3)' AS check,
       rvbbit.metric('total_sales','{}'::jsonb, now(), :'ts2')::text AS got;

-- ════════ Part D: composition {metric:X} + params {p} ════════
SELECT rvbbit.define_metric('base_rows', 'SELECT id, amount FROM mt_sales');
SELECT rvbbit.define_metric('big_sales',
       'SELECT count(*) AS n, sum(amount) AS total FROM {metric:base_rows} b WHERE b.amount >= {min}',
       '{"min": 50}'::jsonb);

SELECT 'D1 preview sql (inlined subquery + literal)' AS check,
       rvbbit.metric_sql('big_sales') AS got;
SELECT 'D2 default min=50 over latest (want n=2,total=160)' AS check,
       rvbbit.metric('big_sales')::text AS got;
SELECT 'D3 override min=100 (want n=1,total=100)' AS check,
       rvbbit.metric('big_sales','{"min":100}'::jsonb)::text AS got;
SELECT 'D4 min=50 over data=ts1 (want n=1,total=60)' AS check,
       rvbbit.metric('big_sales','{"min":50}'::jsonb, now(), :'ts1')::text AS got;

-- ════════ Part E: cycle detection + missing metric (both must RAISE) ════════
SELECT rvbbit.define_metric('cyc_a', 'SELECT * FROM {metric:cyc_b} b');
SELECT rvbbit.define_metric('cyc_b', 'SELECT * FROM {metric:cyc_a} a');
\set ON_ERROR_STOP off
SELECT 'E1 expect cycle error below:' AS check;
SELECT rvbbit.metric_sql('cyc_a');
SELECT 'E2 expect missing-metric error below:' AS check;
SELECT rvbbit.metric_sql('does_not_exist');
\set ON_ERROR_STOP on

-- cleanup
DELETE FROM rvbbit.metric_defs
 WHERE name IN ('total_sales','base_rows','big_sales','cyc_a','cyc_b');
DROP TABLE mt_sales;
SELECT 'metrics-verify complete' AS done;
