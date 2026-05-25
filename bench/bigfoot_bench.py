"""Bigfoot semantic-SQL bench.

Loads the BFRO sightings CSV into the rvbbit instance and runs the
user's example multi-operator query under different conditions to
characterize where time goes:

  baseline_cold  : sequential, no cache (first run)
  warm_cache     : sequential, all cache hits (L1 hot)
  parallel_cold  : PG parallel workers, no cache
  parallel_warm  : PG parallel workers, all cache hits

Usage from inside the bench container:

    docker compose exec -e LIMIT=100 bench python /bench/bigfoot_bench.py load
    docker compose exec -e LIMIT=100 bench python /bench/bigfoot_bench.py run

LIMIT caps the row count so you can dry-run small before committing
real money on 15k LLM calls. Defaults to LIMIT=20.
"""
from __future__ import annotations

import csv
import os
import sys
import time

import psycopg

CSV_PATH_CANDIDATES = [
    "/csv-files/bigfoot_sightings.csv",         # bench container mount
    "/bench/bigfoot_sightings.csv",             # copied next to script
    "/home/ryanr/csv-files/bigfoot_sightings.csv",  # host path
]
RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)
LIMIT = int(os.environ.get("LIMIT", "20"))


def load_table():
    """Create a clean bigfoot_sightings table with just the columns we'll
    semantically query, then COPY the CSV in."""
    with psycopg.connect(RVBBIT_DSN, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS bigfoot_sightings")
        c.execute("""
            CREATE TABLE bigfoot_sightings (
                bfroid   text PRIMARY KEY,
                title    text,
                state    text,
                county   text,
                observed text
            )
        """)
        csv_path = next((p for p in CSV_PATH_CANDIDATES if os.path.exists(p)), None)
        if csv_path is None:
            print(f"CSV not found. Tried: {CSV_PATH_CANDIDATES}", file=sys.stderr)
            sys.exit(1)
        print(f"loading from {csv_path}")
        n = 0
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            with c.cursor().copy(
                "COPY bigfoot_sightings (bfroid, title, state, county, observed) FROM STDIN"
            ) as cp:
                for row in reader:
                    cp.write_row((
                        row.get("bfroid") or row.get("BfroId") or "",
                        row.get("title") or "",
                        row.get("state") or "",
                        row.get("county") or "",
                        (row.get("observed") or "")[:8000],  # cap to keep prompts manageable
                    ))
                    n += 1
        print(f"loaded {n} rows into bigfoot_sightings")


# The user's example query — 3 operator calls per row.
# Runs against bigfoot_sample (a fixed N-row copy of the full table)
# so PG's parallel-scan planner has something to chew on. LIMIT in the
# main query forces a serial Limit+SeqScan and defeats parallel workers,
# which is why we sample upfront instead.
USER_QUERY = """
    SELECT
      rvbbit.safe_classify(observed, 'visual,audio,encounter') AS category,
      rvbbit.summarize(observed) AS short_summary,
      rvbbit.summarize(title)    AS short_title
    FROM bigfoot_sample
"""


def make_sample(limit: int) -> None:
    with psycopg.connect(RVBBIT_DSN, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS bigfoot_sample")
        c.execute(
            f"CREATE TABLE bigfoot_sample AS "
            f"SELECT * FROM bigfoot_sightings ORDER BY bfroid LIMIT {limit}"  # type: ignore[arg-type]
        )
        # Tell PG this table is worth parallelizing even though it's small,
        # and disable the cost-model floor so 8 workers actually launch.
        c.execute("ALTER TABLE bigfoot_sample SET (parallel_workers = 8)")
        c.execute("ANALYZE bigfoot_sample")


def _set_parallel(c: psycopg.Connection, n: int) -> None:
    c.execute(f"SET max_parallel_workers_per_gather = {n}".encode())  # type: ignore[arg-type]
    c.execute(b"SET parallel_setup_cost = 0")  # type: ignore[arg-type]
    c.execute(b"SET parallel_tuple_cost = 0")  # type: ignore[arg-type]
    c.execute(b"SET min_parallel_table_scan_size = 0")  # type: ignore[arg-type]
    # PG's parallel mode forbids INSERTs from any worker (including the
    # leader during a parallel scan). Skip receipt writes for the run;
    # cache still works per-backend.
    if n > 0:
        c.execute(b"SELECT rvbbit.set_skip_receipts(true)")  # type: ignore[arg-type]
    else:
        c.execute(b"SELECT rvbbit.set_skip_receipts(false)")  # type: ignore[arg-type]


def time_query(label: str, dsn: str, sql: str, params=(), parallel_workers: int = 0,
               flush_cache: bool = False, flush_receipts: bool = False) -> float:
    with psycopg.connect(dsn, autocommit=True) as c:
        if flush_receipts:
            c.execute("TRUNCATE rvbbit.receipts")
        if flush_cache:
            c.execute("SELECT rvbbit.flush_cache()")
        _set_parallel(c, parallel_workers)
        t0 = time.perf_counter()
        rows = c.execute(sql, params).fetchall()  # type: ignore[arg-type]
        elapsed = time.perf_counter() - t0
        print(f"  {label:<36}  {elapsed:7.2f}s   ({len(rows)} rows)")
        return elapsed


SENTIMENT_SPEC = "sentiment_bench"
SENTIMENT_OP = "sentiment_bigfoot"
SENTIMENT_URL = "http://rvbbit-sentiment:8080/predict"


def _sentiment_alive() -> bool:
    """Reach the sentiment sidecar from the bench container."""
    import urllib.request
    try:
        urllib.request.urlopen("http://rvbbit-sentiment:8080/health", timeout=3).read()
        return True
    except Exception:
        return False


def _setup_sentiment(dsn: str) -> None:
    """Idempotent — register the specialist + operator. Safe to re-run."""
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(
            "SELECT rvbbit.register_backend("
            "  backend_name => %s, backend_endpoint => %s, "
            "  backend_batch_size => 32, backend_timeout_ms => 60000)",
            (SENTIMENT_SPEC, SENTIMENT_URL),
        )
        c.execute("SELECT rvbbit.reload_backends()")
        c.execute(f"DELETE FROM rvbbit.operators WHERE name = '{SENTIMENT_OP}'")
        c.execute(f"DROP FUNCTION IF EXISTS rvbbit.{SENTIMENT_OP}(text, jsonb)")
        c.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'jsonb', "
            "  op_system => 'unused', op_user => 'unused', "
            "  op_steps => %s::jsonb)",
            (
                SENTIMENT_OP,
                """[{"name":"s","kind":"specialist","specialist":\""""
                + SENTIMENT_SPEC + """\",
                     "inputs":{"text":"{{ inputs.text }}"}}]""",
            ),
        )


def _maybe_run_sentiment_pass(n_rows: int):
    """Part B of the bench: same rows, but classified by the sentiment
    sidecar instead of the safe_classify LLM op. Returns dict with the
    measurements, or None if the sidecar isn't reachable."""
    if not _sentiment_alive():
        print("\nPART B (sentiment specialist): SKIPPED — sidecar not reachable. "
              "Bring it up with: docker compose -f docker/docker-compose.yml "
              "-f docker/docker-compose.sidecars.yml --profile models up -d sentiment")
        return None

    print(f"\nPART B: specialist sidecar (sentiment, DistilBERT-SST-2) "
          f"on {n_rows} rows × 1 op = {n_rows} calls")
    _setup_sentiment(RVBBIT_DSN)

    # Warmup so the first measured pass doesn't pay the model-load cost.
    with psycopg.connect(RVBBIT_DSN, autocommit=True) as c:
        try:
            c.execute(f"SELECT rvbbit.{SENTIMENT_OP}('warming up')").fetchone()
        except Exception as e:
            print(f"  warmup failed: {e}")
            return None

    query = f"SELECT rvbbit.{SENTIMENT_OP}(observed) FROM bigfoot_sample"

    print("Pass B1: COLD cache (sequential)")
    cold_seq = time_query(
        "  serial, no cache",
        RVBBIT_DSN, query, (),
        parallel_workers=0, flush_cache=True, flush_receipts=True,
    )

    print("\nPass B2: WARM cache (L1 hits)")
    warm_seq = time_query(
        "  serial, full warm cache",
        RVBBIT_DSN, query, (),
        parallel_workers=0, flush_cache=False, flush_receipts=False,
    )

    print("\nPass B3: PREWARM (batched, batch_size=32) + sequential query")
    with psycopg.connect(RVBBIT_DSN, autocommit=True) as c:
        c.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{SENTIMENT_OP}'")
        c.execute("SELECT rvbbit.flush_cache()")

        prewarm_t0 = time.perf_counter()
        r = c.execute(
            f"SELECT * FROM rvbbit.prewarm_operator("
            f"  '{SENTIMENT_OP}', "
            f"  $$SELECT observed AS text FROM bigfoot_sample$$, "
            f"  32)"
        ).fetchone()
        prewarm_t = time.perf_counter() - prewarm_t0
        if r is not None:
            n_in, n_hits, n_exec, n_err, wall_ms = r
            print(f"    prewarm: {n_in} inputs, {n_hits} cache, "
                  f"{n_exec} new calls, {n_err} err, {wall_ms} ms")

        query_t0 = time.perf_counter()
        c.execute(query.encode()).fetchall()  # type: ignore[arg-type]
        query_t = time.perf_counter() - query_t0
        print(f"    prewarm phase  : {prewarm_t:7.2f}s")
        print(f"    serial query   : {query_t:7.2f}s")
        print(f"    TOTAL          : {prewarm_t + query_t:7.2f}s")

    print(f"\n=== Part B summary ({n_rows} rows × 1 op = {n_rows} calls) ===")
    print(f"  cold serial      : {cold_seq:7.2f}s   ({cold_seq / max(n_rows,1) * 1000:.1f} ms/call)")
    print(f"  warm L1 hits     : {warm_seq:7.2f}s   ({warm_seq / max(n_rows,1) * 1000:.2f} ms/call)")
    print(f"  prewarm + query  : {prewarm_t + query_t:7.2f}s   "
          f"speedup vs serial: {cold_seq/max(prewarm_t+query_t, 0.001):.1f}x")
    return {
        "cold_seq": cold_seq,
        "warm_seq": warm_seq,
        "prewarm_total": prewarm_t + query_t,
    }


def run_bench():
    print(f"\n=== bigfoot semantic-SQL bench, LIMIT={LIMIT} ===\n")
    make_sample(LIMIT)
    with psycopg.connect(RVBBIT_DSN, autocommit=True) as c:
        row = c.execute("SELECT count(*) FROM bigfoot_sample").fetchone()
        n = row[0] if row else 0
        n_ops = n * 3
        print(f"sampled {n} rows into bigfoot_sample ({n_ops} ops per pass)\n")

    print("Pass 1: COLD cache (true first-run cost, sequential)")
    cold_seq = time_query(
        "  serial, no cache",
        RVBBIT_DSN, USER_QUERY, (),
        parallel_workers=0, flush_cache=True, flush_receipts=True,
    )

    print("\nPass 2: WARM cache (L1 + L2 hits — repeated query)")
    warm_seq = time_query(
        "  serial, full warm cache",
        RVBBIT_DSN, USER_QUERY, (),
        parallel_workers=0, flush_cache=False, flush_receipts=False,
    )

    print("\nPass 3: WARM but L1 flushed (forces L2 SPI roundtrip per hit)")
    warm_l2_only = time_query(
        "  serial, L1 flushed (L2 hits)",
        RVBBIT_DSN, USER_QUERY, (),
        parallel_workers=0, flush_cache=True, flush_receipts=False,
    )

    print("\nPass 4: PG parallel workers (cold cache; 8-way)")
    cold_par = time_query(
        "  8 PG workers, no cache",
        RVBBIT_DSN, USER_QUERY, (),
        parallel_workers=8, flush_cache=True, flush_receipts=True,
    )

    print("\nPass 5: PG parallel workers (warm cache)")
    warm_par = time_query(
        "  8 PG workers, warm cache",
        RVBBIT_DSN, USER_QUERY, (),
        parallel_workers=8, flush_cache=False, flush_receipts=False,
    )

    # ---- Pre-warm approach: thread pool drives cross-row parallelism --
    print("\nPass 6: PREWARM (thread pool) + sequential query")
    with psycopg.connect(RVBBIT_DSN, autocommit=True) as c:
        c.execute(b"TRUNCATE rvbbit.receipts")  # type: ignore[arg-type]
        c.execute(b"SELECT rvbbit.flush_cache()")  # type: ignore[arg-type]

        prewarm_t0 = time.perf_counter()
        # Three prewarms (one per unique op/input combo in the user query)
        for spec_sql in [
            "SELECT * FROM rvbbit.prewarm_operator("
            "  'safe_classify', "
            "  $$SELECT observed AS text, 'visual,audio,encounter' AS categories FROM bigfoot_sample$$, "
            "  32)",
            "SELECT * FROM rvbbit.prewarm_operator("
            "  'summarize', "
            "  $$SELECT observed AS text FROM bigfoot_sample$$, "
            "  32)",
            "SELECT * FROM rvbbit.prewarm_operator("
            "  'summarize', "
            "  $$SELECT title AS text FROM bigfoot_sample$$, "
            "  32)",
        ]:
            r = c.execute(spec_sql.encode()).fetchone()  # type: ignore[arg-type]
            if r is None:
                continue
            n_in, n_hits, n_exec, n_err, wall_ms = r
            print(f"    prewarm: {n_in} inputs, {n_hits} cache, "
                  f"{n_exec} new calls, {n_err} err, {wall_ms} ms")
        prewarm_t = time.perf_counter() - prewarm_t0

        query_t0 = time.perf_counter()
        c.execute(USER_QUERY.encode()).fetchall()  # type: ignore[arg-type]
        query_t = time.perf_counter() - query_t0

        prewarm_total = prewarm_t + query_t
        print(f"    prewarm phase  : {prewarm_t:7.2f}s")
        print(f"    serial query   : {query_t:7.2f}s")
        print(f"    TOTAL          : {prewarm_total:7.2f}s")

    # ---- PART B: specialist sidecar (sentiment) over the SAME rows ---
    # Runs before the summary so the cross-comparison block below can use it.
    sentiment_section = _maybe_run_sentiment_pass(n)

    print(f"\n=== summary ({n} rows × 3 ops = {n_ops} calls) ===")
    print(f"  cold serial      : {cold_seq:7.2f}s   ({cold_seq / n_ops * 1000:.0f} ms/call)")
    print(f"  warm L1 hits     : {warm_seq:7.2f}s   ({warm_seq / n_ops * 1000:.2f} ms/call)")
    print(f"  warm L2-only     : {warm_l2_only:7.2f}s   ({warm_l2_only / n_ops * 1000:.2f} ms/call)")
    print(f"  cold + parallel  : {cold_par:7.2f}s   speedup vs serial: {cold_seq/cold_par:.1f}x")
    print(f"  warm + parallel  : {warm_par:7.2f}s")
    print(f"  prewarm + query  : {prewarm_total:7.2f}s   "
          f"speedup vs serial: {cold_seq/prewarm_total:.1f}x   "
          f"(prewarm {prewarm_t:.1f}s, query {query_t:.3f}s)")

    # ---- cross-comparison: LLM op vs specialist op, same rows ---------
    # (sentiment_section comes from _maybe_run_sentiment_pass earlier in run_bench)
    if sentiment_section is not None:
        # LLM has 3 ops per row, sentiment has 1 — normalize to per-call.
        llm_cold_per_call = cold_seq / n_ops * 1000
        sent_cold_per_call = sentiment_section["cold_seq"] / max(n, 1) * 1000
        speedup_cold = llm_cold_per_call / max(sent_cold_per_call, 0.001)
        print("\n=== cross-comparison: LLM op vs specialist op (per-call) ===")
        print(f"  LLM safe_classify    cold: {llm_cold_per_call:8.1f} ms/call")
        print(f"  specialist sentiment cold: {sent_cold_per_call:8.1f} ms/call   "
              f"({speedup_cold:.0f}x faster)")
        print("  cost/call: LLM ~$0.001-$0.005 (provider-dependent); specialist $0")

    print("\nReceipt audit:")
    with psycopg.connect(RVBBIT_DSN, autocommit=True) as c:
        for row in c.execute("""
            SELECT operator, count(*) AS calls,
                   round(avg(latency_ms))::int AS avg_ms,
                   sum(n_tokens_in) AS toks_in, sum(n_tokens_out) AS toks_out
            FROM rvbbit.receipts
            GROUP BY operator ORDER BY operator
        """).fetchall():
            print(f"  {row[0]:<20}  {row[1]:>5} calls   "
                  f"avg {row[2]:>5} ms   "
                  f"{row[3]:>7} tok_in / {row[4]:>6} tok_out")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "load":
        load_table()
    elif cmd == "run":
        run_bench()
    elif cmd == "both":
        load_table()
        run_bench()
    else:
        print("usage: bigfoot_bench.py [load|run|both]")
        sys.exit(2)
