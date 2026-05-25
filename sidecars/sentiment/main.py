"""Sentiment sidecar — DistilBERT SST-2.

Reference rvbbit-transport specialist for binary sentiment. Loaded lazily
on first request so container startup stays fast; once loaded the model
stays resident for the container's lifetime.

rvbbit wire shape:
  POST /predict
  {"inputs": [{"text": "..."}, {"text": "..."}, ...]}
  → {"outputs": [{"label": "POSITIVE", "score": 0.998}, ...]}

Rvbbit-side wiring (one-time):
  SELECT rvbbit.register_backend(
      backend_name => 'sentiment',
      backend_endpoint => 'http://sentiment:8080/predict',
      backend_batch_size => 32);

  SELECT rvbbit.create_operator(
      op_name => 'sentiment',
      op_shape => 'scalar',
      op_arg_names => ARRAY['text'], op_return_type => 'jsonb',
      op_system => 'unused', op_user => 'unused',
      op_steps => '[{"name":"s","kind":"specialist","specialist":"sentiment",
                     "inputs":{"text":"{{ inputs.text }}"}}]'::jsonb);

  SELECT rvbbit.sentiment(review) FROM reviews;
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
import os

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

MODEL_NAME = os.environ.get(
    "SENTIMENT_MODEL", "distilbert-base-uncased-finetuned-sst-2-english"
)
EXPECTED_TOKEN = os.environ.get("SENTIMENT_TOKEN", "")

_pipeline = None


def _load_model():
    """Import + load on demand; keeps cold-import out of the import path
    so /health responds before the model is ready."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    from transformers import pipeline  # heavy import, do it lazily

    _pipeline = pipeline(
        task="sentiment-analysis",
        model=MODEL_NAME,
        device=-1,  # CPU; override by mounting GPU + setting CUDA_VISIBLE_DEVICES
    )
    return _pipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eager-load if SENTIMENT_EAGER=1 so the first user request doesn't
    # pay the model-load cost. Default is lazy (faster container boot).
    if os.environ.get("SENTIMENT_EAGER", "0") == "1":
        _load_model()
    yield


app = FastAPI(lifespan=lifespan)


class PredictRequest(BaseModel):
    inputs: list[dict[str, Any]]


class PredictResponse(BaseModel):
    outputs: list[dict[str, Any]]


def _check_auth(authorization: str | None) -> None:
    if not EXPECTED_TOKEN:
        return
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


@app.post("/predict", response_model=PredictResponse)
def predict(
    req: PredictRequest,
    authorization: str | None = Header(default=None),
) -> PredictResponse:
    _check_auth(authorization)
    if not req.inputs:
        return PredictResponse(outputs=[])

    pipe = _load_model()
    texts = [str(x.get("text") or "") for x in req.inputs]
    # transformers' pipeline accepts a list and batches internally.
    raw = pipe(texts, truncation=True)
    outputs = [
        {"label": r["label"], "score": float(r["score"])} for r in raw
    ]
    return PredictResponse(outputs=outputs)


@app.get("/health")
def health() -> dict[str, Any]:
    # Doesn't trigger model load — lets orchestrators verify liveness fast.
    return {"ok": True, "model": MODEL_NAME, "loaded": _pipeline is not None}


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    """Optional: force model load so the next /predict is hot."""
    _load_model()
    return {"ok": True, "loaded": True}
