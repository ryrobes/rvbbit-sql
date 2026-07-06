# Sirius (GPU DuckDB) — assessment for rvbbit

Status: research memo, 2026-07-05. Question: is Sirius a low-risk drop-in for
the DuckDB we ship, what's the lift, and does it work without a GPU?

## What Sirius actually is

- A **DuckDB extension** (not a fork): DuckDB parses/optimizes, an optimizer
  hook intercepts the plan (via Substrait) and executes it on GPU with
  **libcudf/RMM** — the same substrate as our GQE engine. Apache-2.0, by
  NVIDIA + UW-Madison. NVIDIA blog (Dec 2025) claims ClickBench
  cost-efficiency records (GH200, ≥7.2× vs CPU metal boxes).
- **Transparent**: `LOAD sirius` → all queries on that connection run GPU;
  `SET gpu_execution = false` per-connection turns it off. Unsupported
  operators/types/memory pressure → **automatic fallback to DuckDB CPU**.
- **Reads parquet directly** with out-of-core execution — i.e. it runs on the
  row-group artifacts we ALREADY produce. (Vortex layout: no — CPU fallback.)
- Coverage today: filter/project/join/group-by/order-by/top-n/limit/CTE;
  INTEGER/BIGINT/FLOAT/DOUBLE/VARCHAR/DATE/TIMESTAMP/DECIMAL. No window, no
  ASOF join, no nested types yet.

## The catch: it is NOT a drop-in for what we ship

1. **We don't ship a duckdb binary.** `rvbbit-duck` embeds libduckdb via the
   Rust `duckdb` crate (currently 1.10502.0) and runtime-loads extensions
   (we already do this for vortex). DuckDB extensions are **ABI-locked to an
   exact DuckDB version**, and Sirius has **no released binaries** — it builds
   from source against a **pinned duckdb submodule commit**. Unless that pin
   matches our crate's libduckdb, `LOAD sirius` into rvbbit-duck fails.
2. **Hardware/software wall**: CUDA 12/13 + driver, Turing+ (CC 7.5+), Linux,
   glibc ≥ 2.28, **io_uring + direct I/O for the parquet path**. Docker's
   default seccomp profile blocks io_uring — the compose overlay would need a
   seccomp allowance (same class of friction as the GQE overlay's shm/ulimit
   settings). Must be validated in a container early.
3. **No-GPU behavior**: the extension links CUDA libraries, so on a plain box
   the `.so` won't even load. That's fine for us — it's the **same two-image
   pattern we already ship for GQE**: plain image unchanged (candidate simply
   unavailable), GPU image adds the engine. No new "two binaries" dilemma —
   we already crossed that bridge, and the router's availability gates were
   built exactly for this.

## How it would slot in (the router makes this cheap)

New candidate `duck_sirius` next to `duck_vortex`/`gpu_gqe`:
- availability = try-`LOAD sirius` probe (mirrors vortex's INSTALL/LOAD and
  GQE's cached binary probe) + type/shape vetoes (reuse the GQE lessons:
  numeric/timestamptz drift vetoes, empty-result bail);
- forced system `rvbbit_duck_sirius_forced` → parity harness + bench coverage;
- route_observations/self-train learns where it wins; ML layer ranks it.
- Per-request `SET gpu_execution` even lets ONE sidecar serve both duck (CPU)
  and duck_sirius (GPU) candidates.

## vs GQE (both are cuDF underneath)

Sirius is operationally much simpler: in-process extension, no node/task
managers, no external server to prewarm, no external-table catalog to keep in
sync (the whole GQE stale-binding class disappears). If Sirius matures, it
plausibly **replaces the GQE sidecar complexity** while keeping the GPU wins.
That strategic option is the strongest reason to run the experiment. Caveat:
don't co-schedule both on one GPU (two RMM pools fighting for VRAM).

## Lift & phasing

