#!/usr/bin/env bash
set -euo pipefail

dsn="${RVBBIT_DSN:-postgresql://postgres:${POSTGRES_PASSWORD:-rvbbit}@postgres:5432/${POSTGRES_DB:-rvbbit}}"
warren_node="${WARREN_NODE:-compose-warren}"
target_selector="${RVBBIT_UBER_TARGET_SELECTOR:-}"
if [[ -z "$target_selector" ]]; then
    target_selector='{"capability":true,"docker":true,"gpu":false}'
fi
capabilities_csv="${RVBBIT_UBER_BOOTSTRAP_CAPABILITIES:-smoke/warren-echo,runtimes/python-runtime,runtimes/mcp-gateway,data/dlt-mirror}"
timeout_seconds="${RVBBIT_UBER_BOOTSTRAP_TIMEOUT_SECONDS:-600}"
poll_seconds="${RVBBIT_UBER_BOOTSTRAP_POLL_SECONDS:-2}"
clover_required="${RVBBIT_CLOVER_REQUIRED:-false}"
clover_verify_remote="${RVBBIT_CLOVER_VERIFY_REMOTE:-false}"
clover_install_source="${RVBBIT_CLOVER_INSTALL_SOURCE:-live}"
clover_openai_base_url="${RVBBIT_CLOVER_OPENAI_BASE_URL:-https://clover.rvbb.it/v1}"
clover_required_model="${RVBBIT_CLOVER_REQUIRED_MODEL:-}"
hosted_services="${RVBBIT_HOSTED_SERVICES:-false}"
hindsight_endpoint="${RVBBIT_HINDSIGHT_ENDPOINT:-http://hindsight:8888}"
lens_connections_path="${RVBBIT_LENS_CONNECTIONS_PATH:-}"
lens_bootstrap_connection="${RVBBIT_LENS_BOOTSTRAP_CONNECTION:-true}"
lens_connection_id="${RVBBIT_LENS_CONNECTION_ID:-rvbbit-uber}"
lens_connection_label="${RVBBIT_LENS_CONNECTION_LABEL:-Rvbbit Uber}"
lens_connection_host="${RVBBIT_LENS_CONNECTION_HOST:-postgres}"
lens_connection_port="${RVBBIT_LENS_CONNECTION_PORT:-5432}"
lens_connection_database="${RVBBIT_LENS_CONNECTION_DATABASE:-${POSTGRES_DB:-rvbbit}}"
lens_connection_user="${RVBBIT_LENS_CONNECTION_USER:-postgres}"
lens_connection_password="${RVBBIT_LENS_CONNECTION_PASSWORD:-${POSTGRES_PASSWORD:-rvbbit}}"
lens_connection_ssl_mode="${RVBBIT_LENS_CONNECTION_SSL_MODE:-disable}"
lens_connection_file_uid="${RVBBIT_LENS_CONNECTION_FILE_UID:-1001}"
lens_connection_file_gid="${RVBBIT_LENS_CONNECTION_FILE_GID:-1001}"

log() {
    printf '[rvbbit-uber-bootstrap] %s\n' "$*"
}

psql_scalar() {
    psql "$dsn" -X -v ON_ERROR_STOP=1 -Atq "$@"
}

wait_sql_true() {
    local label="$1"
    local sql="$2"
    local deadline=$((SECONDS + timeout_seconds))
    while (( SECONDS < deadline )); do
        if [[ "$(psql_scalar -c "$sql" 2>/dev/null || true)" == "t" ]]; then
            log "$label ready"
            return 0
        fi
        sleep "$poll_seconds"
    done
    log "$label did not become ready within ${timeout_seconds}s"
    return 1
}

