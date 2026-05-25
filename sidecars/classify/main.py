"""Zero-shot classify sidecar — cross-encoder/nli-deberta-v3-xsmall.

Reference rvbbit-transport specialist for arbitrary-label text
classification using natural-language inference. Each input carries
its OWN candidate labels — no fixed taxonomy. Lazy-loaded; ~280MB.

rvbbit wire shape:
  POST /predict
  {"inputs": [{"text":"...", "candidate_labels":"a,b,c"}, ...]}
  → {"outputs": [
       {"label": "b", "score": 0.83, "all": {"a": 0.05, "b": 0.83, "c": 0.12}},
       ...
     ]}

`candidate_labels` is a comma-separated string (or a JSON array — both work).

Rvbbit-side wiring:
  SELECT rvbbit.register_backend(
      backend_name => 'classify',
      backend_endpoint => 'http://classify:8080/predict',
      backend_batch_size => 16);

  SELECT rvbbit.create_operator(
      op_name => 'classify',
      op_shape => 'scalar',
      op_arg_names => ARRAY['text','candidate_labels'],
      op_return_type => 'jsonb',
      op_system => 'unused', op_user => 'unused',
      op_steps => '[{"name":"c","kind":"specialist","specialist":"classify",
                     "inputs":{"text":"{{ inputs.text }}",
                               "candidate_labels":"{{ inputs.candidate_labels }}"}}]'::jsonb);

  SELECT rvbbit.classify(article_body, 'sports,politics,tech') FROM articles;
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
import os
import json

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

MODEL_NAME = os.environ.get("CLASSIFY_MODEL", "cross-encoder/nli-deberta-v3-xsmall")
EXPECTED_TOKEN = os.environ.get("CLASSIFY_TOKEN", "")

_pipeline = None


def _load_model():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    from transformers import pipeline

    _pipeline = pipeline(
        task="zero-shot-classification",
        model=MODEL_NAME,
        device=-1,
    )
    return _pipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("CLASSIFY_EAGER", "0") == "1":
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


def _parse_labels(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    s = str(raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [str(x) for x in arr]
        except Exception:
            pass
    return [p.strip() for p in s.split(",") if p.strip()]


@app.post("/predict", response_model=PredictResponse)
def predict(
    req: PredictRequest,
    authorization: str | None = Header(default=None),
) -> PredictResponse:
    _check_auth(authorization)
    if not req.inputs:
        return PredictResponse(outputs=[])

    pipe = _load_model()
    # transformers' zero-shot pipeline can take a list of texts AND a per-batch
    # candidate_labels list, BUT only if labels are the same for every input.
    # We need per-row labels, so we call one-at-a-time. (Still in one Python
    # process; rvbbit's pool concurrency happens outside.)
    outputs: list[dict[str, Any]] = []
    for item in req.inputs:
        text = str(item.get("text") or "")
        labels = _parse_labels(item.get("candidate_labels"))
        if not text or not labels:
            outputs.append({"label": None, "score": 0.0, "all": {}})
            continue
        result = pipe(text, candidate_labels=labels, multi_label=False)
        # result = {"labels": [...sorted...], "scores": [...sorted...], "sequence": ...}
        all_scores = {lbl: float(sc) for lbl, sc in zip(result["labels"], result["scores"])}
        outputs.append({
            "label": result["labels"][0],
            "score": float(result["scores"][0]),
            "all": all_scores,
        })
    return PredictResponse(outputs=outputs)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "model": MODEL_NAME, "loaded": _pipeline is not None}


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    _load_model()
    return {"ok": True, "loaded": True}
