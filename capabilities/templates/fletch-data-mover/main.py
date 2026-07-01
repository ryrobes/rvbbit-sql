from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="RVBBIT Fletch Data Mover")

FLETCH_BIN = os.environ.get("RVBBIT_FLETCH_BIN", "fletch")
DATA_DIR = Path(os.environ.get("RVBBIT_FLETCH_DATA_DIR", "/var/lib/rvbbit/fletch"))
REGISTRY_PATH = Path(os.environ.get("RVBBIT_FLETCH_DRIVER_REGISTRY", "/app/driver_registry.json"))
ALLOW_DRIVER_INSTALL = os.environ.get("RVBBIT_FLETCH_ALLOW_DRIVER_INSTALL", "true").lower() in {"1", "true", "yes", "on"}
ALLOW_CUSTOM_DRIVERS = os.environ.get("RVBBIT_FLETCH_ALLOW_CUSTOM_DRIVERS", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_TIMEOUT_MS = int(os.environ.get("RVBBIT_FLETCH_TIMEOUT_MS", "1800000"))
APT_LOCK = threading.Lock()

SECRET_KEYS = {"password", "passwd", "pwd", "token", "access_token", "secret", "api_key", "key"}


class Endpoint(BaseModel):
    driver: str
    uri: str


class ProbeRequest(BaseModel):
    driver: str
    uri: str
    auto_install_drivers: bool = True
    timeout_ms: int = 120000


class DriverInstallRequest(BaseModel):
    driver: str
    packages: list[str] | None = None


class TransferRequest(BaseModel):
    source: Endpoint
    destination: Endpoint
    dest_table: str = ""
    query: str
    ingest_mode: str = "create"
    transfer_mode: str = "batch"
    dry_run: bool = False
    auto_install_drivers: bool = True
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class CommandResult(BaseModel):
    ok: bool
    command: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    output: Any = None
    duration_ms: int


def load_registry() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def normalize_driver(driver: str) -> str:
    key = driver.strip().lower()
    registry = load_registry()
    seen: set[str] = set()
    while key in registry and registry[key].get("alias_for") and key not in seen:
        seen.add(key)
        key = str(registry[key]["alias_for"]).strip().lower()
    return key


def redact_uri(uri: str) -> str:
    if not uri:
        return uri
    try:
        parts = urlsplit(uri)
    except Exception:
        return re.sub(r"://([^:/?#]+):([^@/?#]+)@", r"://\1:****@", uri)
    netloc = parts.netloc
    if "@" in netloc and ":" in netloc.split("@", 1)[0]:
        creds, host = netloc.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        netloc = f"{user}:****@{host}"
    query = urlencode(
        [
            (k, "****" if any(secret in k.lower() for secret in SECRET_KEYS) else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_args(args: list[str]) -> list[str]:
    out = list(args)
    for i, item in enumerate(out):
        if item in {"--source-uri", "--dest-uri", "--uri"} and i + 1 < len(out):
            out[i + 1] = redact_uri(out[i + 1])
    return out


def parse_json_output(text: str) -> Any:
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def run_fletch(args: list[str], timeout_ms: int) -> CommandResult:
    command = [FLETCH_BIN, *args]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=max(1, timeout_ms) / 1000.0,
            check=False,
            env=os.environ.copy(),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        output = parse_json_output(proc.stdout)
        return CommandResult(
            ok=proc.returncode == 0,
            command=redact_args(command),
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            output=output,
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            ok=False,
            command=redact_args(command),
            exit_code=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or f"fletch timed out after {timeout_ms}ms",
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def apt_install(packages: list[str]) -> dict[str, Any]:
    if not ALLOW_DRIVER_INSTALL:
        raise HTTPException(status_code=403, detail="driver installation is disabled")
    if not packages:
        raise HTTPException(status_code=400, detail="no installable packages are registered for this driver")
    with APT_LOCK:
        started = time.monotonic()
        cmds = [
            ["apt-get", "update"],
            ["apt-get", "install", "-y", "--no-install-recommends", *packages],
        ]
        logs: list[dict[str, Any]] = []
        for cmd in cmds:
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
            logs.append(
                {
                    "command": cmd,
                    "exit_code": proc.returncode,
                    "stdout_tail": proc.stdout[-4000:],
                    "stderr_tail": proc.stderr[-4000:],
                }
            )
            if proc.returncode != 0:
                return {"ok": False, "logs": logs, "duration_ms": int((time.monotonic() - started) * 1000)}
        return {"ok": True, "logs": logs, "duration_ms": int((time.monotonic() - started) * 1000)}


@app.get("/health")
def health() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    version = run_fletch(["version", "--output", "json"], 10000)
    return {
        "ok": version.ok,
        "fletch": version.output if version.output is not None else version.stdout.strip(),
        "data_dir": str(DATA_DIR),
        "allow_driver_install": ALLOW_DRIVER_INSTALL,
        "allow_custom_drivers": ALLOW_CUSTOM_DRIVERS,
    }


@app.get("/drivers")
def drivers() -> dict[str, Any]:
    registry = load_registry()
    listed = run_fletch(["list-drivers", "--output", "json"], 30000)
    return {
        "ok": listed.ok,
        "registry": registry,
        "fletch": listed.output,
        "stderr": listed.stderr,
    }


@app.post("/drivers/install")
def install_driver(req: DriverInstallRequest) -> dict[str, Any]:
    registry = load_registry()
    driver = normalize_driver(req.driver)
    spec = registry.get(driver)
    if spec is None and not ALLOW_CUSTOM_DRIVERS:
        raise HTTPException(status_code=403, detail=f"driver {req.driver!r} is not in the allowlist")
    packages = req.packages if req.packages is not None else list((spec or {}).get("packages") or [])
    if req.packages is not None and not ALLOW_CUSTOM_DRIVERS:
        raise HTTPException(status_code=403, detail="custom driver package install is disabled")
    result = apt_install(packages)
    return {"driver": driver, "packages": packages, **result}


@app.post("/probe")
def probe(req: ProbeRequest) -> CommandResult:
    args = [
        "test-connection",
        "--driver",
        req.driver,
        "--uri",
        req.uri,
        "--output",
        "json",
    ]
    return run_fletch(args, req.timeout_ms)


@app.post("/transfer")
def transfer(req: TransferRequest) -> CommandResult:
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query is required")
    args = [
        "transfer",
        "--source-driver",
        req.source.driver,
        "--source-uri",
        req.source.uri,
        "--dest-driver",
        req.destination.driver,
        "--dest-uri",
        req.destination.uri,
        "--ingest-mode",
        req.ingest_mode,
        "--transfer-mode",
        req.transfer_mode,
        "--query",
        req.query,
        "--yes",
        "--output",
        "json",
    ]
    if req.dest_table.strip():
        args.extend(["--dest-table", req.dest_table.strip()])
    if req.dry_run:
        args.append("--dry-run")
    if req.auto_install_drivers:
        args.append("--auto-install-drivers")
    else:
        args.append("--no-install-drivers")
    return run_fletch(args, req.timeout_ms)
