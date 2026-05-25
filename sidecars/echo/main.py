"""Echo sidecar — the minimal rvbbit-transport reference.

No model, no ML. Returns transformations the deterministic test suite
can assert against (uppercase, reverse, length, identity). Tracks how
many calls + max batch size it has seen so tests can verify rvbbit is
actually batching across rows.

Two functions selected by the `fn` field on each input:
  {"fn": "upper", "text": "abc"}   → "ABC"
  {"fn": "reverse", "text": "abc"} → "cba"
  {"fn": "length", "text": "abc"}  → 3
  default                          → text verbatim

Endpoints:
  POST /predict       {"inputs": [...]} → {"outputs": [...]}
  GET  /health        → {"ok": true}
  GET  /debug/stats   → {"calls": N, "max_batch": N, "total_inputs": N}
  POST /debug/reset   → clears stats
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Any
import os

app = FastAPI()


class PredictRequest(BaseModel):
    inputs: list[dict[str, Any]]


class PredictResponse(BaseModel):
    outputs: list[Any]


# Mutable state — single-process FastAPI, no concurrency primitives needed.
_stats = {"calls": 0, "max_batch": 0, "total_inputs": 0}

# Optional shared-secret check, on if ECHO_TOKEN env is set.
EXPECTED_TOKEN = os.environ.get("ECHO_TOKEN", "")


def _check_auth(authorization: str | None) -> None:
    if not EXPECTED_TOKEN:
        return
    expected = f"Bearer {EXPECTED_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="bad token")


def _transform(item: dict[str, Any]) -> Any:
    fn = (item.get("fn") or "identity").lower()
    text = item.get("text", "")
    if fn == "upper":
        return str(text).upper()
    if fn == "reverse":
        return str(text)[::-1]
    if fn == "length":
        return len(str(text))
    return text


@app.post("/predict", response_model=PredictResponse)
def predict(
    req: PredictRequest,
    authorization: str | None = Header(default=None),
) -> PredictResponse:
    _check_auth(authorization)
    n = len(req.inputs)
    _stats["calls"] += 1
    _stats["total_inputs"] += n
    if n > _stats["max_batch"]:
        _stats["max_batch"] = n
    return PredictResponse(outputs=[_transform(x) for x in req.inputs])


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/debug/stats")
def stats() -> dict[str, int]:
    return dict(_stats)


@app.post("/debug/reset")
def reset() -> dict[str, str]:
    _stats["calls"] = 0
    _stats["max_batch"] = 0
    _stats["total_inputs"] = 0
    return {"ok": "reset"}
