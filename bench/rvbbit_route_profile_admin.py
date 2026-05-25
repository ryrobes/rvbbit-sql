#!/usr/bin/env python3
"""Admin helpers for native Rvbbit route profiles."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg


DEFAULT_DSN = os.environ.get("RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench")


def dump(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def active_profile_name(cur: psycopg.Cursor[Any]) -> str:
    cur.execute("SELECT name FROM rvbbit.route_profiles WHERE active ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        raise SystemExit("no active Rvbbit route profile")
    return str(row[0])


def cmd_export(args: argparse.Namespace) -> None:
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            profile_name = active_profile_name(cur) if args.active else args.name
            if not profile_name:
                raise SystemExit("export requires --name or --active")
            cur.execute("SELECT rvbbit.route_export_profile(%s)", (profile_name,))
            profile = cur.fetchone()[0]

    out = json.dumps(profile, indent=2, sort_keys=True, default=str)
    if args.output == "-":
        print(out)
        return
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out + "\n")
    print(f"exported {profile_name} to {path}")


def cmd_import(args: argparse.Namespace) -> None:
    path = Path(args.profile)
    profile = json.loads(path.read_text())
    profile_name = args.name or profile.get("name") or path.stem
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rvbbit.route_import_profile(%s, %s::jsonb, %s)",
                (profile_name, json.dumps(profile), args.active),
            )
            result = cur.fetchone()[0]
        conn.commit()
    dump(result)


def cmd_activate(args: argparse.Namespace) -> None:
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT rvbbit.route_activate_profile(%s)", (args.name,))
            result = cur.fetchone()[0]
        conn.commit()
    dump(result)


def cmd_retire(args: argparse.Namespace) -> None:
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT rvbbit.route_retire_profile(%s)", (args.name,))
            result = cur.fetchone()[0]
        conn.commit()
    dump(result)


def cmd_clone(args: argparse.Namespace) -> None:
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rvbbit.route_clone_profile(%s, %s, %s)",
                (args.source, args.target, args.active),
            )
            result = cur.fetchone()[0]
        conn.commit()
    dump(result)


def cmd_merge(args: argparse.Namespace) -> None:
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rvbbit.route_merge_profiles(%s, %s::jsonb, %s)",
                (args.target, json.dumps(args.sources), args.active),
            )
            result = cur.fetchone()[0]
        conn.commit()
    dump(result)


def cmd_eval(args: argparse.Namespace) -> None:
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            profile_name = active_profile_name(cur) if args.active else args.name
            if not profile_name:
                raise SystemExit("eval requires --name or --active")
            cur.execute("SELECT rvbbit.route_eval(%s)", (profile_name,))
            result = cur.fetchone()[0]
    dump(result)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dsn", default=DEFAULT_DSN)
    sub = p.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Export a route profile JSON document")
    export.add_argument("--name")
    export.add_argument("--active", action="store_true")
    export.add_argument("--output", "-o", default="-")
    export.set_defaults(func=cmd_export)

    import_ = sub.add_parser("import", help="Import a route profile JSON document")
    import_.add_argument("--profile", required=True)
    import_.add_argument("--name")
    import_.add_argument("--active", action="store_true")
    import_.set_defaults(func=cmd_import)

    activate = sub.add_parser("activate", help="Activate one profile")
    activate.add_argument("name")
    activate.set_defaults(func=cmd_activate)

    retire = sub.add_parser("retire", help="Retire one profile")
    retire.add_argument("name")
    retire.set_defaults(func=cmd_retire)

    clone = sub.add_parser("clone", help="Clone a profile")
    clone.add_argument("source")
    clone.add_argument("target")
    clone.add_argument("--active", action="store_true")
    clone.set_defaults(func=cmd_clone)

    merge = sub.add_parser("merge", help="Merge profiles")
    merge.add_argument("target")
    merge.add_argument("sources", nargs="+")
    merge.add_argument("--active", action="store_true")
    merge.set_defaults(func=cmd_merge)

    eval_ = sub.add_parser("eval", help="Summarize one profile")
    eval_.add_argument("--name")
    eval_.add_argument("--active", action="store_true")
    eval_.set_defaults(func=cmd_eval)

    return p


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
