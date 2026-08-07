#!/bin/bash
# First-boot Scheduler seeds. These jobs are visible immediately in DataRabbit
# but remain disabled until an operator opts in. pg_cron lives in its home
# database (normally postgres) while jobs target POSTGRES_DB where rvbbit lives.

set -uo pipefail

CRON_DB="${RVBBIT_CRON_DATABASE:-postgres}"
TARGET_DB="${POSTGRES_DB:-rvbbit}"

echo "rvbbit: seeding paused Scheduled Tasks in ${CRON_DB} for ${TARGET_DB}"

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

UPDATE cron.job
   SET active=false
 WHERE jobname='rvbbit_calliope_dreams'
   AND database=:'target_db';
SQL
then
  echo "rvbbit: Calliope Dreaming is present in Scheduled Tasks (paused by default)"
else
  echo "rvbbit: could not seed the paused Calliope Dreaming task; add it from DataRabbit Scheduler"
fi

exit 0
