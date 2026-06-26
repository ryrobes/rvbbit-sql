-- Pivot acceleration identity from the rvbbit table access method to the
-- rvbbit.tables registry. USING rvbbit remains accepted, but the DDL trigger
-- immediately normalizes new regular tables back to heap after registration so
-- extension uninstall does not leave user tables access-method-bound.

ALTER TABLE rvbbit.tables
    ADD COLUMN IF NOT EXISTS acceleration_enabled boolean NOT NULL DEFAULT true;

CREATE OR REPLACE VIEW rvbbit.table_dirty_state AS
SELECT
    t.table_oid,
    coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
    (coalesce(t.shadow_heap_dirty, false) OR coalesce(dm.has_marker, false))
        AS shadow_heap_dirty,
    (coalesce(t.dirty_has_insert, false) OR coalesce(dm.has_insert, false))
        AS dirty_has_insert,
    (coalesce(t.dirty_has_update, false) OR coalesce(dm.has_update, false))
        AS dirty_has_update,
    (coalesce(t.dirty_has_delete, false) OR coalesce(dm.has_delete, false))
        AS dirty_has_delete,
    (coalesce(t.dirty_has_truncate, false) OR coalesce(dm.has_truncate, false))
        AS dirty_has_truncate,
    CASE
        WHEN NOT (coalesce(t.shadow_heap_dirty, false) OR coalesce(dm.has_marker, false))
            THEN NULL
        WHEN t.dirty_since IS NULL THEN dm.dirty_since
        WHEN dm.dirty_since IS NULL THEN t.dirty_since
        ELSE least(t.dirty_since, dm.dirty_since)
    END AS dirty_since,
    CASE
        WHEN t.last_write_at IS NULL THEN dm.last_write_at
        WHEN dm.last_write_at IS NULL THEN t.last_write_at
        ELSE greatest(t.last_write_at, dm.last_write_at)
    END AS last_write_at
FROM rvbbit.tables t
LEFT JOIN LATERAL (
    SELECT count(*) > 0 AS has_marker,
           bool_or(m.dirty_op = 'I') AS has_insert,
           bool_or(m.dirty_op = 'U') AS has_update,
           bool_or(m.dirty_op = 'D') AS has_delete,
           bool_or(m.dirty_op = 'T') AS has_truncate,
           min(m.marked_at) AS dirty_since,
           max(m.marked_at) AS last_write_at
      FROM rvbbit.table_dirty_markers m
     WHERE m.table_oid = t.table_oid
) dm ON true
WHERE coalesce(t.acceleration_enabled, true);

CREATE OR REPLACE FUNCTION rvbbit.drop_shadow_heap_dirty_triggers(reloid regclass)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    trigger_name text;
BEGIN
    FOREACH trigger_name IN ARRAY ARRAY[
        'rvbbit_shadow_heap_dirty',
        'rvbbit_shadow_heap_dirty_insert',
        'rvbbit_shadow_heap_dirty_update',
        'rvbbit_shadow_heap_dirty_delete',
        'rvbbit_shadow_heap_dirty_truncate',
        'rvbbit_shadow_heap_ctid_update',
        'rvbbit_shadow_heap_ctid_delete',
        'rvbbit_shadow_heap_dirty_row_insert',
        'rvbbit_shadow_heap_dirty_row_update',
        'rvbbit_shadow_heap_dirty_row_delete'
    ]
    LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_trigger
            WHERE tgrelid = reloid
              AND tgname = trigger_name
              AND NOT tgisinternal
        ) THEN
            EXECUTE format('DROP TRIGGER %I ON %s', trigger_name, reloid);
        END IF;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.install_shadow_heap_dirty_triggers(reloid regclass)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    identity_mode text := rvbbit.accel_identity_mode(reloid);
    is_partition boolean := false;
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
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.is_rvbbit_table(rel regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM rvbbit.tables t
        JOIN pg_class c ON c.oid = t.table_oid
        WHERE t.table_oid = rel
          AND coalesce(t.acceleration_enabled, true)
    );
$$;

