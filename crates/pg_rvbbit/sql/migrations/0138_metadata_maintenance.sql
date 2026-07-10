-- 0138: metadata maintenance — autovacuum tuning, orphaned-tombstone pruning,
-- a pg_cron VACUUM template, and a formal maintenance mode.
--
-- Field motivation: a production install that fully re-copies ~1,500 source
-- tables every few hours grew rvbbit.delete_log to ~250M live tombstones /
-- 42GB with ZERO autovacuum runs — Postgres' default
-- autovacuum_vacuum_scale_factor (0.2) means a table that size needs ~50M
-- dead tuples before autovacuum even looks at it, and stale stats degrade
-- every consumer (the duck sidecar's catalog refresh runs a correlated
-- count(*) against delete_log per table). rvbbit.row_groups showed the dual
-- failure: 29 live rows under ~700MB of dead TOAST (stats jsonb rewrites).
-- None of this is data loss — just unbounded bloat — but the fix belongs in
-- the extension, not in every operator's runbook.
--
-- Partitioning delete_log was considered and rejected: converting a live
-- multi-GB table to a partitioned one in a migration means a full rewrite
-- under lock at exactly the installs that hurt most, and tuned autovacuum +
-- orphan pruning bounds the same debt without a rewrite.

-- ── 1. Per-table autovacuum tuning ───────────────────────────────────
-- Two tiers. 'volume' tables scale with user data (tombstones, identity
-- maps, staging rows) or append-heavy telemetry: a small scale factor plus a
-- fixed threshold keeps dead-tuple debt bounded regardless of size. 'hot'
-- tables are tiny but update-churned catalogs where the default scale factor
-- technically triggers but the TOAST relation (where the real bloat lives —
-- stats/jsonb payloads) needs its own explicit override.
--
-- Idempotent and deferential: a table whose reloptions already set
-- autovacuum_vacuum_scale_factor is skipped, so operator overrides are never
-- stomped and re-running from the maintenance heartbeat is a near-free no-op.
-- Runs from rvbbit.maintain_storage so tables that bootstrap lazily at
-- runtime (route_decisions, fleet_endpoints, alert_*) converge too.
CREATE OR REPLACE FUNCTION rvbbit.tune_metadata_autovacuum()
RETURNS TABLE (table_name text, tier text)
LANGUAGE plpgsql AS $$
DECLARE
    spec record;
    reloid regclass;
    has_toast boolean;
    opts text;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            -- volume tier: rows scale with user data / query traffic
            ('rvbbit.delete_log',                'volume'),
            ('rvbbit.row_identity_map',          'volume'),
            ('rvbbit.shreds',                    'volume'),
            ('rvbbit.group_stats',               'volume'),
            ('rvbbit.column_bitmaps',            'volume'),
            ('rvbbit.semantic_bitmaps',          'volume'),
            ('rvbbit.route_decisions',           'volume'),
            ('rvbbit.route_executions',          'volume'),
            ('rvbbit.route_observations',        'volume'),
            ('rvbbit.accel_tick_runs',           'volume'),
            ('rvbbit.cost_events',               'volume'),
            ('rvbbit.receipts',                  'volume'),
            ('rvbbit.mcp_invocations',           'volume'),
            ('rvbbit.duck_sidecar_query_events', 'volume'),
            ('rvbbit.duck_sidecar_heartbeats',   'volume'),
            ('rvbbit.alert_events',              'volume'),
            ('rvbbit.alert_sweep_runs',          'volume'),
            -- hot tier: small, constantly-updated catalog state
            ('rvbbit.row_groups',                'hot'),
            ('rvbbit.row_group_variants',        'hot'),
            ('rvbbit.text_dictionaries',         'hot'),
            ('rvbbit.generations',               'hot'),
            ('rvbbit.tables',                    'hot'),
            ('rvbbit.table_dirty_markers',       'hot'),
            ('rvbbit.layout_variant_status',     'hot'),
            ('rvbbit.acceleration_state',        'hot'),
            ('rvbbit.settings',                  'hot'),
            ('rvbbit.orphaned_files',            'hot'),
            ('rvbbit.hot_objects',               'hot'),
            ('rvbbit.gqe_warm_state',            'hot'),
            ('rvbbit.materialize_queue',         'hot'),
            ('rvbbit.alert_queue',               'hot'),
            ('rvbbit.alert_state',               'hot'),
            ('rvbbit.fleet_endpoints',           'hot'),
            ('rvbbit.publish_policy',            'hot')
        ) AS t(tbl, tr)
    LOOP
        reloid := to_regclass(spec.tbl);
        IF reloid IS NULL THEN
            CONTINUE;  -- feature not installed / table bootstraps later
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_class c, unnest(coalesce(c.reloptions, '{}')) AS o
            WHERE c.oid = reloid AND o LIKE 'autovacuum_vacuum_scale_factor=%'
        ) THEN
            CONTINUE;  -- already tuned (by us or the operator) — don't stomp
        END IF;
        SELECT c.reltoastrelid <> 0 INTO has_toast
        FROM pg_class c WHERE c.oid = reloid;
        IF spec.tr = 'volume' THEN
            opts := 'autovacuum_vacuum_scale_factor = 0.005, '
                 || 'autovacuum_vacuum_threshold = 10000, '
                 || 'autovacuum_vacuum_insert_scale_factor = 0.05, '
                 || 'autovacuum_vacuum_insert_threshold = 100000, '
                 || 'autovacuum_analyze_scale_factor = 0.01, '
                 || 'autovacuum_analyze_threshold = 10000';
            IF has_toast THEN
                opts := opts || ', toast.autovacuum_vacuum_scale_factor = 0.01'
                             || ', toast.autovacuum_vacuum_threshold = 10000';
            END IF;
        ELSE
            opts := 'autovacuum_vacuum_scale_factor = 0.02, '
                 || 'autovacuum_vacuum_threshold = 50, '
                 || 'autovacuum_analyze_scale_factor = 0.02, '
                 || 'autovacuum_analyze_threshold = 50';
            IF has_toast THEN
                opts := opts || ', toast.autovacuum_vacuum_scale_factor = 0.02'
                             || ', toast.autovacuum_vacuum_threshold = 100';
            END IF;
        END IF;
        EXECUTE format('ALTER TABLE %s SET (%s)', spec.tbl, opts);
        table_name := spec.tbl;
        tier := spec.tr;
        RETURN NEXT;
    END LOOP;
