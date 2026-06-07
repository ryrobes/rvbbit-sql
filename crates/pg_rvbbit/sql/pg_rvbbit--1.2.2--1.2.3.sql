-- pg_rvbbit 1.2.2 -> 1.2.3
--
-- rvbbit.snapshot_load(dest, source_query): the gap-free trunc+load primitive
-- behind the Postgres->rvbbit table-sync workflow. Pure SQL on top of the
-- existing two-arg compact() + the 1.2.2 visibility floor, so no .so change.

CREATE OR REPLACE FUNCTION rvbbit.snapshot_load(dest regclass, source_query text)
RETURNS TABLE (generation bigint, rows_loaded bigint, action text)
LANGUAGE plpgsql
AS $$
DECLARE
    g bigint;
    n bigint;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(dest) THEN
        RAISE EXCEPTION '% is not an rvbbit table', dest;
    END IF;

    EXECUTE format('TRUNCATE TABLE %s', dest);
    EXECUTE format('INSERT INTO %s %s', dest, source_query);

    PERFORM rvbbit.compact(dest, keep_heap => true);

    SELECT t.next_generation - 1 INTO g FROM rvbbit.tables t WHERE t.table_oid = dest;

    SELECT count(*) INTO n
    FROM rvbbit.generations gg
    WHERE gg.table_oid = dest AND gg.generation = g;
    IF n = 0 THEN
        INSERT INTO rvbbit.generations (table_oid, generation, n_rows, n_row_groups)
        VALUES (dest, g, 0, 0);
    END IF;

    PERFORM rvbbit.set_visible_floor(dest, g);

    SELECT gg.n_rows INTO n
    FROM rvbbit.generations gg
    WHERE gg.table_oid = dest AND gg.generation = g;

    RETURN QUERY
        SELECT g,
               coalesce(n, 0),
               CASE WHEN coalesce(n, 0) = 0 THEN 'empty' ELSE 'snapshot' END;
END;
$$;