wait_warren_node() {
    local deadline=$((SECONDS + timeout_seconds))
    while (( SECONDS < deadline )); do
        if [[ "$(
            psql "$dsn" -X -v ON_ERROR_STOP=1 -Atq -v warren_node="$warren_node" <<'SQL' 2>/dev/null || true
SELECT EXISTS (
  SELECT 1
  FROM rvbbit.warren_node_effective_status
  WHERE name = :'warren_node'
    AND is_eligible
);
SQL
        )" == "t" ]]; then
            log "Warren node $warren_node ready"
            return 0
        fi
        sleep "$poll_seconds"
    done
    log "Warren node $warren_node did not become ready within ${timeout_seconds}s"
    return 1
}

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

is_true() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

bootstrap_lens_connection() {
    if ! is_true "$lens_bootstrap_connection"; then
        log "Lens default connection bootstrap disabled"
        return 0
    fi
    if [[ -z "$lens_connections_path" ]]; then
        return 0
    fi
    if [[ -s "$lens_connections_path" ]]; then
        log "Lens connections file already exists; skipping default connection seed"
        return 0
    fi

    log "seeding Lens default connection"
    mkdir -p "$(dirname "$lens_connections_path")"
    local tmp="${lens_connections_path}.tmp"
    local now
    now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

    psql "$dsn" -X -v ON_ERROR_STOP=1 -Atq \
        -v conn_id="$lens_connection_id" \
        -v conn_label="$lens_connection_label" \
        -v conn_host="$lens_connection_host" \
        -v conn_port="$lens_connection_port" \
        -v conn_database="$lens_connection_database" \
        -v conn_user="$lens_connection_user" \
        -v conn_password="$lens_connection_password" \
        -v conn_ssl_mode="$lens_connection_ssl_mode" \
        -v conn_now="$now" <<'SQL' > "$tmp"
SELECT jsonb_pretty(jsonb_build_object(
  'version', 1,
  'connections', jsonb_build_array(jsonb_build_object(
    'id', :'conn_id',
    'label', :'conn_label',
    'host', :'conn_host',
    'port', :'conn_port'::int,
    'database', :'conn_database',
    'user', :'conn_user',
    'password', :'conn_password',
    'sslMode', :'conn_ssl_mode',
    'isDefault', true,
    'createdAt', :'conn_now',
    'updatedAt', :'conn_now'
  ))
));
SQL
    chmod 0600 "$tmp"
    chown "${lens_connection_file_uid}:${lens_connection_file_gid}" "$tmp" 2>/dev/null || true
    mv "$tmp" "$lens_connections_path"
    log "Lens default connection seeded"
}

install_managed_clover() {
    if [[ -z "${RVBBIT_CLOVER_KEY:-}" ]]; then
        if is_true "$clover_required"; then
            log "RVBBIT_CLOVER_KEY is required by this appliance but is unset"
            return 1
        fi
        log "RVBBIT_CLOVER_KEY is unset; managed Clover install skipped"
        return 0
    fi

    if [[ "$(psql_scalar -c "SELECT rvbbit.env_present('RVBBIT_CLOVER_KEY');")" != "t" ]]; then
        log "RVBBIT_CLOVER_KEY is not visible to the Postgres extension process"
        return 1
    fi

    if [[ "$clover_install_source" == "shipped" ]]; then
        # seed_capability_catalog() deliberately preserves URL catalog rows once
        # they exist, because a normal extension migration must not replace a
        # newer live import. A hosted appliance is different: its image is the
        # pin, so refresh this one row from files in the running Postgres image
        # before applying it. This also upgrades existing Docker volumes.
        log "refreshing managed/clover from the pinned image snapshot"
        psql "$dsn" -X -v ON_ERROR_STOP=1 <<'SQL'
DO $shipped_clover$
DECLARE
    shipped_entry jsonb;
    shipped_manifest jsonb;
    shipped_source text;
BEGIN
    SELECT entry
    INTO shipped_entry
    FROM jsonb_array_elements(
        (pg_read_file('/usr/share/rvbbit/capabilities/catalog.json')::jsonb)->'capabilities'
    ) AS catalog(entry)
    WHERE entry->>'id' = 'managed/clover';

    shipped_manifest := pg_read_file(
        '/usr/share/rvbbit/capabilities/packs/managed/clover/capability.json'
    )::jsonb;

    IF shipped_entry IS NULL OR shipped_manifest IS NULL THEN
        RAISE EXCEPTION 'managed/clover is missing from the pinned image snapshot';
    END IF;

    shipped_source := 'url:' || regexp_replace(
        shipped_entry->>'catalog_url',
        '^https?://',
        ''
    );
    PERFORM rvbbit.upsert_capability_catalog_entry(
        catalog_entry => shipped_entry,
        capability_manifest => shipped_manifest,
        catalog_source => shipped_source,
        entry_active => true
    );
END
$shipped_clover$;
SQL
    elif [[ "$clover_install_source" != "live" ]]; then
        log "unsupported RVBBIT_CLOVER_INSTALL_SOURCE=$clover_install_source (expected live or shipped)"
        return 1
    fi

    # The hosted appliance installs from the catalog embedded in its pinned
    # Postgres image. Reapplying it is idempotent and also upgrades existing
    # volumes, where docker-entrypoint-initdb.d is intentionally not rerun.
    log "installing managed/clover from the shipped capability snapshot"
    psql "$dsn" -X -v ON_ERROR_STOP=1 <<'SQL'
DO $managed_clover$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM rvbbit.capability_catalog
    WHERE id = 'managed/clover'
      AND active
      AND jsonb_typeof(manifest #> '{managed,install,sql}') = 'array'
      AND jsonb_array_length(manifest #> '{managed,install,sql}') > 0
  ) THEN
    RAISE EXCEPTION 'managed/clover shipped snapshot is unavailable';
  END IF;
END
$managed_clover$;

SELECT step
FROM rvbbit.capability_catalog c
CROSS JOIN LATERAL jsonb_array_elements_text(c.manifest #> '{managed,install,sql}')
  WITH ORDINALITY AS s(step, ordinal)
WHERE c.id = 'managed/clover'
  AND c.active
ORDER BY ordinal
\gexec
SQL

    if [[ "$(psql_scalar -c "SELECT EXISTS (SELECT 1 FROM rvbbit.backends WHERE name='embed' AND auth_header_env='RVBBIT_CLOVER_KEY') AND EXISTS (SELECT 1 FROM rvbbit.backends WHERE name='clover_llm' AND auth_header_env='RVBBIT_CLOVER_KEY') AND EXISTS (SELECT 1 FROM rvbbit.backends WHERE name='forecast' AND auth_header_env='RVBBIT_CLOVER_KEY') AND EXISTS (SELECT 1 FROM rvbbit.operators WHERE name='clover_llm_ask') AND EXISTS (SELECT 1 FROM rvbbit.operators WHERE name='clover_forecast') AND to_regprocedure('rvbbit.clover_forecast(text,text,jsonb)') IS NOT NULL;")" != "t" ]]; then
        log "managed Clover registration verification failed"
        return 1
    fi

    if is_true "$clover_verify_remote"; then
        local embed_ok models_payload compact_models
        embed_ok="$(psql_scalar -c "SELECT coalesce((rvbbit.backend_probe('embed')->>'ok')::boolean, false);")"
        if [[ "$embed_ok" != "t" ]]; then
            log "Clover embed backend probe failed"
            return 1
        fi
        if [[ -n "$clover_required_model" ]]; then
            models_payload="$(
                curl -fsS --connect-timeout 10 --max-time 30 \
                    -H "Authorization: Bearer ${RVBBIT_CLOVER_KEY}" \
                    "${clover_openai_base_url%/}/models"
            )" || {
                log "Clover model-list probe failed"
                return 1
            }
            compact_models="$(printf '%s' "$models_payload" | tr -d '[:space:]')"
            if ! grep -Fq "\"id\":\"${clover_required_model}\"" <<< "$compact_models"; then
                log "required Clover model '$clover_required_model' is not available to this key"
                return 1
            fi
        fi
        log "managed Clover key, embeddings, and model entitlement verified"
    else
        log "managed Clover registration verified"
    fi
}

refresh_hosted_capability_index() {
    if ! is_true "$hosted_services"; then
        return 0
    fi
    log "refreshing hosted capability index"
    psql_scalar -c "SELECT rvbbit.capability_crawl();" >/dev/null
    if [[ -n "${RVBBIT_CLOVER_KEY:-}" ]] &&
       [[ "$(psql_scalar -c "SELECT EXISTS (SELECT 1 FROM rvbbit.catalog_docs WHERE graph_id='rvbbit_capabilities' AND kind='cap_operator' AND rel_name='clover_forecast' AND doc LIKE '%signature: rvbbit.clover_forecast(%');")" != "t" ]]; then
        log "managed Clover capability discovery verification failed"
        return 1
    fi
    log "hosted capability index verified"
}

configure_hosted_clover_defaults() {
    if ! is_true "$hosted_services"; then
        return 0
    fi
    # Preserve the generic uber stack's optional-Clover behavior. The hosted
    # Calliope compose makes this key mandatory, so reaching this branch there
    # is already prevented by install_managed_clover().
    if [[ -z "${RVBBIT_CLOVER_KEY:-}" ]]; then
        log "RVBBIT_CLOVER_KEY is unset; hosted Clover defaults skipped"
        return 0
    fi

    # Document Brain discovers NER support through the canonical
    # rvbbit.extract_entities operator. The managed Clover capability ships
    # both the hosted GLiNER-large backend and an explicit binding helper, but
    # installing the capability alone deliberately does not replace an OSS
    # install's local GLiNER choice. A hosted Calliope appliance is the place
    # where Clover is the known deployment default, so bind it here.
    log "binding Document Brain entity extraction to managed Clover"
    psql_scalar -c "SELECT rvbbit.bind_extract_entities_to_clover();" >/dev/null

    if [[ "$(psql_scalar -c "SELECT EXISTS (
        SELECT 1
        FROM rvbbit.operators
        WHERE name = 'extract_entities'
          AND steps @> '[{\"kind\":\"specialist\",\"specialist\":\"extract\"}]'::jsonb
    ) AND to_regprocedure('rvbbit.extract_entities(text,text,jsonb)') IS NOT NULL;")" != "t" ]]; then
        log "hosted Clover Document Brain binding verification failed"
        return 1
    fi
    log "Document Brain entity extraction uses managed Clover"
}

prepare_hosted_services() {
    if ! is_true "$hosted_services"; then
        return 0
    fi

    log "preparing hosted Hindsight schema and service registration"
    psql "$dsn" -X -v ON_ERROR_STOP=1 -v hindsight_endpoint="$hindsight_endpoint" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
DO $pg_trgm_schema$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_extension e
    JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'pg_trgm'
      AND n.nspname <> 'public'
  ) THEN
    EXECUTE 'ALTER EXTENSION pg_trgm SET SCHEMA public';
  END IF;
END
$pg_trgm_schema$;
CREATE SCHEMA IF NOT EXISTS hindsight;
SELECT rvbbit.register_memory_service(
  service_name => 'hindsight_default',
  endpoint_url => :'hindsight_endpoint',
  service_provider => 'hindsight',
  service_status => 'ready',
  auth_header_env => NULL,
  service_labels => '{"agent_memory":true,"deployment":"calliope"}'::jsonb,
  service_source => 'compose',
  install_manifest => '{"runtime":"pinned-external","database_schema":"hindsight"}'::jsonb,
  health => '{"configured":true}'::jsonb,
  set_default => true
);
SQL
}

