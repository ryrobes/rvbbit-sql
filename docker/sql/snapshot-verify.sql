-- Step 2 re-verify: snapshot AS OF must be EXACT (= G), not cumulative (<= G).
\set ON_ERROR_STOP on
\pset pager off

-- ════════ SNAPSHOT table (min_visible_generation > 0) ════════
DROP TABLE IF EXISTS snap_t;
CREATE TABLE snap_t (id int, label text) USING rvbbit;

SELECT * FROM rvbbit.snapshot_load('snap_t', $q$SELECT * FROM (VALUES (1,'A1'),(2,'A2'),(3,'A3')) v(id,label)$q$);  -- gen 1
SELECT * FROM rvbbit.snapshot_load('snap_t', $q$SELECT * FROM (VALUES (1,'A1'),(2,'B2'),(4,'A4')) v(id,label)$q$);  -- gen 2 (upd 2, del 3, add 4)
SELECT * FROM rvbbit.snapshot_load('snap_t', $q$SELECT 1::int, 'x'::text WHERE false$q$);                          -- gen 3 (empty)

SELECT 'latest (want 1=A1,2=B2,4=A4? NO — gen3 empty => EMPTY)' AS check, coalesce(string_agg(id||'='||label,',' ORDER BY id),'<empty>') AS got FROM snap_t;
SELECT * FROM rvbbit.snapshot_load('snap_t', $q$SELECT * FROM (VALUES (5,'C5'),(6,'C6')) v(id,label)$q$);          -- gen 4
SELECT 'latest after gen4 (want 5=C5,6=C6)' AS check, string_agg(id||'='||label,',' ORDER BY id) AS got FROM snap_t;

SET rvbbit.as_of_generation = 1;
SELECT 'asof 1 (want 1=A1,2=A2,3=A3 EXACT)' AS check, string_agg(id||'='||label,',' ORDER BY id) AS got FROM snap_t;
SET rvbbit.as_of_generation = 2;
SELECT 'asof 2 (want 1=A1,2=B2,4=A4 EXACT — not union w/ gen1)' AS check, string_agg(id||'='||label,',' ORDER BY id) AS got FROM snap_t;
SET rvbbit.as_of_generation = 3;
SELECT 'asof 3 (want <empty>)' AS check, coalesce(string_agg(id||'='||label,',' ORDER BY id),'<empty>') AS got FROM snap_t;
RESET rvbbit.as_of_generation;

SELECT 'gens' AS check, string_agg(generation||':'||n_rows, ' ' ORDER BY generation) AS got FROM rvbbit.generations WHERE table_oid='snap_t'::regclass;
DROP TABLE snap_t;

-- ════════ APPEND table (floor = 0): predicate change must be a NO-OP ════════
DROP TABLE IF EXISTS app_t;
CREATE TABLE app_t (id int, label text) USING rvbbit;
INSERT INTO app_t VALUES (1,'a1'),(2,'a2');
SELECT rvbbit.compact('app_t');                       -- gen 1 (no floor set => append mode)
INSERT INTO app_t VALUES (3,'a3');
SELECT rvbbit.compact('app_t');                       -- gen 2 (delta)
SELECT 'append latest (want 1,2,3 cumulative)' AS check, string_agg(id||'='||label,',' ORDER BY id) AS got FROM app_t;
SET rvbbit.as_of_generation = 1;
SELECT 'append asof 1 (want a1,a2 cumulative <= 1)' AS check, string_agg(id||'='||label,',' ORDER BY id) AS got FROM app_t;
RESET rvbbit.as_of_generation;
SELECT 'append floor (want 0)' AS check, min_visible_generation::text AS got FROM rvbbit.tables WHERE table_oid='app_t'::regclass;
DROP TABLE app_t;
