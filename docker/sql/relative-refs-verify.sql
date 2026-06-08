-- Relative-time metric refs ({metric:NAME.OFFSET} / {metric:self.OFFSET}).
--   * a rolling THRESHOLD check (self at a prior snapshot)
--   * a DELTA in a metric body (self-ref, stripped to break recursion)
--   * a cross-metric ref (the recommended pattern)
\set ON_ERROR_STOP on
\pset pager off

SELECT '-1day'::text, rvbbit._parse_offset('-1day')::text;          -- -1 day
SELECT '-12hours'::text, rvbbit._parse_offset('-12hours')::text;    -- -12:00:00
SELECT 'yesterday'::text, rvbbit._parse_offset('yesterday')::text;  -- -1 day

DELETE FROM rvbbit.metric_defs WHERE name IN ('growth','growth_delta','rev_delta');
DROP TABLE IF EXISTS roll_demo;
CREATE TABLE roll_demo (x int) USING rvbbit;
SELECT count(*) FROM rvbbit.snapshot_load('roll_demo', $q$SELECT * FROM (VALUES (40),(60)) v(x)$q$);     -- gen1 total 100 @ T1
SELECT pg_sleep(3.0);
SELECT count(*) FROM rvbbit.snapshot_load('roll_demo', $q$SELECT * FROM (VALUES (40),(60),(150)) v(x)$q$); -- gen2 total 250 @ T2=T1+3s
SELECT committed_at AS t2 FROM rvbbit.generations WHERE table_oid='roll_demo'::regclass AND generation=2 \gset

-- self-ref in a CHECK: "must not shrink vs the prior snapshot"
SELECT rvbbit.define_metric('growth',
  'SELECT sum(x) AS total FROM roll_demo', '{}'::jsonb, 'all','Must not shrink','analytics','{}'::jsonb,
  'SELECT total >= {metric:self.-2seconds} AS ok, total AS value, {metric:self.-2seconds}::numeric AS prev FROM metric');
SELECT 'C1 self-ref CHECK (want ok=true,value=250,prev=100)' AS check,
       rvbbit.check_metric('growth','{}'::jsonb, now(), :'t2')::text AS got;

-- self-ref in a metric BODY (delta)
SELECT rvbbit.define_metric('growth_delta',
  'SELECT sum(x) AS total, sum(x) - {metric:self.-2seconds} AS delta FROM roll_demo', '{}'::jsonb, 'all','Delta','analytics');
SELECT 'C2 self-ref BODY (want total=250,delta=150)' AS check,
       rvbbit.metric('growth_delta','{}'::jsonb, now(), :'t2')::text AS got;

-- cross-metric ref (recommended): a delta metric over a base metric
SELECT rvbbit.define_metric('rev_delta',
  'SELECT total, total - {metric:growth.-2seconds} AS delta FROM {metric:growth} g', '{}'::jsonb, 'all','Delta','analytics');
SELECT 'C3 OTHER-metric ref (want total=250,delta=150)' AS check,
       rvbbit.metric('rev_delta','{}'::jsonb, now(), :'t2')::text AS got;

DELETE FROM rvbbit.metric_defs WHERE name IN ('growth','growth_delta','rev_delta');
DELETE FROM rvbbit.metric_materialize WHERE metric_name IN ('growth','growth_delta','rev_delta');
DELETE FROM rvbbit.metric_dependencies WHERE metric_name IN ('growth','growth_delta','rev_delta');
DROP TABLE roll_demo;
SELECT 'relative-refs-verify complete' AS done;
