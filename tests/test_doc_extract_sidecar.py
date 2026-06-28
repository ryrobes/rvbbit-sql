"""Sidecar-level tests for document extraction staging safety."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _load_doc_extract(monkeypatch, tmp_path):
    module_path = (
        Path(__file__).resolve().parents[1] / "sidecars" / "doc-extract" / "main.py"
    )
    monkeypatch.setenv("EXTRACT_STAGING_DIR", str(tmp_path / "staging"))

    module_name = f"rvbbit_doc_extract_test_{os.getpid()}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_doc_extract_reads_only_staged_files(monkeypatch, tmp_path):
    sidecar = _load_doc_extract(monkeypatch, tmp_path)
    staged = tmp_path / "staging" / "doc.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("staged text\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside text\n", encoding="utf-8")

    assert sidecar._extract_one({"staged_path": str(staged), "mime": "text/plain"}) == (
        "staged text"
    )
    assert sidecar._extract_one({"staged_path": str(outside), "mime": "text/plain"}) == ""


def test_doc_extract_rejects_symlink_escape(monkeypatch, tmp_path):
    sidecar = _load_doc_extract(monkeypatch, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside text\n", encoding="utf-8")
    link = tmp_path / "staging" / "link.txt"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    assert sidecar._extract_one({"staged_path": str(link), "mime": "text/plain"}) == ""


def test_doc_extract_invalid_max_bytes_env_uses_default(monkeypatch, tmp_path):
    monkeypatch.setenv("EXTRACT_MAX_BYTES", "not-an-int")
    sidecar = _load_doc_extract(monkeypatch, tmp_path)

    assert sidecar.MAX_BYTES == 64 * 1024 * 1024
