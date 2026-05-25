"""Download NYC TLC yellow-taxi parquets for the cross-DB bench.

Run from inside the bench container (which has psycopg + requests):
  docker compose exec bench python /bench/columnar_comparison/download_taxi.py

Defaults: 3 months of 2023 yellow trips (~9-10M rows total, ~150MB on disk).
Override months / year:
  TAXI_YEAR=2023 TAXI_MONTHS=01,02,03,04 python download_taxi.py

The files land in /data (mounted at bench/columnar_comparison/data/ on
the host, mounted read-only into every competitor container as /data).
"""
from __future__ import annotations

import os
import sys
import urllib.request

DATA_DIR = "/data"
URL_TMPL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_{year}-{month:02d}.parquet"


def parse_months(raw: str) -> list[int]:
    out = []
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        n = int(s)
        if not 1 <= n <= 12:
            raise ValueError(f"month out of range: {n}")
        out.append(n)
    return out


def download_one(year: int, month: int) -> str:
    fname = f"yellow_tripdata_{year}-{month:02d}.parquet"
    dest = os.path.join(DATA_DIR, fname)
    if os.path.exists(dest):
        size_mb = os.path.getsize(dest) / 1024 / 1024
        print(f"  {fname}: already present ({size_mb:.1f} MB)")
        return dest
    url = URL_TMPL.format(year=year, month=month)
    print(f"  {fname}: downloading from {url}")
    tmp = dest + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.rename(tmp, dest)
    size_mb = os.path.getsize(dest) / 1024 / 1024
    print(f"  {fname}: done ({size_mb:.1f} MB)")
    return dest


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    year = int(os.environ.get("TAXI_YEAR", "2023"))
    months = parse_months(os.environ.get("TAXI_MONTHS", "01,02,03"))
    print(f"NYC TLC yellow taxi {year}, months: {months}")
    print(f"writing to {DATA_DIR}")
    paths = [download_one(year, m) for m in months]
    total = sum(os.path.getsize(p) for p in paths) / 1024 / 1024
    print(f"\ntotal: {len(paths)} files, {total:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
