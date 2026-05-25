"""Reranker sidecar — Gradio + BAAI/bge-reranker-v2-m3.

GPU-friendly cross-encoder relevance scoring. Used by rvbbit.score (and
the criterion-mode of rvbbit.outliers) to get fast, calibrated 0-1
relevance scores without a full LLM call.

This sidecar uses the GRADIO transport intentionally — paired with the
native-transport embed sidecar to demonstrate that rvbbit speaks both
wire formats. ParadeDB-style reranker quality, one Docker image.

Gradio wire shape (handled by rvbbit's gradio transport):
  POST /api/predict
  {"data": [{"text": "...", "criterion": "..."}]}
  → {"data": [0.83]}

Rvbbit-side registration is done by docker/init-specialists.sql:
  SELECT rvbbit.register_backend(
      backend_name => 'rerank',
      backend_endpoint => 'http://rerank:7860/api/predict',
      backend_transport => 'gradio',
      backend_batch_size => 1);  -- gradio is server-batched

Per-input form: inputs are objects of {text, criterion}. The Gradio
transport unpacks each row individually so we accept one pair per call;
the heavy lifting is in batching N concurrent requests server-side
via Gradio's queue.
"""
from __future__ import annotations

import os
import time

import gradio as gr
from fastapi import FastAPI

MODEL_NAME = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
FORCE_DEVICE = os.environ.get("RERANK_DEVICE", "").lower()

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
        _dtype = torch.float16
    else:
        _device = "cpu"
        _dtype = torch.float32


def _load_model():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    _pick_device()
    t0 = time.time()
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    _model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, torch_dtype=_dtype
    ).to(_device)
    _model.eval()
    for p in _model.parameters():
        p.requires_grad = False
    print(
        f"[rerank] loaded {MODEL_NAME} on {_device}/{_dtype} "
        f"in {time.time()-t0:.1f}s",
        flush=True,
    )
    return _model, _tokenizer


def _score_one(text: str, criterion: str) -> float:
    import torch

    model, tokenizer = _load_model()
    enc = tokenizer(
        [criterion],
        [text],
        padding=True,
        truncation=True,
        max_length=512,
        return_tensors="pt",
    )
    enc = {k: v.to(_device) for k, v in enc.items()}
    with torch.no_grad():
        logits = model(**enc, return_dict=True).logits
    # bge-reranker emits a single relevance logit; sigmoid → [0,1].
    score = torch.sigmoid(logits.view(-1).float()).item()
    return float(score)


def score_row(payload: dict) -> float:
    """Gradio entry point. `payload` is the single input dict; rvbbit's
    gradio transport sends one per call (server batches via queue)."""
    if not isinstance(payload, dict):
        return 0.0
    text = str(payload.get("text") or "")
    criterion = str(payload.get("criterion") or "")
    if not text or not criterion:
        return 0.0
    return _score_one(text, criterion)


demo = gr.Interface(
    fn=score_row,
    inputs=gr.JSON(label="payload (object with text + criterion)"),
    outputs=gr.Number(label="relevance"),
    title="rvbbit rerank (gradio)",
    description=f"Cross-encoder relevance scoring via {MODEL_NAME}.",
).queue(api_open=True, default_concurrency_limit=8)

app = FastAPI()


@app.get("/health")
def health():
    return {
        "ok": True,
        "model": MODEL_NAME,
        "device": _device,
        "loaded": _model is not None,
    }


@app.post("/warmup")
def warmup():
    _load_model()
    # One real scoring call so any CUDA kernels are compiled.
    _ = _score_one("warmup probe", "test relevance")
    return {"ok": True, "device": _device, "loaded": True}


app = gr.mount_gradio_app(app, demo, path="/")


if __name__ == "__main__":
    import uvicorn
    if os.environ.get("RERANK_EAGER", "1") == "1":
        _load_model()
    uvicorn.run(app, host="0.0.0.0", port=7860)
