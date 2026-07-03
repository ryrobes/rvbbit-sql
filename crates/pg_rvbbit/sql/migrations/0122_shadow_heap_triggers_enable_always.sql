-- 0122_shadow_heap_triggers_enable_always
--
-- Pre-release audit fix (2026-07-03): the shadow-heap dirty/tombstone triggers
-- were created as ordinary (ORIGIN) triggers, which do NOT fire when
-- session_replication_role = 'replica' (logical-replication apply worker,
-- pg_restore --disable-triggers, some ETL). Heap writes in that mode bypass
-- dirty tracking and the accelerator goes silently stale. Redefine the
-- installer to ENABLE ALWAYS every trigger it creates, and backfill all
-- already-installed shadow-heap triggers.

CREATE OR REPLACE FUNCTION rvbbit.install_shadow_heap_dirty_triggers(reloid regclass)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
DECLARE
    identity_mode text := rvbbit.accel_identity_mode(reloid);
    is_partition boolean := false;
    trg record;
BEGIN
    SELECT coalesce(c.relispartition, false)
      INTO is_partition
      FROM pg_class c
     WHERE c.oid = reloid;

    PERFORM rvbbit.drop_shadow_heap_dirty_triggers(reloid);

    IF is_partition THEN
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_row_insert
                 AFTER INSERT ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty_row()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_row_update
                 AFTER UPDATE ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty_row()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_row_delete
                 AFTER DELETE ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty_row()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_truncate
                 AFTER TRUNCATE ON %s
                 FOR EACH STATEMENT
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );

        FOR trg IN
            SELECT tgname FROM pg_trigger
            WHERE tgrelid = reloid AND NOT tgisinternal
              AND starts_with(tgname, 'rvbbit_shadow_heap_')
        LOOP
            EXECUTE format('ALTER TABLE %s ENABLE ALWAYS TRIGGER %I', reloid, trg.tgname);
        END LOOP;
        RETURN;
    END IF;

    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty_insert
             AFTER INSERT ON %s
             FOR EACH STATEMENT
             EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );
    IF identity_mode = 'primary_key' THEN
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_update
                 AFTER UPDATE ON %s
                 REFERENCING OLD TABLE AS rvbbit_old_rows
                 FOR EACH STATEMENT
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_delete
                 AFTER DELETE ON %s
                 REFERENCING OLD TABLE AS rvbbit_old_rows
                 FOR EACH STATEMENT
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
    ELSE
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_update
                 AFTER UPDATE ON %s
                 FOR EACH STATEMENT
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_delete
                 AFTER DELETE ON %s
                 FOR EACH STATEMENT
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_ctid_update
                 AFTER UPDATE ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_ctid_tombstone()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_ctid_delete
                 AFTER DELETE ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_ctid_tombstone()',
            reloid
        );
    END IF;
    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty_truncate
             AFTER TRUNCATE ON %s
             FOR EACH STATEMENT
             EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );
    FOR trg IN
        SELECT tgname FROM pg_trigger
        WHERE tgrelid = reloid AND NOT tgisinternal
          AND starts_with(tgname, 'rvbbit_shadow_heap_')
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ALWAYS TRIGGER %I', reloid, trg.tgname);
    END LOOP;
END;
$function$;

-- Backfill: enable-always every shadow-heap trigger already installed.
DO $mig$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT t.tgrelid::regclass AS rel, t.tgname
        FROM pg_trigger t
        WHERE NOT t.tgisinternal
          AND starts_with(t.tgname, 'rvbbit_shadow_heap_')
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ALWAYS TRIGGER %I', r.rel, r.tgname);
    END LOOP;
END
$mig$;
