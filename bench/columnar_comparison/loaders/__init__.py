"""Per-system loaders for the cross-DB bench.

Each loader exposes a load(data_dir: str) -> dict function that:
  - drops + recreates the target table
  - ingests every parquet file in data_dir
  - returns {"rows": int, "load_seconds": float, "size_bytes": int|None}

Loaders are kept simple — one function per system, one import path per
function so an unavailable system doesn't break the others.
"""
