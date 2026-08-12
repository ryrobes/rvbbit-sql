# Ingestion test sources

`docker-compose.ingestion-test.yml` supplies disposable PostgreSQL, MySQL, SQL
Server, and Oracle source systems for end-to-end dlt mirror tests. Oracle is
seeded with Oracle's official Customer Orders (`CO`) and Sales History (`SH`)
schemas. It also includes a minimal Hermes session-API double so Calliope can
create its setup notebook without a real provider key. The double reports
degraded model, gateway, and memory readiness; it must never be mistaken for a
working Hermes service.

All fixtures join the existing `rvbbit_uber` network and expose no host ports.
Database fixtures use dedicated SELECT-only principals. The fixed reader
passwords are intentionally test-only and must never be copied into a real
deployment.

The default images are locked to the multi-platform registry digests exercised
by this fixture contract. Override `SAMPLE_POSTGRES_IMAGE`,
`SAMPLE_MYSQL_IMAGE`, `SAMPLE_MSSQL_IMAGE`, or `SAMPLE_HERMES_IMAGE`
deliberately when testing a new driver/database version. The locally built
Oracle fixture pins `gvenzl/oracle-free:23.26.2-slim-faststart` by manifest
digest and verifies the official sample archive from revision
`6660bad68c07bd143430ace58565b3f727e17263` before installing it.

Start them after the appliance network exists:

```bash
docker compose -f docker/docker-compose.ingestion-test.yml up -d --wait
```

The source DSNs are:

```text
postgresql://mirror_reader:mirror_readonly@sample-postgres:5432/commerce
mysql+pymysql://mirror_reader:mirror_readonly@sample-mysql:3306/crm
mssql+pymssql://mirror_reader:Rvbbit_Mirror_Readonly_2026%21@sample-mssql:1433/operations
oracle+oracledb://mirror_reader:Rvbbit_Oracle_Readonly_2026%21@sample-oracle:1521/?service_name=FREEPDB1
```

The automated appliance smoke test keeps each remote schema visible in the
destination name: `commerce_sales_pg`, `commerce_support_pg`, `crm_mysql`,
`operations_mssql`, `oracle_co`, and `oracle_sh`. Source relation names remain
unchanged inside those schemas.

The Oracle fixture contributes 392 customers, 1,950 orders, and 3,914 order
items in `CO`; `SH` contributes 55,500 customers, 82,112 costs, and 918,843
sales rows plus its supporting dimensions and views. The initializer is
idempotent and skips the load when those exact fixture counts already exist.
The slim database image omits Oracle Text, so the optional `CTXSYS.CONTEXT`
index on `SH.SUPPLEMENTARY_DEMOGRAPHICS.COMMENTS` is the one upstream sample
object deliberately excluded; no source table, row, relationship, or view is
removed.

After the appliance and fixtures are healthy, exercise the real authenticated
Calliope setup APIs, canonical credential store, discovery, reviewed mirror
plans, loads, and normal post-setup mirror administration with:

```bash
HOSTED_SMOKE_PASSWORD='<the one-email bootstrap password>' \
  python3 scripts/hosted-appliance-smoke.py \
  --base-url http://127.0.0.1:8080 \
  --email admin@example.com
```

The harness keeps passwords and source DSNs in process memory and prints only
credential-free health, lineage, and run receipts.

For a repeatable connector-only pass against Oracle after first boot, leave the
company profile untouched and select just that fixture:

```bash
HOSTED_SMOKE_PASSWORD='<the one-email bootstrap password>' \
  python3 scripts/hosted-appliance-smoke.py \
  --base-url http://127.0.0.1:8080 \
  --email admin@example.com \
  --skip-profile \
  --fixture fixture_oracle
```

Remove all fixture containers and data with:

```bash
docker compose -f docker/docker-compose.ingestion-test.yml down --volumes
```
