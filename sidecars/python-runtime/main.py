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
    if marker.exists() and python_bin.exists():
        return python_bin

    tmp_dir = env_dir.with_name(f"{env_dir.name}.tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.parent.mkdir(parents=True, exist_ok=True)

    venv.EnvBuilder(with_pip=True, clear=True).create(tmp_dir)
    tmp_python = tmp_dir / "bin" / "python"
    requirements = [req.strip() for req in env.requirements if req.strip()]
    if requirements:
        req_file = tmp_dir / "requirements.txt"
        req_file.write_text("\n".join(requirements) + "\n", encoding="utf-8")
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

    (tmp_dir / ".rvbbit-ready").write_text(
        json.dumps(
            {
                "name": env.name,
                "python_version": env.python_version,
                "requirements_hash": hashlib.sha256(
                    "\n".join(requirements).encode("utf-8")
                ).hexdigest(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if env_dir.exists():
        shutil.rmtree(env_dir)
    tmp_dir.rename(env_dir)
    _stats["env_builds"] += 1
    return python_bin


def _ensure_handler(handler: HandlerSpec) -> Path:
    handler_dir = HANDLERS_DIR / handler.code_hash
    handler_path = handler_dir / "handler.py"
    if handler_path.exists():
        return handler_path
    handler_dir.mkdir(parents=True, exist_ok=True)
    handler_path.write_text(handler.code, encoding="utf-8")
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
    if not value or any(ch not in "0123456789abcdef" for ch in value.lower()):
        raise RuntimeError(f"{name} must be a lowercase hex hash")


def _version_matches(requested: str) -> bool:
    wanted = requested.strip()
    if not wanted:
        return True
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    return current == wanted or str(sys.version_info.major) == wanted
