"""Echo sidecar — OpenAI-embeddings shape.

Mocks the OpenAI /v1/embeddings response shape with deterministic output:
each text is hashed to a small fixed-dim vector. Used by rvbbit's openai
transport tests so we can verify wire format + batching + ordering
without depending on a real embedding model.

Real Ollama at http://ollama:11434/v1/embeddings speaks the same shape;
swap the URL in the catalog row and you're talking to real embeddings.

Tracks call count + max batch so tests can verify rvbbit is batching
multiple rows into one POST.

Endpoints:
  POST /v1/embeddings    OpenAI shape, returns deterministic vectors
  GET  /health           liveness
  GET  /debug/stats      {calls, max_batch, total_inputs}
  POST /debug/reset      clears stats
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI()

DIM = 8  # deterministic small dim — tests don't need real embeddings

_stats = {"calls": 0, "max_batch": 0, "total_inputs": 0}
EXPECTED_TOKEN = os.environ.get("ECHO_TOKEN", "")


def _check_auth(authorization: str | None) -> None:
    if not EXPECTED_TOKEN:
        return
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


def _embed(text: str) -> list[float]:
    """Hash to a fixed-dim float vector. Deterministic per input."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # 8 floats in [-1, 1], from successive byte pairs.
    return [
        ((h[i] << 8 | h[i + 1]) / 65535.0) * 2.0 - 1.0
        for i in range(0, DIM * 2, 2)
    ]


class EmbedRequest(BaseModel):
    # OpenAI accepts a string OR list of strings — handle both for parity.
    input: list[str] | str
    model: str


@app.post("/v1/embeddings")
def embeddings(
    req: EmbedRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(authorization)
    inputs = [req.input] if isinstance(req.input, str) else req.input
    n = len(inputs)
    _stats["calls"] += 1
    _stats["total_inputs"] += n
    if n > _stats["max_batch"]:
        _stats["max_batch"] = n
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": _embed(text)}
            for i, text in enumerate(inputs)
        ],
        "model": req.model,
        "usage": {"prompt_tokens": sum(len(t) for t in inputs), "total_tokens": sum(len(t) for t in inputs)},
    }


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
