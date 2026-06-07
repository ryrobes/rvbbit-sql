-- Step 6 verification: the run_sync executor, end to end.
-- Prereq: source db `src_demo` on the same server with:
--   customers(id int pk, name text, tier text) + orders(order_id int pk, customer_id int, amount numeric)
-- Run against the rvbbit (dest) database.
\set ON_ERROR_STOP on
\pset pager off

DROP SCHEMA IF EXISTS mirror CASCADE;
DELETE FROM rvbbit.sync_jobs WHERE job_name='demo';
DELETE FROM rvbbit.sync_runs WHERE job_name='demo';

INSERT INTO rvbbit.sync_jobs(job_name, spec) VALUES ('demo', $j$
{
  "server": {"name":"demo_src","host":"localhost","port":5432,"dbname":"src_demo","user":"postgres","password":"rvbbit","fetch_size":10000},
  "remote_schema":"public", "fdw_schema":"demo_fdw", "dest_schema":"mirror",
  "tables":["customers","orders"]
}$j$::jsonb);

CALL rvbbit.run_sync('demo');

-- Per-table observability (written inside the loop => visible mid-run).
SELECT source_table, action, generation, rows_loaded, left(error,60) AS error
FROM rvbbit.sync_runs WHERE job_name='demo' ORDER BY source_table;
-- orders.amount (numeric in source) is coerced to double precision in the mirror.
SELECT 'orders amount type' AS check, format_type(atttypid,atttypmod) AS got
FROM pg_attribute WHERE attrelid='mirror.orders'::regclass AND attname='amount';

-- Mutate source + re-run, then time-travel: latest vs AS OF gen 1.
--   (in src_demo) UPDATE customers SET tier='diamond' WHERE id=1; INSERT INTO orders VALUES (104,1,7.77);
--   CALL rvbbit.run_sync('demo');
--   latest customers => 1=diamond ;  SET rvbbit.as_of_generation=1 => 1=<prior tier>
