#!/bin/bash
# First-boot hosted Scheduler seed. The normal bootstrap repairs this inventory
# on existing volumes; this early seed makes a fresh database useful immediately.

set -uo pipefail

CRON_DB="${RVBBIT_CRON_DATABASE:-postgres}"
TARGET_DB="${POSTGRES_DB:-rvbbit}"

echo "rvbbit: seeding hosted Scheduled Tasks in ${CRON_DB} for ${TARGET_DB}"

if psql -X -v ON_ERROR_STOP=1 \
    --username "${POSTGRES_USER:-postgres}" \
    --dbname "$CRON_DB" \
    -v target_db="$TARGET_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_cron;

SELECT cron.schedule_in_database(
    'rvbbit_calliope_dreams',
    '0 3 * * *',
    'SELECT rvbbit.calliope_dream_enqueue(''cron'',''calliope@system'',false);',
    :'target_db'
)
WHERE NOT EXISTS (
    SELECT 1 FROM cron.job
     WHERE jobname='rvbbit_calliope_dreams'
       AND database=:'target_db'
);

SELECT cron.schedule_in_database('rvbbit_catalog_refresh','0 3 * * *','CALL rvbbit.catalog_crawl_run();',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_catalog_refresh' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_olap_autopilot','* * * * *','SELECT rvbbit.accel_tick(4);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_olap_autopilot' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_layout_tick_worker_1','* * * * *','CALL rvbbit.layout_tick_worker_pass(1, 1, 1);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_layout_tick_worker_1' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_accel_observer','7 * * * *','SELECT rvbbit.accel_autopilot_observe(''scheduler'');',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_accel_observer' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit-maintain','*/15 * * * *','SELECT rvbbit.maintain();',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit-maintain' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit-storage-maintain','0 * * * *','SELECT rvbbit.maintain(storage_tables => 2);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit-storage-maintain' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_materialize_all','0 * * * *','SELECT rvbbit.materialize_all_metrics();',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_materialize_all' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_refresh_cubes','0 */2 * * *','CALL rvbbit.refresh_all_cubes();',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_refresh_cubes' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_route_optimize','0 5 * * *','SELECT rvbbit.route_optimize_auto(20, 600, 3);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_route_optimize' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_brain_sync','0 2 * * *','CALL rvbbit.brain_update_drain(''auto'');',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_brain_sync' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_brain_enrich','*/5 * * * *','CALL rvbbit.brain_enrich_drain(NULL, 20, 0, 270, ''cron'');',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_brain_enrich' AND database=:'target_db');

UPDATE cron.job SET active=true
 WHERE database=:'target_db' AND jobname LIKE 'rvbbit%'
   AND jobname <> 'rvbbit_sync' AND jobname NOT LIKE 'rvbbit_alert_%'
   AND command !~* 'rvbbit\.(run_sync|alert_sweep|alert_worker_tick)';
SQL
then
  echo "rvbbit: hosted Scheduled Tasks are present and enabled"
else
  echo "rvbbit: could not seed hosted Scheduled Tasks; add them from DataRabbit Scheduler"
fi

exit 0
