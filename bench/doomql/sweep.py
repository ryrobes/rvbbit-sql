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
        DEFAULT_ALLOYDB_DSN,
        DEFAULT_CITUS_DSN,
        DEFAULT_CLICKHOUSE_HOST,
        DEFAULT_CLICKHOUSE_PORT,
        DEFAULT_DSN,
        DEFAULT_HYDRA_DSN,
        DEFAULT_POSTGRES_DSN,
        DEFAULT_SYSTEMS,
        fmt_ms,
        load_session,
        system_label,
    )
    from .wad_world import DEFAULT_GRID_SCALE
    from .workload import RENDER_TYPES
except ImportError:
    from load import DEFAULT_WAD, WORLD_COLUMNS, simple_identifier
    from run import (
        DEFAULT_ALLOYDB_DSN,
        DEFAULT_CITUS_DSN,
        DEFAULT_CLICKHOUSE_HOST,
        DEFAULT_CLICKHOUSE_PORT,
        DEFAULT_DSN,
        DEFAULT_HYDRA_DSN,
        DEFAULT_POSTGRES_DSN,
        DEFAULT_SYSTEMS,
        fmt_ms,
        load_session,
        system_label,
    )
    from wad_world import DEFAULT_GRID_SCALE
    from workload import RENDER_TYPES


HERE = Path(__file__).resolve().parent


def parse_scales(value: str) -> list[int]:
    multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    try:
        scales = []
        for item in value.split(","):
            token = item.strip().lower().replace("_", "")
            if not token:
                continue
            multiplier = multipliers.get(token[-1], 1)
            number = token[:-1] if multiplier != 1 else token
            scales.append(int(number) * multiplier)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "scales must be comma-separated integers or k/m/b values"
        ) from exc
    if not scales or any(scale <= 0 for scale in scales):
        raise argparse.ArgumentTypeError("scales must contain positive integers")
    return scales


def scale_label(scale: int) -> str:
    for suffix, divisor in (("b", 1_000_000_000), ("m", 1_000_000), ("k", 1_000)):
        if scale % divisor == 0:
            return f"{scale // divisor}{suffix}"
    return str(scale)


def scale_table(base_table: str, scale: int) -> str:
    return simple_identifier(f"{base_table}_{scale_label(scale)}")


