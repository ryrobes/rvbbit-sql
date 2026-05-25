"""ClickBench per-system loaders.

Pattern matches columnar_comparison/loaders/: each loader exposes a
load(data_path, limit) -> dict that creates the hits table and
ingests up to `limit` rows.
"""
