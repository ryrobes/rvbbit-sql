-- 0115_system_learning_brain_provider
--
-- Feed RVBBIT's own learned state into the Brain/query-source pipeline. The
-- output is intentionally document-shaped: stable uri, title, body, timestamp,
-- content_hash, and structured props. brain_nightly() can then sync it like any
-- MCP/SQL-backed source, and ask_brain()/MCP callers can discover the same
-- breadcrumbs Lens shows.

CREATE OR REPLACE VIEW rvbbit.system_learning_items AS
WITH layout_items AS (
    SELECT
        'rvbbit:layout:' || table_oid::text || ':' || layout AS uri,
        'Workload layout ' || layout || ' for ' || table_name AS title,
        coalesce(updated_at, recommended_at, now()) AS occurred_at,
        jsonb_build_object(
            'object_type', 'workload_layout',
            'table', table_name,
            'column', column_name,
            'layout', layout,
            'layout_kind', layout_kind,
            'status', status,
            'layout_status', coalesce(layout_status, 'not_built'),
            'score', score,
            'observations', observations,
            'weighted_ms', weighted_ms,
            'role_counts', role_counts,
            'sample_shapes', sample_shapes
        ) AS props,
        concat_ws(E'\n',
            'RVBBIT learned a workload layout recommendation.',
            'Table: ' || table_name,
            'Layout: ' || layout || ' (' || layout_kind || ' on ' || column_name || ')',
            'Status: ' || status || ', build: ' || coalesce(layout_status, 'not built'),
            'Score: ' || round(score::numeric, 2)::text,
            'Observations: ' || observations::text,
            'Weighted latency: ' || round(weighted_ms::numeric, 2)::text || ' ms',
            'Roles: where=' || coalesce(role_counts->>'where', '0') ||
                ', group_by=' || coalesce(role_counts->>'group_by', '0') ||
                ', order_by=' || coalesce(role_counts->>'order_by', '0') ||
                ', count_distinct=' || coalesce(role_counts->>'count_distinct', '0'),
            'Reason: ' || nullif(reason, ''),
            CASE WHEN layout_status = 'ready'
                 THEN 'Ready layout rows: ' || coalesce(layout_rows::text, 'unknown') ||
                      ', files: ' || coalesce(layout_files::text, 'unknown')
                 ELSE 'Not serving yet. Accept and build this layout to create physical variants.'
            END
        ) AS body
    FROM rvbbit.workload_layout_recommendation_status
),
route_shape_items AS (
    SELECT
        'rvbbit:route-shape:' || s.shape_key AS uri,
        'Route shape ' || left(s.shape_key, 24) ||
            CASE WHEN rs.best_candidate IS NULL THEN '' ELSE ' prefers ' || rs.best_candidate END AS title,
        coalesce(rs.last_seen, s.captured_at, now()) AS occurred_at,
        jsonb_build_object(
            'object_type', 'route_shape',
            'shape_key', s.shape_key,
            'shape_family', s.shape_family,
            'engine', rs.best_candidate,
            'observations', rs.observations,
            'best_median_ms', rs.best_median_ms,
            'observed_gain', rs.observed_gain,
            'needs_exploration', rs.needs_exploration,
            'sample_sql', left(s.sql, 4000)
        ) AS props,
        concat_ws(E'\n',
            'RVBBIT observed a repeatable SQL route shape.',
            'Shape key: ' || s.shape_key,
            'Family: ' || s.shape_family,
            'Best candidate: ' || coalesce(rs.best_candidate, 'not enough observations'),
            'Best median: ' || coalesce(round(rs.best_median_ms::numeric, 2)::text || ' ms', 'unknown'),
            'Observed gain: ' || coalesce(round((rs.observed_gain * 100.0)::numeric, 1)::text || '%', 'unknown'),
            'Needs exploration: ' || coalesce(rs.needs_exploration::text, 'unknown'),
            'Native median: ' || coalesce(round(rs.native_median_ms::numeric, 2)::text || ' ms', 'unknown'),
            'Duck vortex median: ' || coalesce(round(rs.duck_vortex_median_ms::numeric, 2)::text || ' ms', 'unknown'),
            'DataFusion vortex median: ' || coalesce(round(rs.datafusion_vortex_median_ms::numeric, 2)::text || ' ms', 'unknown'),
            'Representative SQL:',
            left(s.sql, 4000)
        ) AS body
    FROM rvbbit.route_shape_samples s
    LEFT JOIN rvbbit.route_shape_summary rs ON rs.shape_key = s.shape_key
),
accel_table_items AS (
    SELECT
        'rvbbit:accel-table:' || table_oid::text AS uri,
        'Acceleration state for ' || table_name AS title,
        coalesce(last_write_at, last_refresh_at, dirty_since, now()) AS occurred_at,
        jsonb_build_object(
            'object_type', 'acceleration_state',
            'table', table_name,
            'status', CASE WHEN shadow_heap_dirty THEN 'dirty' ELSE 'fresh' END,
            'parquet_authoritative', parquet_authoritative,
            'parquet_rows', parquet_rows,
            'row_groups', row_groups,
            'parquet_bytes', parquet_bytes,
            'heap_live_tuples', heap_live_tuples,
            'drift_rows', drift_rows,
            'drift_ratio', drift_ratio,
            'heap_seq_scans', heap_seq_scans,
            'op_running', op_running
        ) AS props,
        concat_ws(E'\n',
            'RVBBIT acceleration freshness signal.',
            'Table: ' || table_name,
            'Status: ' || CASE WHEN shadow_heap_dirty THEN 'dirty' ELSE 'fresh' END,
            'Parquet authoritative: ' || parquet_authoritative::text,
            'Parquet rows: ' || parquet_rows::text || ', row groups: ' || row_groups::text,
            'Heap live tuples: ' || heap_live_tuples::text,
            'Drift rows: ' || drift_rows::text ||
                coalesce(' (' || round((drift_ratio * 100.0)::numeric, 2)::text || '%)', ''),
            'Heap slow-path sequential scans: ' || heap_seq_scans::text,
            'Last refresh: ' || coalesce(last_refresh_at::text, 'never'),
            CASE WHEN op_running THEN 'A maintenance operation is currently running.'
                 ELSE 'No maintenance operation is currently running.'
            END
        ) AS body
    FROM rvbbit.accel_freshness
),
heap_candidate_items AS (
    SELECT
        'rvbbit:heap-candidate:' || c.oid::text AS uri,
        'Heap acceleration candidate ' || c.oid::regclass::text AS title,
        now() AS occurred_at,
        jsonb_build_object(
            'object_type', 'heap_acceleration_candidate',
            'table', c.oid::regclass::text,
            'status', 'not_accelerated',
            'seq_scans', coalesce(s.seq_scan, 0),
            'seq_rows', coalesce(s.seq_tup_read, 0),
            'idx_scans', coalesce(s.idx_scan, 0),
            'writes', coalesce(s.n_tup_ins, 0) + coalesce(s.n_tup_upd, 0) + coalesce(s.n_tup_del, 0),
            'size_bytes', pg_total_relation_size(c.oid),
            'row_estimate', greatest(c.reltuples, 0)
        ) AS props,
        concat_ws(E'\n',
            'RVBBIT found a regular heap table that may benefit from registry-backed acceleration.',
            'Table: ' || c.oid::regclass::text,
            'Sequential scans: ' || coalesce(s.seq_scan, 0)::text,
            'Rows read by sequential scans: ' || coalesce(s.seq_tup_read, 0)::text,
            'Index scans: ' || coalesce(s.idx_scan, 0)::text,
            'Writes observed: ' || (coalesce(s.n_tup_ins, 0) + coalesce(s.n_tup_upd, 0) + coalesce(s.n_tup_del, 0))::text,
            'Size bytes: ' || pg_total_relation_size(c.oid)::text,
            'Use rvbbit.enable_table(...) to register it, then build acceleration if the workload is read-heavy.'
        ) AS body
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
    LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
    WHERE c.relkind IN ('r', 'p', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'rvbbit')
      AND n.nspname NOT LIKE 'pg_toast%'
      AND n.nspname NOT LIKE 'pg_temp_%'
      AND t.table_oid IS NULL
      AND (coalesce(s.seq_scan, 0) > 0 OR coalesce(s.seq_tup_read, 0) > 0)
),
operator_stats AS (
    SELECT
        operator,
        count(*)::bigint AS calls,
        count(DISTINCT inputs_hash)::bigint AS unique_inputs,
        coalesce(sum(cost_usd), 0)::numeric AS cost_usd,
        avg(latency_ms)::double precision AS avg_latency_ms,
        max(invocation_at) AS last_seen
    FROM rvbbit.receipts
    WHERE invocation_at >= now() - interval '30 days'
    GROUP BY operator
),
operator_items AS (
    SELECT
        'rvbbit:operator:' || o.name AS uri,
        'SQL operator ' || o.name AS title,
        coalesce(greatest(o.updated_at, os.last_seen), o.updated_at, o.created_at, now()) AS occurred_at,
        jsonb_build_object(
            'object_type', 'operator',
            'operator', o.name,
            'shape', o.shape,
            'model', o.model,
            'cache_policy', o.cache_policy,
            'return_type', o.return_type,
            'parser', o.parser,
            'calls_30d', coalesce(os.calls, 0),
            'unique_inputs_30d', coalesce(os.unique_inputs, 0),
            'cost_usd_30d', coalesce(os.cost_usd, 0),
            'avg_latency_ms_30d', os.avg_latency_ms,
            'has_steps', o.steps IS NOT NULL,
            'has_retry', o.retry IS NOT NULL,
            'has_wards', o.wards IS NOT NULL,
            'has_takes', o.takes IS NOT NULL
        ) AS props,
        concat_ws(E'\n',
            'RVBBIT trusted SQL operator.',
            'Operator: rvbbit.' || o.name,
            'Shape: ' || o.shape || ', returns: ' || o.return_type || ', parser: ' || o.parser,
            'Model: ' || o.model || ', cache policy: ' || o.cache_policy,
            'Description: ' || coalesce(nullif(o.description, ''), 'none'),
            'Steps: ' || coalesce((
                SELECT string_agg(DISTINCT step->>'kind', ', ' ORDER BY step->>'kind')
                FROM jsonb_array_elements(coalesce(o.steps, '[]'::jsonb)) step
            ), 'single llm call'),
            'Retry: ' || CASE WHEN o.retry IS NULL THEN 'none' ELSE left(o.retry::text, 600) END,
            'Wards: ' || CASE WHEN o.wards IS NULL THEN 'none' ELSE left(o.wards::text, 600) END,
            'Takes: ' || CASE WHEN o.takes IS NULL THEN 'none' ELSE left(o.takes::text, 600) END,
            'Last 30 days: calls=' || coalesce(os.calls, 0)::text ||
                ', unique inputs=' || coalesce(os.unique_inputs, 0)::text ||
                ', cost_usd=' || coalesce(round(os.cost_usd, 6)::text, '0') ||
                ', avg_latency_ms=' || coalesce(round(os.avg_latency_ms::numeric, 2)::text, 'unknown')
        ) AS body
    FROM rvbbit.operators o
    LEFT JOIN operator_stats os ON os.operator = o.name
)
SELECT
    uri,
    title,
    md5(coalesce(body, '') || coalesce(props::text, '')) AS content_hash,
    occurred_at,
    body,
    props
