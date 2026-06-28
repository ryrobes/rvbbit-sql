"""Managed CPython runtime for rvbbit `kind: python` operator nodes.

The database sends desired state on each request: env name/version/package
list plus handler name/code/entrypoint. This sidecar reconciles env specs
into hashed venv directories, writes handler modules by code hash, and runs
handlers in subprocesses over JSON.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import venv
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()

ENVS_DIR = Path(os.environ.get("RVBBIT_PYTHON_ENVS_DIR", "/tmp/rvbbit-python-envs"))
HANDLERS_DIR = Path(
    os.environ.get("RVBBIT_PYTHON_HANDLERS_DIR", "/tmp/rvbbit-python-handlers")
)
RUNNER = Path(__file__).with_name("runner.py")
SUPPORTED_HASH_LENGTHS = {32, 64}

_lock = threading.Lock()
_stats = {
    "runs": 0,
    "failures": 0,
    "env_builds": 0,
    "handler_writes": 0,
}


class EnvSpec(BaseModel):
    name: str
    python_version: str = "3.12"
    requirements: list[str] = Field(default_factory=list)
    env_hash: str


class HandlerSpec(BaseModel):
    name: str
    code: str
    code_hash: str
    entrypoint: str = "run"


class RunRequest(BaseModel):
    env: EnvSpec
    handler: HandlerSpec
    inputs: dict[str, Any] | list[Any] | str | int | float | bool | None
    timeout_ms: int = 1000


class RunResponse(BaseModel):
    ok: bool
    output: Any = None
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    env_hash: str | None = None
    code_hash: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "python": sys.version.split()[0],
        "envs_dir": str(ENVS_DIR),
    }


@app.get("/debug/stats")
def stats() -> dict[str, int]:
    return dict(_stats)


@app.post("/debug/reset")
def reset() -> dict[str, str]:
    for key in _stats:
        _stats[key] = 0
    return {"ok": "reset"}


@app.post("/run", response_model=RunResponse)
def run(req: RunRequest) -> RunResponse:
    started = time.monotonic()
    _stats["runs"] += 1
    try:
        _validate_hash("env_hash", req.env.env_hash)
        _validate_hash("code_hash", req.handler.code_hash)
        _validate_content_hash("code_hash", req.handler.code_hash, req.handler.code)
        if not _version_matches(req.env.python_version):
            raise RuntimeError(
                f"runtime Python {sys.version.split()[0]} does not satisfy "
                f"requested {req.env.python_version!r}"
            )

        with _lock:
            python_bin = _ensure_env(req.env)
            handler_path = _ensure_handler(req.handler)

        proc = subprocess.run(
            [str(python_bin), str(RUNNER), str(handler_path), req.handler.entrypoint],
            input=json.dumps(req.inputs),
            text=True,
            capture_output=True,
            timeout=max(req.timeout_ms, 1) / 1000.0,
            check=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        payload = _parse_protocol(proc.stdout)
        if proc.returncode != 0 or not payload.get("ok", False):
            _stats["failures"] += 1
            return RunResponse(
                ok=False,
                error=payload.get("error") or proc.stderr or "python handler failed",
                stdout=payload.get("stdout", ""),
                stderr=proc.stderr,
                duration_ms=duration_ms,
                env_hash=req.env.env_hash,
                code_hash=req.handler.code_hash,
            )
        return RunResponse(
            ok=True,
            output=payload.get("output"),
            stdout=payload.get("stdout", ""),
            stderr=proc.stderr,
            duration_ms=duration_ms,
            env_hash=req.env.env_hash,
            code_hash=req.handler.code_hash,
        )
    except subprocess.TimeoutExpired as exc:
        _stats["failures"] += 1
        return RunResponse(
            ok=False,
            error=f"python handler timed out after {req.timeout_ms}ms",
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000),
            env_hash=req.env.env_hash,
            code_hash=req.handler.code_hash,
        )
    except Exception as exc:  # noqa: BLE001 - report runtime setup failures
        _stats["failures"] += 1
        return RunResponse(
            ok=False,
            error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
            env_hash=req.env.env_hash,
            code_hash=req.handler.code_hash,
        )


def _ensure_env(env: EnvSpec) -> Path:
    env_dir = ENVS_DIR / env.env_hash
    marker = env_dir / ".rvbbit-ready"
    python_bin = env_dir / "bin" / "python"
    requirements = _normalize_requirements(env.requirements)
    if (
        marker.exists()
        and python_bin.exists()
        and _env_marker_matches(marker, env, requirements)
    ):
        return python_bin

    tmp_dir = _unique_tmp_path(env_dir)
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(tmp_dir)
        tmp_python = tmp_dir / "bin" / "python"
        if requirements:
            req_file = tmp_dir / "requirements.txt"
            _atomic_write_text(req_file, "\n".join(requirements) + "\n")
            proc = subprocess.run(
                [
                    str(tmp_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    str(req_file),
                ],
                text=True,
                capture_output=True,
                timeout=600,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "pip install failed:\n"
                    + (proc.stdout or "")
                    + ("\n" + proc.stderr if proc.stderr else "")
                )

        _atomic_write_text(
            tmp_dir / ".rvbbit-ready",
            json.dumps(
                {
                    "name": env.name,
                    "python_version": env.python_version,
                    "requirements_hash": _requirements_hash(requirements),
                },
                indent=2,
            ),
        )
        if env_dir.exists():
            shutil.rmtree(env_dir)
        tmp_dir.rename(env_dir)
        _stats["env_builds"] += 1
        return python_bin
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _ensure_handler(handler: HandlerSpec) -> Path:
    _validate_content_hash("code_hash", handler.code_hash, handler.code)
    handler_dir = HANDLERS_DIR / handler.code_hash
    handler_path = handler_dir / "handler.py"
    if handler_path.exists() and handler_path.read_text(encoding="utf-8") == handler.code:
        return handler_path
    handler_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(handler_path, handler.code)
    _stats["handler_writes"] += 1
    return handler_path


def _parse_protocol(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {"ok": False, "error": "handler produced no protocol output"}
    try:
        return json.loads(text.splitlines()[-1])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid handler protocol output: {exc}: {text}"}


def _validate_hash(name: str, value: str) -> None:
    if (
        not value
        or len(value) not in SUPPORTED_HASH_LENGTHS
        or value.lower() != value
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise RuntimeError(f"{name} must be a lowercase hex hash")


def _validate_content_hash(name: str, value: str, content: str) -> None:
    if len(value) == 32:
        expected = hashlib.md5(content.encode("utf-8")).hexdigest()
    elif len(value) == 64:
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    else:
        raise RuntimeError(f"{name} must be a lowercase hex hash")
    if value != expected:
        raise RuntimeError(f"{name} does not match supplied handler code")


def _normalize_requirements(requirements: list[str]) -> list[str]:
    return [req.strip() for req in requirements if req.strip()]


def _requirements_hash(requirements: list[str]) -> str:
    return hashlib.sha256("\n".join(requirements).encode("utf-8")).hexdigest()


def _env_marker_matches(marker: Path, env: EnvSpec, requirements: list[str]) -> bool:
    try:
        doc = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (
        doc.get("python_version") == env.python_version
        and doc.get("requirements_hash") == _requirements_hash(requirements)
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _unique_tmp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")


def _version_matches(requested: str) -> bool:
    wanted = requested.strip()
    if not wanted:
        return True
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    return current == wanted or str(sys.version_info.major) == wanted
