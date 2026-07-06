# v3.0.0 fresh-deploy test — GCP g4-standard-48 (Blackwell), 2026-07-05

Target: `rvbbit-sql-test1` (us-east4-c), Ubuntu 24.04.4, kernel 6.17-gcp,
Docker 29.4.2, NVIDIA RTX PRO 6000 Blackwell-class (PCI 2bb5). Deploy kit =
`dist/release/3.0.0/deploy/` scp'd to the box.

## What worked out of the box

- **Anonymous GHCR pulls**: rvbbit-postgres / rvbbit-lens / rvbbit-warren-agent
  :3.0.0 all pulled with no auth.
- **`docker compose -f docker-compose.release.yml up -d` came up first try**:
  postgres → healthy → migrate one-shot → lens + warren.
- **Fresh-boot init**: 130 migrations applied, **10 factory routing models
  seeded**, extension reports `pg_rvbbit 3.0.0`.
- **GQE grace on a driverless box**: `accelerator_runtime_status` = `ok`,
  `gate=true`, `avail=false`, reason "gqe-cli is not installed" — no warnings,
  no misbehavior (the default-on routing gate self-gates as designed).
- **End-to-end accel**: `CREATE TABLE ... USING rvbbit` + `refresh_acceleration`
  + routed query (native) all clean.
- Lens serves HTTP 200 on :3000.

## Issues found

1. **"GPU-enabled server" wasn't** — no NVIDIA driver, no nvidia-smi, no
   nvidia container runtime (only runc). The GPU silicon was attached but cold.
   Fix applied during test: `nvidia-driver-580-open` + `nvidia-container-toolkit`
   (see below). DOC ACTION: deploy README should include a 3-line GPU
   preflight (`nvidia-smi`, `docker info | grep nvidia`, and the install
   commands) so "GPU-enabled" is verifiable before blaming rvbbit.
2. **Default GCP user isn't in the `docker` group** → everything needs sudo.
   DOC ACTION: mention `sudo usermod -aG docker $USER` in README.
3. **First-table onboarding gap**: a plain `CREATE TABLE` then
   `refresh_acceleration` fails with "not an rvbbit table" — correct behavior,
   but the deploy README never shows `CREATE TABLE ... USING rvbbit`. A fresh
   user's first five minutes will hit this. DOC ACTION: add a "your first
   accelerated table" snippet to deploy/README.md.
4. *(pre-checked, OK)* GQE cudf build includes Blackwell (`...;120` in
   CUDF_CMAKE_CUDA_ARCHITECTURES) — no arch gap for this box.

## GPU leg

Working recipe on Ubuntu 24.04 / kernel 6.17-gcp / Blackwell (RTX PRO 6000,
CC 12.0, 96GB):

```bash
sudo apt-get install -y nvidia-driver-580-open nvidia-utils-580
sudo reboot   # REQUIRED: modprobe alone gives "Driver/library version mismatch"
# nvidia-container-toolkit (NVIDIA apt repo), then:
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
sudo docker run --rm --gpus all ghcr.io/ryrobes/rvbbit-postgres:3.0.0 nvidia-smi -L
```

Issues found in this leg:

5. **`restart: unless-stopped` was MISSING on the postgres service** in
   docker-compose.release.yml — after the reboot, lens/warren came back but
   postgres stayed down and warren crash-looped. FIXED in the compose (needs
   re-release of the deploy kit); uber.yml was already correct.
6. Driver install without reboot leaves NVML "version mismatch" (DKMS module
   built but old/none loaded). Reboot, don't fight it. DOC ACTION: put the
   reboot in the GPU preflight.
7. GQE Blackwell compatibility pre-verified: cudf built with
   `CUDA arch ...;120` and conda CUDA ≥ 12.8 — no arch gap.

