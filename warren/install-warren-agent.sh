#!/usr/bin/env bash
# Install the Rvbbit Warren agent as a systemd service.
#
# Intended production shape for V1:
#   curl -fsSL https://raw.githubusercontent.com/rvbbit/rvbbit-sql/v1.0.0/warren/install-warren-agent.sh | sudo -E bash
#
# Required:
#   RVBBIT_DSN='postgresql://rvbbit_warren:...@db-host:5432/bench'
#
# Optional:
#   RVBBIT_VERSION=1.0.0
#   WARREN_NODE=$(hostname -s)
#   WARREN_LABELS='{"capability":true,"docker":true,"gpu":false}'
#   WARREN_CAPACITY='{}'
#   WARREN_WORK_DIR=/var/lib/rvbbit/warren
#   RVBBIT_DOCKER_NETWORK=docker_default
#   WARREN_AGENT_URL=https://.../warren-agent-linux-amd64

set -euo pipefail

die() { echo "install-warren-agent.sh: $*" >&2; exit 1; }
info() { echo "install-warren-agent.sh: $*"; }
systemd_quote() {
    local v="${1//\\/\\\\}"
    v="${v//\"/\\\"}"
    printf '"%s"' "$v"
}
write_env_var() {
    local key="$1"
    local value="$2"
    printf '%s=%s\n' "$key" "$(systemd_quote "$value")"
}

[[ "${EUID}" -eq 0 ]] || die "must run as root (try: sudo -E bash)"
[[ -n "${RVBBIT_DSN:-}" ]] || die "RVBBIT_DSN is required"

RVBBIT_VERSION="${RVBBIT_VERSION:-1.0.0}"
WARREN_NODE="${WARREN_NODE:-$(hostname -s 2>/dev/null || hostname)}"
WARREN_LABELS="${WARREN_LABELS:-{\"capability\":true,\"docker\":true,\"gpu\":false}}"
WARREN_CAPACITY="${WARREN_CAPACITY:-{}}"
WARREN_WORK_DIR="${WARREN_WORK_DIR:-/var/lib/rvbbit/warren}"
RVBBIT_DOCKER_NETWORK="${RVBBIT_DOCKER_NETWORK:-docker_default}"
WARREN_METRICS_MS="${WARREN_METRICS_MS:-10000}"
WARREN_RECONCILE_MS="${WARREN_RECONCILE_MS:-15000}"

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) ASSET_ARCH="amd64" ;;
    aarch64|arm64) ASSET_ARCH="arm64" ;;
    *) die "unsupported architecture: $ARCH" ;;
esac

WARREN_AGENT_URL="${WARREN_AGENT_URL:-https://github.com/rvbbit/rvbbit-sql/releases/download/v${RVBBIT_VERSION}/warren-agent-linux-${ASSET_ARCH}}"

command -v install >/dev/null || die "install command not found"
command -v systemctl >/dev/null || die "systemctl not found; this installer targets systemd hosts"
command -v curl >/dev/null || die "curl not found"
command -v docker >/dev/null || die "docker not found; install Docker before installing Warren"

if ! getent group rvbbit-warren >/dev/null; then
    groupadd --system rvbbit-warren
fi
if ! id -u rvbbit-warren >/dev/null 2>&1; then
    useradd --system --gid rvbbit-warren --home-dir "$WARREN_WORK_DIR" \
        --shell /usr/sbin/nologin rvbbit-warren
fi
if getent group docker >/dev/null; then
    usermod -aG docker rvbbit-warren
fi

install -d -m 0755 /etc/rvbbit
install -d -m 0755 /usr/local/bin
install -d -o rvbbit-warren -g rvbbit-warren -m 0750 "$WARREN_WORK_DIR"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
info "downloading $WARREN_AGENT_URL"
curl -fsSL "$WARREN_AGENT_URL" -o "$tmp"
install -m 0755 "$tmp" /usr/local/bin/warren-agent

{
    write_env_var RVBBIT_DSN "$RVBBIT_DSN"
    write_env_var WARREN_NODE "$WARREN_NODE"
    write_env_var WARREN_WORK_DIR "$WARREN_WORK_DIR"
    write_env_var RVBBIT_DOCKER_NETWORK "$RVBBIT_DOCKER_NETWORK"
    write_env_var WARREN_LABELS "$WARREN_LABELS"
    write_env_var WARREN_CAPACITY "$WARREN_CAPACITY"
    write_env_var WARREN_METRICS_MS "$WARREN_METRICS_MS"
    write_env_var WARREN_RECONCILE_MS "$WARREN_RECONCILE_MS"
} >/etc/rvbbit/warren-agent.env
chmod 0600 /etc/rvbbit/warren-agent.env
chown root:root /etc/rvbbit/warren-agent.env

cat >/etc/systemd/system/rvbbit-warren-agent.service <<'EOF'
[Unit]
Description=Rvbbit Warren Agent
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
EnvironmentFile=/etc/rvbbit/warren-agent.env
ExecStart=/usr/local/bin/warren-agent
Restart=always
RestartSec=5
User=rvbbit-warren
Group=rvbbit-warren
WorkingDirectory=/var/lib/rvbbit/warren
StateDirectory=rvbbit
LogsDirectory=rvbbit
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/var/lib/rvbbit/warren
SupplementaryGroups=docker

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now rvbbit-warren-agent.service

cat <<EOF

Installed Warren agent.

Useful commands:
  systemctl status rvbbit-warren-agent
  journalctl -u rvbbit-warren-agent -f
  systemctl restart rvbbit-warren-agent

Database role shape for the DSN:
  CREATE ROLE rvbbit_warren LOGIN PASSWORD '<strong password>';
  GRANT USAGE ON SCHEMA rvbbit TO rvbbit_warren;
  GRANT CREATE ON SCHEMA rvbbit TO rvbbit_warren;
  GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA rvbbit TO rvbbit_warren;
  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rvbbit TO rvbbit_warren;
  GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rvbbit TO rvbbit_warren;

Network shape:
  - allow this Warren host to connect to Postgres in pg_hba.conf
  - keep generated sidecars and Postgres on a private network
  - do not expose Warren-managed model containers directly to the public internet
EOF