- **Phase 0 — out-of-tree experiment (½–1 day, ZERO product risk):** build
  Sirius per their recipe in a container on the GPU box; point its duckdb CLI
  at our existing parquet row groups (`read_parquet` over the live rvbbit
  file set, hive partitioning included); run the ClickBench/TPC-H bench SQL;
  compare against duck_vortex/gpu_gqe numbers already in bench_history. Also
  answers the io_uring-in-docker question. Touches nothing we ship.
- **Phase 1 — integration (1–2 weeks, post-launch):** align libduckdb —
  either pin the `duckdb` crate to Sirius's duckdb version (non-bundled,
  link their libduckdb build) or run Sirius as its own sidecar via our JSON
  protocol. Add the candidate + vetoes + forced system; parity harness; ship
  in the GPU image only, gate default-off until parity is clean, then flip
  (the GQE un-gating playbook, rerun).

## Risks

- **Maturity**: no releases, active-dev, pinned-submodule upgrade treadmill;
  correctness surface is young (our parity harness + vetoes are the designed
  mitigation — same as GQE's rollout).
- **Container I/O**: io_uring/seccomp + direct-I/O-on-overlayfs are the most
  likely real-world deploy failures; Phase 0 must test inside Docker.
- **Version coupling**: adopting Sirius couples our duckdb crate upgrades to
  their submodule pin until they publish versioned releases.

## Phase 0 RESULTS (2026-07-05 — executed, run `clickbench_20260705T062118Z`)

Built from source (pixi/cuda13, ~40 min, prebuilt conda libcudf), ran as bench
system `sirius` over rvbbit's live parquet row groups (shim: persistent
CLI-pipe in container `rvbbit-sirius`; views generated from rvbbit.row_groups).
ClickBench 5M ×3 vs forced engines:

| metric | sirius | duck_vortex | native | GQE (hist. same-5M) |
|---|---|---|---|---|
| suite time | 6.6s | **3.2s** | 14.7s | — |
| geomean | 145ms | **43ms** | 124ms | — |
| wins | 0 | **31** | 11 | — |
| Q33 heavy agg | 367ms | 224ms | 1314ms | **109ms** |
| Q13 group-by | 238ms | 96ms | 733ms | **51ms** |

- Correctness spot-checks vs PG: exact (filter-count, sum/count).
- 9 "failures" were all harness artifacts (missing cast layer in our views,
  offline extension autoload, shim error-string false positive) — not engine
  correctness bugs. One real engine crash: SIGSEGV on `round(sum(x),2)`.
- Confirmed in-container requirements: `--security-opt seccomp=unconfined`
  (io_uring) and a `sirius.yaml` GPU-memory cap (defaults grab 95% VRAM).
- Confirmed co-scheduling hazard: sirius's RMM pool starved GQE's node
  manager at startup (GQE skipped in the live run).

**Verdict: on our hardware (PCIe RTX 3090 Ti), Sirius today loses broadly to
duck_vortex and to GQE on GPU-friendly shapes — no Phase 1 for v1.** Their
record numbers came from GH200/NVLink; on PCIe the transfer tax eats the win.
Re-evaluate when they publish versioned releases (watch: prebuilt extensions,
PCIe-class benchmarks, window functions, the projection SIGSEGV). The bench
plumbing (`sirius` system + shim + views generator) stays in-tree for a
rematch at zero marginal cost.

## Verdict

Not a drop-in swap, and **not a launch item** — but a **cheap, high-upside
Phase-0 experiment** precisely because it consumes the parquet artifacts we
already produce and our multi-engine router was built for exactly this shape
of addition. Run Phase 0 on the GPU box whenever convenient (it cannot
destabilize the release); decide on Phase 1 after seeing parity + container
behavior. Strategic upside if it lands: GQE-class speedups with a fraction of
GQE's operational machinery.

Sources: [sirius-db/sirius](https://github.com/sirius-db/sirius) ·
[NVIDIA blog (Dec 2025)](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/) ·
[DuckDB: Rethinking Analytical Processing in the GPU Era](https://duckdb.org/library/rethinking-analytical-processing-in-the-gpu-era/) ·
[CIDR 2026 paper](https://vldb.org/cidrdb/papers/2026/p12-yogatama.pdf)
