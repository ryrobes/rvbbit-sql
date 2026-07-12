#!/usr/bin/env python3
"""Run DoomQL across multiple row counts and print the crossover matrix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .load import DEFAULT_WAD, WORLD_COLUMNS, simple_identifier
    from .run import (
        DEFAULT_CLICKHOUSE_HOST,
        DEFAULT_CLICKHOUSE_PORT,
        DEFAULT_DSN,
        DEFAULT_POSTGRES_DSN,
        DEFAULT_SYSTEMS,
        fmt_ms,
    )
    from .wad_world import DEFAULT_GRID_SCALE
    from .workload import RENDER_TYPES
except ImportError:
    from load import DEFAULT_WAD, WORLD_COLUMNS, simple_identifier
    from run import (
        DEFAULT_CLICKHOUSE_HOST,
        DEFAULT_CLICKHOUSE_PORT,
        DEFAULT_DSN,
        DEFAULT_POSTGRES_DSN,
        DEFAULT_SYSTEMS,
        fmt_ms,
    )
    from wad_world import DEFAULT_GRID_SCALE
    from workload import RENDER_TYPES


HERE = Path(__file__).resolve().parent


def parse_scales(value: str) -> list[int]:
    try:
        scales = [int(item.strip().replace("_", "")) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("scales must be comma-separated integers") from exc
    if not scales or any(scale <= 0 for scale in scales):
        raise argparse.ArgumentTypeError("scales must contain positive integers")
    return scales


def run_command(command: list[str]) -> None:
    display = list(command)
    for flag in ("--dsn", "--postgres-dsn"):
        if flag in display:
            dsn_index = display.index(flag) + 1
            display[dsn_index] = display[dsn_index].rsplit("@", 1)[-1]
    printable = " ".join(display)
    print(f"\n$ {printable}", flush=True)
    subprocess.run(command, cwd=HERE.parents[1], check=True)


def print_matrix(runs: list[dict[str, Any]]) -> None:
    systems: list[str] = []
    for run in runs:
        for result in run["results"]:
            if result["system"] not in systems:
                systems.append(result["system"])
    print("\nDoomQL scale crossover (warm median)")
    print(f"{'rows':>12} " + " ".join(f"{system:>20}" for system in systems))
    for run in runs:
        by_system = {result["system"]: result for result in run["results"]}
        cells = []
        for system in systems:
            result = by_system.get(system)
            if not result or result["status"] != "ok":
                cells.append(f"{(result or {}).get('status', '-'):>20}")
            else:
                cells.append(f"{fmt_ms(result['median_ms']):>20}")
        print(f"{run['environment']['source_rows']:>12,} " + " ".join(cells))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--postgres-dsn", default=DEFAULT_POSTGRES_DSN)
    parser.add_argument("--clickhouse-host", default=DEFAULT_CLICKHOUSE_HOST)
    parser.add_argument("--clickhouse-port", type=int, default=DEFAULT_CLICKHOUSE_PORT)
    parser.add_argument("--table", type=simple_identifier, default="doomql_world")
    parser.add_argument("--world", choices=sorted(WORLD_COLUMNS), default="synthetic")
    parser.add_argument("--wad", type=Path, default=DEFAULT_WAD)
    parser.add_argument("--map-name", default="E1M1")
    parser.add_argument("--grid-scale", type=int, default=DEFAULT_GRID_SCALE)
    parser.add_argument("--scales", type=parse_scales, default=parse_scales("1000000,5000000,10000000"))
    parser.add_argument("--systems", default=DEFAULT_SYSTEMS)
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=40)
    parser.add_argument("--draw-distance", type=int, default=128)
    parser.add_argument("--render-type", choices=sorted(RENDER_TYPES), default="ascii")
    parser.add_argument("--copy-batch-rows", type=int, default=100_000)
    parser.add_argument("--skip-variants", action="store_true")
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--output", type=Path, default=HERE / "results" / "sweep-last.json")
    args = parser.parse_args()

    runs: list[dict[str, Any]] = []
    selected_systems = [item.strip() for item in args.systems.split(",") if item.strip()]
    competitor_targets = [
        system for system in ("postgres", "clickhouse") if system in selected_systems
    ]
    for scale in args.scales:
        parquet_name = (
            f"doomql_world_{scale}.parquet"
            if args.world == "synthetic"
            else f"doomql_{args.map_name.lower()}_{scale}.parquet"
        )
        parquet = HERE / "data" / parquet_name
        result_path = HERE / "results" / f"scale-{args.world}-{scale}.json"
        load_command = [
            sys.executable,
            str(HERE / "load.py"),
            "--dsn",
            args.dsn,
            "--table",
            args.table,
            "--world",
            args.world,
            "--rows",
            str(scale),
            "--copy-batch-rows",
            str(args.copy_batch_rows),
            "--parquet",
            str(parquet),
            "--wad",
            str(args.wad.expanduser()),
            "--map-name",
            args.map_name,
            "--grid-scale",
            str(args.grid_scale),
        ]
        if parquet.exists() and not args.regenerate:
            load_command.append("--reuse-parquet")
        if args.skip_variants:
            load_command.append("--skip-variants")
        run_command(load_command)

        if competitor_targets:
            run_command(
                [
                    sys.executable,
                    str(HERE / "load_competitors.py"),
                    "--parquet",
                    str(parquet),
                    "--table",
                    args.table,
                    "--world",
                    args.world,
                    "--targets",
                    ",".join(competitor_targets),
                    "--postgres-dsn",
                    args.postgres_dsn,
                    "--clickhouse-host",
                    args.clickhouse_host,
                    "--clickhouse-port",
                    str(args.clickhouse_port),
                    "--copy-batch-rows",
                    str(args.copy_batch_rows),
                    "--output",
                    str(HERE / "results" / f"competitor-load-{args.world}-{scale}.json"),
                ]
            )

        run_command(
            [
                sys.executable,
                str(HERE / "run.py"),
                "--dsn",
                args.dsn,
                "--table",
                args.table,
                "--world",
                args.world,
                "--wad",
                str(args.wad.expanduser()),
                "--map-name",
                args.map_name,
                "--grid-scale",
                str(args.grid_scale),
                "--postgres-dsn",
                args.postgres_dsn,
                "--clickhouse-host",
                args.clickhouse_host,
                "--clickhouse-port",
                str(args.clickhouse_port),
                "--parquet",
                str(parquet),
                "--systems",
                args.systems,
                "--frames",
                str(args.frames),
                "--warmups",
                str(args.warmups),
                "--width",
                str(args.width),
                "--height",
                str(args.height),
                "--draw-distance",
                str(args.draw_distance),
                "--render-type",
                args.render_type,
                "--output",
                str(result_path),
            ]
        )
        runs.append(json.loads(result_path.read_text(encoding="utf-8")))

    print_matrix(runs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "world": args.world,
        "map_name": args.map_name if args.world == "e1m1" else None,
        "scales": args.scales,
        "systems": args.systems,
        "render_type": args.render_type,
        "runs": runs,
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