END $$;

-- ── 2. Orphaned-tombstone pruning ────────────────────────────────────
-- Tombstones are LIVE data: every scan projects them into skip-bitmaps, and
-- rebuild_acceleration() is the only path that retires them wholesale. But
-- two paths strand them permanently: (a) generation reaping deletes
-- row_groups rows without touching delete_log, and (b) tables dropped
-- without the event trigger firing (inheritance gap). A tombstone whose
-- (table_oid, rg_id) no longer exists in row_groups can never influence any
-- scan — latest or AS OF — because scans enumerate row_groups first.
--
-- Cost is proportional to the number of DISTINCT row groups referenced (a
-- recursive skip-scan over the PK), never the total tombstone count, so this
-- is safe on a heartbeat even against a 250M-row delete_log. Each table is
-- pruned under the same per-table advisory lock compaction takes (class id
-- 0x52564254 "RVBT"), try-lock so a mid-compact/rebuild table is skipped
-- until the next pass — belt and suspenders against rebuild's tombstone
-- remapping window.
--
-- max_tables is a safety valve, not a working limit: iteration is oid-ordered,
-- so a low cap would starve high-oid tables forever on installs where
-- thousands of tables legitimately carry tombstones. Examining a clean table
-- costs a handful of index probes — examine them all by default.
CREATE OR REPLACE FUNCTION rvbbit.prune_delete_log(max_tables int DEFAULT 100000)
RETURNS TABLE (table_name text, tombstones_pruned bigint)
LANGUAGE plpgsql AS $$
DECLARE
    t oid;
    n bigint;
    orphan_rgs bigint[];
