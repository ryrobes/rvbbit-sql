-- 0265_acceleration_select_evidence -- keep ETL/COPY traffic out of acceleration advice.
--
-- Admission now requires an observed SELECT/WITH/TABLE query.  Cumulative
-- pg_stat_user_tables counters remain useful write/churn context, but they
-- cannot distinguish SELECT from COPY and therefore no longer create or score
-- candidates.  The unbounded SQL text key is also removed from the temporary
-- shape table so a long dashboard query cannot disable the activity source.
--

CREATE OR REPLACE FUNCTION rvbbit.accel_autopilot_observe(
    p_source text DEFAULT 'manual'
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $fn$
DECLARE
    cfg                     rvbbit.accel_autopilot_config%ROWTYPE;
    v_run_id                bigint;
    v_shapes_seen           bigint := 0;
    v_shapes_resolved       bigint := 0;
    v_tables_observed       bigint := 0;
    v_ready                 bigint := 0;
    v_activity_available    boolean := false;
    v_activity_error        text;
    v_error                 text;
BEGIN
    SELECT * INTO cfg
      FROM rvbbit.accel_autopilot_config
     WHERE singleton;

    IF NOT FOUND THEN
        INSERT INTO rvbbit.accel_autopilot_config (singleton) VALUES (true)
        RETURNING * INTO cfg;
    END IF;

    IF NOT pg_try_advisory_xact_lock(1381187156, 9) THEN
        RETURN jsonb_build_object(
            'status', 'busy',
            'mode', cfg.mode,
            'message', 'another workload observer run is active'
        );
    END IF;

    INSERT INTO rvbbit.accel_observer_runs (
        status, source, lookback_hours, config_snapshot
    ) VALUES (
        CASE WHEN cfg.mode = 'off' THEN 'skipped' ELSE 'running' END,
        coalesce(nullif(btrim(p_source), ''), 'manual'),
        cfg.lookback_hours,
        to_jsonb(cfg) - 'singleton'
    ) RETURNING run_id INTO v_run_id;

    IF cfg.mode = 'off' THEN
        UPDATE rvbbit.accel_observer_runs
           SET finished_at = clock_timestamp(), message = 'observer mode is off'
         WHERE run_id = v_run_id;
        RETURN jsonb_build_object(
            'status', 'skipped', 'mode', cfg.mode, 'run_id', v_run_id,
            'message', 'observer mode is off'
        );
    END IF;

    BEGIN
        DROP TABLE IF EXISTS pg_temp.rvbbit_accel_observer_shapes;
        DROP TABLE IF EXISTS pg_temp.rvbbit_accel_observer_activity;
        DROP TABLE IF EXISTS pg_temp.rvbbit_accel_observer_current;
        DROP TABLE IF EXISTS pg_temp.rvbbit_accel_observer_evidence;

        CREATE TEMP TABLE rvbbit_accel_observer_shapes (
            sql_text       text NOT NULL,
            calls          bigint NOT NULL,
            actors         text[] NOT NULL,
            active_buckets timestamptz[] NOT NULL,
            inclusive_ms   double precision NOT NULL,
            p95_ms         double precision,
            source_tables  text[] NOT NULL DEFAULT '{}'
        ) ON COMMIT DROP;

        v_activity_available :=
            to_regclass('rvbbit.mcp_activity') IS NOT NULL
            AND to_regprocedure('rvbbit._cube_source_tables(text)') IS NOT NULL;

        IF v_activity_available THEN
            BEGIN
                -- to_jsonb(a)->>'subject' keeps this compatible with old activity
                -- tables which predate the subject/actor identity columns.
                EXECUTE $sql$
                    INSERT INTO pg_temp.rvbbit_accel_observer_shapes (
                        sql_text, calls, actors, active_buckets, inclusive_ms, p95_ms
                    )
                    SELECT a.args->>'sql',
                           count(*)::bigint,
                           array_agg(DISTINCT coalesce(
                               nullif(to_jsonb(a)->>'subject', ''),
                               nullif(to_jsonb(a)->>'actor', ''),
                               nullif(a.caller, ''),
                               '?'
                           )),
                           array_agg(DISTINCT date_trunc('hour', a.ts)),
                           coalesce(sum(greatest(coalesce(a.elapsed_ms, 0), 0)), 0)::double precision,
                           percentile_cont(0.95) WITHIN GROUP (
                               ORDER BY greatest(coalesce(a.elapsed_ms, 0), 0)
                           )::double precision
                      FROM rvbbit.mcp_activity a
                     WHERE a.ok
                       AND a.tool IN ('dashboard_query', 'run_sql')
                       AND a.ts >= clock_timestamp() - make_interval(hours => $1)
                       AND nullif(btrim(a.args->>'sql'), '') IS NOT NULL
                       AND (a.args->>'sql') ~* '^[[:space:]]*(select|with|table)\M'
                     GROUP BY a.args->>'sql'
                     ORDER BY sum(greatest(coalesce(a.elapsed_ms, 0), 0)) DESC,
                              count(*) DESC
                     LIMIT $2
                $sql$ USING cfg.lookback_hours, cfg.max_query_shapes;

                SELECT count(*) INTO v_shapes_seen
                  FROM pg_temp.rvbbit_accel_observer_shapes;

                -- EXPLAIN (never ANALYZE) is isolated in the existing best-effort
                -- helper; a malformed/stale query resolves to an empty array.
                UPDATE pg_temp.rvbbit_accel_observer_shapes
                   SET source_tables = rvbbit._cube_source_tables(sql_text);

                SELECT count(*) INTO v_shapes_resolved
                  FROM pg_temp.rvbbit_accel_observer_shapes
                 WHERE cardinality(source_tables) > 0;
            EXCEPTION WHEN OTHERS THEN
                GET STACKED DIAGNOSTICS v_activity_error = MESSAGE_TEXT;
                v_activity_available := false;
                v_shapes_seen := 0;
                v_shapes_resolved := 0;
                TRUNCATE pg_temp.rvbbit_accel_observer_shapes;
            END;
        END IF;

        CREATE TEMP TABLE rvbbit_accel_observer_activity ON COMMIT DROP AS
        WITH expanded AS MATERIALIZED (
            SELECT to_regclass(src.table_name)::oid AS table_oid,
                   s.sql_text,
                   s.calls,
                   s.actors,
                   s.active_buckets,
                   s.inclusive_ms,
                   s.p95_ms,
                   greatest(cardinality(s.source_tables), 1)::double precision AS relation_count
              FROM pg_temp.rvbbit_accel_observer_shapes s
              CROSS JOIN LATERAL unnest(s.source_tables) AS src(table_name)
             WHERE to_regclass(src.table_name) IS NOT NULL
        ), metrics AS (
            SELECT table_oid,
                   count(*)::bigint AS query_shapes,
                   sum(calls)::bigint AS query_calls,
                   sum(inclusive_ms)::double precision AS inclusive_ms,
                   sum(inclusive_ms / relation_count)::double precision AS attributed_ms,
                   max(p95_ms)::double precision AS p95_ms
              FROM expanded
             GROUP BY table_oid
        ), people AS (
            SELECT e.table_oid, count(DISTINCT actor)::bigint AS users
              FROM expanded e
              CROSS JOIN LATERAL unnest(e.actors) AS actor
             GROUP BY e.table_oid
        ), hours AS (
            SELECT e.table_oid, count(DISTINCT bucket)::bigint AS active_hours
              FROM expanded e
              CROSS JOIN LATERAL unnest(e.active_buckets) AS bucket
             GROUP BY e.table_oid
        )
        SELECT m.table_oid, m.query_shapes, m.query_calls,
               coalesce(p.users, 0)::bigint AS users,
               coalesce(h.active_hours, 0)::bigint AS active_hours,
               m.inclusive_ms, m.attributed_ms, m.p95_ms
          FROM metrics m
          LEFT JOIN people p USING (table_oid)
          LEFT JOIN hours h USING (table_oid);

        CREATE TEMP TABLE rvbbit_accel_observer_current ON COMMIT DROP AS
        WITH row_group_totals AS (
            SELECT table_oid, count(*)::bigint AS row_groups
              FROM rvbbit.row_groups
             GROUP BY table_oid
        )
        SELECT c.oid AS table_oid,
               c.oid::regclass::text AS table_name,
               n.nspname AS schema_name,
               c.relkind,
               c.relpersistence,
               coalesce(am.amname, '') AS access_method,
               c.relispartition AS is_partition,
               (c.relrowsecurity OR c.relforcerowsecurity) AS row_security,
               greatest(coalesce(s.n_live_tup, 0), greatest(c.reltuples, 0))::bigint AS row_estimate,
               CASE WHEN c.relkind IN ('r', 'p', 'm')
                    THEN pg_total_relation_size(c.oid)::bigint ELSE 0::bigint END AS table_bytes,
               coalesce(s.seq_scan, 0)::bigint AS seq_scans_total,
               coalesce(s.seq_tup_read, 0)::bigint AS seq_rows_total,
               (coalesce(s.n_tup_ins, 0) + coalesce(s.n_tup_upd, 0) + coalesce(s.n_tup_del, 0))::bigint AS writes_total,
               t.table_oid IS NOT NULL AS registered,
               coalesce(t.acceleration_enabled, false) AS acceleration_enabled,
               coalesce(rg.row_groups, 0)::bigint AS row_groups,
               (
                   coalesce(t.acceleration_enabled, false)
                   AND coalesce(rg.row_groups, 0) = 0
                   AND greatest(coalesce(s.n_live_tup, 0), greatest(c.reltuples, 0)) > 0
               ) AS baseline_missing
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN pg_am am ON am.oid = c.relam
          LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
          LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
          LEFT JOIN row_group_totals rg ON rg.table_oid = c.oid
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'rvbbit')
           AND n.nspname NOT LIKE 'pg_toast%'
           AND n.nspname NOT LIKE 'pg_temp_%'
           AND n.nspname <> ALL(cfg.excluded_schemas)
           AND (
               c.relkind = 'r'
               OR EXISTS (
                   SELECT 1 FROM pg_temp.rvbbit_accel_observer_activity a
                    WHERE a.table_oid = c.oid
               )
           );

        -- OIDs can be reused after a drop. A changed regclass name is a new
        -- table and must start with a clean counter baseline.
        DELETE FROM rvbbit.accel_autopilot_candidates c
         WHERE to_regclass(c.table_name) IS NULL
            OR to_regclass(c.table_name)::oid IS DISTINCT FROM c.table_oid;
        DELETE FROM rvbbit.accel_observer_counters c
         WHERE to_regclass(c.table_name) IS NULL
            OR to_regclass(c.table_name)::oid IS DISTINCT FROM c.table_oid;

        CREATE TEMP TABLE rvbbit_accel_observer_evidence ON COMMIT DROP AS
        WITH base AS (
            SELECT cur.*,
                   coalesce(a.query_shapes, 0)::bigint AS query_shapes,
                   coalesce(a.query_calls, 0)::bigint AS query_calls,
                   coalesce(a.users, 0)::bigint AS users,
                   coalesce(a.active_hours, 0)::bigint AS active_hours,
                   coalesce(a.inclusive_ms, 0)::double precision AS inclusive_ms,
                   coalesce(a.attributed_ms, 0)::double precision AS attributed_ms,
                   a.p95_ms,
                   CASE
                       WHEN prev.table_oid IS NULL OR prev.table_name <> cur.table_name THEN 0
                       WHEN cur.seq_scans_total >= prev.seq_scans_total
                           THEN cur.seq_scans_total - prev.seq_scans_total
                       ELSE 0
                   END::bigint AS seq_scans_delta,
                   CASE
                       WHEN prev.table_oid IS NULL OR prev.table_name <> cur.table_name THEN 0
                       WHEN cur.seq_rows_total >= prev.seq_rows_total
                           THEN cur.seq_rows_total - prev.seq_rows_total
                       ELSE 0
                   END::bigint AS seq_rows_delta,
                   CASE
                       WHEN prev.table_oid IS NULL OR prev.table_name <> cur.table_name THEN 0
                       WHEN cur.writes_total >= prev.writes_total
                           THEN cur.writes_total - prev.writes_total
                       ELSE 0
                   END::bigint AS writes_delta,
                   old.table_oid IS NOT NULL AS previously_observed
              FROM pg_temp.rvbbit_accel_observer_current cur
              LEFT JOIN pg_temp.rvbbit_accel_observer_activity a USING (table_oid)
              LEFT JOIN rvbbit.accel_observer_counters prev USING (table_oid)
              LEFT JOIN rvbbit.accel_autopilot_candidates old USING (table_oid)
        ), measured AS (
            SELECT b.*,
                   (b.relkind = 'r'
                    AND b.relpersistence = 'p'
                    AND NOT b.is_partition
                    AND NOT b.row_security
                    AND b.access_method IN ('heap', 'rvbbit')) AS structurally_eligible,
                   (b.writes_delta::double precision / greatest(b.row_estimate, 1)) AS write_ratio,
                   (b.query_calls >= cfg.min_calls
                    AND b.active_hours >= cfg.min_active_hours
                    AND b.attributed_ms >= cfg.min_attributed_ms) AS hot
              FROM base b
        ), classified AS (
            SELECT m.*,
                   (m.structurally_eligible
                    AND m.table_bytes BETWEEN cfg.min_table_bytes AND cfg.max_table_bytes
                    AND m.write_ratio <= cfg.max_write_ratio) AS eligible,
                   least(100.0, greatest(0.0,
                       least(32.0, ln(1 + greatest(m.query_calls, 0)) * 6.0)
                       + least(18.0, greatest(m.active_hours, 0) * 3.0)
                       + least(28.0, greatest(m.attributed_ms, 0) / 1000.0)
                       + CASE WHEN m.baseline_missing THEN 8.0 ELSE 0.0 END
                       - least(30.0, m.write_ratio * 100.0)
                   ))::double precision AS score
              FROM measured m
        )
        SELECT c.*,
               CASE
                   WHEN c.acceleration_enabled AND c.row_groups > 0 THEN 'managed'
                   WHEN NOT c.structurally_eligible THEN 'held'
                   WHEN c.hot AND NOT c.eligible THEN 'held'
                   WHEN c.hot AND c.eligible THEN 'ready'
                   ELSE 'observing'
               END::text AS status,
               array_remove(ARRAY[
                   CASE WHEN c.query_calls > 0 THEN format(
                       '%s calls across %s active hour%s; %s ms attributed',
                       c.query_calls, c.active_hours,
                       CASE WHEN c.active_hours = 1 THEN '' ELSE 's' END,
                       round(c.attributed_ms::numeric)
                   ) END,
                   CASE WHEN c.baseline_missing THEN
                       'registered and enabled, but no accelerator baseline exists' END,
                   CASE WHEN c.acceleration_enabled AND c.row_groups > 0 THEN
                       'already accelerated; retained as post-build evidence' END,
                   CASE WHEN c.relkind <> 'r' THEN
                       'not an ordinary table' END,
                   CASE WHEN c.relpersistence <> 'p' THEN
                       'temporary or unlogged tables are never auto-admission candidates' END,
                   CASE WHEN c.is_partition THEN
                       'partition children require an explicit layout decision' END,
                   CASE WHEN c.row_security THEN
                       'row-level-security tables require explicit review' END,
                   CASE WHEN c.access_method NOT IN ('heap', 'rvbbit') THEN
                       'unsupported table access method: ' || coalesce(nullif(c.access_method, ''), 'none') END,
                   CASE WHEN c.table_bytes < cfg.min_table_bytes THEN format(
                       'below the %s byte minimum; acceleration is unlikely to pay back',
                       cfg.min_table_bytes
                   ) END,
                   CASE WHEN c.table_bytes > cfg.max_table_bytes THEN format(
                       'above the %s byte unattended-build ceiling', cfg.max_table_bytes
                   ) END,
                   CASE WHEN c.write_ratio > cfg.max_write_ratio THEN format(
                       'write ratio %s exceeds the %s safety ceiling',
                       round(c.write_ratio::numeric, 4), cfg.max_write_ratio
                   ) END,
                   CASE WHEN NOT c.hot THEN
                       'collecting more recurring SELECT-query evidence' END
               ]::text[], NULL)::text[] AS reasons
          FROM classified c
         WHERE c.query_calls > 0
            OR c.previously_observed;

        INSERT INTO rvbbit.accel_observer_observations (
            run_id, table_oid, table_name, relkind, relpersistence, access_method,
            is_partition, row_security, structurally_eligible, eligible, hot,
            status, score, reasons, query_shapes, query_calls, users, active_hours,
            inclusive_ms, attributed_ms, p95_ms, seq_scans_total, seq_scans_delta,
            seq_rows_total, seq_rows_delta, writes_total, writes_delta, write_ratio,
            row_estimate, table_bytes, registered, acceleration_enabled, row_groups,
            baseline_missing
        )
        SELECT v_run_id, table_oid, table_name, relkind, relpersistence, access_method,
               is_partition, row_security, structurally_eligible, eligible, hot,
               status, score, reasons, query_shapes, query_calls, users, active_hours,
               inclusive_ms, attributed_ms, p95_ms, seq_scans_total, seq_scans_delta,
               seq_rows_total, seq_rows_delta, writes_total, writes_delta, write_ratio,
               row_estimate, table_bytes, registered, acceleration_enabled, row_groups,
               baseline_missing
          FROM pg_temp.rvbbit_accel_observer_evidence;

        INSERT INTO rvbbit.accel_autopilot_candidates (
            table_oid, table_name, first_observed_at, last_observed_at,
            last_evidence_at, observation_count, hot_observation_count, hot_streak,
            status, structurally_eligible, eligible, hot, score, reasons,
            query_shapes, query_calls, users, active_hours, inclusive_ms,
            attributed_ms, p95_ms, seq_scans_total, seq_scans_delta,
            seq_rows_total, seq_rows_delta, writes_total, writes_delta, write_ratio,
            row_estimate, table_bytes, registered, acceleration_enabled, row_groups,
            baseline_missing, last_run_id
        )
        SELECT table_oid, table_name, clock_timestamp(), clock_timestamp(),
               clock_timestamp(), 1, CASE WHEN hot THEN 1 ELSE 0 END,
               CASE WHEN hot THEN 1 ELSE 0 END,
               status, structurally_eligible, eligible, hot, score, reasons,
               query_shapes, query_calls, users, active_hours, inclusive_ms,
               attributed_ms, p95_ms, seq_scans_total, seq_scans_delta,
               seq_rows_total, seq_rows_delta, writes_total, writes_delta, write_ratio,
               row_estimate, table_bytes, registered, acceleration_enabled, row_groups,
               baseline_missing, v_run_id
          FROM pg_temp.rvbbit_accel_observer_evidence
        ON CONFLICT (table_oid) DO UPDATE SET
            table_name = EXCLUDED.table_name,
            last_observed_at = EXCLUDED.last_observed_at,
            last_evidence_at = CASE
                WHEN EXCLUDED.query_calls > 0
                    THEN EXCLUDED.last_observed_at
                ELSE rvbbit.accel_autopilot_candidates.last_evidence_at
            END,
            observation_count = rvbbit.accel_autopilot_candidates.observation_count + 1,
            hot_observation_count = rvbbit.accel_autopilot_candidates.hot_observation_count
                + CASE WHEN EXCLUDED.hot THEN 1 ELSE 0 END,
            hot_streak = CASE WHEN EXCLUDED.hot
                THEN rvbbit.accel_autopilot_candidates.hot_streak + 1 ELSE 0 END,
            status = EXCLUDED.status,
            structurally_eligible = EXCLUDED.structurally_eligible,
            eligible = EXCLUDED.eligible,
            hot = EXCLUDED.hot,
            score = EXCLUDED.score,
            reasons = EXCLUDED.reasons,
            query_shapes = EXCLUDED.query_shapes,
            query_calls = EXCLUDED.query_calls,
            users = EXCLUDED.users,
            active_hours = EXCLUDED.active_hours,
            inclusive_ms = EXCLUDED.inclusive_ms,
            attributed_ms = EXCLUDED.attributed_ms,
            p95_ms = EXCLUDED.p95_ms,
            seq_scans_total = EXCLUDED.seq_scans_total,
            seq_scans_delta = EXCLUDED.seq_scans_delta,
            seq_rows_total = EXCLUDED.seq_rows_total,
            seq_rows_delta = EXCLUDED.seq_rows_delta,
            writes_total = EXCLUDED.writes_total,
            writes_delta = EXCLUDED.writes_delta,
            write_ratio = EXCLUDED.write_ratio,
            row_estimate = EXCLUDED.row_estimate,
            table_bytes = EXCLUDED.table_bytes,
            registered = EXCLUDED.registered,
            acceleration_enabled = EXCLUDED.acceleration_enabled,
            row_groups = EXCLUDED.row_groups,
            baseline_missing = EXCLUDED.baseline_missing,
            last_run_id = EXCLUDED.last_run_id;

        -- Snapshot every ordinary table so the next run sees interval deltas,
        -- while retaining detailed history only for tables with evidence.
        INSERT INTO rvbbit.accel_observer_counters (
            table_oid, table_name, seq_scans_total, seq_rows_total, writes_total, observed_at
        )
        SELECT table_oid, table_name, seq_scans_total, seq_rows_total,
               writes_total, clock_timestamp()
          FROM pg_temp.rvbbit_accel_observer_current
         WHERE relkind = 'r'
        ON CONFLICT (table_oid) DO UPDATE SET
            table_name = EXCLUDED.table_name,
            seq_scans_total = EXCLUDED.seq_scans_total,
            seq_rows_total = EXCLUDED.seq_rows_total,
            writes_total = EXCLUDED.writes_total,
            observed_at = EXCLUDED.observed_at;

        DELETE FROM rvbbit.accel_observer_observations
         WHERE observed_at < clock_timestamp() - make_interval(days => cfg.retention_days);
        DELETE FROM rvbbit.accel_autopilot_candidates
         WHERE last_evidence_at < clock_timestamp() - make_interval(days => cfg.retention_days);
        DELETE FROM rvbbit.accel_observer_runs r
         WHERE r.started_at < clock_timestamp() - make_interval(days => cfg.retention_days)
           AND NOT EXISTS (
               SELECT 1 FROM rvbbit.accel_observer_observations o WHERE o.run_id = r.run_id
           );

        SELECT count(*), count(*) FILTER (WHERE status = 'ready')
          INTO v_tables_observed, v_ready
          FROM pg_temp.rvbbit_accel_observer_evidence;

        UPDATE rvbbit.accel_observer_runs
           SET finished_at = clock_timestamp(),
               status = 'ok',
               query_shapes_seen = v_shapes_seen,
               query_shapes_resolved = v_shapes_resolved,
               tables_observed = v_tables_observed,
               ready_count = v_ready,
               message = CASE WHEN v_activity_available
                   THEN 'observe-only; no tables were changed'
                   ELSE 'observe-only; SELECT activity unavailable; table statistics retained as context only'
                        || CASE WHEN v_activity_error IS NOT NULL THEN ': ' || v_activity_error ELSE '' END
               END
         WHERE run_id = v_run_id;

        RETURN jsonb_build_object(
            'status', 'ok',
            'mode', cfg.mode,
            'run_id', v_run_id,
            'query_shapes_seen', v_shapes_seen,
            'query_shapes_resolved', v_shapes_resolved,
            'tables_observed', v_tables_observed,
            'ready', v_ready,
            'activity_source', v_activity_available,
            'activity_error', v_activity_error,
            'mutations', 0,
            'message', 'observe-only; no tables were changed'
        );
    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS v_error = MESSAGE_TEXT;
        UPDATE rvbbit.accel_observer_runs
           SET finished_at = clock_timestamp(), status = 'failed', error = v_error
         WHERE run_id = v_run_id;
        RETURN jsonb_build_object(
            'status', 'failed', 'mode', cfg.mode, 'run_id', v_run_id,
            'error', v_error, 'mutations', 0
        );
    END;
