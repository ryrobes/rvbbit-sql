#!/bin/bash
# First-boot Clover auto-install: when the container starts with
# RVBBIT_CLOVER_KEY set, fetch the current managed-operator install from the
# docs site and apply it — one `docker run -e RVBBIT_CLOVER_KEY=...` yields a
# Postgres where semantic SQL just works. Best-effort by design: offline or
# airgapped boots log a note and continue (re-run the curl|psql line later).
set -u

if [ -z "${RVBBIT_CLOVER_KEY:-}" ]; then
  echo "rvbbit: RVBBIT_CLOVER_KEY not set — skipping Clover operator install"
  exit 0
fi

CLOVER_INSTALL_URL="${RVBBIT_CLOVER_INSTALL_URL:-https://rvbbit.ai/clover-install.sql}"
echo "rvbbit: RVBBIT_CLOVER_KEY present — installing Clover operators from ${CLOVER_INSTALL_URL}"

if curl -fsSL --max-time 30 "$CLOVER_INSTALL_URL" \
  | psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"; then
  echo "rvbbit: Clover operators installed"
else
  echo "rvbbit: Clover install skipped (fetch or apply failed) — run manually:"
  echo "  curl -fsSL ${CLOVER_INSTALL_URL} | psql \$DSN"
fi
