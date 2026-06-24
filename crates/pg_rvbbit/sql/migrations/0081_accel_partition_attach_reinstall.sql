-- If a table is accelerated before it is attached as a partition, it has the
-- standalone statement-level dirty triggers. Parent-routed DML does not fire
-- those child statement triggers, so reinstall child triggers after ALTER TABLE
-- touches a partitioned parent.

CREATE OR REPLACE FUNCTION rvbbit.reinstall_partition_dirty_triggers_on_alter()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    cmd record;
    rec record;
BEGIN
    FOR cmd IN
        SELECT *
        FROM pg_event_trigger_ddl_commands()
        WHERE command_tag = 'ALTER TABLE'
          AND classid = 'pg_class'::regclass
    LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_class c
            WHERE c.oid = cmd.objid
              AND c.relkind = 'p'
        ) THEN
            FOR rec IN
                SELECT t.table_oid::regclass AS reloid
                FROM pg_partition_tree(cmd.objid) p
                JOIN rvbbit.tables t ON t.table_oid = p.relid
                WHERE p.relid <> cmd.objid
                  AND t.shadow_heap_retained
            LOOP
                PERFORM rvbbit.install_shadow_heap_dirty_triggers(rec.reloid);
            END LOOP;
        END IF;
    END LOOP;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_event_trigger
        WHERE evtname = 'rvbbit_partition_dirty_triggers_on_alter'
    ) THEN
        CREATE EVENT TRIGGER rvbbit_partition_dirty_triggers_on_alter
            ON ddl_command_end
            WHEN TAG IN ('ALTER TABLE')
            EXECUTE FUNCTION rvbbit.reinstall_partition_dirty_triggers_on_alter();
    END IF;
END;
$$;