CREATE OR REPLACE FUNCTION rvbbit.enable_table(
    reloid regclass,
    convert_to_heap boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    rel_kind char;
    am_name text;
    was_registered boolean := false;
    was_enabled boolean := false;
    had_row_groups boolean := false;
    converted boolean := false;
BEGIN
    SELECT c.relkind, a.amname
      INTO rel_kind, am_name
      FROM pg_class c
      LEFT JOIN pg_am a ON a.oid = c.relam
     WHERE c.oid = reloid;

    IF rel_kind IS NULL THEN
        RAISE EXCEPTION 'rvbbit.enable_table: relation % does not exist', reloid;
    END IF;
    IF rel_kind NOT IN ('r', 'p', 'm') THEN
        RAISE EXCEPTION 'rvbbit.enable_table: % has unsupported relkind %', reloid, rel_kind;
    END IF;
    IF rel_kind <> 'p' AND coalesce(am_name, '') NOT IN ('heap', 'rvbbit') THEN
        RAISE EXCEPTION 'rvbbit.enable_table: % uses access method %, expected heap-compatible storage', reloid, am_name;
    END IF;

    SELECT true, coalesce(t.acceleration_enabled, true)
      INTO was_registered, was_enabled
      FROM rvbbit.tables t
     WHERE t.table_oid = reloid;
    was_registered := coalesce(was_registered, false);
    was_enabled := coalesce(was_enabled, false);

    SELECT EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = reloid)
      INTO had_row_groups;

    INSERT INTO rvbbit.tables (table_oid, acceleration_enabled)
    VALUES (reloid, true)
    ON CONFLICT (table_oid) DO UPDATE
       SET acceleration_enabled = true,
           shadow_heap_dirty = CASE
               WHEN NOT coalesce(rvbbit.tables.acceleration_enabled, true)
                    AND EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = reloid)
               THEN true
               ELSE rvbbit.tables.shadow_heap_dirty
           END,
           dirty_has_insert = CASE
               WHEN NOT coalesce(rvbbit.tables.acceleration_enabled, true)
                    AND EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = reloid)
               THEN true
               ELSE rvbbit.tables.dirty_has_insert
           END,
           dirty_has_update = CASE
               WHEN NOT coalesce(rvbbit.tables.acceleration_enabled, true)
                    AND EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = reloid)
               THEN true
               ELSE rvbbit.tables.dirty_has_update
           END,
           dirty_has_delete = CASE
               WHEN NOT coalesce(rvbbit.tables.acceleration_enabled, true)
                    AND EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = reloid)
               THEN true
               ELSE rvbbit.tables.dirty_has_delete
           END,
           dirty_since = CASE
               WHEN NOT coalesce(rvbbit.tables.acceleration_enabled, true)
                    AND EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = reloid)
                    AND rvbbit.tables.dirty_since IS NULL
               THEN clock_timestamp()
               ELSE rvbbit.tables.dirty_since
           END,
           last_write_at = CASE
               WHEN NOT coalesce(rvbbit.tables.acceleration_enabled, true)
                    AND EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = reloid)
               THEN clock_timestamp()
               ELSE rvbbit.tables.last_write_at
           END;

    IF convert_to_heap AND am_name = 'rvbbit' AND rel_kind IN ('r', 'm') THEN
        EXECUTE format('ALTER TABLE %s SET ACCESS METHOD heap', reloid);
        converted := true;
    END IF;

    RETURN jsonb_build_object(
        'status', 'enabled',
        'table', reloid::text,
        'registered_before', was_registered,
        'enabled_before', was_enabled,
        'had_row_groups', had_row_groups,
        'converted_to_heap', converted
    );
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.disable_table(
    reloid regclass,
    convert_to_heap boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    am_name text;
    rel_kind char;
    was_registered boolean := false;
    was_enabled boolean := false;
    converted boolean := false;
BEGIN
    SELECT c.relkind, a.amname
      INTO rel_kind, am_name
      FROM pg_class c
      LEFT JOIN pg_am a ON a.oid = c.relam
     WHERE c.oid = reloid;

    IF rel_kind IS NULL THEN
        RAISE EXCEPTION 'rvbbit.disable_table: relation % does not exist', reloid;
    END IF;

    SELECT true, coalesce(t.acceleration_enabled, true)
      INTO was_registered, was_enabled
      FROM rvbbit.tables t
     WHERE t.table_oid = reloid;
    was_registered := coalesce(was_registered, false);
    was_enabled := coalesce(was_enabled, false);

    UPDATE rvbbit.tables
       SET acceleration_enabled = false,
           shadow_heap_dirty = false,
           dirty_has_insert = false,
           dirty_has_update = false,
           dirty_has_delete = false,
           dirty_has_truncate = false
     WHERE table_oid = reloid;

    PERFORM rvbbit.clear_table_dirty_markers(reloid::oid);
    PERFORM rvbbit.drop_shadow_heap_dirty_triggers(reloid);

    IF convert_to_heap AND am_name = 'rvbbit' AND rel_kind IN ('r', 'm') THEN
        EXECUTE format('ALTER TABLE %s SET ACCESS METHOD heap', reloid);
        converted := true;
    END IF;

    RETURN jsonb_build_object(
        'status', 'disabled',
        'table', reloid::text,
        'registered_before', was_registered,
        'enabled_before', was_enabled,
        'converted_to_heap', converted
    );
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.detach_table(
    reloid regclass,
    convert_to_heap boolean DEFAULT true
) RETURNS jsonb
LANGUAGE sql
AS $$
    -- Compatibility alias. `disable_table` is the user-facing off switch.
    SELECT rvbbit.disable_table(reloid, convert_to_heap)
$$;

CREATE OR REPLACE FUNCTION rvbbit.list_tables()
RETURNS TABLE (table_oid oid, table_name text, n_row_groups bigint, n_deletes bigint)
LANGUAGE sql
STABLE
AS $$
    SELECT
        t.table_oid,
        c.oid::regclass::text,
        (SELECT count(*) FROM rvbbit.row_groups_visible rg WHERE rg.table_oid = t.table_oid),
        rvbbit.visible_tombstone_count(t.table_oid::regclass)
    FROM rvbbit.tables t
    JOIN pg_class c ON c.oid = t.table_oid
    WHERE coalesce(t.acceleration_enabled, true);
$$;

CREATE OR REPLACE FUNCTION rvbbit.on_create_table()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    obj record;
    rvbbit_am_oid oid;
    rel_kind char;
BEGIN
    IF to_regclass('rvbbit.tables') IS NULL THEN
        RETURN;
    END IF;

    SELECT oid INTO rvbbit_am_oid FROM pg_am WHERE amname = 'rvbbit';
    IF rvbbit_am_oid IS NULL THEN
        RETURN;
    END IF;

    FOR obj IN
        SELECT * FROM pg_event_trigger_ddl_commands()
        WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
          AND object_type = 'table'
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_class
            WHERE oid = obj.objid AND relam = rvbbit_am_oid
        ) THEN
            INSERT INTO rvbbit.tables (table_oid, acceleration_enabled)
            VALUES (obj.objid, true)
            ON CONFLICT (table_oid) DO UPDATE
                SET acceleration_enabled = true;

            SELECT relkind INTO rel_kind
            FROM pg_class
            WHERE oid = obj.objid;

            IF rel_kind IN ('r', 'm') THEN
                EXECUTE format('ALTER TABLE %s SET ACCESS METHOD heap', obj.objid::regclass);
            END IF;

            RAISE DEBUG 'rvbbit: registered table % (oid=%)',
                obj.object_identity, obj.objid;
        END IF;
    END LOOP;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_event_trigger
        WHERE evtname = 'rvbbit_on_create_table'
    ) THEN
        CREATE EVENT TRIGGER rvbbit_on_create_table
            ON ddl_command_end
            WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
            EXECUTE FUNCTION rvbbit.on_create_table();
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.doctor(live boolean DEFAULT false)
RETURNS TABLE (
    area text,
    name text,
    status text,
    detail jsonb
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_extversion text;
    v_rvbbit_tables bigint;
    v_row_groups bigint;
    v_variants bigint;
    v_dirty bigint;
    v_disabled bigint;
    v_am_bound bigint;
    v_route_status jsonb;
    v_cost_total bigint;
    v_cost_problem bigint;
    v_cost_warn bigint;
    v_mcp_servers bigint;
    v_mcp_tools bigint;
    v_warren_nodes bigint;
    v_warren_bad_jobs bigint;
    v_backend_count bigint;
    v_accel_status jsonb;
BEGIN
    SELECT e.extversion INTO v_extversion
    FROM pg_extension e
    WHERE e.extname = 'pg_rvbbit';

    RETURN QUERY
    SELECT
        'core'::text,
        'extension'::text,
        CASE WHEN v_extversion IS NULL THEN 'error' ELSE 'ok' END::text,
        jsonb_build_object('extversion', v_extversion);

    SELECT count(*), count(*) FILTER (WHERE shadow_heap_dirty)
    INTO v_rvbbit_tables, v_dirty
    FROM rvbbit.table_dirty_state;
    SELECT count(*) INTO v_disabled
    FROM rvbbit.tables
    WHERE NOT coalesce(acceleration_enabled, true);
    SELECT count(*) INTO v_am_bound
    FROM pg_class c
    JOIN pg_am a ON a.oid = c.relam
    WHERE a.amname = 'rvbbit';

    SELECT count(*) INTO v_row_groups FROM rvbbit.row_groups;
    SELECT count(*) INTO v_variants FROM rvbbit.row_group_variants;

    RETURN QUERY
    SELECT
        'storage'::text,
        'rvbbit_tables'::text,
        CASE WHEN coalesce(v_dirty, 0) > 0 THEN 'warn' ELSE 'ok' END::text,
        jsonb_build_object(
            'tables', coalesce(v_rvbbit_tables, 0),
            'disabled_tables', coalesce(v_disabled, 0),
            'dirty_shadow_heaps', coalesce(v_dirty, 0),
            'row_groups', coalesce(v_row_groups, 0),
            'layout_variants', coalesce(v_variants, 0)
        );

    RETURN QUERY
    SELECT
        'storage'::text,
        'access_method_aliases'::text,
        CASE WHEN coalesce(v_am_bound, 0) > 0 THEN 'warn' ELSE 'ok' END::text,
        jsonb_build_object(
            'am_bound_tables', coalesce(v_am_bound, 0),
            'impact', CASE
                WHEN coalesce(v_am_bound, 0) > 0
                THEN 'DROP EXTENSION pg_rvbbit will be blocked until these tables are disabled or converted to heap'
                ELSE 'all registered acceleration tables are heap catalog tables'
            END,
            'fix', 'SELECT rvbbit.disable_table(''schema.table''::regclass)'
        );

    BEGIN
        SELECT rvbbit.accelerator_runtime_status(live) INTO v_accel_status;
        RETURN QUERY
        SELECT
            'accelerator'::text,
            'runtime'::text,
            coalesce(nullif(v_accel_status->>'status', ''), 'warn')::text,
            v_accel_status;
    EXCEPTION WHEN undefined_function THEN
        RETURN QUERY
        SELECT
            'accelerator'::text,
            'runtime'::text,
            'warn'::text,
            jsonb_build_object('reason', 'accelerator_runtime_status_unavailable');
    END;

    BEGIN
        SELECT rvbbit.route_status() INTO v_route_status;
        RETURN QUERY
        SELECT
            'routing'::text,
            'route_status'::text,
            'ok'::text,
            v_route_status;
    EXCEPTION WHEN undefined_function THEN
        RETURN QUERY
        SELECT
            'routing'::text,
            'route_status'::text,
            'warn'::text,
            jsonb_build_object('reason', 'route_status_unavailable');
    END;

    SELECT count(*) INTO v_backend_count FROM rvbbit.backends;
    RETURN QUERY
    SELECT
        'backend'::text,
        'registry'::text,
        CASE WHEN coalesce(v_backend_count, 0) > 0 THEN 'ok' ELSE 'error' END::text,
        jsonb_build_object('backends', coalesce(v_backend_count, 0));

    RETURN QUERY SELECT * FROM rvbbit.provider_doctor(live);

    SELECT
        count(*),
        count(*) FILTER (
            WHERE audit_status IN ('missing_cost_events', 'stale_pending', 'errors')
        ),
        count(*) FILTER (
            WHERE audit_status IN ('pending', 'uncosted')
        )
    INTO v_cost_total, v_cost_problem, v_cost_warn
    FROM rvbbit.receipt_cost_audit;

    RETURN QUERY
    SELECT
        'costs'::text,
        'receipt_cost_audit'::text,
        CASE
            WHEN coalesce(v_cost_problem, 0) > 0 THEN 'error'
            WHEN coalesce(v_cost_warn, 0) > 0 THEN 'warn'
            ELSE 'ok'
        END::text,
        jsonb_build_object(
            'receipt_rows', coalesce(v_cost_total, 0),
            'problem_rows', coalesce(v_cost_problem, 0),
            'warning_rows', coalesce(v_cost_warn, 0)
        );

    SELECT count(*) INTO v_mcp_servers FROM rvbbit.mcp_servers;
    SELECT count(*) INTO v_mcp_tools FROM rvbbit.mcp_tools;

    RETURN QUERY
    SELECT
        'mcp'::text,
        'registry'::text,
        'ok'::text,
        jsonb_build_object(
            'servers', coalesce(v_mcp_servers, 0),
            'tools', coalesce(v_mcp_tools, 0)
        );

    SELECT count(*) INTO v_warren_nodes FROM rvbbit.warren_nodes;
    SELECT count(*) FILTER (WHERE wj.status = 'failed')
    INTO v_warren_bad_jobs
    FROM rvbbit.warren_jobs wj;

    RETURN QUERY
    SELECT
        'warren'::text,
        'registry'::text,
        CASE WHEN coalesce(v_warren_bad_jobs, 0) > 0 THEN 'warn' ELSE 'ok' END::text,
        jsonb_build_object(
            'nodes', coalesce(v_warren_nodes, 0),
            'failed_jobs', coalesce(v_warren_bad_jobs, 0)
        );
END
$$;
