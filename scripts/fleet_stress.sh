#!/usr/bin/env bash
# fleet_stress — hammer the read fleet with shaped, repeatable load and let
# the breadcrumbs tell the story. Companion to the Adaptive Routing UI's
# exact-query pin: EXACT mode repeats identical statements (stable
# query_hash → click the SQL in Recent Executions to filter every panel to
# it, apples-to-apples across engines/nodes/concurrency); UNIQUE mode salts
# literals per iteration to defeat the result cache and exercise routing.
#
# Portable on purpose: bash + psql, no other deps — runs on the brain box,
# in the postgres container, or anywhere that can reach the DSN.
#
#   ./scripts/fleet_stress.sh "host=... dbname=rvbbit user=postgres" \
#       [-c "1 4 8 16"]   concurrency ladder            (default "1 4 8")
#   [-i 12]               iterations per worker rung    (default 12)
#   [-m exact|unique|both]                              (default both)
#   [-t traffic_violations] target table                (default traffic_violations)
#   [--hare]              add a hare_run phase per rung (needs rvbbit.hare_endpoint)
#
# Output: phase timestamps + rvbbit.brain_pressure() per rung (correlate the
# spikes), and a closing distribution summary. The real artifact is what
# lands in route_executions / hare_invocations — open Adaptive Routing with
# the window set to cover the run.
set -euo pipefail

DSN="${1:?usage: fleet_stress.sh <dsn> [-c \"1 4 8\"] [-i 12] [-m both] [-t table] [--hare]}"
shift
CONC="1 4 8"
ITERS=12
MODE="both"
TABLE="traffic_violations"
HARE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c) CONC="$2"; shift 2 ;;
    -i) ITERS="$2"; shift 2 ;;
    -m) MODE="$2"; shift 2 ;;
    -t) TABLE="$2"; shift 2 ;;
    --hare) HARE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

q() { psql "$DSN" -X -Atq -c "$1"; }

# Shapes over a wide ~2M-row table. S = filtered count (native territory),
# M = 1-col aggregate (engine-eligible), L = 2-col aggregate + distinct,
# XL = 3-col rollup + ilike (the outlier a hare should someday inherit).
# $SALT is empty in EXACT mode (stable hash) or per-iteration in UNIQUE mode
# (always inside a literal so the SHAPE stays identical either way).
shape_sql() {
  local shape="$1" salt="$2"
  case "$shape" in
    S)  echo "SELECT count(*) FROM ${TABLE} WHERE latitude > 39.0${salt:-1}" ;;
    M)  echo "SELECT violation_type, count(*) FROM ${TABLE} WHERE longitude < -76.9${salt:-1} GROUP BY 1" ;;
    L)  echo "SELECT violation_type, vehicletype, count(*) AS n, count(DISTINCT arrest_type) AS a FROM ${TABLE} WHERE latitude > 38.8${salt:-1} GROUP BY 1,2 ORDER BY n DESC LIMIT 25" ;;
    XL) echo "SELECT violation_type, vehicletype, arrest_type, count(*) AS n FROM ${TABLE} WHERE description ILIKE '%speed${salt:+_}%' OR latitude > 38.${salt:-1} GROUP BY 1,2,3 ORDER BY n DESC LIMIT 50" ;;
  esac
}

pressure() { q "SELECT rvbbit.brain_pressure()" | head -c 400; }

run_rung() {
  local mode="$1" workers="$2"
  echo "── ${mode} · ${workers} parallel × ${ITERS} iters · $(date -u '+%H:%M:%S')"
  echo "   pressure(before): $(pressure)"
  local t0=$EPOCHSECONDS
  for w in $(seq 1 "$workers"); do
    (
      for i in $(seq 1 "$ITERS"); do
        local_salt=""
        [[ "$mode" == "unique" ]] && local_salt="$(( (w * 100 + i) % 97 ))"
        for shape in S M L XL; do
          psql "$DSN" -X -Atq -c "$(shape_sql "$shape" "$local_salt")" >/dev/null 2>&1 || true
        done
      done
    ) &
  done
  wait
  local dt=$(( EPOCHSECONDS - t0 ))
  local total=$(( workers * ITERS * 4 ))
  echo "   ${total} queries in ${dt}s ($(( dt > 0 ? total / dt : total )) q/s) · pressure(after): $(pressure)"
  if [[ "$HARE" == "1" ]]; then
    echo "   hare phase: ${workers} calls"
    for w in $(seq 1 "$workers"); do
      psql "$DSN" -X -Atq -c "SELECT (rvbbit.hare_run(\$q\$$(shape_sql M "")\$q\$))->>'ok'" >/dev/null 2>&1 || true &
    done
    wait
  fi
}

echo "fleet_stress → ${TABLE} · rungs: ${CONC} · mode: ${MODE} · started $(date -u '+%Y-%m-%d %H:%M:%SZ')"
for workers in $CONC; do
  [[ "$MODE" == "exact"  || "$MODE" == "both" ]] && run_rung exact  "$workers"
  [[ "$MODE" == "unique" || "$MODE" == "both" ]] && run_rung unique "$workers"
done

echo
echo "── distribution (this run's window) ──"
psql "$DSN" -X -c "
SELECT coalesce(node,'brain') AS placement, candidate,
       coalesce(executed_engine,'(local)') AS ran_on, count(*) AS n,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms)::numeric,1) AS p50_ms,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms)::numeric,1) AS p95_ms
FROM rvbbit.route_executions
WHERE executed_at > now() - interval '30 min' AND status = 'ok'
GROUP BY 1,2,3 ORDER BY n DESC;"
echo "── pin these hashes in Adaptive Routing for apples-to-apples ──"
psql "$DSN" -X -c "
SELECT query_hash, count(*) AS runs, count(DISTINCT coalesce(node,'brain')) AS placements,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms)::numeric,1) AS p50_ms
FROM rvbbit.route_executions
WHERE executed_at > now() - interval '30 min' AND status = 'ok'
GROUP BY 1 HAVING count(*) >= 8 ORDER BY runs DESC LIMIT 8;"
