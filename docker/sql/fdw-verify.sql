-- Step 3 verification: FDW source substrate + snapshot_load end-to-end.
-- Prereq: a source database `src_demo` on the SAME server with:
--   CREATE TABLE customers (id int primary key, name text, tier text);
--   INSERT INTO customers VALUES (1,'Acme','gold'),(2,'Globex','silver'),(3,'Initech','bronze');
-- Run this against the rvbbit (dest) database.
\set ON_ERROR_STOP on
\pset pager off

SELECT rvbbit.fdw_setup_server('demo_src','localhost',5432,'src_demo','postgres','rvbbit') AS setup;
SELECT rvbbit.fdw_import('demo_src','public','demo_fdw',ARRAY['customers']) AS imported;
SELECT 'fdw read' AS step, string_agg(id||':'||name||'/'||tier, ', ' ORDER BY id) FROM demo_fdw.customers;

DROP TABLE IF EXISTS cust_mirror;
CREATE TABLE cust_mirror (id int, name text, tier text) USING rvbbit;
SELECT * FROM rvbbit.snapshot_load('cust_mirror','SELECT id,name,tier FROM demo_fdw.customers');  -- gen 1
SELECT 'mirror latest' AS step, string_agg(id||':'||tier,',' ORDER BY id) FROM cust_mirror;

-- (Mutate src_demo.customers between snapshots, then re-import + re-load:)
--   UPDATE customers SET tier='platinum' WHERE id=1; DELETE FROM customers WHERE id=3; INSERT INTO customers VALUES (4,'Umbrella','gold');
-- SELECT rvbbit.fdw_import('demo_src','public','demo_fdw',ARRAY['customers']);
-- SELECT * FROM rvbbit.snapshot_load('cust_mirror','SELECT id,name,tier FROM demo_fdw.customers');  -- gen 2
-- latest => 1=platinum,2=silver,4=gold (no 3);  SET rvbbit.as_of_generation=1 => 1=gold,2=silver,3=bronze
DROP TABLE cust_mirror;
