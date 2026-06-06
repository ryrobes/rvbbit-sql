#!/usr/bin/env bash
set -euo pipefail

dsn="${RVBBIT_DSN:-postgresql://postgres:${POSTGRES_PASSWORD:-rvbbit}@postgres:5432/${POSTGRES_DB:-rvbbit}}"
warren_node="${WARREN_NODE:-compose-warren}"
target_selector="${RVBBIT_UBER_TARGET_SELECTOR:-{\"capability\":true,\"docker\":true,\"gpu\":false}}"
capabilities_csv="${RVBBIT_UBER_BOOTSTRAP_CAPABILITIES:-smoke/warren-echo,runtimes/python-runtime,runtimes/mcp-gateway}"
timeout_seconds="${RVBBIT_UBER_BOOTSTRAP_TIMEOUT_SECONDS:-600}"
poll_seconds="${RVBBIT_UBER_BOOTSTRAP_POLL_SECONDS:-2}"

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

log "seeding capability catalog"
psql_scalar -c "SELECT rvbbit.seed_capability_catalog();" >/dev/null

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
