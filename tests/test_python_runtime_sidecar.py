"""Sidecar-level tests for managed Python operator execution."""

import hashlib
import importlib.util
import json
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


def _payload(
    *,
    env_name="ops_rules",
    handler_name="sla_score",
    code: str,
    inputs,
    entrypoint="run",
    timeout_ms=5000,
):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {
        "env": {
            "name": env_name,
            "python_version": version,
            "requirements": [],
            "env_hash": hashlib.md5(f"{version}\n".encode()).hexdigest(),
        },
        "handler": {
            "name": handler_name,
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "entrypoint": entrypoint,
            "code": code,
        },
        "inputs": inputs,
        "timeout_ms": timeout_ms,
    }


def test_sidecar_reconciles_venv_handler_and_runs(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    client = TestClient(runtime.app)
    payload = _payload(
        code=(
            "def run(inputs):\n"
            "    print('scoring ticket')\n"
            "    priority = 4 if inputs['tier'] == 'enterprise' else 2\n"
            "    return {'priority': priority, 'breached': inputs['age_hours'] > 24}\n"
        ),
        inputs={"tier": "enterprise", "age_hours": 31},
    )

    first = client.post("/run", json=payload)
    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert first.json()["output"] == {"priority": 4, "breached": True}
    assert first.json()["stdout"] == "scoring ticket\n"

    second = client.post("/run", json=payload)
    assert second.json()["ok"] is True
    assert client.get("/debug/stats").json()["env_builds"] == 1
    assert client.get("/debug/stats").json()["handler_writes"] == 1


def test_sidecar_uses_declared_entrypoint_for_scalar_inputs(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    client = TestClient(runtime.app)
    payload = _payload(
        handler_name="double_value",
        entrypoint="transform",
        code=(
            "def transform(inputs):\n"
            "    return {'value': int(inputs) * 2, 'kind': type(inputs).__name__}\n"
        ),
        inputs=21,
    )

    res = client.post("/run", json=payload)

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["output"] == {"value": 42, "kind": "int"}


def test_sidecar_returns_handler_failures_with_stderr(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    client = TestClient(runtime.app)
    payload = _payload(
        handler_name="broken_rule",
        code=(
            "def run(inputs):\n"
            "    print('about to fail')\n"
            "    raise ValueError('bad support ticket')\n"
        ),
        inputs={"ticket_id": 7},
    )

    res = client.post("/run", json=payload)
    body = res.json()

    assert res.status_code == 200
    assert body["ok"] is False
    assert body["error"] == "bad support ticket"
    assert "ValueError: bad support ticket" in body["stderr"]
    assert body["stdout"] == ""
    assert client.get("/debug/stats").json()["failures"] == 1


def test_sidecar_times_out_long_running_handlers(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    client = TestClient(runtime.app)
    payload = _payload(
        handler_name="slow_rule",
        code=(
            "import time\n"
            "def run(inputs):\n"
            "    time.sleep(1)\n"
            "    return {'done': True}\n"
        ),
        inputs={"ticket_id": 8},
        timeout_ms=50,
    )

    res = client.post("/run", json=payload)
    body = res.json()

    assert res.status_code == 200
    assert body["ok"] is False
    assert "timed out" in body["error"]
    assert client.get("/debug/stats").json()["failures"] == 1


def test_sidecar_rejects_invalid_hashes_before_reconcile(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    client = TestClient(runtime.app)
    payload = _payload(
        handler_name="hash_rule",
        code="def run(inputs):\n    return inputs\n",
        inputs={"ok": True},
    )
    payload["env"]["env_hash"] = "not-a-hex-hash"

    res = client.post("/run", json=payload)
    body = res.json()

    assert res.status_code == 200
    assert body["ok"] is False
    assert "env_hash must be a lowercase hex hash" in body["error"]
    assert not (tmp_path / "envs").exists()


def test_sidecar_rejects_code_hash_mismatch_before_reconcile(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    client = TestClient(runtime.app)
    payload = _payload(
        handler_name="hash_rule",
        code="def run(inputs):\n    return inputs\n",
        inputs={"ok": True},
    )
    payload["handler"]["code_hash"] = hashlib.sha256(b"different code").hexdigest()

    res = client.post("/run", json=payload)
    body = res.json()

    assert res.status_code == 200
    assert body["ok"] is False
    assert "code_hash does not match supplied handler code" in body["error"]
    assert not (tmp_path / "envs").exists()
    assert not (tmp_path / "handlers").exists()


def test_sidecar_rewrites_corrupt_cached_handler(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    client = TestClient(runtime.app)
    code = "def run(inputs):\n    return {'value': inputs['value'] * 2}\n"
    payload = _payload(handler_name="double_rule", code=code, inputs={"value": 7})

    first = client.post("/run", json=payload)
    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert first.json()["output"] == {"value": 14}

    handler_path = (
        tmp_path / "handlers" / payload["handler"]["code_hash"] / "handler.py"
    )
    handler_path.write_text(
        "def run(inputs):\n    return {'value': -1}\n",
        encoding="utf-8",
    )

    second = client.post("/run", json=payload)
    assert second.status_code == 200
    assert second.json()["ok"] is True
    assert second.json()["output"] == {"value": 14}
    assert handler_path.read_text(encoding="utf-8") == code


def test_env_marker_requires_matching_python_and_requirements(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    env = runtime.EnvSpec(
        name="ops_rules",
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        requirements=[],
        env_hash="0" * 32,
    )
    marker = tmp_path / ".rvbbit-ready"
    marker.write_text(
        json.dumps(
            {
                "python_version": env.python_version,
                "requirements_hash": runtime._requirements_hash([]),
            }
        ),
        encoding="utf-8",
    )

    assert runtime._env_marker_matches(marker, env, []) is True

    marker.write_text(
        json.dumps(
            {
                "python_version": env.python_version,
                "requirements_hash": runtime._requirements_hash(["requests==2.32.0"]),
            }
        ),
        encoding="utf-8",
    )

    assert runtime._env_marker_matches(marker, env, []) is False
