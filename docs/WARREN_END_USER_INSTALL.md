# Warren End-User Install

This is the short production install path for adding a Warren node to an
existing Rvbbit database.

Warren is a small host service. It runs on a machine with Docker, polls the
database for capability deployment jobs, starts sidecar containers, probes
them, and reports the resulting backend/runtime endpoints back to SQL.

## Prerequisites

- A running Rvbbit/Postgres database with `pg_rvbbit` installed.
- A Linux host for Warren with systemd and Docker installed.
- Network access from the Warren host to Postgres.
- Network access from Postgres to Warren-managed sidecar endpoints.
- A dedicated database role for Warren.

The Warren service must be able to run Docker without an interactive sudo
password. The installer runs as root, creates a `rvbbit-warren` service user,
and adds that user to the host Docker group when the group exists.

For GPU capabilities, install the NVIDIA container runtime and confirm
`nvidia-smi` works on the Warren host. CPU-only runtimes do not need GPU setup.

## 1. Create The Database Role

Run this as a DBA on the Rvbbit database:

```sql
CREATE ROLE rvbbit_warren LOGIN PASSWORD '<strong password>';

GRANT USAGE ON SCHEMA rvbbit TO rvbbit_warren;
GRANT CREATE ON SCHEMA rvbbit TO rvbbit_warren;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA rvbbit TO rvbbit_warren;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rvbbit TO rvbbit_warren;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rvbbit TO rvbbit_warren;
```

Allow the Warren host in `pg_hba.conf` using your normal production auth
policy, preferably SCRAM/TLS. Do not use `trust` except in local development.

## 2. Install The Warren Agent

On the Warren host, inspect and run the installer:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/rvbbit/rvbbit-sql/v1.0.0/warren/install-warren-agent.sh \
  -o install-warren-agent.sh

less install-warren-agent.sh

sudo env \
  RVBBIT_DSN='postgresql://rvbbit_warren:<password>@<db-host>:5432/<database>' \
  WARREN_NODE='warren-1' \
  WARREN_LABELS='{"capability":true,"docker":true,"gpu":false}' \
  WARREN_CAPACITY='{}' \
  bash install-warren-agent.sh
```

For a GPU node:

```bash
sudo env \
  RVBBIT_DSN='postgresql://rvbbit_warren:<password>@<db-host>:5432/<database>' \
  WARREN_NODE='gpu-1' \
  WARREN_LABELS='{"capability":true,"docker":true,"gpu":true,"cuda":true}' \
  WARREN_CAPACITY='{"gpu":{"vram_usable_ratio":0.9}}' \
  bash install-warren-agent.sh
```

The installer writes credentials to `/etc/rvbbit/warren-agent.env` with mode
`0600`, installs `/usr/local/bin/warren-agent`, and starts the systemd service
`rvbbit-warren-agent`.

### Docker Access On Rootful And Rootless Hosts

Many fresh Linux VMs use rootful Docker and require `sudo docker ...` because
the login user is not in the Docker group. That is fine for launching the
installer or compose stack, but the Warren service itself still needs
non-interactive Docker access.

Preferred host setup:

```bash
sudo usermod -aG docker "$USER"
newgrp docker
docker ps
```

For Warren, the installer handles the service user:

```bash
sudo env \
  RVBBIT_DSN='postgresql://rvbbit_warren:<password>@<db-host>:5432/<database>' \
  bash install-warren-agent.sh
```

If the host has a root-only Docker socket and no usable Docker group, run Warren
as root explicitly:

```bash
sudo env \
  RVBBIT_DSN='postgresql://rvbbit_warren:<password>@<db-host>:5432/<database>' \
  WARREN_SERVICE_USER=root \
  bash install-warren-agent.sh
