#!/usr/bin/env bash
set -euo pipefail

dsn="${RVBBIT_DSN:-postgresql://postgres:${POSTGRES_PASSWORD:-rvbbit}@postgres:5432/${POSTGRES_DB:-rvbbit}}"
warren_node="${WARREN_NODE:-compose-warren}"
target_selector="${RVBBIT_UBER_TARGET_SELECTOR:-}"
if [[ -z "$target_selector" ]]; then
    target_selector='{"capability":true,"docker":true,"gpu":false}'
fi
capabilities_csv="${RVBBIT_UBER_BOOTSTRAP_CAPABILITIES:-smoke/warren-echo,runtimes/python-runtime,runtimes/mcp-gateway}"
timeout_seconds="${RVBBIT_UBER_BOOTSTRAP_TIMEOUT_SECONDS:-600}"
poll_seconds="${RVBBIT_UBER_BOOTSTRAP_POLL_SECONDS:-2}"
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
bootstrap_lens_connection

log "waiting for Warren node '$warren_node'"
wait_warren_node

IFS=',' read -r -a capabilities <<< "$capabilities_csv"
for raw_capability in "${capabilities[@]}"; do
    capability="$(trim "$raw_capability")"
    [[ -n "$capability" ]] || continue
    deploy_capability "$capability"
done

verify_baseline
log "baseline capabilities ready"
