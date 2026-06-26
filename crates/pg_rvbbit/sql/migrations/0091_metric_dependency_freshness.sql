-- 0091_metric_dependency_freshness
--
-- A metric can materialize perfectly and still be operationally stale if its
-- dependency tables stopped receiving source data. Surface that separately from
-- accelerator freshness: this checks common source freshness columns on each
-- table referenced by a metric definition.

CREATE OR REPLACE FUNCTION rvbbit.metric_dependency_freshness(
    p_metrics     text[] DEFAULT NULL,
    p_stale_after interval DEFAULT interval '2 days'
) RETURNS TABLE(
    metric_name      text,
    table_schema     text,
    table_name       text,
    freshness_column text,
    max_freshness    timestamptz,
    age              interval,
    stale            boolean
) LANGUAGE plpgsql AS $fn$
DECLARE
    rec record;
    v_col text;
    v_max timestamptz;
BEGIN
    FOR rec IN
        SELECT d.metric_name, d.table_schema, d.table_name
        FROM rvbbit.metric_dependencies d
        WHERE p_metrics IS NULL OR d.metric_name = ANY(p_metrics)
        ORDER BY d.metric_name, d.table_schema, d.table_name
    LOOP
        SELECT c.column_name
          INTO v_col
        FROM information_schema.columns c
        WHERE c.table_schema = rec.table_schema
          AND c.table_name = rec.table_name
          AND c.column_name IN (
              'last_refreshed_at',
              '_fivetran_synced',
              'synced_at',
              'updated_at',
              'last_modified_date',
              'modified_at',
              'created_at'
          )
        ORDER BY array_position(ARRAY[
              'last_refreshed_at',
              '_fivetran_synced',
              'synced_at',
              'updated_at',
              'last_modified_date',
              'modified_at',
              'created_at'
          ]::text[], c.column_name)
        LIMIT 1;

        v_max := NULL;
        IF v_col IS NOT NULL THEN
            BEGIN
                EXECUTE format(
                    'SELECT max(%I)::timestamptz FROM %I.%I',
                    v_col,
                    rec.table_schema,
                    rec.table_name
                )
                INTO v_max;
            EXCEPTION WHEN OTHERS THEN
                v_max := NULL;
            END;
        END IF;

        metric_name := rec.metric_name;
        table_schema := rec.table_schema;
        table_name := rec.table_name;
        freshness_column := v_col;
        max_freshness := v_max;
        age := CASE WHEN v_max IS NULL THEN NULL ELSE now() - v_max END;
        stale := CASE
            WHEN v_col IS NULL OR v_max IS NULL THEN NULL
            ELSE now() - v_max > p_stale_after
        END;
        RETURN NEXT;
    END LOOP;
END;
$fn$;