END
$fn$;

COMMENT ON FUNCTION rvbbit.accel_autopilot_observe(text) IS
    'Collect durable SELECT workload evidence and classify acceleration candidates. Raw table counters are context only; observe-only never mutates a table.';


-- Candidate rows are derived state, not user configuration.  Earlier observer
-- runs could admit COPY/ETL traffic through pg_stat_user_tables deltas, and the
-- rows do not retain enough statement provenance to repair them safely.  Reset
-- the current projection once; observation/run history remains available.
DELETE FROM rvbbit.accel_autopilot_candidates;

COMMENT ON TABLE rvbbit.accel_autopilot_candidates IS
    'Latest persistent SELECT workload evidence per table. Raw table counters are diagnostic context and never admit or score a candidate.';

COMMENT ON COLUMN rvbbit.accel_autopilot_config.min_seq_scans_delta IS
    'Legacy compatibility setting. Sequential-scan counters are context only and no longer admit acceleration candidates.';
COMMENT ON COLUMN rvbbit.accel_autopilot_config.min_seq_rows_delta IS
    'Legacy compatibility setting. Sequential-row counters are context only and no longer score acceleration candidates.';

-- The original system-learning view predates the observer and exposes every
-- heap table with a sequential-scan counter.  Keep that raw inventory intact,
-- but give the Brain a curated surface where heap candidates must have durable
-- SELECT evidence.
CREATE OR REPLACE VIEW rvbbit.system_learning_select_items AS
SELECT i.*
  FROM rvbbit.system_learning_items i
 WHERE coalesce(i.props->>'object_type', '') <> 'heap_acceleration_candidate'
    OR EXISTS (
        SELECT 1
          FROM rvbbit.accel_autopilot_candidates c
         WHERE c.table_name = i.props->>'table'
           AND c.query_calls > 0
    );

