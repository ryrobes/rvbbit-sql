-- Keep snapshot-loaded tables visible after a major accelerator fold.
--
-- Snapshot tables use rvbbit.tables.min_visible_generation as an exact latest
-- generation pointer. rebuild_acceleration writes a new compacted generation
-- and removes the old row groups; if the pointer stays on the removed
-- generation, the latest parquet scan can see zero row groups even though the
-- new parquet file exists.

CREATE OR REPLACE FUNCTION rvbbit._accel_fold_snapshot_floor_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.operation = 'rebuild_acceleration'
       AND NEW.status = 'ok'
       AND NEW.generation_after IS NOT NULL
       AND NEW.generation_after > 0 THEN
        UPDATE rvbbit.tables
           SET min_visible_generation = NEW.generation_after
         WHERE table_oid = NEW.table_oid
           AND min_visible_generation > 0
           AND min_visible_generation <> NEW.generation_after;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS accel_fold_snapshot_floor_guard ON rvbbit.acceleration_operations;
CREATE TRIGGER accel_fold_snapshot_floor_guard
AFTER INSERT OR UPDATE OF status, generation_after
ON rvbbit.acceleration_operations
FOR EACH ROW
EXECUTE FUNCTION rvbbit._accel_fold_snapshot_floor_guard();

-- Repair any already-stranded snapshot table: floor points at a generation
-- that no longer has row groups and is not an intentional empty snapshot.
WITH stranded AS (
    SELECT t.table_oid, max(rg.generation)::bigint AS latest_generation
    FROM rvbbit.tables t
    JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
    LEFT JOIN rvbbit.generations floor_gen
      ON floor_gen.table_oid = t.table_oid
     AND floor_gen.generation = t.min_visible_generation
    WHERE t.min_visible_generation > 0
      AND NOT EXISTS (
          SELECT 1
          FROM rvbbit.row_groups floor_rg
          WHERE floor_rg.table_oid = t.table_oid
            AND floor_rg.generation = t.min_visible_generation
      )
      AND coalesce(floor_gen.n_rows, 1) > 0
    GROUP BY t.table_oid
)
UPDATE rvbbit.tables t
   SET min_visible_generation = stranded.latest_generation
  FROM stranded
 WHERE t.table_oid = stranded.table_oid
   AND stranded.latest_generation > 0;
