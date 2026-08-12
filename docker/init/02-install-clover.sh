#!/bin/bash
# First-boot Clover auto-install: when the container starts with
# RVBBIT_CLOVER_KEY set, fetch the current managed-operator install from the
# docs site and apply it — one `docker run -e RVBBIT_CLOVER_KEY=...` yields a
# Postgres where semantic SQL just works. The extension also ships a known-good
# catalog snapshot, so offline/airgapped boots can install that cached version.
set -uo pipefail

if [ -z "${RVBBIT_CLOVER_KEY:-}" ]; then
  echo "rvbbit: RVBBIT_CLOVER_KEY not set — skipping Clover operator install"
  exit 0
fi

CLOVER_INSTALL_URL="${RVBBIT_CLOVER_INSTALL_URL:-https://rvbbit.ai/clover-install.sql}"
CLOVER_INSTALL_SOURCE="${RVBBIT_CLOVER_INSTALL_SOURCE:-live}"

install_shipped_clover() {
  psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
DO $cached_clover$
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
$cached_clover$;

SELECT step
FROM rvbbit.capability_catalog c
CROSS JOIN LATERAL jsonb_array_elements_text(c.manifest #> '{managed,install,sql}')
  WITH ORDINALITY AS s(step, ordinal)
WHERE c.id = 'managed/clover'
  AND c.active
ORDER BY ordinal
\gexec
SQL
}

if [ "$CLOVER_INSTALL_SOURCE" = "shipped" ]; then
  echo "rvbbit: RVBBIT_CLOVER_KEY present — installing pinned shipped Clover snapshot"
  if install_shipped_clover; then
    echo "rvbbit: Clover operators installed from shipped snapshot"
  else
    echo "rvbbit: shipped Clover install failed"
    exit 1
  fi
elif [ "$CLOVER_INSTALL_SOURCE" = "live" ]; then
  echo "rvbbit: RVBBIT_CLOVER_KEY present — installing Clover operators from ${CLOVER_INSTALL_URL}"
  if curl -fsSL --max-time 30 "$CLOVER_INSTALL_URL" \
    | psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"; then
    echo "rvbbit: Clover operators installed from live catalog"
  else
    echo "rvbbit: live Clover install unavailable — applying shipped catalog snapshot"
    if install_shipped_clover; then
      echo "rvbbit: Clover operators installed from shipped snapshot"
    else
      echo "rvbbit: Clover install failed from both live and shipped catalogs — run manually:"
      echo "  curl -fsSL ${CLOVER_INSTALL_URL} | psql \$DSN"
      exit 1
    fi
  fi
else
  echo "rvbbit: unsupported RVBBIT_CLOVER_INSTALL_SOURCE=${CLOVER_INSTALL_SOURCE} (expected live or shipped)"
  exit 1
fi
