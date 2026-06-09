# Rvbbit Release Images

Rvbbit ships three product images plus the deterministic Warren smoke
capability image. The rest of the built-in Warren capability images can also be
built, but they are opt-in because the full catalog build is expensive. GHCR is
the canonical registry for V1; GitHub Releases hold native tarballs and Warren
agent binaries.

## Product Images

| Image | Purpose |
|---|---|
| `ghcr.io/ryrobes/rvbbit-postgres:<version>` | PostgreSQL 18 with `pg_rvbbit`, `rvbbit-duck`, first-boot catalog seed, and tuned config. |
| `ghcr.io/ryrobes/rvbbit-lens:<version>` | Standalone Lens SQL desktop with local capability scaffold/build support. Persist `/data` for `RVBBIT_LENS_HOME`. |
| `ghcr.io/ryrobes/rvbbit-warren-agent:<version>` | Warren deployment agent with Docker CLI and Compose plugin. Mount the host Docker socket. |

## Capability Images

The release script always builds `smoke/warren-echo` with the core release so a
fresh Warren install has a deterministic backend for smoke tests. Pass
`--with-capabilities` to build every built-in Warren capability as a separate
image. Runtime/system capabilities get short names:

| Catalog id | Image |
|---|---|
| `runtimes/python-runtime` | `ghcr.io/ryrobes/rvbbit-python-runtime:<version>` |
| `runtimes/mcp-gateway` | `ghcr.io/ryrobes/rvbbit-mcp-gateway:<version>` |
| `smoke/warren-echo` | `ghcr.io/ryrobes/rvbbit-warren-smoke-echo:<version>` |

Model and example packs use `rvbbit-capability-<pack-name>`. The table below
shows the image name; the full reference is
`ghcr.io/ryrobes/<image>:<version>` unless `--registry` or `--namespace` is
overridden.

| Catalog id | Image |
|---|---|
| `classify/deberta-v3-base-zero-shot` | `rvbbit-capability-deberta-v3-base-zero-shot` |
| `classify/deberta-v3-zero-shot` | `rvbbit-capability-deberta-v3-zero-shot` |
| `classify/emotion-distilroberta` | `rvbbit-capability-emotion-distilroberta` |
| `classify/language-detection-xlm-roberta` | `rvbbit-capability-language-detection-xlm-roberta` |
| `classify/toxic-bert` | `rvbbit-capability-toxic-bert` |
| `classify/twitter-roberta-sentiment` | `rvbbit-capability-twitter-roberta-sentiment` |
| `embeddings/bge-m3` | `rvbbit-capability-bge-m3` |
| `embeddings/bge-small-en-v1.5` | `rvbbit-capability-bge-small-en-v1-5` |
| `embeddings/e5-small-v2` | `rvbbit-capability-e5-small-v2` |
| `extract/gliner-medium-v2.1` | `rvbbit-capability-gliner-medium-v2-1` |
| `rerank/bge-reranker-base` | `rvbbit-capability-bge-reranker-base` |
| `rerank/bge-reranker-v2-m3` | `rvbbit-capability-bge-reranker-v2-m3` |
| `rerank/ms-marco-minilm-l6-v2` | `rvbbit-capability-ms-marco-minilm-l6-v2` |
| `tabular/california-housing-sklearn` | `rvbbit-capability-california-housing-sklearn` |
| `tabular/wine-quality-sklearn` | `rvbbit-capability-wine-quality-sklearn` |

Images include runtime dependencies and handler code. Hugging Face model
weights are not baked into the image; they download into the Warren-managed
container volume on first use. V1 catalog entries also include source-build
metadata, so Warren can build a capability from its packaged manifest/source
when a prebuilt capability image is not available.

## Build Locally

```bash
make release-build RELEASE_VERSION=1.0.0
```

This builds images locally and stages:

- `dist/release/<version>/capabilities.catalog.<version>.json`
- `dist/release/<version>/capability_catalog_seed.<version>.json`
- `dist/release/<version>/capability-images.<version>.json`
- `dist/release/<version>/warren-agent-linux-<arch>`

By default, `capability-images.<version>.json` contains only
`smoke/warren-echo`. If `--with-capabilities` is used, it contains the full
built-in capability image set.

The Postgres image is built from a temporary release context whose embedded
catalog seed points at the release image namespace/tag and includes source-build
metadata for capabilities. The working tree stays in normal source/build mode.

## Publish

```bash
docker login ghcr.io
make release-push RELEASE_VERSION=1.0.0 IMAGE_NAMESPACE=ryrobes
```

Equivalent direct script:

```bash
scripts/release/build-and-push.sh \
  --version 1.0.0 \
  --namespace ryrobes \
  --push \
  --tag-latest
```

Build and push the full catalog image set only when needed:

```bash
scripts/release/build-and-push.sh \
  --version 1.0.0 \
  --namespace ryrobes \
  --push \
  --tag-latest \
  --with-capabilities
```

Use `--bump` to update version files first:

```bash
scripts/release/build-and-push.sh --version 1.0.1 --bump --push --tag-latest
```

`--platform` controls product images; `--capability-platform` controls model
capability images. V1 defaults both to `linux/amd64`.

## Public GHCR Pulls

