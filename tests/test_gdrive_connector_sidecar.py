"""Sidecar-level tests for Google Drive connector local safety helpers."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_gdrive(monkeypatch, tmp_path, max_stage_bytes: str | None = "10"):
    module_path = (
        Path(__file__).resolve().parents[1] / "sidecars" / "gdrive-connector" / "main.py"
    )
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    if max_stage_bytes is not None:
        monkeypatch.setenv("GDRIVE_MAX_STAGE_BYTES", max_stage_bytes)

    module_name = f"rvbbit_gdrive_test_{os.getpid()}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_gdrive_sync_request_defaults_are_isolated(monkeypatch, tmp_path):
    sidecar = _load_gdrive(monkeypatch, tmp_path)

    first = sidecar.SyncRequest()
    second = sidecar.SyncRequest()
    first.folders.append("folder-a")
    first.known["file-a"] = "hash-a"

    assert second.folders == []
    assert second.known == {}


def test_gdrive_stage_rejects_declared_oversize_before_download(monkeypatch, tmp_path):
    sidecar = _load_gdrive(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="larger than the staging byte limit"):
        sidecar._stage(
            object(),
            {
                "id": "file-1",
                "name": "large.txt",
                "mimeType": "text/plain",
                "size": "11",
            },
            str(tmp_path / "staging"),
        )


def test_gdrive_safe_stage_name_removes_path_characters(monkeypatch, tmp_path):
    sidecar = _load_gdrive(monkeypatch, tmp_path)

    assert sidecar._safe_stage_name("../odd/id") == ".._odd_id"


def test_gdrive_invalid_numeric_env_uses_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv("GDRIVE_MAX_STAGE_BYTES", "not-an-int")
    monkeypatch.setenv("GDRIVE_PAGE_SIZE", "9001")
    sidecar = _load_gdrive(monkeypatch, tmp_path, max_stage_bytes=None)

    assert sidecar.MAX_STAGE_BYTES == 64 * 1024 * 1024
    assert sidecar.PAGE_SIZE == 1000
