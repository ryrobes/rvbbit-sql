-- Step 1 verification: the snapshot visibility floor.
-- Loads a table as two FULL snapshots (trunc+load, the sync workflow's shape),
-- with row 2 UPDATED, row 3 DELETED, row 4 ADDED in the second snapshot.
-- Expected: latest view = snapshot B only; AS OF generation 1 = snapshot A
-- (rewind through the update + delete). Heap kept (keep_heap => true) as the
-- gold-source fallback.
\set ON_ERROR_STOP on
\pset pager off

DROP TABLE IF EXISTS floor_test;
CREATE TABLE floor_test (id int, label text) USING rvbbit;

-- ── Snapshot A (generation 1) ──────────────────────────────────────────
INSERT INTO floor_test VALUES (1, 'A1'), (2, 'A2'), (3, 'A3');
SELECT 'compact A' AS step, * FROM rvbbit.compact('floor_test', keep_heap => true);
SELECT 'set_floor A -> ' || rvbbit.set_visible_floor('floor_test') AS step;

SELECT '--- latest after A (expect 1=A1,2=A2,3=A3) ---' AS marker;
SELECT id, label FROM floor_test ORDER BY id;

-- ── Snapshot B (generation 2): update 2, delete 3, add 4 ───────────────
TRUNCATE floor_test;
INSERT INTO floor_test VALUES (1, 'A1'), (2, 'B2'), (4, 'A4');
SELECT 'compact B' AS step, * FROM rvbbit.compact('floor_test', keep_heap => true);
SELECT 'set_floor B -> ' || rvbbit.set_visible_floor('floor_test') AS step;

SELECT '--- latest after B (expect 1=A1,2=B2,4=A4; NO row 3) ---' AS marker;
SELECT id, label FROM floor_test ORDER BY id;

-- ── Time-travel: rewind to generation 1 (snapshot A) ───────────────────
SET rvbbit.as_of_generation = 1;
SELECT '--- AS OF gen 1 (expect 1=A1,2=A2,3=A3; NO row 4; row 2 = A2) ---' AS marker;
SELECT id, label FROM floor_test ORDER BY id;
RESET rvbbit.as_of_generation;

-- ── Floor state + generation timeline ──────────────────────────────────
SELECT '--- floor + generations ---' AS marker;
SELECT min_visible_generation FROM rvbbit.tables WHERE table_oid = 'floor_test'::regclass;
SELECT generation, n_rows, n_row_groups FROM rvbbit.generations
WHERE table_oid = 'floor_test'::regclass ORDER BY generation;

DROP TABLE floor_test;
