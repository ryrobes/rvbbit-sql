"""Sidecar-level tests for managed Python operator execution."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


def _load_runtime(monkeypatch, tmp_path):
    runtime_path = (
        Path(__file__).resolve().parents[1] / "sidecars" / "python-runtime" / "main.py"
    )
    monkeypatch.setenv("RVBBIT_PYTHON_ENVS_DIR", str(tmp_path / "envs"))
    monkeypatch.setenv("RVBBIT_PYTHON_HANDLERS_DIR", str(tmp_path / "handlers"))

    module_name = f"rvbbit_python_runtime_test_{os.getpid()}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, runtime_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_sidecar_reconciles_venv_handler_and_runs(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    client = TestClient(runtime.app)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    payload = {
        "env": {
            "name": "ops_rules",
            "python_version": version,
            "requirements": [],
            "env_hash": "a" * 32,
        },
        "handler": {
            "name": "sla_score",
            "code_hash": "b" * 64,
            "entrypoint": "run",
            "code": (
                "def run(inputs):\n"
                "    print('scoring ticket')\n"
                "    priority = 4 if inputs['tier'] == 'enterprise' else 2\n"
                "    return {'priority': priority, 'breached': inputs['age_hours'] > 24}\n"
            ),
        },
        "inputs": {"tier": "enterprise", "age_hours": 31},
        "timeout_ms": 5000,
    }

    first = client.post("/run", json=payload)
    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert first.json()["output"] == {"priority": 4, "breached": True}
    assert first.json()["stdout"] == "scoring ticket\n"

    second = client.post("/run", json=payload)
    assert second.json()["ok"] is True
    assert client.get("/debug/stats").json()["env_builds"] == 1
    assert client.get("/debug/stats").json()["handler_writes"] == 1