GitHub creates newly pushed container packages as private by default. Docker
pushes cannot set package visibility. To test on fresh machines without GHCR
login, make each package public from GitHub's package settings:

1. Open the package URL.
2. Click **Package settings**.
3. Under **Danger Zone**, click **Change visibility**.
4. Select **Public**.

Once public, GHCR container images can be pulled anonymously. No
`docker login ghcr.io` is required on the test machine.

List the release package URLs:

```bash
scripts/release/check-public-images.py \
  --image-prefix ghcr.io/ryrobes \
  --version 1.0.0 \
  --list-only
```

After changing visibility, verify anonymous access with a clean Docker config:

```bash
make release-public-check RELEASE_VERSION=1.0.0 IMAGE_NAMESPACE=ryrobes
```

The default public check covers the three product images plus
`rvbbit-warren-smoke-echo`. To verify the full catalog after a
`--with-capabilities` release, run the checker with `--with-capabilities`.

Or as a release gate after a push:

```bash
scripts/release/build-and-push.sh \
  --version 1.0.0 \
  --namespace ryrobes \
  --push \
  --tag-latest \
  --check-public
```

## Clean-Slate Compose

```bash
RVBBIT_VERSION=1.0.0 docker compose -f docker/docker-compose.release.yml up -d
```

This starts:

- `postgres` from the published `rvbbit-postgres` image
- `lens` from `rvbbit-lens`
- `warren` from `rvbbit-warren-agent`

The Warren container needs the Docker socket:

```yaml
volumes:
  - ${RVBBIT_DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock
```

That is intentional for the V1 single-host Warren model: Warren manages local
Docker capability containers on behalf of the database.

Lens also mounts the same socket in the packaged compose files. That makes the
Capability UI's `Local` install target work from inside the Lens container
against the same Docker daemon. The Lens image includes Docker CLI + Compose
plugin and drops privileges after adding its runtime user to the mounted socket
group. Local scaffold/build output is written under `RVBBIT_LOCAL_WORK_ROOT`,
which defaults to `/data`, so generated capability projects persist in the Lens
volume instead of the immutable app directory.

On rootful Docker hosts, including hosts where the login user must run
`sudo docker compose ...`, the default socket path is correct:

```bash
sudo env RVBBIT_VERSION=1.0.0 docker compose -f docker/docker-compose.release.yml up -d
```

Once the Warren container is running with the socket mounted, it manages
capability containers through the Docker daemon. It does not need to run sudo
inside the container.

On rootless/user Docker hosts, point `RVBBIT_DOCKER_SOCKET` at the user Docker
socket and launch with that same user-level Docker daemon:

```bash
export RVBBIT_DOCKER_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
RVBBIT_VERSION=1.0.0 docker compose -f docker/docker-compose.release.yml up -d
```

The socket is mounted into Warren at `/var/run/docker.sock`, and
`DOCKER_HOST` inside the Warren and Lens containers defaults to
`unix:///var/run/docker.sock`.
For a host-installed Warren service, use the installer in
[WARREN_END_USER_INSTALL.md](WARREN_END_USER_INSTALL.md); it grants the service
user Docker group access or can be run with `WARREN_SERVICE_USER=root` for
root-only Docker sockets.

## Turnkey Uber Compose

For first-run QA, demos, and the easiest local install, use:

```bash
RVBBIT_VERSION=1.0.0 docker compose -f docker/docker-compose.uber.yml up -d
```

Use the same Docker socket rules as the release compose stack: rootful/sudo
Docker can use `sudo env RVBBIT_VERSION=... docker compose ...`, and
rootless/user Docker should set `RVBBIT_DOCKER_SOCKET` to the user Docker
socket before launching.

This starts the same Postgres, Lens, and Warren services, plus a one-shot
`bootstrap` service from the Postgres image. The bootstrap waits for Warren to
register, then deploys and verifies:

- `smoke/warren-echo`
- `runtimes/python-runtime`
- `runtimes/mcp-gateway`

The bootstrap container exits successfully when the baseline is ready. Inspect
it with:

```bash
docker logs rvbbit-bootstrap
```

Override the baseline list with:

```bash
RVBBIT_UBER_BOOTSTRAP_CAPABILITIES=smoke/warren-echo,runtimes/python-runtime
```

For private GHCR packages, Warren also needs Docker registry credentials because
it pulls child capability images from inside the `rvbbit-warren-agent`
container. Use a plain Docker auth config:

```bash
export RVBBIT_DOCKER_CONFIG=/tmp/rvbbit-ghcr-auth
mkdir -p "$RVBBIT_DOCKER_CONFIG"
echo "$CR_PAT" | docker --config "$RVBBIT_DOCKER_CONFIG" login ghcr.io -u "$GH_USER" --password-stdin

RVBBIT_VERSION=1.0.0 \
RVBBIT_DOCKER_CONFIG="$RVBBIT_DOCKER_CONFIG" \
docker compose -f docker/docker-compose.uber.yml up -d
```

The default Warren auth mount is
`${RVBBIT_DOCKER_CONFIG:-$HOME/.docker}:/root/.docker:ro`. If your normal
Docker config uses a platform credential helper that is not available inside
the Warren container, use the dedicated `RVBBIT_DOCKER_CONFIG` flow above.
