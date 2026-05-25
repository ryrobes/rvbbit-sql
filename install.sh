#!/usr/bin/env bash
# Install pg_rvbbit + rvbbit-duck from a release tarball.
#
# Usage (from inside the unpacked tarball):
#   sudo ./install.sh                  # auto-detects pg_config on PATH
#   sudo PG_CONFIG=/path/to/pg_config ./install.sh
#
# What it does:
#   1. Copies pg_rvbbit.so          -> $(pg_config --pkglibdir)/
#   2. Copies pg_rvbbit*.{control,sql} -> $(pg_config --sharedir)/extension/
#   3. Copies rvbbit-duck binary    -> /usr/local/bin/   (override with DUCK_BIN_DIR)
#   4. Verifies shared_preload_libraries includes pg_rvbbit and warns loudly
#      if it does not.
#
# Idempotent. Re-run after upgrade to drop in newer artifacts.

set -euo pipefail

die() { echo "install.sh: $*" >&2; exit 1; }
warn() { echo "install.sh: WARNING: $*" >&2; }
info() { echo "install.sh: $*"; }

[[ $EUID -eq 0 ]] || die "must run as root (try: sudo $0)"

PG_CONFIG="${PG_CONFIG:-$(command -v pg_config || true)}"
[[ -n "$PG_CONFIG" && -x "$PG_CONFIG" ]] \
    || die "pg_config not found on PATH. Set PG_CONFIG=/path/to/pg_config."

PKGLIBDIR="$("$PG_CONFIG" --pkglibdir)"
SHAREDIR="$("$PG_CONFIG" --sharedir)"
PG_VERSION="$("$PG_CONFIG" --version | awk '{print $2}' | cut -d. -f1)"
EXTDIR="$SHAREDIR/extension"
DUCK_BIN_DIR="${DUCK_BIN_DIR:-/usr/local/bin}"

[[ "$PG_VERSION" == "18" ]] \
    || warn "pg_config reports PostgreSQL $PG_VERSION, but this package was built for PG18. Continuing — change at your own risk."

# Locate artifacts in the unpacked tarball (relative to this script).
HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LIB_SRC="$HERE/lib/pg_rvbbit.so"
EXT_SRC="$HERE/extension"
DUCK_SRC="$HERE/bin/rvbbit-duck"

[[ -f "$LIB_SRC" ]]  || die "missing $LIB_SRC"
[[ -d "$EXT_SRC" ]]  || die "missing $EXT_SRC"
[[ -f "$DUCK_SRC" ]] || die "missing $DUCK_SRC"

info "pg_config:    $PG_CONFIG  (PG$PG_VERSION)"
info "pkglibdir:    $PKGLIBDIR"
info "extensiondir: $EXTDIR"
info "duck binary:  $DUCK_BIN_DIR/rvbbit-duck"

install -m 0755 "$LIB_SRC" "$PKGLIBDIR/pg_rvbbit.so"
install -d -m 0755 "$EXTDIR"
install -m 0644 "$EXT_SRC"/pg_rvbbit*.control "$EXTDIR/"
install -m 0644 "$EXT_SRC"/pg_rvbbit*.sql     "$EXTDIR/"
install -d -m 0755 "$DUCK_BIN_DIR"
install -m 0755 "$DUCK_SRC" "$DUCK_BIN_DIR/rvbbit-duck"

info "files installed."

# Check shared_preload_libraries. Critical: extension's planner hooks only
# fire at backend start; without preload, CREATE EXTENSION succeeds but no
# parquet routing happens — silent misbehavior.
PG_CONF_DIR="$("$PG_CONFIG" --sysconfdir 2>/dev/null || true)"
if [[ -z "$PG_CONF_DIR" || ! -d "$PG_CONF_DIR" ]]; then
    # PGDG layout: postgresql.conf lives under PG's runtime data dir, not
    # sysconfdir. We can't reliably find it without running the server, so
    # just print the directive.
    PG_CONF_DIR="(your postgresql.conf location)"
fi

cat <<EOF

────────────────────────────────────────────────────────────────────────
Installed. Two more steps before queries route through pg_rvbbit:

  1) Add the extension to shared_preload_libraries in postgresql.conf:

         shared_preload_libraries = 'pg_rvbbit'

     (configuration file is typically under $PG_CONF_DIR)

  2) Restart PostgreSQL, then in your database:

         CREATE EXTENSION pg_rvbbit;
         SELECT rvbbit.rvbbit_version();

Skipping step 1 will NOT produce an error — CREATE EXTENSION still
succeeds — but parquet routing will silently fall back to heap scans.
────────────────────────────────────────────────────────────────────────
EOF
