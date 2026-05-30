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

use pgrx::extension_sql;

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
CREATE OR REPLACE FUNCTION rvbbit.tombstone(
    reloid regclass, rg_id bigint, ordinal int
) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    gen bigint;
BEGIN
    gen := rvbbit.allocate_generation(reloid);
    INSERT INTO rvbbit.delete_log
        (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
    VALUES
        (reloid, rg_id, ordinal, pg_current_xact_id(), gen)
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

    SELECT count(*)::int INTO dropped_rgs
      FROM rvbbit.row_groups WHERE table_oid = reloid;

    -- Wipe derived state. Old on-disk parquet files are left orphaned;
    -- the rebuild uses the same path scheme (rg_id starting at 0) so the
    -- next write overwrites active names. Stale orphans are harmless because
    -- nothing in the catalog references them.
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
    CHECK (shape IN ('scalar', 'aggregate', 'dimension')),
    CHECK (cardinality(arg_names) = cardinality(arg_types)),
    CHECK (return_type IN ('bool', 'text', 'float8', 'jsonb')),
    CHECK (parser IN ('yes_no', 'score_0_1', 'raw_text', 'strip', 'json')),
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
    op_shape       text DEFAULT 'scalar',         -- scalar | aggregate | dimension
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

    -- Args list WITH trailing opts JSONB (user-facing variant).
    wrapper_args_with_opts := array_to_string(
        ARRAY(SELECT format('%I %s', a, t) FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
        ', '
    ) || ', opts jsonb DEFAULT ''{}''::jsonb';

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
            EXECUTE format(
                'CREATE OPERATOR rvbbit.%s (LEFTARG = %s, RIGHTARG = %s, FUNCTION = rvbbit.%I)',
                op_infix_symbol, actual_arg_types[1], actual_arg_types[2],
                '_op_' || op_name
            );
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
    raw_query_id text;
    next_query_id uuid;
BEGIN
    raw_query_id := NULLIF(current_setting('rvbbit.query_id', true), '');
    IF raw_query_id IS NOT NULL THEN
        BEGIN
            RETURN raw_query_id::uuid;
        EXCEPTION WHEN OTHERS THEN
            -- A bad manually-set value should not poison the session.
            NULL;
        END;
    END IF;

    next_query_id := gen_random_uuid();
    PERFORM set_config('rvbbit.query_id', next_query_id::text, false);
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
    PERFORM set_config('rvbbit.query_id', next_query_id::text, false);
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

CREATE TABLE rvbbit.warren_jobs (
    job_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL,
    desired_state    text NOT NULL DEFAULT 'running',
    name             text NOT NULL,
    manifest         jsonb NOT NULL,
    target_selector  jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'queued',
    claimed_by       text,
    claimed_at       timestamptz,
    attempts         int NOT NULL DEFAULT 0,
    endpoint_url     text,
    backend_name     text,
    operator_name    text,
    runtime_name     text,
    error            text,
    logs             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
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
    CONSTRAINT warren_jobs_manifest_is_object CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT warren_jobs_target_selector_is_object CHECK (jsonb_typeof(target_selector) = 'object'),
    CONSTRAINT warren_jobs_logs_is_object CHECK (jsonb_typeof(logs) = 'object')
);

CREATE INDEX warren_jobs_queue_idx
    ON rvbbit.warren_jobs (status, created_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX warren_jobs_target_selector_idx ON rvbbit.warren_jobs USING gin (target_selector);

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
        status IN ('starting', 'running', 'stopped', 'failed', 'removed')
    ),
    CONSTRAINT warren_deployments_manifest_is_object CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT warren_deployments_health_is_object CHECK (jsonb_typeof(health) = 'object')
);

CREATE INDEX warren_deployments_node_idx ON rvbbit.warren_deployments (node_name, status);
CREATE INDEX warren_deployments_backend_idx ON rvbbit.warren_deployments (backend_name)
    WHERE backend_name IS NOT NULL;

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
    n.status AS node_status,
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
    d.updated_at AS deployment_updated_at
FROM rvbbit.warren_nodes n
LEFT JOIN rvbbit.warren_node_latest_metrics lm
  ON lm.node_id = n.node_id
LEFT JOIN rvbbit.warren_deployments d
  ON d.node_id = n.node_id
 AND d.status IN ('starting', 'running', 'failed');

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
    ),
    picked AS (
        SELECT j.job_id
        FROM rvbbit.warren_jobs j
        CROSS JOIN node n
        WHERE j.status = 'queued'
          AND n.labels @> j.target_selector
        ORDER BY j.created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    ),
    updated AS (
        UPDATE rvbbit.warren_jobs j
        SET status = 'running',
            claimed_by = claim_warren_job.node_name,
            claimed_at = clock_timestamp(),
            started_at = COALESCE(started_at, clock_timestamp()),
            attempts = attempts + 1
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
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = complete_warren_job.node_name;

    IF actual_node_id IS NULL THEN
        RAISE EXCEPTION 'warren node % is not registered', node_name;
    END IF;

    SELECT kind, name INTO actual_kind, actual_name
    FROM rvbbit.warren_jobs
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

    IF actual_kind IS NULL THEN
        RAISE EXCEPTION 'warren job % not found', job_id;
    END IF;

    UPDATE rvbbit.warren_jobs
    SET status = 'completed',
        endpoint_url = complete_warren_job.endpoint_url,
        backend_name = complete_warren_job.backend_name,
        operator_name = complete_warren_job.operator_name,
        runtime_name = complete_warren_job.runtime_name,
        logs = complete_warren_job.logs,
        error = NULL,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

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
        error = NULL;
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
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = fail_warren_job.node_name;

    SELECT kind, name INTO actual_kind, actual_name
    FROM rvbbit.warren_jobs
    WHERE warren_jobs.job_id = fail_warren_job.job_id;

    UPDATE rvbbit.warren_jobs
    SET status = 'failed',
        error = fail_warren_job.error,
        logs = fail_warren_job.logs,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = fail_warren_job.job_id;

    IF actual_kind IS NOT NULL THEN
        INSERT INTO rvbbit.warren_deployments
            (job_id, node_id, node_name, kind, name, status, manifest, error,
             health)
        VALUES
            (fail_warren_job.job_id, actual_node_id, fail_warren_job.node_name,
             actual_kind, actual_name, 'failed', '{}'::jsonb,
             fail_warren_job.error, fail_warren_job.logs)
        ON CONFLICT ON CONSTRAINT warren_deployments_job_id_key DO UPDATE SET
            status = 'failed',
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
    SET shadow_heap_dirty = true
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
