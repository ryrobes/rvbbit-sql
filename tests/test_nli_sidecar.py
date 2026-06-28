"""Sidecar-level tests for NLI import-time configuration parsing."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _load_nli(monkeypatch):
    module_path = Path(__file__).resolve().parents[1] / "sidecars" / "nli" / "main.py"
    module_name = f"rvbbit_nli_test_{os.getpid()}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_nli_invalid_numeric_env_uses_defaults_and_bounds(monkeypatch):
    monkeypatch.setenv("NLI_BATCH_SIZE", "not-an-int")
    monkeypatch.setenv("NLI_CLASSIFY_MAX_LEN", "-20")
    monkeypatch.setenv("NLI_MAX_LEN", "999999")
    monkeypatch.setenv("NLI_ENTAIL_THRESHOLD", "not-a-float")
    monkeypatch.setenv("NLI_CONTRADICT_THRESHOLD", "9.0")

    sidecar = _load_nli(monkeypatch)

    assert sidecar.NLI_BATCH_SIZE == 64
    assert sidecar.CLASSIFY_MAX_LEN == 1
    assert sidecar.NLI_MAX_LEN == 8192
    assert sidecar.ENTAIL_THRESHOLD == 0.4
    assert sidecar.CONTRADICT_THRESHOLD == 1.0
