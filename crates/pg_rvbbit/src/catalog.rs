//! Rvbbit's own catalog + access method registration.
//!
//! Phase 1a strategy: register the `rvbbit` access method as an alias for
//! `pg_catalog.heap_tableam_handler`. This makes rvbbit tables identical
//! to heap tables at the storage layer — the goal here is purely to get
//! `CREATE TABLE ... USING rvbbit` working, get the catalog plumbing in
//! place, and have a benchmarkable baseline.
//!
//! Phase 1b will introduce our own handler that routes inserts to a
//! shadow catcher heap. Phase 2 layers parquet on top.

use pgrx::{extension_sql, pg_extern, JsonB, Spi};
use serde_json::{json, Value};

extension_sql!(
    r#"
-- Catalog tables --------------------------------------------------------------

CREATE TABLE rvbbit.tables (
    table_oid       oid PRIMARY KEY,
    catcher_oid     oid,                  -- NULL in Phase 1a (no catcher yet)
    data_dir        text,                 -- NULL until Phase 2
    shadow_heap_retained boolean NOT NULL DEFAULT false,
    shadow_heap_dirty boolean NOT NULL DEFAULT false,
    -- Phase 2: monotonic per-table compaction generation. Each compact()
    -- call atomically increments this and stamps every row group it writes
    -- with the OLD value. Reads see the latest generation by default;
    -- AS OF queries (future) can narrow to `generation <= asof`.
    next_generation bigint NOT NULL DEFAULT 1,
    -- Snapshot visibility floor. When > 0, the "latest" (non-AS-OF) scan
    -- shows only row groups at generation >= this value, hiding older
    -- retained generations. Used by the snapshot-load sync workflow, where
    -- each run writes one full-table snapshot generation and bumps the floor
    -- to it, so the current view is the newest snapshot (not the union of all
    -- snapshots) while AS OF still reads the full history. Default 0 = no-op
    -- (generations start at 1), so ordinary append tables are unaffected.
    min_visible_generation bigint NOT NULL DEFAULT 0,
    -- Phase 4 Lance auto-refresh. When lance_url IS NOT NULL, compact()
    -- mirrors the named vector column into a Lance dataset at this URL
    -- (overwriting per compact), so rvbbit.knn() can do indexed KNN
    -- without the operator having to call lance_import_column manually.
    -- lance_vector_column is the source column name (must be real[]),
    -- lance_dim is the expected vector dimension. Set together via
    -- rvbbit.lance_enable().
    lance_url            text,
    lance_vector_column  text,
    lance_dim            int,
    -- Freshness control plane (Layer 1). last_write_at is bumped by the
    -- shadow-heap dirty trigger on every DML statement; dirty_since is
    -- stamped only on the clean->dirty transition so we can age how long a
    -- table has been stale. Both are NULL until the first write after a
    -- refresh. rvbbit.accel_freshness reads them (and NULLs dirty_since when
    -- the table is clean, so the clear-sites in refresh/rebuild need no edit).
    dirty_since          timestamptz,
    last_write_at        timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rvbbit.lance_text_indexes (
    table_oid      oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    column_name    text NOT NULL,
    specialist     text NOT NULL DEFAULT 'embed',
    lance_url      text NOT NULL,
    dim            int NOT NULL,
    n_values       bigint NOT NULL DEFAULT 0,
    status         text NOT NULL DEFAULT 'ready',
    status_message text,
    refreshed_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (table_oid, column_name, specialist),
    CONSTRAINT lance_text_indexes_status_check CHECK (
        status IN ('ready', 'refreshing', 'failed', 'disabled')
    )
);

CREATE TABLE rvbbit.row_groups (
    table_oid       oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    rg_id           bigint NOT NULL,
    path            text NOT NULL,
    n_rows          bigint NOT NULL,
    n_bytes         bigint NOT NULL,
    min_xid         xid8,
    max_xid         xid8,
    -- Phase 2 generation stamp. Default 0 covers pre-Phase-2 row groups
    -- that pre-date the column; new compactions allocate a real value
    -- (>= 1) under a per-table advisory lock so two concurrent compacts
    -- can never collide.
    generation      bigint NOT NULL DEFAULT 0,
    -- Phase 2 ObjectStore tiered storage. When NULL, this row group lives
    -- at `path` (a local filesystem path under PGDATA/rvbbit/) and is read
    -- by the native custom_scan. When non-NULL, this row group has been
    -- migrated to a cold tier — `cold_url` is a full ObjectStore URL
    -- (file://, s3://, gs://) and reads route through in-process
    -- DataFusion which has ObjectStore-aware parquet support. The custom
    -- scan path doesn't handle URL schemes, so tables with any cold row
    -- groups fall back to df.rs entirely.
    cold_url        text,
    stats           jsonb,
    -- Per-group aggregate blocks for low-cardinality columns. Powers
    -- GROUP BY pushdown — see rvbbit.agg_groupby_*. NULL when no
    -- column qualified (high-cardinality table) or pre-migration.
    per_group_stats jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, rg_id)
);

	CREATE INDEX row_groups_table_generation_idx
	    ON rvbbit.row_groups (table_oid, generation);

	-- Floor-aware view: only generations visible under each table's snapshot floor
	-- (min_visible_generation). SNAPSHOT table => just the latest generation;
	-- APPEND table (floor 0) => every generation. The metadata fast paths
	-- (count/sum/min/max/groupby answered from row-group stats without scanning)
	-- read THIS instead of rvbbit.row_groups, so they never sum across hidden
	-- generations. Diagnostic/maintenance queries keep using rvbbit.row_groups.
	CREATE OR REPLACE VIEW rvbbit.row_groups_visible AS
	SELECT rg.*
	FROM rvbbit.row_groups rg
	JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
	WHERE rg.generation >= t.min_visible_generation;

	CREATE TABLE rvbbit.group_stats (
	    table_oid        oid NOT NULL,
	    rg_id            bigint NOT NULL,
	    group_col        text NOT NULL,
	    group_key        text NOT NULL,
	    group_value_text text,
	    count            bigint NOT NULL,
	    agg              jsonb,
	    created_at       timestamptz NOT NULL DEFAULT now(),
	    PRIMARY KEY (table_oid, rg_id, group_col, group_key),
	    FOREIGN KEY (table_oid, rg_id)
	        REFERENCES rvbbit.row_groups(table_oid, rg_id) ON DELETE CASCADE
	);

	CREATE INDEX group_stats_lookup_idx
	    ON rvbbit.group_stats (table_oid, group_col, group_key);

	CREATE TABLE rvbbit.column_bitmaps (
	    table_oid   oid NOT NULL,
	    rg_id       bigint NOT NULL,
	    column_name text NOT NULL,
	    bitmap_kind text NOT NULL,
	    value_text  text NOT NULL,
	    value_json  jsonb,
	    bitmap      bytea NOT NULL,
	    n_set       bigint NOT NULL,
	    n_total     bigint NOT NULL,
	    created_at  timestamptz NOT NULL DEFAULT now(),
	    PRIMARY KEY (table_oid, rg_id, column_name, bitmap_kind, value_text),
	    FOREIGN KEY (table_oid, rg_id)
	        REFERENCES rvbbit.row_groups(table_oid, rg_id) ON DELETE CASCADE
	);

	CREATE INDEX column_bitmaps_lookup_idx
	    ON rvbbit.column_bitmaps (table_oid, column_name, bitmap_kind, value_text);

	CREATE TABLE rvbbit.text_dictionaries (
	    table_oid   oid NOT NULL,
	    rg_id       bigint NOT NULL,
	    column_name text NOT NULL,
	    path        text NOT NULL,
	    n_rows      bigint NOT NULL,
	    n_values    bigint NOT NULL,
	    n_nulls     bigint NOT NULL,
	    n_empty     bigint NOT NULL,
	    n_bytes     bigint NOT NULL,
	    created_at  timestamptz NOT NULL DEFAULT now(),
	    PRIMARY KEY (table_oid, rg_id, column_name),
	    FOREIGN KEY (table_oid, rg_id)
	        REFERENCES rvbbit.row_groups(table_oid, rg_id) ON DELETE CASCADE
	);

	CREATE INDEX text_dictionaries_lookup_idx
	    ON rvbbit.text_dictionaries (table_oid, column_name, rg_id);

	-- The latest generation present in row_groups for a table. Returns 0 when
-- nothing has been compacted yet (matches the row_groups column default).
CREATE OR REPLACE FUNCTION rvbbit.current_generation(reloid regclass)
RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT coalesce(max(generation), 0)::bigint
    FROM rvbbit.row_groups
    WHERE table_oid = reloid
$$;

-- Per-table generation timeline. compact() INSERTs one row per call with
-- the wall-clock time the compaction committed, so future AS OF TIMESTAMP
-- queries can resolve a timestamp to the right generation.
CREATE TABLE rvbbit.generations (
    table_oid     oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    generation    bigint NOT NULL,
    committed_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    n_rows        bigint NOT NULL DEFAULT 0,
    n_row_groups  int NOT NULL DEFAULT 0,
    PRIMARY KEY (table_oid, generation)
);

CREATE INDEX generations_table_committed_idx
    ON rvbbit.generations (table_oid, committed_at);

-- Return a table's generation timeline, latest first. Useful for picking
-- an AS OF point manually before SETting rvbbit.as_of_generation.
CREATE OR REPLACE FUNCTION rvbbit.list_generations(reloid regclass)
RETURNS TABLE (generation bigint, committed_at timestamptz,
               n_rows bigint, n_row_groups int)
LANGUAGE sql STABLE AS $$
    SELECT generation, committed_at, n_rows, n_row_groups
    FROM rvbbit.generations
    WHERE table_oid = reloid
    ORDER BY generation DESC
$$;

-- UI-facing time-travel timeline. This is metadata-only: it reads the
-- generation log, row-group catalog, and delete log; it never scans the heap
-- or parquet files. `rows_written` is the delta written at that tick.
-- `visible_rows_estimate` is the approximate row count visible at that tick
-- after generation-aware tombstones.
CREATE OR REPLACE FUNCTION rvbbit.time_travel_timeline(reloid regclass)
RETURNS TABLE (
    generation            bigint,
    committed_at          timestamptz,
    rows_written          bigint,
    row_groups_written    int,
    visible_rows_estimate bigint,
    visible_row_groups    bigint,
    tombstones_visible    bigint
) LANGUAGE sql STABLE AS $$
    SELECT
        g.generation,
        g.committed_at,
        g.n_rows AS rows_written,
        g.n_row_groups AS row_groups_written,
        greatest(
            coalesce(rg.visible_rows, 0) - coalesce(dl.tombstones_visible, 0),
            0
        )::bigint AS visible_rows_estimate,
        coalesce(rg.visible_row_groups, 0)::bigint AS visible_row_groups,
        coalesce(dl.tombstones_visible, 0)::bigint AS tombstones_visible
    FROM rvbbit.generations g
    LEFT JOIN LATERAL (
        SELECT
            coalesce(sum(rg.n_rows), 0)::bigint AS visible_rows,
            count(*)::bigint AS visible_row_groups
        FROM rvbbit.row_groups rg
        WHERE rg.table_oid = reloid
          AND rg.generation <= g.generation
    ) rg ON true
    LEFT JOIN LATERAL (
        SELECT count(*)::bigint AS tombstones_visible
        FROM rvbbit.delete_log dl
        WHERE dl.table_oid = reloid
          AND dl.deleted_generation <= g.generation
    ) dl ON true
    WHERE g.table_oid = reloid
    ORDER BY g.generation DESC
$$;

-- Resolve a timestamp to the generation that was committed at or before
-- that point, set rvbbit.as_of_generation to it, and return the value.
-- The set is session-level (not transaction-local) so it persists across
-- subsequent SELECTs in the same psql connection until RESET. Operators
-- can use this as the AS OF TIMESTAMP user interface:
--
--   SELECT rvbbit.set_as_of('orders'::regclass, '2026-05-25 19:00');
--   SELECT * FROM orders;          -- reads at that historical point
--   SELECT rvbbit.set_as_of_reset();
CREATE OR REPLACE FUNCTION rvbbit.set_as_of(reloid regclass, ts timestamptz)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    gen bigint;
BEGIN
    SELECT max(generation) INTO gen
    FROM rvbbit.generations
    WHERE table_oid = reloid AND committed_at <= ts;

    -- Resolves to 0 when ts predates the earliest generation. The catalog
    -- discovery in df.rs treats 0/unset the same way: "no AS OF filter".
    -- Result: querying before the table existed returns the current view.
    -- That's a documented limitation rather than a silent failure — use
    -- list_generations() to confirm a real generation exists before
    -- setting an AS OF.
    IF gen IS NULL THEN
        gen := 0;
    END IF;

    PERFORM set_config('rvbbit.as_of_generation', gen::text, false);
    RETURN gen;
END $$;

-- Convenience: clear the AS OF setting and go back to the latest view.
CREATE OR REPLACE FUNCTION rvbbit.set_as_of_reset()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM set_config('rvbbit.as_of_generation', '', false);
END $$;

-- Optional physical copies of the same compacted rows, with a different
-- layout. `row_groups` remains the canonical scan layout consumed by older
-- metadata paths; readers may opt into these variants when their predicates
-- make the alternate layout cheaper.
CREATE TABLE rvbbit.row_group_variants (
    table_oid       oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    layout          text NOT NULL,
    rg_id           bigint NOT NULL,
    path            text NOT NULL,
    n_rows          bigint NOT NULL,
    n_bytes         bigint NOT NULL,
    stats           jsonb,
    per_group_stats jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, layout, rg_id)
);

CREATE TABLE rvbbit.layout_variant_status (
    table_oid       oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    layout          text NOT NULL,
    status          text NOT NULL DEFAULT 'ready',
    expected_rows   bigint NOT NULL DEFAULT 0,
    actual_rows     bigint NOT NULL DEFAULT 0,
    file_count      integer NOT NULL DEFAULT 0,
    status_message  text,
    refreshed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, layout),
    CHECK (status IN ('ready', 'refreshing', 'invalid', 'failed'))
);

CREATE TABLE rvbbit.acceleration_state (
    table_oid                 oid PRIMARY KEY REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    last_refresh_xid          numeric NOT NULL DEFAULT 0,
    last_refresh_generation   bigint NOT NULL DEFAULT 0,
    last_refresh_rows         bigint NOT NULL DEFAULT 0,
    last_refresh_row_groups   bigint NOT NULL DEFAULT 0,
    last_refresh_at           timestamptz,
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CHECK (last_refresh_xid >= 0),
    CHECK (last_refresh_generation >= 0),
    CHECK (last_refresh_rows >= 0),
    CHECK (last_refresh_row_groups >= 0)
);

CREATE TABLE rvbbit.acceleration_operations (
    id                bigserial PRIMARY KEY,
    table_oid         oid REFERENCES rvbbit.tables(table_oid) ON DELETE SET NULL,
    table_name        text NOT NULL,
    operation         text NOT NULL,
    status            text NOT NULL DEFAULT 'running',
    started_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at       timestamptz,
    watermark_before  numeric,
    watermark_after   numeric,
    rows_written      bigint,
    row_groups_written bigint,
    variants_rows     bigint,
    generation_after  bigint,
    settings          jsonb NOT NULL DEFAULT '{}'::jsonb,
    error             text,
    CHECK (operation IN ('refresh_acceleration', 'rebuild_acceleration', 'compact_acceleration', 'legacy_compact')),
    CHECK (status IN ('running', 'ok', 'failed', 'noop')),
    CHECK (watermark_before IS NULL OR watermark_before >= 0),
    CHECK (watermark_after IS NULL OR watermark_after >= 0),
    CHECK (rows_written IS NULL OR rows_written >= 0),
    CHECK (row_groups_written IS NULL OR row_groups_written >= 0),
    CHECK (variants_rows IS NULL OR variants_rows >= 0),
    CHECK (generation_after IS NULL OR generation_after >= 0)
);

CREATE INDEX acceleration_operations_table_started_idx
    ON rvbbit.acceleration_operations (table_oid, started_at DESC);

CREATE INDEX acceleration_operations_status_idx
    ON rvbbit.acceleration_operations (status, started_at DESC);

CREATE TABLE rvbbit.acceleration_operation_phases (
    id                  bigserial PRIMARY KEY,
    operation_id        bigint REFERENCES rvbbit.acceleration_operations(id) ON DELETE CASCADE,
    table_oid           oid REFERENCES rvbbit.tables(table_oid) ON DELETE SET NULL,
    table_name          text NOT NULL,
    phase               text NOT NULL,
    layout              text,
    partition_key       text,
    status              text NOT NULL DEFAULT 'running',
    started_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at         timestamptz,
    rows_written        bigint,
    row_groups_written  bigint,
    bytes_written       bigint,
    files_written       integer,
    expected_rows       bigint,
    actual_rows         bigint,
    details             jsonb NOT NULL DEFAULT '{}'::jsonb,
    error               text,
    CHECK (status IN ('running', 'ok', 'failed', 'invalid', 'skipped')),
    CHECK (rows_written IS NULL OR rows_written >= 0),
    CHECK (row_groups_written IS NULL OR row_groups_written >= 0),
    CHECK (bytes_written IS NULL OR bytes_written >= 0),
    CHECK (files_written IS NULL OR files_written >= 0),
    CHECK (expected_rows IS NULL OR expected_rows >= 0),
    CHECK (actual_rows IS NULL OR actual_rows >= 0)
);

CREATE INDEX acceleration_operation_phases_operation_idx
    ON rvbbit.acceleration_operation_phases (operation_id, started_at);

CREATE INDEX acceleration_operation_phases_table_started_idx
    ON rvbbit.acceleration_operation_phases (table_oid, started_at DESC);

CREATE TABLE rvbbit.delete_log (
    table_oid       oid NOT NULL,
    rg_id           bigint NOT NULL,
    ordinal         int NOT NULL,
    deleted_xid     xid8 NOT NULL,
    -- Phase 2 slice 4: tombstones carry a generation so AS OF queries
    -- can honor them ("at AS OF gen N, apply tombstones with
    -- deleted_generation <= N"). Default 0 covers any pre-Phase-2
    -- entries written without this column.
    deleted_generation bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (table_oid, rg_id, ordinal)
);

CREATE INDEX delete_log_xid_idx ON rvbbit.delete_log (deleted_xid);
CREATE INDEX delete_log_table_generation_idx
    ON rvbbit.delete_log (table_oid, deleted_generation);

-- mvcc-08: reconstruct a 32-bit heap xmin into the full 64-bit transaction id
-- (as numeric) so it can be compared against xid8 watermarks (pg_snapshot_xmin)
-- across XID wraparound. The bare 32-bit xmin wraps every ~4.29e9 transactions,
-- so a plain `xmin::numeric > watermark` silently excludes all new rows once the
-- watermark passes 2^32 — stopping incremental accelerator ingest. Pairing xmin
-- with the current snapshot's epoch (and stepping back one epoch when the
-- candidate lands in the future) yields the monotonic full xid.
CREATE OR REPLACE FUNCTION rvbbit.xid_to_fxid(x xid)
RETURNS numeric LANGUAGE sql STABLE AS $$
    -- `cur` must be the HIGH watermark (xmax = the next xid to be assigned), not
    -- xmin: a row's xmin is normally newer than the oldest active xid, so using
    -- xmin as the reference would make candidate > cur fire spuriously and
    -- subtract an epoch. With xmax, every assigned xid <= cur, so the subtract
    -- only triggers for xids that genuinely wrapped from the previous epoch.
    SELECT CASE
        WHEN floor(cur / 4294967296::numeric) * 4294967296::numeric + xv > cur
        THEN floor(cur / 4294967296::numeric) * 4294967296::numeric + xv - 4294967296::numeric
        ELSE floor(cur / 4294967296::numeric) * 4294967296::numeric + xv
    END
    FROM (
        SELECT (x::text)::numeric AS xv,
               (pg_snapshot_xmax(pg_current_snapshot())::text)::numeric AS cur
    ) v
$$;

-- resources-02/ops-02: bound the append-only telemetry/heartbeat log tables.
-- They grow forever otherwise (accel_tick_runs alone is ~1 row/table/minute on
-- the heartbeat) and several feed per-tick budget subqueries that degrade with
-- history. The immutable BI log (metric_observations) and drift baselines
-- (catalog_snapshots) are intentionally NOT reaped here — they have functional
-- dependents. Call from a maintenance heartbeat (rvbbit.maintain_storage does).
CREATE OR REPLACE FUNCTION rvbbit.reap_logs(max_age interval DEFAULT interval '14 days')
RETURNS TABLE (table_name text, rows_reaped bigint)
LANGUAGE plpgsql AS $$
DECLARE
    spec   record;
    cutoff timestamptz := now() - max_age;
    n      bigint;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('rvbbit.accel_tick_runs',  'ran_at'),
            ('rvbbit.route_decisions',  'decided_at'),
            ('rvbbit.route_executions', 'executed_at'),
            ('rvbbit.mcp_invocations',  'invocation_at'),
            ('rvbbit.cost_events',      'created_at'),
            ('rvbbit.sync_runs',        'started_at'),
            ('rvbbit.receipts',         'invocation_at')
        ) AS t(tbl, col)
    LOOP
        IF to_regclass(spec.tbl) IS NULL THEN
            CONTINUE;  -- table from an uninstalled feature; skip
        END IF;
        EXECUTE format('DELETE FROM %s WHERE %I < $1', spec.tbl, spec.col) USING cutoff;
        GET DIAGNOSTICS n = ROW_COUNT;
        table_name  := spec.tbl;
        rows_reaped := n;
        RETURN NEXT;
    END LOOP;
END $$;

-- Allocate a new generation for the given table — same per-table
-- advisory-lock pattern that compact() uses, so any tombstone-writing
-- code can stamp delete_log entries with a number that doesn't collide
-- with a concurrent compact. Returns the allocated value.
CREATE OR REPLACE FUNCTION rvbbit.allocate_generation(reloid regclass)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    gen bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(
        ((1380336724::bigint) << 32) | reloid::oid::bigint);
    UPDATE rvbbit.tables
       SET next_generation = next_generation + 1
     WHERE table_oid = reloid
    RETURNING next_generation - 1 INTO gen;
    IF gen IS NULL THEN
        RAISE EXCEPTION 'rvbbit.allocate_generation: table % is not registered with the rvbbit access method', reloid;
    END IF;
    RETURN gen;
END $$;

-- Write a single tombstone. Allocates a new generation, inserts one
-- delete_log entry, returns the allocated generation. Pre-existing
-- tombstones for the same (rg_id, ordinal) update their deleted_xid
-- and bump deleted_generation forward — a re-delete is a no-op
-- semantically but should observably appear in a later generation.
-- NB: parameters are prefixed p_ so they don't shadow the delete_log column
-- names in the ON CONFLICT target (table_oid, rg_id, ordinal) — an unprefixed
-- `rg_id`/`ordinal` parameter makes that reference ambiguous and the function
-- raises at runtime ("column reference \"rg_id\" is ambiguous").
CREATE OR REPLACE FUNCTION rvbbit.tombstone(
    reloid regclass, p_rg_id bigint, p_ordinal int
) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    gen bigint;
BEGIN
    gen := rvbbit.allocate_generation(reloid);
    INSERT INTO rvbbit.delete_log
        (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
    VALUES
        (reloid, p_rg_id, p_ordinal, pg_current_xact_id(), gen)
    ON CONFLICT (table_oid, rg_id, ordinal) DO UPDATE SET
        deleted_xid = EXCLUDED.deleted_xid,
        deleted_generation = EXCLUDED.deleted_generation;
    RETURN gen;
END $$;

-- Batch tombstones in one generation. `items` is a JSON array of
-- {"rg": bigint, "ord": int} objects. All entries land at the same
-- allocated generation, so a DELETE statement touching N rows is one
-- atomic time-travel event.
CREATE OR REPLACE FUNCTION rvbbit.tombstone_batch(
    reloid regclass, items jsonb
) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    gen bigint;
    n_items int;
BEGIN
    IF items IS NULL OR jsonb_typeof(items) <> 'array' THEN
        RAISE EXCEPTION 'rvbbit.tombstone_batch: items must be a JSON array of {"rg":..,"ord":..} objects';
    END IF;
    n_items := jsonb_array_length(items);
    IF n_items = 0 THEN
        RETURN 0;   -- nothing to do; don't burn a generation
    END IF;
    gen := rvbbit.allocate_generation(reloid);
    INSERT INTO rvbbit.delete_log
        (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
    SELECT reloid,
           (e->>'rg')::bigint,
           (e->>'ord')::int,
           pg_current_xact_id(),
           gen
      FROM jsonb_array_elements(items) AS e
    ON CONFLICT (table_oid, rg_id, ordinal) DO UPDATE SET
        deleted_xid = EXCLUDED.deleted_xid,
        deleted_generation = EXCLUDED.deleted_generation;
    RETURN gen;
END $$;

-- Count effective tombstones for a table at a given AS OF generation.
-- Pass NULL for the latest view (all tombstones applied).
CREATE OR REPLACE FUNCTION rvbbit.tombstone_count(
    reloid regclass, asof bigint DEFAULT NULL
) RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT count(*)::bigint
    FROM rvbbit.delete_log
    WHERE table_oid = reloid
      AND (asof IS NULL OR deleted_generation <= asof)
$$;

-- ---------------------------------------------------------------------------
-- Phase 2 slice 5: UPDATE-by-composition.
--
-- PG can't lower the SQL-standard UPDATE syntax onto a parquet-backed
-- relation without exposing per-row identity in the scan, which is a
-- multi-week change. Until that lands, the practical UPDATE operation
-- on a rvbbit table is "tombstone old + INSERT new + compact" — the same
-- three things a heap UPDATE does logically, just composed by the
-- operator. This helper wraps that composition so the user can do it in
-- a single SQL call, and so all three steps share a single transaction:
--
--   SELECT rvbbit.update_rows(
--       'orders'::regclass,
--       '[{"rg":0,"ord":17}, {"rg":1,"ord":4}]'::jsonb,
--       $$INSERT INTO orders (status, amount) VALUES ('shipped', 99),
--                                                    ('shipped', 145)$$);
--
-- Returns the tombstone generation and the new-insert generation as
-- jsonb so AS OF queries against either can be constructed cleanly.
-- Skipping tombstones (empty array) reduces to plain INSERT+compact;
-- skipping inserts (NULL or empty string) reduces to plain tombstone.
CREATE OR REPLACE FUNCTION rvbbit.update_rows(
    reloid     regclass,
    tombstones jsonb,
    inserts    text
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    tombstone_gen bigint;
    insert_gen    bigint;
    insert_sql    text := coalesce(btrim(inserts), '');
BEGIN
    tombstone_gen := rvbbit.tombstone_batch(reloid, coalesce(tombstones, '[]'::jsonb));
    IF length(insert_sql) > 0 THEN
        EXECUTE insert_sql;
        -- compact stamps a new generation and writes a fresh row group for
        -- the inserted rows (heap is drained as part of the call).
        PERFORM rvbbit.compact(reloid);
    END IF;
    insert_gen := rvbbit.current_generation(reloid);
    RETURN jsonb_build_object(
        'tombstone_generation', tombstone_gen,
        'insert_generation',    insert_gen
    );
END $$;

-- ---------------------------------------------------------------------------
-- Phase 2 slice 6: pg_dump / pg_restore safety net.
--
-- Background. Rvbbit's parquet row groups live on disk under PGDATA, not
-- inside the database itself. A pg_dump captures the heap, the catalog
-- tables (rvbbit.tables, rvbbit.row_groups, rvbbit.generations, etc.),
-- but NOT the parquet files. On the restore target the catalog ends up
-- pointing at parquet files that don't exist — a broken state.
--
-- rebuild_acceleration is the recovery primitive: it wipes the derived
-- catalog state for one table, resets next_generation to 1, and regenerates
-- parquet from the current heap contents without truncating the heap. After
-- this call the table is queryable through accelerated paths again.
--
-- Returns {dropped_row_groups, new_row_count} so the caller can sanity-
-- check that the rebuild matches expectations.
-- ---------------------------------------------------------------------------
-- Phase 2 (post-DF53): ObjectStore tiered storage.
--
-- A row group with rvbbit.row_groups.cold_url IS NOT NULL lives on an
-- ObjectStore-addressable URL (file://, s3://, gs://, ...) instead of the
-- default local PGDATA path. In-process DataFusion reads via DataFusion's
-- ObjectStore abstraction; the native custom_scan can't handle URL schemes,
-- so it falls through to df.rs for any table with a cold row group.
--
-- rvbbit.migrate_to_cold copies every row group's parquet file from its
-- current `path` to `<cold_url_prefix>/<table_oid>/scan/<rg_id>.parquet`
-- and sets cold_url accordingly. Files are COPIED, not moved; the local
-- copies remain on disk until the operator explicitly cleans them up
-- (rvbbit.drop_local_after_migrate, future). Re-running the migration is
-- idempotent for already-cold row groups.
--
-- The MVP supports file:// only — sufficient to validate the wiring on a
-- single-machine demo (cold URL points to a different mount). s3:// and
-- gs:// land when we add the corresponding ObjectStore credential helpers.
CREATE OR REPLACE FUNCTION rvbbit.migrate_to_cold(
    reloid          regclass,
    cold_url_prefix text
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    table_oid_str text := reloid::oid::text;
    prefix_norm   text := rtrim(cold_url_prefix, '/');
    n_migrated    int := 0;
    n_bytes_total bigint := 0;
    rg_record     record;
    src_path      text;
    dest_url      text;
    dest_local    text;
    cp_status     int;
BEGIN
    -- MVP: file:// scheme only. Strip the prefix to get a local filesystem
    -- destination. Future work plugs s3:// + gs:// in here.
    IF position('://' IN prefix_norm) = 0 OR
       (NOT prefix_norm LIKE 'file://%') THEN
        RAISE EXCEPTION 'rvbbit.migrate_to_cold: only file:// cold_url_prefix is supported in this MVP, got %', cold_url_prefix;
    END IF;
    dest_local := substring(prefix_norm FROM 8);   -- strip "file://"

    -- security-04: dest_local is interpolated into COPY ... TO PROGRAM shell
    -- commands below. SQL-quote-doubling only stops it breaking the SQL literal,
    -- not the shell, so a path like '/tmp/x; rm -rf /' would inject. Require a
    -- strict safe-path allowlist (letters, digits, / _ . -) — no spaces or shell
    -- metacharacters. (table_oid and rg_id are numeric, so they're already safe.)
    IF dest_local !~ '^/[A-Za-z0-9_./-]+$' THEN
        RAISE EXCEPTION 'rvbbit.migrate_to_cold: cold_url_prefix path may only contain letters, digits, and / _ . - (got %)', dest_local;
    END IF;

    -- Create destination directory tree using COPY TO PROGRAM, which is the
    -- least-bad way to invoke `mkdir -p` from inside a function. We're
    -- already running as a superuser-only extension, so this stays inside
    -- the existing trust boundary.
    EXECUTE format(
        'COPY (SELECT 1) TO PROGRAM ''mkdir -p %s/%s/scan''',
        replace(dest_local, '''', ''''''), replace(table_oid_str, '''', '''''')
    );

    FOR rg_record IN
        SELECT rg_id, path, n_bytes, cold_url
        FROM rvbbit.row_groups
        WHERE table_oid = reloid
        ORDER BY rg_id
    LOOP
        IF rg_record.cold_url IS NOT NULL THEN
            -- Already migrated; skip.
            CONTINUE;
        END IF;
        src_path := rg_record.path;
        dest_url := format('%s/%s/scan/%s.parquet',
            prefix_norm, table_oid_str, rg_record.rg_id);

        EXECUTE format(
            'COPY (SELECT 1) TO PROGRAM ''cp %s %s/%s/scan/%s.parquet''',
            replace(src_path, '''', ''''''),
            replace(dest_local, '''', ''''''),
            replace(table_oid_str, '''', ''''''),
            rg_record.rg_id::text
        );

        UPDATE rvbbit.row_groups
           SET cold_url = dest_url
         WHERE table_oid = reloid AND rg_id = rg_record.rg_id;

        n_migrated := n_migrated + 1;
        n_bytes_total := n_bytes_total + rg_record.n_bytes;
    END LOOP;

    RETURN jsonb_build_object(
        'migrated_row_groups', n_migrated,
        'total_bytes',          n_bytes_total,
        'cold_url_prefix',      prefix_norm
    );
END $$;

CREATE OR REPLACE FUNCTION rvbbit.rebuild_acceleration(
    reloid regclass,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb LANGUAGE plpgsql AS $$
<<accel_rebuild>>
DECLARE
    op_id bigint;
    table_name_text text := reloid::text;
    dropped_rgs int := 0;
    rebuilt_rows bigint := 0;
    row_groups_written bigint := 0;
    variants_rows bigint;
    generation_after bigint := 0;
    safe_upper_xid numeric;
    phase_id bigint;
    phase_bytes_written bigint := 0;
    orphan_paths text[];
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    -- Rebuild is an accelerator maintenance operation. Block writers while
    -- we take the heap snapshot so the retained heap can be marked clean
    -- without racing a concurrent INSERT/UPDATE/DELETE.
    EXECUTE format('LOCK TABLE %s IN SHARE MODE', reloid);

    safe_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmin(pg_current_snapshot())::text)::numeric - 1
    );

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status,
        watermark_before, watermark_after, settings
    ) VALUES (
        reloid, table_name_text, 'rebuild_acceleration', 'running',
        NULL, safe_upper_xid,
        jsonb_build_object(
            'refresh_variants', refresh_variants,
            'mode', 'full_heap_rebuild',
            'heap_guard', 'LOCK TABLE IN SHARE MODE'
        )
    )
    RETURNING id INTO op_id;

    -- resources-01: capture the on-disk parquet paths so we can unlink them.
    -- rg_id restarts at 0 on rebuild, so any file above the post-rebuild
    -- high-water mark would never be overwritten and would leak forever.
    SELECT count(*)::int, array_agg(path)
      INTO dropped_rgs, orphan_paths
      FROM rvbbit.row_groups WHERE table_oid = reloid;

    -- Wipe derived state, then unlink the orphaned files. The retained heap is
    -- authoritative (shadow_heap_retained set below) so the data is recoverable
    -- even though the unlink runs before this transaction commits.
    DELETE FROM rvbbit.delete_log         WHERE table_oid = reloid;
    DELETE FROM rvbbit.layout_variant_status WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_group_variants WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_groups         WHERE table_oid = reloid;
    DELETE FROM rvbbit.generations        WHERE table_oid = reloid;
    UPDATE rvbbit.tables
       SET next_generation = 1,
           shadow_heap_retained = true,
           shadow_heap_dirty = false
     WHERE table_oid = reloid;
    DELETE FROM rvbbit.acceleration_state WHERE table_oid = reloid;

    -- resources-01: drop the orphaned parquet files (idempotent; missing files
    -- are a no-op). Done after the catalog wipe so a failure before here leaves
    -- the files referenced and intact.
    IF orphan_paths IS NOT NULL THEN
        PERFORM rvbbit.reap_unlink_files(orphan_paths);
    END IF;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_full_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'full_heap_rebuild',
            'dropped_row_groups', dropped_rgs
        )
    )
    RETURNING id INTO phase_id;

    SELECT rvbbit.export_to_parquet_full_scan(reloid::oid) INTO rebuilt_rows;

    SELECT count(*)::bigint, coalesce(max(generation), 0)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_written
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    UPDATE rvbbit.acceleration_operation_phases
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = rebuilt_rows,
           row_groups_written = accel_rebuild.row_groups_written,
           files_written = accel_rebuild.row_groups_written::integer,
           bytes_written = phase_bytes_written,
           expected_rows = rebuilt_rows,
           actual_rows = rebuilt_rows
     WHERE id = phase_id;

    IF refresh_variants AND rebuilt_rows > 0 THEN
        PERFORM set_config('rvbbit.acceleration_operation_id', op_id::text, true);
        SELECT rvbbit.refresh_layout_variants(reloid) INTO variants_rows;
        PERFORM set_config('rvbbit.acceleration_operation_id', '', true);
    END IF;

    INSERT INTO rvbbit.acceleration_state (
        table_oid,
        last_refresh_xid,
        last_refresh_generation,
        last_refresh_rows,
        last_refresh_row_groups,
        last_refresh_at,
        updated_at
    ) VALUES (
        reloid,
        safe_upper_xid,
        generation_after,
        coalesce(rebuilt_rows, 0),
        coalesce(row_groups_written, 0),
        clock_timestamp(),
        clock_timestamp()
    )
    ON CONFLICT (table_oid) DO UPDATE
       SET last_refresh_xid = EXCLUDED.last_refresh_xid,
           last_refresh_generation = EXCLUDED.last_refresh_generation,
           last_refresh_rows = EXCLUDED.last_refresh_rows,
           last_refresh_row_groups = EXCLUDED.last_refresh_row_groups,
           last_refresh_at = EXCLUDED.last_refresh_at,
           updated_at = EXCLUDED.updated_at;

    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );

    UPDATE rvbbit.acceleration_operations
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = rebuilt_rows,
           row_groups_written = accel_rebuild.row_groups_written,
           variants_rows = accel_rebuild.variants_rows,
           generation_after = accel_rebuild.generation_after,
           settings = settings || jsonb_build_object('dropped_row_groups', dropped_rgs)
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', 'ok',
        'operation_id', op_id,
        'table', table_name_text,
        'operation', 'rebuild_acceleration',
        'dropped_row_groups', dropped_rgs,
        'rows_written', rebuilt_rows,
        'row_groups_written', row_groups_written,
        'variants_rows', variants_rows,
        'generation_after', generation_after,
        'watermark_after', safe_upper_xid
    );
EXCEPTION WHEN OTHERS THEN
    IF op_id IS NOT NULL THEN
        UPDATE rvbbit.acceleration_operation_phases
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE operation_id = op_id
           AND status = 'running';
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE id = op_id;
    END IF;
    RAISE;
END $$;

-- Shreds: typed parquet columns that materialize a Postgres expression
-- against the source row's data. Populated by rvbbit.compact() when it
-- knows about extractable paths (Phase 4 hardcodes the LLM shreds; future
-- versions accept a user config).
--
-- Both the read path (transparent SELECT) and a future rewriter hook
-- consume this catalog. The rewriter walks the Query tree, finds
-- expressions matching `source_expr`, and substitutes a Var pointing at
-- the shredded column — so `response->>'stop_reason'` silently becomes a
-- typed column read with no query change required.
-- Semantic operator registry. Each row is a complete "mini workflow"
-- declaration that the generic Rust executor consumes. Editable in
-- place — UPDATE rvbbit.operators SET user_prompt = '...' takes effect
-- on the next call, no recompile needed.
--
-- The Rust side knows how to execute a row of this table; it doesn't
-- hardcode any prompts. Users can edit built-ins or create entirely
-- new operators via rvbbit.create_operator(...).
CREATE TABLE rvbbit.operators (
    name           text PRIMARY KEY,                -- 'means', 'about', user-defined names
    -- SHAPE drives the executor's iteration model — this is load-bearing
    -- for the parallel/batching design and MUST be set right at creation
    -- time. Inspired by Lars:
    --   scalar    : one LLM call per row. Parallel = thread pool over rows.
    --   aggregate : ONE LLM call sees ALL rows in the group (CLASSIFY_LLM).
    --               PG aggregate (sfunc + ffunc) underneath.
    --   dimension : ONE LLM call sees the collection and returns per-input
    --               assignments (TOPICS_LLM). Materialize-and-distribute model.
    shape          text NOT NULL DEFAULT 'scalar',
    arg_names      text[] NOT NULL,                 -- {'text','criterion'} — template var names
    arg_types      text[] NOT NULL,                 -- {'text','text'} — for now all text; future ints/jsonb
    return_type    text NOT NULL,                   -- 'bool' | 'text' | 'float8' | 'jsonb'
    model          text NOT NULL,                   -- e.g. 'openai/gpt-5.4-mini'
    system_prompt  text NOT NULL,                   -- system message
    user_prompt    text NOT NULL,                   -- user message template; uses {{arg_name}}
    parser         text NOT NULL,                   -- 'yes_no' | 'score_0_1' | 'raw_text' | 'strip'
    max_tokens     int NOT NULL DEFAULT 256,
    temperature    real,                            -- nullable; defaults to provider default
    cache_policy   text NOT NULL DEFAULT 'memoize', -- 'memoize' | 'always' | 'never'
    opts_default   jsonb NOT NULL DEFAULT '{}'::jsonb,  -- merged with per-call opts
    infix_symbol   text,                            -- e.g. '~~?' (PG operator chars only)
    infix_word     text,                            -- e.g. 'MEANS' (future text rewriter)
    -- Self-tests for the operator. Array of {sql, expect:{type, value|pattern}, description}.
    -- Run via rvbbit.run_tests(operator_name).
    tests          jsonb,
    -- Multi-step pipeline. When NULL the operator is single-LLM-call
    -- (system_prompt + user_prompt + parser, today's path — backward
    -- compatible). When non-NULL, the executor iterates the array, each
    -- entry is one step with `kind` in {'llm', 'code', 'python', 'specialist'}.
    -- The output of each step is available to subsequent steps as
    -- {{ steps.<step_name>.<field> }} in templates.
    --
    -- Example: TOPICS-style cascade
    --   [
    --     {"name":"embed",    "kind":"specialist", "specialist":"bge-m3",
    --      "inputs":{"texts":"{{ inputs.texts }}"}, "output_var":"vectors"},
    --     {"name":"cluster",  "kind":"code", "fn":"kmeans_cluster",
    --      "inputs":{"vectors":"{{ steps.embed.vectors }}",
    --                "k":"{{ inputs.num_topics }}"}},
    --     {"name":"name",     "kind":"llm", "model":"haiku",
    --      "system":"...", "user":"..."}
    --   ]
    steps          jsonb,
    -- Operator-level retry plan (Loop 16). NULL = run once. Shape:
    --   {"until": <validator>, "max_attempts": int, "instructions": text}
    -- where <validator> is {"sql":"<bool expr>"} | {"function":"schema.fn"}
    -- | "fn_name". $output (the raw output text) and $inputs (the inputs
    -- jsonb) are bound inside a SQL validator. The operator re-runs, with
    -- `instructions` appended to the prompt, until the validator passes.
    retry          jsonb,
    -- Pre/post validator gates (Loop 17). {"pre":[...],"post":[...]} where
    -- each ward is {"validator": <ref>, "mode": "blocking"|"advisory"}.
    -- pre-wards gate the inputs; post-wards gate the final output.
    wards          jsonb,
    -- Multi-take plan (Loop 18). {"factor": int, "models": [...],
    -- "reduce": "vote"|"first_valid"|"evaluator", "filter": <validator>,
    -- "evaluator": {"model": text, "instructions": text}}. Runs the
    -- operator N times and reduces the takes to one answer.
    takes          jsonb,
    description    text,                            -- human-readable docs
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (shape IN ('scalar', 'aggregate', 'dimension', 'rowset', 'query')),
    CHECK (cardinality(arg_names) = cardinality(arg_types)),
    CHECK (return_type IN ('bool', 'text', 'float8', 'jsonb')),
    CHECK (parser IN ('yes_no', 'score_0_1', 'raw_text', 'strip', 'json', 'sql')),
    CHECK (infix_symbol IS NULL OR cardinality(arg_names) = 2),
    -- Infix only makes sense for per-row evaluation.
    CHECK (infix_symbol IS NULL OR shape = 'scalar')
);

-- Bump updated_at on every UPDATE so users can see when prompts changed.
CREATE OR REPLACE FUNCTION rvbbit.touch_operators_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER operators_touch_updated_at
    BEFORE UPDATE ON rvbbit.operators
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_operators_updated_at();

-- DDL helper: create a new operator AND a SQL wrapper function in one call.
-- The wrapper dispatches to rvbbit._exec_op_<return_type> with named args
-- bundled into JSONB so the executor can render the prompt template.
CREATE OR REPLACE FUNCTION rvbbit.create_operator(
    op_name        text,
    op_arg_names   text[],
    op_return_type text,
    -- Prompts default to '' so a steps-only operator (specialist / python / code
    -- nodes, no LLM) needs no prompt boilerplate. They are used only when
    -- op_steps is NULL (the single-LLM-call path).
    op_system      text DEFAULT '',
    op_user        text DEFAULT '',
    op_shape       text DEFAULT 'scalar',         -- scalar | aggregate | dimension | rowset
    op_model       text DEFAULT 'openai/gpt-5.4-mini',
    op_parser      text DEFAULT NULL,            -- auto: yes_no/strip/score_0_1 by return_type
    op_max_tokens  int  DEFAULT 256,
    op_temperature real DEFAULT NULL,
    op_arg_types   text[] DEFAULT NULL,          -- defaults to all 'text'
    op_description text DEFAULT NULL,
    op_infix_symbol text DEFAULT NULL,           -- PG operator symbol (binary scalar ops only)
    op_infix_word   text DEFAULT NULL,           -- future: text-name rewriter
    op_tests        jsonb DEFAULT NULL,          -- [{sql, expect:{type, value|pattern}, ...}]
    op_steps        jsonb DEFAULT NULL           -- multi-step pipeline (NULL = single LLM call)
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    actual_parser    text;
    actual_arg_types text[];
    exec_fn          text;
    wrapper_args_with_opts text;
    wrapper_args_no_opts   text;
    wrapper_inputs   text;
    n_args           int;
BEGIN
    n_args := cardinality(op_arg_names);
    actual_arg_types := COALESCE(op_arg_types,
        ARRAY(SELECT 'text' FROM generate_series(1, n_args)));
    actual_parser := COALESCE(op_parser, CASE op_return_type
        WHEN 'bool'   THEN 'yes_no'
        WHEN 'float8' THEN 'score_0_1'
        WHEN 'jsonb'  THEN 'json'
        ELSE 'strip'
    END);

    INSERT INTO rvbbit.operators
        (name, shape, arg_names, arg_types, return_type, model, system_prompt, user_prompt,
         parser, max_tokens, temperature, description, infix_symbol, infix_word, tests, steps)
    VALUES
        (op_name, op_shape, op_arg_names, actual_arg_types, op_return_type, op_model,
         op_system, op_user, actual_parser, op_max_tokens, op_temperature, op_description,
         op_infix_symbol, op_infix_word, op_tests, op_steps)
    ON CONFLICT (name) DO UPDATE SET
        shape = EXCLUDED.shape,
        arg_names = EXCLUDED.arg_names,
        arg_types = EXCLUDED.arg_types,
        return_type = EXCLUDED.return_type,
        model = EXCLUDED.model,
        system_prompt = EXCLUDED.system_prompt,
        user_prompt = EXCLUDED.user_prompt,
        parser = EXCLUDED.parser,
        max_tokens = EXCLUDED.max_tokens,
        temperature = EXCLUDED.temperature,
        description = EXCLUDED.description,
        infix_symbol = EXCLUDED.infix_symbol,
        infix_word = EXCLUDED.infix_word,
        tests = EXCLUDED.tests,
        steps = EXCLUDED.steps;

    -- Branch by shape. Scalar = thin wrapper around _exec_op_<type>.
    -- Dimension = wrapper RETURNS SETOF <type> calling _dim_exec_<type>.
    -- Aggregate = generated SFUNC + FFUNC + CREATE AGGREGATE binding
    -- the op name into the per-op SQL wrappers (the SFUNC/FFUNC
    -- themselves dispatch to the generic _agg_* helpers in Rust).

    IF op_shape = 'dimension' THEN
        exec_fn := '_dim_exec_' || op_return_type;

        wrapper_inputs := 'jsonb_build_object(' || array_to_string(
            ARRAY(SELECT format('%L, $%s', a, i)
                  FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
            ', '
        ) || ')';

        wrapper_args_with_opts := array_to_string(
            ARRAY(SELECT format('%I %s', a, t)
                  FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
            ', '
        ) || ', opts jsonb DEFAULT ''{}''::jsonb';

        -- SETOF wrapper: SELECT * FROM rvbbit._dim_exec_<type>(...)
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS SETOF %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT * FROM rvbbit.%I(%L, %s, $%s) $wb$',
            op_name, wrapper_args_with_opts, op_return_type,
            exec_fn, op_name, wrapper_inputs, n_args + 1
        );
        RETURN;
    END IF;

    IF op_shape IN ('rowset', 'query') THEN
        -- Rowset operators (pipeline cascade stages) take a whole resultset and
        -- return a new one; query operators (shape='query', parser='sql') take a
        -- natural-language intent and author a SELECT over the live DB. Both are
        -- dispatched through Rust (run_rowset_op / rvbbit.synth_sql), so the catalog
        -- row inserted above is all that's needed -- no per-operator SQL wrapper.
        RETURN;
    END IF;

    IF op_shape = 'aggregate' THEN
        -- SFUNC: state jsonb + per-row args + opts → state jsonb.
        -- Body calls rvbbit._agg_append_state to push this row's
        -- inputs jsonb onto state.collection.
        wrapper_inputs := 'jsonb_build_object(' || array_to_string(
            ARRAY(SELECT format('%L, $%s', a, i + 1)  -- +1 because state is $1
                  FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
            ', '
        ) || ')';
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb, %s, opts jsonb DEFAULT ''{}''::jsonb) RETURNS jsonb LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit._agg_append_state(state, %s) $wb$',
            '_agg_' || op_name || '_sfunc',
            array_to_string(
                ARRAY(SELECT format('%I %s', a, t)
                      FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
                ', '
            ),
            wrapper_inputs
        );

        -- FFUNC: state jsonb → return_type. Body calls
        -- rvbbit._agg_run_op_<type>(op_name, state).
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb) RETURNS %s LANGUAGE sql PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, state) $wb$',
            '_agg_' || op_name || '_ffunc',
            op_return_type,
            '_agg_run_op_' || op_return_type,
            op_name
        );

        -- CREATE AGGREGATE. Drop first so re-registration works.
        EXECUTE format('DROP AGGREGATE IF EXISTS rvbbit.%I(%s, jsonb)',
            op_name,
            array_to_string(actual_arg_types, ', ')
        );
        EXECUTE format(
            'CREATE AGGREGATE rvbbit.%I(%s, jsonb) (SFUNC = rvbbit.%I, STYPE = jsonb, INITCOND = ''{}'', FINALFUNC = rvbbit.%I)',
            op_name,
            array_to_string(actual_arg_types, ', '),
            '_agg_' || op_name || '_sfunc',
            '_agg_' || op_name || '_ffunc'
        );

        -- Also create a no-opts wrapper so users can call without the
        -- trailing jsonb. PG aggregates can't have DEFAULT on direct
        -- args, so we register a second aggregate with one fewer arg.
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb, %s) RETURNS jsonb LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit._agg_append_state(state, %s) $wb$',
            '_agg_' || op_name || '_sfunc_no_opts',
            array_to_string(
                ARRAY(SELECT format('%I %s', a, t)
                      FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
                ', '
            ),
            wrapper_inputs
        );
        EXECUTE format('DROP AGGREGATE IF EXISTS rvbbit.%I(%s)',
            op_name,
            array_to_string(actual_arg_types, ', ')
        );
        EXECUTE format(
            'CREATE AGGREGATE rvbbit.%I(%s) (SFUNC = rvbbit.%I, STYPE = jsonb, INITCOND = ''{}'', FINALFUNC = rvbbit.%I)',
            op_name,
            array_to_string(actual_arg_types, ', '),
            '_agg_' || op_name || '_sfunc_no_opts',
            '_agg_' || op_name || '_ffunc'
        );
        RETURN;
    END IF;

    -- Default: scalar.
    exec_fn := '_exec_op_' || op_return_type;

    -- Build the JSONB inputs object (named args).
    wrapper_inputs := 'jsonb_build_object(' || array_to_string(
        ARRAY(SELECT format('%L, $%s', a, i)
              FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
        ', '
    ) || ')';

    -- Args list WITH trailing opts JSONB (user-facing variant). Guard the
    -- zero-arg case (e.g. an MCP tool that takes no inputs) so we don't emit a
    -- leading comma: "(, opts jsonb …)".
    wrapper_args_with_opts := nullif(array_to_string(
        ARRAY(SELECT format('%I %s', a, t) FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
        ', '
    ), '');
    wrapper_args_with_opts := CASE
        WHEN wrapper_args_with_opts IS NULL THEN 'opts jsonb DEFAULT ''{}''::jsonb'
        ELSE wrapper_args_with_opts || ', opts jsonb DEFAULT ''{}''::jsonb'
    END;

    -- (n_args+1)-arg wrapper: full opts surface.
    -- PARALLEL SAFE on the SQL wrapper is what makes PG actually consider
    -- parallel workers for queries like SELECT rvbbit.X(...) FROM big_table.
    -- Without it, SQL functions default to PARALLEL UNSAFE and the
    -- planner picks a serial plan no matter what max_parallel_workers
    -- says. The underlying _exec_op_* Rust functions are #[pg_extern(parallel_safe)]
    -- so we can safely propagate the marker.
    EXECUTE format(
        'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, %s, $%s) $wb$',
        op_name, wrapper_args_with_opts, op_return_type, exec_fn, op_name, wrapper_inputs, n_args + 1
    );

    IF n_args = 2 THEN
        wrapper_args_no_opts := array_to_string(
            ARRAY(SELECT format('%I %s', a, t)
                  FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
            ', '
        );
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, %s, ''{}''::jsonb) $wb$',
            '_op_' || op_name, wrapper_args_no_opts, op_return_type,
            exec_fn, op_name, wrapper_inputs
        );

        IF op_infix_symbol IS NOT NULL THEN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_operator op
                WHERE op.oprnamespace = 'rvbbit'::regnamespace
                  AND op.oprname = op_infix_symbol
                  AND op.oprleft = actual_arg_types[1]::regtype
                  AND op.oprright = actual_arg_types[2]::regtype
            ) THEN
                EXECUTE format(
                    'CREATE OPERATOR rvbbit.%s (LEFTARG = %s, RIGHTARG = %s, FUNCTION = rvbbit.%I)',
                    op_infix_symbol, actual_arg_types[1], actual_arg_types[2],
                    '_op_' || op_name
                );
            END IF;
        END IF;
    END IF;
END $$;

-- rvbbit.run_tests(operator_name) — run every embedded test case for one
-- operator and return per-test pass/fail. Lars-inspired: tests live in the
-- catalog row, run via SQL, results are queryable.
--
-- Supported expect.type values:
--   exact     : value must equal expect.value  (text/bool/numeric)
--   contains  : (text only) expect.value is a substring of the result
--   regex     : (text only) result matches expect.pattern
--   min       : (numeric) result >= expect.value
--   max       : (numeric) result <= expect.value
--   not_empty : result is non-NULL and length > 0
CREATE OR REPLACE FUNCTION rvbbit.run_tests(operator_name text)
RETURNS TABLE (
    test_name   text,
    passed      boolean,
    actual      text,
    expected    text,
    description text,
    error       text
) LANGUAGE plpgsql AS $$
DECLARE
    op_row    record;
    test_case jsonb;
    actual_text text;
    expect_type text;
    expect_val  text;
    expect_pat  text;
    test_ok   boolean;
    err_msg   text;
BEGIN
    SELECT * INTO op_row FROM rvbbit.operators WHERE name = operator_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.run_tests: operator % not found', operator_name;
    END IF;
    IF op_row.tests IS NULL OR jsonb_array_length(op_row.tests) = 0 THEN
        RETURN;
    END IF;

    FOR test_case IN SELECT jsonb_array_elements(op_row.tests) LOOP
        test_name   := COALESCE(test_case->>'name', '<unnamed>');
        description := test_case->>'description';
        expect_type := test_case->'expect'->>'type';
        expect_val  := test_case->'expect'->>'value';
        expect_pat  := test_case->'expect'->>'pattern';
        actual_text := NULL;
        test_ok     := false;
        error       := NULL;
        expected    := COALESCE(expect_val, expect_pat, expect_type);

        BEGIN
            EXECUTE test_case->>'sql' INTO actual_text;
        EXCEPTION WHEN OTHERS THEN
            actual := NULL;
            passed := false;
            error  := SQLERRM;
            RETURN NEXT;
            CONTINUE;
        END;
        actual := actual_text;

        test_ok := CASE expect_type
            WHEN 'exact'     THEN actual_text IS NOT DISTINCT FROM expect_val
            WHEN 'contains'  THEN actual_text IS NOT NULL AND position(expect_val IN actual_text) > 0
            WHEN 'regex'     THEN actual_text IS NOT NULL AND actual_text ~ expect_pat
            WHEN 'min'       THEN actual_text IS NOT NULL AND actual_text::numeric >= expect_val::numeric
            WHEN 'max'       THEN actual_text IS NOT NULL AND actual_text::numeric <= expect_val::numeric
            WHEN 'not_empty' THEN actual_text IS NOT NULL AND length(actual_text) > 0
            ELSE false
        END;
        passed := test_ok;
        RETURN NEXT;
    END LOOP;
END $$;

-- Convenience: run tests for ALL operators that have them.
CREATE OR REPLACE FUNCTION rvbbit.run_all_tests()
RETURNS TABLE (
    operator    text,
    test_name   text,
    passed      boolean,
    actual      text,
    expected    text,
    description text,
    error       text
) LANGUAGE sql AS $$
    SELECT o.name, t.test_name, t.passed, t.actual, t.expected, t.description, t.error
    FROM rvbbit.operators o,
         LATERAL rvbbit.run_tests(o.name) t
    WHERE o.tests IS NOT NULL AND jsonb_array_length(o.tests) > 0
    ORDER BY o.name, t.test_name;
$$;

-- DDL helper: attach (or clear) an operator-level retry plan. Loop 16.
-- A retry plan loops the operator until its output passes a validator:
--   SELECT rvbbit.set_operator_retry('classify', jsonb_build_object(
--       'until',        jsonb_build_object('sql',
--           '$output = ANY(string_to_array($inputs->>''categories'','',''))'),
--       'max_attempts', 3,
--       'instructions', 'Answer {{ output }} was not a listed category. '
--                       'Return exactly one of: {{ inputs.categories }}'));
-- Pass NULL to remove the plan. The validator is one of:
--   {"sql": "<boolean expression>"}   -- $output / $inputs bound
--   {"function": "schema.fn"}          -- fn(output text, inputs jsonb) -> bool
--   "fn_name"                          -- shorthand for {"function": ...}
CREATE OR REPLACE FUNCTION rvbbit.set_operator_retry(
    op_name      text,
    retry_config jsonb
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF retry_config IS NOT NULL THEN
        IF jsonb_typeof(retry_config) <> 'object' THEN
            RAISE EXCEPTION 'rvbbit.set_operator_retry: retry_config must be a JSON object';
        END IF;
        IF retry_config->'until' IS NULL THEN
            RAISE EXCEPTION 'rvbbit.set_operator_retry: retry_config needs an "until" validator';
        END IF;
    END IF;
    UPDATE rvbbit.operators SET retry = retry_config WHERE name = op_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_operator_retry: unknown operator %', op_name;
    END IF;
END $$;

-- DDL helper: attach (or clear) pre/post validator gates. Loop 17.
--   SELECT rvbbit.set_operator_wards('classify', jsonb_build_object(
--       'pre',  jsonb_build_array(jsonb_build_object(
--           'validator', jsonb_build_object('sql','length($inputs->>''text'')>0'),
--           'mode', 'blocking')),
--       'post', jsonb_build_array(jsonb_build_object(
--           'validator', jsonb_build_object('sql','$output <> ''''')),
--           'mode', 'advisory'))));
-- Pass NULL to remove all wards.
CREATE OR REPLACE FUNCTION rvbbit.set_operator_wards(
    op_name      text,
    wards_config jsonb
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF wards_config IS NOT NULL AND jsonb_typeof(wards_config) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.set_operator_wards: wards_config must be a JSON object';
    END IF;
    UPDATE rvbbit.operators SET wards = wards_config WHERE name = op_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_operator_wards: unknown operator %', op_name;
    END IF;
END $$;

-- DDL helper: attach (or clear) a multi-take plan. Loop 18.
--   SELECT rvbbit.set_operator_takes('classify', jsonb_build_object(
--       'factor', 3,
--       'models', jsonb_build_array('openai/gpt-5.4-mini',
--                                   'openai/gpt-4o-mini'),
--       'reduce', 'vote'));
-- reduce is 'vote' | 'first_valid' | 'evaluator'. Pass NULL to remove.
CREATE OR REPLACE FUNCTION rvbbit.set_operator_takes(
    op_name      text,
    takes_config jsonb
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF takes_config IS NOT NULL THEN
        IF jsonb_typeof(takes_config) <> 'object' THEN
            RAISE EXCEPTION 'rvbbit.set_operator_takes: takes_config must be a JSON object';
        END IF;
        IF takes_config->'factor' IS NULL AND takes_config->'nodes' IS NULL THEN
            RAISE EXCEPTION 'rvbbit.set_operator_takes: takes_config needs a "factor" (homogeneous takes) or "nodes" (heterogeneous takes)';
        END IF;
    END IF;
    UPDATE rvbbit.operators SET takes = takes_config WHERE name = op_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_operator_takes: unknown operator %', op_name;
    END IF;
END $$;

-- Seed the three built-in operators. These are EDITABLE — users can
--   UPDATE rvbbit.operators SET system_prompt = '...new...' WHERE name='MEANS';
-- and the change takes effect on the next call. The rvbbit.create_operator()
-- DDL helper above is used so each built-in also gets a typed SQL wrapper
-- (rvbbit.means / rvbbit.about / rvbbit.summarize) auto-generated.
DO $$
BEGIN
    PERFORM rvbbit.create_operator(
        op_name => 'means',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text', 'criterion'],
        op_return_type => 'bool',
        op_system =>
            'You are a precise binary classifier. Given a TEXT and a CRITERION, decide ' ||
            'whether the TEXT expresses the CRITERION. Respond with ONLY one word — ' ||
            'either YES or NO — and nothing else.',
        op_user =>
            E'CRITERION: {{ criterion }}\n\nTEXT: {{ text }}\n\nDoes TEXT express CRITERION?',
        op_max_tokens => 16,
        op_description => 'Binary semantic match: does TEXT express CRITERION?',
        op_infix_symbol => '~~?',
        op_infix_word => 'MEANS',
        op_tests => $jt$[
            {
                "name": "obvious_positive",
                "sql": "SELECT rvbbit.means('Worst experience ever, cancelling immediately and disputing the charge', 'angry customer who wants to cancel')",
                "expect": {"type": "exact", "value": true},
                "description": "Clearly angry cancellation -> true"
            },
            {
                "name": "obvious_negative",
                "sql": "SELECT rvbbit.means('Just wanted to say thanks for the great service', 'angry customer who wants to cancel')",
                "expect": {"type": "exact", "value": false},
                "description": "Compliment -> false"
            }
        ]$jt$::jsonb
    );

    PERFORM rvbbit.create_operator(
        op_name => 'about',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text', 'topic'],
        op_return_type => 'float8',
        op_system =>
            'You are a precise relevance scorer. Rate how relevant the TEXT is to ' ||
            'the TOPIC on a scale from 0.0 (not relevant at all) to 1.0 (entirely about it). ' ||
            'Respond with ONLY the number — for example "0.73" — and nothing else.',
        op_user =>
            E'TOPIC: {{ topic }}\n\nTEXT: {{ text }}\n\nRelevance score (0.0 to 1.0):',
        op_max_tokens => 16,
        op_description => 'Relevance score in [0,1] of TEXT to TOPIC.',
        op_infix_symbol => '~~%',
        op_infix_word => 'ABOUT',
        op_tests => $jt$[
            {
                "name": "strong_relevance",
                "sql": "SELECT rvbbit.about('My credit card was charged twice this month and I need a refund', 'billing problems')",
                "expect": {"type": "min", "value": 0.6},
                "description": "Billing dispute -> >= 0.6 billing relevance"
            },
            {
                "name": "no_relevance",
                "sql": "SELECT rvbbit.about('Loving the new dashboard, way more useful than before', 'billing problems')",
                "expect": {"type": "max", "value": 0.3},
                "description": "Unrelated -> <= 0.3"
            }
        ]$jt$::jsonb
    );

    PERFORM rvbbit.create_operator(
        op_name => 'summarize',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_system =>
            'You are a concise summarizer. Summarize the given TEXT in ONE short sentence. ' ||
            'Output ONLY the sentence — no preamble, no quotes, no markdown.',
        op_user =>
            E'TEXT: {{ text }}\n\nOne-sentence summary:',
        op_max_tokens => 120,
        op_description => 'One-sentence summary of TEXT.',
        op_tests => $jt$[
            {
                "name": "length_bounds",
                "sql": "SELECT rvbbit.summarize('The quarterly report shows fifteen percent revenue growth driven by international markets, with North America declining slightly and Asia leading at thirty percent growth')",
                "expect": {"type": "regex", "pattern": ".{20,250}"},
                "description": "Summary is 20-250 chars (PG regex caps repetition at 255)"
            }
        ]$jt$::jsonb
    );

    -- Multi-step DEMO: safe_classify
    --   step 1 (llm)  : ask the LLM to classify into one of the categories
    --   step 2 (code) : validate_one_of — clamp the result to the allowed list
    -- Two sub-calls, one operator, one receipt. Demonstrates step
    -- composition (LLM + code) and the templating chain (step.<n>.output
    -- referenced from later step's inputs).
    PERFORM rvbbit.create_operator(
        op_name => 'safe_classify',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text', 'categories'],
        op_return_type => 'text',
        op_system => 'unused — overridden per step',
        op_user => 'unused — overridden per step',
        op_max_tokens => 160,
        op_description =>
            'Classify TEXT into one of CATEGORIES (comma-separated). ' ||
            'Two-step: LLM proposes a category, then code validates the answer ' ||
            'is one of the allowed values (falls back to ''unknown'' if not).',
        op_steps => $st$[
            {
                "name": "classify",
                "kind": "llm",
                "system": "You are a precise text classifier. Reply with exactly one of the provided categories, copied verbatim. No quotes, no explanation.",
                "user": "CATEGORIES: {{ inputs.categories }}\n\nTEXT: {{ inputs.text }}\n\nThe single best category from the list above:",
                "max_tokens": 16
            },
            {
                "name": "validate",
                "kind": "code",
                "fn": "validate_one_of",
                "inputs": {
                    "value": "{{ steps.classify.output }}",
                    "allowed": "{{ inputs.categories }}",
                    "default": "unknown"
                }
            }
        ]$st$::jsonb,
        op_tests => $jt$[
            {
                "name": "obvious_classification",
                "sql": "SELECT rvbbit.safe_classify('I was charged twice this month, please refund the duplicate payment', 'billing,shipping,bug-report,other')",
                "expect": {"type": "exact", "value": "billing"},
                "description": "Duplicate charge / refund -> billing"
            },
            {
                "name": "in_allowed_set",
                "sql": "SELECT rvbbit.safe_classify('Where is my package?', 'billing,shipping,other') IN ('billing','shipping','other')",
                "expect": {"type": "exact", "value": "true"},
                "description": "Result is always one of the allowed categories (validator clamps any rogue output)"
            }
        ]$jt$::jsonb
    );

    -- AGGREGATE placeholder: catalog declaration only (no executor yet).
    -- Lands fully when we tackle parallel/batching. Demonstrates that the
    -- shape column accepts non-scalar values and that the executor will
    -- pick a different code path.
    PERFORM rvbbit.create_operator(
        op_name => 'classify_collection',
        op_shape => 'aggregate',
        op_arg_names => ARRAY['text', 'categories'],
        op_return_type => 'text',
        op_system =>
            'Classify this collection of texts into ONE of the provided categories. ' ||
            'Consider the overall theme. Return ONLY the category name, exactly as provided.',
        op_user =>
            E'TEXTS:\n{{ text }}\n\nCATEGORIES: {{ categories }}\n\nWhich category best describes this collection as a whole?',
        op_max_tokens => 240,
        op_description => 'AGGREGATE: classify a collection of texts as one of the given categories. ' ||
                          'Executor not yet implemented (calls will error until aggregate shape lands).'
    );

    -- ---- Tier A semantic operator bundle (RYR-303) ----------------------
    -- LLM-path scalar operators shipped with every install. A fresh
    -- CREATE EXTENSION must include these — they used to live only in
    -- the 0.9.0->0.10.0 upgrade migration, so a clean install (or an
    -- extension recreated by a benchmark harness) was missing them.
    -- wire-operators-to-specialists.sql can later flip each operator's
    -- `steps` to route through a GPU specialist instead of the LLM.

    PERFORM rvbbit.create_operator(
        op_name => 'classify',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text', 'categories'],
        op_return_type => 'text',
        op_system =>
            'You are a strict classifier. Given a TEXT and a comma-separated ' ||
            'list of CATEGORIES, return ONLY the single category name that ' ||
            'best matches the TEXT. Use the exact spelling from the list. ' ||
            'No explanation, no quotes, just the category name.',
        op_user =>
            E'CATEGORIES: {{ categories }}\n\nTEXT: {{ text }}\n\nBest category:',
        op_max_tokens => 320,
        op_description => 'Classify text into ONE of the comma-separated CATEGORIES.',
        op_parser => 'strip'
    );

    PERFORM rvbbit.create_operator(
        op_name => 'extract',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text', 'what'],
        op_return_type => 'text',
        op_system =>
            'You are a precise information extractor. Given a TEXT and a ' ||
            'description WHAT of the value to find, return ONLY the literal ' ||
            'value from the text. If the value is not present, return ' ||
            'exactly: NULL. No explanation, no quotes, no surrounding text.',
        op_user =>
            E'TEXT: {{ text }}\n\nWHAT: {{ what }}\n\nExtracted value:',
        op_max_tokens => 640,
        op_description => 'Extract a specific value WHAT from TEXT (or NULL).',
        op_parser => 'strip'
    );

    PERFORM rvbbit.create_operator(
        op_name => 'condense',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_system =>
            'You are a concise summarizer. Return a 1-3 sentence summary ' ||
            'of the TEXT. Preserve the most important facts, names, and ' ||
            'numbers. Use plain prose — no bullet points, no preamble like ' ||
            '"Here is a summary".',
        op_user =>
            E'TEXT: {{ text }}\n\nSummary:',
        op_max_tokens => 200,
        op_description => 'Condense TEXT into a 1-3 sentence summary (scalar, per-row).',
        op_parser => 'strip'
    );

    PERFORM rvbbit.create_operator(
        op_name => 'sentiment',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_system =>
            'You are a sentiment classifier. Return ONLY one of: ' ||
            'positive, negative, neutral, mixed. Use lowercase. No ' ||
            'explanation, no period, just the label.',
        op_user =>
            E'TEXT: {{ text }}\n\nSentiment:',
        op_max_tokens => 16,
        op_description => 'Sentiment label: positive | negative | neutral | mixed.',
        op_parser => 'strip'
    );

    PERFORM rvbbit.create_operator(
        op_name => 'contradicts',
        op_shape => 'scalar',
        op_arg_names => ARRAY['a', 'b'],
        op_return_type => 'bool',
        op_system =>
            'You are a strict logical relation classifier. Given two ' ||
            'statements A and B, decide whether A directly CONTRADICTS B. ' ||
            'They contradict if both cannot be true at the same time. ' ||
            'Respond ONLY with YES or NO.',
        op_user =>
            E'A: {{ a }}\n\nB: {{ b }}\n\nDoes A contradict B?',
        op_max_tokens => 16,
        op_description => 'Does statement A contradict statement B?',
        op_parser => 'yes_no'
    );

    PERFORM rvbbit.create_operator(
        op_name => 'supports',
        op_shape => 'scalar',
        op_arg_names => ARRAY['a', 'b'],
        op_return_type => 'bool',
        op_system =>
            'You are a strict logical relation classifier. Given two ' ||
            'statements A and B, decide whether A provides direct evidence ' ||
            'SUPPORTING B. Respond ONLY with YES or NO.',
        op_user =>
            E'A: {{ a }}\n\nB: {{ b }}\n\nDoes A support B?',
        op_max_tokens => 16,
        op_description => 'Does statement A support statement B?',
        op_parser => 'yes_no'
    );

    PERFORM rvbbit.create_operator(
        op_name => 'implies',
        op_shape => 'scalar',
        op_arg_names => ARRAY['a', 'b'],
        op_return_type => 'bool',
        op_system =>
            'You are a strict logical relation classifier. Given two ' ||
            'statements A and B, decide whether A logically IMPLIES B ' ||
            '(if A is true, B must also be true). Respond ONLY with YES or NO.',
        op_user =>
            E'A: {{ a }}\n\nB: {{ b }}\n\nDoes A imply B?',
        op_max_tokens => 16,
        op_description => 'Does statement A logically imply statement B?',
        op_parser => 'yes_no'
    );

    -- ---- Flow-feature built-ins (Loop 19) -------------------------------
    -- Each showcases one semantic-flow feature and is a genuinely useful
    -- built-in: clean_year (retry), redact (post-ward), headline (takes).

    -- clean_year — retry-validated 4-digit year extraction.
    PERFORM rvbbit.create_operator(
        op_name => 'clean_year',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_system =>
            'You extract the calendar year an event took place from messy ' ||
            'text. Respond with ONLY a 4-digit year such as 1997. Expand ' ||
            'two-digit years (97 becomes 1997, 05 becomes 2005). If the ' ||
            'text states no year at all, respond with exactly: unknown',
        op_user => E'TEXT: {{ text }}\n\nYear:',
        op_max_tokens => 120,
        op_temperature => 0.0,
        op_description => 'Extract a clean 4-digit year from messy text (retry-validated).',
        op_parser => 'strip'
    );
    PERFORM rvbbit.set_operator_retry('clean_year',
        $cfg${"until":{"sql":"btrim($output) ~ '^((1[6-9]|20)[0-9]{2}|unknown)$'"},"max_attempts":3,"instructions":"Your previous answer was not a valid year. Respond with ONLY a 4-digit year such as 1997, or exactly the word: unknown"}$cfg$::jsonb);

    -- redact — strip PII; a blocking post-ward rejects any leaked email.
    PERFORM rvbbit.create_operator(
        op_name => 'redact',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_system =>
            'You remove personally identifying information from text. ' ||
            'Replace each person name with [NAME], email address with ' ||
            '[EMAIL], phone number with [PHONE], street address with ' ||
            '[ADDRESS], and government id number with [ID]. Leave place ' ||
            'names such as cities, counties and states intact. Return ONLY ' ||
            'the rewritten text, preserving all other wording.',
        op_user => E'TEXT: {{ text }}\n\nRedacted text:',
        op_max_tokens => 1024,
        op_temperature => 0.0,
        op_description => 'Strip PII from text; post-ward rejects output that still contains an email.',
        op_parser => 'strip'
    );
    PERFORM rvbbit.set_operator_wards('redact',
        $cfg${"post":[{"validator":{"sql":"$output !~ '[A-Za-z0-9._%+-]+@[A-Za-z0-9._-]+'"},"mode":"blocking"}]}$cfg$::jsonb);

    -- headline — 3 takes, an LLM evaluator picks the punchiest.
    PERFORM rvbbit.create_operator(
        op_name => 'headline',
        op_shape => 'scalar',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_system =>
            'You write one short, punchy headline that captures the single ' ||
            'most striking thing in the TEXT. Under 12 words. No quotation ' ||
            'marks, no trailing period. Return ONLY the headline.',
        op_user => E'TEXT: {{ text }}\n\nHeadline:',
        op_max_tokens => 320,
        op_temperature => 0.8,
        op_description => 'Generate a punchy headline; 3 takes, an LLM evaluator picks the best.',
        op_parser => 'strip'
    );
    PERFORM rvbbit.set_operator_takes('headline',
        $cfg${"factor":3,"reduce":"evaluator","evaluator":{"instructions":"Pick the headline that is the most vivid and specific while staying accurate to the text. Reply with only its number."}}$cfg$::jsonb);

    -- Dimension (fan-out) operators: one text -> a SET of canonical labels
    -- (themes/tags/aspects/entities/keyphrases). The SETOF wrapper + per-row
    -- output split make them GROUP-BY dimensions. Base-LLM (default model),
    -- no sidecar/Warren model required.
    PERFORM rvbbit.create_operator(
        op_name => 'themes',
        op_shape => 'dimension',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_max_tokens => 128,
        op_system => $sys$You are a literary/narrative theme analyst. Given a single text, identify the recurring THEMES it touches — the higher-level narrative threads, underlying tensions, and human preoccupations it explores (e.g. "Loss And Grief", "Found Family", "Corporate Greed", "Coming Of Age", "Betrayal Of Trust"). Themes are NOT the literal subject matter or topics; they are the deeper ideas and emotional/moral throughlines the text is really about.

Output ONLY a JSON array of short string labels — nothing else. No prose, no explanation, no markdown, no code fences.

Rules for the labels:
- Output between 1 and 5 themes; pick only ones genuinely present, most central first.
- Each label is 1–3 words, Title Case (e.g. "Loss And Grief", "Power And Control").
- Use CANONICAL, reusable theme names from a stable shared vocabulary so identical themes bucket together across many texts — prefer the common phrasing over a hyper-specific one (use "Found Family", not "The Crew Becomes Her Family").
- Deduplicate; do not emit near-synonyms.
- If the text is too thin or generic to carry a real theme, return [].$sys$,
        op_user => $usr$TEXT:
{{ text }}

Return ONLY a JSON array of canonical theme labels (1–5, Title Case, most central first), e.g. ["Loss And Grief", "Found Family", "Corporate Greed"].$usr$,
        op_description => $dsc$DIMENSION: Fans one text out into a small set of canonical, higher-level narrative THEMES it touches (the recurring threads / underlying preoccupations, e.g. "Loss And Grief", "Found Family", "Corporate Greed") — distinct from topics, which name literal subject matter. Returns SETOF text (1–5 short Title Case labels), so Postgres expands each row into one row per theme. GROUP BY the resulting label to count how many texts share a theme, build theme-frequency charts, or cross-tab themes against other columns across a corpus.$dsc$
    );
    PERFORM rvbbit.create_operator(
        op_name => 'tags',
        op_shape => 'dimension',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_max_tokens => 128,
        op_system => $sys$You are a precise content tagger. Given a single piece of text, you produce short, hashtag-style TOPICAL TAGS — the kind of labels you would file or filter the text under in a content library or feed.

Output contract (follow EXACTLY):
- Output ONLY a raw JSON array of strings. No prose, no explanation, no markdown, no code fences, no keys.
- Produce 3 to 5 tags. Never zero; if the text is thin, give the 1-3 most defensible tags.
- Each tag is a SHORT canonical keyword: 1-2 words, Title Case (e.g. "Machine Learning", "Privacy"). No leading '#', no punctuation, no hashtags glyphs, no emojis.
- Tags must be CANONICAL and reusable so the same concept always gets the same tag across rows. Prefer a stable shared vocabulary ("Machine Learning", not "ML models in production"). Generalize hyper-specific phrasings to their common filing term.
- Deduplicate. No two tags that mean the same thing. No tag that is a substring synonym of another.
- Order by centrality: the most defining tag first.
- Tags describe the SUBJECT/topic for filing — not sentiment, tone, format, or length.

Example shape (format only, not real content): ["Machine Learning", "Healthcare", "Privacy", "Regulation"]$sys$,
        op_user => $usr$Read the text and return its filing/filter tags as a JSON array of short Title Case keywords (3-5 tags, most defining first). Output ONLY the JSON array.

TEXT:
{{ text }}

TAGS (JSON array):$usr$,
        op_description => $dsc$DIMENSION: per-row fan-out of one text value into a SET of short hashtag-style topical tags (Title Case, 1-2 words, canonical and deduplicated) for filing/filtering. Postgres expands each input row into one row per tag, so you GROUP BY the tag to count or aggregate how many rows fall under each tag (a one-to-many tag dimension). Distinct from topics() in that tags are terse, filter-shelf-style keywords drawn from a stable shared vocabulary rather than descriptive topic phrases.$dsc$
    );
    PERFORM rvbbit.create_operator(
        op_name => 'aspects',
        op_shape => 'dimension',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_max_tokens => 128,
        op_system => $sys$You are an aspect-based opinion mining engine for reviews and customer feedback. Given one piece of text (a review, survey response, support ticket, or comment), you identify the distinct ASPECTS — the concrete features, attributes, or subjects of the product/service/experience that the writer actually discusses or evaluates.

An aspect is WHAT is being talked about, not the sentiment about it. "The battery dies in two hours" → aspect "Battery Life". "Shipped late and the box was crushed" → aspects "Shipping", "Packaging".

Output contract (STRICT):
- Output ONLY a JSON array of short string labels. Nothing else — no prose, no explanation, no keys, no markdown, no code fences.
- Each label is a CANONICAL aspect name in Title Case, 1-3 words, chosen from a stable shared vocabulary so the same aspect always gets the same label across rows (e.g. always "Customer Support", never "support team" or "the support people"; always "Price", never "cost" or "how much it costs"; always "Battery Life", "Shipping", "Build Quality", "Ease Of Use", "Sound Quality", "Packaging", "Delivery Speed", "Comfort", "Reliability", "Screen Quality").
- Return only aspects the text genuinely addresses. Most-central / most-emphasized aspect first.
- Deduplicate. Return between 1 and 5 labels. If the text discusses no identifiable aspect (e.g. pure greeting or noise), return [].
- Labels are nouns/noun-phrases naming a subject, never sentiment words ("Good", "Disappointing") and never full sentences.

Example: "Love the camera and the screen is gorgeous, but it's overpriced and the battery barely lasts a day." → ["Camera Quality","Screen Quality","Price","Battery Life"]$sys$,
        op_user => $usr$Identify the canonical aspects discussed in the text below.

TEXT:
{{ text }}

Aspects (JSON array of 1-5 short Title-Case labels, ONLY the raw array):$usr$,
        op_description => $dsc$DIMENSION: aspects(text) fans one review/feedback text out into a SET of canonical aspect labels (the features/subjects discussed, e.g. "Battery Life", "Price", "Shipping", "Customer Support"). Each input row expands to one row per aspect, so GROUP BY the label to count how many rows mention each aspect or to aggregate sentiment/ratings per aspect. Returns SETOF text; labels are Title Case, 1-3 words, deduplicated, most-central first, capped at 5, drawn from a stable shared vocabulary so buckets group cleanly.$dsc$
    );
    PERFORM rvbbit.create_operator(
        op_name => 'entities',
        op_shape => 'dimension',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_max_tokens => 128,
        op_system => $sys$You are a precise named-entity extractor for a SQL GROUP BY dimension. From a single text value, identify the salient NAMED ENTITIES that are actually mentioned — real people, organizations/companies, places (cities, countries, regions, landmarks), and products/brands. Proper nouns only; never invent entities that are not in the text.

Output contract — follow EXACTLY:
- Output ONLY a JSON array of short string labels. No prose, no explanation, no markdown, no code fences.
- Each label is a single CANONICAL entity name in Title Case, normally 1-3 words.
- Canonicalize to the standard, reusable form so the same entity buckets cleanly across rows: drop legal suffixes and honorifics (e.g. "Apple Inc." → "Apple", "Dr. Jane Doe" → "Jane Doe"), expand to the common full name when clear, resolve aliases/abbreviations to one canonical name (e.g. "the EU" → "European Union", "NYC" → "New York"), and use each entity's most recognizable name.
- Deduplicate: list each distinct real-world entity at most once.
- Order by salience: most central/frequently-referenced entity first.
- Return at most 5 labels — the genuinely salient ones, not every passing mention.
- Exclude generic nouns, roles, dates, quantities, and concepts (e.g. "the company", "Tuesday", "privacy", "the team") — only concrete named entities.
- If the text contains no named entities, return an empty array: []

Shape example (illustrative only, do not echo): ["Apple", "Tim Cook", "Cupertino", "iPhone"]$sys$,
        op_user => $usr$TEXT:
{{ text }}

Salient named entities (people, organizations, places, products), canonicalized — JSON array only:$usr$,
        op_description => $dsc$DIMENSION: fans one text value out into a set of canonical NAMED ENTITY labels (people, organizations, places, products), one row per entity. The model is called once per row and its JSON-array output is expanded into label rows, so you GROUP BY the entity to count mentions, find co-occurrence, or aggregate metrics per entity across a text column — turning a free-text column into a queryable entity dimension.$dsc$
    );
    PERFORM rvbbit.create_operator(
        op_name => 'keyphrases',
        op_shape => 'dimension',
        op_arg_names => ARRAY['text'],
        op_return_type => 'text',
        op_max_tokens => 128,
        op_system => $sys$You are a precise keyphrase extractor for a SQL semantic engine. Given a single text value, you identify the most salient KEY PHRASES — the concise noun phrases that capture what the text is actually about (its core subjects, entities, and concepts).

Output contract (follow EXACTLY):
- Output ONLY a JSON array of strings. No prose, no explanation, no markdown, no code fences. The first character must be `[` and the last must be `]`.
- Each element is a short noun phrase: 1-3 words, in Title Case (e.g. "Cloud Migration", "Customer Churn", "Patient Privacy").
- Extract 1-5 phrases. Fewer is fine for short or thin text; never pad with filler.
- Make phrases CANONICAL and reusable so they bucket cleanly when grouped: prefer a stable, generalized term over a hyper-specific verbatim quote (e.g. "Battery Life" not "the phone's battery only lasting four hours").
- Prefer noun phrases; strip leading articles ("the", "a"), verbs, and adjectives that don't add identity.
- Deduplicate (no two phrases meaning the same thing) and order by centrality — the most defining phrase first.
- If the text is empty or has no extractable subject, output [].

Example: text "The new pricing tier upset long-time subscribers who feel nickel-and-dimed by add-on fees." → ["Pricing Tier","Subscriber Backlash","Add-On Fees"]$sys$,
        op_user => $usr$Extract the most salient key phrases (concise noun phrases) describing what this text is about. Return ONLY a JSON array of 1-5 Title Case strings, most central first. No prose, no code fences.

TEXT:
{{ text }}

key phrases (JSON array):$usr$,
        op_description => $dsc$DIMENSION: per-row keyphrase fan-out. rvbbit.keyphrases(text) reads one text value and returns SETOF text — a small set (1-5) of canonical, Title Case noun phrases capturing what the text is about. Postgres expands each input row into one row per phrase (one-to-many), so you GROUP BY the phrase to count documents per concept, find the most-mentioned topics across a column, or build a keyphrase frequency / co-occurrence breakdown. Use it like a content tag dimension: SELECT kp, COUNT(*) FROM docs, LATERAL rvbbit.keyphrases(docs.body) AS kp GROUP BY kp ORDER BY 2 DESC.$dsc$
    );
END $$;

-- Per-call audit + cache. Every LLM invocation by a semantic operator
-- writes one (or more, for takes>1) rows here. Acts as both a debugging
-- log and a content-addressed cache.
CREATE OR REPLACE FUNCTION rvbbit.current_query_id()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    cur_txid       text := txid_current()::text;
    stored_txid    text := NULLIF(current_setting('rvbbit.query_txid', true), '');
    raw_query_id   text := NULLIF(current_setting('rvbbit.query_id', true), '');
    next_query_id  uuid;
BEGIN
    -- Reuse the stored id when it is either:
    --   (a) explicitly PINNED via rvbbit.reset_query_id() (stored_txid = 'pinned')
    --       — sticky across statements, even on an autocommit connection, so a
    --       caller can tag a multi-statement sequence of operations with one id; or
    --   (b) from THIS transaction (stored_txid = cur_txid) — the implicit per-query
    --       default, keyed to txid_current() so a pooled connection never carries a
    --       stale session id into an unrelated later query.
    -- Multiple receipt writes in one query (and a prewarm's SPI, same txn) all
    -- share the id; without a pin, the next query is a new txn -> new id.
    IF raw_query_id IS NOT NULL
       AND (stored_txid = 'pinned' OR stored_txid = cur_txid) THEN
        BEGIN
            RETURN raw_query_id::uuid;
        EXCEPTION WHEN OTHERS THEN
            NULL;  -- a bad manually-set value falls through to a fresh id
        END;
    END IF;

    next_query_id := gen_random_uuid();
    PERFORM set_config('rvbbit.query_id', next_query_id::text, false);
    PERFORM set_config('rvbbit.query_txid', cur_txid, false);
    RETURN next_query_id;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.reset_query_id()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    next_query_id uuid := gen_random_uuid();
BEGIN
    -- Explicitly PIN a fresh query_id for this session. current_query_id() then
    -- reuses it across statements (even on an autocommit connection) until the
    -- next reset_query_id() or a session reset (DISCARD/RESET ALL) — so a caller
    -- can tag a whole sequence of operations with one id. Pinning is opt-in;
    -- without it current_query_id() stays per-query (keyed to txid_current()), so
    -- pooled connections never carry a stale id. The 'pinned' sentinel is what
    -- current_query_id() recognizes (txid_current() is always numeric, never that).
    PERFORM set_config('rvbbit.query_id', next_query_id::text, false);
    PERFORM set_config('rvbbit.query_txid', 'pinned', false);
    RETURN next_query_id;
END $$;

CREATE TABLE rvbbit.receipts (
    receipt_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operator       text NOT NULL,                  -- 'means', 'about', user-defined
    inputs_hash    bytea NOT NULL,                 -- blake3 of (op, model, prompt, inputs)
    model          text NOT NULL,                  -- final model used (after opts override)
    inputs         jsonb,                          -- the named inputs passed to the operator
    output         text,                           -- final operator output (after all steps)
    parsed         jsonb,                          -- typed parsed result
    take_index     int,                            -- which try in a takes.factor batch (future)
    take_verdict   text,                           -- 'winner'|'loser'|null (future)
    n_tokens_in    int,                            -- TOTAL across all sub-calls
    n_tokens_out   int,
    cost_usd       numeric(12, 6),                 -- nullable; needs price table lookup
    latency_ms     int,                            -- TOTAL wall time
    error          text,
    -- One operator invocation = ONE receipt = N sub-call entries below.
    -- This is the multi-step audit trail: when an operator runs
    --   [embed → cluster → name]
    -- you see all three calls here with their individual latency, model,
    -- token counts. Roll-up totals are in the columns above.
    sub_calls      jsonb,
    -- All receipts from one user query share a query_id; lets you
    -- audit cost per query, see which operator calls hit cache, etc.
    query_id       uuid,
    invocation_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX receipts_inputs_hash_idx ON rvbbit.receipts (inputs_hash)
    WHERE error IS NULL;
CREATE INDEX receipts_operator_time_idx ON rvbbit.receipts (operator, invocation_at);
CREATE INDEX receipts_query_id_idx ON rvbbit.receipts (query_id);

-- Model backend registry. Holds every endpoint rvbbit talks to — specialist
-- models (embedder, reranker, classifier, NLI, …) AND LLM providers
-- (OpenRouter, Anthropic, Gemini, a local vLLM, …). An operator references
-- a backend by name from its node definitions:
--   {"kind":"specialist", "specialist":"bge-m3", ...}      — a specialist node
--   {"kind":"llm",        "provider":"openrouter",  ...}    — an llm node
-- The Rust Transport trait dispatches by `transport` to the right adapter.
--
-- transport       — which wire protocol the backend speaks
-- endpoint_url    — base URL for rvbbit/openai (path appended by transport),
--                   full URL for gradio/openai_chat/anthropic, or a
--                   `{model}` template for gemini
-- batch_size      — for client-batched transports, max rows per HTTP call
-- max_concurrent  — per-backend semaphore cap on in-flight calls
-- auth_header_env — name of env var whose value is sent as the auth header
--                   (Authorization: Bearer, x-api-key, x-goog-api-key — the
--                   transport chooses). NOT the literal token (don't write
--                   secrets to a DB catalog).
-- transport_opts  — transport-specific knobs: gradio fn_index, openai
--                   model name, etc.
CREATE TABLE rvbbit.backends (
    name             text PRIMARY KEY,
    transport        text NOT NULL DEFAULT 'rvbbit',
    endpoint_url     text NOT NULL,
    batch_size       int  DEFAULT 32,
    max_concurrent   int  DEFAULT 4,
    timeout_ms       int  DEFAULT 30000,
    auth_header_env  text,
    transport_opts   jsonb NOT NULL DEFAULT '{}'::jsonb,
    description      text,
    source_provider  text,
    source_model     text,
    source_revision  text,
    install_manifest jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- rvbbit/gradio/openai/local_embed/stub serve specialists;
    -- openai_chat/anthropic/gemini serve LLM providers.
    CONSTRAINT backends_transport_check
        CHECK (transport IN ('rvbbit', 'gradio', 'openai', 'local_embed', 'stub',
                             'openai_chat', 'anthropic', 'gemini'))
);

-- Seed the default embedding backend. It is intentionally inserted with
-- DO NOTHING so users can replace `embed` with an OpenAI-compatible endpoint,
-- sidecar, Gradio app, or any future transport through register_backend.
INSERT INTO rvbbit.backends
    (name, transport, endpoint_url, batch_size, max_concurrent, timeout_ms,
     transport_opts, description)
VALUES
    ('embed', 'local_embed', 'local://embed', 128, 1, 120000,
     '{"model":"bge-small-en-v1.5"}'::jsonb,
     'Default local CPU text embedding backend.')
ON CONFLICT (name) DO NOTHING;

-- Seed the default LLM provider. An LLM provider is just a backend with a
-- chat transport, so it lives in this same registry. A fresh install calls
-- models with zero setup — auth comes from the OPENROUTER_API_KEY env var.
-- Register more providers (a local vLLM/Ollama, OpenAI, Together, …) with
-- register_backend(..., 'openai_chat').
INSERT INTO rvbbit.backends
    (name, transport, endpoint_url, max_concurrent, timeout_ms,
     auth_header_env, description)
VALUES
    ('openrouter', 'openai_chat',
     'https://openrouter.ai/api/v1/chat/completions',
     8, 120000, 'OPENROUTER_API_KEY',
     'Default LLM provider — OpenRouter multi-model gateway.')
ON CONFLICT (name) DO NOTHING;

-- Rvbbit runtime settings that need to be data-driven instead of only
-- environment-driven. Keep values JSONB so later settings can be structured.
CREATE TABLE rvbbit.settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO rvbbit.settings (key, value)
VALUES ('default_provider', to_jsonb('openrouter'::text))
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION rvbbit.default_provider()
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(
        (SELECT value #>> '{}' FROM rvbbit.settings WHERE key = 'default_provider'),
        'openrouter'
    )
$$;

CREATE OR REPLACE FUNCTION rvbbit.set_default_provider(provider_name text)
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized text := nullif(btrim(provider_name), '');
    backend_transport text;
    reloaded int := NULL;
BEGIN
    IF normalized IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_default_provider: provider_name cannot be empty';
    END IF;

    SELECT transport INTO backend_transport
    FROM rvbbit.backends
    WHERE name = normalized;

    IF backend_transport IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_default_provider: backend "%" is not registered', normalized;
    END IF;
    IF backend_transport NOT IN ('openai_chat', 'anthropic', 'gemini', 'stub') THEN
        RAISE EXCEPTION 'rvbbit.set_default_provider: backend "%" uses transport "%", not a chat transport',
            normalized, backend_transport;
    END IF;

    INSERT INTO rvbbit.settings (key, value, updated_at)
    VALUES ('default_provider', to_jsonb(normalized), clock_timestamp())
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        updated_at = clock_timestamp();

    BEGIN
        SELECT rvbbit.reload_backends() INTO reloaded;
    EXCEPTION WHEN undefined_function THEN
        reloaded := NULL;
    END;

    RETURN jsonb_build_object(
        'default_provider', normalized,
        'transport', backend_transport,
        'reloaded_backends', reloaded
    );
END
$$;

-- DDL helper: register a model backend. Just an INSERT with UPSERT so
-- users can re-run without re-DROP'ing — same shape as create_operator.
CREATE OR REPLACE FUNCTION rvbbit.register_backend(
    backend_name        text,
    backend_endpoint    text,
    backend_transport   text DEFAULT 'rvbbit',
    backend_batch_size  int  DEFAULT 32,
    backend_max_concur  int  DEFAULT 4,
    backend_timeout_ms  int  DEFAULT 30000,
    backend_auth_env    text DEFAULT NULL,
    backend_opts        jsonb DEFAULT '{}'::jsonb,
    backend_description text DEFAULT NULL,
    backend_source_provider text DEFAULT NULL,
    backend_source_model text DEFAULT NULL,
    backend_source_revision text DEFAULT NULL,
    backend_install_manifest jsonb DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $rb$
BEGIN
    -- security-03: backends.endpoint_url drives outbound HTTP (an SSRF target);
    -- gate registration like the other DDL rather than leaving it ungated.
    PERFORM rvbbit.require_mcp_gateway_admin();
    INSERT INTO rvbbit.backends
        (name, transport, endpoint_url, batch_size, max_concurrent,
         timeout_ms, auth_header_env, transport_opts, description,
         source_provider, source_model, source_revision, install_manifest)
    VALUES
        (backend_name, backend_transport, backend_endpoint, backend_batch_size,
         backend_max_concur, backend_timeout_ms, backend_auth_env, backend_opts,
         backend_description, backend_source_provider, backend_source_model,
         backend_source_revision, backend_install_manifest)
    ON CONFLICT (name) DO UPDATE SET
        transport       = EXCLUDED.transport,
        endpoint_url    = EXCLUDED.endpoint_url,
        batch_size      = EXCLUDED.batch_size,
        max_concurrent  = EXCLUDED.max_concurrent,
        timeout_ms      = EXCLUDED.timeout_ms,
        auth_header_env = EXCLUDED.auth_header_env,
        transport_opts  = EXCLUDED.transport_opts,
        description     = EXCLUDED.description,
        source_provider = EXCLUDED.source_provider,
        source_model    = EXCLUDED.source_model,
        source_revision = EXCLUDED.source_revision,
        install_manifest = EXCLUDED.install_manifest;
END
$rb$;

-- ---------------------------------------------------------------------------
-- User-trained models.
--
-- A trained model is a capability-backed asset whose training data came from
-- SQL. The extension owns metadata, job state, generated backend/operator
-- wiring, and observability. The actual training work stays outside the
-- Postgres backend in a trainer process/sidecar.
-- ---------------------------------------------------------------------------

CREATE TABLE rvbbit.ml_models (
    name                 text PRIMARY KEY,
    task                 text NOT NULL,
    status               text NOT NULL DEFAULT 'registered',

    -- Provenance. `source_sql` is the user-supplied training query; workers
    -- execute it under their own connection and emit an artifact.
    source_sql           text,
    target_column        text,
    feature_schema       jsonb NOT NULL DEFAULT '[]'::jsonb,
    training_opts        jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Artifact + serving surface. `backend_name` points at rvbbit.backends by
    -- convention, but is not an FK so audit survives backend deletion.
    artifact_uri         text,
    artifact_format      text,
    backend_name         text,
    operator_name        text,
    operator_arg_names   text[] NOT NULL DEFAULT ARRAY['row'],
    operator_arg_types   text[] NOT NULL DEFAULT ARRAY['jsonb'],
    operator_return_type text NOT NULL DEFAULT 'jsonb',

    metrics              jsonb NOT NULL DEFAULT '{}'::jsonb,
    install_manifest     jsonb,
    description          text,

    created_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    trained_at           timestamptz,

    CONSTRAINT ml_models_task_check CHECK (
        task IN (
            'classification', 'regression',
            'tabular_classification', 'tabular_regression',
            'forecasting', 'anomaly', 'survival', 'causal',
            'embedding', 'rerank', 'custom'
        )
    ),
    CONSTRAINT ml_models_status_check CHECK (
        status IN ('queued', 'running', 'active', 'failed', 'disabled', 'dropped', 'registered')
    ),
    CONSTRAINT ml_models_feature_schema_is_array CHECK (jsonb_typeof(feature_schema) = 'array'),
    CONSTRAINT ml_models_metrics_is_object CHECK (jsonb_typeof(metrics) = 'object'),
    CONSTRAINT ml_models_training_opts_is_object CHECK (jsonb_typeof(training_opts) = 'object'),
    CONSTRAINT ml_models_install_manifest_is_object CHECK (
        install_manifest IS NULL OR jsonb_typeof(install_manifest) = 'object'
    )
);

CREATE INDEX ml_models_status_idx ON rvbbit.ml_models (status, updated_at DESC);
CREATE INDEX ml_models_backend_idx ON rvbbit.ml_models (backend_name)
    WHERE backend_name IS NOT NULL;

CREATE OR REPLACE FUNCTION rvbbit.touch_ml_models_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

CREATE TRIGGER ml_models_touch_updated_at
    BEFORE UPDATE ON rvbbit.ml_models
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_ml_models_updated_at();

CREATE TABLE rvbbit.ml_training_runs (
    run_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name      text NOT NULL REFERENCES rvbbit.ml_models(name) ON DELETE CASCADE,
    status          text NOT NULL DEFAULT 'queued',
    task            text NOT NULL,
    source_sql      text NOT NULL,
    target_column   text,
    feature_schema  jsonb NOT NULL DEFAULT '[]'::jsonb,
    training_opts   jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics         jsonb NOT NULL DEFAULT '{}'::jsonb,
    artifact_uri    text,
    artifact_format text,
    backend_name    text,
    operator_name   text,
    worker_id       text,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at      timestamptz,
    finished_at     timestamptz,

    CONSTRAINT ml_training_runs_status_check CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
    ),
    CONSTRAINT ml_training_runs_task_check CHECK (
        task IN (
            'classification', 'regression',
            'tabular_classification', 'tabular_regression',
            'forecasting', 'anomaly', 'survival', 'causal',
            'embedding', 'rerank', 'custom'
        )
    ),
    CONSTRAINT ml_training_runs_feature_schema_is_array CHECK (jsonb_typeof(feature_schema) = 'array'),
    CONSTRAINT ml_training_runs_training_opts_is_object CHECK (jsonb_typeof(training_opts) = 'object'),
    CONSTRAINT ml_training_runs_metrics_is_object CHECK (jsonb_typeof(metrics) = 'object')
);

CREATE INDEX ml_training_runs_queue_idx
    ON rvbbit.ml_training_runs (status, created_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX ml_training_runs_model_time_idx
    ON rvbbit.ml_training_runs (model_name, created_at DESC);

CREATE OR REPLACE VIEW rvbbit.ml_model_status AS
SELECT
    m.*,
    r.run_id AS latest_run_id,
    r.status AS latest_run_status,
    r.worker_id AS latest_worker_id,
    r.error AS latest_error,
    r.created_at AS latest_run_created_at,
    r.started_at AS latest_run_started_at,
    r.finished_at AS latest_run_finished_at
FROM rvbbit.ml_models m
LEFT JOIN LATERAL (
    SELECT *
    FROM rvbbit.ml_training_runs r
    WHERE r.model_name = m.name
    ORDER BY r.created_at DESC
    LIMIT 1
) r ON true;

-- Queue a model-training request. A trainer sidecar can claim the run with
-- claim_model_training_run(), execute source_sql, write an artifact, then call
-- complete_model_training().
CREATE OR REPLACE FUNCTION rvbbit.train_model(
    model_name      text,
    source_sql      text,
    target_column   text,
    task            text DEFAULT 'classification',
    feature_schema  jsonb DEFAULT '[]'::jsonb,
    training_opts   jsonb DEFAULT '{}'::jsonb,
    description     text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $tm$
DECLARE
    actual_run_id uuid;
BEGIN
    IF model_name IS NULL OR btrim(model_name) = '' THEN
        RAISE EXCEPTION 'model_name is required';
    END IF;
    IF source_sql IS NULL OR btrim(source_sql) = '' THEN
        RAISE EXCEPTION 'source_sql is required';
    END IF;
    IF jsonb_typeof(feature_schema) <> 'array' THEN
        RAISE EXCEPTION 'feature_schema must be a JSON array';
    END IF;
    IF jsonb_typeof(training_opts) <> 'object' THEN
        RAISE EXCEPTION 'training_opts must be a JSON object';
    END IF;

    INSERT INTO rvbbit.ml_models
        (name, task, status, source_sql, target_column, feature_schema,
         training_opts, description)
    VALUES
        (model_name, task, 'queued', source_sql, target_column, feature_schema,
         training_opts, description)
    ON CONFLICT (name) DO UPDATE SET
        task           = EXCLUDED.task,
        status         = 'queued',
        source_sql     = EXCLUDED.source_sql,
        target_column  = EXCLUDED.target_column,
        feature_schema = EXCLUDED.feature_schema,
        training_opts  = EXCLUDED.training_opts,
        description    = COALESCE(EXCLUDED.description, rvbbit.ml_models.description);

    INSERT INTO rvbbit.ml_training_runs
        (model_name, task, source_sql, target_column, feature_schema, training_opts)
    VALUES
        (model_name, task, source_sql, target_column, feature_schema, training_opts)
    RETURNING run_id INTO actual_run_id;

    RETURN actual_run_id;
END
$tm$;

-- Claim one queued model-training run. Intended for a trainer worker; uses
-- SKIP LOCKED so multiple workers can poll concurrently.
CREATE OR REPLACE FUNCTION rvbbit.claim_model_training_run(
    worker_id text DEFAULT NULL
) RETURNS TABLE (
    run_id uuid,
    model_name text,
    task text,
    source_sql text,
    target_column text,
    feature_schema jsonb,
    training_opts jsonb
)
LANGUAGE plpgsql
VOLATILE
AS $cmtr$
BEGIN
    RETURN QUERY
    WITH picked AS (
        SELECT r.run_id
        FROM rvbbit.ml_training_runs r
        WHERE r.status = 'queued'
        ORDER BY r.created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    ),
    updated_run AS (
        UPDATE rvbbit.ml_training_runs r
        SET status = 'running',
            worker_id = COALESCE(claim_model_training_run.worker_id,
                                 current_setting('application_name', true)),
            started_at = clock_timestamp()
        FROM picked
        WHERE r.run_id = picked.run_id
        RETURNING r.run_id, r.model_name, r.task, r.source_sql, r.target_column,
                  r.feature_schema, r.training_opts
    ),
    updated_model AS (
        UPDATE rvbbit.ml_models m
        SET status = 'running'
        FROM updated_run u
        WHERE m.name = u.model_name
        RETURNING m.name
    )
    SELECT u.run_id, u.model_name, u.task, u.source_sql, u.target_column,
           u.feature_schema, u.training_opts
    FROM updated_run u;
END
$cmtr$;

-- Register a completed trained model as a backend and, by default, create a
-- JSONB row-oriented SQL operator that invokes that backend.
CREATE OR REPLACE FUNCTION rvbbit.register_trained_model(
    model_name           text,
    model_task           text,
    backend_name         text,
    backend_endpoint     text,
    backend_transport    text DEFAULT 'rvbbit',
    artifact_uri         text DEFAULT NULL,
    artifact_format      text DEFAULT NULL,
    feature_schema       jsonb DEFAULT '[]'::jsonb,
    target_column        text DEFAULT NULL,
    source_sql           text DEFAULT NULL,
    metrics              jsonb DEFAULT '{}'::jsonb,
    training_opts        jsonb DEFAULT '{}'::jsonb,
    install_manifest     jsonb DEFAULT NULL,
    backend_opts         jsonb DEFAULT '{}'::jsonb,
    backend_batch_size   int DEFAULT 256,
    backend_max_concur   int DEFAULT 2,
    backend_timeout_ms   int DEFAULT 120000,
    backend_auth_env     text DEFAULT NULL,
    model_description    text DEFAULT NULL,
    create_sql_operator  boolean DEFAULT true,
    operator_name        text DEFAULT NULL,
    operator_arg_name    text DEFAULT 'row',
    operator_arg_type    text DEFAULT 'jsonb',
    backend_input_key    text DEFAULT 'row',
    operator_return_type text DEFAULT 'jsonb',
    operator_parser      text DEFAULT 'json'
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $rtm$
DECLARE
    safe_model_name text;
    actual_operator_name text;
    step_inputs jsonb;
    step_doc jsonb;
BEGIN
    IF model_name IS NULL OR btrim(model_name) = '' THEN
        RAISE EXCEPTION 'model_name is required';
    END IF;
    IF backend_name IS NULL OR btrim(backend_name) = '' THEN
        RAISE EXCEPTION 'backend_name is required';
    END IF;
    IF backend_endpoint IS NULL OR btrim(backend_endpoint) = '' THEN
        RAISE EXCEPTION 'backend_endpoint is required';
    END IF;
    IF jsonb_typeof(feature_schema) <> 'array' THEN
        RAISE EXCEPTION 'feature_schema must be a JSON array';
    END IF;
    IF jsonb_typeof(metrics) <> 'object' THEN
        RAISE EXCEPTION 'metrics must be a JSON object';
    END IF;
    IF jsonb_typeof(training_opts) <> 'object' THEN
        RAISE EXCEPTION 'training_opts must be a JSON object';
    END IF;
    IF jsonb_typeof(backend_opts) <> 'object' THEN
        RAISE EXCEPTION 'backend_opts must be a JSON object';
    END IF;

    safe_model_name := regexp_replace(lower(model_name), '[^a-z0-9_]+', '_', 'g');
    safe_model_name := regexp_replace(safe_model_name, '^_+|_+$', '', 'g');
    IF safe_model_name = '' THEN
        safe_model_name := 'model_' || substr(md5(model_name), 1, 8);
    END IF;
    actual_operator_name := COALESCE(operator_name, 'predict_' || safe_model_name);

    PERFORM rvbbit.register_backend(
        backend_name             => backend_name,
        backend_endpoint         => backend_endpoint,
        backend_transport        => backend_transport,
        backend_batch_size       => backend_batch_size,
        backend_max_concur       => backend_max_concur,
        backend_timeout_ms       => backend_timeout_ms,
        backend_auth_env         => backend_auth_env,
        backend_opts             => backend_opts,
        backend_description      => COALESCE(model_description, 'Trained Rvbbit model ' || model_name),
        backend_source_provider  => 'rvbbit-trained',
        backend_source_model     => model_name,
        backend_source_revision  => artifact_uri,
        backend_install_manifest => install_manifest
    );

    INSERT INTO rvbbit.ml_models
        (name, task, status, source_sql, target_column, feature_schema,
         training_opts, artifact_uri, artifact_format, backend_name,
         operator_name, operator_arg_names, operator_arg_types,
         operator_return_type, metrics, install_manifest, description,
         trained_at)
    VALUES
        (model_name, model_task, 'active', source_sql, target_column, feature_schema,
         training_opts, artifact_uri, artifact_format, backend_name,
         CASE WHEN create_sql_operator THEN actual_operator_name ELSE NULL END,
         ARRAY[operator_arg_name], ARRAY[operator_arg_type],
         operator_return_type, metrics, install_manifest, model_description,
         clock_timestamp())
    ON CONFLICT (name) DO UPDATE SET
        task                 = EXCLUDED.task,
        status               = 'active',
        source_sql           = COALESCE(EXCLUDED.source_sql, rvbbit.ml_models.source_sql),
        target_column        = COALESCE(EXCLUDED.target_column, rvbbit.ml_models.target_column),
        feature_schema       = EXCLUDED.feature_schema,
        training_opts        = EXCLUDED.training_opts,
        artifact_uri         = EXCLUDED.artifact_uri,
        artifact_format      = EXCLUDED.artifact_format,
        backend_name         = EXCLUDED.backend_name,
        operator_name        = EXCLUDED.operator_name,
        operator_arg_names   = EXCLUDED.operator_arg_names,
        operator_arg_types   = EXCLUDED.operator_arg_types,
        operator_return_type = EXCLUDED.operator_return_type,
        metrics              = EXCLUDED.metrics,
        install_manifest     = EXCLUDED.install_manifest,
        description          = COALESCE(EXCLUDED.description, rvbbit.ml_models.description),
        trained_at           = EXCLUDED.trained_at;

    IF create_sql_operator THEN
        step_inputs := jsonb_build_object(
            backend_input_key,
            '{{ inputs.' || operator_arg_name || ' }}'
        );
        step_doc := jsonb_build_array(jsonb_build_object(
            'name', backend_name,
            'kind', 'specialist',
            'specialist', backend_name,
            'inputs', step_inputs
        ));

        PERFORM rvbbit.create_operator(
            op_name        => actual_operator_name,
            op_arg_names   => ARRAY[operator_arg_name],
            op_arg_types   => ARRAY[operator_arg_type],
            op_return_type => operator_return_type,
            op_parser      => operator_parser,
            op_shape       => 'scalar',
            op_description => COALESCE(model_description, 'Predict with trained Rvbbit model ' || model_name),
            op_steps       => step_doc
        );
    END IF;

    PERFORM rvbbit.reload_backends();
END
$rtm$;

CREATE OR REPLACE FUNCTION rvbbit.complete_model_training(
    run_id               uuid,
    backend_name         text,
    backend_endpoint     text,
    backend_transport    text DEFAULT 'rvbbit',
    artifact_uri         text DEFAULT NULL,
    artifact_format      text DEFAULT NULL,
    metrics              jsonb DEFAULT '{}'::jsonb,
    install_manifest     jsonb DEFAULT NULL,
    backend_opts         jsonb DEFAULT '{}'::jsonb,
    backend_batch_size   int DEFAULT 256,
    backend_max_concur   int DEFAULT 2,
    backend_timeout_ms   int DEFAULT 120000,
    backend_auth_env     text DEFAULT NULL,
    model_description    text DEFAULT NULL,
    create_sql_operator  boolean DEFAULT true,
    operator_name        text DEFAULT NULL,
    operator_arg_name    text DEFAULT 'row',
    operator_arg_type    text DEFAULT 'jsonb',
    backend_input_key    text DEFAULT 'row',
    operator_return_type text DEFAULT 'jsonb',
    operator_parser      text DEFAULT 'json'
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $cmt$
DECLARE
    r rvbbit.ml_training_runs%ROWTYPE;
BEGIN
    SELECT * INTO r
    FROM rvbbit.ml_training_runs
    WHERE ml_training_runs.run_id = complete_model_training.run_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'training run % not found', run_id;
    END IF;

    PERFORM rvbbit.register_trained_model(
        model_name           => r.model_name,
        model_task           => r.task,
        backend_name         => backend_name,
        backend_endpoint     => backend_endpoint,
        backend_transport    => backend_transport,
        artifact_uri         => artifact_uri,
        artifact_format      => artifact_format,
        feature_schema       => r.feature_schema,
        target_column        => r.target_column,
        source_sql           => r.source_sql,
        metrics              => metrics,
        training_opts        => r.training_opts,
        install_manifest     => install_manifest,
        backend_opts         => backend_opts,
        backend_batch_size   => backend_batch_size,
        backend_max_concur   => backend_max_concur,
        backend_timeout_ms   => backend_timeout_ms,
        backend_auth_env     => backend_auth_env,
        model_description    => model_description,
        create_sql_operator  => create_sql_operator,
        operator_name        => operator_name,
        operator_arg_name    => operator_arg_name,
        operator_arg_type    => operator_arg_type,
        backend_input_key    => backend_input_key,
        operator_return_type => operator_return_type,
        operator_parser      => operator_parser
    );

    UPDATE rvbbit.ml_training_runs
    SET status = 'completed',
        metrics = complete_model_training.metrics,
        artifact_uri = complete_model_training.artifact_uri,
        artifact_format = complete_model_training.artifact_format,
        backend_name = complete_model_training.backend_name,
        operator_name = COALESCE(complete_model_training.operator_name,
                                 (SELECT m.operator_name FROM rvbbit.ml_models m WHERE m.name = r.model_name)),
        finished_at = clock_timestamp(),
        error = NULL
    WHERE ml_training_runs.run_id = complete_model_training.run_id;
END
$cmt$;

CREATE OR REPLACE FUNCTION rvbbit.fail_model_training(
    run_id  uuid,
    error   text,
    metrics jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $fmt$
DECLARE
    failed_model_name text;
BEGIN
    UPDATE rvbbit.ml_training_runs
    SET status = 'failed',
        error = fail_model_training.error,
        metrics = fail_model_training.metrics,
        finished_at = clock_timestamp()
    WHERE ml_training_runs.run_id = fail_model_training.run_id
    RETURNING model_name INTO failed_model_name;

    IF failed_model_name IS NULL THEN
        RAISE EXCEPTION 'training run % not found', run_id;
    END IF;

    UPDATE rvbbit.ml_models
    SET status = 'failed'
    WHERE name = failed_model_name
      AND status IN ('queued', 'running', 'registered');
END
$fmt$;

-- ---------------------------------------------------------------------------
-- Capability catalog — curated deployable sidecars exposed through SQL.
--
-- The local capabilities/catalog.json remains a build artifact. Fresh
-- extension installs seed this table from the bundled canonical seed; the
-- capability CLI can refresh it after manifest changes. The manifest column is
-- the deployable Warren payload.
-- ---------------------------------------------------------------------------

CREATE TABLE rvbbit.capability_catalog (
    id                 text PRIMARY KEY,
    manifest_path      text,
    name               text NOT NULL,
    title              text NOT NULL,
    description        text,
    tags               text[] NOT NULL DEFAULT ARRAY[]::text[],
    kind               text NOT NULL,
    system_runtime     boolean NOT NULL DEFAULT false,
    capability_role    text,
    license            text,
    source_provider    text,
    source_model       text,
    source_revision    text,
    backend_name       text,
    backend_transport  text,
    runtime_name       text,
    runtime_language   text,
    runtime_template   text,
    runtime_handler    text,
    runtime_port       int,
    health_path        text,
    endpoint_path      text,
    device             text,
    resource_profile   jsonb NOT NULL DEFAULT '{}'::jsonb,
    gpu_required       boolean NOT NULL DEFAULT false,
    gpu_placement      text,
    model_size_bytes   bigint,
    vram_required_bytes bigint,
    vram_headroom_pct  numeric,
    operators          text[] NOT NULL DEFAULT ARRAY[]::text[],
    manifest           jsonb NOT NULL,
    catalog_entry      jsonb NOT NULL DEFAULT '{}'::jsonb,
    catalog_source     text NOT NULL DEFAULT 'manual',
    active             boolean NOT NULL DEFAULT true,
    created_by         oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT capability_catalog_id_check CHECK (id ~ '^[A-Za-z0-9_./-]+$'),
    CONSTRAINT capability_catalog_kind_check CHECK (kind <> ''),
    CONSTRAINT capability_catalog_manifest_is_object CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT capability_catalog_resource_profile_is_object CHECK (jsonb_typeof(resource_profile) = 'object'),
    CONSTRAINT capability_catalog_model_size_nonnegative CHECK (model_size_bytes IS NULL OR model_size_bytes >= 0),
    CONSTRAINT capability_catalog_vram_required_nonnegative CHECK (vram_required_bytes IS NULL OR vram_required_bytes >= 0),
    CONSTRAINT capability_catalog_entry_is_object CHECK (jsonb_typeof(catalog_entry) = 'object')
);

CREATE INDEX capability_catalog_active_idx ON rvbbit.capability_catalog (active, kind, name);
CREATE INDEX capability_catalog_tags_idx ON rvbbit.capability_catalog USING gin (tags);
CREATE INDEX capability_catalog_manifest_idx ON rvbbit.capability_catalog USING gin (manifest);
CREATE INDEX capability_catalog_backend_idx ON rvbbit.capability_catalog (backend_name)
    WHERE backend_name IS NOT NULL;
CREATE INDEX capability_catalog_runtime_idx ON rvbbit.capability_catalog (runtime_name)
    WHERE runtime_name IS NOT NULL;
CREATE INDEX capability_catalog_resource_idx ON rvbbit.capability_catalog
    (gpu_required, vram_required_bytes)
    WHERE gpu_required OR vram_required_bytes IS NOT NULL;

CREATE OR REPLACE FUNCTION rvbbit.touch_capability_catalog_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

CREATE TRIGGER capability_catalog_touch_updated_at
    BEFORE UPDATE ON rvbbit.capability_catalog
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_capability_catalog_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.normalize_capability_catalog_resources()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    resource_doc jsonb;
    gpu_doc jsonb;
BEGIN
    resource_doc := coalesce(
        CASE WHEN jsonb_typeof(NEW.resource_profile) = 'object'
                  AND NEW.resource_profile <> '{}'::jsonb
             THEN NEW.resource_profile END,
        CASE WHEN jsonb_typeof(NEW.catalog_entry->'resources') = 'object'
             THEN NEW.catalog_entry->'resources' END,
        CASE WHEN jsonb_typeof(NEW.manifest->'resources') = 'object'
             THEN NEW.manifest->'resources' END,
        '{}'::jsonb
    );
    gpu_doc := CASE
        WHEN jsonb_typeof(resource_doc->'gpu') = 'object' THEN resource_doc->'gpu'
        ELSE '{}'::jsonb
    END;

    NEW.resource_profile := resource_doc;
    NEW.gpu_required := CASE
        WHEN gpu_doc ? 'required' THEN coalesce((gpu_doc->>'required')::boolean, false)
        ELSE false
    END;
    NEW.gpu_placement := nullif(gpu_doc->>'placement', '');
    NEW.model_size_bytes := nullif(gpu_doc->>'model_size_bytes', '')::bigint;
    NEW.vram_required_bytes := nullif(gpu_doc->>'vram_required_bytes', '')::bigint;
    NEW.vram_headroom_pct := nullif(gpu_doc->>'headroom_pct', '')::numeric;
    RETURN NEW;
END $$;

CREATE TRIGGER capability_catalog_normalize_resources
    BEFORE INSERT OR UPDATE ON rvbbit.capability_catalog
    FOR EACH ROW EXECUTE FUNCTION rvbbit.normalize_capability_catalog_resources();

CREATE OR REPLACE FUNCTION rvbbit.require_capability_catalog_admin()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper
    ) THEN
        RAISE EXCEPTION 'rvbbit capability catalog changes require a superuser in this release';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.upsert_capability_catalog_entry(
    catalog_entry jsonb,
    capability_manifest jsonb,
    catalog_source text DEFAULT 'curated',
    entry_active boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $ucc$
DECLARE
    normalized_entry jsonb := coalesce(catalog_entry, '{}'::jsonb);
    normalized_manifest jsonb := coalesce(capability_manifest, '{}'::jsonb);
    normalized_id text;
    normalized_source text := coalesce(nullif(btrim(catalog_source), ''), 'curated');
    entry_tags text[] := ARRAY[]::text[];
    entry_operators text[] := ARRAY[]::text[];
    resource_doc jsonb := '{}'::jsonb;
    gpu_doc jsonb := '{}'::jsonb;
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF jsonb_typeof(normalized_entry) <> 'object' THEN
        RAISE EXCEPTION 'catalog_entry must be a JSON object';
    END IF;
    IF jsonb_typeof(normalized_manifest) <> 'object' THEN
        RAISE EXCEPTION 'capability_manifest must be a JSON object';
    END IF;

    normalized_id := nullif(btrim(coalesce(
        normalized_entry->>'id',
        normalized_entry->>'manifest_path',
        normalized_manifest->>'name'
    )), '');
    IF normalized_id IS NULL THEN
        RAISE EXCEPTION 'catalog entry id is required';
    END IF;
    IF normalized_id !~ '^[A-Za-z0-9_./-]+$' THEN
        RAISE EXCEPTION 'catalog entry id contains unsupported characters: %', normalized_id;
    END IF;

    IF jsonb_typeof(coalesce(normalized_entry->'tags', '[]'::jsonb)) = 'array' THEN
        SELECT coalesce(array_agg(tag ORDER BY tag), ARRAY[]::text[])
        INTO entry_tags
        FROM jsonb_array_elements_text(normalized_entry->'tags') AS t(tag);
    END IF;

    IF jsonb_typeof(coalesce(normalized_entry->'operators', '[]'::jsonb)) = 'array' THEN
        SELECT coalesce(array_agg(op ORDER BY op), ARRAY[]::text[])
        INTO entry_operators
        FROM jsonb_array_elements_text(normalized_entry->'operators') AS o(op);
    END IF;

    resource_doc := coalesce(
        CASE WHEN jsonb_typeof(normalized_entry->'resources') = 'object'
             THEN normalized_entry->'resources' END,
        CASE WHEN jsonb_typeof(normalized_manifest->'resources') = 'object'
             THEN normalized_manifest->'resources' END,
        '{}'::jsonb
    );
    gpu_doc := CASE
        WHEN jsonb_typeof(resource_doc->'gpu') = 'object' THEN resource_doc->'gpu'
        ELSE '{}'::jsonb
    END;

    INSERT INTO rvbbit.capability_catalog
        (id, manifest_path, name, title, description, tags, kind,
         system_runtime, capability_role, license,
         source_provider, source_model, source_revision,
         backend_name, backend_transport,
         runtime_name, runtime_language, runtime_template, runtime_handler,
         runtime_port, health_path, endpoint_path, device,
         resource_profile, gpu_required, gpu_placement, model_size_bytes,
         vram_required_bytes, vram_headroom_pct,
         operators, manifest, catalog_entry,
         catalog_source, active)
    VALUES
        (normalized_id,
         nullif(normalized_entry->>'manifest_path', ''),
         coalesce(nullif(normalized_entry->>'name', ''), normalized_manifest->>'name', normalized_id),
         coalesce(nullif(normalized_entry->>'title', ''), normalized_manifest->>'title',
                  normalized_manifest->>'name', normalized_id),
         coalesce(normalized_entry->>'description', normalized_manifest->>'description'),
         entry_tags,
         coalesce(nullif(normalized_entry->>'kind', ''), normalized_manifest->>'kind', 'unknown'),
         CASE
             WHEN normalized_entry ? 'system_runtime' THEN coalesce((normalized_entry->>'system_runtime')::boolean, false)
             WHEN normalized_manifest ? 'system_runtime' THEN coalesce((normalized_manifest->>'system_runtime')::boolean, false)
             ELSE false
         END,
         coalesce(nullif(normalized_entry->>'capability_role', ''), nullif(normalized_manifest->>'capability_role', '')),
         coalesce(normalized_entry->>'license', normalized_manifest->>'license'),
         coalesce(normalized_entry->>'source_provider', normalized_manifest #>> '{source,provider}'),
         coalesce(normalized_entry->>'source_model', normalized_manifest #>> '{source,model}'),
         coalesce(normalized_entry->>'source_revision', normalized_manifest #>> '{source,revision}'),
         coalesce(normalized_entry->>'backend_name', normalized_manifest #>> '{backend,name}'),
         coalesce(normalized_entry->>'backend_transport', normalized_manifest #>> '{backend,transport}'),
         coalesce(normalized_entry->>'runtime_name', normalized_manifest #>> '{runtime_registration,name}'),
         coalesce(normalized_entry->>'runtime_language', normalized_manifest #>> '{runtime_registration,language}',
                  normalized_manifest #>> '{runtime,language}'),
         coalesce(normalized_entry->>'runtime_template', normalized_manifest #>> '{runtime,template}'),
         coalesce(normalized_entry->>'runtime_handler', normalized_manifest #>> '{runtime,handler}'),
         nullif(coalesce(normalized_entry->>'runtime_port', normalized_manifest #>> '{runtime,port}'), '')::int,
         coalesce(normalized_entry->>'health_path', normalized_manifest #>> '{warren,health_path}',
                  normalized_manifest #>> '{runtime,health_path}'),
         coalesce(normalized_entry->>'endpoint_path', normalized_manifest #>> '{warren,endpoint_path}',
                  normalized_manifest #>> '{runtime_registration,endpoint_path}'),
         coalesce(normalized_entry->>'device', normalized_manifest #>> '{runtime,device}'),
         resource_doc,
         CASE
             WHEN gpu_doc ? 'required' THEN coalesce((gpu_doc->>'required')::boolean, false)
             ELSE false
         END,
         nullif(gpu_doc->>'placement', ''),
         nullif(gpu_doc->>'model_size_bytes', '')::bigint,
         nullif(gpu_doc->>'vram_required_bytes', '')::bigint,
         nullif(gpu_doc->>'headroom_pct', '')::numeric,
         entry_operators,
         normalized_manifest,
         normalized_entry,
         normalized_source,
         coalesce(entry_active, true))
    ON CONFLICT (id) DO UPDATE SET
        manifest_path = EXCLUDED.manifest_path,
        name = EXCLUDED.name,
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        tags = EXCLUDED.tags,
        kind = EXCLUDED.kind,
        system_runtime = EXCLUDED.system_runtime,
        capability_role = EXCLUDED.capability_role,
        license = EXCLUDED.license,
        source_provider = EXCLUDED.source_provider,
        source_model = EXCLUDED.source_model,
        source_revision = EXCLUDED.source_revision,
        backend_name = EXCLUDED.backend_name,
        backend_transport = EXCLUDED.backend_transport,
        runtime_name = EXCLUDED.runtime_name,
        runtime_language = EXCLUDED.runtime_language,
        runtime_template = EXCLUDED.runtime_template,
        runtime_handler = EXCLUDED.runtime_handler,
        runtime_port = EXCLUDED.runtime_port,
        health_path = EXCLUDED.health_path,
        endpoint_path = EXCLUDED.endpoint_path,
        device = EXCLUDED.device,
        resource_profile = EXCLUDED.resource_profile,
        gpu_required = EXCLUDED.gpu_required,
        gpu_placement = EXCLUDED.gpu_placement,
        model_size_bytes = EXCLUDED.model_size_bytes,
        vram_required_bytes = EXCLUDED.vram_required_bytes,
        vram_headroom_pct = EXCLUDED.vram_headroom_pct,
        operators = EXCLUDED.operators,
        manifest = EXCLUDED.manifest,
        catalog_entry = EXCLUDED.catalog_entry,
        catalog_source = EXCLUDED.catalog_source,
        active = EXCLUDED.active;

    SELECT to_jsonb(c) INTO row_doc
    FROM rvbbit.capability_catalog c
    WHERE c.id = normalized_id;
    RETURN row_doc;
END
$ucc$;

CREATE OR REPLACE FUNCTION rvbbit.prune_capability_catalog(
    catalog_source text DEFAULT 'curated',
    keep_ids text[] DEFAULT ARRAY[]::text[]
) RETURNS integer
LANGUAGE plpgsql
VOLATILE
AS $pcc$
DECLARE
    affected integer;
    normalized_source text := coalesce(nullif(btrim(catalog_source), ''), 'curated');
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    UPDATE rvbbit.capability_catalog c
    SET active = false
    WHERE c.catalog_source = normalized_source
      AND NOT (c.id = ANY(coalesce(keep_ids, ARRAY[]::text[])))
      AND c.active;
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END
$pcc$;

-- ---------------------------------------------------------------------------
-- Warren nodes — optional remote deployment agents.
--
-- A Warren is a host-local agent that can build/run sidecars near CPU/GPU
-- resources. Rvbbit remains the control plane: SQL queues desired
-- deployments, Warrens claim jobs, then register resulting backend endpoints.
-- Security is deliberately light for prerelease, but the catalog keeps
-- key/auth fields so stronger policies can be added without changing shape.
-- ---------------------------------------------------------------------------

CREATE TABLE rvbbit.warren_nodes (
    node_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL UNIQUE,
    base_url         text,
    labels           jsonb NOT NULL DEFAULT '{}'::jsonb,
    capacity         jsonb NOT NULL DEFAULT '{}'::jsonb,
    inventory        jsonb NOT NULL DEFAULT '[]'::jsonb,
    status           text NOT NULL DEFAULT 'registered',
    version          text,
    shared_key_hash  text,
    auth_config      jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_heartbeat   timestamptz,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT warren_nodes_status_check CHECK (
        status IN ('registered', 'ready', 'busy', 'draining', 'offline', 'error')
    ),
    CONSTRAINT warren_nodes_labels_is_object CHECK (jsonb_typeof(labels) = 'object'),
    CONSTRAINT warren_nodes_capacity_is_object CHECK (jsonb_typeof(capacity) = 'object'),
    CONSTRAINT warren_nodes_inventory_is_array CHECK (jsonb_typeof(inventory) = 'array'),
    CONSTRAINT warren_nodes_auth_config_is_object CHECK (jsonb_typeof(auth_config) = 'object')
);

CREATE INDEX warren_nodes_status_idx ON rvbbit.warren_nodes (status, last_heartbeat DESC);
CREATE INDEX warren_nodes_labels_idx ON rvbbit.warren_nodes USING gin (labels);

CREATE OR REPLACE FUNCTION rvbbit.touch_warren_nodes_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

CREATE TRIGGER warren_nodes_touch_updated_at
    BEFORE UPDATE ON rvbbit.warren_nodes
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_warren_nodes_updated_at();

CREATE OR REPLACE VIEW rvbbit.warren_node_effective_status AS
WITH heartbeat AS (
    SELECT
        n.*,
        CASE
            WHEN n.last_heartbeat IS NULL THEN 'unknown'
            WHEN clock_timestamp() - n.last_heartbeat < interval '30 seconds' THEN 'fresh'
            WHEN clock_timestamp() - n.last_heartbeat < interval '2 minutes' THEN 'stale'
            ELSE 'offline'
        END AS heartbeat_state
    FROM rvbbit.warren_nodes n
)
SELECT
    h.node_id,
    h.name,
    h.base_url,
    h.labels,
    h.capacity,
    h.inventory,
    h.status AS reported_status,
    h.heartbeat_state,
    CASE
        WHEN h.status = 'error' THEN 'error'
        WHEN h.status = 'draining' THEN 'draining'
        WHEN h.heartbeat_state = 'offline' THEN 'offline'
        WHEN h.heartbeat_state = 'unknown' THEN 'registered'
        WHEN h.heartbeat_state = 'stale' AND h.status IN ('ready', 'busy') THEN 'stale'
        ELSE h.status
    END AS effective_status,
    h.status IN ('ready', 'busy')
        AND h.heartbeat_state IN ('fresh', 'stale') AS is_eligible,
    h.version,
    h.last_heartbeat,
    CASE
        WHEN h.last_heartbeat IS NULL THEN NULL::interval
        ELSE clock_timestamp() - h.last_heartbeat
    END AS heartbeat_age,
    h.created_at,
    h.updated_at
FROM heartbeat h;

CREATE TABLE rvbbit.warren_jobs (
    job_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL,
    desired_state    text NOT NULL DEFAULT 'running',
    name             text NOT NULL,
    manifest         jsonb NOT NULL,
    target_selector  jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'queued',
    phase            text NOT NULL DEFAULT 'queued',
    claimed_by       text,
    claimed_at       timestamptz,
    attempts         int NOT NULL DEFAULT 0,
    endpoint_url     text,
    backend_name     text,
    operator_name    text,
    runtime_name     text,
    error            text,
    progress         jsonb NOT NULL DEFAULT '{}'::jsonb,
    logs             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at       timestamptz,
    finished_at      timestamptz,
    CONSTRAINT warren_jobs_kind_check CHECK (
        kind IN ('capability', 'trained_model', 'mcp_server', 'compose', 'custom')
    ),
    CONSTRAINT warren_jobs_desired_state_check CHECK (
        desired_state IN ('running', 'stopped', 'removed')
    ),
    CONSTRAINT warren_jobs_status_check CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
    ),
    CONSTRAINT warren_jobs_phase_check CHECK (phase <> ''),
    CONSTRAINT warren_jobs_manifest_is_object CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT warren_jobs_target_selector_is_object CHECK (jsonb_typeof(target_selector) = 'object'),
    CONSTRAINT warren_jobs_progress_is_object CHECK (jsonb_typeof(progress) = 'object'),
    CONSTRAINT warren_jobs_logs_is_object CHECK (jsonb_typeof(logs) = 'object')
);

CREATE INDEX warren_jobs_queue_idx
    ON rvbbit.warren_jobs (status, created_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX warren_jobs_target_selector_idx ON rvbbit.warren_jobs USING gin (target_selector);

CREATE OR REPLACE FUNCTION rvbbit.touch_warren_jobs_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

CREATE TRIGGER warren_jobs_touch_updated_at
    BEFORE UPDATE ON rvbbit.warren_jobs
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_warren_jobs_updated_at();

CREATE TABLE rvbbit.warren_deployments (
    deployment_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           uuid UNIQUE REFERENCES rvbbit.warren_jobs(job_id) ON DELETE SET NULL,
    node_id          uuid REFERENCES rvbbit.warren_nodes(node_id) ON DELETE SET NULL,
    node_name        text NOT NULL,
    kind             text NOT NULL,
    name             text NOT NULL,
    status           text NOT NULL DEFAULT 'running',
    endpoint_url     text,
    backend_name     text,
    operator_name    text,
    runtime_name     text,
    manifest         jsonb NOT NULL DEFAULT '{}'::jsonb,
    compose_project  text,
    work_dir         text,
    health           jsonb NOT NULL DEFAULT '{}'::jsonb,
    error            text,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    stopped_at       timestamptz,
    CONSTRAINT warren_deployments_kind_check CHECK (
        kind IN ('capability', 'trained_model', 'mcp_server', 'compose', 'custom')
    ),
    CONSTRAINT warren_deployments_status_check CHECK (
        status IN ('starting', 'running', 'stopping', 'stopped', 'failed', 'removed',
                   'drifted', 'orphaned')
    ),
    CONSTRAINT warren_deployments_manifest_is_object CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT warren_deployments_health_is_object CHECK (jsonb_typeof(health) = 'object')
);

CREATE INDEX warren_deployments_node_idx ON rvbbit.warren_deployments (node_name, status);
CREATE INDEX warren_deployments_backend_idx ON rvbbit.warren_deployments (backend_name)
    WHERE backend_name IS NOT NULL;
CREATE UNIQUE INDEX warren_deployments_active_unique_idx ON rvbbit.warren_deployments
    (node_name, kind, name)
    WHERE status IN ('starting', 'running', 'stopping');

CREATE TABLE rvbbit.warren_node_metrics (
    metric_id             bigserial PRIMARY KEY,
    node_id               uuid REFERENCES rvbbit.warren_nodes(node_id) ON DELETE SET NULL,
    node_name             text NOT NULL,
    collected_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    metrics               jsonb NOT NULL,
    cpu_pct               double precision,
    load1                 double precision,
    load5                 double precision,
    load15                double precision,
    mem_used_bytes        bigint,
    mem_total_bytes       bigint,
    gpu_count             int,
    gpu_util_pct          double precision,
    gpu_mem_used_bytes    bigint,
    gpu_mem_total_bytes   bigint,
    CONSTRAINT warren_node_metrics_metrics_is_object CHECK (jsonb_typeof(metrics) = 'object')
);

CREATE INDEX warren_node_metrics_node_time_idx
    ON rvbbit.warren_node_metrics (node_name, collected_at DESC);
CREATE INDEX warren_node_metrics_collected_at_idx
    ON rvbbit.warren_node_metrics (collected_at);

CREATE OR REPLACE VIEW rvbbit.warren_node_latest_metrics AS
SELECT DISTINCT ON (node_name)
    metric_id,
    node_id,
    node_name,
    collected_at,
    metrics,
    cpu_pct,
    load1,
    load5,
    load15,
    mem_used_bytes,
    mem_total_bytes,
    gpu_count,
    gpu_util_pct,
    gpu_mem_used_bytes,
    gpu_mem_total_bytes
FROM rvbbit.warren_node_metrics
ORDER BY node_name, collected_at DESC;

CREATE OR REPLACE FUNCTION rvbbit.capability_gpu_required(
    capability_manifest jsonb
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN coalesce(NULLIF(capability_manifest #>> '{resources,gpu,required}', '')::boolean, false)
        OR coalesce(capability_manifest #>> '{runtime,device}', '') = 'cuda';
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.capability_vram_required_bytes(
    capability_manifest jsonb
) RETURNS bigint
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN coalesce(
        NULLIF(capability_manifest #>> '{resources,gpu,vram_required_bytes}', '')::bigint,
        NULLIF(capability_manifest #>> '{resource_profile,gpu,vram_required_bytes}', '')::bigint,
        NULLIF(capability_manifest #>> '{resources,vram_required_bytes}', '')::bigint
    );
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.capability_gpu_reserved(
    capability_manifest jsonb
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN rvbbit.capability_gpu_required(capability_manifest)
        OR coalesce(NULLIF(capability_manifest #>> '{resources,gpu,reserved}', '')::boolean, false);
END
$$;

CREATE OR REPLACE VIEW rvbbit.warren_gpu_capacity AS
WITH gpu_rows AS (
    SELECT
        n.node_id,
        n.name AS node_name,
        n.capacity,
        n.inventory,
        coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90) AS vram_usable_ratio,
        g.elem AS gpu
    FROM rvbbit.warren_nodes n
    LEFT JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(n.inventory) = 'array' THEN n.inventory ELSE '[]'::jsonb END
    ) AS g(elem) ON true
),
provisioned AS (
    SELECT
        d.node_id,
        coalesce(sum(rvbbit.capability_vram_required_bytes(d.manifest)), 0)::bigint
            AS gpu_provisioned_bytes
    FROM rvbbit.warren_deployments d
    WHERE d.status IN ('starting', 'running', 'stopping')
      AND rvbbit.capability_gpu_reserved(d.manifest)
      AND rvbbit.capability_vram_required_bytes(d.manifest) IS NOT NULL
    GROUP BY d.node_id
)
SELECT
    n.node_id,
    n.name AS node_name,
    n.capacity,
    n.inventory AS gpu_inventory,
    coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90)
        AS vram_usable_ratio,
    count(g.gpu)::int AS gpu_count,
    coalesce(
        array_remove(array_agg(DISTINCT g.gpu->>'name') FILTER (WHERE g.gpu ? 'name'), NULL),
        ARRAY[]::text[]
    ) AS gpu_names,
    coalesce(sum(NULLIF(g.gpu->>'memory_total_bytes', '')::numeric), 0)::bigint
        AS gpu_mem_total_bytes,
    coalesce(
        floor(sum(NULLIF(g.gpu->>'memory_total_bytes', '')::numeric)
            * coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90)),
        0
    )::bigint AS gpu_mem_usable_bytes,
    coalesce(
        max(floor(NULLIF(g.gpu->>'memory_total_bytes', '')::numeric
            * coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90))),
        0
    )::bigint AS single_gpu_mem_usable_bytes,
    coalesce(p.gpu_provisioned_bytes, 0)::bigint AS gpu_provisioned_bytes,
    greatest(
        coalesce(
            floor(sum(NULLIF(g.gpu->>'memory_total_bytes', '')::numeric)
                * coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90)),
            0
        )::bigint - coalesce(p.gpu_provisioned_bytes, 0)::bigint,
        0
    )::bigint AS gpu_available_bytes
FROM rvbbit.warren_nodes n
LEFT JOIN gpu_rows g ON g.node_id = n.node_id
LEFT JOIN provisioned p ON p.node_id = n.node_id
GROUP BY n.node_id, n.name, n.capacity, n.inventory, p.gpu_provisioned_bytes;

CREATE OR REPLACE FUNCTION rvbbit.touch_warren_deployments_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

CREATE TRIGGER warren_deployments_touch_updated_at
    BEFORE UPDATE ON rvbbit.warren_deployments
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_warren_deployments_updated_at();

CREATE OR REPLACE VIEW rvbbit.warren_inventory AS
SELECT
    n.node_id,
    n.name AS node_name,
    n.base_url,
    n.labels,
    n.capacity,
    n.reported_status AS node_status,
    n.effective_status AS node_effective_status,
    n.heartbeat_state,
    n.is_eligible,
    n.version,
    n.last_heartbeat,
    lm.collected_at AS latest_metrics_at,
    lm.cpu_pct,
    lm.load1,
    lm.mem_used_bytes,
    lm.mem_total_bytes,
    lm.gpu_count,
    lm.gpu_util_pct,
    lm.gpu_mem_used_bytes,
    lm.gpu_mem_total_bytes,
    lm.metrics AS latest_metrics,
    d.deployment_id,
    d.kind,
    d.name AS deployment_name,
    d.status AS deployment_status,
    d.endpoint_url,
    d.backend_name,
    d.operator_name,
    d.runtime_name,
    d.health,
    d.error,
    d.updated_at AS deployment_updated_at,
    cap.gpu_names,
    cap.vram_usable_ratio,
    cap.gpu_mem_usable_bytes,
    cap.single_gpu_mem_usable_bytes,
    cap.gpu_provisioned_bytes,
    cap.gpu_available_bytes
FROM rvbbit.warren_node_effective_status n
LEFT JOIN rvbbit.warren_node_latest_metrics lm
  ON lm.node_id = n.node_id
LEFT JOIN rvbbit.warren_gpu_capacity cap
  ON cap.node_id = n.node_id
LEFT JOIN rvbbit.warren_deployments d
  ON d.node_id = n.node_id
 AND d.status IN ('starting', 'running', 'stopping', 'stopped', 'failed', 'drifted', 'orphaned');

CREATE OR REPLACE VIEW rvbbit.warren_backend_status AS
WITH ranked AS (
    SELECT
        d.*,
        row_number() OVER (
            PARTITION BY d.backend_name
            ORDER BY d.updated_at DESC, d.created_at DESC, d.deployment_id DESC
        ) AS rn
    FROM rvbbit.warren_deployments d
    WHERE d.backend_name IS NOT NULL
)
SELECT
    b.name,
    b.transport,
    b.endpoint_url,
    b.batch_size,
    b.max_concurrent,
    b.timeout_ms,
    b.auth_header_env,
    b.transport_opts,
    b.description,
    b.source_provider,
    b.source_model,
    b.source_revision,
    b.install_manifest,
    b.created_at,
    d.deployment_id,
    d.node_name,
    d.kind AS deployment_kind,
    d.name AS deployment_name,
    d.status AS deployment_status,
    CASE
        WHEN d.deployment_id IS NULL THEN 'external'
        WHEN d.status = 'running' THEN 'running'
        WHEN d.status IN ('starting', 'stopping') THEN d.status
        ELSE 'unavailable'
    END AS serving_status,
    (d.deployment_id IS NULL OR d.status = 'running') AS callable,
    d.error AS deployment_error,
    d.health AS deployment_health,
    d.updated_at AS deployment_updated_at,
    d.stopped_at
FROM rvbbit.backends b
LEFT JOIN ranked d
  ON d.backend_name = b.name
 AND d.rn = 1;

CREATE OR REPLACE FUNCTION rvbbit.register_warren_node(
    node_name        text,
    node_base_url    text DEFAULT NULL,
    node_labels      jsonb DEFAULT '{}'::jsonb,
    node_capacity    jsonb DEFAULT '{}'::jsonb,
    node_version     text DEFAULT NULL,
    node_shared_key_hash text DEFAULT NULL,
    node_auth_config jsonb DEFAULT '{}'::jsonb
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $rwn$
DECLARE
    actual_node_id uuid;
BEGIN
    IF node_name IS NULL OR btrim(node_name) = '' THEN
        RAISE EXCEPTION 'node_name is required';
    END IF;
    IF jsonb_typeof(node_labels) <> 'object' THEN
        RAISE EXCEPTION 'node_labels must be a JSON object';
    END IF;
    IF jsonb_typeof(node_capacity) <> 'object' THEN
        RAISE EXCEPTION 'node_capacity must be a JSON object';
    END IF;
    IF jsonb_typeof(node_auth_config) <> 'object' THEN
        RAISE EXCEPTION 'node_auth_config must be a JSON object';
    END IF;

    INSERT INTO rvbbit.warren_nodes
        (name, base_url, labels, capacity, version, shared_key_hash,
         auth_config, status, last_heartbeat)
    VALUES
        (node_name, node_base_url, node_labels, node_capacity, node_version,
         node_shared_key_hash, node_auth_config, 'ready', clock_timestamp())
    ON CONFLICT (name) DO UPDATE SET
        base_url = COALESCE(EXCLUDED.base_url, rvbbit.warren_nodes.base_url),
        labels = EXCLUDED.labels,
        capacity = EXCLUDED.capacity,
        version = COALESCE(EXCLUDED.version, rvbbit.warren_nodes.version),
        shared_key_hash = COALESCE(EXCLUDED.shared_key_hash, rvbbit.warren_nodes.shared_key_hash),
        auth_config = EXCLUDED.auth_config,
        status = 'ready',
        last_heartbeat = clock_timestamp()
    RETURNING node_id INTO actual_node_id;

    RETURN actual_node_id;
END
$rwn$;

CREATE OR REPLACE FUNCTION rvbbit.warren_heartbeat(
    node_name      text,
    node_status    text DEFAULT 'ready',
    node_labels    jsonb DEFAULT NULL,
    node_capacity  jsonb DEFAULT NULL,
    node_inventory jsonb DEFAULT NULL,
    node_version   text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $whb$
BEGIN
    UPDATE rvbbit.warren_nodes
    SET status = node_status,
        labels = COALESCE(node_labels, labels),
        capacity = COALESCE(node_capacity, capacity),
        inventory = COALESCE(node_inventory, inventory),
        version = COALESCE(node_version, version),
        last_heartbeat = clock_timestamp()
    WHERE name = node_name;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'warren node % is not registered', node_name;
    END IF;
END
$whb$;

CREATE OR REPLACE FUNCTION rvbbit.record_warren_metrics(
    node_name  text,
    metric_doc jsonb
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $rwm$
DECLARE
    actual_node_id uuid;
BEGIN
    IF jsonb_typeof(metric_doc) <> 'object' THEN
        RAISE EXCEPTION 'metric_doc must be a JSON object';
    END IF;

    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = record_warren_metrics.node_name;

    IF actual_node_id IS NULL THEN
        RAISE EXCEPTION 'warren node % is not registered', node_name;
    END IF;

    INSERT INTO rvbbit.warren_node_metrics
        (node_id, node_name, metrics, cpu_pct, load1, load5, load15,
         mem_used_bytes, mem_total_bytes, gpu_count, gpu_util_pct,
         gpu_mem_used_bytes, gpu_mem_total_bytes)
    VALUES
        (actual_node_id,
         record_warren_metrics.node_name,
         metric_doc,
         NULLIF(metric_doc #>> '{system,cpu,usage_pct}', '')::double precision,
         NULLIF(metric_doc #>> '{system,load1}', '')::double precision,
         NULLIF(metric_doc #>> '{system,load5}', '')::double precision,
         NULLIF(metric_doc #>> '{system,load15}', '')::double precision,
         NULLIF(metric_doc #>> '{system,memory,used_bytes}', '')::bigint,
         NULLIF(metric_doc #>> '{system,memory,total_bytes}', '')::bigint,
         NULLIF(metric_doc #>> '{summary,gpu_count}', '')::int,
         NULLIF(metric_doc #>> '{summary,gpu_util_pct}', '')::double precision,
         NULLIF(metric_doc #>> '{summary,gpu_mem_used_bytes}', '')::bigint,
         NULLIF(metric_doc #>> '{summary,gpu_mem_total_bytes}', '')::bigint);

    IF jsonb_typeof(metric_doc->'gpus') = 'array' THEN
        UPDATE rvbbit.warren_nodes
        SET inventory = metric_doc->'gpus'
        WHERE node_id = actual_node_id;
    END IF;
END
$rwm$;

CREATE OR REPLACE FUNCTION rvbbit.prune_warren_metrics(
    retain interval DEFAULT '7 days'::interval
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $pwm$
DECLARE
    deleted_rows bigint;
BEGIN
    IF retain IS NULL OR retain <= interval '0 seconds' THEN
        RAISE EXCEPTION 'retain must be a positive interval';
    END IF;

    DELETE FROM rvbbit.warren_node_metrics
    WHERE collected_at < clock_timestamp() - retain;

    GET DIAGNOSTICS deleted_rows = ROW_COUNT;
    RETURN deleted_rows;
END
$pwm$;

CREATE OR REPLACE FUNCTION rvbbit.enqueue_warren_job(
    job_kind        text,
    job_name        text,
    job_manifest    jsonb,
    target_selector jsonb DEFAULT '{}'::jsonb,
    desired_state   text DEFAULT 'running'
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $ewj$
DECLARE
    actual_job_id uuid;
BEGIN
    IF job_name IS NULL OR btrim(job_name) = '' THEN
        RAISE EXCEPTION 'job_name is required';
    END IF;
    IF jsonb_typeof(job_manifest) <> 'object' THEN
        RAISE EXCEPTION 'job_manifest must be a JSON object';
    END IF;
    IF jsonb_typeof(target_selector) <> 'object' THEN
        RAISE EXCEPTION 'target_selector must be a JSON object';
    END IF;

    INSERT INTO rvbbit.warren_jobs
        (kind, desired_state, name, manifest, target_selector)
    VALUES
        (job_kind, desired_state, job_name, job_manifest, target_selector)
    RETURNING job_id INTO actual_job_id;

    RETURN actual_job_id;
END
$ewj$;

CREATE OR REPLACE FUNCTION rvbbit.deploy_capability(
    capability_manifest jsonb,
    target_selector     jsonb DEFAULT '{}'::jsonb,
    job_name            text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $dcap$
DECLARE
    actual_name text;
BEGIN
    IF jsonb_typeof(capability_manifest) <> 'object' THEN
        RAISE EXCEPTION 'capability_manifest must be a JSON object';
    END IF;
    actual_name := COALESCE(job_name, capability_manifest->>'name');
    IF actual_name IS NULL OR btrim(actual_name) = '' THEN
        actual_name := 'capability_' || substr(md5(capability_manifest::text), 1, 12);
    END IF;

    RETURN rvbbit.enqueue_warren_job(
        'capability',
        actual_name,
        capability_manifest,
        target_selector,
        'running'
    );
END
$dcap$;

DROP FUNCTION IF EXISTS rvbbit.deploy_catalog_capability(text, jsonb, text);
CREATE OR REPLACE FUNCTION rvbbit.deploy_catalog_capability(
    catalog_id      text,
    target_selector jsonb DEFAULT '{}'::jsonb,
    job_name        text DEFAULT NULL,
    install_mode    text DEFAULT 'build'
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $dcc$
DECLARE
    catalog_manifest jsonb;
    catalog_name text;
    catalog_backend_name text;
    catalog_runtime_name text;
    catalog_operator_name text;
    catalog_resource_doc jsonb;
    queued_job_id uuid;
    normalized_install_mode text := lower(coalesce(nullif(btrim(install_mode), ''), 'build'));
    prebuilt_runtime_doc jsonb;
    runtime_doc jsonb;
BEGIN
    IF catalog_id IS NULL OR btrim(catalog_id) = '' THEN
        RAISE EXCEPTION 'catalog_id is required';
    END IF;
    IF jsonb_typeof(target_selector) <> 'object' THEN
        RAISE EXCEPTION 'target_selector must be a JSON object';
    END IF;
    IF normalized_install_mode NOT IN ('build', 'image') THEN
        RAISE EXCEPTION 'install_mode must be build or image';
    END IF;

    SELECT manifest, name, backend_name, runtime_name, operators[1], resource_profile
    INTO catalog_manifest, catalog_name, catalog_backend_name, catalog_runtime_name,
         catalog_operator_name, catalog_resource_doc
    FROM rvbbit.capability_catalog
    WHERE id = btrim(catalog_id)
      AND active;

    IF catalog_manifest IS NULL THEN
        RAISE EXCEPTION 'active capability catalog entry % not found', catalog_id;
    END IF;
    IF jsonb_typeof(catalog_manifest->'resources') IS DISTINCT FROM 'object'
       AND jsonb_typeof(catalog_resource_doc) = 'object'
       AND catalog_resource_doc <> '{}'::jsonb THEN
        catalog_manifest := catalog_manifest || jsonb_build_object('resources', catalog_resource_doc);
    END IF;
    prebuilt_runtime_doc := catalog_manifest->'prebuilt_runtime';
    IF normalized_install_mode = 'image' THEN
        IF jsonb_typeof(prebuilt_runtime_doc) = 'object'
           AND nullif(prebuilt_runtime_doc->>'image', '') IS NOT NULL THEN
            runtime_doc := coalesce(catalog_manifest->'runtime', '{}'::jsonb)
                || jsonb_build_object(
                    'mode', 'image',
                    'image', prebuilt_runtime_doc->>'image',
                    'pull_policy', coalesce(nullif(prebuilt_runtime_doc->>'pull_policy', ''), 'missing')
                );
            catalog_manifest := jsonb_set(catalog_manifest, '{runtime}', runtime_doc, true);
        ELSIF nullif(catalog_manifest #>> '{runtime,image}', '') IS NULL THEN
            RAISE EXCEPTION 'catalog entry % has no prebuilt runtime image', catalog_id;
        END IF;
    ELSIF jsonb_typeof(prebuilt_runtime_doc) = 'object'
          AND catalog_manifest #>> '{runtime,image}' = prebuilt_runtime_doc->>'image' THEN
        runtime_doc := (coalesce(catalog_manifest->'runtime', '{}'::jsonb)
            - 'image' - 'image_digest' - 'pull_policy')
            || jsonb_build_object('mode', 'build');
        catalog_manifest := jsonb_set(catalog_manifest, '{runtime}', runtime_doc, true);
    END IF;

    queued_job_id := rvbbit.deploy_capability(
        catalog_manifest,
        target_selector,
        coalesce(job_name, catalog_name)
    );
    UPDATE rvbbit.warren_jobs AS j
    SET backend_name = coalesce(catalog_backend_name, j.backend_name),
        runtime_name = coalesce(catalog_runtime_name, j.runtime_name),
        operator_name = coalesce(catalog_operator_name, j.operator_name)
    WHERE j.job_id = queued_job_id;
    RETURN queued_job_id;
END
$dcc$;

CREATE OR REPLACE FUNCTION rvbbit.request_warren_deployment_state(
    deployment_id uuid,
    desired_state text
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $rwds$
DECLARE
    d rvbbit.warren_deployments%ROWTYPE;
    normalized_state text := nullif(btrim(desired_state), '');
    lifecycle_manifest jsonb;
    queued_job_id uuid;
BEGIN
    IF normalized_state NOT IN ('stopped', 'removed') THEN
        RAISE EXCEPTION 'desired_state must be stopped or removed';
    END IF;

    SELECT * INTO d
    FROM rvbbit.warren_deployments
    WHERE warren_deployments.deployment_id = request_warren_deployment_state.deployment_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Warren deployment % not found', deployment_id;
    END IF;
    IF d.status = 'removed' THEN
        RAISE EXCEPTION 'Warren deployment % is already removed', deployment_id;
    END IF;
    IF d.status = 'stopped' AND normalized_state = 'stopped' THEN
        RAISE EXCEPTION 'Warren deployment % is already stopped', deployment_id;
    END IF;
    IF d.status = 'stopping' THEN
        RAISE EXCEPTION 'Warren deployment % already has a lifecycle request in progress',
            deployment_id;
    END IF;

    lifecycle_manifest := coalesce(d.manifest, '{}'::jsonb)
        || jsonb_build_object(
            'warren_deployment',
            jsonb_build_object(
                'deployment_id', d.deployment_id,
                'node_id', d.node_id,
                'node_name', d.node_name,
                'kind', d.kind,
                'name', d.name,
                'status', d.status,
                'endpoint_url', d.endpoint_url,
                'backend_name', d.backend_name,
                'operator_name', d.operator_name,
                'runtime_name', d.runtime_name,
                'compose_project', d.compose_project,
                'work_dir', d.work_dir
            )
        );

    queued_job_id := rvbbit.enqueue_warren_job(
        d.kind,
        d.name,
        lifecycle_manifest,
        '{}'::jsonb,
        normalized_state
    );

    UPDATE rvbbit.warren_jobs AS j
    SET backend_name = d.backend_name,
        operator_name = d.operator_name,
        runtime_name = d.runtime_name,
        endpoint_url = d.endpoint_url,
        progress = jsonb_build_object(
            'phase', 'queued',
            'desired_state', normalized_state,
            'deployment_id', d.deployment_id,
            'node_name', d.node_name,
            'queued_at', clock_timestamp()
        )
    WHERE j.job_id = queued_job_id;

    UPDATE rvbbit.warren_deployments AS existing
    SET status = 'stopping',
        error = NULL,
        health = existing.health || jsonb_build_object(
            'lifecycle_request',
            jsonb_build_object(
                'desired_state', normalized_state,
                'job_id', queued_job_id,
                'requested_at', clock_timestamp()
            )
        )
    WHERE existing.deployment_id = d.deployment_id;

    RETURN queued_job_id;
END
$rwds$;

CREATE OR REPLACE FUNCTION rvbbit.request_warren_deployment_stop(
    deployment_id uuid
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    RETURN rvbbit.request_warren_deployment_state(deployment_id, 'stopped');
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.request_warren_deployment_remove(
    deployment_id uuid
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    RETURN rvbbit.request_warren_deployment_state(deployment_id, 'removed');
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.request_warren_deployment_redeploy(
    deployment_id uuid,
    target_selector jsonb DEFAULT NULL,
    job_name text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $rwdr$
DECLARE
    d rvbbit.warren_deployments%ROWTYPE;
    source_job rvbbit.warren_jobs%ROWTYPE;
    redeploy_manifest jsonb;
    redeploy_selector jsonb;
    queued_job_id uuid;
    actual_job_name text;
BEGIN
    IF target_selector IS NOT NULL AND jsonb_typeof(target_selector) <> 'object' THEN
        RAISE EXCEPTION 'target_selector must be a JSON object';
    END IF;

    SELECT * INTO d
    FROM rvbbit.warren_deployments
    WHERE warren_deployments.deployment_id = request_warren_deployment_redeploy.deployment_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Warren deployment % not found', deployment_id;
    END IF;

    IF d.job_id IS NOT NULL THEN
        SELECT * INTO source_job
        FROM rvbbit.warren_jobs
        WHERE warren_jobs.job_id = d.job_id;
    END IF;

    redeploy_manifest := coalesce(
        NULLIF(d.manifest - 'warren_deployment', '{}'::jsonb),
        NULLIF(source_job.manifest - 'warren_deployment', '{}'::jsonb)
    );
    IF redeploy_manifest IS NULL OR jsonb_typeof(redeploy_manifest) <> 'object' THEN
        RAISE EXCEPTION 'Warren deployment % has no reusable manifest', deployment_id;
    END IF;

    redeploy_selector := coalesce(
        target_selector,
        source_job.target_selector,
        CASE
            WHEN jsonb_typeof(d.health->'target_selector') = 'object'
            THEN d.health->'target_selector'
            ELSE NULL
        END,
        '{}'::jsonb
    );

    actual_job_name := coalesce(nullif(btrim(job_name), ''), d.name);
    queued_job_id := rvbbit.enqueue_warren_job(
        d.kind,
        actual_job_name,
        redeploy_manifest,
        redeploy_selector,
        'running'
    );

    UPDATE rvbbit.warren_jobs AS j
    SET backend_name = d.backend_name,
        operator_name = d.operator_name,
        runtime_name = d.runtime_name,
        progress = jsonb_build_object(
            'phase', 'queued',
            'desired_state', 'running',
            'redeploy_of', d.deployment_id,
            'previous_status', d.status,
            'queued_at', clock_timestamp()
        )
    WHERE j.job_id = queued_job_id;

    UPDATE rvbbit.warren_deployments AS existing
    SET health = existing.health || jsonb_build_object(
            'redeploy_request',
            jsonb_build_object(
                'job_id', queued_job_id,
                'requested_at', clock_timestamp()
            )
        )
    WHERE existing.deployment_id = d.deployment_id;

    RETURN queued_job_id;
END
$rwdr$;

CREATE OR REPLACE FUNCTION rvbbit.report_warren_deployment_observation(
    deployment_id uuid,
    node_name text,
    observed_state text,
    observation jsonb DEFAULT '{}'::jsonb,
    observation_error text DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql
VOLATILE
AS $rwdo$
DECLARE
    normalized_observed text := coalesce(nullif(btrim(observed_state), ''), 'unknown');
    normalized_observation jsonb := coalesce(observation, '{}'::jsonb);
    current_status text;
    current_desired_state text;
    next_status text;
BEGIN
    IF jsonb_typeof(normalized_observation) <> 'object' THEN
        RAISE EXCEPTION 'observation must be a JSON object';
    END IF;

    SELECT status, health #>> '{lifecycle_request,desired_state}'
    INTO current_status, current_desired_state
    FROM rvbbit.warren_deployments AS d
    WHERE d.deployment_id = report_warren_deployment_observation.deployment_id
      AND d.node_name = report_warren_deployment_observation.node_name
    FOR UPDATE;

    IF current_status IS NULL THEN
        RAISE EXCEPTION 'Warren deployment % not found for node %',
            deployment_id, node_name;
    END IF;

    next_status := CASE
        WHEN current_status IN ('starting', 'running', 'drifted')
             AND normalized_observed IN ('running', 'healthy') THEN 'running'
        WHEN current_status IN ('starting', 'running', 'drifted')
             AND normalized_observed IN ('missing', 'exited', 'dead', 'stopped') THEN 'drifted'
        WHEN current_status IN ('stopped', 'removed', 'orphaned')
             AND normalized_observed IN ('running', 'healthy') THEN 'orphaned'
        WHEN current_status = 'orphaned'
             AND normalized_observed IN ('missing', 'exited', 'dead', 'stopped')
        THEN CASE WHEN current_desired_state = 'removed' THEN 'removed' ELSE 'stopped' END
        ELSE current_status
    END;

    UPDATE rvbbit.warren_deployments AS d
    SET status = next_status,
        health = d.health || jsonb_build_object(
            'last_reconcile',
            normalized_observation || jsonb_build_object(
                'observed_state', normalized_observed,
                'observed_at', clock_timestamp()
            )
        ),
        error = CASE
            WHEN next_status IN ('drifted', 'orphaned')
            THEN coalesce(observation_error, 'Warren deployment state drift detected')
            WHEN d.status IN ('drifted', 'orphaned') AND next_status = 'running'
            THEN NULL
            ELSE d.error
        END,
        stopped_at = CASE
            WHEN next_status IN ('stopped', 'removed') THEN coalesce(d.stopped_at, clock_timestamp())
            WHEN next_status = 'running' THEN NULL
            ELSE d.stopped_at
        END
    WHERE d.deployment_id = report_warren_deployment_observation.deployment_id;

    RETURN next_status;
END
$rwdo$;

CREATE OR REPLACE FUNCTION rvbbit.claim_warren_job(
    node_name text
) RETURNS TABLE (
    job_id uuid,
    kind text,
    desired_state text,
    name text,
    manifest jsonb,
    target_selector jsonb
)
LANGUAGE plpgsql
VOLATILE
AS $cwj$
BEGIN
    RETURN QUERY
    WITH node AS (
        SELECT n.node_id, n.name, n.labels
        FROM rvbbit.warren_nodes n
        WHERE n.name = claim_warren_job.node_name
          AND n.status IN ('ready', 'busy')
          AND n.last_heartbeat IS NOT NULL
          AND clock_timestamp() - n.last_heartbeat < interval '2 minutes'
    ),
    picked AS (
        SELECT
            j.job_id,
            req.gpu_reservation_required,
            req.vram_required_bytes,
            req.gpu_placement,
            cap.gpu_available_bytes,
            cap.single_gpu_mem_usable_bytes
        FROM rvbbit.warren_jobs j
        CROSS JOIN node n
        LEFT JOIN rvbbit.warren_gpu_capacity cap
          ON cap.node_id = n.node_id
        CROSS JOIN LATERAL (
            SELECT
                CASE WHEN j.desired_state = 'running' THEN (
                    rvbbit.capability_gpu_required(j.manifest)
                    OR coalesce(NULLIF(j.target_selector->>'gpu', '')::boolean, false)
                ) ELSE false END AS gpu_reservation_required,
                CASE WHEN j.desired_state = 'running'
                     THEN rvbbit.capability_vram_required_bytes(j.manifest)
                     ELSE NULL::bigint
                END AS vram_required_bytes,
                coalesce(NULLIF(j.manifest #>> '{resources,gpu,placement}', ''), 'single_gpu') AS gpu_placement
        ) req
        WHERE j.status = 'queued'
          -- model_training jobs are compute, not deployment: they are claimed by
          -- a trainer worker via rvbbit.claim_model_training_job, never by the
          -- deploy agent. Excluding them here prevents the deploy/train race.
          AND j.kind <> 'model_training'
          AND (
              (j.desired_state = 'running' AND n.labels @> j.target_selector)
              OR (
                  j.desired_state IN ('stopped', 'removed')
                  AND j.manifest #>> '{warren_deployment,node_name}' = n.name
              )
          )
          AND (
              NOT req.gpu_reservation_required
              OR req.vram_required_bytes IS NULL
              OR (
                  req.vram_required_bytes <= coalesce(cap.gpu_available_bytes, 0)
                  AND (
                      req.gpu_placement <> 'single_gpu'
                      OR req.vram_required_bytes <= coalesce(cap.single_gpu_mem_usable_bytes, 0)
                  )
              )
          )
        ORDER BY j.created_at
        LIMIT 1
        FOR UPDATE OF j SKIP LOCKED
    ),
    updated AS (
        UPDATE rvbbit.warren_jobs j
        SET status = 'running',
            phase = 'claimed',
            manifest = CASE
                WHEN picked.gpu_reservation_required
                     AND picked.vram_required_bytes IS NOT NULL
                THEN jsonb_set(j.manifest, '{resources,gpu,reserved}', 'true'::jsonb, true)
                ELSE j.manifest
            END,
            claimed_by = claim_warren_job.node_name,
            claimed_at = clock_timestamp(),
            started_at = COALESCE(started_at, clock_timestamp()),
            attempts = attempts + 1,
            progress = progress || jsonb_build_object(
                'phase', 'claimed',
                'desired_state', j.desired_state,
                'node_name', claim_warren_job.node_name,
                'claimed_at', clock_timestamp(),
                'gpu_reserved', picked.gpu_reservation_required,
                'gpu_placement', picked.gpu_placement,
                'vram_required_bytes', picked.vram_required_bytes,
                'gpu_available_bytes', picked.gpu_available_bytes,
                'single_gpu_mem_usable_bytes', picked.single_gpu_mem_usable_bytes
            )
        FROM picked
        WHERE j.job_id = picked.job_id
        RETURNING j.job_id, j.kind, j.desired_state, j.name, j.manifest,
                  j.target_selector
    )
    SELECT u.job_id, u.kind, u.desired_state, u.name, u.manifest,
           u.target_selector
    FROM updated u;
END
$cwj$;

CREATE OR REPLACE FUNCTION rvbbit.update_warren_job_progress(
    job_id       uuid,
    node_name    text,
    job_phase    text,
    progress_doc jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $uwjp$
DECLARE
    normalized_phase text := nullif(btrim(job_phase), '');
    normalized_doc jsonb := coalesce(progress_doc, '{}'::jsonb);
BEGIN
    IF normalized_phase IS NULL THEN
        RAISE EXCEPTION 'job_phase is required';
    END IF;
    IF jsonb_typeof(normalized_doc) <> 'object' THEN
        RAISE EXCEPTION 'progress_doc must be a JSON object';
    END IF;

    UPDATE rvbbit.warren_jobs j
    SET phase = normalized_phase,
        progress = j.progress
            || normalized_doc
            || jsonb_build_object(
                'phase', normalized_phase,
                'node_name', update_warren_job_progress.node_name,
                'updated_at', clock_timestamp()
            ),
        logs = j.logs || jsonb_build_object(
            'last_phase', normalized_phase,
            'last_phase_at', clock_timestamp()
        )
    WHERE j.job_id = update_warren_job_progress.job_id
      AND j.status = 'running'
      AND j.claimed_by = update_warren_job_progress.node_name;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'running Warren job % is not claimed by node %',
            job_id, node_name;
    END IF;
END
$uwjp$;

CREATE OR REPLACE FUNCTION rvbbit.complete_warren_job(
    job_id            uuid,
    node_name         text,
    deployment_status text DEFAULT 'running',
    endpoint_url      text DEFAULT NULL,
    backend_name      text DEFAULT NULL,
    operator_name     text DEFAULT NULL,
    deploy_manifest   jsonb DEFAULT '{}'::jsonb,
    compose_project   text DEFAULT NULL,
    work_dir          text DEFAULT NULL,
    health            jsonb DEFAULT '{}'::jsonb,
    logs              jsonb DEFAULT '{}'::jsonb,
    runtime_name      text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $cwjd$
DECLARE
    actual_node_id uuid;
    actual_kind text;
    actual_name text;
    actual_desired_state text;
    completion_phase text;
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = complete_warren_job.node_name;

    IF actual_node_id IS NULL THEN
        RAISE EXCEPTION 'warren node % is not registered', node_name;
    END IF;

    IF deployment_status NOT IN ('starting', 'running', 'stopping', 'stopped',
                                 'failed', 'removed', 'drifted', 'orphaned') THEN
        RAISE EXCEPTION 'unsupported Warren deployment status %', deployment_status;
    END IF;

    SELECT kind, name, desired_state
    INTO actual_kind, actual_name, actual_desired_state
    FROM rvbbit.warren_jobs
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

    IF actual_kind IS NULL THEN
        RAISE EXCEPTION 'warren job % not found', job_id;
    END IF;

    completion_phase := CASE
        WHEN deployment_status = 'running' THEN 'ready'
        ELSE deployment_status
    END;

    UPDATE rvbbit.warren_jobs
    SET status = 'completed',
        phase = completion_phase,
        endpoint_url = complete_warren_job.endpoint_url,
        backend_name = complete_warren_job.backend_name,
        operator_name = complete_warren_job.operator_name,
        runtime_name = complete_warren_job.runtime_name,
        progress = progress || jsonb_build_object(
            'phase', completion_phase,
            'desired_state', actual_desired_state,
            'deployment_status', deployment_status,
            'endpoint_url', complete_warren_job.endpoint_url,
            'backend_name', complete_warren_job.backend_name,
            'operator_name', complete_warren_job.operator_name,
            'runtime_name', complete_warren_job.runtime_name,
            'finished_at', clock_timestamp()
        ),
        logs = complete_warren_job.logs,
        error = NULL,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

    -- model_training jobs are compute, not managed serving deployments: the
    -- warren job is now marked completed, but we do not create a
    -- warren_deployments row for it (the reconciler must not try to manage a
    -- training run, and 'model_training' is not a deployment kind).
    IF actual_kind = 'model_training' THEN
        RETURN;
    END IF;

    UPDATE rvbbit.warren_deployments AS d
    SET job_id = complete_warren_job.job_id,
        node_id = actual_node_id,
        status = deployment_status,
        endpoint_url = complete_warren_job.endpoint_url,
        backend_name = complete_warren_job.backend_name,
        operator_name = complete_warren_job.operator_name,
        runtime_name = complete_warren_job.runtime_name,
        manifest = complete_warren_job.deploy_manifest,
        compose_project = complete_warren_job.compose_project,
        work_dir = complete_warren_job.work_dir,
        health = complete_warren_job.health,
        error = NULL,
        stopped_at = CASE
            WHEN deployment_status IN ('starting', 'running', 'stopping') THEN NULL
            ELSE coalesce(d.stopped_at, clock_timestamp())
        END
    WHERE d.deployment_id = (
        SELECT d2.deployment_id
        FROM rvbbit.warren_deployments d2
        WHERE d2.node_name = complete_warren_job.node_name
          AND d2.kind = actual_kind
          AND d2.name = actual_name
          AND d2.status IN ('starting', 'running', 'stopping', 'stopped',
                           'failed', 'removed', 'drifted', 'orphaned')
        ORDER BY
            CASE
                WHEN d2.status IN ('starting', 'running', 'stopping') THEN 0
                WHEN d2.job_id = complete_warren_job.job_id THEN 1
                ELSE 2
            END,
            d2.updated_at DESC,
            d2.created_at DESC
        LIMIT 1
    );

    IF NOT FOUND THEN
        INSERT INTO rvbbit.warren_deployments
            (job_id, node_id, node_name, kind, name, status, endpoint_url,
             backend_name, operator_name, runtime_name, manifest, compose_project, work_dir,
             health, error)
        VALUES
            (complete_warren_job.job_id, actual_node_id, complete_warren_job.node_name,
             actual_kind, actual_name, deployment_status, endpoint_url,
             backend_name, operator_name, runtime_name, deploy_manifest, compose_project, work_dir,
             health, NULL)
        ON CONFLICT ON CONSTRAINT warren_deployments_job_id_key DO UPDATE SET
            node_id = EXCLUDED.node_id,
            node_name = EXCLUDED.node_name,
            status = EXCLUDED.status,
            endpoint_url = EXCLUDED.endpoint_url,
            backend_name = EXCLUDED.backend_name,
            operator_name = EXCLUDED.operator_name,
            runtime_name = EXCLUDED.runtime_name,
            manifest = EXCLUDED.manifest,
            compose_project = EXCLUDED.compose_project,
            work_dir = EXCLUDED.work_dir,
            health = EXCLUDED.health,
            error = NULL,
            stopped_at = CASE
                WHEN EXCLUDED.status IN ('starting', 'running', 'stopping') THEN NULL
                ELSE coalesce(rvbbit.warren_deployments.stopped_at, clock_timestamp())
            END;
    END IF;

    IF deployment_status IN ('stopped', 'removed') AND complete_warren_job.runtime_name IS NOT NULL THEN
        IF to_regclass('rvbbit.python_runtimes') IS NOT NULL THEN
            UPDATE rvbbit.python_runtimes AS r
            SET status = 'disabled',
                health = r.health || jsonb_build_object(
                    'warren_lifecycle', deployment_status,
                    'warren_job_id', complete_warren_job.job_id,
                    'updated_at', clock_timestamp()
                )
            WHERE r.name = complete_warren_job.runtime_name
              AND r.runtime_source = 'warren';
        END IF;
        IF to_regclass('rvbbit.mcp_gateways') IS NOT NULL THEN
            UPDATE rvbbit.mcp_gateways AS g
            SET status = 'disabled',
                health = g.health || jsonb_build_object(
                    'warren_lifecycle', deployment_status,
                    'warren_job_id', complete_warren_job.job_id,
                    'updated_at', clock_timestamp()
                )
            WHERE g.name = complete_warren_job.runtime_name
              AND g.gateway_source = 'warren';
        END IF;
    END IF;

    IF deployment_status IN ('stopped', 'removed') AND complete_warren_job.backend_name IS NOT NULL THEN
        PERFORM rvbbit.reload_backends();
    END IF;
END
$cwjd$;

CREATE OR REPLACE FUNCTION rvbbit.fail_warren_job(
    job_id uuid,
    node_name text,
    error text,
    logs jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $fwj$
DECLARE
    actual_node_id uuid;
    actual_kind text;
    actual_name text;
    actual_manifest jsonb;
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = fail_warren_job.node_name;

    SELECT kind, name, manifest INTO actual_kind, actual_name, actual_manifest
    FROM rvbbit.warren_jobs
    WHERE warren_jobs.job_id = fail_warren_job.job_id;

    UPDATE rvbbit.warren_jobs
    SET status = 'failed',
        phase = 'failed',
        error = fail_warren_job.error,
        progress = progress || jsonb_build_object(
            'phase', 'failed',
            'error', fail_warren_job.error,
            'failed_at', clock_timestamp(),
            'node_name', fail_warren_job.node_name
        ),
        logs = fail_warren_job.logs,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = fail_warren_job.job_id;

    -- model_training jobs are compute, not managed serving deployments: do not
    -- record them in warren_deployments (the reconciler must not manage a
    -- training run, and 'model_training' is not a deployment kind).
    IF actual_kind IS NOT NULL AND actual_kind <> 'model_training' THEN
        INSERT INTO rvbbit.warren_deployments
            (job_id, node_id, node_name, kind, name, status, manifest, error,
             health)
        VALUES
            (fail_warren_job.job_id, actual_node_id, fail_warren_job.node_name,
             actual_kind, actual_name, 'failed', coalesce(actual_manifest, '{}'::jsonb),
             fail_warren_job.error, fail_warren_job.logs)
        ON CONFLICT ON CONSTRAINT warren_deployments_job_id_key DO UPDATE SET
            status = 'failed',
            manifest = EXCLUDED.manifest,
            error = EXCLUDED.error,
            health = EXCLUDED.health;
    END IF;
END
$fwj$;

-- ---------------------------------------------------------------------------
-- MCP servers — the Model Context Protocol bridge.
--
-- An MCP server is an external process (stdio subprocess or HTTP service)
-- that exposes a set of typed tools — Anthropic's standard for letting
-- agents talk to filesystems, APIs, etc. rvbbit brings those tools into
-- SQL: register a server here, refresh its tools, then call them via
-- rvbbit.mcp_call(server, tool, args). Phase 2 will also let operators
-- carry an `mcp` node alongside llm/specialist/code/sql.
--
-- A separate `mcp-gateway` sidecar daemon (Python) hosts the actual
-- subprocesses and proxies HTTP MCP servers. PG backends never fork; they
-- POST to the gateway over HTTP and the gateway reads this catalog to
-- know what to spawn. Subprocess lifecycle, per-server locking,
-- re-introspection, and crash recovery all live in the sidecar.
-- ---------------------------------------------------------------------------

INSERT INTO rvbbit.settings (key, value)
VALUES ('mcp_gateway_endpoint', to_jsonb('http://mcp-gateway:9180'::text))
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION rvbbit.mcp_gateway_endpoint()
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(
        (SELECT value #>> '{}' FROM rvbbit.settings WHERE key = 'mcp_gateway_endpoint'),
        'http://mcp-gateway:9180'
    )
$$;

CREATE OR REPLACE FUNCTION rvbbit.set_mcp_gateway_endpoint(endpoint_url text)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    normalized text := nullif(btrim(endpoint_url), '');
BEGIN
    IF normalized IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_mcp_gateway_endpoint: endpoint_url cannot be empty';
    END IF;
    IF normalized !~ '^https?://' THEN
        RAISE EXCEPTION 'rvbbit.set_mcp_gateway_endpoint: endpoint_url must be an http(s) URL';
    END IF;
    INSERT INTO rvbbit.settings (key, value, updated_at)
    VALUES ('mcp_gateway_endpoint', to_jsonb(rtrim(normalized, '/')), clock_timestamp())
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        updated_at = clock_timestamp();

    BEGIN
        PERFORM rvbbit.reload_mcp_gateway();
    EXCEPTION WHEN undefined_function THEN
        NULL;
    END;

    RETURN rtrim(normalized, '/');
END
$$;

CREATE TABLE rvbbit.mcp_gateways (
    name                  text PRIMARY KEY,
    endpoint_url          text NOT NULL,
    status                text NOT NULL DEFAULT 'ready',
    labels                jsonb NOT NULL DEFAULT '{}'::jsonb,
    gateway_source        text NOT NULL DEFAULT 'manual',
    warren_deployment_id  uuid,
    install_manifest      jsonb NOT NULL DEFAULT '{}'::jsonb,
    health                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by            oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT mcp_gateways_name_check CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT mcp_gateways_status_check CHECK (
        status IN ('starting', 'ready', 'failed', 'disabled')
    ),
    CONSTRAINT mcp_gateways_endpoint_check CHECK (endpoint_url ~ '^https?://'),
    CONSTRAINT mcp_gateways_labels_is_object CHECK (jsonb_typeof(labels) = 'object'),
    CONSTRAINT mcp_gateways_manifest_is_object CHECK (jsonb_typeof(install_manifest) = 'object'),
    CONSTRAINT mcp_gateways_health_is_object CHECK (jsonb_typeof(health) = 'object')
);

CREATE OR REPLACE FUNCTION rvbbit.touch_mcp_gateways_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

CREATE TRIGGER mcp_gateways_touch_updated_at
    BEFORE UPDATE ON rvbbit.mcp_gateways
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_mcp_gateways_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.require_mcp_gateway_admin()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper
    ) AND NOT (
        EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rvbbit_warren')
        AND pg_has_role(current_user, 'rvbbit_warren', 'member')
    ) THEN
        RAISE EXCEPTION 'rvbbit MCP gateway DDL requires a superuser or rvbbit_warren role membership in this release';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.register_mcp_gateway(
    gateway_name text,
    endpoint_url text,
    gateway_status text DEFAULT 'ready',
    gateway_labels jsonb DEFAULT '{}'::jsonb,
    gateway_source text DEFAULT 'manual',
    warren_deployment_id uuid DEFAULT NULL,
    install_manifest jsonb DEFAULT '{}'::jsonb,
    health jsonb DEFAULT '{}'::jsonb,
    set_default boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_name text := nullif(btrim(gateway_name), '');
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
    normalized_status text := coalesce(nullif(btrim(gateway_status), ''), 'ready');
    normalized_source text := coalesce(nullif(btrim(gateway_source), ''), 'manual');
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_mcp_gateway_admin();
    IF normalized_name IS NULL THEN
        RAISE EXCEPTION 'rvbbit.register_mcp_gateway: gateway_name cannot be empty';
    END IF;
    IF normalized_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.register_mcp_gateway: gateway_name must be an identifier-like name';
    END IF;
    IF normalized_endpoint IS NULL OR normalized_endpoint !~ '^https?://' THEN
        RAISE EXCEPTION 'rvbbit.register_mcp_gateway: endpoint_url must be an http(s) URL';
    END IF;
    IF normalized_status NOT IN ('starting', 'ready', 'failed', 'disabled') THEN
        RAISE EXCEPTION 'rvbbit.register_mcp_gateway: unsupported status "%"', gateway_status;
    END IF;
    IF jsonb_typeof(coalesce(gateway_labels, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_mcp_gateway: gateway_labels must be a JSON object';
    END IF;
    IF jsonb_typeof(coalesce(install_manifest, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_mcp_gateway: install_manifest must be a JSON object';
    END IF;
    IF jsonb_typeof(coalesce(health, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_mcp_gateway: health must be a JSON object';
    END IF;

    INSERT INTO rvbbit.mcp_gateways
        (name, endpoint_url, status, labels, gateway_source,
         warren_deployment_id, install_manifest, health)
    VALUES
        (normalized_name, rtrim(normalized_endpoint, '/'), normalized_status,
         coalesce(gateway_labels, '{}'::jsonb), normalized_source,
         register_mcp_gateway.warren_deployment_id,
         coalesce(install_manifest, '{}'::jsonb), coalesce(health, '{}'::jsonb))
    ON CONFLICT (name) DO UPDATE SET
        endpoint_url = EXCLUDED.endpoint_url,
        status = EXCLUDED.status,
        labels = EXCLUDED.labels,
        gateway_source = EXCLUDED.gateway_source,
        warren_deployment_id = EXCLUDED.warren_deployment_id,
        install_manifest = EXCLUDED.install_manifest,
        health = EXCLUDED.health;

    IF coalesce(set_default, true) AND normalized_status = 'ready' THEN
        PERFORM rvbbit.set_mcp_gateway_endpoint(rtrim(normalized_endpoint, '/'));
    ELSE
        BEGIN
            PERFORM rvbbit.reload_mcp_gateway();
        EXCEPTION WHEN undefined_function THEN
            NULL;
        END;
    END IF;

    SELECT to_jsonb(g) INTO row_doc FROM rvbbit.mcp_gateways g WHERE g.name = normalized_name;
    RETURN row_doc;
END
$$;

CREATE TABLE rvbbit.mcp_servers (
    name             text PRIMARY KEY,
    transport        text NOT NULL DEFAULT 'stdio',
    command          text,                          -- stdio: executable name
    args             text[],                        -- stdio: argv tail
    env              jsonb,                         -- stdio: env (${VAR} refs resolved at spawn)
    url              text,                          -- http: full MCP endpoint URL
    auth_header_env  text,                          -- http: env var holding the bearer token
    timeout_ms       int  NOT NULL DEFAULT 30000,
    description      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT mcp_servers_transport_check
        CHECK (transport IN ('stdio', 'http')),
    CONSTRAINT mcp_servers_stdio_needs_command
        CHECK (transport <> 'stdio' OR command IS NOT NULL),
    CONSTRAINT mcp_servers_http_needs_url
        CHECK (transport <> 'http'  OR url IS NOT NULL)
);

-- One row per (server, tool) discovered at registration / refresh time.
-- Populated by rvbbit.refresh_mcp_server(name).
CREATE TABLE rvbbit.mcp_tools (
    server         text NOT NULL REFERENCES rvbbit.mcp_servers(name) ON DELETE CASCADE,
    name           text NOT NULL,
    description    text,
    input_schema   jsonb,                          -- the MCP tool's JSON schema for args
    discovered_at  timestamptz NOT NULL DEFAULT now(),
    -- Phase 4: selective result caching. Opt-in per tool via
    -- rvbbit.set_mcp_tool_caching(); idempotent tools (get_*, list_*,
    -- search_*) benefit; tools with side effects must stay un-cached.
    cacheable      boolean NOT NULL DEFAULT false,
    ttl_seconds    int,                            -- NULL = no expiry
    PRIMARY KEY (server, name)
);

-- One row per discovered MCP resource (URI-addressable read-only data the
-- server exposes). Populated by rvbbit.refresh_mcp_server(name); read via
-- rvbbit.mcp_resource(server, uri).
CREATE TABLE rvbbit.mcp_resources (
    server         text NOT NULL REFERENCES rvbbit.mcp_servers(name) ON DELETE CASCADE,
    uri            text NOT NULL,
    name           text,
    description    text,
    mime_type      text,
    discovered_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server, uri)
);

-- Per-call audit log. Every mcp_call() lands here — successful, tool-
-- isError, AND cache-hit alike — so the future UI can show the full
-- history. (Transport-level failures, where the gateway is unreachable,
-- raise a SQL error and roll back; the row is lost. That's a known
-- limitation.) query_id correlates with rvbbit.receipts.query_id when
-- called inside an operator pipeline (wired in Phase 2's `mcp` node kind).
CREATE TABLE rvbbit.mcp_invocations (
    id             bigserial PRIMARY KEY,
    server         text NOT NULL,                  -- not FK: audit survives drop_mcp_server
    tool           text NOT NULL,
    args           jsonb,
    output         jsonb,
    error          text,                           -- text of MCP isError result, or NULL
    latency_ms     int,
    cache_hit      boolean NOT NULL DEFAULT false, -- Phase 4: served from mcp_cache
    query_id       uuid,
    invocation_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Phase 4: cache of MCP tool results, opt-in per tool via
-- mcp_tools.cacheable. Keyed by (server, tool, args_hash) where args_hash
-- is blake3 over the canonical (sorted-key) JSON of args. Updated by
-- mcp_call on a cache miss; consulted on every mcp_call.
CREATE TABLE rvbbit.mcp_cache (
    server      text NOT NULL,
    tool        text NOT NULL,
    args_hash   text NOT NULL,                     -- 32-char hex (128 bits of blake3)
    args        jsonb,                             -- kept for human/debug; not load-bearing
    output      jsonb NOT NULL,
    cached_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (server, tool, args_hash)
);

CREATE INDEX mcp_invocations_server_time_idx
    ON rvbbit.mcp_invocations (server, invocation_at DESC);
CREATE INDEX mcp_invocations_query_idx
    ON rvbbit.mcp_invocations (query_id);

-- DDL helper: register (or upsert) an MCP server.
CREATE OR REPLACE FUNCTION rvbbit.register_mcp_server(
    server_name        text,
    server_transport   text   DEFAULT 'stdio',
    server_command     text   DEFAULT NULL,
    server_args        text[] DEFAULT NULL,
    server_env         jsonb  DEFAULT NULL,
    server_url         text   DEFAULT NULL,
    server_auth_env    text   DEFAULT NULL,
    server_timeout_ms  int    DEFAULT 30000,
    server_description text   DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $rm$
BEGIN
    -- security-03: writing mcp_servers == arbitrary command execution on the
    -- gateway host (command/args are spawned). Gate it like the other gateway
    -- DDL instead of leaving the most dangerous registration ungated.
    PERFORM rvbbit.require_mcp_gateway_admin();
    INSERT INTO rvbbit.mcp_servers
        (name, transport, command, args, env, url, auth_header_env,
         timeout_ms, description)
    VALUES
        (server_name, server_transport, server_command, server_args,
         server_env, server_url, server_auth_env, server_timeout_ms,
         server_description)
    ON CONFLICT (name) DO UPDATE SET
        transport       = EXCLUDED.transport,
        command         = EXCLUDED.command,
        args            = EXCLUDED.args,
        env             = EXCLUDED.env,
        url             = EXCLUDED.url,
        auth_header_env = EXCLUDED.auth_header_env,
        timeout_ms      = EXCLUDED.timeout_ms,
        description     = EXCLUDED.description;
END
$rm$;

-- DDL helper: deregister an MCP server. Cascades to mcp_tools and
-- mcp_resources rows; the mcp_invocations audit log and mcp_cache rows
-- are preserved (no FK).
CREATE OR REPLACE FUNCTION rvbbit.drop_mcp_server(server_name text)
RETURNS void
LANGUAGE plpgsql
AS $rm$
BEGIN
    DELETE FROM rvbbit.mcp_servers WHERE name = server_name;
END
$rm$;

-- ---------------------------------------------------------------------------
-- Package a registered + introspected MCP server as a publishable catalog
-- entry. Reads mcp_servers/mcp_tools/mcp_resources and returns
-- { catalog_entry, manifest } — the connection spec with secret VALUES stripped
-- to *declared* inputs (every ${VAR} env ref + the http auth-header env var),
-- plus the tool surface (-> namespaced operators) and resource surface
-- (-> table-functions). So a published entry shows "adds N operators + M
-- tables" and the required keys with NONE of the publisher's secrets. Review /
-- annotate the result, then publish via rvbbit.upsert_capability_catalog_entry,
-- or use rvbbit.publish_mcp_capability() to scan + publish in one shot.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rvbbit.export_mcp_manifest(server_name text)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $emm$
DECLARE
    s rvbbit.mcp_servers%ROWTYPE;
    v_secrets jsonb; v_tools jsonb; v_resources jsonb; v_operators text[];
    v_op_defs jsonb;
    v_n_tools int; v_n_res int; v_manifest jsonb; v_entry jsonb;
BEGIN
    SELECT * INTO s FROM rvbbit.mcp_servers WHERE name = server_name;
    IF NOT FOUND THEN RAISE EXCEPTION 'mcp server % is not registered', server_name; END IF;

    -- Declared secrets: every ${VAR} referenced in env values, plus the http
    -- auth-header env var. Values are NEVER included — only the names, which
    -- the installer fills in at install time (pushed to the gateway, not PG).
    WITH vars AS (
        SELECT DISTINCT m[1] AS var
        FROM jsonb_each_text(coalesce(s.env, '{}'::jsonb)) e,
             LATERAL regexp_matches(e.value, '\$\{(\w+)\}', 'g') AS m
        UNION
        SELECT s.auth_header_env WHERE s.auth_header_env IS NOT NULL
    )
    SELECT coalesce(jsonb_agg(jsonb_build_object(
        'name', var, 'env_var', var, 'required', true, 'secret', true,
        'label', var, 'help', '', 'link', '') ORDER BY var), '[]'::jsonb)
    INTO v_secrets FROM vars WHERE var IS NOT NULL;

    SELECT coalesce(jsonb_agg(jsonb_build_object('name', t.name, 'description', t.description,
        'input_schema', coalesce(t.input_schema, '{}'::jsonb), 'cacheable', t.cacheable,
        'ttl_seconds', t.ttl_seconds) ORDER BY t.name), '[]'::jsonb), count(*)
    INTO v_tools, v_n_tools FROM rvbbit.mcp_tools t WHERE t.server = server_name;

    SELECT coalesce(jsonb_agg(jsonb_build_object('uri', r.uri, 'name', r.name,
        'description', r.description, 'mime_type', r.mime_type) ORDER BY r.uri), '[]'::jsonb), count(*)
    INTO v_resources, v_n_res FROM rvbbit.mcp_resources r WHERE r.server = server_name;

    -- Each tool becomes a server-namespaced operator (avoids cross-server
    -- collisions on common names like `search`). Tools that already
    -- self-namespace (e.g. firecrawl's `firecrawl_scrape`) keep their name
    -- rather than getting a doubled `firecrawl_firecrawl_` prefix.
    SELECT coalesce(array_agg(server_name || '_' ||
        CASE WHEN left(t.name, length(server_name) + 1) = server_name || '_'
             THEN substr(t.name, length(server_name) + 2) ELSE t.name END
        ORDER BY t.name), ARRAY[]::text[])
    INTO v_operators FROM rvbbit.mcp_tools t WHERE t.server = server_name;

    -- Rich operator defs (name + typed arg signature + doc) so the UI can show
    -- the call signature in operator tooltips BEFORE install — the args are
    -- otherwise only knowable from the live rvbbit.operators row post-install.
    -- Arg names/types mirror generate_mcp_operators exactly (sorted by key,
    -- same JSON-Schema -> PG type mapping), so the displayed def matches the
    -- function that install actually creates.
    SELECT coalesce(jsonb_agg(jsonb_build_object(
        'name', server_name || '_' ||
            CASE WHEN left(t.name, length(server_name) + 1) = server_name || '_'
                 THEN substr(t.name, length(server_name) + 2) ELSE t.name END,
        'description', coalesce(t.description, ''),
        'arg_names', coalesce(a.names, '[]'::jsonb),
        'arg_types', coalesce(a.types, '[]'::jsonb),
        'return_type', 'text', 'shape', 'scalar'
    ) ORDER BY t.name), '[]'::jsonb)
    INTO v_op_defs
    FROM rvbbit.mcp_tools t
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(p.key ORDER BY p.key) AS names,
               jsonb_agg(CASE p.value->>'type'
                   WHEN 'integer' THEN 'bigint' WHEN 'number' THEN 'double precision'
                   WHEN 'boolean' THEN 'boolean' WHEN 'object' THEN 'jsonb'
                   WHEN 'array' THEN 'jsonb' ELSE 'text' END ORDER BY p.key) AS types
        FROM jsonb_each(CASE WHEN jsonb_typeof(t.input_schema->'properties') = 'object'
                             THEN t.input_schema->'properties' ELSE '{}'::jsonb END) AS p
    ) a ON true
    WHERE t.server = server_name;

    v_manifest := jsonb_build_object('name', server_name, 'kind', 'mcp',
        'description', coalesce(s.description, ''),
        'connection', jsonb_strip_nulls(jsonb_build_object('transport', s.transport,
            'command', s.command, 'args', to_jsonb(s.args), 'env', s.env, 'url', s.url,
            'auth_header_env', s.auth_header_env, 'timeout_ms', s.timeout_ms)),
        'secrets', v_secrets, 'tools', v_tools, 'resources', v_resources,
        'operators', v_op_defs,
        'surface', jsonb_build_object('n_tools', v_n_tools, 'n_resources', v_n_res),
        'scanned_at', to_jsonb(clock_timestamp()));
    v_entry := jsonb_build_object('id', 'mcp/' || server_name, 'kind', 'mcp',
        'name', server_name, 'title', server_name, 'description', coalesce(s.description, ''),
        'tags', jsonb_build_array('mcp'), 'operators', to_jsonb(v_operators),
        'manifest_path', 'mcp/' || server_name);
    RETURN jsonb_build_object('catalog_entry', v_entry, 'manifest', v_manifest);
END $emm$;

-- Scan + publish in one call (export_mcp_manifest -> upsert_capability_catalog_entry).
CREATE OR REPLACE FUNCTION rvbbit.publish_mcp_capability(
    server_name text, p_title text DEFAULT NULL, p_tags text[] DEFAULT NULL, p_active boolean DEFAULT true)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $pmc$
DECLARE
    pkg jsonb := rvbbit.export_mcp_manifest(server_name);
    v_entry jsonb := pkg->'catalog_entry'; v_manifest jsonb := pkg->'manifest';
BEGIN
    IF p_title IS NOT NULL THEN v_entry := jsonb_set(v_entry, '{title}', to_jsonb(p_title)); END IF;
    IF p_tags IS NOT NULL THEN v_entry := jsonb_set(v_entry, '{tags}', to_jsonb(p_tags)); END IF;
    RETURN rvbbit.upsert_capability_catalog_entry(v_entry, v_manifest, 'mcp-scan', p_active);
END $pmc$;

-- ---------------------------------------------------------------------------
-- Semantic capability search (Tier A): a def-doc view + a ranking function.
-- No stored vector and no trigger — rvbbit.knn_text embeds the query, batch-
-- embeds the def docs (cached by text_hash, so unchanged defs are free), and
-- ranks. A def only changes via upsert_capability_catalog_entry, which changes
-- its doc text -> new hash -> re-embedded on the next search. Promote to a
-- stored def_vec computed in that upsert (+ Lance index) only if the catalog
-- ever grows into the thousands.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW rvbbit.capability_search_doc AS
SELECT id,
  concat_ws(' ',
    title, name, coalesce(description, ''),
    array_to_string(tags, ' '),
    array_to_string(operators, ' ')
  ) AS doc
FROM rvbbit.capability_catalog
WHERE active;

CREATE OR REPLACE FUNCTION rvbbit.search_capabilities(p_query text, p_k int DEFAULT 24)
RETURNS TABLE(id text, score double precision)
LANGUAGE sql VOLATILE AS $scap$
  SELECT c.id, kt.score
  FROM rvbbit.knn_text('rvbbit.capability_search_doc'::regclass, 'doc', p_query, p_k) kt
  JOIN rvbbit.capability_search_doc c ON c.doc = kt.value
  ORDER BY kt.score DESC;
$scap$;

-- ---------------------------------------------------------------------------
-- Generate one scalar operator per tool of an MCP server: a single `mcp` step
-- routing to the gateway. Typed args come from the tool's input_schema; the
-- return is text (the tool's result body — JSON for structured tools, castable
-- with ::jsonb). Drop-first so a re-scan with changed signatures is idempotent.
-- They land in rvbbit.operators -> appear in the Operators window / Scry / SQL
-- exactly like specialist or semantic operators.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rvbbit.generate_mcp_operators(server_name text)
RETURNS int LANGUAGE plpgsql AS $gmo$
DECLARE
    t record; prop record; r record;
    v_op text; v_args text[]; v_types text[]; v_inputs jsonb; v_steps jsonb; v_n int := 0;
BEGIN
    FOR t IN SELECT name, description, input_schema FROM rvbbit.mcp_tools WHERE server = server_name LOOP
        -- Tools that already self-namespace (e.g. firecrawl's `firecrawl_scrape`)
        -- must not get a doubled `firecrawl_firecrawl_` prefix.
        v_op := server_name || '_' ||
            CASE WHEN left(t.name, length(server_name) + 1) = server_name || '_'
                 THEN substr(t.name, length(server_name) + 2) ELSE t.name END;
        -- Drop prior wrapper(s) — return type / signature may change between scans.
        FOR r IN SELECT oid::regprocedure AS sig FROM pg_proc
                 WHERE proname IN (v_op, '_op_' || v_op) AND pronamespace = 'rvbbit'::regnamespace LOOP
            EXECUTE 'DROP FUNCTION IF EXISTS ' || r.sig::text || ' CASCADE';
        END LOOP;
        DELETE FROM rvbbit.operators WHERE name = v_op;
        v_args := ARRAY[]::text[]; v_types := ARRAY[]::text[]; v_inputs := '{}'::jsonb;
        IF jsonb_typeof(t.input_schema->'properties') = 'object' THEN
            FOR prop IN SELECT key, value FROM jsonb_each(t.input_schema->'properties') ORDER BY key LOOP
                v_args := v_args || prop.key;
                v_types := v_types || CASE prop.value->>'type'
                    WHEN 'integer' THEN 'bigint' WHEN 'number' THEN 'double precision'
                    WHEN 'boolean' THEN 'boolean' WHEN 'object' THEN 'jsonb'
                    WHEN 'array' THEN 'jsonb' ELSE 'text' END;
                v_inputs := v_inputs || jsonb_build_object(prop.key, '{{ inputs.' || prop.key || ' }}');
            END LOOP;
        END IF;
        v_steps := jsonb_build_array(jsonb_build_object(
            'name','call','kind','mcp','server',server_name,'tool',t.name,'inputs',v_inputs));
        PERFORM rvbbit.create_operator(
            op_name => v_op, op_arg_names => v_args, op_return_type => 'text',
            op_shape => 'scalar', op_arg_types => v_types,
            op_description => coalesce(t.description,'') || ' [MCP ' || server_name || '.' || t.name || ']',
            op_steps => v_steps);
        v_n := v_n + 1;
    END LOOP;
    RETURN v_n;
END $gmo$;

-- Install step 1 (lens-orchestrated): register an mcp_servers row from a
-- published mcp capability's connection spec. Returns the server name. Runs as
-- its own statement so the row commits before the gateway (a separate
-- connection) is asked to refresh; the lens pushes secrets to the gateway in
-- between (they never transit Postgres).
CREATE OR REPLACE FUNCTION rvbbit.install_mcp_register(catalog_id text, p_server_name text DEFAULT NULL)
RETURNS text LANGUAGE plpgsql AS $imr$
DECLARE c rvbbit.capability_catalog%ROWTYPE; m jsonb; conn jsonb; v_server text;
BEGIN
    SELECT * INTO c FROM rvbbit.capability_catalog WHERE id = catalog_id AND kind = 'mcp';
    IF NOT FOUND THEN RAISE EXCEPTION 'mcp capability % not found', catalog_id; END IF;
    m := c.manifest; conn := coalesce(m->'connection', '{}'::jsonb);
    v_server := coalesce(nullif(btrim(p_server_name), ''), m->>'name');
    PERFORM rvbbit.register_mcp_server(
        server_name => v_server, server_transport => coalesce(conn->>'transport','stdio'),
        server_command => conn->>'command',
        server_args => CASE WHEN jsonb_typeof(conn->'args')='array' THEN ARRAY(SELECT jsonb_array_elements_text(conn->'args')) ELSE NULL END,
        server_env => conn->'env', server_url => conn->>'url', server_auth_env => conn->>'auth_header_env',
        server_timeout_ms => coalesce((conn->>'timeout_ms')::int, 30000), server_description => m->>'description');
    RETURN v_server;
END $imr$;

-- Install step 2: after secrets are set on the gateway, re-introspect the live
-- server, reconcile its surface against the published manifest (drift), and
-- generate operators. Returns { server, operators_created, tools, drift }.
CREATE OR REPLACE FUNCTION rvbbit.install_mcp_finalize(catalog_id text, p_server_name text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql AS $imf$
DECLARE c rvbbit.capability_catalog%ROWTYPE; m jsonb; v_server text;
    v_baked text[]; v_live text[]; v_added text[]; v_removed text[]; v_n int;
BEGIN
    SELECT * INTO c FROM rvbbit.capability_catalog WHERE id = catalog_id AND kind = 'mcp';
    IF NOT FOUND THEN RAISE EXCEPTION 'mcp capability % not found', catalog_id; END IF;
    m := c.manifest; v_server := coalesce(nullif(btrim(p_server_name), ''), m->>'name');
    PERFORM rvbbit.refresh_mcp_server(v_server);
    SELECT coalesce(array_agg(t->>'name'), '{}') INTO v_baked FROM jsonb_array_elements(coalesce(m->'tools','[]'::jsonb)) t;
    SELECT coalesce(array_agg(name), '{}') INTO v_live FROM rvbbit.mcp_tools WHERE server = v_server;
    SELECT coalesce(array_agg(x), '{}') INTO v_added FROM unnest(v_live) x WHERE x <> ALL(v_baked);
    SELECT coalesce(array_agg(x), '{}') INTO v_removed FROM unnest(v_baked) x WHERE x <> ALL(v_live);
    v_n := rvbbit.generate_mcp_operators(v_server);
    RETURN jsonb_build_object('server', v_server, 'operators_created', v_n, 'tools', to_jsonb(v_live),
        'drift', jsonb_build_object('added', to_jsonb(v_added), 'removed', to_jsonb(v_removed),
                 'changed', (cardinality(v_added) > 0 OR cardinality(v_removed) > 0)));
END $imf$;

-- Phase 4 — opt a tool into result caching. `t` (ttl_seconds) NULL =
-- cache forever. Re-running with a different `t` updates the policy.
CREATE OR REPLACE FUNCTION rvbbit.set_mcp_tool_caching(
    server_name text,
    tool_name   text,
    ttl_seconds int DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $sc$
BEGIN
    UPDATE rvbbit.mcp_tools
    SET cacheable = true, ttl_seconds = set_mcp_tool_caching.ttl_seconds
    WHERE server = server_name AND name = tool_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_mcp_tool_caching: tool %.% not in rvbbit.mcp_tools (refresh first?)',
            server_name, tool_name;
    END IF;
END
$sc$;

-- Phase 4 — drop cached results. NULL tool means all tools for that
-- server. Returns the number of rows removed.
CREATE OR REPLACE FUNCTION rvbbit.purge_mcp_cache(
    server_name text,
    tool_name   text DEFAULT NULL
) RETURNS int
LANGUAGE plpgsql
AS $pc$
DECLARE n int;
BEGIN
    IF tool_name IS NULL THEN
        DELETE FROM rvbbit.mcp_cache WHERE server = server_name;
    ELSE
        DELETE FROM rvbbit.mcp_cache WHERE server = server_name AND tool = tool_name;
    END IF;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$pc$;

CREATE TABLE rvbbit.shreds (
    table_oid     oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    column_name   text NOT NULL,            -- the parquet column name (e.g. x_response_stop_reason)
    source_expr   text NOT NULL,            -- the SQL expression it materializes (for humans/audit)
    src_column    text NOT NULL,            -- base column name on the heap (e.g. 'response')
    path          text[] NOT NULL,          -- json keys from src to leaf ({'stop_reason'} or
                                            --   {'usage','input_tokens'})
    data_type     text NOT NULL,            -- 'text', 'int4', 'int8', 'jsonb', ...
    notes         text,
    PRIMARY KEY (table_oid, column_name)
);

-- Access method registration --------------------------------------------------
-- Phase 1a: alias of heap. Phase 1b will replace this with our own handler.

CREATE ACCESS METHOD rvbbit
    TYPE TABLE
    HANDLER pg_catalog.heap_tableam_handler;

COMMENT ON ACCESS METHOD rvbbit IS
    'Rvbbit columnar (Phase 1a: heap alias; storage layer not yet differentiated)';

-- DDL bookkeeping -------------------------------------------------------------
-- Auto-register newly created rvbbit tables into rvbbit.tables, and tear them
-- down on drop. We hang off DDL event triggers so the catalog stays in sync
-- without users having to call helper functions.

CREATE OR REPLACE FUNCTION rvbbit.on_create_table()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    obj record;
    rvbbit_am_oid oid;
BEGIN
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
            INSERT INTO rvbbit.tables (table_oid)
            VALUES (obj.objid)
            ON CONFLICT (table_oid) DO NOTHING;
            RAISE DEBUG 'rvbbit: registered table % (oid=%)',
                obj.object_identity, obj.objid;
        END IF;
    END LOOP;
END;
$$;

CREATE EVENT TRIGGER rvbbit_on_create_table
    ON ddl_command_end
    WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
    EXECUTE FUNCTION rvbbit.on_create_table();

CREATE OR REPLACE FUNCTION rvbbit.on_drop_table()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT * FROM pg_event_trigger_dropped_objects()
        WHERE object_type = 'table'
    LOOP
        DELETE FROM rvbbit.tables WHERE table_oid = obj.objid;
        -- ON DELETE CASCADE on row_groups handles those; delete_log is keyed
        -- by table_oid so wipe it explicitly.
        DELETE FROM rvbbit.delete_log WHERE table_oid = obj.objid;
    END LOOP;
END;
$$;

CREATE EVENT TRIGGER rvbbit_on_drop_table
    ON sql_drop
    EXECUTE FUNCTION rvbbit.on_drop_table();

CREATE OR REPLACE FUNCTION rvbbit.mark_shadow_heap_dirty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE rvbbit.tables
    SET shadow_heap_dirty = true,
        -- clock_timestamp() (real wall-clock per statement), not now()
        -- (transaction start), so last_write_at reflects the actual write
        -- time and a long writer txn doesn't backdate it.
        last_write_at = clock_timestamp(),
        -- Stamp the onset only on the clean->dirty edge; keep it stable
        -- across subsequent writes so seconds_dirty measures the whole
        -- stale window, not just the last statement.
        dirty_since = CASE WHEN shadow_heap_dirty THEN dirty_since ELSE clock_timestamp() END
    WHERE table_oid = TG_RELID
      AND shadow_heap_retained;
    RETURN NULL;
END;
$$;

-- User-facing helpers ---------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.is_rvbbit_table(rel regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_am a ON c.relam = a.oid
        WHERE c.oid = rel AND a.amname = 'rvbbit'
    );
$$;

CREATE OR REPLACE FUNCTION rvbbit.list_tables()
RETURNS TABLE (table_oid oid, table_name text, n_row_groups bigint, n_deletes bigint)
LANGUAGE sql
STABLE
AS $$
    SELECT
        t.table_oid,
        c.oid::regclass::text,
        (SELECT count(*) FROM rvbbit.row_groups rg WHERE rg.table_oid = t.table_oid),
        (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = t.table_oid)
    FROM rvbbit.tables t
    JOIN pg_class c ON c.oid = t.table_oid;
$$;

CREATE OR REPLACE FUNCTION rvbbit.refresh_acceleration(
    reloid regclass,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb LANGUAGE plpgsql AS $$
<<accel_refresh>>
DECLARE
    op_id bigint;
    table_name_text text := reloid::text;
    last_xid numeric;
    safe_upper_xid numeric;
    rows_written bigint := 0;
    row_groups_written bigint := 0;
    variants_rows bigint;
    max_rg_id_pre bigint;
    existing_rgs bigint;
    generation_after bigint := 0;
    shadow_retained boolean := false;
    shadow_dirty boolean := false;
    heap_bytes bigint := 0;
    phase_id bigint;
    phase_bytes_before bigint := 0;
    phase_bytes_after bigint := 0;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    -- The refresh watermark is a correctness boundary. Block writers while
    -- we snapshot/export the safe heap range, then install the dirty trigger
    -- before releasing the lock at transaction end.
    EXECUTE format('LOCK TABLE %s IN SHARE MODE', reloid);

    INSERT INTO rvbbit.acceleration_state (table_oid)
    VALUES (reloid)
    ON CONFLICT (table_oid) DO NOTHING;

    SELECT s.last_refresh_xid
      INTO last_xid
      FROM rvbbit.acceleration_state s
     WHERE s.table_oid = reloid
     FOR UPDATE;

    -- pg_snapshot_xmin is the oldest still-active xid in this snapshot.
    -- XIDs below it are complete, so rows in that range are safe to mark
    -- accelerated without skipping concurrent transactions that commit later.
    safe_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmin(pg_current_snapshot())::text)::numeric - 1
    );

    SELECT count(*)::bigint, coalesce(max(rg_id), -1)::bigint,
           coalesce(max(generation), 0)::bigint
      INTO existing_rgs, max_rg_id_pre, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT coalesce(t.shadow_heap_retained, false),
           coalesce(t.shadow_heap_dirty, false)
      INTO shadow_retained, shadow_dirty
      FROM rvbbit.tables t
     WHERE t.table_oid = reloid;

    heap_bytes := pg_relation_size(reloid);

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status,
        watermark_before, watermark_after, settings
    ) VALUES (
        reloid, table_name_text, 'refresh_acceleration', 'running',
        last_xid, safe_upper_xid,
        jsonb_build_object(
            'refresh_variants', refresh_variants,
            'watermark', 'heap xmin <= pg_snapshot_xmin(pg_current_snapshot()) - 1',
            'heap_guard', 'LOCK TABLE IN SHARE MODE'
        )
    )
    RETURNING id INTO op_id;

    IF last_xid = 0 AND existing_rgs > 0 AND heap_bytes > 0 THEN
        IF shadow_retained AND NOT shadow_dirty THEN
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true,
                   shadow_heap_dirty = false
             WHERE table_oid = reloid;
            EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
            EXECUTE format(
                'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
                reloid
            );
            UPDATE rvbbit.acceleration_state
               SET last_refresh_xid = safe_upper_xid,
                   last_refresh_generation = generation_after,
                   last_refresh_at = clock_timestamp(),
                   updated_at = clock_timestamp()
             WHERE table_oid = reloid;
            UPDATE rvbbit.acceleration_operations
               SET status = 'noop',
                   finished_at = clock_timestamp(),
                   rows_written = 0,
                   row_groups_written = 0,
                   generation_after = accel_refresh.generation_after,
                   settings = settings || jsonb_build_object('bootstrap', 'clean shadow heap already covered by existing row groups')
             WHERE id = op_id;
            RETURN jsonb_build_object(
                'status', 'noop',
                'operation_id', op_id,
                'table', table_name_text,
                'watermark_before', last_xid,
                'watermark_after', safe_upper_xid,
                'rows_written', 0,
                'row_groups_written', 0,
                'bootstrap', true
            );
        ELSIF shadow_dirty THEN
            RAISE EXCEPTION
                'rvbbit.refresh_acceleration: % has existing row groups and a dirty retained heap; run rvbbit.rebuild_acceleration(%) before incremental refresh',
                reloid, quote_literal(reloid::text);
        END IF;
    END IF;

    IF safe_upper_xid <= last_xid THEN
        IF existing_rgs > 0 AND NOT shadow_dirty THEN
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true,
                   shadow_heap_dirty = false
             WHERE table_oid = reloid;
            EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
            EXECUTE format(
                'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
                reloid
            );
        END IF;
        UPDATE rvbbit.acceleration_operations
           SET status = 'noop',
               finished_at = clock_timestamp(),
               rows_written = 0,
               row_groups_written = 0,
               generation_after = accel_refresh.generation_after
         WHERE id = op_id;
        RETURN jsonb_build_object(
            'status', 'noop',
            'operation_id', op_id,
            'table', table_name_text,
            'watermark_before', last_xid,
            'watermark_after', safe_upper_xid,
            'rows_written', 0,
            'row_groups_written', 0
        );
    END IF;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_before
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_delta_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'watermark_delta',
            'watermark_before', last_xid,
            'watermark_after', safe_upper_xid
        )
    )
    RETURNING id INTO phase_id;

    SELECT rvbbit.export_to_parquet_xid_range(
        reloid::oid,
        last_xid::text,
        safe_upper_xid::text
    ) INTO rows_written;

    SELECT count(*)::bigint, coalesce(max(generation), generation_after)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id > max_rg_id_pre;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    UPDATE rvbbit.acceleration_operation_phases
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = accel_refresh.rows_written,
           row_groups_written = accel_refresh.row_groups_written,
           files_written = accel_refresh.row_groups_written::integer,
           bytes_written = greatest(0, phase_bytes_after - phase_bytes_before),
           expected_rows = accel_refresh.rows_written,
           actual_rows = accel_refresh.rows_written
     WHERE id = phase_id;

    IF refresh_variants AND rows_written > 0 THEN
        PERFORM set_config('rvbbit.acceleration_operation_id', op_id::text, true);
        SELECT rvbbit.refresh_layout_variants_xid_range(
            reloid::oid,
            last_xid::text,
            safe_upper_xid::text
        ) INTO variants_rows;
        PERFORM set_config('rvbbit.acceleration_operation_id', '', true);
    END IF;

    IF existing_rgs > 0 OR row_groups_written > 0 THEN
        UPDATE rvbbit.tables
           SET shadow_heap_retained = true,
               shadow_heap_dirty = false
         WHERE table_oid = reloid;
        EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
    END IF;

    UPDATE rvbbit.acceleration_state
       SET last_refresh_xid = safe_upper_xid,
           last_refresh_generation = generation_after,
           last_refresh_rows = coalesce(last_refresh_rows, 0) + coalesce(rows_written, 0),
           last_refresh_row_groups = coalesce(last_refresh_row_groups, 0) + coalesce(row_groups_written, 0),
           last_refresh_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE table_oid = reloid;

    UPDATE rvbbit.acceleration_operations
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = accel_refresh.rows_written,
           row_groups_written = accel_refresh.row_groups_written,
           variants_rows = accel_refresh.variants_rows,
           generation_after = accel_refresh.generation_after
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', 'ok',
        'operation_id', op_id,
        'table', table_name_text,
        'watermark_before', last_xid,
        'watermark_after', safe_upper_xid,
        'rows_written', rows_written,
        'row_groups_written', row_groups_written,
        'variants_rows', variants_rows,
        'generation_after', generation_after
    );
EXCEPTION WHEN OTHERS THEN
    IF op_id IS NOT NULL THEN
        UPDATE rvbbit.acceleration_operation_phases
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE operation_id = op_id
           AND status = 'running';
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE id = op_id;
    END IF;
    RAISE;
END;
$$;

CREATE OR REPLACE VIEW rvbbit.acceleration_status AS
SELECT
    t.table_oid,
    c.oid::regclass::text AS table_name,
    coalesce(s.last_refresh_xid, 0) AS last_refresh_xid,
    s.last_refresh_at,
    coalesce(s.last_refresh_generation, 0) AS last_refresh_generation,
    coalesce(s.last_refresh_rows, 0) AS last_refresh_rows,
    coalesce(s.last_refresh_row_groups, 0) AS last_refresh_row_groups,
    coalesce((SELECT sum(rg.n_rows)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = t.table_oid), 0) AS parquet_rows,
    coalesce((SELECT count(*)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = t.table_oid), 0) AS row_groups,
    pg_relation_size(t.table_oid)::bigint AS heap_bytes,
    coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
    coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
    (
        pg_relation_size(t.table_oid) = 0
        OR coalesce(t.shadow_heap_retained AND NOT t.shadow_heap_dirty, false)
    ) AS parquet_authoritative,
    (SELECT max(o.started_at) FROM rvbbit.acceleration_operations o WHERE o.table_oid = t.table_oid) AS last_operation_at
FROM rvbbit.tables t
JOIN pg_class c ON c.oid = t.table_oid
LEFT JOIN rvbbit.acceleration_state s ON s.table_oid = t.table_oid;

-- rvbbit.compact(rel, keep_heap) is the legacy physical rebuild primitive:
--   1. Writes the current heap contents of `rel` as a new immutable
--      parquet row group (with JSON shredding for known paths).
--   2. With keep_heap=false, TRUNCATEs the source heap. This legacy mode is
--      still available explicitly, but the zero-argument compact(rel) wrapper
--      now maps to refresh_acceleration so normal callers preserve the heap
--      as the PostgreSQL source of truth.
--   3. Adds PLAIN columns for each registered shred so users can SELECT
--      x_response_stop_reason directly. These are populated from parquet
--      by our custom scan after compaction.

CREATE OR REPLACE FUNCTION rvbbit.compact(rel regclass, keep_heap boolean)
RETURNS TABLE (rg_id bigint, n_rows bigint, n_bytes bigint, heap_freed_bytes bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    written_rows  bigint;
    heap_size_pre bigint;
    max_rg_id_pre bigint;
    shred         record;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(rel) THEN
        RAISE EXCEPTION '% is not an rvbbit table', rel;
    END IF;

    heap_size_pre := pg_total_relation_size(rel);
    SELECT COALESCE(max(rg.rg_id), -1)
    INTO max_rg_id_pre
    FROM rvbbit.row_groups rg
    WHERE rg.table_oid = rel;

    -- Preserve planner statistics for the rows that are about to move into
    -- parquet. The heap is truncated below, so analyzing after compact would
    -- teach PostgreSQL that every predicate is operating on an empty table.
    EXECUTE format('ANALYZE %s', rel);

    -- Write a new parquet row group from the heap contents.
    SELECT rvbbit.export_to_parquet(rel) INTO written_rows;

    IF keep_heap THEN
        UPDATE rvbbit.tables
        SET shadow_heap_retained = true,
            shadow_heap_dirty = false
        WHERE table_oid = rel;
        EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', rel);
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            rel
        );
        RAISE NOTICE 'rvbbit.compact: preserving clean shadow heap for %; parquet remains authoritative until the heap is mutated', rel;
    ELSE
        UPDATE rvbbit.tables
        SET shadow_heap_retained = false,
            shadow_heap_dirty = false
        WHERE table_oid = rel;
        EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', rel);
        -- Drop the heap data. Source is now empty; parquet is authoritative.
        EXECUTE format('TRUNCATE TABLE %s', rel);
    END IF;

    -- Add the shred columns to the heap relation as PLAIN columns
    -- (not VIRTUAL GENERATED — those get expanded inline by the planner
    -- and the scan never sees them). These columns:
    --   - exist in pg_attribute so Vars to them are valid (rewriter R3 needs this)
    --   - are NULL for any heap row (heap is empty post-compact anyway)
    --   - are populated from parquet by our custom scan
    -- A comment + COMMENT ON COLUMN warns users not to write to them
    -- directly; future versions can add a CHECK constraint.
    FOR shred IN
        SELECT s.column_name, s.source_expr, s.data_type
        FROM rvbbit.shreds s
        WHERE s.table_oid = rel
    LOOP
        BEGIN
            EXECUTE format(
                'ALTER TABLE %s ADD COLUMN IF NOT EXISTS %I %s',
                rel, shred.column_name, shred.data_type
            );
        EXCEPTION WHEN duplicate_column THEN
            -- already added by a prior compact; fine
        END;
    END LOOP;

    RETURN QUERY
        SELECT rg.rg_id, rg.n_rows, rg.n_bytes, heap_size_pre - pg_total_relation_size(rel)
        FROM rvbbit.row_groups rg
        WHERE rg.table_oid = rel
          AND rg.rg_id > max_rg_id_pre
        ORDER BY rg.rg_id DESC
        ;
END;
$$;

-- Set the snapshot visibility floor for a table: the "latest" (non-AS-OF)
-- view will show only row groups at generation >= the floor, hiding older
-- retained snapshots. gen => NULL floors to the current newest generation
-- that has row groups (the just-written snapshot); pass an explicit
-- generation for the empty-snapshot case (0 rows => no row groups written, so
-- max() would pick the prior generation). Returns the floor that was set.
--
-- Cache: callers must ensure the per-backend scan cache is invalidated for
-- this table after the floor moves. In the snapshot-load workflow this is
-- automatic because compact() runs first and calls invalidate_scan_metadata();
-- a standalone caller that changes the floor without a preceding compact in
-- the same backend should not expect the change to be visible until the next
-- compact/invalidation.
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

-- Gap-free snapshot load: replace a destination rvbbit table's contents with a
-- fresh full snapshot from `source_query`, recording it as one immutable
-- generation so the latest view is the new snapshot while AS OF still reads the
-- prior ones (append/update/delete history falls out of the generation diff —
-- no row-level merge, no PK, no tombstones). This is the core primitive behind
-- the Postgres->rvbbit table-sync workflow.
--
--   * TRUNCATE clears the heap only (parquet/generations history is untouched).
--   * compact(keep_heap=>true) full-scans the reloaded heap into a new
--     generation and keeps the heap retained+clean (gold-source fallback;
--     parquet stays authoritative).
--   * For an EMPTY snapshot, compact() writes no rvbbit.generations row, so we
--     add a synthetic zero-row one — otherwise AS OF would resolve to the prior
--     (stale) snapshot instead of "empty here".
--   * set_visible_floor pins the latest view to this generation.
--
-- Runs in the caller's (sub)transaction, so wrapping it in one txn (or a
-- per-table SAVEPOINT in the sync executor) makes the whole swap atomic — a
-- concurrent reader never sees the half-loaded heap. `source_query` is
-- operator-authored (sync config), not end-user input.
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

    -- The generation compact just allocated. The generation advisory lock is
    -- xact-scoped (held to txn end), so no concurrent compact can interleave
    -- between the compact above and this read.
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

-- ── FDW source substrate (Postgres -> rvbbit sync read path) ──────────
-- Provision a postgres_fdw connection to a source server. Idempotent. The
-- read path is a plain non-locking SELECT on the source; fetch_size on the
-- server avoids the 100-rows/round-trip default that kills full-table pulls.
CREATE OR REPLACE FUNCTION rvbbit.fdw_setup_server(
    server_name text,
    host text,
    port integer,
    dbname text,
    user_name text,
    password text,
    fetch_size integer DEFAULT 10000
) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS postgres_fdw;
    EXECUTE format(
        'CREATE SERVER IF NOT EXISTS %I FOREIGN DATA WRAPPER postgres_fdw '
        'OPTIONS (host %L, port %L, dbname %L, fetch_size %L)',
        server_name, host, port::text, dbname, fetch_size::text);
    -- reconcile options each run (CREATE IF NOT EXISTS is a no-op once it exists)
    EXECUTE format(
        'ALTER SERVER %I OPTIONS (SET host %L, SET port %L, SET dbname %L, SET fetch_size %L)',
        server_name, host, port::text, dbname, fetch_size::text);
    EXECUTE format(
        'CREATE USER MAPPING IF NOT EXISTS FOR CURRENT_USER SERVER %I '
        'OPTIONS (user %L, password %L)',
        server_name, user_name, password);
    EXECUTE format(
        'ALTER USER MAPPING FOR CURRENT_USER SERVER %I OPTIONS (SET user %L, SET password %L)',
        server_name, user_name, password);
    RETURN format('postgres_fdw server "%s" ready (-> %s@%s:%s/%s)',
        server_name, user_name, host, port::text, dbname);
END;
$$;

-- (Re-)import foreign tables from a remote schema into a local schema. Drops
-- the targeted foreign tables first so a re-import picks up remote DDL
-- (added/changed columns) — how the sync tolerates source schema drift.
-- only_tables => NULL imports the whole schema; pass an array for à-la-carte.
CREATE OR REPLACE FUNCTION rvbbit.fdw_import(
    server_name text,
    remote_schema text,
    local_schema text,
    only_tables text[] DEFAULT NULL
) RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    t text;
    limit_clause text := '';
    n integer;
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', local_schema);
    IF only_tables IS NULL THEN
        FOR t IN
            SELECT c.relname FROM pg_foreign_table ft
            JOIN pg_class c ON c.oid = ft.ftrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = local_schema
        LOOP
            EXECUTE format('DROP FOREIGN TABLE IF EXISTS %I.%I', local_schema, t);
        END LOOP;
    ELSE
        FOREACH t IN ARRAY only_tables LOOP
            EXECUTE format('DROP FOREIGN TABLE IF EXISTS %I.%I', local_schema, t);
        END LOOP;
        limit_clause := format(' LIMIT TO (%s)',
            (SELECT string_agg(quote_ident(x), ', ') FROM unnest(only_tables) x));
    END IF;
    EXECUTE format('IMPORT FOREIGN SCHEMA %I%s FROM SERVER %I INTO %I',
        remote_schema, limit_clause, server_name, local_schema);
    SELECT count(*) INTO n FROM pg_foreign_table ft
        JOIN pg_class c ON c.oid = ft.ftrelid
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        WHERE ns.nspname = local_schema;
    RETURN n;
END;
$$;

-- Cheap remote-schema fingerprint over postgres_fdw — a single stable foreign
-- table per server over the remote information_schema.columns (created once,
-- reused), hashed. Lets run_sync SKIP the expensive DROP + IMPORT FOREIGN SCHEMA
-- (a catalog-cache-invalidation storm that slows the whole DB) when the source
-- shape is unchanged. Returns the fingerprint + remote table count (so the caller
-- can also detect missing local foreign tables). ~20ms vs per-table DDL.
CREATE OR REPLACE FUNCTION rvbbit.fdw_remote_fingerprint(
    server_name text, remote_schema text, only_tables text[] DEFAULT NULL
) RETURNS TABLE(fingerprint text, n_tables int)
LANGUAGE plpgsql AS $fn$
DECLARE
    v_meta text := format('%I.%I', 'rvbbit_meta', 'cols__' || server_name);
BEGIN
    CREATE SCHEMA IF NOT EXISTS rvbbit_meta;
    EXECUTE format($f$
        CREATE FOREIGN TABLE IF NOT EXISTS %s (
            table_schema text, table_name text, column_name text,
            ordinal_position int, data_type text,
            character_maximum_length int, numeric_precision int, numeric_scale int, udt_name text
        ) SERVER %I OPTIONS (schema_name 'information_schema', table_name 'columns')
    $f$, v_meta, server_name);
    RETURN QUERY EXECUTE format($q$
        SELECT md5(coalesce(string_agg(
                   table_name||'|'||ordinal_position||'|'||column_name||'|'||data_type||'|'||
                   coalesce(character_maximum_length::text,'')||'|'||
                   coalesce(numeric_precision::text,'')||'|'||
                   coalesce(numeric_scale::text,'')||'|'||udt_name,
                   ',' ORDER BY table_name, ordinal_position), '')),
               count(DISTINCT table_name)::int
        FROM %s
        WHERE table_schema = %L AND (%L::text[] IS NULL OR table_name = ANY(%L::text[]))
    $q$, v_meta, remote_schema, only_tables, only_tables);
END $fn$;

-- Escape hatch: clear stored fingerprints so the next run re-imports (NULL job =
-- all). Use after a manual fdw change, or if you suspect drift slipped past the
-- fingerprint. Returns the number of jobs reset.
CREATE OR REPLACE FUNCTION rvbbit.reset_sync_fingerprint(p_job_name text DEFAULT NULL)
RETURNS integer LANGUAGE sql AS $$
    WITH upd AS (
        UPDATE rvbbit.sync_jobs SET fdw_fingerprint = NULL
        WHERE p_job_name IS NULL OR job_name = p_job_name
        RETURNING 1
    )
    SELECT count(*)::int FROM upd;
$$;

-- ── Retention reaper ──────────────────────────────────────────────────
-- Bound disk + AS OF history for SNAPSHOT tables (min_visible_generation > 0).
-- Reaps generations strictly BELOW the live snapshot (never the current one)
-- whose committed_at is older than keep_days: deletes the catalog rows
-- (row_groups + generations) first, then unlinks the parquet files (orphan-safe
-- ordering). reloid => NULL reaps every snapshot table (cron-friendly).
--
-- Append tables (floor = 0) are SKIPPED: their generations are cumulative, so
-- reaping an old one would drop live rows. Only snapshot tables have
-- self-contained generations that are safe to age out.
CREATE OR REPLACE FUNCTION rvbbit.reap_generations(
    reloid regclass DEFAULT NULL,
    keep_days integer DEFAULT 30
) RETURNS TABLE (relname text, generations_reaped bigint, row_groups_reaped bigint, files_unlinked integer)
LANGUAGE plpgsql AS $$
DECLARE
    rec    record;
    cutoff timestamptz := now() - make_interval(days => greatest(keep_days, 0));
    paths  text[];
    gens   bigint;
    rgs    bigint;
    nfiles integer;
BEGIN
    FOR rec IN
        SELECT t.table_oid, t.min_visible_generation AS floor
        FROM rvbbit.tables t
        WHERE t.min_visible_generation > 0
          AND (reloid IS NULL OR t.table_oid = reloid)
    LOOP
        -- local file paths for the generations we're about to reap
        SELECT array_agg(rg.path)
        INTO paths
        FROM rvbbit.row_groups rg
        JOIN rvbbit.generations g
          ON g.table_oid = rg.table_oid AND g.generation = rg.generation
        WHERE rg.table_oid = rec.table_oid
          AND rg.generation < rec.floor
          AND g.committed_at < cutoff
          AND rg.cold_url IS NULL;

        WITH reap_gens AS (
            SELECT g.generation
            FROM rvbbit.generations g
            WHERE g.table_oid = rec.table_oid
              AND g.generation < rec.floor
              AND g.committed_at < cutoff
        ),
        del_rg AS (
            DELETE FROM rvbbit.row_groups rg
            WHERE rg.table_oid = rec.table_oid
              AND rg.generation IN (SELECT generation FROM reap_gens)
            RETURNING 1
        ),
        del_gen AS (
            DELETE FROM rvbbit.generations g
            WHERE g.table_oid = rec.table_oid
              AND g.generation IN (SELECT generation FROM reap_gens)
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM del_gen), (SELECT count(*) FROM del_rg)
        INTO gens, rgs;

        -- catalog rows are gone; unlinking the files now is orphan-safe
        nfiles := coalesce(rvbbit.reap_unlink_files(paths), 0);

        IF coalesce(gens, 0) > 0 OR coalesce(rgs, 0) > 0 THEN
            relname := rec.table_oid::regclass::text;
            generations_reaped := coalesce(gens, 0);
            row_groups_reaped := coalesce(rgs, 0);
            files_unlinked := nfiles;
            RETURN NEXT;
        END IF;
    END LOOP;
END;
$$;

-- ── Sync config + executor ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rvbbit.sync_jobs (
    job_name    text PRIMARY KEY,
    enabled     boolean NOT NULL DEFAULT true,
    spec        jsonb NOT NULL,
    last_run_at timestamptz,
    fdw_fingerprint text,           -- last-seen remote schema fingerprint (skip re-import when unchanged)
    fdw_imported_at timestamptz,    -- when the foreign tables were last (re-)imported
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rvbbit.sync_runs (
    run_id       uuid NOT NULL,
    job_name     text NOT NULL,
    source_table text,
    dest_table   text,
    action       text,
    generation   bigint,
    rows_loaded  bigint,
    elapsed_ms   integer,
    error        text,
    started_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS sync_runs_job_started_idx ON rvbbit.sync_runs (job_name, started_at DESC);

-- Self-healing singleton lock for run_sync (a crashed run's lock auto-expires).
CREATE TABLE IF NOT EXISTS rvbbit.sync_lock (
    id          integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    acquired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    pid         integer NOT NULL
);

-- Map a source column type to an rvbbit-storable dest type. rvbbit's columnar
-- engine supports a fixed set; the sync coerces numeric -> double precision and
-- everything else unsupported -> text (a mirror, not the system of record).
CREATE OR REPLACE FUNCTION rvbbit.rvbbit_storable_type(typ oid, typmod integer DEFAULT -1)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN typ IN (16,21,23,20,700,701,25,1043,1042,19,1114,1184,1082,3802,17,1021)
         THEN format_type(typ, typmod)
    WHEN typ = 1700 THEN 'double precision'
    ELSE 'text'
  END
$$;

-- Sync ONE foreign table into an rvbbit dest (DDL-adapt + type coercion).
CREATE OR REPLACE FUNCTION rvbbit.sync_table(fdw_table regclass, dest_schema text, dest_name text)
RETURNS TABLE (generation bigint, rows_loaded bigint, action text)
LANGUAGE plpgsql AS $$
DECLARE
    v_dest_qual text := format('%I.%I', dest_schema, dest_name);
    v_dest_oid  oid;
    v_col_ddl   text;
    v_stmt      text;
    v_select    text;
BEGIN
    v_dest_oid := to_regclass(v_dest_qual);
    IF v_dest_oid IS NULL THEN
        EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', dest_schema);
        SELECT string_agg(format('%I %s', a.attname, rvbbit.rvbbit_storable_type(a.atttypid, a.atttypmod)), ', ' ORDER BY a.attnum)
        INTO v_col_ddl
        FROM pg_attribute a
        WHERE a.attrelid = fdw_table AND a.attnum > 0 AND NOT a.attisdropped;
        IF v_col_ddl IS NULL THEN
            RAISE EXCEPTION 'sync_table: source % has no storable columns', fdw_table;
        END IF;
        EXECUTE format('CREATE TABLE %s (%s) USING rvbbit', v_dest_qual, v_col_ddl);
        v_dest_oid := to_regclass(v_dest_qual);
    ELSE
        FOR v_stmt IN
            SELECT format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS %I %s',
                          v_dest_qual, a.attname, rvbbit.rvbbit_storable_type(a.atttypid, a.atttypmod))
            FROM pg_attribute a
            WHERE a.attrelid = fdw_table AND a.attnum > 0 AND NOT a.attisdropped
              AND NOT EXISTS (
                  SELECT 1 FROM pg_attribute d
                  WHERE d.attrelid = v_dest_oid AND d.attname = a.attname
                    AND d.attnum > 0 AND NOT d.attisdropped)
        LOOP
            EXECUTE v_stmt;
        END LOOP;
    END IF;

    SELECT string_agg(
        CASE WHEN EXISTS (
                SELECT 1 FROM pg_attribute f
                WHERE f.attrelid = fdw_table AND f.attname = d.attname
                  AND f.attnum > 0 AND NOT f.attisdropped)
             THEN format('%I::%s', d.attname, format_type(d.atttypid, d.atttypmod))
             ELSE format('NULL::%s AS %I', format_type(d.atttypid, d.atttypmod), d.attname)
        END, ', ' ORDER BY d.attnum)
    INTO v_select
    FROM pg_attribute d
    WHERE d.attrelid = v_dest_oid AND d.attnum > 0 AND NOT d.attisdropped;

    RETURN QUERY
        SELECT * FROM rvbbit.snapshot_load(
            v_dest_oid::regclass,
            format('SELECT %s FROM %s', v_select, fdw_table::text));
END;
$$;

-- Run sync jobs. Procedure so it can COMMIT per table (durable progress +
-- sync_runs visible mid-run). Singleton via a session advisory lock. Must be
-- called outside an explicit transaction.
CREATE OR REPLACE PROCEDURE rvbbit.run_sync(p_job_name text DEFAULT NULL, dry_run boolean DEFAULT false)
LANGUAGE plpgsql AS $$
DECLARE
    v_rid        uuid := gen_random_uuid();
    v_lock_pid   integer;
    v_jobs       text[];
    v_jn         text;
    v_spec       jsonb;
    v_srv        jsonb;
    v_remote     text;
    v_fdw_schema text;
    v_dest_schema text;
    v_spec_tbls  text[];
    v_tbls       text[];
    v_missing    text;
    v_tbl        text;
    v_fdw_tbl    regclass;
    v_t0         timestamptz;
    v_gen        bigint;
    v_rows       bigint;
    v_action     text;
    v_job_ok     boolean;
    -- fingerprint gate (skip DROP+IMPORT FOREIGN SCHEMA when the remote is unchanged)
    v_fp         text;
    v_prev_fp    text;
    v_remote_n   int;
    v_ft_present int;
    v_force      boolean;
BEGIN
    -- Self-healing singleton lock. Steal it when the holder is provably gone —
    -- its backend pid is no longer active, or the lock predates this server's
    -- start (a restart killed the holder mid-sync, e.g. a deploy) — or, as a last
    -- resort, when it hasn't heartbeated in > 1h. Without the first two checks a
    -- crashed/killed run wedged every subsequent run for a full hour.
    INSERT INTO rvbbit.sync_lock (id, acquired_at, pid)
    VALUES (1, clock_timestamp(), pg_backend_pid())
    ON CONFLICT (id) DO UPDATE
        SET acquired_at = clock_timestamp(), pid = pg_backend_pid()
        WHERE rvbbit.sync_lock.acquired_at < clock_timestamp() - interval '1 hour'
           OR rvbbit.sync_lock.acquired_at <= pg_postmaster_start_time()
           OR NOT EXISTS (SELECT 1 FROM pg_stat_activity a
                          WHERE a.pid = rvbbit.sync_lock.pid)
    RETURNING pid INTO v_lock_pid;
    IF v_lock_pid IS DISTINCT FROM pg_backend_pid() THEN
        RAISE NOTICE 'rvbbit.run_sync is already running; skipping';
        RETURN;
    END IF;
    COMMIT;

    SELECT array_agg(job_name ORDER BY job_name) INTO v_jobs
    FROM rvbbit.sync_jobs
    WHERE enabled AND (p_job_name IS NULL OR job_name = p_job_name);

    FOREACH v_jn IN ARRAY coalesce(v_jobs, ARRAY[]::text[]) LOOP
        SELECT spec INTO v_spec FROM rvbbit.sync_jobs WHERE job_name = v_jn;
        v_srv := v_spec->'server';
        v_remote := coalesce(v_spec->>'remote_schema', 'public');
        v_fdw_schema := coalesce(v_spec->>'fdw_schema', 'rvbbit_fdw');
        v_dest_schema := coalesce(v_spec->>'dest_schema', 'public');

        IF v_spec ? 'tables' AND jsonb_typeof(v_spec->'tables') = 'array' THEN
            SELECT array_agg(t) INTO v_spec_tbls FROM jsonb_array_elements_text(v_spec->'tables') AS t;
        ELSE
            v_spec_tbls := NULL;
        END IF;
        IF v_spec_tbls IS NOT NULL AND cardinality(v_spec_tbls) = 0 THEN
            v_spec_tbls := NULL;  -- empty array => whole schema
        END IF;

        v_job_ok := true;
        BEGIN
            PERFORM rvbbit.fdw_setup_server(
                v_srv->>'name', v_srv->>'host', (v_srv->>'port')::int, v_srv->>'dbname',
                v_srv->>'user', v_srv->>'password', coalesce((v_srv->>'fetch_size')::int, 10000));

            -- Skip the expensive DROP + IMPORT FOREIGN SCHEMA (a catalog-invalidation
            -- storm that slows every query DB-wide) when the remote shape is unchanged
            -- AND the foreign tables are all present. Re-import only on drift / first
            -- run / missing FTs / explicit force. ~20ms fingerprint vs per-table DDL.
            v_fp := NULL; v_remote_n := NULL;
            BEGIN
                SELECT fingerprint, n_tables INTO v_fp, v_remote_n
                FROM rvbbit.fdw_remote_fingerprint(v_srv->>'name', v_remote, v_spec_tbls);
            EXCEPTION WHEN OTHERS THEN
                v_fp := NULL; v_remote_n := NULL;  -- can't fingerprint => import (surfaces the real error)
            END;
            SELECT fdw_fingerprint INTO v_prev_fp FROM rvbbit.sync_jobs WHERE job_name = v_jn;
            SELECT count(*) INTO v_ft_present
            FROM pg_foreign_table ft
            JOIN pg_class c ON c.oid = ft.ftrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = v_fdw_schema
              AND (v_spec_tbls IS NULL OR c.relname = ANY(v_spec_tbls));
            v_force := lower(coalesce(current_setting('rvbbit.sync_force_reimport', true), 'off'))
                       IN ('1','true','on','yes');

            IF v_force
               OR v_fp IS NULL                              -- couldn't fingerprint => be safe
               OR v_prev_fp IS DISTINCT FROM v_fp           -- schema drift (or first run)
               OR v_ft_present IS DISTINCT FROM v_remote_n  -- foreign tables missing/extra
            THEN
                PERFORM rvbbit.fdw_import(v_srv->>'name', v_remote, v_fdw_schema, v_spec_tbls);
                UPDATE rvbbit.sync_jobs
                   SET fdw_fingerprint = v_fp, fdw_imported_at = now()
                 WHERE job_name = v_jn;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            v_job_ok := false;
            INSERT INTO rvbbit.sync_runs(run_id, job_name, action, error, started_at)
            VALUES (v_rid, v_jn, 'error', 'provisioning: ' || left(SQLERRM, 500), clock_timestamp());
            PERFORM pg_notify('rvbbit_sync_error',
                json_build_object('job', v_jn, 'phase', 'provision', 'error', left(SQLERRM, 500))::text);
        END;
        COMMIT;

        IF v_job_ok THEN
            -- foreign tables actually present after import
            SELECT array_agg(c.relname ORDER BY c.relname) INTO v_tbls
            FROM pg_foreign_table ft
            JOIN pg_class c ON c.oid = ft.ftrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = v_fdw_schema
              AND (v_spec_tbls IS NULL OR c.relname = ANY(v_spec_tbls));

            -- spec tables that did NOT import (missing on source / import failed)
            IF v_spec_tbls IS NOT NULL THEN
                FOREACH v_missing IN ARRAY v_spec_tbls LOOP
                    IF v_missing <> ALL (coalesce(v_tbls, ARRAY[]::text[])) THEN
                        INSERT INTO rvbbit.sync_runs(run_id, job_name, source_table, action, error, started_at)
                        VALUES (v_rid, v_jn, v_missing, 'error',
                                'not found in fdw schema after import (missing on source or import failed)',
                                clock_timestamp());
                        PERFORM pg_notify('rvbbit_sync_error',
                            json_build_object('job', v_jn, 'table', v_missing, 'error', 'missing on source')::text);
                    END IF;
                END LOOP;
                COMMIT;
            END IF;

            IF NOT dry_run THEN
                FOREACH v_tbl IN ARRAY coalesce(v_tbls, ARRAY[]::text[]) LOOP
                    v_fdw_tbl := to_regclass(format('%I.%I', v_fdw_schema, v_tbl));
                    v_t0 := clock_timestamp();
                    BEGIN
                        SELECT generation, rows_loaded, action INTO v_gen, v_rows, v_action
                        FROM rvbbit.sync_table(v_fdw_tbl, v_dest_schema, v_tbl);
                        INSERT INTO rvbbit.sync_runs(run_id, job_name, source_table, dest_table, action, generation, rows_loaded, elapsed_ms, started_at)
                        VALUES (v_rid, v_jn, v_tbl, format('%I.%I', v_dest_schema, v_tbl), v_action, v_gen, v_rows,
                                (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int, v_t0);
                    EXCEPTION WHEN OTHERS THEN
                        INSERT INTO rvbbit.sync_runs(run_id, job_name, source_table, dest_table, action, elapsed_ms, error, started_at)
                        VALUES (v_rid, v_jn, v_tbl, format('%I.%I', v_dest_schema, v_tbl), 'error',
                                (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int, left(SQLERRM, 500), v_t0);
                        PERFORM pg_notify('rvbbit_sync_error',
                            json_build_object('job', v_jn, 'table', v_tbl, 'error', left(SQLERRM, 500))::text);
                    END;
                    -- heartbeat the lock so a long sweep isn't seen as crashed
                    UPDATE rvbbit.sync_lock SET acquired_at = clock_timestamp()
                    WHERE id = 1 AND pid = pg_backend_pid();
                    COMMIT;
                END LOOP;
            END IF;
        END IF;

        UPDATE rvbbit.sync_jobs SET last_run_at = now() WHERE job_name = v_jn;
        COMMIT;
    END LOOP;

    DELETE FROM rvbbit.sync_lock WHERE id = 1 AND pid = pg_backend_pid();
    COMMIT;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.compact(rel regclass)
RETURNS TABLE (rg_id bigint, n_rows bigint, n_bytes bigint, heap_freed_bytes bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    max_rg_id_pre bigint;
    _result jsonb;
BEGIN
    SELECT COALESCE(max(rg.rg_id), -1)
    INTO max_rg_id_pre
    FROM rvbbit.row_groups rg
    WHERE rg.table_oid = rel;

    SELECT rvbbit.refresh_acceleration(
        rel,
        lower(coalesce(current_setting('rvbbit.compact_refresh_variants', true), 'off'))
            IN ('1', 'true', 'on', 'yes')
    ) INTO _result;

    RETURN QUERY
        SELECT rg.rg_id, rg.n_rows, rg.n_bytes, 0::bigint
        FROM rvbbit.row_groups rg
        WHERE rg.table_oid = rel
          AND rg.rg_id > max_rg_id_pre
        ORDER BY rg.rg_id DESC;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.shadow_heap_status(rel regclass)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    heap_bytes bigint,
    heap_total_bytes bigint,
    parquet_rows bigint,
    parquet_bytes bigint,
    row_groups bigint,
    delete_rows bigint,
    parquet_authoritative boolean,
    shadow_heap_present boolean,
    shadow_heap_retained boolean,
    shadow_heap_dirty boolean
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        rel::oid,
        rel::text,
        pg_relation_size(rel)::bigint,
        pg_total_relation_size(rel)::bigint,
        coalesce((SELECT sum(rg.n_rows)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = rel), 0),
        coalesce((SELECT sum(rg.n_bytes)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = rel), 0),
        coalesce((SELECT count(*)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = rel), 0),
        coalesce((SELECT count(*)::bigint FROM rvbbit.delete_log dl WHERE dl.table_oid = rel), 0),
        coalesce((SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = rel), 0) = 0
            AND (
                pg_relation_size(rel) = 0
                OR coalesce((SELECT t.shadow_heap_retained AND NOT t.shadow_heap_dirty FROM rvbbit.tables t WHERE t.table_oid = rel), false)
            ),
        pg_relation_size(rel) > 0
            AND coalesce((SELECT t.shadow_heap_retained FROM rvbbit.tables t WHERE t.table_oid = rel), false),
        coalesce((SELECT t.shadow_heap_retained FROM rvbbit.tables t WHERE t.table_oid = rel), false),
        coalesce((SELECT t.shadow_heap_dirty FROM rvbbit.tables t WHERE t.table_oid = rel), false);
$$;

-- List all compacted row groups for a table, including sizes and stats.
CREATE OR REPLACE FUNCTION rvbbit.row_groups_for(rel regclass)
RETURNS TABLE (rg_id bigint, n_rows bigint, n_bytes bigint, path text, created_at timestamptz)
LANGUAGE sql
STABLE
AS $$
    SELECT rg.rg_id, rg.n_rows, rg.n_bytes, rg.path, rg.created_at
    FROM rvbbit.row_groups rg
    WHERE rg.table_oid = rel
    ORDER BY rg.rg_id;
$$;

CREATE OR REPLACE FUNCTION rvbbit.row_group_variants_for(rel regclass)
RETURNS TABLE (layout text, rg_id bigint, n_rows bigint, n_bytes bigint, path text, created_at timestamptz)
LANGUAGE sql
STABLE
AS $$
    SELECT rg.layout, rg.rg_id, rg.n_rows, rg.n_bytes, rg.path, rg.created_at
    FROM rvbbit.row_group_variants rg
    WHERE rg.table_oid = rel
    ORDER BY rg.layout, rg.rg_id;
$$;

CREATE OR REPLACE FUNCTION rvbbit.layout_variant_status_for(rel regclass)
RETURNS TABLE (
    layout text,
    layout_kind text,
    partition_key text,
    status text,
    expected_rows bigint,
    actual_rows bigint,
    file_count integer,
    n_bytes bigint,
    status_message text,
    refreshed_at timestamptz
)
LANGUAGE sql
STABLE
AS $$
    SELECT s.layout,
           CASE
             WHEN s.layout LIKE 'hive:%' THEN 'hive'
             WHEN s.layout LIKE 'cluster:%' THEN 'cluster'
             WHEN s.layout = 'vortex_scan' THEN 'vortex'
             ELSE s.layout
           END,
           CASE
             WHEN s.layout LIKE 'hive:%' THEN substring(s.layout from 6)
             WHEN s.layout LIKE 'cluster:%' THEN substring(s.layout from 9)
             ELSE NULL
           END,
           s.status,
           s.expected_rows,
           s.actual_rows,
           s.file_count,
           coalesce((
             SELECT sum(v.n_bytes)::bigint
             FROM rvbbit.row_group_variants v
             WHERE v.table_oid = s.table_oid AND v.layout = s.layout
           ), 0),
           s.status_message,
           s.refreshed_at
    FROM rvbbit.layout_variant_status s
    WHERE s.table_oid = rel
    ORDER BY s.layout;
$$;

CREATE OR REPLACE FUNCTION rvbbit.acceleration_phase_log_for(rel regclass)
RETURNS TABLE (
    operation_id bigint,
    operation text,
    phase text,
    layout text,
    layout_kind text,
    partition_key text,
    status text,
    started_at timestamptz,
    finished_at timestamptz,
    elapsed_ms numeric,
    rows_written bigint,
    row_groups_written bigint,
    bytes_written bigint,
    files_written integer,
    expected_rows bigint,
    actual_rows bigint,
    details jsonb,
    error text
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        p.operation_id,
        o.operation,
        p.phase,
        p.layout,
        CASE
          WHEN p.layout LIKE 'hive:%' THEN 'hive'
          WHEN p.layout LIKE 'cluster:%' THEN 'cluster'
          WHEN p.layout = 'vortex_scan' THEN 'vortex'
          ELSE p.layout
        END,
        coalesce(
          p.partition_key,
          CASE
            WHEN p.layout LIKE 'hive:%' THEN substring(p.layout from 6)
            WHEN p.layout LIKE 'cluster:%' THEN substring(p.layout from 9)
            ELSE NULL
          END
        ),
        p.status,
        p.started_at,
        p.finished_at,
        round((extract(epoch FROM coalesce(p.finished_at, clock_timestamp()) - p.started_at) * 1000)::numeric, 3),
        p.rows_written,
        p.row_groups_written,
        p.bytes_written,
        p.files_written,
        p.expected_rows,
        p.actual_rows,
        p.details,
        p.error
    FROM rvbbit.acceleration_operation_phases p
    LEFT JOIN rvbbit.acceleration_operations o ON o.id = p.operation_id
    WHERE p.table_oid = rel
    ORDER BY p.started_at DESC, p.id DESC;
$$;
"#,
    name = "rvbbit_bootstrap",
);

// =====================================================================
// Metrics / BI layer
// =====================================================================
// A metric is a named, versioned SQL template. Definitions live in a
// PLAIN (non-accelerated) append-versioned table so def-time is a simple
// created_at filter, fully decoupled from data-time (rvbbit AS OF on the
// underlying tables). That decoupling is what makes bitemporal metrics
// work: "today's definition over last-quarter's data" and "last-quarter's
// definition over today's data" are independent axes.
//
// Template tokens (resolved by rvbbit.metric_sql / rvbbit.metric):
//   {param}        -> safe SQL literal (quote_nullable of the value)
//   {param!}       -> raw text (identifiers / SQL fragments; caller's risk)
//   {metric:NAME}  -> the named metric inlined as a (subquery);
//                     give it an alias yourself, e.g. FROM {metric:base} b
//
// NOTE: keep this in sync with sql/pg_rvbbit--1.2.7--1.2.8.sql (the
// migration applied to already-installed extensions).
extension_sql!(
    r#"
CREATE TABLE IF NOT EXISTS rvbbit.metric_defs (
    metric_id    bigint GENERATED ALWAYS AS IDENTITY,
    name         text        NOT NULL,
    version      integer     NOT NULL,
    sql          text        NOT NULL,
    params       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    grain        text,
    description  text,
    owner        text,
    labels       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    check_sql    text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);

CREATE INDEX IF NOT EXISTS metric_defs_name_created_idx
    ON rvbbit.metric_defs (name, created_at DESC, version DESC);

CREATE OR REPLACE VIEW rvbbit.metric_catalog AS
SELECT DISTINCT ON (name)
    name, version, sql, params, grain, description, owner, labels, check_sql, created_at
FROM rvbbit.metric_defs
ORDER BY name, created_at DESC, version DESC;

CREATE OR REPLACE FUNCTION rvbbit.define_metric(
    p_name        text,
    p_sql         text,
    p_params      jsonb DEFAULT '{}'::jsonb,
    p_grain       text  DEFAULT NULL,
    p_description text  DEFAULT NULL,
    p_owner       text  DEFAULT NULL,
    p_labels      jsonb DEFAULT '{}'::jsonb,
    p_check       text  DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
BEGIN
    IF p_name IS NULL OR btrim(p_name) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_metric: name is required';
    END IF;
    IF p_sql IS NULL OR btrim(p_sql) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_metric: sql is required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.metric:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version
    FROM rvbbit.metric_defs WHERE name = p_name;
    INSERT INTO rvbbit.metric_defs
        (name, version, sql, params, grain, description, owner, labels, check_sql)
    VALUES
        (p_name, v_version, p_sql, coalesce(p_params, '{}'::jsonb), p_grain,
         p_description, p_owner, coalesce(p_labels, '{}'::jsonb),
         CASE WHEN btrim(coalesce(p_check, '')) = '' THEN NULL ELSE p_check END);
    -- best-effort: cache table deps + default to compaction-materialized.
    PERFORM rvbbit.refresh_metric_dependencies(p_name);
    INSERT INTO rvbbit.metric_materialize (metric_name) VALUES (p_name)
        ON CONFLICT (metric_name) DO NOTHING;
    RETURN v_version;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.resolve_metric(
    p_name      text,
    p_def_as_of timestamptz,
    p_stack     text[],
    OUT r_sql      text,
    OUT r_defaults jsonb
) LANGUAGE plpgsql AS $fn$
DECLARE
    v_params jsonb;
    v_child  text;
    v_rec    record;
BEGIN
    IF p_name = ANY(p_stack) THEN
        RAISE EXCEPTION 'rvbbit.resolve_metric: cycle detected: % -> %',
            array_to_string(p_stack, ' -> '), p_name;
    END IF;

    SELECT sql, coalesce(params, '{}'::jsonb)
      INTO r_sql, v_params
    FROM rvbbit.metric_defs
    WHERE name = p_name
      AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC
    LIMIT 1;

    IF r_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.resolve_metric: metric "%" is not defined as of %',
            p_name, p_def_as_of;
    END IF;

    r_defaults := '{}'::jsonb;

    FOR v_child IN
        SELECT DISTINCT m[1]
        FROM regexp_matches(r_sql, '\{metric:([a-zA-Z0-9_]+)\}', 'g') AS m
    LOOP
        v_rec := rvbbit.resolve_metric(v_child, p_def_as_of, p_stack || p_name);
        r_sql := replace(r_sql, '{metric:' || v_child || '}', '(' || v_rec.r_sql || ')');
        r_defaults := r_defaults || v_rec.r_defaults;
    END LOOP;

    r_defaults := r_defaults || v_params;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.metric_sql(
    p_name      text,
    p_params    jsonb DEFAULT '{}'::jsonb,
    p_def_as_of timestamptz DEFAULT now()
) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_res       record;
    v_effective jsonb;
    v_sql       text;
    v_key       text;
    v_val       text;
BEGIN
    v_res := rvbbit.resolve_metric(p_name, p_def_as_of, ARRAY[]::text[]);
    v_sql := v_res.r_sql;
    v_effective := v_res.r_defaults || coalesce(p_params, '{}'::jsonb);

    FOR v_key, v_val IN SELECT key, value FROM jsonb_each_text(v_effective)
    LOOP
        v_sql := replace(v_sql, '{' || v_key || '!}', coalesce(v_val, ''));
        v_sql := replace(v_sql, '{' || v_key || '}', quote_nullable(v_val));
    END LOOP;

    IF v_sql ~ '\{metric:[a-zA-Z0-9_]+\}' THEN
        RAISE EXCEPTION 'rvbbit.metric_sql: unresolved metric reference in "%": %',
            p_name, (regexp_match(v_sql, '\{metric:[a-zA-Z0-9_]+\}'))[1];
    END IF;

    RETURN v_sql;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.metric(
    p_name       text,
    p_params     jsonb DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS SETOF jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_sql   text;
    v_saved text;
BEGIN
    v_sql := rvbbit.metric_sql(p_name, p_params, p_def_as_of);
    v_sql := rvbbit._resolve_relative_refs(v_sql, v_sql, p_params, p_def_as_of, p_data_as_of);

    -- Pin an EXPLICIT instant for "latest" (now()) rather than leaving it empty:
    -- the implicit latest-snapshot floor is only applied to top-level scans, but
    -- a metric body runs nested in a subquery. An explicit AS OF is read by every
    -- scan via the GUC, so the floor reaches the nested table. now() == latest for
    -- both snapshot (= latest gen) and append (<= now() = cumulative) tables.
    v_saved := current_setting('rvbbit.as_of_timestamp', true);
    PERFORM set_config('rvbbit.as_of_timestamp',
                       coalesce(p_data_as_of::text, now()::text), true);

    BEGIN
        RETURN QUERY EXECUTE 'SELECT to_jsonb(t) FROM (' || v_sql || ') AS t';
    EXCEPTION WHEN OTHERS THEN
        PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
        RAISE EXCEPTION 'rvbbit.metric("%"): % | SQL: %', p_name, SQLERRM, v_sql;
    END;

    PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
    RETURN;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.metric_versions(p_name text)
RETURNS TABLE(version integer, created_at timestamptz, sql text, params jsonb,
              grain text, description text, owner text, check_sql text)
LANGUAGE sql STABLE AS $fn$
    SELECT version, created_at, sql, params, grain, description, owner, check_sql
    FROM rvbbit.metric_defs
    WHERE name = p_name
    ORDER BY version DESC;
$fn$;

-- Compose the executable SQL for an UNSAVED draft body. Mirrors metric_sql
-- but is seeded from an inline `p_sql` instead of a saved name, so the
-- creator UI can preview a metric before it is defined. {metric:NAME} refs
-- still resolve against the saved catalog (as of def-time).
CREATE OR REPLACE FUNCTION rvbbit.preview_metric_sql(
    p_sql       text,
    p_params    jsonb DEFAULT '{}'::jsonb,
    p_def_as_of timestamptz DEFAULT now()
) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_sql       text := p_sql;
    v_defaults  jsonb := '{}'::jsonb;
    v_effective jsonb;
    v_child     text;
    v_rec       record;
    v_key       text;
    v_val       text;
BEGIN
    IF p_sql IS NULL OR btrim(p_sql) = '' THEN
        RETURN p_sql;
    END IF;

    FOR v_child IN
        SELECT DISTINCT m[1]
        FROM regexp_matches(v_sql, '\{metric:([a-zA-Z0-9_]+)\}', 'g') AS m
    LOOP
        v_rec := rvbbit.resolve_metric(v_child, p_def_as_of, ARRAY[]::text[]);
        v_sql := replace(v_sql, '{metric:' || v_child || '}', '(' || v_rec.r_sql || ')');
        v_defaults := v_defaults || v_rec.r_defaults;
    END LOOP;

    v_effective := v_defaults || coalesce(p_params, '{}'::jsonb);

    FOR v_key, v_val IN SELECT key, value FROM jsonb_each_text(v_effective)
    LOOP
        v_sql := replace(v_sql, '{' || v_key || '!}', coalesce(v_val, ''));
        v_sql := replace(v_sql, '{' || v_key || '}', quote_nullable(v_val));
    END LOOP;

    IF v_sql ~ '\{metric:[a-zA-Z0-9_]+\}' THEN
        RAISE EXCEPTION 'rvbbit.preview_metric_sql: unresolved metric reference: %',
            (regexp_match(v_sql, '\{metric:[a-zA-Z0-9_]+\}'))[1];
    END IF;

    RETURN v_sql;
END;
$fn$;

-- =====================================================================
-- KPI checks: a metric becomes a KPI when its def carries a `check_sql`.
-- The check runs against the metric's result exposed as a CTE named `metric`
-- and must reduce to exactly ONE verdict row yielding an `ok` boolean (and
-- optionally status/value/target/...). Thresholds are {param} tokens — versioned
-- defaults, overridable per call. Because check_sql lives on the versioned def
-- row, the threshold is bitemporal: def_as_of pins the metric+check+threshold,
-- data_as_of pins the data.
-- =====================================================================

-- Compose + evaluate an already-resolved metric body + check body. Returns the
-- single verdict jsonb (NULL when there is no check).
CREATE OR REPLACE FUNCTION rvbbit._run_check(
    p_metric_sql text,
    p_check_sql  text,
    p_data_as_of timestamptz
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_full    text;
    v_verdict jsonb;
    v_saved   text;
BEGIN
    IF p_check_sql IS NULL OR btrim(p_check_sql) = '' THEN
        RETURN NULL;
    END IF;

    v_full := 'WITH metric AS (' || p_metric_sql || E'\n) ' || p_check_sql;

    -- Explicit instant for "latest" (now()) so the snapshot floor reaches the
    -- nested `metric` CTE — see rvbbit.metric() for why empty doesn't suffice.
    v_saved := current_setting('rvbbit.as_of_timestamp', true);
    PERFORM set_config('rvbbit.as_of_timestamp', coalesce(p_data_as_of::text, now()::text), true);

    BEGIN
        EXECUTE 'SELECT to_jsonb(t) FROM (' || v_full || ') t' INTO STRICT v_verdict;
    EXCEPTION
        WHEN TOO_MANY_ROWS THEN
            PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
            RAISE EXCEPTION 'rvbbit check returned more than one row; reduce the metric CTE to a single verdict row. | SQL: %', v_full;
        WHEN NO_DATA_FOUND THEN
            PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
            RAISE EXCEPTION 'rvbbit check returned no rows. | SQL: %', v_full;
        WHEN OTHERS THEN
            PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
            RAISE EXCEPTION 'rvbbit check failed: % | SQL: %', SQLERRM, v_full;
    END;

    PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);

    IF v_verdict IS NULL OR NOT (v_verdict ? 'ok') THEN
        RAISE EXCEPTION 'rvbbit check must yield an "ok" boolean column (got: %)',
            coalesce(v_verdict::text, 'no row');
    END IF;

    -- A NULL ok is never "pass"; attach a default pass/fail status if absent.
    IF NOT (v_verdict ? 'status') THEN
        v_verdict := v_verdict || jsonb_build_object(
            'status', CASE WHEN (v_verdict->>'ok')::boolean IS TRUE THEN 'pass' ELSE 'fail' END);
    END IF;

    RETURN v_verdict;
END;
$fn$;

-- Evaluate a SAVED metric's KPI check across the bitemporal axes. def_as_of pins
-- the metric+check+threshold version; data_as_of pins the data. Returns NULL when
-- the metric has no check at that def-time (i.e. it is not a KPI).
CREATE OR REPLACE FUNCTION rvbbit.check_metric(
    p_name       text,
    p_params     jsonb DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_check    text;
    v_defaults jsonb;
    v_eff      jsonb;
    v_msql     text;
    v_csql     text;
BEGIN
    SELECT check_sql, coalesce(params, '{}'::jsonb)
      INTO v_check, v_defaults
    FROM rvbbit.metric_defs
    WHERE name = p_name AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC
    LIMIT 1;

    IF v_check IS NULL OR btrim(v_check) = '' THEN
        RETURN NULL;
    END IF;

    -- The threshold defaults live in the metric def's params; merge them under
    -- the caller's overrides so {target} etc. resolve in the check too.
    v_eff := v_defaults || coalesce(p_params, '{}'::jsonb);
    v_msql := rvbbit.metric_sql(p_name, v_eff, p_def_as_of);
    v_csql := rvbbit.preview_metric_sql(v_check, v_eff, p_def_as_of);
    v_csql := rvbbit._resolve_relative_refs(v_csql, v_msql, v_eff, p_def_as_of, p_data_as_of);
    v_msql := rvbbit._resolve_relative_refs(v_msql, v_msql, v_eff, p_def_as_of, p_data_as_of);
    RETURN rvbbit._run_check(v_msql, v_csql, p_data_as_of);
END;
$fn$;

-- Preview a DRAFT check (Creator): inline metric + check bodies, resolve tokens
-- against the saved catalog, evaluate. data_as_of defaults to latest.
CREATE OR REPLACE FUNCTION rvbbit.preview_check_sql(
    p_metric_sql text,
    p_check_sql  text,
    p_params     jsonb DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_msql text;
    v_csql text;
BEGIN
    IF p_check_sql IS NULL OR btrim(p_check_sql) = '' THEN
        RETURN NULL;
    END IF;
    v_msql := rvbbit.preview_metric_sql(p_metric_sql, p_params, p_def_as_of);
    v_csql := rvbbit.preview_metric_sql(p_check_sql, p_params, p_def_as_of);
    v_csql := rvbbit._resolve_relative_refs(v_csql, v_msql, p_params, p_def_as_of, p_data_as_of);
    v_msql := rvbbit._resolve_relative_refs(v_msql, v_msql, p_params, p_def_as_of, p_data_as_of);
    RETURN rvbbit._run_check(v_msql, v_csql, p_data_as_of);
END;
$fn$;

-- =====================================================================
-- Relative-time metric refs: {metric:NAME.OFFSET} / {metric:self.OFFSET} = the
-- target's SCALAR headline at a SHIFTED data-time (base ± OFFSET), def held fixed.
-- A statement can't carry two AS-OFs, so it's EAGER-EVALUATED + spliced inline as
-- a numeric literal (rolling/delta/WoW become one-liners). Refs don't nest.
-- =====================================================================
CREATE OR REPLACE FUNCTION rvbbit._parse_offset(p_off text) RETURNS interval
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    v text := lower(btrim(p_off));
    n text;
    u text;
BEGIN
    IF v IN ('yesterday','yday')        THEN RETURN interval '-1 day';  END IF;
    IF v IN ('lastweek','lastwk','lwk')  THEN RETURN interval '-7 days'; END IF;
    IF v IN ('lastmonth','lastmo')       THEN RETURN interval '-1 month'; END IF;
    n := (regexp_match(v, '^([+-]?[0-9]+)'))[1];
    u := (regexp_match(v, '([a-z]+)$'))[1];
    IF n IS NULL OR u IS NULL THEN
        RAISE EXCEPTION 'rvbbit: bad relative-time offset "%" (e.g. -1day, -12hours, yesterday)', p_off;
    END IF;
    u := CASE
        WHEN u IN ('s','sec','secs','second','seconds') THEN 'seconds'
        WHEN u IN ('h','hr','hrs','hour','hours')   THEN 'hours'
        WHEN u IN ('d','day','days')                THEN 'days'
        WHEN u IN ('w','wk','wks','week','weeks')    THEN 'weeks'
        WHEN u IN ('min','mins','minute','minutes')  THEN 'minutes'
        WHEN u IN ('mo','mon','month','months')      THEN 'months'
        ELSE u
    END;
    RETURN (n || ' ' || u)::interval;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._resolve_relative_refs(
    p_sql        text,
    p_self_sql   text,
    p_params     jsonb,
    p_def_as_of  timestamptz,
    p_data_as_of timestamptz
) RETURNS text
LANGUAGE plpgsql AS $fn$
DECLARE
    v_sql        text := p_sql;
    v_base       timestamptz := coalesce(p_data_as_of, now());
    v_depth      integer := coalesce(nullif(current_setting('rvbbit.relref_depth', true), ''), '0')::integer;
    v_token      text;
    v_name       text;
    v_off        text;
    v_shifted    timestamptz;
    v_obj        jsonb;
    v_scalar     text;
    v_saved      text;
    v_self_clean text;
BEGIN
    IF p_sql IS NULL OR strpos(p_sql, '{metric:') = 0 THEN
        RETURN p_sql;
    END IF;
    IF v_depth > 8 THEN
        RAISE EXCEPTION 'rvbbit: relative metric-ref recursion too deep (cycle?)';
    END IF;
    PERFORM set_config('rvbbit.relref_depth', (v_depth + 1)::text, true);

    v_self_clean := regexp_replace(coalesce(p_self_sql, ''),
        '\{metric:[a-zA-Z0-9_]+\.[+-]?[0-9a-zA-Z]+\}', 'NULL', 'g');

    FOR v_token, v_name, v_off IN
        SELECT DISTINCT '{metric:' || x[1] || '.' || x[2] || '}', x[1], x[2]
        FROM (SELECT regexp_matches(v_sql, '\{metric:([a-zA-Z0-9_]+)\.([+-]?[0-9a-zA-Z]+)\}', 'g') AS x) s
    LOOP
        v_shifted := v_base + rvbbit._parse_offset(v_off);
        v_obj := NULL;

        IF v_name = 'self' THEN
            v_saved := current_setting('rvbbit.as_of_timestamp', true);
            PERFORM set_config('rvbbit.as_of_timestamp', v_shifted::text, true);
            BEGIN
                EXECUTE format('SELECT to_jsonb(t) FROM (%s) t LIMIT 1', v_self_clean) INTO v_obj;
            EXCEPTION WHEN OTHERS THEN
                v_obj := NULL;
            END;
            PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
        ELSE
            BEGIN
                SELECT mm.obj INTO v_obj
                FROM rvbbit.metric(v_name, p_params, p_def_as_of, v_shifted) AS mm(obj) LIMIT 1;
            EXCEPTION WHEN OTHERS THEN
                v_obj := NULL;
            END;
        END IF;

        v_scalar := NULL;
        IF v_obj IS NOT NULL THEN
            SELECT coalesce(
              (SELECT je.value FROM jsonb_each_text(v_obj) je
                 WHERE je.key = 'value' AND je.value ~ '^-?[0-9]+(\.[0-9]+)?$' LIMIT 1),
              (SELECT je.value FROM jsonb_each_text(v_obj) je
                 WHERE je.value ~ '^-?[0-9]+(\.[0-9]+)?$' LIMIT 1)
            ) INTO v_scalar;
        END IF;

        v_sql := replace(v_sql, v_token, coalesce(v_scalar, 'NULL'));
    END LOOP;

    PERFORM set_config('rvbbit.relref_depth', v_depth::text, true);
    RETURN v_sql;
EXCEPTION WHEN OTHERS THEN
    PERFORM set_config('rvbbit.relref_depth', v_depth::text, true);
    RAISE;
END;
$fn$;

-- =====================================================================
-- Materialization: a durable, append-only log of what-was-reported
-- (value, verdict, threshold-version, bitemporal coords, trigger). Live reads
-- stay live (re-run AS OF); this outlives generation reaping + records the
-- verdict AS-DECIDED. Default cadence = COMPACTION-TRIGGERED (a new generation
-- enqueues; materialize_tick drains, materializing dependents at def_as_of = the
-- gen's commit time). Observations are immutable; alt-history stays a live query.
-- =====================================================================
CREATE TABLE IF NOT EXISTS rvbbit.metric_observations (
    observation_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    metric_name     text NOT NULL,
    metric_version  integer,
    def_as_of       timestamptz,
    data_as_of      timestamptz,
    data_generation bigint,
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    value           jsonb,
    verdict         jsonb,
    status          text,
    observed_at     timestamptz NOT NULL DEFAULT now(),
    trigger         text NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS metric_observations_name_data_idx
    ON rvbbit.metric_observations (metric_name, data_as_of DESC);
CREATE INDEX IF NOT EXISTS metric_observations_name_observed_idx
    ON rvbbit.metric_observations (metric_name, observed_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.metric_materialize (
    metric_name   text PRIMARY KEY,
    on_compaction boolean NOT NULL DEFAULT true,
    cron_schedule text,
    enabled       boolean NOT NULL DEFAULT true,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION rvbbit.set_materialize(
    p_name          text,
    p_on_compaction boolean DEFAULT true,
    p_cron_schedule text DEFAULT NULL,
    p_enabled       boolean DEFAULT true
) RETURNS void LANGUAGE sql AS $fn$
    INSERT INTO rvbbit.metric_materialize (metric_name, on_compaction, cron_schedule, enabled, updated_at)
    VALUES (p_name, p_on_compaction, p_cron_schedule, p_enabled, now())
    ON CONFLICT (metric_name) DO UPDATE
      SET on_compaction = EXCLUDED.on_compaction,
          cron_schedule = EXCLUDED.cron_schedule,
          enabled       = EXCLUDED.enabled,
          updated_at    = now();
$fn$;

CREATE TABLE IF NOT EXISTS rvbbit.metric_dependencies (
    metric_name  text NOT NULL,
    table_oid    oid  NOT NULL,
    table_schema text,
    table_name   text,
    PRIMARY KEY (metric_name, table_oid)
);
CREATE INDEX IF NOT EXISTS metric_dependencies_table_idx
    ON rvbbit.metric_dependencies (table_oid);

CREATE OR REPLACE FUNCTION rvbbit.refresh_metric_dependencies(p_name text)
RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    v_sql   text;
    v_expl  jsonb;
    v_count integer := 0;
BEGIN
    BEGIN
        v_sql := rvbbit.metric_sql(p_name, '{}'::jsonb, now());
    EXCEPTION WHEN OTHERS THEN
        RETURN 0;
    END;
    IF v_sql IS NULL OR btrim(v_sql) = '' THEN RETURN 0; END IF;

    BEGIN
        v_expl := rvbbit.route_explain(v_sql);
    EXCEPTION WHEN OTHERS THEN
        RETURN 0;
    END;

    DELETE FROM rvbbit.metric_dependencies WHERE metric_name = p_name;
    INSERT INTO rvbbit.metric_dependencies (metric_name, table_oid, table_schema, table_name)
    SELECT p_name, (t->>'oid')::oid, t->>'schema', t->>'table'
    FROM jsonb_array_elements(coalesce(v_expl->'rvbbit_tables', '[]'::jsonb)) t
    WHERE (t->>'oid') IS NOT NULL
    ON CONFLICT (metric_name, table_oid) DO NOTHING;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.materialize_metric(
    p_name            text,
    p_params          jsonb DEFAULT '{}'::jsonb,
    p_def_as_of       timestamptz DEFAULT now(),
    p_data_as_of      timestamptz DEFAULT NULL,
    p_data_generation bigint DEFAULT NULL,
    p_trigger         text DEFAULT 'manual'
) RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
    v_value   jsonb;
    v_verdict jsonb;
    v_obs_id  bigint;
BEGIN
    SELECT version INTO v_version
    FROM rvbbit.metric_defs
    WHERE name = p_name AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC LIMIT 1;
    IF v_version IS NULL THEN
        RAISE EXCEPTION 'rvbbit.materialize_metric: metric "%" not defined as of %', p_name, p_def_as_of;
    END IF;

    SELECT jsonb_agg(obj) INTO v_value
    FROM rvbbit.metric(p_name, p_params, p_def_as_of, p_data_as_of) AS m(obj);

    v_verdict := rvbbit.check_metric(p_name, p_params, p_def_as_of, p_data_as_of);

    INSERT INTO rvbbit.metric_observations
        (metric_name, metric_version, def_as_of, data_as_of, data_generation,
         params, value, verdict, status, trigger)
    VALUES
        (p_name, v_version, p_def_as_of, coalesce(p_data_as_of, now()), p_data_generation,
         coalesce(p_params, '{}'::jsonb), v_value, v_verdict, v_verdict->>'status', p_trigger)
    RETURNING observation_id INTO v_obs_id;
    RETURN v_obs_id;
END;
$fn$;

CREATE TABLE IF NOT EXISTS rvbbit.materialize_queue (
    table_oid    oid NOT NULL,
    generation   bigint NOT NULL,
    committed_at timestamptz NOT NULL,
    enqueued_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, generation)
);

CREATE OR REPLACE FUNCTION rvbbit._enqueue_materialize() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    BEGIN
        IF EXISTS (SELECT 1 FROM rvbbit.metric_dependencies WHERE table_oid = NEW.table_oid) THEN
            INSERT INTO rvbbit.materialize_queue (table_oid, generation, committed_at)
            VALUES (NEW.table_oid, NEW.generation, NEW.committed_at)
            ON CONFLICT (table_oid, generation) DO NOTHING;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        NULL;
    END;
    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS rvbbit_generations_materialize ON rvbbit.generations;
CREATE TRIGGER rvbbit_generations_materialize
    AFTER INSERT ON rvbbit.generations
    FOR EACH ROW EXECUTE FUNCTION rvbbit._enqueue_materialize();

CREATE OR REPLACE FUNCTION rvbbit.materialize_tick(p_max integer DEFAULT 200)
RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    v_item   record;
    v_metric text;
    v_done   integer := 0;
BEGIN
    FOR v_item IN
        SELECT table_oid, generation, committed_at
        FROM rvbbit.materialize_queue
        ORDER BY enqueued_at
        LIMIT greatest(p_max, 1)
        FOR UPDATE SKIP LOCKED
    LOOP
        FOR v_metric IN
            SELECT d.metric_name
            FROM rvbbit.metric_dependencies d
            JOIN rvbbit.metric_materialize p ON p.metric_name = d.metric_name
            WHERE d.table_oid = v_item.table_oid
              AND p.enabled AND p.on_compaction
        LOOP
            BEGIN
                PERFORM rvbbit.materialize_metric(
                    v_metric, '{}'::jsonb, v_item.committed_at, v_item.committed_at,
                    v_item.generation, 'compaction');
                v_done := v_done + 1;
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;
        END LOOP;
        DELETE FROM rvbbit.materialize_queue
        WHERE table_oid = v_item.table_oid AND generation = v_item.generation;
    END LOOP;
    RETURN v_done;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.metric_history(p_name text, p_limit integer DEFAULT 200)
RETURNS TABLE(observation_id bigint, metric_version integer, def_as_of timestamptz,
              data_as_of timestamptz, data_generation bigint, value jsonb,
              verdict jsonb, status text, observed_at timestamptz, trigger text)
LANGUAGE sql STABLE AS $fn$
    SELECT observation_id, metric_version, def_as_of, data_as_of, data_generation,
           value, verdict, status, observed_at, trigger
    FROM rvbbit.metric_observations
    WHERE metric_name = p_name
    ORDER BY data_as_of DESC, observed_at DESC
    LIMIT greatest(p_limit, 1);
$fn$;

-- ---------------------------------------------------------------------
-- KPI board: pivot the observation log into a (metric x data-time) grid.
-- One row per (metric, data-time bucket) = the latest observation that
-- landed in that bucket. The board reads this for the fast historical
-- matrix; live recompute (restatement, def-time scrub, threshold what-if)
-- layers on top via metric()/check_metric()/metric_sql().
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rvbbit.metric_board(
    p_metrics text[]      DEFAULT NULL,
    p_from    timestamptz DEFAULT (now() - interval '30 days'),
    p_to      timestamptz DEFAULT now(),
    p_bucket  text        DEFAULT 'day'
) RETURNS SETOF jsonb
LANGUAGE sql STABLE AS $fn$
    SELECT to_jsonb(c)
    FROM (
        SELECT DISTINCT ON (o.metric_name, date_trunc(p_bucket, COALESCE(o.data_as_of, o.observed_at)))
            o.metric_name                                              AS metric,
            date_trunc(p_bucket, COALESCE(o.data_as_of, o.observed_at)) AS bucket,
            COALESCE(o.data_as_of, o.observed_at)                       AS data_as_of,
            o.def_as_of,
            o.params,
            o.data_generation,
            o.metric_version,
            o.value,
            o.verdict,
            o.status,
            o.trigger,
            o.observed_at
        FROM rvbbit.metric_observations o
        WHERE COALESCE(o.data_as_of, o.observed_at) >= p_from
          AND COALESCE(o.data_as_of, o.observed_at) <= p_to
          AND (p_metrics IS NULL OR o.metric_name = ANY(p_metrics))
        ORDER BY o.metric_name,
                 date_trunc(p_bucket, COALESCE(o.data_as_of, o.observed_at)),
                 COALESCE(o.data_as_of, o.observed_at) DESC,
                 o.observed_at DESC
    ) c;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.schedule_materialize_tick(
    cron_schedule text DEFAULT '* * * * *',
    budget        integer DEFAULT 200
) RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
    jobid     bigint;
    cron_home text := current_setting('cron.database_name', true);
    this_db   text := current_database();
    command   text := format('SELECT rvbbit.materialize_tick(%s)', budget);
BEGIN
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = format('connect to %L and run: SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                cron_home, 'rvbbit_materialize_tick', cron_schedule, command, this_db);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; call rvbbit.materialize_tick() manually.';
    END IF;
    EXECUTE format('SELECT cron.schedule(%L, %L, %L)', 'rvbbit_materialize_tick', cron_schedule, command)
        INTO jobid;
    RETURN jobid;
END;
$fn$;
"#,
    name = "rvbbit_metrics",
    requires = ["rvbbit_bootstrap"],
);

// ===========================================================================
// Alerts — reactive condition -> operator automation (P0: schema + rule DDL).
//
// A rule is a versioned, immutable DEFINITION (alert_rules, mirrors metric_defs)
// plus a small MUTABLE control row (alert_control, mirrors metric_materialize)
// holding enabled/muted/cadence. The reconciler (P1) reads rules, diffs against
// alert_state, and enqueues transitions to alert_queue; a worker (P2) drains it
// and logs to alert_events; alert_sweep_runs is the sweep heartbeat.
//
// KEEP IN SYNC with crates/pg_rvbbit/sql/pg_rvbbit--A--B.sql (the upgrade edge).
// ===========================================================================
extension_sql!(
    r#"
-- Versioned, immutable rule definition (def-time axis = created_at).
CREATE TABLE IF NOT EXISTS rvbbit.alert_rules (
    alert_rule_id  bigint GENERATED ALWAYS AS IDENTITY,
    name           text        NOT NULL,
    version        integer     NOT NULL,
    condition_spec jsonb       NOT NULL DEFAULT '{}'::jsonb,
    fire_policy    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    action_spec    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    cardinality    text        NOT NULL DEFAULT 'per_entity',
    fan_out_cap    integer     NOT NULL DEFAULT 100,
    description    text,
    owner          text,
    labels         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);
CREATE INDEX IF NOT EXISTS alert_rules_name_created_idx
    ON rvbbit.alert_rules (name, created_at DESC, version DESC);

-- Mutable runtime control, one row per rule name (survives re-definition).
CREATE TABLE IF NOT EXISTS rvbbit.alert_control (
    name         text PRIMARY KEY,
    enabled      boolean     NOT NULL DEFAULT true,
    muted_until  timestamptz,
    cadence_tier text        NOT NULL DEFAULT 'normal',
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Reconciler memory: last observed status per (rule, entity). Keyed by NAME so
-- it survives re-definition; '' entity_key for a scalar rule.
CREATE TABLE IF NOT EXISTS rvbbit.alert_state (
    rule_name       text        NOT NULL,
    entity_key      text        NOT NULL DEFAULT '',
    last_status     text,
    score           numeric,
    consecutive     integer     NOT NULL DEFAULT 0,
    last_changed_at timestamptz,
    last_fired_at   timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_name, entity_key)
);

-- Pending actions: the sweep enqueues transitions; the worker (P2) drains.
CREATE TABLE IF NOT EXISTS rvbbit.alert_queue (
    queue_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_name     text        NOT NULL,
    entity_key    text        NOT NULL DEFAULT '',
    transition    text        NOT NULL DEFAULT 'enter_fail',
    rendered_args jsonb,
    status        text        NOT NULL DEFAULT 'pending',
    attempts      integer     NOT NULL DEFAULT 0,
    enqueued_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alert_queue_drain_idx
    ON rvbbit.alert_queue (status, enqueued_at);

-- Firing log: one row per action attempt (audit + external-artifact correlation).
CREATE TABLE IF NOT EXISTS rvbbit.alert_events (
    event_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_name         text        NOT NULL,
    entity_key        text        NOT NULL DEFAULT '',
    transition        text        NOT NULL DEFAULT 'enter_fail',
    action_receipt_id text,
    action_output     jsonb,
    external_artifact text,
    status            text        NOT NULL DEFAULT 'fired',
    error             text,
    ts                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alert_events_rule_ts_idx
    ON rvbbit.alert_events (rule_name, ts DESC);

-- Sweep heartbeat: makes the reconciler itself observable.
CREATE TABLE IF NOT EXISTS rvbbit.alert_sweep_runs (
    sweep_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tier            text        NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    rules_evaluated integer     NOT NULL DEFAULT 0,
    transitions     integer     NOT NULL DEFAULT 0,
    enqueued        integer     NOT NULL DEFAULT 0,
    errors          integer     NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alert_sweep_runs_tier_started_idx
    ON rvbbit.alert_sweep_runs (tier, started_at DESC);

-- Global kill-switch (the sweep + worker check this).
INSERT INTO rvbbit.settings (key, value)
VALUES ('alerts_enabled', to_jsonb(true))
ON CONFLICT (key) DO NOTHING;

-- ---- DDL functions (mirror define_metric / resolve_metric) ----------------
CREATE OR REPLACE FUNCTION rvbbit.define_alert(
    p_name        text,
    p_condition   jsonb,
    p_action      jsonb,
    p_fire_policy jsonb    DEFAULT '{}'::jsonb,
    p_cardinality text     DEFAULT 'per_entity',
    p_fan_out_cap integer  DEFAULT 100,
    p_cadence     text     DEFAULT 'normal',
    p_description text     DEFAULT NULL,
    p_owner       text     DEFAULT NULL,
    p_labels      jsonb    DEFAULT '{}'::jsonb
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
BEGIN
    IF p_name IS NULL OR btrim(p_name) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_alert: name is required';
    END IF;
    IF p_condition IS NULL OR p_condition = '{}'::jsonb THEN
        RAISE EXCEPTION 'rvbbit.define_alert: condition is required';
    END IF;
    IF p_action IS NULL OR p_action = '{}'::jsonb THEN
        RAISE EXCEPTION 'rvbbit.define_alert: action is required';
    END IF;
    IF coalesce(p_condition->>'kind', 'sql') = 'metric'
       AND coalesce(p_condition->>'metric', '') = '' THEN
        RAISE EXCEPTION 'rvbbit.define_alert: condition kind=metric requires a metric name';
    END IF;
    IF coalesce(p_action->>'operator', '') = 'operator'
       AND coalesce(p_action->>'operator_name', '') = '' THEN
        RAISE EXCEPTION 'rvbbit.define_alert: operator action requires operator_name';
    END IF;
    IF p_cardinality NOT IN ('per_entity', 'aggregate') THEN
        RAISE EXCEPTION 'rvbbit.define_alert: cardinality must be per_entity or aggregate (got %)', p_cardinality;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.alert:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version
    FROM rvbbit.alert_rules WHERE name = p_name;
    INSERT INTO rvbbit.alert_rules
        (name, version, condition_spec, fire_policy, action_spec,
         cardinality, fan_out_cap, description, owner, labels)
    VALUES
        (p_name, v_version, p_condition, coalesce(p_fire_policy, '{}'::jsonb), p_action,
         p_cardinality, greatest(coalesce(p_fan_out_cap, 100), 1), p_description, p_owner,
         coalesce(p_labels, '{}'::jsonb));
    -- Runtime control row: created on first define, preserved on re-definition.
    INSERT INTO rvbbit.alert_control (name, cadence_tier)
    VALUES (p_name, coalesce(nullif(btrim(p_cadence), ''), 'normal'))
    ON CONFLICT (name) DO NOTHING;
    RETURN v_version;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.resolve_alert(
    p_name        text,
    p_def_as_of   timestamptz DEFAULT now(),
    OUT r_version     integer,
    OUT r_condition   jsonb,
    OUT r_fire_policy jsonb,
    OUT r_action      jsonb,
    OUT r_cardinality text,
    OUT r_fan_out_cap integer
) LANGUAGE plpgsql AS $fn$
BEGIN
    SELECT version, condition_spec, fire_policy, action_spec, cardinality, fan_out_cap
      INTO r_version, r_condition, r_fire_policy, r_action, r_cardinality, r_fan_out_cap
    FROM rvbbit.alert_rules
    WHERE name = p_name
      AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC
    LIMIT 1;
    IF r_version IS NULL THEN
        RAISE EXCEPTION 'rvbbit.resolve_alert: no alert named % as of %', p_name, p_def_as_of;
    END IF;
END;
$fn$;

-- Latest definition per name, joined with the runtime control flags.
CREATE OR REPLACE VIEW rvbbit.alert_catalog AS
SELECT DISTINCT ON (r.name)
    r.name, r.version, r.condition_spec, r.fire_policy, r.action_spec,
    r.cardinality, r.fan_out_cap, r.description, r.owner, r.labels, r.created_at,
    coalesce(c.enabled, true)                             AS enabled,
    c.muted_until,
    (c.muted_until IS NOT NULL AND c.muted_until > now()) AS muted,
    coalesce(c.cadence_tier, 'normal')                    AS cadence_tier
FROM rvbbit.alert_rules r
LEFT JOIN rvbbit.alert_control c ON c.name = r.name
ORDER BY r.name, r.created_at DESC, r.version DESC;

-- ---- Control toggles (mutate alert_control) -------------------------------
CREATE OR REPLACE FUNCTION rvbbit.enable_alert(p_name text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
    INSERT INTO rvbbit.alert_control (name, enabled, updated_at)
    VALUES (p_name, true, now())
    ON CONFLICT (name) DO UPDATE SET enabled = true, updated_at = now();
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.disable_alert(p_name text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
    INSERT INTO rvbbit.alert_control (name, enabled, updated_at)
    VALUES (p_name, false, now())
    ON CONFLICT (name) DO UPDATE SET enabled = false, updated_at = now();
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.mute_alert(p_name text, p_duration interval DEFAULT NULL)
RETURNS timestamptz
LANGUAGE plpgsql AS $fn$
DECLARE
    v_until timestamptz := CASE WHEN p_duration IS NULL THEN 'infinity'::timestamptz ELSE now() + p_duration END;
BEGIN
    INSERT INTO rvbbit.alert_control (name, muted_until, updated_at)
    VALUES (p_name, v_until, now())
    ON CONFLICT (name) DO UPDATE SET muted_until = v_until, updated_at = now();
    RETURN v_until;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.unmute_alert(p_name text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
    INSERT INTO rvbbit.alert_control (name, muted_until, updated_at)
    VALUES (p_name, NULL, now())
    ON CONFLICT (name) DO UPDATE SET muted_until = NULL, updated_at = now();
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.set_alert_cadence(p_name text, p_tier text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
    IF p_tier NOT IN ('fast', 'normal', 'slow') THEN
        RAISE EXCEPTION 'rvbbit.set_alert_cadence: tier must be fast, normal, or slow (got %)', p_tier;
    END IF;
    INSERT INTO rvbbit.alert_control (name, cadence_tier, updated_at)
    VALUES (p_name, p_tier, now())
    ON CONFLICT (name) DO UPDATE SET cadence_tier = p_tier, updated_at = now();
END;
$fn$;

-- Remove a rule and everything keyed to it. Returns true if it existed.
CREATE OR REPLACE FUNCTION rvbbit.delete_alert(p_name text) RETURNS boolean
LANGUAGE plpgsql AS $fn$
DECLARE
    v_existed boolean;
BEGIN
    SELECT EXISTS (SELECT 1 FROM rvbbit.alert_rules WHERE name = p_name) INTO v_existed;
    DELETE FROM rvbbit.alert_events  WHERE rule_name = p_name;
    DELETE FROM rvbbit.alert_queue   WHERE rule_name = p_name;
    DELETE FROM rvbbit.alert_state   WHERE rule_name = p_name;
    DELETE FROM rvbbit.alert_control WHERE name = p_name;
    DELETE FROM rvbbit.alert_rules   WHERE name = p_name;
    RETURN v_existed;
END;
$fn$;

-- ---- Global kill-switch ----------------------------------------------------
CREATE OR REPLACE FUNCTION rvbbit.alerts_enabled() RETURNS boolean
LANGUAGE sql STABLE AS $fn$
    SELECT coalesce(
        (SELECT value #>> '{}' FROM rvbbit.settings WHERE key = 'alerts_enabled'),
        'true'
    )::boolean
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.set_alerts_enabled(p_on boolean) RETURNS boolean
LANGUAGE plpgsql AS $fn$
BEGIN
    INSERT INTO rvbbit.settings (key, value, updated_at)
    VALUES ('alerts_enabled', to_jsonb(p_on), clock_timestamp())
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = clock_timestamp();
    RETURN p_on;
END;
$fn$;

-- ---- P1: the reconciler ----------------------------------------------------
-- One sweep over the rules in a cadence tier: evaluate each rule's condition
-- query -> (entity_key, status), diff against alert_state, and ENQUEUE
-- transitions (it never calls the action — the P2 worker drains alert_queue).
-- Edge-triggered: fires once per fail-episode (status='fail'), after
-- consecutive_n hysteresis, re-arming on recovery; cooldown_secs throttles;
-- fan_out_cap bounds enqueues per rule per sweep (excess stays eligible and
-- drains over later sweeps — nothing is silently dropped). Each rule runs in
-- its own subtransaction so a bad condition query can't abort the whole sweep.
-- Uses clock_timestamp() (not now()) so episode timing advances within a txn.
-- A metric-ref condition rides the referenced metric's latest KPI verdict
-- (pass/fail, already 'pass'/'fail' text) from the pre-materialized observations
-- log — no re-run, no threshold. entity_key = the metric name (scalar).
CREATE OR REPLACE FUNCTION rvbbit._alert_metric_condition_sql(p_metric text)
RETURNS text
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT format(
        'SELECT %L::text AS entity_key, o.status AS status FROM ('
        || 'SELECT status FROM rvbbit.metric_observations '
        || 'WHERE metric_name = %L ORDER BY data_as_of DESC NULLS LAST, observed_at DESC LIMIT 1'
        || ') o WHERE o.status IS NOT NULL',
        p_metric, p_metric);
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.alert_sweep(p_tier text DEFAULT 'normal')
RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_now         timestamptz := clock_timestamp();
    v_sweep_id    bigint;
    v_rule        record;
    v_row         record;
    v_j           jsonb;
    v_query       text;
    v_n           integer;
    v_cooldown    integer;
    v_cap         integer;
    v_thresh      numeric;
    v_cmp         text;
    v_score       numeric;
    v_rules       integer := 0;
    v_transitions integer := 0;
    v_enqueued    integer := 0;
    v_errors      integer := 0;
    v_rule_fires  integer;
    v_ek          text;
    v_status      text;
    v_ps          text;
    v_pc          integer;
    v_pchg        timestamptz;
    v_pfire       timestamptz;
    v_consec      integer;
    v_changed     timestamptz;
    v_eligible    boolean;
    v_did_enqueue boolean;
    v_kind        text;
    v_expr        text;
    v_has_expr    boolean;
BEGIN
    INSERT INTO rvbbit.alert_sweep_runs (tier, started_at)
    VALUES (p_tier, v_now) RETURNING sweep_id INTO v_sweep_id;

    IF NOT rvbbit.alerts_enabled() THEN
        UPDATE rvbbit.alert_sweep_runs SET finished_at = clock_timestamp() WHERE sweep_id = v_sweep_id;
        RETURN jsonb_build_object('sweep_id', v_sweep_id, 'skipped', true, 'reason', 'alerts_disabled');
    END IF;

    FOR v_rule IN
        SELECT name, condition_spec, fire_policy, fan_out_cap
        FROM rvbbit.alert_catalog
        WHERE enabled AND NOT muted AND cadence_tier = p_tier
    LOOP
        v_rules := v_rules + 1;
        v_rule_fires := 0;
        v_n        := greatest(coalesce((v_rule.fire_policy->>'consecutive_n')::int, 1), 1);
        v_cooldown := greatest(coalesce((v_rule.fire_policy->>'cooldown_secs')::int, 0), 0);
        v_cap      := greatest(coalesce(v_rule.fan_out_cap, 100), 1);
        v_thresh   := nullif(v_rule.condition_spec->>'threshold', '')::numeric;
        v_cmp      := coalesce(v_rule.condition_spec->>'compare', 'gte');
        v_kind     := coalesce(v_rule.condition_spec->>'kind', 'sql');

        BEGIN  -- per-rule subtransaction: an error here won't abort the sweep
            -- Resolve the condition into an executable query. 'sql' runs the
            -- free-form query; 'metric' rides the referenced metric's latest KPI
            -- verdict from metric_observations; unknown kinds are skipped.
            IF v_kind = 'metric' THEN
                IF coalesce(v_rule.condition_spec->>'metric', '') = '' THEN
                    RAISE EXCEPTION 'alert %: condition kind=metric has no metric name', v_rule.name;
                END IF;
                v_query := rvbbit._alert_metric_condition_sql(v_rule.condition_spec->>'metric');
            ELSIF v_kind = 'sql' THEN
                v_query := v_rule.condition_spec->>'query';
            ELSE
                CONTINUE;
            END IF;
            IF v_query IS NULL OR btrim(v_query) = '' THEN
                RAISE EXCEPTION 'alert %: condition produced no query (kind=%)', v_rule.name, v_kind;
            END IF;
            -- optional boolean expression over the query's columns: a row fails
            -- when the expr is true. Postgres rejects a non-boolean expr (the
            -- CASE/WHEN type check), so a bad expr surfaces as a per-rule error.
            v_expr := v_rule.condition_spec->>'expr';
            v_has_expr := (v_kind = 'sql' AND coalesce(btrim(v_expr), '') <> '');
            IF v_has_expr THEN
                v_query := format(
                    'SELECT q2.*, CASE WHEN (%s) THEN ''fail'' ELSE ''pass'' END AS _alert_status FROM (%s) q2',
                    v_expr, v_query);
            END IF;

            -- Read each row as jsonb so the query may return a `status` (text
            -- 'pass'/'fail') OR a `score` (numeric, thresholded below) — a missing
            -- key comes back NULL instead of erroring. '' entity_key = scalar.
            FOR v_row IN EXECUTE 'SELECT to_jsonb(q) AS j FROM (' || v_query || ') q' LOOP
                v_j      := v_row.j;
                v_ek     := coalesce(v_j ->> 'entity_key', '');
                v_status := CASE WHEN v_has_expr THEN v_j ->> '_alert_status' ELSE v_j ->> 'status' END;
                v_score  := nullif(v_j ->> 'score', '')::numeric;
                -- score-not-vibe: derive status from a numeric score + threshold,
                -- so a semantic/anomaly score rides the same edge-trigger path.
                IF v_status IS NULL AND v_score IS NOT NULL AND v_thresh IS NOT NULL THEN
                    v_status := CASE
                        WHEN v_cmp = 'lte' AND v_score <= v_thresh THEN 'fail'
                        WHEN v_cmp = 'gte' AND v_score >= v_thresh THEN 'fail'
                        ELSE 'pass' END;
                END IF;

                SELECT last_status, consecutive, last_changed_at, last_fired_at
                  INTO v_ps, v_pc, v_pchg, v_pfire
                  FROM rvbbit.alert_state
                 WHERE rule_name = v_rule.name AND entity_key = v_ek;

                IF v_status = 'fail' THEN
                    IF v_ps = 'fail' THEN
                        v_consec  := coalesce(v_pc, 0) + 1;
                        v_changed := coalesce(v_pchg, v_now);
                    ELSE
                        v_consec  := 1;
                        v_changed := v_now;          -- new fail-episode starts here
                    END IF;
                ELSE
                    v_consec  := 0;
                    v_changed := CASE WHEN v_ps IS DISTINCT FROM v_status
                                      THEN v_now ELSE coalesce(v_pchg, v_now) END;
                END IF;

                -- Fire once per episode (last_fired_at < the episode's start),
                -- after hysteresis, under cooldown.
                v_eligible := v_status = 'fail'
                          AND v_consec >= v_n
                          AND (v_pfire IS NULL OR v_pfire < v_changed)
                          AND (v_pfire IS NULL OR extract(epoch FROM (v_now - v_pfire)) >= v_cooldown);

                v_did_enqueue := false;
                IF v_eligible THEN
                    v_transitions := v_transitions + 1;
                    IF v_rule_fires < v_cap THEN
                        INSERT INTO rvbbit.alert_queue (rule_name, entity_key, transition, enqueued_at)
                        VALUES (v_rule.name, v_ek, 'enter_fail', v_now);
                        v_rule_fires  := v_rule_fires + 1;
                        v_enqueued    := v_enqueued + 1;
                        v_did_enqueue := true;
                    END IF;  -- over cap: leave eligible (drains next sweep)
                END IF;

                INSERT INTO rvbbit.alert_state
                    (rule_name, entity_key, last_status, score, consecutive, last_changed_at, last_fired_at, updated_at)
                VALUES
                    (v_rule.name, v_ek, v_status, v_score, v_consec, v_changed,
                     CASE WHEN v_did_enqueue THEN v_now ELSE v_pfire END, v_now)
                ON CONFLICT (rule_name, entity_key) DO UPDATE
                   SET last_status     = EXCLUDED.last_status,
                       score           = EXCLUDED.score,
                       consecutive     = EXCLUDED.consecutive,
                       last_changed_at = EXCLUDED.last_changed_at,
                       last_fired_at   = EXCLUDED.last_fired_at,
                       updated_at      = EXCLUDED.updated_at;
            END LOOP;
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors + 1;
        END;
    END LOOP;

    UPDATE rvbbit.alert_sweep_runs
       SET finished_at = clock_timestamp(), rules_evaluated = v_rules,
           transitions = v_transitions, enqueued = v_enqueued, errors = v_errors
     WHERE sweep_id = v_sweep_id;

    RETURN jsonb_build_object('sweep_id', v_sweep_id, 'rules_evaluated', v_rules,
                              'transitions', v_transitions, 'enqueued', v_enqueued, 'errors', v_errors);
END;
$fn$;

-- ---- P3: action arg-binding (templated body <- alert context) -------------
-- Replace {key} tokens in a string with context values.
CREATE OR REPLACE FUNCTION rvbbit._alert_interpolate(p_str text, p_ctx jsonb)
RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    v_out text := p_str;
    v_key text;
BEGIN
    FOR v_key IN SELECT DISTINCT (regexp_matches(p_str, '\{(\w+)\}', 'g'))[1] LOOP
        v_out := replace(v_out, '{' || v_key || '}', coalesce(p_ctx ->> v_key, ''));
    END LOOP;
    RETURN v_out;
END;
$fn$;

-- Recursively render an args template against the context. A whole-string
-- placeholder ("{count}") keeps the context value's JSON TYPE (number stays a
-- number); an embedded one ("hi {entity}") interpolates as text.
CREATE OR REPLACE FUNCTION rvbbit._alert_render_args(p_template jsonb, p_ctx jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    v_type  text := jsonb_typeof(p_template);
    v_out   jsonb;
    v_key   text;
    v_val   jsonb;
    v_str   text;
    v_whole text;
BEGIN
    IF v_type = 'object' THEN
        v_out := '{}'::jsonb;
        FOR v_key, v_val IN SELECT * FROM jsonb_each(p_template) LOOP
            v_out := v_out || jsonb_build_object(v_key, rvbbit._alert_render_args(v_val, p_ctx));
        END LOOP;
        RETURN v_out;
    ELSIF v_type = 'array' THEN
        SELECT coalesce(jsonb_agg(rvbbit._alert_render_args(elem, p_ctx)), '[]'::jsonb)
          INTO v_out FROM jsonb_array_elements(p_template) elem;
        RETURN v_out;
    ELSIF v_type = 'string' THEN
        v_str   := p_template #>> '{}';
        v_whole := substring(v_str from '^\{(\w+)\}$');
        IF v_whole IS NOT NULL AND p_ctx ? v_whole THEN
            RETURN p_ctx -> v_whole;
        END IF;
        RETURN to_jsonb(rvbbit._alert_interpolate(v_str, p_ctx));
    ELSE
        RETURN p_template;  -- number / boolean / null pass through
    END IF;
END;
$fn$;

-- Resolve + run an alert's action. Every action is one operator-call shape:
--   noop     -> {ok}                       (test)
--   sql      -> EXECUTE action.sql USING $1=context jsonb  (test; safe, parameterized)
--   mcp_call -> rvbbit.mcp_call(server, tool, render(args, ctx))  (live)
--   flow     -> rvbbit.flow(interpolate(spec, ctx))              (live)
-- Validation is LIGHT here (server/tool present, rendered args is an object);
-- deep manifest-schema validation lives in the Alerts UI authoring form.
CREATE OR REPLACE FUNCTION rvbbit._alert_dispatch(
    p_rule text, p_entity text, p_transition text, p_action jsonb
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_op   text  := coalesce(p_action->>'operator', '');
    v_ctx  jsonb := jsonb_build_object('rule', p_rule, 'entity', p_entity, 'transition', p_transition);
    v_args jsonb;
    v_sql  text;
    v_spec text;
BEGIN
    IF v_op = 'noop' THEN
        RETURN jsonb_build_object('ok', true, 'operator', 'noop') || v_ctx;
    ELSIF v_op = 'sql' THEN
        v_sql := p_action->>'sql';
        IF v_sql IS NULL OR btrim(v_sql) = '' THEN
            RAISE EXCEPTION 'sql action: action_spec.sql is empty';
        END IF;
        EXECUTE v_sql USING v_ctx;   -- the action SQL references the context as $1 (jsonb)
        RETURN jsonb_build_object('ok', true, 'operator', 'sql');
    ELSIF v_op = 'mcp_call' THEN
        IF coalesce(p_action->>'server', '') = '' OR coalesce(p_action->>'tool', '') = '' THEN
            RAISE EXCEPTION 'mcp_call action: server and tool are required';
        END IF;
        v_args := rvbbit._alert_render_args(coalesce(p_action->'args', '{}'::jsonb), v_ctx);
        IF jsonb_typeof(v_args) <> 'object' THEN
            RAISE EXCEPTION 'mcp_call action: rendered args must be a JSON object';
        END IF;
        RETURN jsonb_build_object('ok', true, 'operator', 'mcp_call',
            'result', rvbbit.mcp_call(p_action->>'server', p_action->>'tool', v_args));
    ELSIF v_op = 'operator' THEN
        -- Invoke a catalogued operator by name with rendered, typed positional
        -- args (arg_names order). Calling rvbbit.<op>() runs through the operator
        -- wrapper, so receipts/observability are captured for free.
        DECLARE
            v_opname    text := p_action->>'operator_name';
            v_arg_names text[];
            v_arg_types text[];
            v_parts     text[] := '{}';
            v_call      text;
            v_oresult   jsonb;
            v_type      regtype;
            i           integer;
        BEGIN
            IF coalesce(v_opname, '') = '' THEN
                RAISE EXCEPTION 'operator action: action_spec.operator_name is required';
            END IF;
            SELECT arg_names, arg_types INTO v_arg_names, v_arg_types
              FROM rvbbit.operators WHERE name = v_opname;
            IF v_arg_names IS NULL THEN
                RAISE EXCEPTION 'operator action: unknown operator %', v_opname;
            END IF;
            v_args := rvbbit._alert_render_args(coalesce(p_action->'args', '{}'::jsonb), v_ctx);
            FOR i IN 1 .. coalesce(array_length(v_arg_names, 1), 0) LOOP
                -- validate the catalog-declared type is a real type (so it can't
                -- smuggle SQL into the cast) and use its canonical, safe name
                v_type := to_regtype(coalesce(v_arg_types[i], 'text'));
                IF v_type IS NULL THEN
                    RAISE EXCEPTION 'operator action: operator % has an invalid arg type %', v_opname, v_arg_types[i];
                END IF;
                v_parts := v_parts || (quote_nullable(v_args ->> v_arg_names[i]) || '::' || v_type::text);
            END LOOP;
            v_call := format('SELECT to_jsonb(rvbbit.%I(%s)) AS r',
                             v_opname, array_to_string(v_parts, ', '));
            EXECUTE v_call INTO v_oresult;
            RETURN jsonb_build_object('ok', true, 'operator', 'operator',
                'name', v_opname, 'result', v_oresult);
        END;
    ELSIF v_op = 'flow' THEN
        v_spec := rvbbit._alert_interpolate(coalesce(p_action->>'spec', ''), v_ctx);
        IF btrim(v_spec) = '' THEN
            RAISE EXCEPTION 'flow action: action_spec.spec is empty';
        END IF;
        PERFORM rvbbit.flow(v_spec);
        RETURN jsonb_build_object('ok', true, 'operator', 'flow');
    ELSE
        RAISE EXCEPTION 'rvbbit._alert_dispatch: unknown operator %', v_op;
    END IF;
END;
$fn$;

-- Drain up to p_max pending queue items: dispatch each action, log to
-- alert_events, mark the item done/failed. FOR UPDATE SKIP LOCKED so multiple
-- workers don't collide. Fire-and-forget (no retry) for v1; the kill-switch
-- (alerts_enabled) is the "stop acting" stage. Each item runs in its own
-- subtransaction so one failing action can't abort the whole drain.
CREATE OR REPLACE FUNCTION rvbbit.alert_worker_tick(p_max integer DEFAULT 50)
RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_item   record;
    v_action jsonb;
    v_out    jsonb;
    v_done   integer := 0;
    v_failed integer := 0;
BEGIN
    IF NOT rvbbit.alerts_enabled() THEN
        RETURN jsonb_build_object('skipped', true, 'reason', 'alerts_disabled');
    END IF;

    FOR v_item IN
        SELECT queue_id, rule_name, entity_key, transition
        FROM rvbbit.alert_queue
        WHERE status = 'pending'
        ORDER BY enqueued_at
        LIMIT greatest(p_max, 1)
        FOR UPDATE SKIP LOCKED
    LOOP
        BEGIN
            SELECT action_spec INTO v_action FROM rvbbit.alert_catalog WHERE name = v_item.rule_name;
            IF v_action IS NULL THEN
                RAISE EXCEPTION 'alert %: no current rule definition', v_item.rule_name;
            END IF;
            v_out := rvbbit._alert_dispatch(v_item.rule_name, v_item.entity_key, v_item.transition, v_action);
            UPDATE rvbbit.alert_queue SET status = 'done', attempts = attempts + 1 WHERE queue_id = v_item.queue_id;
            INSERT INTO rvbbit.alert_events (rule_name, entity_key, transition, action_output, status)
            VALUES (v_item.rule_name, v_item.entity_key, v_item.transition, v_out, 'fired');
            v_done := v_done + 1;
        EXCEPTION WHEN OTHERS THEN
            UPDATE rvbbit.alert_queue SET status = 'failed', attempts = attempts + 1 WHERE queue_id = v_item.queue_id;
            INSERT INTO rvbbit.alert_events (rule_name, entity_key, transition, status, error)
            VALUES (v_item.rule_name, v_item.entity_key, v_item.transition, 'failed', SQLERRM);
            v_failed := v_failed + 1;
        END;
    END LOOP;

    RETURN jsonb_build_object('done', v_done, 'failed', v_failed);
END;
$fn$;

-- ---- P4: pg_cron tier wiring + kill-switch ---------------------------------
-- Register the sweep (one job per cadence tier) + the worker as pg_cron jobs.
-- p_dry_run returns the plan without scheduling (no pg_cron needed) — that's
-- the deterministic test path and a safe "what would this do?" preview. Mirrors
-- rvbbit.schedule_materialize_tick's home-db + pg_cron-exists guards. The kill-
-- switch (rvbbit.alerts_enabled / set_alerts_enabled) lets the jobs keep firing
-- cheaply while doing nothing; alerts_uninstall_cron() stops them entirely.
CREATE OR REPLACE FUNCTION rvbbit.alerts_install_cron(
    p_fast       text    DEFAULT '* * * * *',
    p_normal     text    DEFAULT '*/15 * * * *',
    p_slow       text    DEFAULT '0 * * * *',
    p_worker     text    DEFAULT '* * * * *',
    p_worker_max integer DEFAULT 50,
    p_dry_run    boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_cron_home text := current_setting('cron.database_name', true);
    v_this_db   text := current_database();
    v_jobs      jsonb := '[]'::jsonb;
    v_spec      record;
    v_jobid     bigint;
BEGIN
    FOR v_spec IN
        SELECT * FROM (VALUES
            ('rvbbit_alert_sweep_fast',   p_fast,   format('SELECT rvbbit.alert_sweep(%L)', 'fast')),
            ('rvbbit_alert_sweep_normal', p_normal, format('SELECT rvbbit.alert_sweep(%L)', 'normal')),
            ('rvbbit_alert_sweep_slow',   p_slow,   format('SELECT rvbbit.alert_sweep(%L)', 'slow')),
            ('rvbbit_alert_worker',       p_worker, format('SELECT rvbbit.alert_worker_tick(%s)', p_worker_max))
        ) AS t(job_name, schedule, command)
    LOOP
        IF p_dry_run THEN
            v_jobs := v_jobs || jsonb_build_object('name', v_spec.job_name,
                'schedule', v_spec.schedule, 'command', v_spec.command);
            CONTINUE;
        END IF;
        IF v_cron_home IS NOT NULL AND v_cron_home <> '' AND v_cron_home <> v_this_db THEN
            RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
                v_cron_home, v_this_db
                USING HINT = format('connect to %L and use cron.schedule_in_database(..., %L)', v_cron_home, v_this_db);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
            RAISE EXCEPTION 'pg_cron is not installed; run rvbbit.alert_sweep()/alert_worker_tick() manually.';
        END IF;
        EXECUTE format('SELECT cron.schedule(%L, %L, %L)', v_spec.job_name, v_spec.schedule, v_spec.command)
            INTO v_jobid;
        v_jobs := v_jobs || jsonb_build_object('name', v_spec.job_name, 'schedule', v_spec.schedule,
            'command', v_spec.command, 'jobid', v_jobid);
    END LOOP;
    RETURN jsonb_build_object('dry_run', p_dry_run, 'jobs', v_jobs);
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.alerts_uninstall_cron() RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_name    text;
    v_removed text[] := '{}';
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RETURN jsonb_build_object('removed', '[]'::jsonb, 'note', 'pg_cron not installed');
    END IF;
    FOREACH v_name IN ARRAY ARRAY['rvbbit_alert_sweep_fast', 'rvbbit_alert_sweep_normal',
                                  'rvbbit_alert_sweep_slow', 'rvbbit_alert_worker'] LOOP
        BEGIN
            EXECUTE format('SELECT cron.unschedule(%L)', v_name);
            v_removed := v_removed || v_name;
        EXCEPTION WHEN OTHERS THEN
            NULL;  -- wasn't scheduled
        END;
    END LOOP;
    RETURN jsonb_build_object('removed', to_jsonb(v_removed));
END;
$fn$;
"#,
    name = "rvbbit_alerts",
    requires = ["rvbbit_bootstrap", "rvbbit_metrics"],
);

// ---- Cross-cutting org taxonomy (metrics + alerts share one category tree) ----
// A mutable, per-entity 2-level category that lives APART from the versioned defs,
// so re-categorizing is an in-place "move to a folder" — not a new definition
// version. Both catalogs LEFT JOIN it, so the taxonomy + lookup are unified.
extension_sql!(
    r#"
CREATE TABLE IF NOT EXISTS rvbbit.entity_categories (
    entity_kind  text NOT NULL,            -- 'metric' | 'alert'
    entity_name  text NOT NULL,
    category     text,
    subcategory  text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_kind, entity_name),
    CONSTRAINT entity_categories_subcat_requires_cat
        CHECK (subcategory IS NULL OR category IS NOT NULL)
);

-- Set (or clear) an entity's category. Empty strings normalize to NULL; clearing
-- the category removes the row. A subcategory requires a category.
CREATE OR REPLACE FUNCTION rvbbit.set_category(
    p_kind text, p_name text, p_category text DEFAULT NULL, p_subcategory text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql AS $fn$
DECLARE
    v_cat text := nullif(btrim(coalesce(p_category, '')), '');
    v_sub text := nullif(btrim(coalesce(p_subcategory, '')), '');
BEGIN
    IF p_kind NOT IN ('metric', 'alert') THEN
        RAISE EXCEPTION 'rvbbit.set_category: kind must be metric or alert (got %)', p_kind;
    END IF;
    IF v_sub IS NOT NULL AND v_cat IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_category: subcategory requires a category';
    END IF;
    IF v_cat IS NULL THEN
        DELETE FROM rvbbit.entity_categories WHERE entity_kind = p_kind AND entity_name = p_name;
        RETURN;
    END IF;
    INSERT INTO rvbbit.entity_categories (entity_kind, entity_name, category, subcategory, updated_at)
    VALUES (p_kind, p_name, v_cat, v_sub, now())
    ON CONFLICT (entity_kind, entity_name) DO UPDATE
       SET category = EXCLUDED.category, subcategory = EXCLUDED.subcategory, updated_at = now();
END;
$fn$;

-- Distinct (category, subcategory) pairs in use — the reusable lookup. Unified
-- across kinds by default (pass a kind to scope it).
CREATE OR REPLACE FUNCTION rvbbit.category_options(p_kind text DEFAULT NULL)
RETURNS TABLE(category text, subcategory text)
LANGUAGE sql STABLE AS $fn$
    SELECT DISTINCT category, subcategory
    FROM rvbbit.entity_categories
    WHERE category IS NOT NULL
      AND (p_kind IS NULL OR entity_kind = p_kind)
    ORDER BY category, subcategory NULLS FIRST;
$fn$;

-- Re-project both catalogs with the joined category (append-only columns, so
-- CREATE OR REPLACE VIEW is allowed).
CREATE OR REPLACE VIEW rvbbit.metric_catalog AS
SELECT DISTINCT ON (m.name)
    m.name, m.version, m.sql, m.params, m.grain, m.description, m.owner, m.labels, m.check_sql, m.created_at,
    ec.category, ec.subcategory
FROM rvbbit.metric_defs m
LEFT JOIN rvbbit.entity_categories ec ON ec.entity_kind = 'metric' AND ec.entity_name = m.name
ORDER BY m.name, m.created_at DESC, m.version DESC;

CREATE OR REPLACE VIEW rvbbit.alert_catalog AS
SELECT DISTINCT ON (r.name)
    r.name, r.version, r.condition_spec, r.fire_policy, r.action_spec,
    r.cardinality, r.fan_out_cap, r.description, r.owner, r.labels, r.created_at,
    coalesce(c.enabled, true)                             AS enabled,
    c.muted_until,
    (c.muted_until IS NOT NULL AND c.muted_until > now()) AS muted,
    coalesce(c.cadence_tier, 'normal')                    AS cadence_tier,
    ec.category, ec.subcategory
FROM rvbbit.alert_rules r
LEFT JOIN rvbbit.alert_control c ON c.name = r.name
LEFT JOIN rvbbit.entity_categories ec ON ec.entity_kind = 'alert' AND ec.entity_name = r.name
ORDER BY r.name, r.created_at DESC, r.version DESC;
"#,
    name = "rvbbit_categories",
    requires = ["rvbbit_bootstrap", "rvbbit_metrics", "rvbbit_alerts"],
);

const CAPABILITY_CATALOG_SEED: &str = include_str!("capability_catalog_seed.json");

fn seed_sql_lit(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn seed_jsonb_sql(value: &Value) -> String {
    let text = serde_json::to_string(value).unwrap_or_else(|err| {
        pgrx::error!("rvbbit.seed_capability_catalog: JSON encode failed: {err}")
    });
    format!("{}::jsonb", seed_sql_lit(&text))
}

fn seed_text_array_sql(values: &[String]) -> String {
    let inner = values
        .iter()
        .map(|value| seed_sql_lit(value))
        .collect::<Vec<_>>()
        .join(",");
    format!("ARRAY[{inner}]::text[]")
}

#[pg_extern(volatile)]
fn seed_capability_catalog() -> JsonB {
    let doc: Value = serde_json::from_str(CAPABILITY_CATALOG_SEED).unwrap_or_else(|err| {
        pgrx::error!("rvbbit.seed_capability_catalog: seed JSON is invalid: {err}")
    });
    let source = doc
        .get("catalog_source")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("curated")
        .to_string();
    let Some(entries) = doc.get("capabilities").and_then(Value::as_array) else {
        pgrx::error!("rvbbit.seed_capability_catalog: seed JSON missing capabilities array");
    };

    let mut keep_ids = Vec::with_capacity(entries.len());
    let mut prepared_rows = Vec::with_capacity(entries.len());
    for entry in entries {
        let Some(catalog_entry) = entry.get("catalog_entry").filter(|value| value.is_object())
        else {
            pgrx::error!("rvbbit.seed_capability_catalog: seed entry missing catalog_entry object");
        };
        let Some(capability_manifest) = entry
            .get("capability_manifest")
            .filter(|value| value.is_object())
        else {
            pgrx::error!(
                "rvbbit.seed_capability_catalog: seed entry missing capability_manifest object"
            );
        };
        let Some(id) = catalog_entry.get("id").and_then(Value::as_str) else {
            pgrx::error!("rvbbit.seed_capability_catalog: seed entry missing catalog id");
        };
        keep_ids.push(id.to_string());
        prepared_rows.push((catalog_entry, capability_manifest));
    }

    let result = Spi::connect_mut(|client| -> Result<(usize, i32), pgrx::spi::Error> {
        for (catalog_entry, capability_manifest) in &prepared_rows {
            let sql = format!(
                "SELECT rvbbit.upsert_capability_catalog_entry(\
                    catalog_entry => {}, \
                    capability_manifest => {}, \
                    catalog_source => {}, \
                    entry_active => true)",
                seed_jsonb_sql(catalog_entry),
                seed_jsonb_sql(capability_manifest),
                seed_sql_lit(&source),
            );
            client.update(&sql, None, &[])?;
        }

        let prune_sql = format!(
            "SELECT rvbbit.prune_capability_catalog(catalog_source => {}, keep_ids => {})",
            seed_sql_lit(&source),
            seed_text_array_sql(&keep_ids),
        );
        let pruned = client
            .update(&prune_sql, Some(1), &[])?
            .first()
            .get_one::<i32>()?
            .unwrap_or(0);
        Ok((prepared_rows.len(), pruned))
    })
    .unwrap_or_else(|err| pgrx::error!("rvbbit.seed_capability_catalog: {err}"));

    JsonB(json!({
        "ok": true,
        "catalog_source": source,
        "seeded": result.0,
        "pruned": result.1,
    }))
}

extension_sql!(
    r#"
SELECT rvbbit.seed_capability_catalog();
"#,
    name = "seed_capability_catalog_on_install",
    requires = ["rvbbit_bootstrap", seed_capability_catalog],
);

// ===========================================================================
// Alerts P0 — define_alert round-trip + versioning + control + kill-switch.
// ===========================================================================
#[cfg(any(test, feature = "pg_test"))]
#[pgrx::pg_schema]
mod tests {
    use pgrx::prelude::*;

    #[pg_test]
    fn define_alert_roundtrip_versioning_and_control() {
        // First definition returns version 1.
        let v1: i32 = Spi::get_one(
            "SELECT rvbbit.define_alert('rev_drop', \
             '{\"kind\":\"sql\",\"metric_name\":\"daily_revenue\"}'::jsonb, \
             '{\"operator\":\"mcp_call\",\"server\":\"linear\"}'::jsonb)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(v1, 1, "first define_alert returns version 1");

        // Re-defining the same name bumps the version.
        let v2: i32 = Spi::get_one(
            "SELECT rvbbit.define_alert('rev_drop', '{\"kind\":\"sql\"}'::jsonb, '{\"operator\":\"x\"}'::jsonb)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(v2, 2, "re-define bumps version");

        // alert_catalog exposes the LATEST version + default runtime flags.
        let cat_version: i32 =
            Spi::get_one("SELECT version FROM rvbbit.alert_catalog WHERE name = 'rev_drop'")
                .unwrap()
                .unwrap();
        assert_eq!(cat_version, 2, "catalog shows the latest version");
        let enabled: bool =
            Spi::get_one("SELECT enabled FROM rvbbit.alert_catalog WHERE name = 'rev_drop'")
                .unwrap()
                .unwrap();
        assert!(enabled, "a new alert is enabled by default");
        let tier: String =
            Spi::get_one("SELECT cadence_tier FROM rvbbit.alert_catalog WHERE name = 'rev_drop'")
                .unwrap()
                .unwrap();
        assert_eq!(tier, "normal", "default cadence is normal");

        // disable -> reflected in the catalog.
        Spi::run("SELECT rvbbit.disable_alert('rev_drop')").unwrap();
        let enabled: bool =
            Spi::get_one("SELECT enabled FROM rvbbit.alert_catalog WHERE name = 'rev_drop'")
                .unwrap()
                .unwrap();
        assert!(!enabled, "disable_alert clears enabled");

        // Re-defining must PRESERVE the disabled control state (separate table).
        Spi::run(
            "SELECT rvbbit.define_alert('rev_drop', '{\"kind\":\"sql\"}'::jsonb, '{\"operator\":\"x\"}'::jsonb)",
        )
        .unwrap();
        let enabled: bool =
            Spi::get_one("SELECT enabled FROM rvbbit.alert_catalog WHERE name = 'rev_drop'")
                .unwrap()
                .unwrap();
        assert!(!enabled, "re-define preserves the disabled runtime state");

        // cadence + mute/unmute.
        Spi::run("SELECT rvbbit.set_alert_cadence('rev_drop', 'fast')").unwrap();
        let tier: String =
            Spi::get_one("SELECT cadence_tier FROM rvbbit.alert_catalog WHERE name = 'rev_drop'")
                .unwrap()
                .unwrap();
        assert_eq!(tier, "fast", "set_alert_cadence updates the tier");

        Spi::run("SELECT rvbbit.mute_alert('rev_drop', interval '1 hour')").unwrap();
        let muted: bool =
            Spi::get_one("SELECT muted FROM rvbbit.alert_catalog WHERE name = 'rev_drop'")
                .unwrap()
                .unwrap();
        assert!(muted, "mute_alert marks the alert muted");
        Spi::run("SELECT rvbbit.unmute_alert('rev_drop')").unwrap();
        let muted: bool =
            Spi::get_one("SELECT muted FROM rvbbit.alert_catalog WHERE name = 'rev_drop'")
                .unwrap()
                .unwrap();
        assert!(!muted, "unmute_alert clears muted");

        // resolve_alert returns the latest version (3 after the third define).
        let rv: i32 = Spi::get_one("SELECT r_version FROM rvbbit.resolve_alert('rev_drop')")
            .unwrap()
            .unwrap();
        assert_eq!(rv, 3, "resolve_alert returns the latest version");
    }

    #[pg_test]
    fn alerts_enabled_killswitch_toggles() {
        let on: bool = Spi::get_one("SELECT rvbbit.alerts_enabled()").unwrap().unwrap();
        assert!(on, "alerts_enabled() defaults to true");
        Spi::run("SELECT rvbbit.set_alerts_enabled(false)").unwrap();
        let off: bool = Spi::get_one("SELECT rvbbit.alerts_enabled()").unwrap().unwrap();
        assert!(!off, "the global kill-switch flips the flag");
    }

    #[pg_test(error = "rvbbit.define_alert: condition is required")]
    fn define_alert_rejects_empty_condition() {
        Spi::run("SELECT rvbbit.define_alert('bad', '{}'::jsonb, '{\"operator\":\"x\"}'::jsonb)")
            .unwrap();
    }

    // ---- P1: reconciler (alert_sweep) ----
    fn qcount(rule: &str) -> i64 {
        Spi::get_one(&format!(
            "SELECT count(*)::bigint FROM rvbbit.alert_queue WHERE rule_name = '{rule}'"
        ))
        .unwrap()
        .unwrap()
    }

    #[pg_test]
    fn alert_sweep_edge_triggers_and_rearms() {
        Spi::run("CREATE TABLE _aobs (entity_key text, status text)").unwrap();
        Spi::run("INSERT INTO _aobs VALUES ('', 'pass')").unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('s1', \
             '{\"kind\":\"sql\",\"query\":\"SELECT entity_key, status FROM _aobs\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb)",
        )
        .unwrap();

        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s1"), 0, "pass does not enqueue");

        Spi::run("UPDATE _aobs SET status='fail'").unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s1"), 1, "enter_fail enqueues once");

        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s1"), 1, "sustained fail does not re-fire");

        Spi::run("UPDATE _aobs SET status='pass'").unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        Spi::run("UPDATE _aobs SET status='fail'").unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s1"), 2, "re-arms after recovery");
    }

    #[pg_test]
    fn alert_sweep_consecutive_n_hysteresis() {
        Spi::run("CREATE TABLE _aobs (entity_key text, status text)").unwrap();
        Spi::run("INSERT INTO _aobs VALUES ('', 'fail')").unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('s2', \
             '{\"kind\":\"sql\",\"query\":\"SELECT entity_key, status FROM _aobs\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb, p_fire_policy => '{\"consecutive_n\":3}'::jsonb)",
        )
        .unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s2"), 0, "no fire before consecutive_n");
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s2"), 1, "fires on the Nth consecutive fail");
    }

    #[pg_test]
    fn alert_sweep_fan_out_cap_drains_over_sweeps() {
        Spi::run("CREATE TABLE _aobs (entity_key text, status text)").unwrap();
        Spi::run("INSERT INTO _aobs SELECT 'e'||g, 'fail' FROM generate_series(1,5) g").unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('s3', \
             '{\"kind\":\"sql\",\"query\":\"SELECT entity_key, status FROM _aobs\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb, p_fan_out_cap => 2)",
        )
        .unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s3"), 2, "cap=2 enqueues 2 on the first sweep");
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s3"), 4, "remaining drain on the next sweep");
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("s3"), 5, "all 5 enqueued, nothing dropped");
    }

    // ---- P2: worker (alert_worker_tick) ----
    #[pg_test]
    fn alert_worker_drains_queue_logs_events_and_is_idempotent() {
        Spi::run(
            "SELECT rvbbit.define_alert('w1', '{\"kind\":\"sql\",\"query\":\"SELECT 1\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb)",
        )
        .unwrap();
        Spi::run("INSERT INTO rvbbit.alert_queue (rule_name, entity_key, transition) VALUES ('w1','','enter_fail')")
            .unwrap();

        Spi::run("SELECT rvbbit.alert_worker_tick(50)").unwrap();
        let done: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_queue WHERE rule_name='w1' AND status='done'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(done, 1, "worker marks the item done");
        let pending: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_queue WHERE rule_name='w1' AND status='pending'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(pending, 0, "no pending remains");
        let fired: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_events WHERE rule_name='w1' AND status='fired'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(fired, 1, "a fired event is logged");

        // idempotent: a second tick has nothing to drain
        Spi::run("SELECT rvbbit.alert_worker_tick(50)").unwrap();
        let events: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_events WHERE rule_name='w1'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(events, 1, "drained items are not re-processed");
    }

    #[pg_test]
    fn alert_worker_killswitch_holds_and_logs_failures() {
        // kill-switch off → worker no-ops, the item stays pending
        Spi::run(
            "SELECT rvbbit.define_alert('k1', '{\"kind\":\"sql\",\"query\":\"SELECT 1\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb)",
        )
        .unwrap();
        Spi::run("INSERT INTO rvbbit.alert_queue (rule_name, entity_key, transition) VALUES ('k1','','enter_fail')")
            .unwrap();
        Spi::run("SELECT rvbbit.set_alerts_enabled(false)").unwrap();
        Spi::run("SELECT rvbbit.alert_worker_tick(50)").unwrap();
        let pending: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_queue WHERE rule_name='k1' AND status='pending'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(pending, 1, "kill-switch off: worker does not drain");
        Spi::run("SELECT rvbbit.set_alerts_enabled(true)").unwrap();

        // an operator not wired until P3 → failed + logged, no crash
        Spi::run(
            "SELECT rvbbit.define_alert('f1', '{\"kind\":\"sql\",\"query\":\"SELECT 1\"}'::jsonb, \
             '{\"operator\":\"mcp_call\",\"server\":\"linear\"}'::jsonb)",
        )
        .unwrap();
        Spi::run("INSERT INTO rvbbit.alert_queue (rule_name, entity_key, transition) VALUES ('f1','','enter_fail')")
            .unwrap();
        Spi::run("SELECT rvbbit.alert_worker_tick(50)").unwrap();
        let failed: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_queue WHERE rule_name='f1' AND status='failed'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(failed, 1, "unsupported operator marks the item failed");
        let logged: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_events \
             WHERE rule_name='f1' AND status='failed' AND error IS NOT NULL",
        )
        .unwrap()
        .unwrap();
        assert_eq!(logged, 1, "the failure is logged with an error");
    }

    // ---- P3: action arg-binding + real dispatch (sql / noop) ----
    #[pg_test]
    fn alert_render_args_substitutes_placeholders() {
        let title: String = Spi::get_one(
            "SELECT rvbbit._alert_render_args('{\"title\":\"{rule} breached for {entity}\"}'::jsonb, \
             '{\"rule\":\"rev\",\"entity\":\"US\"}'::jsonb) ->> 'title'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(title, "rev breached for US", "embedded placeholders interpolate");

        let typ: String = Spi::get_one(
            "SELECT jsonb_typeof(rvbbit._alert_render_args('{\"n\":\"{count}\"}'::jsonb, \
             '{\"count\":42}'::jsonb) -> 'n')",
        )
        .unwrap()
        .unwrap();
        assert_eq!(typ, "number", "a whole-string placeholder keeps the typed value");
    }

    #[pg_test]
    fn alert_worker_runs_sql_action_with_context() {
        Spi::run("CREATE TABLE _hits (r text, e text)").unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('sa', '{\"kind\":\"sql\",\"query\":\"SELECT 1\"}'::jsonb, \
             '{\"operator\":\"sql\",\"sql\":\"INSERT INTO _hits(r,e) VALUES ($1->>''rule'', $1->>''entity'')\"}'::jsonb)",
        )
        .unwrap();
        Spi::run("INSERT INTO rvbbit.alert_queue (rule_name, entity_key, transition) VALUES ('sa','US','enter_fail')")
            .unwrap();
        Spi::run("SELECT rvbbit.alert_worker_tick(50)").unwrap();

        let hits: i64 = Spi::get_one("SELECT count(*)::bigint FROM _hits WHERE r='sa' AND e='US'")
            .unwrap()
            .unwrap();
        assert_eq!(hits, 1, "sql action runs with the alert context bound to $1");
        let done: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_queue WHERE rule_name='sa' AND status='done'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(done, 1, "the sql-action item is marked done");
        let fired: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.alert_events WHERE rule_name='sa' AND status='fired'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(fired, 1, "a fired event is logged");
    }

    // ---- P4: pg_cron tier wiring ----
    #[pg_test]
    fn alerts_install_cron_dry_run_plans_tiers_and_worker() {
        let n: i64 = Spi::get_one(
            "SELECT jsonb_array_length(rvbbit.alerts_install_cron(p_dry_run => true) -> 'jobs')::bigint",
        )
        .unwrap()
        .unwrap();
        assert_eq!(n, 4, "plans 3 sweep tiers + 1 worker");

        let worker_ok: bool = Spi::get_one(
            "SELECT EXISTS (SELECT 1 FROM jsonb_array_elements(\
             rvbbit.alerts_install_cron(p_dry_run => true) -> 'jobs') j \
             WHERE j->>'name' = 'rvbbit_alert_worker' AND j->>'command' LIKE '%alert_worker_tick%')",
        )
        .unwrap()
        .unwrap();
        assert!(worker_ok, "worker job is planned with the right command");

        let custom_ok: bool = Spi::get_one(
            "SELECT EXISTS (SELECT 1 FROM jsonb_array_elements(\
             rvbbit.alerts_install_cron(p_fast => '*/2 * * * *', p_dry_run => true) -> 'jobs') j \
             WHERE j->>'name' = 'rvbbit_alert_sweep_fast' AND j->>'schedule' = '*/2 * * * *')",
        )
        .unwrap()
        .unwrap();
        assert!(custom_ok, "a custom schedule flows through to the plan");
    }

    #[pg_test(error = "pg_cron is not installed; run rvbbit.alert_sweep()/alert_worker_tick() manually.")]
    fn alerts_install_cron_requires_pg_cron_to_schedule() {
        // the test harness has no pg_cron, so the live (non-dry-run) path errors clearly
        Spi::run("SELECT rvbbit.alerts_install_cron()").unwrap();
    }

    // ---- P5: scored / semantic conditions (score + threshold -> status) ----
    #[pg_test]
    fn alert_sweep_scored_condition_thresholds_to_status() {
        Spi::run("CREATE TABLE _sc (entity_key text, score numeric)").unwrap();
        Spi::run("INSERT INTO _sc VALUES ('US', 0.95)").unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('sc_hi', \
             '{\"kind\":\"sql\",\"query\":\"SELECT entity_key, score FROM _sc\",\"threshold\":0.8,\"compare\":\"gte\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb)",
        )
        .unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("sc_hi"), 1, "a score above the gte threshold breaches and fires");

        let s: f64 = Spi::get_one(
            "SELECT score::float8 FROM rvbbit.alert_state WHERE rule_name='sc_hi' AND entity_key='US'",
        )
        .unwrap()
        .unwrap();
        assert!((s - 0.95).abs() < 1e-9, "the numeric score is recorded in alert_state");

        // below threshold → no fire
        Spi::run("CREATE TABLE _sc2 (entity_key text, score numeric)").unwrap();
        Spi::run("INSERT INTO _sc2 VALUES ('EU', 0.5)").unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('sc_lo', \
             '{\"kind\":\"sql\",\"query\":\"SELECT entity_key, score FROM _sc2\",\"threshold\":0.8,\"compare\":\"gte\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb)",
        )
        .unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("sc_lo"), 0, "a score below the gte threshold does not fire");
    }

    #[pg_test]
    fn alert_sweep_scored_lte_breaches_on_low_score() {
        Spi::run("CREATE TABLE _sc (entity_key text, score numeric)").unwrap();
        Spi::run("INSERT INTO _sc VALUES ('svc', 0.2)").unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('sc_lte', \
             '{\"kind\":\"sql\",\"query\":\"SELECT entity_key, score FROM _sc\",\"threshold\":0.5,\"compare\":\"lte\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb)",
        )
        .unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        assert_eq!(qcount("sc_lte"), 1, "a low score breaches an lte threshold (e.g. health dropping)");
    }

    #[pg_test]
    fn delete_alert_removes_rule_and_all_state() {
        Spi::run(
            "SELECT rvbbit.define_alert('d1', '{\"kind\":\"sql\",\"query\":\"SELECT 1\"}'::jsonb, '{\"operator\":\"noop\"}'::jsonb)",
        )
        .unwrap();
        Spi::run("INSERT INTO rvbbit.alert_state (rule_name, entity_key, last_status) VALUES ('d1','x','fail')").unwrap();
        Spi::run("INSERT INTO rvbbit.alert_events (rule_name) VALUES ('d1')").unwrap();

        let existed: bool = Spi::get_one("SELECT rvbbit.delete_alert('d1')").unwrap().unwrap();
        assert!(existed, "delete returns true when the rule existed");
        let rules: i64 = Spi::get_one("SELECT count(*)::bigint FROM rvbbit.alert_catalog WHERE name='d1'")
            .unwrap()
            .unwrap();
        assert_eq!(rules, 0, "the rule is gone from the catalog");
        let state: i64 = Spi::get_one("SELECT count(*)::bigint FROM rvbbit.alert_state WHERE rule_name='d1'")
            .unwrap()
            .unwrap();
        assert_eq!(state, 0, "its per-entity state is gone");
        let again: bool = Spi::get_one("SELECT rvbbit.delete_alert('d1')").unwrap().unwrap();
        assert!(!again, "delete returns false for a missing rule");
    }

    #[pg_test]
    fn alert_metric_condition_rides_the_verdict() {
        // a metric-ref condition reads the referenced metric's latest KPI verdict
        Spi::run(
            "INSERT INTO rvbbit.metric_observations (metric_name, data_as_of, status) \
             VALUES ('mc_fail', now(), 'fail'), ('mc_pass', now(), 'pass')",
        )
        .unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('mc_a', '{\"kind\":\"metric\",\"metric\":\"mc_fail\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb, '{\"consecutive_n\":1}'::jsonb)",
        )
        .unwrap();
        Spi::run(
            "SELECT rvbbit.define_alert('mc_b', '{\"kind\":\"metric\",\"metric\":\"mc_pass\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb, '{\"consecutive_n\":1}'::jsonb)",
        )
        .unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        let fail_st: String = Spi::get_one("SELECT last_status FROM rvbbit.alert_state WHERE rule_name='mc_a'")
            .unwrap()
            .unwrap();
        assert_eq!(fail_st, "fail", "a metric with a 'fail' verdict drives the alert to fail");
        let pass_st: String = Spi::get_one("SELECT last_status FROM rvbbit.alert_state WHERE rule_name='mc_b'")
            .unwrap()
            .unwrap();
        assert_eq!(pass_st, "pass", "a metric with a 'pass' verdict stays passing");
    }

    #[pg_test]
    fn alert_operator_action_invokes_the_operator() {
        // a stand-in operator (plain SQL fn + a catalog row) exercises the dispatch
        // plumbing — rendered typed positional args — without a live model call
        Spi::run("CREATE OR REPLACE FUNCTION rvbbit._t_echo(a text) RETURNS jsonb LANGUAGE sql AS $$ SELECT jsonb_build_object('got', a) $$")
            .unwrap();
        Spi::run(
            "INSERT INTO rvbbit.operators (name, shape, arg_names, arg_types, return_type, model, system_prompt, user_prompt, parser) \
             VALUES ('_t_echo','scalar','{a}','{text}','jsonb','x','x','x','raw_text')",
        )
        .unwrap();
        let got: String = Spi::get_one(
            "SELECT rvbbit._alert_dispatch('r','APAC','enter_fail', \
             '{\"operator\":\"operator\",\"operator_name\":\"_t_echo\",\"args\":{\"a\":\"{entity}\"}}'::jsonb)->'result'->>'got'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(got, "APAC", "the operator ran with the entity-rendered arg");
    }

    #[pg_test(error = "operator action: unknown operator nope")]
    fn alert_operator_action_unknown_operator_errors() {
        Spi::run(
            "SELECT rvbbit._alert_dispatch('r','e','enter_fail', \
             '{\"operator\":\"operator\",\"operator_name\":\"nope\"}'::jsonb)",
        )
        .unwrap();
    }

    #[pg_test(error = "operator action: operator _t_badtype has an invalid arg type wat")]
    fn alert_operator_action_rejects_invalid_arg_type() {
        // a hostile/garbage arg_type in the catalog must be rejected, not concatenated
        Spi::run("CREATE OR REPLACE FUNCTION rvbbit._t_badtype(a text) RETURNS jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb $$")
            .unwrap();
        Spi::run(
            "INSERT INTO rvbbit.operators (name, shape, arg_names, arg_types, return_type, model, system_prompt, user_prompt, parser) \
             VALUES ('_t_badtype','scalar','{a}','{wat}','jsonb','x','x','x','raw_text')",
        )
        .unwrap();
        Spi::run(
            "SELECT rvbbit._alert_dispatch('r','e','enter_fail', \
             '{\"operator\":\"operator\",\"operator_name\":\"_t_badtype\",\"args\":{\"a\":\"x\"}}'::jsonb)",
        )
        .unwrap();
    }

    #[pg_test]
    fn alert_metric_condition_without_name_is_an_error_not_silent() {
        // bypass define_alert's guard by inserting a malformed rule directly; the
        // sweep must surface it as an error (caught per-rule), not a silent no-op
        Spi::run(
            "INSERT INTO rvbbit.alert_rules (name, version, condition_spec, fire_policy, action_spec, cardinality, fan_out_cap) \
             VALUES ('mc_bad', 1, '{\"kind\":\"metric\"}'::jsonb, '{}'::jsonb, '{\"operator\":\"noop\"}'::jsonb, 'per_entity', 100)",
        )
        .unwrap();
        Spi::run("INSERT INTO rvbbit.alert_control (name, cadence_tier) VALUES ('mc_bad','normal')").unwrap();
        let errors: i32 = Spi::get_one("SELECT (rvbbit.alert_sweep('normal')->>'errors')::int")
            .unwrap()
            .unwrap();
        assert!(errors >= 1, "a metric condition with no metric name surfaces as a sweep error, not a silent stale");
    }

    #[pg_test]
    fn alert_sql_expr_condition_drives_status() {
        // a boolean expression over the query's columns decides fail/pass per row
        Spi::run(
            "SELECT rvbbit.define_alert('ex1', \
             '{\"kind\":\"sql\",\"query\":\"SELECT region AS entity_key, drop_pct FROM (VALUES (''US'',0.25),(''EU'',0.05)) v(region,drop_pct)\",\"expr\":\"drop_pct > 0.15\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb, '{\"consecutive_n\":1}'::jsonb)",
        )
        .unwrap();
        Spi::run("SELECT rvbbit.alert_sweep('normal')").unwrap();
        let us: String = Spi::get_one("SELECT last_status FROM rvbbit.alert_state WHERE rule_name='ex1' AND entity_key='US'")
            .unwrap()
            .unwrap();
        assert_eq!(us, "fail", "US drop_pct 0.25 > 0.15 → the expr is true → fail");
        let eu: String = Spi::get_one("SELECT last_status FROM rvbbit.alert_state WHERE rule_name='ex1' AND entity_key='EU'")
            .unwrap()
            .unwrap();
        assert_eq!(eu, "pass", "EU drop_pct 0.05 is not > 0.15 → pass");
    }

    #[pg_test]
    fn alert_sql_expr_non_boolean_is_a_sweep_error() {
        // a non-boolean expr is rejected by the CASE/WHEN type check, surfacing
        // as a per-rule sweep error rather than silently mis-firing
        Spi::run(
            "SELECT rvbbit.define_alert('ex_bad', \
             '{\"kind\":\"sql\",\"query\":\"SELECT ''x'' AS entity_key, 5 AS n\",\"expr\":\"n + 1\"}'::jsonb, \
             '{\"operator\":\"noop\"}'::jsonb, '{\"consecutive_n\":1}'::jsonb)",
        )
        .unwrap();
        let errors: i32 = Spi::get_one("SELECT (rvbbit.alert_sweep('normal')->>'errors')::int")
            .unwrap()
            .unwrap();
        assert!(errors >= 1, "a non-boolean expr (n + 1) surfaces as a sweep error");
    }

    #[pg_test]
    fn entity_categories_set_clear_and_join() {
        Spi::run("SELECT rvbbit.define_metric('cat_m', 'SELECT 1 AS value')").unwrap();
        Spi::run("SELECT rvbbit.set_category('metric','cat_m','Marketing','Data Health')").unwrap();
        let cat: String = Spi::get_one("SELECT category || '/' || subcategory FROM rvbbit.metric_catalog WHERE name='cat_m'")
            .unwrap()
            .unwrap();
        assert_eq!(cat, "Marketing/Data Health", "the category joins onto the metric catalog");
        // category alone (no subcategory) is valid
        Spi::run("SELECT rvbbit.set_category('metric','cat_m','Finance')").unwrap();
        let sub: Option<String> = Spi::get_one("SELECT subcategory FROM rvbbit.metric_catalog WHERE name='cat_m'").unwrap();
        assert!(sub.is_none(), "a category without a subcategory is allowed");
        // clearing removes the assignment
        Spi::run("SELECT rvbbit.set_category('metric','cat_m', NULL)").unwrap();
        let cleared: Option<String> = Spi::get_one("SELECT category FROM rvbbit.metric_catalog WHERE name='cat_m'").unwrap();
        assert!(cleared.is_none(), "clearing nulls the category in the catalog");
        // the lookup is unified across kinds
        Spi::run("SELECT rvbbit.set_category('alert','cat_a','Ops','Latency')").unwrap();
        let opts: i64 = Spi::get_one("SELECT count(*)::bigint FROM rvbbit.category_options() WHERE category='Ops'")
            .unwrap()
            .unwrap();
        assert_eq!(opts, 1, "category_options surfaces the distinct pair across kinds");
    }

    #[pg_test(error = "rvbbit.set_category: subcategory requires a category")]
    fn set_category_subcategory_requires_category() {
        Spi::run("SELECT rvbbit.set_category('metric','x', NULL, 'orphan')").unwrap();
    }
}