def run_command(command: list[str]) -> None:
    display = list(command)
    for flag in (
        "--dsn",
        "--postgres-dsn",
        "--citus-dsn",
        "--hydra-dsn",
        "--alloydb-dsn",
    ):
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
    labels = {system: system_label(system) for system in systems}
    widths = {system: max(20, len(labels[system])) for system in systems}
    print(
        f"{'rows':>12} "
        + " ".join(f"{labels[system]:>{widths[system]}}" for system in systems)
    )
    for run in runs:
        by_system = {result["system"]: result for result in run["results"]}
        cells = []
        for system in systems:
            result = by_system.get(system)
            if not result or result["status"] != "ok":
                cells.append(
                    f"{(result or {}).get('status', '-'):>{widths[system]}}"
                )
            else:
                cells.append(f"{fmt_ms(result['median_ms']):>{widths[system]}}")
        print(f"{run['environment']['source_rows']:>12,} " + " ".join(cells))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--postgres-dsn", default=DEFAULT_POSTGRES_DSN)
    parser.add_argument("--citus-dsn", default=DEFAULT_CITUS_DSN)
    parser.add_argument("--hydra-dsn", default=DEFAULT_HYDRA_DSN)
    parser.add_argument("--alloydb-dsn", default=DEFAULT_ALLOYDB_DSN)
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
    parser.add_argument("--replay-session", type=Path)
    parser.add_argument("--copy-batch-rows", type=int, default=100_000)
    parser.add_argument("--skip-variants", action="store_true")
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--keep-loaded", action="store_true")
    parser.add_argument("--skip-load", action="store_true")
    parser.add_argument("--output", type=Path, default=HERE / "results" / "sweep-last.json")
    args = parser.parse_args()
    if args.replay_session is not None:
        try:
            replay_settings, _, _ = load_session(args.replay_session)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        args.world = str(replay_settings["world"])
        args.wad = Path(str(replay_settings["wad"]))
        args.map_name = str(replay_settings["map_name"])
        args.grid_scale = int(replay_settings["grid_scale"])
        args.table = simple_identifier(str(replay_settings["table"]))
        args.width = int(replay_settings["width"])
        args.height = int(replay_settings["height"])
        args.draw_distance = int(replay_settings["draw_distance"])
        args.render_type = str(replay_settings["render_type"])

    runs: list[dict[str, Any]] = []
    selected_systems = [item.strip() for item in args.systems.split(",") if item.strip()]
    competitor_targets = [
        system
        for system in ("postgres", "citus", "hydra", "alloydb", "clickhouse")
        if system in selected_systems
    ]
    tables: dict[int, str] = {}
    for scale in args.scales:
        table = scale_table(args.table, scale) if args.keep_loaded else args.table
        tables[scale] = table
        parquet_name = (
            f"doomql_world_{scale}.parquet"
            if args.world == "synthetic"
            else f"doomql_{args.map_name.lower()}_{scale}.parquet"
        )
        parquet = HERE / "data" / parquet_name
        result_path = HERE / "results" / f"scale-{args.world}-{scale}.json"
        if not args.skip_load:
            load_command = [
                sys.executable,
                str(HERE / "load.py"),
                "--dsn",
                args.dsn,
                "--table",
                table,
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

        if competitor_targets and not args.skip_load:
            run_command(
                [
                    sys.executable,
                    str(HERE / "load_competitors.py"),
                    "--parquet",
                    str(parquet),
                    "--table",
                    table,
                    "--world",
                    args.world,
                    "--targets",
                    ",".join(competitor_targets),
                    "--postgres-dsn",
                    args.postgres_dsn,
                    "--citus-dsn",
                    args.citus_dsn,
                    "--hydra-dsn",
                    args.hydra_dsn,
                    "--alloydb-dsn",
                    args.alloydb_dsn,
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

        run_args = [
            sys.executable,
            str(HERE / "run.py"),
            "--dsn",
            args.dsn,
            "--postgres-dsn",
            args.postgres_dsn,
            "--citus-dsn",
            args.citus_dsn,
            "--hydra-dsn",
            args.hydra_dsn,
            "--alloydb-dsn",
            args.alloydb_dsn,
            "--clickhouse-host",
            args.clickhouse_host,
            "--clickhouse-port",
            str(args.clickhouse_port),
            "--systems",
            args.systems,
            "--warmups",
            str(args.warmups),
            "--output",
            str(result_path),
        ]
        if args.replay_session is not None:
            run_args.extend(
                [
                    "--replay-session",
                    str(args.replay_session.expanduser()),
                    "--replay-table",
                    table,
                    "--replay-parquet",
                    str(parquet),
                ]
            )
        else:
            run_args.extend(
                [
                    "--table",
                    table,
                    "--world",
                    args.world,
                    "--wad",
                    str(args.wad.expanduser()),
                    "--map-name",
                    args.map_name,
                    "--grid-scale",
                    str(args.grid_scale),
                    "--parquet",
                    str(parquet),
                    "--frames",
                    str(args.frames),
                    "--width",
                    str(args.width),
                    "--height",
                    str(args.height),
                    "--draw-distance",
                    str(args.draw_distance),
                    "--render-type",
                    args.render_type,
                ]
            )
        run_command(run_args)
        runs.append(json.loads(result_path.read_text(encoding="utf-8")))

    print_matrix(runs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "world": args.world,
        "map_name": args.map_name if args.world == "e1m1" else None,
        "scales": args.scales,
        "tables": {str(scale): table for scale, table in tables.items()},
        "systems": args.systems,
        "render_type": args.render_type,
        "replay_session": (
            str(args.replay_session.expanduser())
            if args.replay_session is not None
            else None
        ),
        "runs": runs,
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
