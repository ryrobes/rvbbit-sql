# Rvbbit Release Images

Rvbbit ships three product images plus one image per built-in Warren
capability. GHCR is the canonical registry for V1; GitHub Releases hold native
tarballs and Warren agent binaries.

## Product Images

| Image | Purpose |
|---|---|
| `ghcr.io/ryrobes/rvbbit-postgres:<version>` | PostgreSQL 18 with `pg_rvbbit`, `rvbbit-duck`, first-boot catalog seed, and tuned config. |
| `ghcr.io/ryrobes/rvbbit-lens:<version>` | Standalone Lens SQL desktop. Persist `/data` for `RVBBIT_LENS_HOME`. |
| `ghcr.io/ryrobes/rvbbit-warren-agent:<version>` | Warren deployment agent with Docker CLI and Compose plugin. Mount `/var/run/docker.sock`. |

## Capability Images

The release script builds every built-in Warren capability as a separate image.
Runtime/system capabilities get short names:

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
container volume on first use.

## Build Locally

```bash
make release-build RELEASE_VERSION=1.0.0
```

This builds images locally and stages:

- `dist/release/<version>/capabilities.catalog.<version>.json`
- `dist/release/<version>/capability_catalog_seed.<version>.json`
- `dist/release/<version>/capability-images.<version>.json`
- `dist/release/<version>/warren-agent-linux-<arch>`

The Postgres image is built from a temporary release context whose embedded
catalog seed points at the versioned capability images. The working tree stays
in normal source/build mode.

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

Use `--bump` to update version files first:

```bash
scripts/release/build-and-push.sh --version 1.0.1 --bump --push --tag-latest
```

`--platform` controls product images; `--capability-platform` controls model
capability images. V1 defaults both to `linux/amd64`.

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
  - /var/run/docker.sock:/var/run/docker.sock
```

That is intentional for the V1 single-host Warren model: Warren manages local
Docker capability containers on behalf of the database.
