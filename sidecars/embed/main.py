"""Embeddings sidecar — GPU-friendly with BGE-M3 by default.

Reference rvbbit-transport specialist for sentence embeddings. Defaults
to BAAI/bge-m3 (1024-dim multilingual, ~2GB FP16) which is the practical
sweet spot for retrieval quality on a single GPU. CPU users can override
with EMBED_MODEL=BAAI/bge-small-en-v1.5 (130MB, 384-dim).

Returns vectors as plain float arrays in the rvbbit wire shape — NOT
the OpenAI shape. If you want the OpenAI /v1/embeddings shape (so you
can drop in vLLM/Ollama later via rvbbit's openai transport), use the
echo-openai-embed sidecar as your starting point instead.

rvbbit wire shape:
  POST /predict
  {"inputs": [{"text": "..."}, ...]}
  → {"outputs": [[0.123, -0.456, ...], ...]}

Rvbbit-side wiring (registered automatically by docker/init-specialists.sql):
  SELECT rvbbit.register_backend(
      backend_name => 'embed',
      backend_endpoint => 'http://embed:8080/predict',
      backend_transport => 'rvbbit',
      backend_batch_size => 64);

GPU is detected at startup. Set EMBED_DEVICE=cpu to force CPU even
on a GPU host.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
import os
import time

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

MODEL_NAME = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
EXPECTED_TOKEN = os.environ.get("EMBED_TOKEN", "")
FORCE_DEVICE = os.environ.get("EMBED_DEVICE", "").lower()

_model = None
_tokenizer = None
_device = None
_dtype = None


def _pick_device():
    global _device, _dtype
    import torch
    if FORCE_DEVICE == "cpu":
        _device = "cpu"
        _dtype = torch.float32
    elif torch.cuda.is_available():
        _device = "cuda"
        # BGE-M3 in FP16 fits ~2GB and is ~2x faster than FP32 with no
        # measurable quality loss for retrieval.
        _dtype = torch.float16
    else:
        _device = "cpu"
        _dtype = torch.float32


def _load_model():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    import torch
    from transformers import AutoModel, AutoTokenizer

    _pick_device()
    t0 = time.time()
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    _model = AutoModel.from_pretrained(MODEL_NAME, torch_dtype=_dtype).to(_device)
    _model.eval()
    for p in _model.parameters():
        p.requires_grad = False
    print(f"[embed] loaded {MODEL_NAME} on {_device}/{_dtype} in {time.time()-t0:.1f}s", flush=True)
    return _model, _tokenizer


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("EMBED_EAGER", "1") == "1":
        _load_model()
    yield


app = FastAPI(lifespan=lifespan)


class PredictRequest(BaseModel):
    inputs: list[dict[str, Any]]


class PredictResponse(BaseModel):
    outputs: list[list[float]]


def _check_auth(authorization: str | None) -> None:
    if not EXPECTED_TOKEN:
        return
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


def _embed_batch(texts: list[str]) -> list[list[float]]:
    import torch

    model, tokenizer = _load_model()
    enc = tokenizer(
        texts, padding=True, truncation=True, max_length=512, return_tensors="pt"
    )
    enc = {k: v.to(_device) for k, v in enc.items()}
    with torch.no_grad():
        out = model(**enc)
    # BGE-M3 (and most encoders) use CLS pooling + L2-normalize so dot
    # product equals cosine similarity downstream.
    cls = out.last_hidden_state[:, 0]
    cls = torch.nn.functional.normalize(cls, p=2, dim=1)
    # Move back to CPU + float32 for JSON serialization.
    return cls.float().cpu().tolist()


@app.post("/predict", response_model=PredictResponse)
def predict(
    req: PredictRequest,
    authorization: str | None = Header(default=None),
) -> PredictResponse:
    _check_auth(authorization)
    if not req.inputs:
        return PredictResponse(outputs=[])
    texts = [str(x.get("text") or "") for x in req.inputs]
    return PredictResponse(outputs=_embed_batch(texts))


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
    return {"ok": True, "loaded": True, "device": _device}