BEGIN
    FOR t IN
        -- distinct table_oids via recursive skip-scan (no min(oid) aggregate
        -- exists, and a plain DISTINCT would walk the whole index)
        WITH RECURSIVE d AS (
            (SELECT dl.table_oid AS o FROM rvbbit.delete_log dl
              ORDER BY dl.table_oid LIMIT 1)
            UNION ALL
            SELECT (SELECT dl.table_oid FROM rvbbit.delete_log dl
                     WHERE dl.table_oid > d.o
                     ORDER BY dl.table_oid LIMIT 1)
            FROM d WHERE d.o IS NOT NULL
        )
        SELECT o FROM d WHERE o IS NOT NULL
        LIMIT greatest(coalesce(max_tables, 1), 1)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM rvbbit.tables tt WHERE tt.table_oid = t) THEN
            -- Table no longer registered: every tombstone is unreachable.
            -- Unbounded on purpose — it's a one-time PK-range delete, same
            -- shape as the DROP-trigger path that should have run.
            DELETE FROM rvbbit.delete_log dl WHERE dl.table_oid = t;
            GET DIAGNOSTICS n = ROW_COUNT;
        ELSE
            IF NOT pg_try_advisory_xact_lock((1380336724::bigint << 32) | t::bigint) THEN
                CONTINUE;  -- compact/rebuild in flight; next heartbeat
            END IF;
            WITH RECURSIVE r AS (
                (SELECT dl.rg_id FROM rvbbit.delete_log dl
                  WHERE dl.table_oid = t ORDER BY dl.rg_id LIMIT 1)
                UNION ALL
                SELECT (SELECT dl.rg_id FROM rvbbit.delete_log dl
                         WHERE dl.table_oid = t AND dl.rg_id > r.rg_id
                         ORDER BY dl.rg_id LIMIT 1)
                FROM r WHERE r.rg_id IS NOT NULL
            )
            SELECT array_agg(r.rg_id) INTO orphan_rgs
            FROM r
            WHERE r.rg_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM rvbbit.row_groups rg
                              WHERE rg.table_oid = t AND rg.rg_id = r.rg_id);
            IF orphan_rgs IS NULL THEN
                CONTINUE;
            END IF;
            DELETE FROM rvbbit.delete_log dl
             WHERE dl.table_oid = t AND dl.rg_id = ANY (orphan_rgs);
            GET DIAGNOSTICS n = ROW_COUNT;
        END IF;
        IF n > 0 THEN
            table_name := t::regclass::text;
            tombstones_pruned := n;
            RETURN NEXT;
        END IF;
    END LOOP;
END $$;

-- ── 3. Tombstone-pressure visibility ─────────────────────────────────
-- Diagnostic view: which tables carry how much tombstone weight relative to
-- their accelerated rows. A table whose workload is "delete everything,
-- re-insert everything" (full-replace ELT) accumulates tombstones equal to
-- its row count every cycle — high tombstone_pct is the signal that
-- rvbbit.rebuild_acceleration(t) is due (it folds tombstones into a fresh
-- baseline and empties delete_log for the table). Counts scan delete_log, so
-- this is a diagnosis-time view, not a dashboard poller.
CREATE OR REPLACE VIEW rvbbit.tombstone_pressure AS
WITH dl AS (
    SELECT table_oid, count(*) AS tombstones
    FROM rvbbit.delete_log
    GROUP BY table_oid
),
rg AS (
    SELECT table_oid, sum(n_rows) AS accelerated_rows, count(*) AS row_groups
    FROM rvbbit.row_groups
    GROUP BY table_oid
)
SELECT dl.table_oid::regclass AS table_name,
       dl.tombstones,
       coalesce(rg.accelerated_rows, 0) AS accelerated_rows,
       coalesce(rg.row_groups, 0)       AS row_groups,
       CASE WHEN coalesce(rg.accelerated_rows, 0) > 0
            THEN round(100.0 * dl.tombstones / rg.accelerated_rows, 1)
       END AS tombstone_pct
FROM dl
LEFT JOIN rg USING (table_oid)
ORDER BY dl.tombstones DESC;

-- ── 4. pg_cron VACUUM template ───────────────────────────────────────
-- Tuned reloptions fix the trigger math, but an instance whose autovacuum
-- workers are saturated by thousands of user tables still benefits from a
-- scheduled top-level VACUUM of the extension's own catalogs. VACUUM cannot
-- run inside a function — but pg_cron executes job commands as top-level
-- statements, so the JOB is the VACUUM itself; this function only registers
-- it. The table list is resolved at call time: re-run after enabling
-- features that add tables. Mirrors rvbbit.schedule_accel_tick's cron-home
-- handling.
CREATE OR REPLACE FUNCTION rvbbit.schedule_metadata_vacuum(
    cron_schedule text DEFAULT '35 */6 * * *'
) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    jobid     bigint;
    cron_home text := current_setting('cron.database_name', true);
    this_db   text := current_database();
    tabs      text;
    command   text;
