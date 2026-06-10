-- mvcc-01 regression verify (merge-on-read tombstones).
--
-- delete_log tombstones are keyed by the ABSOLUTE per-row-group ordinal, but
-- the native custom scan reads parquet in READ_BATCH_SIZE (65536)-row Arrow
-- batches with row_in_batch resetting to 0 each batch. The pre-fix scan tested
-- the per-batch index against the bitmap, so a tombstone past the first batch
-- (ordinal >= 65536) hid ZERO rows (resurrection) and a low ordinal also
-- dropped the live row exactly 65536 ordinals later (collateral delete).
--
-- Must run against a LIVE instance (parquet authoritative after a committed
-- compaction); the single-transaction pg_test harness can't publish row groups.
--   docker exec -i rvbbit-pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 \
--     < docker/sql/mvcc01-tombstone-verify.sql
\set ON_ERROR_STOP on

-- ---- Resurrection: tombstone an ordinal in the SECOND read batch (>= 65536) ----
DROP TABLE IF EXISTS mvcc01_verify;
CREATE TABLE mvcc01_verify (id int, v int) USING rvbbit;
INSERT INTO mvcc01_verify SELECT g, g FROM generate_series(0, 99999) g;
SELECT rvbbit.refresh_acceleration('mvcc01_verify'::regclass, false);
SET rvbbit.route_force_candidate = 'rvbbit_native';
SELECT rvbbit.tombstone_batch('mvcc01_verify'::regclass, '[{"rg":0,"ord":70000}]'::jsonb);
DO $$
DECLARE c bigint; resurrected bigint;
BEGIN
  -- OFFSET 0 barrier defeats the count metadata shortcut so the scan emits rows.
  SELECT count(*) INTO c FROM (SELECT id FROM mvcc01_verify OFFSET 0) s;
  IF c <> 99999 THEN
    RAISE EXCEPTION 'mvcc-01 FAIL (resurrection): expected 99999 rows after tombstoning ordinal 70000, got %', c;
  END IF;
  SELECT count(*) INTO resurrected FROM (SELECT id FROM mvcc01_verify WHERE id = 70000 OFFSET 0) s;
  IF resurrected <> 0 THEN
    RAISE EXCEPTION 'mvcc-01 FAIL: tombstoned row id=70000 is still visible';
  END IF;
END $$;

-- ---- Collateral: a low ordinal must NOT also drop the row 65536 ordinals later ----
DROP TABLE IF EXISTS mvcc01_verify2;
CREATE TABLE mvcc01_verify2 (id int, v int) USING rvbbit;
INSERT INTO mvcc01_verify2 SELECT g, g FROM generate_series(0, 99999) g;
SELECT rvbbit.refresh_acceleration('mvcc01_verify2'::regclass, false);
SET rvbbit.route_force_candidate = 'rvbbit_native';
SELECT rvbbit.tombstone_batch('mvcc01_verify2'::regclass, '[{"rg":0,"ord":4000}]'::jsonb);
DO $$
DECLARE c bigint; phantom bigint;
BEGIN
  SELECT count(*) INTO c FROM (SELECT id FROM mvcc01_verify2 OFFSET 0) s;
  IF c <> 99999 THEN
    RAISE EXCEPTION 'mvcc-01 FAIL (collateral): expected 99999 after tombstoning ordinal 4000, got % (phantom id 69536 likely also dropped)', c;
  END IF;
  SELECT count(*) INTO phantom FROM (SELECT id FROM mvcc01_verify2 WHERE id = 69536 OFFSET 0) s;
  IF phantom <> 1 THEN
    RAISE EXCEPTION 'mvcc-01 FAIL: phantom row id=69536 was wrongly dropped';
  END IF;
END $$;

-- ---- Write path: single-row rvbbit.tombstone() must not raise ambiguous-column ----
DROP TABLE IF EXISTS mvcc01_verify3;
CREATE TABLE mvcc01_verify3 (id int) USING rvbbit;
INSERT INTO mvcc01_verify3 SELECT g FROM generate_series(0, 9) g;
SELECT rvbbit.refresh_acceleration('mvcc01_verify3'::regclass, false);
SELECT rvbbit.tombstone('mvcc01_verify3'::regclass, 0::bigint, 3) AS single_tombstone_generation;

DROP TABLE mvcc01_verify;
DROP TABLE mvcc01_verify2;
DROP TABLE mvcc01_verify3;
\echo 'mvcc-01 tombstone verify: PASS'
