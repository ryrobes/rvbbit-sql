-- System Health, full parity, as plate rows. Every tone is a column; every
-- remedy is SQL built by SQL (string_agg over the same top-N the TS builder
-- used) and opened built-not-run.
SELECT rvbbit.upsert_plate(
  'system/health',
  'System Health',
  $tpl$
<div class="plate-section">
  <h2>rvbbit metadata weight &amp; maintenance</h2>
  <div class="plate-cards">
    <div rv-each="cards" class="plate-card {{ row.tone }}">
      <div class="plate-card-title">{{ row.name }}</div>
      <div class="plate-card-value">{{ row.value }}</div>
      <div class="plate-card-note">{{ row.note }}</div>
    </div>
    <div rv-each="cron_card" class="plate-card {{ row.tone }}">
      <div class="plate-card-title">{{ row.name }}</div>
      <div class="plate-card-value">{{ row.value }}</div>
      <div class="plate-card-note">{{ row.note }}</div>
    </div>
  </div>
</div>

<div class="plate-section">
  <h3>Top tombstone holders</h3>
  <table class="plate-table">
    <thead><tr><th>table</th><th>tombstones</th><th></th></tr></thead>
    <tbody>
      <tr rv-each="tombstone_top">
        <td><code>{{ row.rel }}</code></td>
        <td>{{ row.tombstones }}</td>
        <td><button type="button" rv-open-sql="{{ row.rebuild_sql }}" rv-open-sql-title="Rebuild {{ row.rel }}">Rebuild SQL</button></td>
      </tr>
    </tbody>
  </table>
</div>

<div class="plate-section">
  <h3>Remedies — built, reviewed, run by you</h3>
  <div rv-each="remedies">
    <p>
      <button type="button" rv-open-sql="{{ row.rebuild_script }}" rv-open-sql-title="Rebuild top tombstone tables">Rebuild top tables</button>
      <button type="button" rv-open-sql="{{ row.reap_script }}" rv-open-sql-title="Reap old generations">Reap generations</button>
      <button type="button" rv-open-sql="{{ row.snapshots_script }}" rv-open-sql-title="Catalog snapshot retention">Snapshot retention</button>
      <button type="button" rv-open-sql="{{ row.orphans_script }}" rv-open-sql-title="Reap orphaned files">Orphaned files</button>
      <button type="button" rv-open-sql="{{ row.vacuum_script }}" rv-open-sql-title="Vacuum rvbbit metadata">Vacuum metadata</button>
      <button type="button" rv-open-sql="{{ row.jobs_script }}" rv-open-sql-title="Install maintenance jobs">Install cron jobs</button>
    </p>
  </div>
</div>

<div class="plate-section">
  <h3>Scheduled jobs</h3>
  <table class="plate-table">
    <thead><tr><th>job</th><th>schedule</th><th>db</th><th>active</th></tr></thead>
    <tbody>
      <tr rv-each="cron_jobs">
        <td><code>{{ row.jobname }}</code></td>
        <td>{{ row.schedule }}</td>
        <td>{{ row.db }}</td>
        <td><span rv-if="row.active">yes</span><span rv-if="!row.active" class="plate-row-flag">no</span></td>
      </tr>
    </tbody>
  </table>
  <div rv-each="cron_note"><p class="plate-row-flag" rv-if="row.warn">{{ row.msg }}</p></div>
</div>
$tpl$,
  jsonb_build_object(
    'cards', jsonb_build_object('sql', $q$
WITH db AS (SELECT pg_database_size(current_database()) AS bytes),
exhaust AS (
  SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) AS bytes
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'rvbbit' AND c.relkind = 'r'
),
tomb AS (SELECT count(*) AS n FROM rvbbit.delete_log),
gens AS (SELECT count(*) AS n, count(DISTINCT table_oid) AS tables FROM rvbbit.generations),
snaps AS (SELECT (SELECT count(*) FROM rvbbit.catalog_runs) AS runs,
                 (SELECT pg_total_relation_size('rvbbit.catalog_snapshots')) AS bytes),
orph AS (SELECT count(*) AS backlog,
                count(*) FILTER (WHERE last_error IS NOT NULL) AS erroring
         FROM rvbbit.orphaned_files),
vac AS (SELECT count(*) AS active FROM pg_stat_progress_vacuum),
dead AS (SELECT coalesce(max(n_dead_tup), 0) AS worst FROM pg_stat_user_tables WHERE schemaname = 'rvbbit')
SELECT 1 AS ord, 'metadata weight' AS name,
       pg_size_pretty(exhaust.bytes) AS value,
       CASE WHEN exhaust.bytes > db.bytes * 0.5 THEN 'bad'
            WHEN exhaust.bytes > db.bytes * 0.25 THEN 'warn' ELSE 'ok' END AS tone,
       round(100.0 * exhaust.bytes / greatest(db.bytes, 1), 1) || '% of ' || pg_size_pretty(db.bytes) AS note
FROM db, exhaust
UNION ALL
SELECT 2, 'tombstones', n::text,
       CASE WHEN n > 10000000 THEN 'bad' WHEN n > 1000000 THEN 'warn' ELSE 'ok' END,
       'delete_log rows — die only via rebuild' FROM tomb
UNION ALL
SELECT 3, 'generations', n::text,
       CASE WHEN n > 2000 THEN 'bad' WHEN n > 500 THEN 'warn' ELSE 'ok' END,
       'across ' || tables || ' accelerated tables' FROM gens
UNION ALL
SELECT 4, 'catalog snapshots', runs::text,
       CASE WHEN runs > 40 THEN 'bad' WHEN runs > 20 THEN 'warn' ELSE 'ok' END,
       'crawl runs retained · ' || pg_size_pretty(bytes) FROM snaps
UNION ALL
SELECT 5, 'orphaned files', backlog::text,
       CASE WHEN erroring > 0 THEN 'bad' WHEN backlog > 1000 THEN 'warn' ELSE 'ok' END,
       erroring || ' erroring' FROM orph
UNION ALL
SELECT 6, 'vacuum', (SELECT active FROM vac)::text || ' active',
       CASE WHEN (SELECT worst FROM dead) > 1000000 THEN 'warn' ELSE 'ok' END,
       'worst rvbbit dead-tuple count: ' || (SELECT worst FROM dead) FROM vac
ORDER BY 1
    $q$),
    'cron_card', jsonb_build_object('database', 'postgres', 'sql', $q$
SELECT 'maintenance crons' AS name,
       count(*)::text AS value,
       CASE WHEN count(*) = 0 THEN 'warn' ELSE 'ok' END AS tone,
       CASE WHEN count(*) = 0 THEN 'not installed — see remedies'
            ELSE 'rvbbit-* jobs scheduled' END AS note
FROM cron.job WHERE jobname LIKE 'rvbbit-%'
    $q$),
    'tombstone_top', jsonb_build_object('sql', $q$
SELECT n.nspname || '.' || c.relname AS rel,
       count(*)::int8 AS tombstones,
       'SELECT rvbbit.rebuild_acceleration(' || quote_literal(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) || '::regclass, true);' AS rebuild_sql
FROM rvbbit.delete_log dl
JOIN pg_class c ON c.oid = dl.table_oid
JOIN pg_namespace n ON n.oid = c.relnamespace
GROUP BY n.nspname, c.relname ORDER BY 2 DESC LIMIT 10
    $q$),
    'remedies', jsonb_build_object('sql', $q$
SELECT
  coalesce((SELECT E'-- Rebuild the heaviest tombstone holders (order = impact).\n-- Review before running; each rebuild rewrites that table''s columnar files.\n' ||
    string_agg('SELECT rvbbit.rebuild_acceleration(' || quote_literal(rel) || '::regclass, true);  -- ' || tombstones || ' tombstones', E'\n')
    FROM (SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS rel, count(*) AS tombstones
          FROM rvbbit.delete_log dl JOIN pg_class c ON c.oid = dl.table_oid JOIN pg_namespace n ON n.oid = c.relnamespace
          GROUP BY 1 ORDER BY count(*) DESC LIMIT 10) t), '-- no tombstones to rebuild') AS rebuild_script,
  coalesce((SELECT E'-- Reap generations older than 30 days on the deepest tables.\n' ||
    string_agg('SELECT rvbbit.reap_generations(' || quote_literal(rel) || '::regclass, 30);  -- ' || gens || ' generations', E'\n')
    FROM (SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS rel, count(*) AS gens
          FROM rvbbit.generations g JOIN pg_class c ON c.oid = g.table_oid JOIN pg_namespace n ON n.oid = c.relnamespace
          GROUP BY 1 HAVING count(*) > 10 ORDER BY count(*) DESC LIMIT 10) t), '-- no deep generation stacks') AS reap_script,
  E'-- Keep the newest 15 catalog crawl runs; drop the rest.\nDELETE FROM rvbbit.catalog_snapshots WHERE run_id NOT IN\n  (SELECT run_id FROM rvbbit.catalog_runs ORDER BY finished_at DESC NULLS LAST LIMIT 15);\nDELETE FROM rvbbit.catalog_runs WHERE run_id NOT IN\n  (SELECT run_id FROM rvbbit.catalog_runs ORDER BY finished_at DESC NULLS LAST LIMIT 15);\nVACUUM (ANALYZE) rvbbit.catalog_snapshots;' AS snapshots_script,
  E'-- Delete orphaned columnar files past the 7-day grace window (batches of 10k).\nSELECT rvbbit.reap_orphaned_files(''7 days''::interval, 10000);' AS orphans_script,
  E'-- Vacuum the rvbbit metadata hot spots.\nVACUUM (VERBOSE, ANALYZE) rvbbit.delete_log;\nVACUUM (VERBOSE, ANALYZE) rvbbit.catalog_snapshots;\nVACUUM (VERBOSE, ANALYZE) rvbbit.receipts;' AS vacuum_script,
  CASE
    WHEN current_database() = coalesce((SELECT setting FROM pg_settings WHERE name = 'cron.database_name'), 'postgres')
    THEN E'-- This database IS the pg_cron home — install directly.\nSELECT rvbbit.install_maintenance_jobs();'
    ELSE E'-- >>> RUN CONNECTED TO ' || quote_literal(coalesce((SELECT setting FROM pg_settings WHERE name = 'cron.database_name'), 'postgres')) || E' (the pg_cron home db) <<<\nCREATE EXTENSION IF NOT EXISTS pg_cron;\nSELECT cron.schedule_in_database(''rvbbit-maintain'', ''*/15 * * * *'', ''SELECT rvbbit.maintain();'', ' || quote_literal(current_database()) || E');\nSELECT cron.schedule_in_database(''rvbbit-storage-maintain'', ''0 * * * *'', ''SELECT rvbbit.maintain_storage();'', ' || quote_literal(current_database()) || ');'
  END AS jobs_script
    $q$),
    'cron_jobs', jsonb_build_object('database', 'postgres', 'sql',
      'SELECT jobname, schedule, coalesce(database, current_database()) AS db, active FROM cron.job ORDER BY jobname'),
    'cron_note', jsonb_build_object('sql', $q$
SELECT (current_database() <> coalesce((SELECT setting FROM pg_settings WHERE name='cron.database_name'), 'postgres')) AS warn,
       'pg_cron home is ''' || coalesce((SELECT setting FROM pg_settings WHERE name='cron.database_name'), 'postgres') || ''' — the install script targets this db via schedule_in_database.' AS msg
    $q$)
  ),
  '{}'::jsonb, '[]'::jsonb,
  NULL, 'Full System Health as plate rows: 7 status cards, tombstone top-10, six built-not-run remedies, cron state'
);
SELECT 'health plate seeded';