configure_hosted_schedules() {
    if ! is_true "$hosted_services"; then
        return 0
    fi

    local cron_db="${RVBBIT_CRON_DATABASE:-postgres}"
    local target_db="${POSTGRES_DB:-rvbbit}"
    log "installing hosted RVBBIT schedules in ${cron_db} for ${target_db}"
    psql "$dsn" -X -v ON_ERROR_STOP=1 \
        -v cron_db="$cron_db" -v target_db="$target_db" <<'SQL'
\connect :cron_db
CREATE EXTENSION IF NOT EXISTS pg_cron;

SELECT cron.schedule_in_database('rvbbit_calliope_dreams','0 3 * * *',
    'SELECT rvbbit.calliope_dream_enqueue(''cron'',''calliope@system'',false);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_calliope_dreams' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_catalog_refresh','0 3 * * *',
    'CALL rvbbit.catalog_crawl_run();',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_catalog_refresh' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_olap_autopilot','* * * * *',
    'SELECT rvbbit.accel_tick(4);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_olap_autopilot' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_layout_tick_worker_1','* * * * *',
    'CALL rvbbit.layout_tick_worker_pass(1, 1, 1);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_layout_tick_worker_1' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_accel_observer','7 * * * *',
    'SELECT rvbbit.accel_autopilot_observe(''scheduler'');',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_accel_observer' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit-maintain','*/15 * * * *',
    'SELECT rvbbit.maintain();',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit-maintain' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit-storage-maintain','0 * * * *',
    'SELECT rvbbit.maintain(storage_tables => 2);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit-storage-maintain' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_materialize_all','0 * * * *',
    'SELECT rvbbit.materialize_all_metrics();',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_materialize_all' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_refresh_cubes','0 */2 * * *',
    'CALL rvbbit.refresh_all_cubes();',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_refresh_cubes' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_route_optimize','0 5 * * *',
    'SELECT rvbbit.route_optimize_auto(20, 600, 3);',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_route_optimize' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_brain_sync','0 2 * * *',
    'CALL rvbbit.brain_update_drain(''auto'');',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_brain_sync' AND database=:'target_db');
SELECT cron.schedule_in_database('rvbbit_brain_enrich','*/5 * * * *',
    'CALL rvbbit.brain_enrich_drain(NULL, 20, 0, 270, ''cron'');',:'target_db')
WHERE NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname='rvbbit_brain_enrich' AND database=:'target_db');

-- Hosted appliances run the built-in RVBBIT automation by default. Preserve
-- the two intentionally opt-in families: direct postgres temporal mirroring
-- and alert/action dispatch.
UPDATE cron.job
   SET active = true
 WHERE database = :'target_db'
   AND jobname LIKE 'rvbbit%'
   AND jobname <> 'rvbbit_sync'
   AND jobname NOT LIKE 'rvbbit_alert_%'
   AND command !~* 'rvbbit\.(run_sync|alert_sweep|alert_worker_tick)';
SQL
}

