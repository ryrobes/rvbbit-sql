from __future__ import annotations

import json
from pathlib import Path

import pytest

from .viz import FORMAT, load_curve_payload, render_html


def write_result(
    path: Path,
    rows: int,
    median_ms: float,
    *,
    width: int = 120,
) -> None:
    frame_hashes = ["frame-a", "frame-b"]
    path.write_text(
        json.dumps(
            {
                "world": "episode1",
                "frames": 2,
                "warmups": 1,
                "width": width,
                "height": 40,
                "draw_distance": 96,
                "turn_degrees": 15,
                "grid_scale": 16,
                "render_type": "ansi-half",
                "maps": ["E1M1", "E1M2"],
                "replay_session_sha256": None,
                "table": f"doomql_episode1_{rows}",
                "generated_at": "2026-07-13T12:00:00+00:00",
                "parity_reference": "duckdb",
                "environment": {"source_rows": rows},
                "results": [
                    {
                        "system": "duckdb",
                        "status": "ok",
                        "route": "duckdb",
                        "first_ms": median_ms * 1.2,
                        "median_ms": median_ms,
                        "p95_ms": median_ms * 1.1,
                        "fps": 1000 / median_ms,
                        "frame_hashes": frame_hashes,
                        "error": None,
                    },
                    {
                        "system": "postgres",
                        "status": "ok",
                        "route": "postgres_heap",
                        "first_ms": median_ms * 2.4,
                        "median_ms": median_ms * 2,
                        "p95_ms": median_ms * 2.2,
                        "fps": 500 / median_ms,
                        "frame_hashes": frame_hashes,
                        "error": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_curve_payload_sorts_scales_and_keeps_chart_metrics(tmp_path: Path):
    large = tmp_path / "large.json"
    small = tmp_path / "small.json"
    write_result(large, 50_000_000, 80)
    write_result(small, 5_000_000, 20)

    payload = load_curve_payload([large, small])

    assert payload["format"] == FORMAT
    assert payload["scales"] == [5_000_000, 50_000_000]
    assert payload["all_parity_ok"] is True
    assert payload["systems"] == [
        {"id": "duckdb", "label": "DuckDB"},
        {"id": "postgres", "label": "PostgreSQL"},
    ]
    assert payload["points"][1]["results"]["postgres"]["median_ms"] == 160
    assert payload["points"][1]["results"]["postgres"]["parity_ok"] is True

    html = render_html(payload)
    assert "DoomQL Scale Curves" in html
    assert '"format":"doomql-scale-curves-v1"' in html
    assert "https://" not in html


def test_curve_payload_rejects_incomparable_settings(tmp_path: Path):
    baseline = tmp_path / "baseline.json"
    incompatible = tmp_path / "incompatible.json"
    write_result(baseline, 5_000_000, 20)
    write_result(incompatible, 50_000_000, 80, width=160)

    with pytest.raises(ValueError, match="width"):
        load_curve_payload([baseline, incompatible])


def test_curve_payload_rejects_duplicate_scale(tmp_path: Path):
    first = tmp_path / "first.json"
    duplicate = tmp_path / "duplicate.json"
    write_result(first, 5_000_000, 20)
    write_result(duplicate, 5_000_000, 21)

    with pytest.raises(ValueError, match="duplicate 5,000,000-row result"):
        load_curve_payload([first, duplicate])
