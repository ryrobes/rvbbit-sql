#!/usr/bin/env bash
set -Eeuo pipefail

sample_root=/opt/rvbbit-samples
oracle_host=${ORACLE_HOST:-sample-oracle}
oracle_service=${ORACLE_SERVICE:-FREEPDB1}
oracle_password=${ORACLE_PASSWORD:?ORACLE_PASSWORD is required}
reader_password=${ORACLE_READER_PASSWORD:?ORACLE_READER_PASSWORD is required}
co_password=Rvbbit_CO_Sample_2026!
sh_password=Rvbbit_SH_Sample_2026!
system_connect="system/\"${oracle_password}\"@//${oracle_host}:1521/${oracle_service}"
sh_connect="sh/\"${sh_password}\"@//${oracle_host}:1521/${oracle_service}"
reader_connect="mirror_reader/\"${reader_password}\"@//${oracle_host}:1521/${oracle_service}"

mark_ready() {
  touch /tmp/rvbbit-oracle-fixture-ready
  echo "$1"
  exec sleep infinity
}

database_ready=false
for _attempt in $(seq 1 60); do
  if sqlplus -L -s "${system_connect}" <<'SQL' >/dev/null 2>&1
WHENEVER SQLERROR EXIT FAILURE
SELECT 1 FROM dual;
EXIT SUCCESS
SQL
  then
    database_ready=true
    break
  fi
  sleep 2
done
if [[ "${database_ready}" != true ]]; then
  echo "Oracle fixture database did not accept connections in time" >&2
  exit 1
fi

fixture_status=$(
  sqlplus -L -s "${system_connect}" <<'SQL' 2>/dev/null || true
SET HEADING OFF FEEDBACK OFF PAGESIZE 0 VERIFY OFF ECHO OFF
WHENEVER SQLERROR EXIT FAILURE
SELECT CASE
  WHEN (SELECT COUNT(*) FROM co.customers) = 392
   AND (SELECT COUNT(*) FROM co.orders) = 1950
   AND (SELECT COUNT(*) FROM co.order_items) = 3914
   AND (SELECT COUNT(*) FROM sh.customers) = 55500
   AND (SELECT COUNT(*) FROM sh.costs) = 82112
   AND (SELECT COUNT(*) FROM sh.sales) = 918843
   AND (SELECT COUNT(*) FROM all_users WHERE username = 'MIRROR_READER') = 1
  THEN 'RVBBIT_ORACLE_FIXTURE_READY'
  ELSE 'RVBBIT_ORACLE_FIXTURE_STALE'
END
FROM dual;
EXIT SUCCESS
SQL
)
if grep -qx '[[:space:]]*RVBBIT_ORACLE_FIXTURE_READY[[:space:]]*' <<<"${fixture_status}"; then
  mark_ready "Oracle CO and SH fixtures already match the pinned sample revision"
fi

echo "Installing pinned Oracle CO and SH sample schemas"
sqlplus -L -s "${system_connect}" <<SQL
SET ECHO OFF VERIFY OFF FEEDBACK ON
WHENEVER SQLERROR EXIT SQL.SQLCODE

BEGIN
  FOR username IN (
    SELECT column_value
    FROM TABLE(sys.odcivarchar2list('MIRROR_READER', 'CO', 'SH'))
  ) LOOP
    BEGIN
      EXECUTE IMMEDIATE 'DROP USER ' || username.column_value || ' CASCADE';
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLCODE != -1918 THEN RAISE; END IF;
    END;
  END LOOP;
END;
/

CREATE USER co IDENTIFIED BY "${co_password}"
  DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;
GRANT CREATE MATERIALIZED VIEW, CREATE PROCEDURE, CREATE SEQUENCE,
      CREATE SESSION, CREATE SYNONYM, CREATE TABLE, CREATE TRIGGER,
      CREATE TYPE, CREATE VIEW TO co;
ALTER SESSION SET CURRENT_SCHEMA=CO;
ALTER SESSION SET NLS_LANGUAGE=American;
ALTER SESSION SET NLS_TERRITORY=America;
@${sample_root}/customer_orders/co_create.sql
@${sample_root}/customer_orders/co_populate.sql

ALTER SESSION SET CURRENT_SCHEMA=SYSTEM;
CREATE USER sh IDENTIFIED BY "${sh_password}"
  DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;