capability_ready_sql() {
    case "$1" in
        smoke/warren-echo)
            cat <<'SQL'
SELECT EXISTS (
  SELECT 1
  FROM rvbbit.warren_inventory
  WHERE backend_name = 'warren_smoke_echo'
    AND deployment_status = 'running'
);
SQL
            ;;
        runtimes/python-runtime)
            cat <<'SQL'
SELECT EXISTS (
  SELECT 1
  FROM rvbbit.python_runtimes r
  WHERE r.name = 'python_default'
    AND r.status = 'ready'
    AND r.runtime_source = 'warren'
) AND EXISTS (
  SELECT 1
  FROM rvbbit.warren_inventory
  WHERE runtime_name = 'python_default'
    AND deployment_status = 'running'
);
SQL
            ;;
        runtimes/mcp-gateway)
            cat <<'SQL'
SELECT EXISTS (
  SELECT 1
  FROM rvbbit.mcp_gateways g
  WHERE g.name = 'mcp_default'
    AND g.status = 'ready'
    AND g.gateway_source = 'warren'
) AND EXISTS (
  SELECT 1
  FROM rvbbit.warren_inventory
  WHERE runtime_name = 'mcp_default'
    AND deployment_status = 'running'
);
SQL
            ;;
        data/dlt-mirror)
            cat <<'SQL'
