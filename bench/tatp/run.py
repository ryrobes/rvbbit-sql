"""TATP-style transactional benchmark harness.

This is an engineering harness inspired by the TATP schema and transaction
mix, not an audited benchmark result. It is intentionally small and repeatable
so we can catch semantic and latency regressions in Rvbbit's write path.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import psycopg


PG_DSNS = {
    "pg_baseline": "postgresql://postgres:bench@bench-pg-baseline:5432/bench",
    "citus": "postgresql://postgres:bench@bench-citus:5432/bench",
    "hydra": "postgresql://postgres:bench@bench-hydra:5432/bench",
    "alloydb": "postgresql://postgres:bench@bench-alloydb:5432/postgres",
    "rvbbit": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench",
}


DDL = {
    "subscriber": """
        CREATE TABLE subscriber (
            s_id bigint NOT NULL,
            sub_nbr varchar(15) NOT NULL,
            bit_1 smallint NOT NULL,
            bit_2 smallint NOT NULL,
            hex_1 smallint NOT NULL,
            hex_2 smallint NOT NULL,
            byte2_1 integer NOT NULL,
            byte2_2 integer NOT NULL,
            msc_location integer NOT NULL,
            vlr_location integer NOT NULL
        ) {using}
    """,
    "access_info": """
        CREATE TABLE access_info (
            s_id bigint NOT NULL,
            ai_type smallint NOT NULL,
            data1 smallint NOT NULL,
            data2 smallint NOT NULL,
            data3 varchar(3) NOT NULL,
            data4 varchar(5) NOT NULL
        ) {using}
    """,
    "special_facility": """
        CREATE TABLE special_facility (
            s_id bigint NOT NULL,
            sf_type smallint NOT NULL,
            is_active smallint NOT NULL,
            error_cntrl smallint NOT NULL,
            data_a smallint NOT NULL,
            data_b varchar(5) NOT NULL
        ) {using}
    """,
    "call_forwarding": """
        CREATE TABLE call_forwarding (
            s_id bigint NOT NULL,
            sf_type smallint NOT NULL,
            start_time smallint NOT NULL,
            end_time smallint NOT NULL,
            numberx varchar(15) NOT NULL
        ) {using}
    """,
}


INDEXES = [
    "CREATE UNIQUE INDEX subscriber_sid_idx ON subscriber (s_id)",
    "CREATE UNIQUE INDEX subscriber_nbr_idx ON subscriber (sub_nbr)",
    "CREATE INDEX access_sid_type_idx ON access_info (s_id, ai_type)",
    "CREATE INDEX special_sid_type_idx ON special_facility (s_id, sf_type)",
    "CREATE INDEX callf_sid_type_start_idx ON call_forwarding (s_id, sf_type, start_time)",
]


COPY_COLUMNS = {
    "subscriber": "s_id, sub_nbr, bit_1, bit_2, hex_1, hex_2, byte2_1, byte2_2, msc_location, vlr_location",
    "access_info": "s_id, ai_type, data1, data2, data3, data4",
    "special_facility": "s_id, sf_type, is_active, error_cntrl, data_a, data_b",
    "call_forwarding": "s_id, sf_type, start_time, end_time, numberx",
}


@dataclass
class TxnResult:
    latencies_ms: list[float]
    errors: int
    error_samples: dict[str, int]


def using_for(system: str) -> str | None:
    mode = os.environ.get("TATP_TABLE_AM", "native")
    if mode == "heap":
        return None
    if system == "rvbbit":
        return "rvbbit"
    if system in {"citus", "hydra"}:
        return "columnar"
    return None


def _subscriber_rows(n: int) -> Iterable[tuple]:
    for sid in range(1, n + 1):
        yield (
            sid,
            f"{sid:015d}",
            sid % 2,
            (sid + 1) % 2,
            sid % 16,
            (sid * 3) % 16,
            sid % 256,
            (sid * 7) % 256,
            sid % 1_000_000,
            (sid * 13) % 1_000_000,
        )


def _access_rows(n: int) -> Iterable[tuple]:
    for sid in range(1, n + 1):
        for ai_type in range(1, 5):
            yield (sid, ai_type, sid % 256, (sid + ai_type) % 256, "abc", "defgh")


def _facility_rows(n: int) -> Iterable[tuple]:
    for sid in range(1, n + 1):
        for sf_type in range(1, 5):
            yield (sid, sf_type, 1 if (sid + sf_type) % 4 else 0, 0, sf_type, "fdata")


def _forwarding_rows(n: int) -> Iterable[tuple]:
    for sid in range(1, n + 1):
        yield (sid, 1, 0, 8, f"555{sid % 10_000_000:07d}")
        yield (sid, 2, 8, 16, f"556{sid % 10_000_000:07d}")


ROW_GENERATORS: dict[str, Callable[[int], Iterable[tuple]]] = {
    "subscriber": _subscriber_rows,
    "access_info": _access_rows,
    "special_facility": _facility_rows,
    "call_forwarding": _forwarding_rows,
}


def _copy_rows(cur, table: str, rows: Iterable[tuple]) -> int:
    stmt = f"COPY {table} ({COPY_COLUMNS[table]}) FROM STDIN"
    count = 0
    with cur.copy(stmt) as cp:
        for row in rows:
            cp.write_row(row)
            count += 1
    return count


def load_system(system: str, subscribers: int) -> dict:
    dsn = PG_DSNS[system]
    using = using_for(system)
    suffix = f"USING {using}" if using else ""
    t0 = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            if system == "citus":
                cur.execute("CREATE EXTENSION IF NOT EXISTS citus")
            for table in reversed(list(DDL.keys())):
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            for table, ddl in DDL.items():
                cur.execute(ddl.format(using=suffix))
            rows = 0
            for table in DDL:
                print(f"    copy {table}")
                rows += _copy_rows(cur, table, ROW_GENERATORS[table](subscribers))
            for idx in INDEXES:
                try:
                    cur.execute(idx)
                except Exception as e:
                    print(f"    index skipped: {str(e).splitlines()[0][:100]}")
            for table in DDL:
                cur.execute(f"ANALYZE {table}")
    return {
        "system": system,
        "rows": rows,
        "load_seconds": time.perf_counter() - t0,
        "using": using or "heap",
    }


def _rand_sid(rng: random.Random, subscribers: int) -> int:
    return rng.randint(1, subscribers)


def _run_txn(cur, rng: random.Random, subscribers: int) -> None:
    pick = rng.random()
    sid = _rand_sid(rng, subscribers)
    if pick < 0.35:
        cur.execute(
            "SELECT bit_1, bit_2, hex_1, hex_2, byte2_1, byte2_2, msc_location, vlr_location "
            "FROM subscriber WHERE s_id = %s",
            (sid,),
        )
        cur.fetchall()
    elif pick < 0.70:
        cur.execute(
            "SELECT data1, data2, data3, data4 FROM access_info WHERE s_id = %s AND ai_type = %s",
            (sid, rng.randint(1, 4)),
        )
        cur.fetchall()
    elif pick < 0.80:
        cur.execute(
            """
            SELECT cf.numberx
            FROM special_facility sf
            JOIN call_forwarding cf
              ON cf.s_id = sf.s_id AND cf.sf_type = sf.sf_type
            WHERE sf.s_id = %s
              AND sf.sf_type = %s
              AND sf.is_active = 1
              AND cf.start_time <= %s
              AND cf.end_time > %s
            """,
            (sid, rng.randint(1, 4), rng.randint(0, 23), rng.randint(0, 23)),
        )
        cur.fetchall()
    elif pick < 0.86:
        cur.execute(
            "UPDATE subscriber SET bit_1 = ((bit_1 + 1) %% 2)::smallint WHERE s_id = %s",
            (sid,),
        )
    elif pick < 0.96:
        cur.execute(
            "UPDATE subscriber SET vlr_location = %s WHERE sub_nbr = %s",
            (rng.randint(1, 1_000_000), f"{sid:015d}"),
        )
    elif pick < 0.98:
        cur.execute(
            "INSERT INTO call_forwarding VALUES (%s, %s, %s, %s, %s)",
            (
                sid,
                rng.randint(1, 4),
                rng.randint(0, 23),
                rng.randint(1, 24),
                f"557{sid % 10_000_000:07d}",
            ),
        )
    else:
        cur.execute(
            "DELETE FROM call_forwarding WHERE s_id = %s AND sf_type = %s AND start_time = %s",
            (sid, rng.randint(1, 4), rng.randint(0, 23)),
        )


def run_client(system: str, client_id: int, txns: int, subscribers: int, timeout_s: int) -> TxnResult:
    rng = random.Random(42 + client_id)
    latencies: list[float] = []
    errors = 0
    error_samples: dict[str, int] = {}
    with psycopg.connect(PG_DSNS[system], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {timeout_s * 1000}".encode())  # type: ignore[arg-type]
            for _ in range(txns):
                t0 = time.perf_counter()
                try:
                    _run_txn(cur, rng, subscribers)
                    latencies.append((time.perf_counter() - t0) * 1000)
                except Exception:
                    errors += 1
                    key = sys.exc_info()[1]
                    msg = str(key).splitlines()[0][:160] if key else "unknown"
                    error_samples[msg] = error_samples.get(msg, 0) + 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    return TxnResult(latencies, errors, error_samples)


def _percentile(xs: list[float], pct: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def run_system(system: str, subscribers: int, txns: int, clients: int, timeout_s: int) -> dict:
    per_client = [txns // clients for _ in range(clients)]
    for i in range(txns % clients):
        per_client[i] += 1
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [
            pool.submit(run_client, system, i, per_client[i], subscribers, timeout_s)
            for i in range(clients)
        ]
        results = [f.result() for f in futures]
    elapsed = time.perf_counter() - t0
    latencies = [x for r in results for x in r.latencies_ms]
    errors = sum(r.errors for r in results)
    error_samples: dict[str, int] = {}
    for r in results:
        for msg, count in r.error_samples.items():
            error_samples[msg] = error_samples.get(msg, 0) + count
    ok = len(latencies)
    return {
        "system": system,
        "txns": txns,
        "ok": ok,
        "errors": errors,
        "seconds": elapsed,
        "tps": ok / elapsed if elapsed > 0 else None,
        "median_ms": statistics.median(latencies) if latencies else None,
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
        "error_samples": error_samples,
    }


def fmt_ms(v: float | None) -> str:
    if v is None:
        return "-"
    if v < 1:
        return f"{v * 1000:.0f}µs"
    return f"{v:.1f}ms"


def main() -> int:
    systems = [
        s.strip()
        for s in os.environ.get("BENCH_SYSTEMS", "rvbbit,pg_baseline,citus,hydra,alloydb").split(",")
        if s.strip()
    ]
    subscribers = int(os.environ.get("TATP_SUBSCRIBERS", "100000"))
    txns = int(os.environ.get("TATP_TXNS", "20000"))
    clients = int(os.environ.get("TATP_CLIENTS", "1"))
    timeout_s = int(os.environ.get("TATP_TIMEOUT", "30"))
    out_path = "/bench/tatp/results/last_run.json"

    print("=== TATP-style transactional benchmark ===")
    print(f"systems     : {systems}")
    print(f"subscribers : {subscribers:,}")
    print(f"txns/system : {txns:,}")
    print(f"clients     : {clients}")
    print(f"table AM    : {os.environ.get('TATP_TABLE_AM', 'native')}")

    loads = []
    if not os.environ.get("SKIP_LOAD"):
        print("\n== loading")
        for system in systems:
            print(f"\n>>> loading {system}")
            try:
                loads.append(load_system(system, subscribers))
            except Exception as e:
                loads.append({"system": system, "status": f"FAIL: {str(e)[:120]}"})
    else:
        print("\n== skipping load")

    print("\n== running transactions")
    runs = []
    for system in systems:
        print(f"\n>>> running {system}")
        try:
            result = run_system(system, subscribers, txns, clients, timeout_s)
        except Exception as e:
            result = {"system": system, "error": str(e)[:200]}
        runs.append(result)
        if "error" in result:
            print(f"    FAIL: {result['error']}")
        else:
            print(
                f"    {result['tps']:.0f} tx/s, median {fmt_ms(result['median_ms'])}, "
                f"p95 {fmt_ms(result['p95_ms'])}, errors {result['errors']}"
            )
            for msg, count in list(result.get("error_samples", {}).items())[:3]:
                print(f"      error x{count}: {msg}")

    print("\n=== summary ===")
    print(f"{'system':<14} {'tx/s':>10} {'median':>10} {'p95':>10} {'p99':>10} {'errors':>10}")
    print("-" * 72)
    for r in runs:
        if "error" in r:
            print(f"{r['system']:<14} {'FAIL':>10} {'-':>10} {'-':>10} {'-':>10} {'-':>10}")
        else:
            print(
                f"{r['system']:<14} {r['tps']:>10.0f} {fmt_ms(r['median_ms']):>10} "
                f"{fmt_ms(r['p95_ms']):>10} {fmt_ms(r['p99_ms']):>10} {r['errors']:>10}"
            )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(
            {
                "suite": "TATP-style",
                "systems": systems,
                "subscribers": subscribers,
                "txns": txns,
                "clients": clients,
                "loads": loads,
                "runs": runs,
            },
            f,
            indent=2,
        )
    print(f"\nresults JSON: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