```

This is not meaningfully more privileged than Docker-group access; controlling
the Docker daemon is already root-equivalent on that host. It is preferable to
interactive sudo inside the agent.

For rootless/user Docker, install Warren as the same user that owns the Docker
daemon or make that user's Docker socket available through your service manager.
For a host-installed systemd Warren service, the rootless Docker user usually
needs lingering enabled so the user Docker daemon and `/run/user/<uid>` socket
exist outside an interactive login:

```bash
sudo loginctl enable-linger "$USER"

sudo env \
  RVBBIT_DSN='postgresql://rvbbit_warren:<password>@<db-host>:5432/<database>' \
  WARREN_SERVICE_USER="$USER" \
  WARREN_SERVICE_GROUP="$(id -gn)" \
  WARREN_DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock" \
  bash install-warren-agent.sh
```

The containerized Warren compose path supports rootless Docker with:

```bash
export RVBBIT_DOCKER_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
docker compose -f docker/docker-compose.release.yml up -d
```

## 3. Verify The Service

On the Warren host:

```bash
systemctl status rvbbit-warren-agent
journalctl -u rvbbit-warren-agent -f
```

From SQL:

```sql
SELECT
  name,
  reported_status,
  effective_status,
  heartbeat_state,
  is_eligible,
  last_heartbeat
FROM rvbbit.warren_node_effective_status
ORDER BY name;
```

A healthy idle node should show `effective_status = 'ready'`,
`heartbeat_state = 'fresh'`, and `is_eligible = true`.

## 4. Install A Capability

From SQL:

```sql
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'runtimes/python-runtime',
  target_selector => '{"docker":true}'::jsonb
);
```

Or install a GPU model/runtime by selecting a catalog item that targets GPU
nodes:

```sql
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => '<catalog-id>',
  target_selector => '{"gpu":true,"cuda":true}'::jsonb
);
```

Track progress:

```sql
SELECT status, phase, name, claimed_by, error, progress
FROM rvbbit.warren_jobs
ORDER BY created_at DESC
LIMIT 20;

SELECT node_name, deployment_name, deployment_status, backend_name, runtime_name
FROM rvbbit.warren_inventory
ORDER BY node_name, deployment_name;
```

## Operational Notes

- Warren needs Docker access. Membership in the Docker group is effectively
  root-equivalent on that host.
- Do not configure Warren to use password-prompt sudo. It is a background
  service; Docker access must be direct, group-based, or root service based.
- Keep Warren sidecars on a private network. Do not expose model containers
  directly to the public internet.
- If Postgres and the sidecars are on the same Docker/private network, Warren
  can register Docker DNS endpoints. If Postgres is remote, configure routing
  so Postgres can reach the advertised sidecar URLs.
- Use separate Warren nodes for different trust/resource pools, then target
  them with labels such as `{"gpu":true}`, `{"region":"lab"}`, or
  `{"tenant":"analytics"}`.

Useful lifecycle SQL:

```sql
SELECT rvbbit.request_warren_deployment_stop('<deployment-id>'::uuid);
SELECT rvbbit.request_warren_deployment_remove('<deployment-id>'::uuid);
SELECT rvbbit.request_warren_deployment_redeploy('<deployment-id>'::uuid);
```

## Troubleshooting

No eligible nodes:

```sql
SELECT name, effective_status, heartbeat_state, labels
FROM rvbbit.warren_node_effective_status;
```

Queued jobs are not being claimed:

```sql
SELECT job_id, name, status, target_selector, progress, error
FROM rvbbit.warren_jobs
WHERE status IN ('queued', 'running')
ORDER BY created_at;
```

Backend is registered but unavailable:

```sql
SELECT name, deployment_status, serving_status, callable, deployment_error
FROM rvbbit.warren_backend_status
WHERE callable IS DISTINCT FROM true;
```

Then inspect the host logs:

```bash
journalctl -u rvbbit-warren-agent -n 200 --no-pager
```

For the full contract, see [WARREN.md](WARREN.md) and
[WARREN_UI_CONTRACT.md](WARREN_UI_CONTRACT.md).