SELECT EXISTS (
  SELECT 1
  FROM rvbbit.python_runtimes r
  WHERE r.name = 'dlt_mirror'
    AND r.language = 'data_mover'
    AND r.status = 'ready'
    AND r.runtime_source = 'warren'
) AND EXISTS (
  SELECT 1
  FROM rvbbit.warren_inventory
  WHERE runtime_name = 'dlt_mirror'
    AND deployment_status = 'running'
);
SQL
            ;;
        *)
            return 1
            ;;
    esac
}

capability_ready() {
    local capability="$1"
    local sql
    if ! sql="$(capability_ready_sql "$capability")"; then
        return 1
    fi
    [[ "$(psql_scalar -c "$sql")" == "t" ]]
}

deploy_capability() {
    local capability="$1"
    local job_name="uber-${capability//\//-}"
    local job_id

    if capability_ready "$capability"; then
        log "$capability already ready; skipping deploy"
        return 0
    fi

    log "queueing $capability"
    job_id="$(
        psql "$dsn" -X -v ON_ERROR_STOP=1 -Atq \
            -v catalog_id="$capability" \
            -v target_selector="$target_selector" \
            -v job_name="$job_name" <<'SQL'
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => :'catalog_id',
  target_selector => :'target_selector'::jsonb,
  job_name => :'job_name'
);
SQL
    )"
    log "$capability job_id=$job_id"

    local deadline=$((SECONDS + timeout_seconds))
    local status phase job_row
    while (( SECONDS < deadline )); do
        job_row="$(
            psql "$dsn" -X -v ON_ERROR_STOP=1 -Atq -v job_id="$job_id" <<'SQL'
