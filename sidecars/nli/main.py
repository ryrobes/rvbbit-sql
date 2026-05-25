"""NLI sidecar — one GPU container, three rvbbit specialists.

Backbone: MoritzLaurer/deberta-v3-large-zeroshot-v2.0 (~1.7GB FP16).
Replaces five separate LLM round-trips with one local forward pass.

Endpoints (all native rvbbit transport, all batched):

  POST /classify
    {"inputs": [{"text": "...", "candidate_labels": "a,b,c"}, ...]}
    → {"outputs": ["b", "a", ...]}                            # argmax label
    Powers rvbbit.classify(text, categories) and rvbbit.sentiment(text)
    (with candidate_labels="positive,negative,neutral,mixed").

  POST /entails
    {"inputs": [{"premise": "...", "hypothesis": "..."}, ...]}
    → {"outputs": ["YES", "NO", ...]}                         # threshold on P(entail)
    Powers rvbbit.supports(a, b) and rvbbit.implies(a, b).

  POST /contradicts
    {"inputs": [{"premise": "...", "hypothesis": "..."}, ...]}
    → {"outputs": ["YES", "NO", ...]}                         # threshold on P(contradict)
    Powers rvbbit.contradicts(a, b).

Why YES/NO strings: rvbbit's existing `yes_no` parser turns them into
PG booleans without any custom parser code. Returning a label string
for /classify lets the `strip` parser pass it through unchanged.

Rvbbit wiring is done by docker/sql/register-gpu-specialists.sql —
THREE specialists pointing at the same container's three paths:
  nli_classify     → http://nli:8080/classify
  nli_entails      → http://nli:8080/entails
  nli_contradicts  → http://nli:8080/contradicts
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
import os
import threading
import time

# Reduce CUDA allocator fragmentation — the GPU is shared with the embed /
# rerank / extract sidecars, so headroom is tight. Must be set before torch
# is imported (torch is imported lazily inside the model functions).
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

MODEL_NAME = os.environ.get(
    "NLI_MODEL",
    # 3-class NLI (entailment/neutral/contradiction) — works for both
    # zero-shot classification AND raw entailment/contradiction queries.
    # The zeroshot-v2.0 variant is binary (no contradiction class) so
    # we use the MNLI/FEVER/ANLI ensemble instead.
    "MoritzLaurer/DeBERTa-v3-large-mnli-fever-anli-ling-wanli",
)
EXPECTED_TOKEN = os.environ.get("NLI_TOKEN", "")
ENTAIL_THRESHOLD = float(os.environ.get("NLI_ENTAIL_THRESHOLD", "0.4"))
CONTRADICT_THRESHOLD = float(os.environ.get("NLI_CONTRADICT_THRESHOLD", "0.4"))
FORCE_DEVICE = os.environ.get("NLI_DEVICE", "").lower()

# Batching knobs. /classify and /entails build NLI premise/hypothesis
# pairs and push them through the model in GPU batches of this size.
NLI_BATCH_SIZE = int(os.environ.get("NLI_BATCH_SIZE", "64"))
# /classify truncates the premise harder than raw NLI: a sentiment or
# topic label is decided by the opening of a narrative, not its tail.
CLASSIFY_MAX_LEN = int(os.environ.get("NLI_CLASSIFY_MAX_LEN", "256"))
NLI_MAX_LEN = int(os.environ.get("NLI_MAX_LEN", "512"))
HYPOTHESIS_TEMPLATE = os.environ.get("NLI_HYPOTHESIS_TEMPLATE", "This example is {}.")

_nli_model = None  # bare AutoModelForSequenceClassification
_nli_tokenizer = None
_device = None
_dtype = None
_label_index: dict[str, int] = {}  # {"entailment": 0, ...}
# Serializes GPU forward passes. FastAPI runs sync endpoints on a thread
# pool, so several /classify requests can arrive at once; without this they
# would each allocate a batch on the GPU concurrently and OOM the shared
# card. Tokenization (CPU) still overlaps — only the forward pass is gated.
_gpu_lock = threading.Lock()


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


def _load_models():
    global _nli_model, _nli_tokenizer, _label_index
    if _nli_model is not None:
        return
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    _pick_device()
    t0 = time.time()
    _nli_tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    _nli_model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, torch_dtype=_dtype
    ).to(_device)
    _nli_model.eval()
    for p in _nli_model.parameters():
        p.requires_grad = False
    # Cache the label → id mapping; deberta-v3 NLI exposes
    # {0: entailment, 1: neutral, 2: contradiction} but we read it
    # from the config in case the upstream changes ordering.
    cfg_id2label = getattr(_nli_model.config, "id2label", {})
    _label_index = {str(v).lower(): int(k) for k, v in cfg_id2label.items()}

    print(
        f"[nli] loaded {MODEL_NAME} on {_device}/{_dtype} in {time.time()-t0:.1f}s; "
        f"labels = {_label_index}",
        flush=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("NLI_EAGER", "1") == "1":
        _load_models()
    yield


app = FastAPI(lifespan=lifespan)


class PredictReq(BaseModel):
    inputs: list[dict[str, Any]]


class PredictResp(BaseModel):
    outputs: list[str]


def _check_auth(authorization: str | None) -> None:
    if not EXPECTED_TOKEN:
        return
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


# ---------------------------------------------------------------------------
# /classify — zero-shot label argmax

def _split_labels(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return []


def _classify_batch(items: list[dict[str, Any]]) -> list[str]:
    """Zero-shot label argmax, fully GPU-batched.

    Each (text, candidate_label) becomes one NLI premise/hypothesis pair.
    Every pair across every item is pushed through the model in batches of
    NLI_BATCH_SIZE — one forward pass per batch, not one per pair. Per item
    the label with the highest entailment logit wins (argmax of a softmax
    over the labels is just the argmax of the raw entailment logits)."""
    _load_models()
    import torch

    entail_id = _label_index.get("entailment", 0)

    premises: list[str] = []
    hypotheses: list[str] = []
    meta: list[tuple[int, str]] = []  # (item_index, label)
    for i, item in enumerate(items):
        text = str(item.get("text") or "")
        labels = _split_labels(item.get("candidate_labels"))
        if not text or not labels:
            continue
        for lab in labels:
            premises.append(text)
            hypotheses.append(HYPOTHESIS_TEMPLATE.format(lab))
            meta.append((i, lab))

    # Entailment logit for every pair, computed in GPU batches.
    entail_logits: list[float] = []
    for start in range(0, len(premises), NLI_BATCH_SIZE):
        enc = _nli_tokenizer(
            premises[start:start + NLI_BATCH_SIZE],
            hypotheses[start:start + NLI_BATCH_SIZE],
            padding=True,
            truncation=True,
            max_length=CLASSIFY_MAX_LEN,
            return_tensors="pt",
        )
        with _gpu_lock:
            enc = {k: v.to(_device) for k, v in enc.items()}
            with torch.no_grad():
                logits = _nli_model(**enc).logits
            chunk = logits[:, entail_id].float().cpu().tolist()
            del enc, logits
        entail_logits.extend(chunk)

    # Per item, keep the label with the top entailment logit.
    best: dict[int, tuple[float, str]] = {}
    for (i, lab), logit in zip(meta, entail_logits):
        cur = best.get(i)
        if cur is None or logit > cur[0]:
            best[i] = (logit, lab)

    return [best[i][1] if i in best else "" for i in range(len(items))]


@app.post("/classify", response_model=PredictResp)
def classify(req: PredictReq, authorization: str | None = Header(default=None)) -> PredictResp:
    _check_auth(authorization)
    return PredictResp(outputs=_classify_batch(req.inputs))


# ---------------------------------------------------------------------------
# /entails + /contradicts — raw NLI probs thresholded to YES/NO

def _nli_batch(items: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Returns one {entailment, neutral, contradiction} dict per input."""
    _load_models()
    import torch

    pairs: list[tuple[str, str]] = []
    valid_idx: list[int] = []
    for i, item in enumerate(items):
        premise = str(item.get("premise") or item.get("a") or "")
        hypothesis = str(item.get("hypothesis") or item.get("b") or "")
        if premise and hypothesis:
            pairs.append((premise, hypothesis))
            valid_idx.append(i)

    scores: list[dict[str, float]] = [
        {"entailment": 0.0, "neutral": 0.0, "contradiction": 0.0} for _ in items
    ]
    if not pairs:
        return scores

    enc = _nli_tokenizer(
        [p[0] for p in pairs],
        [p[1] for p in pairs],
        padding=True,
        truncation=True,
        max_length=NLI_MAX_LEN,
        return_tensors="pt",
    )
    with _gpu_lock:
        enc = {k: v.to(_device) for k, v in enc.items()}
        with torch.no_grad():
            logits = _nli_model(**enc).logits
        probs = torch.softmax(logits.float(), dim=-1).cpu().tolist()
        del enc, logits

    for idx, row in zip(valid_idx, probs):
        s = {"entailment": 0.0, "neutral": 0.0, "contradiction": 0.0}
        for label, model_id in _label_index.items():
            if label in s:
                s[label] = float(row[model_id])
        scores[idx] = s
    return scores


@app.post("/entails", response_model=PredictResp)
def entails(req: PredictReq, authorization: str | None = Header(default=None)) -> PredictResp:
    _check_auth(authorization)
    probs = _nli_batch(req.inputs)
    return PredictResp(
        outputs=["YES" if p["entailment"] >= ENTAIL_THRESHOLD else "NO" for p in probs]
    )


@app.post("/contradicts", response_model=PredictResp)
def contradicts(req: PredictReq, authorization: str | None = Header(default=None)) -> PredictResp:
    _check_auth(authorization)
    probs = _nli_batch(req.inputs)
    return PredictResp(
        outputs=[
            "YES" if p["contradiction"] >= CONTRADICT_THRESHOLD else "NO" for p in probs
        ]
    )


# ---------------------------------------------------------------------------
# Health + warmup

@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": MODEL_NAME,
        "device": _device,
        "loaded": _nli_model is not None,
        "labels": _label_index,
    }


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    _load_models()
    _ = _classify_batch([{"text": "warmup probe", "candidate_labels": "a,b"}])
    _ = _nli_batch([{"premise": "warmup", "hypothesis": "test"}])
    return {"ok": True, "device": _device, "loaded": True}