GQE overlay: one-time toolchain build from the PUBLISHED base
(`compose -f release.yml -f release-gqe.yml build postgres`) — **SUCCEEDED**
(~2h on 48 vCPUs; MLIR compile is the long pole; buildx's 2MiB log clip hides
tail progress — watch `docker ps`/load instead). Result:

- `rvbbit-postgres-gqe:3.0.0` built on-box; `up -d` swapped postgres onto it,
  healthy, migrate re-ran clean.
- `accelerator_runtime_status`: **ok | gate=true | avail=true** on Blackwell.
- Forced `gpu_gqe` aggregation over an accelerated table returned
  **arithmetically verified correct results** (sum of series check).

## Bench-harness-on-fresh-box notes (Blackwell bench runs)

8. **GQE `--no-build` guard also blocks the bench image**: on a box that has
   never built `docker-bench`, an auto-`rvbbit` run selects the GQE overlay →
   `up --no-build` → "No such image: docker-bench". Workaround: build it
   explicitly once (`compose --profile bench build bench`). HARNESS ACTION:
   the rebuild branch should always build `bench` even in refresh mode when
   the image is missing.
9. Remote long-running commands over gcloud-ssh: **use `sudo systemd-run
   --unit=NAME --collect bash -c "..."`**. Both nohup shapes proved flaky for
   the full bench harness (detached compose `up` intermittently failed
   container creation with "No such image" for a locally-built image that
   demonstrably existed — Docker 29 containerd store; a fresh image tag
   (`docker tag … localhost/name:v1` + `image:`/`pull_policy: never` in the
   service) plus a daemon restart cleared part of it, but only the systemd
   unit was reliably reproducible). Foreground runs never failed.
10a. **Docker 29 + `docker compose build` produced an image `up` couldn't
    use** ("No such image: docker-bench:latest" while `docker images` listed
    it) — the buildx provenance/OCI-index output on the containerd store.
    Fix: `docker build --provenance=false -t docker-bench:latest .` (plain
    build), after which `compose create bench` works. Worth pinning
    `--provenance=false` (or `provenance: false` in compose build config) in
    the harness for portability.
10. Harness images on a deploy box: `docker tag ghcr.io/…/rvbbit-postgres:X
    docker-pg-rvbbit` + `docker tag rvbbit-postgres-gqe:X docker-pg-rvbbit-gqe`
    lets the dev harness run against published/on-box images with no source
    build of the extension.

## Blackwell GQE finding (the big one)

11. **GQE silently doesn't execute on Blackwell with the shipped defaults**:
    `NVSHMEM_DISABLE_CUDA_VMM=1` (both GQE composes' default) makes NVSHMEM's
    init fail on RTX PRO 6000 / driver 580 (`InitNvshmem failed:
    pgas_memory_resource allocation failed`) — every GQE-routed query then
    burns the full sidecar timeout before falling back (~120s each; the
    Phase-1 auto run recorded 16 GQE queries at ~120s medians). Fix:
    **`NVSHMEM_DISABLE_CUDA_VMM=0`** (+ a sane `NVSHMEM_SYMMETRIC_SIZE`, 4G
    validated). Both composes now carry the warning comment. Follow-up worth
    considering: auto-detect (probe CC ≥ 12 → default 0), and GQE's warm-probe
    should catch InitNvshmem failure and mark the engine unavailable instead
    of letting queries eat the timeout.

## Bottom line

The shipped "orchestra" works on a virgin cloud box: CPU stack is
zero-friction; the GPU leg's only real obstacles were host provisioning
(driver/toolkit/reboot — now a preflight section in deploy/README.md) and the
one compose bug (postgres restart policy — fixed). Total wall time from bare
box to GPU-accelerated queries: ~3h, of which ~2h was the unavoidable one-time
GQE toolchain build.

Fixes fed back into the tree: release.yml restart policy, deploy README
preflight + docker-group note + first-table `USING rvbbit` snippet
(build-and-push.sh heredoc — regenerate kit on next release).