BEGIN
    SELECT string_agg(t.tbl, ', ' ORDER BY t.tbl) INTO tabs
    FROM (VALUES
        ('rvbbit.delete_log'), ('rvbbit.row_identity_map'), ('rvbbit.shreds'),
        ('rvbbit.group_stats'), ('rvbbit.column_bitmaps'),
        ('rvbbit.row_groups'), ('rvbbit.row_group_variants'),
        ('rvbbit.text_dictionaries'), ('rvbbit.generations'),
        ('rvbbit.tables'), ('rvbbit.table_dirty_markers'),
        ('rvbbit.route_decisions'), ('rvbbit.route_executions'),
        ('rvbbit.orphaned_files'), ('rvbbit.settings')
    ) AS t(tbl)
    WHERE to_regclass(t.tbl) IS NOT NULL;
    command := format('VACUUM (ANALYZE, SKIP_LOCKED) %s', tabs);
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = format(
                'Use the Scheduler UI, or connect to %L and run: '
                'SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                cron_home, 'rvbbit_metadata_vacuum', cron_schedule, command, this_db);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule the metadata vacuum.'
            USING HINT = 'Add pg_cron to shared_preload_libraries and CREATE EXTENSION pg_cron, '
                         'or run the VACUUM from any external scheduler.';
    END IF;
    EXECUTE format('SELECT cron.schedule(%L, %L, %L)',
                   'rvbbit_metadata_vacuum', cron_schedule, command)
        INTO jobid;
    RETURN jobid;
END $$;

-- ── 5. Maintenance mode ──────────────────────────────────────────────
-- Formalizes the field procedure: flip rvbbit.force_heap_scan at the
-- database level so every NEW session routes straight to the heap (correct
-- answers, no acceleration) while you vacuum / rebuild / debug, then flip it
-- back. Existing sessions keep their current behavior until they reconnect —
-- pooled connections cycle through quickly in practice.
CREATE OR REPLACE FUNCTION rvbbit.maintenance_mode(enable boolean)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    db text := current_database();
BEGIN
    IF enable THEN
        EXECUTE format('ALTER DATABASE %I SET rvbbit.force_heap_scan = on', db);
    ELSE
        EXECUTE format('ALTER DATABASE %I RESET rvbbit.force_heap_scan', db);
    END IF;
    RETURN jsonb_build_object(
        'maintenance_mode', enable,
        'database', db,
        'note', 'applies to new sessions; existing sessions keep their current '
                'routing until they reconnect (session override: SET rvbbit.force_heap_scan = on)'
    );
END $$;

-- ── 6. Wire into the maintenance heartbeat ───────────────────────────
-- maintain_storage gains two isolated steps: re-assert autovacuum tuning
-- (near-free no-op once applied; self-heals lazily-bootstrapped tables) and
-- prune orphaned tombstones. Failures in either never fail maintenance.
CREATE OR REPLACE FUNCTION rvbbit.maintain_storage(
    max_tables bigint DEFAULT 4,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
    n bigint;
    compacted jsonb := '[]'::jsonb;
    refreshed jsonb := '[]'::jsonb;
    errors jsonb := '[]'::jsonb;
    logs_reaped jsonb := '[]'::jsonb;
    orphaned_files_reaped jsonb := '{}'::jsonb;
    tombstones_pruned jsonb := '[]'::jsonb;
    cap bigint := greatest(coalesce(max_tables, 0), 0);
BEGIN
    IF cap = 0 THEN
        RETURN jsonb_build_object(
            'compacted', compacted,
            'refreshed_variants', refreshed,
            'errors', errors,
            'skipped', 'max_tables is zero'
        );
    END IF;

    -- Keep per-table autovacuum tuning asserted (no-op once applied; picks up
    -- tables that bootstrap lazily after install). Never stomps operator
    -- overrides — see rvbbit.tune_metadata_autovacuum.
    BEGIN
        PERFORM rvbbit.tune_metadata_autovacuum();
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'tune_metadata_autovacuum', 'error', SQLERRM)
        );
    END;

    FOR rec IN
        SELECT t.table_oid::regclass AS rel
        FROM rvbbit.tables t
        JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
        JOIN pg_class c ON c.oid = t.table_oid
        WHERE ds.shadow_heap_dirty
        ORDER BY t.created_at
        LIMIT cap
    LOOP
        BEGIN
            SELECT count(*) INTO n FROM rvbbit.compact(rec.rel);
            compacted := compacted || jsonb_build_array(
                jsonb_build_object('table', rec.rel::text, 'row_groups', n)
            );
        EXCEPTION WHEN OTHERS THEN
            errors := errors || jsonb_build_array(
                jsonb_build_object('table', rec.rel::text, 'phase', 'compact', 'error', SQLERRM)
            );
        END;
    END LOOP;

    IF refresh_variants THEN
        FOR rec IN
            WITH candidates AS (
                SELECT
                    t.table_oid,
                    t.table_oid::regclass AS rel,
                    coalesce(max(rg.created_at), '-infinity'::timestamptz) AS newest_rg,
                    coalesce(max(rgv.created_at), '-infinity'::timestamptz) AS newest_variant,
                    count(rg.*) AS row_groups,
                    count(rgv.*) AS variants
                FROM rvbbit.tables t
                JOIN pg_class c ON c.oid = t.table_oid
                LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
                LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = t.table_oid
                GROUP BY t.table_oid
            )
            SELECT rel
            FROM candidates
            WHERE row_groups > 0
              AND (variants = 0 OR newest_variant < newest_rg)
            ORDER BY newest_rg DESC
            LIMIT cap
        LOOP
            BEGIN
                SELECT rvbbit.refresh_layout_variants(rec.rel) INTO n;
                refreshed := refreshed || jsonb_build_array(
                    jsonb_build_object('table', rec.rel::text, 'variants', n)
                );
            EXCEPTION WHEN OTHERS THEN
                errors := errors || jsonb_build_array(
                    jsonb_build_object('table', rec.rel::text, 'phase', 'refresh_variants', 'error', SQLERRM)
                );
            END;
        END LOOP;
    END IF;

    -- resources-02/ops-02: trim the append-only telemetry logs on the same
    -- maintenance heartbeat. Isolated so a reap failure never fails maintenance.
    BEGIN
        SELECT coalesce(
                   jsonb_agg(jsonb_build_object('table', table_name, 'rows', rows_reaped)),
                   '[]'::jsonb)
          INTO logs_reaped
          FROM rvbbit.reap_logs();
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'reap_logs', 'error', SQLERRM)
        );
    END;

    -- Drop tombstones stranded by generation reaping or trigger-less table
    -- drops. Cost scales with distinct row groups, not tombstone count.
    BEGIN
        SELECT coalesce(
                   jsonb_agg(jsonb_build_object('table', table_name, 'tombstones', p.tombstones_pruned)),
                   '[]'::jsonb)
          INTO tombstones_pruned
          FROM rvbbit.prune_delete_log() AS p;
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'prune_delete_log', 'error', SQLERRM)
        );
    END;

    -- Reap old accelerator files only after their metadata swap has committed
    -- and aged past the grace period. This protects readers that planned
    -- against the previous row-group set while a fold was committing — and,
    -- once remote engine workers exist, files a warren may be mid-scan on:
    -- the grace (settings key reap_grace_minutes, default 30) must exceed the
    -- max remote query time.
    BEGIN
        SELECT to_jsonb(r)
          INTO orphaned_files_reaped
          FROM rvbbit.reap_orphaned_files(
              make_interval(mins => coalesce((
                  SELECT (value #>> '{}')::int FROM rvbbit.settings
                  WHERE key = 'reap_grace_minutes'), 30))
          ) AS r;
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'reap_orphaned_files', 'error', SQLERRM)
        );
    END;

    RETURN jsonb_build_object(
        'compacted', compacted,
        'refreshed_variants', refreshed,
        'logs_reaped', logs_reaped,
        'tombstones_pruned', tombstones_pruned,
        'orphaned_files_reaped', coalesce(orphaned_files_reaped, '{}'::jsonb),
        'errors', errors
    );
END $$;

-- ── 7. Apply the tuning now ──────────────────────────────────────────
-- Existing installs get the reloptions immediately rather than waiting for
-- the next maintenance heartbeat.
DO $$
BEGIN
    PERFORM count(*) FROM rvbbit.tune_metadata_autovacuum();
END $$;