GRANT CREATE MATERIALIZED VIEW, CREATE DIMENSION, CREATE PROCEDURE,
      CREATE SEQUENCE, CREATE SESSION, CREATE SYNONYM, CREATE TABLE,
      CREATE TRIGGER, CREATE TYPE, CREATE VIEW TO sh;
ALTER SESSION SET CURRENT_SCHEMA=SH;
ALTER SESSION SET NLS_LANGUAGE=American;
ALTER SESSION SET NLS_TERRITORY=America;
@${sample_root}/sales_history/sh_create.sql
@${sample_root}/sales_history/sh_populate_before_load.sql
EXIT SUCCESS
SQL

for table in costs customers promotions sales times supplementary_demographics; do
  log_file="/tmp/rvbbit-oracle-${table}.log"
  bad_file="/tmp/rvbbit-oracle-${table}.bad"
  if ! sqlldr userid="${sh_connect}" \
      control="${sample_root}/loader/${table}.ctl" \
      log="${log_file}" bad="${bad_file}" \
      direct=true skip=1 errors=0 rows=50000 silent=header; then
    echo "SQL*Loader failed for SH.${table}" >&2
    tail -80 "${log_file}" >&2 || true
    exit 1
  fi
done

sqlplus -L -s "${system_connect}" <<SQL
SET ECHO OFF VERIFY OFF FEEDBACK ON SERVEROUTPUT ON
WHENEVER SQLERROR EXIT SQL.SQLCODE
ALTER SESSION SET CURRENT_SCHEMA=SH;
@${sample_root}/sales_history/sh_populate_after_load.sql

BEGIN
  dbms_mview.refresh('SH.CAL_MONTH_SALES_MV', 'C');
  dbms_mview.refresh('SH.FWEEK_PSCAT_SALES_MV', 'C');
END;
/

ALTER SESSION SET CURRENT_SCHEMA=SYSTEM;
CREATE USER mirror_reader IDENTIFIED BY "${reader_password}";
GRANT CREATE SESSION TO mirror_reader;
BEGIN
  FOR relation IN (
    SELECT DISTINCT owner, object_name
    FROM all_objects
    WHERE owner IN ('CO', 'SH')
      AND object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')
  ) LOOP
    EXECUTE IMMEDIATE
      'GRANT SELECT ON "' || relation.owner || '"."' ||
      relation.object_name || '" TO MIRROR_READER';
  END LOOP;
END;
/
ALTER USER co ACCOUNT LOCK;
ALTER USER sh ACCOUNT LOCK;

DECLARE
  actual NUMBER;
  PROCEDURE assert_count(owner_name VARCHAR2, table_name VARCHAR2, expected NUMBER) IS
  BEGIN
    EXECUTE IMMEDIATE
      'SELECT COUNT(*) FROM "' || owner_name || '"."' || table_name || '"'
      INTO actual;
    IF actual != expected THEN
      raise_application_error(
        -20001,
        owner_name || '.' || table_name || ' expected ' || expected ||
        ' rows but found ' || actual
      );
    END IF;
  END;
BEGIN
  assert_count('CO', 'CUSTOMERS', 392);
  assert_count('CO', 'ORDERS', 1950);
  assert_count('CO', 'ORDER_ITEMS', 3914);
  assert_count('SH', 'CUSTOMERS', 55500);
  assert_count('SH', 'COSTS', 82112);
  assert_count('SH', 'SALES', 918843);
END;
/
EXIT SUCCESS
SQL

reader_status=$(
  sqlplus -L -s "${reader_connect}" <<'SQL'
SET HEADING OFF FEEDBACK OFF PAGESIZE 0 VERIFY OFF ECHO OFF
WHENEVER SQLERROR EXIT FAILURE
SELECT CASE
  WHEN (SELECT COUNT(*) FROM co.orders) = 1950
   AND (SELECT COUNT(*) FROM sh.sales) = 918843
   AND (SELECT COUNT(*) FROM session_privs WHERE privilege <> 'CREATE SESSION') = 0
  THEN 'RVBBIT_ORACLE_READER_READY'
  ELSE 'RVBBIT_ORACLE_READER_INVALID'
END
FROM dual;
EXIT SUCCESS
SQL
)
if ! grep -qx '[[:space:]]*RVBBIT_ORACLE_READER_READY[[:space:]]*' <<<"${reader_status}"; then
  echo "Oracle fixture reader validation failed" >&2
  exit 1
fi

mark_ready "Oracle CO and SH fixtures are ready for read-only mirroring"
