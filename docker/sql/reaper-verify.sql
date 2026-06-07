-- Step 4 verification: retention reaper. Three snapshots; backdate gens 1-2 to
-- 40 days; reap with a 30-day window. Expect gens 1-2 reaped (rows + files),
-- gen 3 (the live floor) kept, latest intact, AS OF into a reaped gen empty.
\set ON_ERROR_STOP on
\pset pager off

DROP TABLE IF EXISTS reap_t;
CREATE TABLE reap_t (id int, v text) USING rvbbit;
SELECT 1 FROM rvbbit.snapshot_load('reap_t', $q$SELECT * FROM (VALUES (1,'g1')) x(id,v)$q$);  -- gen 1
SELECT 1 FROM rvbbit.snapshot_load('reap_t', $q$SELECT * FROM (VALUES (1,'g2')) x(id,v)$q$);  -- gen 2
SELECT 1 FROM rvbbit.snapshot_load('reap_t', $q$SELECT * FROM (VALUES (1,'g3')) x(id,v)$q$);  -- gen 3 (floor)

-- Age gens 1-2 past the retention window.
UPDATE rvbbit.generations SET committed_at = now() - interval '40 days'
WHERE table_oid='reap_t'::regclass AND generation IN (1,2);

SELECT 'reap result (want gens=2, rgs=2, files=2)' AS m, * FROM rvbbit.reap_generations('reap_t'::regclass, 30);
SELECT 'gens after (want only 3)' AS m, coalesce(string_agg(generation::text,',' ORDER BY generation),'<none>') AS got FROM rvbbit.generations WHERE table_oid='reap_t'::regclass;
SELECT 'row_groups after (want only gen 3)' AS m, coalesce(string_agg(generation::text,',' ORDER BY generation),'<none>') AS got FROM rvbbit.row_groups WHERE table_oid='reap_t'::regclass;
SELECT 'latest still works (want g3)' AS m, string_agg(v,',') AS got FROM reap_t;
SET rvbbit.as_of_generation = 1;
SELECT 'asof 1 reaped (want <empty>)' AS m, coalesce(string_agg(v,','),'<empty>') AS got FROM reap_t;
RESET rvbbit.as_of_generation;

-- Floor protection: even if the live snapshot is old, it must NOT be reaped.
UPDATE rvbbit.generations SET committed_at = now() - interval '40 days'
WHERE table_oid='reap_t'::regclass AND generation = 3;
SELECT 'reap again (floor is old; want 0 rows returned)' AS m, count(*) AS got FROM rvbbit.reap_generations('reap_t'::regclass, 30);
SELECT 'floor survived (want g3)' AS m, string_agg(v,',') AS got FROM reap_t;
DROP TABLE reap_t;
