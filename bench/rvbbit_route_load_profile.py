#!/usr/bin/env python3
"""Load an offline Rvbbit route profile into the native router catalog."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg


DEFAULT_DSN = os.environ.get("RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="bench/rvbbit_route_profile.json")
    parser.add_argument("--name", default="bench-combined")
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--inactive", action="store_true")
    args = parser.parse_args()

    profile_path = Path(args.profile)
    profile = json.loads(profile_path.read_text())
    active = not args.inactive

    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rvbbit.route_import_profile(%s, %s::jsonb, %s)",
                (args.name, json.dumps(profile), active),
            )
            result = cur.fetchone()[0]
        conn.commit()

    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
