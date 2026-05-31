"""Fast sidecar contract tests for built-in Warren examples.

These avoid loading real ML models. The live capability acceptance SQL covers
model behavior; these tests keep the reference HTTP surfaces from drifting.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def _load_app(monkeypatch, path: Path, env: dict[str, str] | None = None):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    label = "".join(ch if ch.isalnum() else "_" for ch in path.parent.name)
    module_name = f"rvbbit_sidecar_test_{label}_{os.getpid()}_{len(sys.modules)}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_hf_template_echo_handler_contract(monkeypatch):
    module = _load_app(
        monkeypatch,
        ROOT / "capabilities" / "templates" / "hf-rvbbit-fastapi" / "main.py",
        {
            "RVBBIT_CAPABILITY_HANDLER": "echo",
            "RVBBIT_CAPABILITY_MODEL": "rvbbit/warren-smoke-echo",
            "RVBBIT_CAPABILITY_EAGER": "0",
        },
    )
    client = TestClient(module.app)

    health = client.get("/health").json()
    assert health["ok"] is True
    assert health["handler"] == "echo"

    res = client.post(
        "/predict",
        json={
            "inputs": [
                {"text": "refund needed", "labels": "billing, support"},
                {"text": "cannot log in", "categories": ["auth", "support"]},
            ]
        },
    )

    assert res.status_code == 200
    outputs = res.json()["outputs"]
    assert outputs[0]["echo"] == "refund needed"
    assert outputs[0]["labels"] == ["billing", "support"]
    assert outputs[1]["labels"] == ["auth", "support"]


def test_hf_template_auth_guard(monkeypatch):
    module = _load_app(
        monkeypatch,
        ROOT / "capabilities" / "templates" / "hf-rvbbit-fastapi" / "main.py",
        {
            "RVBBIT_CAPABILITY_HANDLER": "echo",
            "RVBBIT_CAPABILITY_TOKEN": "secret-token",
            "RVBBIT_CAPABILITY_EAGER": "0",
        },
    )
    client = TestClient(module.app)

    denied = client.post("/predict", json={"inputs": [{"text": "x"}]})
    allowed = client.post(
        "/predict",
        json={"inputs": [{"text": "x"}]},
        headers={"authorization": "Bearer secret-token"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["outputs"][0]["echo"] == "x"


def test_reference_echo_sidecar_batching_and_stats(monkeypatch):
    module = _load_app(monkeypatch, ROOT / "sidecars" / "echo" / "main.py")
    client = TestClient(module.app)

    assert client.get("/health").json() == {"ok": True}
    client.post("/debug/reset")
    res = client.post(
        "/predict",
        json={
            "inputs": [
                {"fn": "upper", "text": "alpha"},
                {"fn": "reverse", "text": "bravo"},
                {"fn": "length", "text": "charlie"},
            ]
        },
    )

    assert res.status_code == 200
    assert res.json()["outputs"] == ["ALPHA", "ovarb", 7]
    assert client.get("/debug/stats").json() == {
        "calls": 1,
        "max_batch": 3,
        "total_inputs": 3,
    }


def test_reference_openai_embedding_sidecar_shape(monkeypatch):
    module = _load_app(monkeypatch, ROOT / "sidecars" / "echo-openai-embed" / "main.py")
    client = TestClient(module.app)

    client.post("/debug/reset")
    res = client.post(
        "/v1/embeddings",
        json={"model": "rvbbit-test-embed", "input": ["refund", "login"]},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["object"] == "list"
    assert body["model"] == "rvbbit-test-embed"
    assert [row["index"] for row in body["data"]] == [0, 1]
    assert all(len(row["embedding"]) == module.DIM for row in body["data"])
    assert body["data"][0]["embedding"] != body["data"][1]["embedding"]
    assert client.get("/debug/stats").json() == {
        "calls": 1,
        "max_batch": 2,
        "total_inputs": 2,
    }
