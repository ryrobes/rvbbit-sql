-- pg_rvbbit 1.2.1 -> 1.2.2
--
-- Snapshot visibility floor (min_visible_generation) — the read-side primitive
-- behind the Postgres->rvbbit table-sync ("snapshot-load") workflow. The
-- read-path predicates that consume this column live in the Rust engine
-- (custom_scan + df) and ship with the new .so; this migration carries the
-- catalog column and the SQL setter for already-installed databases. Fresh
-- installs get both from pg_rvbbit--1.2.2.sql.
--
-- The needed catalog indexes (row_groups(table_oid, generation),
-- generations(table_oid, generation) PK, generations(table_oid, committed_at))
-- already exist as of 1.0.0 — no index DDL here.

ALTER TABLE rvbbit.tables
    ADD COLUMN IF NOT EXISTS min_visible_generation bigint NOT NULL DEFAULT 0;

-- Set the snapshot visibility floor for a table: the "latest" (non-AS-OF) view
-- shows only row groups at generation >= the floor, hiding older retained
-- snapshots. gen => NULL floors to the current newest generation that has row
-- groups; pass an explicit generation for the empty-snapshot (0-row) case.
-- AS OF reads ignore the floor and continue to read full history.
CREATE OR REPLACE FUNCTION rvbbit.set_visible_floor(rel regclass, gen bigint DEFAULT NULL)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    target bigint;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(rel) THEN
        RAISE EXCEPTION '% is not an rvbbit table', rel;
    END IF;
    IF gen IS NULL THEN
        SELECT coalesce(max(rg.generation), 0)
        INTO target
        FROM rvbbit.row_groups rg
        WHERE rg.table_oid = rel;
    ELSE
        target := gen;
    END IF;
    UPDATE rvbbit.tables SET min_visible_generation = target WHERE table_oid = rel;
    RETURN target;
END;
$$;