SELECT status || E'\t' || coalesce(phase, '')
FROM rvbbit.warren_jobs
WHERE job_id = :'job_id'::uuid;
SQL
        )"
        IFS=$'\t' read -r status phase <<< "$job_row"
        case "$status" in
            completed)
                log "$capability completed"
                return 0
                ;;
            failed|cancelled)
                log "$capability failed with status=$status phase=$phase"
                psql "$dsn" -X -v ON_ERROR_STOP=1 -v job_id="$job_id" <<'SQL'
SELECT job_id, status, phase, error, jsonb_pretty(progress) AS progress, jsonb_pretty(logs) AS logs
FROM rvbbit.warren_jobs
WHERE job_id = :'job_id'::uuid;
SQL
                return 1
                ;;
        esac
        log "$capability status=$status phase=${phase:-unknown}"
        sleep "$poll_seconds"
    done

    log "$capability did not complete within ${timeout_seconds}s"
    psql "$dsn" -X -v ON_ERROR_STOP=1 -v job_id="$job_id" <<'SQL'
SELECT job_id, status, phase, error, jsonb_pretty(progress) AS progress, jsonb_pretty(logs) AS logs
FROM rvbbit.warren_jobs
WHERE job_id = :'job_id'::uuid;
SQL
    return 1
}

verify_baseline() {
    if [[ "$capabilities_csv" == *"smoke/warren-echo"* ]]; then
        psql_scalar -c "SELECT rvbbit.warren_smoke_echo('rvbbit uber bootstrap')->>'echo'" \
            | grep -Fxq "rvbbit uber bootstrap"
        log "smoke/warren-echo operator verified"
    fi
    if [[ "$capabilities_csv" == *"runtimes/python-runtime"* ]]; then
        capability_ready "runtimes/python-runtime"
        log "runtimes/python-runtime verified"
    fi
    if [[ "$capabilities_csv" == *"runtimes/mcp-gateway"* ]]; then
        capability_ready "runtimes/mcp-gateway"
        log "runtimes/mcp-gateway verified"
    fi
    if [[ "$capabilities_csv" == *"data/dlt-mirror"* ]]; then
        capability_ready "data/dlt-mirror"
        log "data/dlt-mirror verified"
    fi
}

if [[ "${RVBBIT_UBER_SKIP_BOOTSTRAP:-false}" == "true" ]]; then
    log "RVBBIT_UBER_SKIP_BOOTSTRAP=true; exiting"
    exit 0
fi

log "waiting for database"
wait_sql_true "database" "SELECT true;"

# Upgrade-safety: the initdb migrate only runs on an EMPTY volume, so a new
# image over an existing volume would otherwise never apply new migrations
# (route bindings, route_model factory seed, ...). Idempotent no-op otherwise.
log "applying schema migrations"
psql "$dsn" -X -v ON_ERROR_STOP=1 -Atq -c \
    "CREATE EXTENSION IF NOT EXISTS pg_rvbbit; SELECT rvbbit.migrate();" \
    -c "ALTER EXTENSION pg_rvbbit UPDATE" \
    | tail -1 | while read -r line; do log "migrate: $line"; done

log "seeding capability catalog"
psql_scalar -c "SELECT rvbbit.seed_capability_catalog();" >/dev/null
install_managed_clover
configure_hosted_clover_defaults
prepare_hosted_services
configure_hosted_schedules
bootstrap_lens_connection

log "waiting for Warren node '$warren_node'"
wait_warren_node

IFS=',' read -r -a capabilities <<< "$capabilities_csv"
for raw_capability in "${capabilities[@]}"; do
    capability="$(trim "$raw_capability")"
    [[ -n "$capability" ]] || continue
    deploy_capability "$capability"
done

refresh_hosted_capability_index
verify_baseline
log "baseline capabilities ready"
