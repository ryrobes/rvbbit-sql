"""GLiNER extract sidecar — zero-shot NER over arbitrary labels.

Powers `rvbbit.extract(text, what)` without an LLM. GLiNER takes
descriptive label phrases (not just canonical NER types) so you can ask
for "customer name", "shipping date", "order number" — anything that
sounds like a thing.

Uses the native rvbbit wire shape (batched). Pair with the Gradio-based
rerank sidecar to show both transports working side-by-side.

rvbbit wire shape:
  POST /predict
  {"inputs": [{"text": "...", "what": "..."}, ...]}
  → {"outputs": ["extracted span" | "NULL", ...]}

Returns the literal string "NULL" when no span clears the threshold
(default 0.3), matching the rvbbit.extract operator's convention. The
LLM operator returns "NULL" too, so a downstream NULLIF or CASE works
either way.

Rvbbit-side wiring is done by docker/init-specialists.sql.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
import os
import time

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

MODEL_NAME = os.environ.get("EXTRACT_MODEL", "urchade/gliner_medium-v2.1")
EXPECTED_TOKEN = os.environ.get("EXTRACT_TOKEN", "")
DEFAULT_THRESHOLD = float(os.environ.get("EXTRACT_THRESHOLD", "0.3"))
FORCE_DEVICE = os.environ.get("EXTRACT_DEVICE", "").lower()

_model = None
_device = None


def _pick_device() -> str:
    import torch
    if FORCE_DEVICE == "cpu":
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _load_model():
    global _model, _device
    if _model is not None:
        return _model
    from gliner import GLiNER

    _device = _pick_device()
    t0 = time.time()
    _model = GLiNER.from_pretrained(MODEL_NAME).to(_device)
    _model.eval()
    print(
        f"[extract] loaded {MODEL_NAME} on {_device} in {time.time()-t0:.1f}s",
        flush=True,
    )
    return _model


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("EXTRACT_EAGER", "1") == "1":
        _load_model()
    yield


app = FastAPI(lifespan=lifespan)


class PredictRequest(BaseModel):
    inputs: list[dict[str, Any]]


class PredictResponse(BaseModel):
    outputs: list[str]


def _check_auth(authorization: str | None) -> None:
    if not EXPECTED_TOKEN:
        return
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


def _extract_one(text: str, what: str, threshold: float) -> str:
    model = _load_model()
    if not text or not what:
        return "NULL"
    # GLiNER takes a list of label phrases; we pass just the one the user
    # asked for. Returned entities have {start, end, label, score, text}.
    entities = model.predict_entities(text, [what], threshold=threshold)
    if not entities:
        return "NULL"
    # Highest-score span wins.
    entities.sort(key=lambda e: -float(e.get("score") or 0.0))
    return str(entities[0].get("text") or "NULL")


@app.post("/predict", response_model=PredictResponse)
def predict(
    req: PredictRequest,
    authorization: str | None = Header(default=None),
) -> PredictResponse:
    _check_auth(authorization)
    outs: list[str] = []
    for item in req.inputs:
        text = str(item.get("text") or "")
        what = str(item.get("what") or "")
        threshold = float(item.get("threshold") or DEFAULT_THRESHOLD)
        outs.append(_extract_one(text, what, threshold))
    return PredictResponse(outputs=outs)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": MODEL_NAME,
        "device": _device,
        "loaded": _model is not None,
    }


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    _load_model()
    return {"ok": True, "device": _device, "loaded": True}