FROM layout_items
UNION ALL
SELECT uri, title, md5(coalesce(body, '') || coalesce(props::text, '')), occurred_at, body, props
FROM route_shape_items
UNION ALL
SELECT uri, title, md5(coalesce(body, '') || coalesce(props::text, '')), occurred_at, body, props
FROM accel_table_items
UNION ALL
SELECT uri, title, md5(coalesce(body, '') || coalesce(props::text, '')), occurred_at, body, props
FROM heap_candidate_items
UNION ALL
SELECT uri, title, md5(coalesce(body, '') || coalesce(props::text, '')), occurred_at, body, props
FROM operator_items;

CREATE OR REPLACE VIEW rvbbit.system_learning_item_summary AS
SELECT
    coalesce(props->>'object_type', 'unknown') AS object_type,
    count(*)::bigint AS items,
    max(occurred_at) AS last_seen_at
FROM rvbbit.system_learning_items
GROUP BY coalesce(props->>'object_type', 'unknown');

SELECT rvbbit.brain_define_provider(
    'rvbbit-system-learning',
    'RVBBIT System Learning',
    $sql$
        SELECT uri, title, content_hash, occurred_at, body, props
        FROM rvbbit.system_learning_items
    $sql$,
    NULL,
    'brain',
    'RVBBIT-learned workload, routing, acceleration, and operator artifacts for the business brain.',
    $edges$
        [
          {"predicate":"about_table","kind":"db_table","path":"$.table"},
          {"predicate":"about_column","kind":"db_column","path":"$.column"},
          {"predicate":"about_shape","kind":"route_shape","path":"$.shape_key"},
          {"predicate":"uses_engine","kind":"engine","path":"$.engine"},
          {"predicate":"has_status","kind":"status","path":"$.status"},
          {"predicate":"about_operator","kind":"operator","path":"$.operator"}
        ]
    $edges$::jsonb,
    'system_learning'
);

SELECT rvbbit.brain_add_query_source(
    'RVBBIT System Learning',
    'rvbbit-system-learning',
    '{"doc_type":"system_learning"}'::jsonb,
    true
);

CREATE OR REPLACE VIEW rvbbit.system_learning_brain_status AS
WITH src AS (
    SELECT source_id, label, kind, enabled, config, last_synced_at
    FROM rvbbit.brain_sources
    WHERE label = 'RVBBIT System Learning'
), last_run AS (
    SELECT r.source_id, r.started_at, r.finished_at, r.added, r.changed, r.removed, r.skipped, r.errors, r.elapsed_sec
    FROM rvbbit.brain_sync_runs r
    JOIN src s ON s.source_id = r.source_id
    ORDER BY r.started_at DESC
    LIMIT 1
)
SELECT
    to_regclass('rvbbit.system_learning_items') IS NOT NULL AS installed,
    (SELECT source_id FROM src) AS source_id,
    coalesce((SELECT enabled FROM src), false) AS enabled,
    coalesce((SELECT count(*) FROM rvbbit.system_learning_items), 0)::bigint AS indexed_items,
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