COMMENT ON VIEW rvbbit.system_learning_select_items IS
    'System-learning items with heap acceleration suggestions admitted only by durable SELECT evidence.';

UPDATE rvbbit.brain_doc_providers
   SET list_sql = $provider$
       SELECT uri, title, content_hash, occurred_at, body, props
       FROM rvbbit.system_learning_select_items
   $provider$,
       updated_at = clock_timestamp()
 WHERE provider = 'rvbbit-system-learning';

CREATE OR REPLACE VIEW rvbbit.system_learning_item_summary AS
SELECT
    coalesce(props->>'object_type', 'unknown') AS object_type,
    count(*)::bigint AS items,
    max(occurred_at) AS last_seen_at
FROM rvbbit.system_learning_select_items
GROUP BY coalesce(props->>'object_type', 'unknown');

CREATE OR REPLACE VIEW rvbbit.system_learning_brain_status AS
WITH src AS (
    SELECT source_id, label, kind, enabled, config, last_synced_at
    FROM rvbbit.brain_sources
    WHERE label = 'RVBBIT System Learning'
), last_run AS (
    SELECT r.source_id, r.started_at, r.finished_at, r.added, r.changed,
           r.removed, r.skipped, r.errors, r.elapsed_sec
    FROM rvbbit.brain_sync_runs r
    JOIN src s ON s.source_id = r.source_id
    ORDER BY r.started_at DESC
    LIMIT 1
)
SELECT
    to_regclass('rvbbit.system_learning_select_items') IS NOT NULL AS installed,
    (SELECT source_id FROM src) AS source_id,
    coalesce((SELECT enabled FROM src), false) AS enabled,
    coalesce((SELECT count(*) FROM rvbbit.system_learning_select_items), 0)::bigint AS indexed_items,
    coalesce((
        SELECT count(*)
        FROM rvbbit.brain_documents d
        JOIN src s ON s.source_id = d.source_id
        WHERE d.deleted_at IS NULL
    ), 0)::bigint AS docs,
    (SELECT last_synced_at FROM src) AS last_synced_at,
    (SELECT started_at FROM last_run) AS last_run_at,
    coalesce((SELECT added FROM last_run), 0)::int AS last_run_added,
    coalesce((SELECT changed FROM last_run), 0)::int AS last_run_changed,
    coalesce((SELECT removed FROM last_run), 0)::int AS last_run_removed,
    coalesce((SELECT skipped FROM last_run), 0)::int AS last_run_skipped,
    coalesce((SELECT errors FROM last_run), 0)::int AS last_run_errors,
    (SELECT elapsed_sec FROM last_run) AS last_run_elapsed_sec;
