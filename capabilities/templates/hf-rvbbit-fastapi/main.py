"""Rvbbit Hugging Face capability sidecar.

Wire protocol:
  POST /predict
  {"inputs": [{...}, ...]}
  -> {"outputs": [...same length...]}

This is intentionally small and editable. For unusual research models, keep
the HTTP shape and customize `predict_batch`.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
import json
import os
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

MODEL_NAME = os.environ.get("RVBBIT_CAPABILITY_MODEL", "{{ model }}")
MODEL_REVISION = os.environ.get("RVBBIT_CAPABILITY_REVISION") or None
HANDLER = os.environ.get("RVBBIT_CAPABILITY_HANDLER", "{{ handler }}")
EXPECTED_TOKEN = os.environ.get("RVBBIT_CAPABILITY_TOKEN", "")
FORCE_DEVICE = os.environ.get("RVBBIT_CAPABILITY_DEVICE", "{{ device }}").lower()
SEQUENCE_MODE = os.environ.get("RVBBIT_SEQUENCE_MODE", "auto").lower()
EAGER = os.environ.get("RVBBIT_CAPABILITY_EAGER", "1") == "1"

_loaded: dict[str, Any] = {}
_startup_error: str | None = None


class PredictRequest(BaseModel):
    inputs: list[dict[str, Any]]


class PredictResponse(BaseModel):
    outputs: list[Any]


def _check_auth(authorization: str | None) -> None:
    if not EXPECTED_TOKEN:
        return
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


def _device() -> str:
    if FORCE_DEVICE in {"cpu", "cuda"}:
        return FORCE_DEVICE
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _load_embedding() -> None:
    if _loaded:
        return
    import torch
    from transformers import AutoModel, AutoTokenizer

    device = _device()
    dtype = torch.float16 if device == "cuda" else torch.float32
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, revision=MODEL_REVISION)
    model = AutoModel.from_pretrained(
        MODEL_NAME, revision=MODEL_REVISION, torch_dtype=dtype
    ).to(device)
    model.eval()
    for param in model.parameters():
        param.requires_grad = False
    _loaded.update(
        {
            "tokenizer": tokenizer,
            "model": model,
            "device": device,
            "dtype": str(dtype),
            "loaded_at": time.time(),
            "load_seconds": round(time.time() - t0, 3),
        }
    )


def _predict_embedding(inputs: list[dict[str, Any]]) -> list[list[float]]:
    import torch

    _load_embedding()
    tokenizer = _loaded["tokenizer"]
    model = _loaded["model"]
    device = _loaded["device"]
    texts = [str(item.get("text") or "") for item in inputs]
    enc = tokenizer(
        texts, padding=True, truncation=True, max_length=512, return_tensors="pt"
    )
    enc = {key: value.to(device) for key, value in enc.items()}
    with torch.no_grad():
        out = model(**enc)
    cls = out.last_hidden_state[:, 0]
    cls = torch.nn.functional.normalize(cls, p=2, dim=1)
    return cls.float().cpu().tolist()


def _load_sequence_classifier() -> None:
    if _loaded:
        return
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = _device()
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, revision=MODEL_REVISION)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, revision=MODEL_REVISION
    ).to(device)
    model.eval()
    for param in model.parameters():
        param.requires_grad = False
    _loaded.update({"tokenizer": tokenizer, "model": model, "device": device, "loaded_at": time.time(), "load_seconds": round(time.time() - t0, 3)})


def _predict_sequence_classification(inputs: list[dict[str, Any]]) -> list[Any]:
    import torch

    _load_sequence_classifier()
    tokenizer = _loaded["tokenizer"]
    model = _loaded["model"]
    device = _loaded["device"]
    outputs = []
    for item in inputs:
        text = str(item.get("text") or "")
        query = item.get("query")
        if query is None:
            enc = tokenizer(text, truncation=True, max_length=512, return_tensors="pt")
        else:
            enc = tokenizer(
                str(query), text, truncation=True, max_length=512, return_tensors="pt"
            )
        enc = {key: value.to(device) for key, value in enc.items()}
        with torch.no_grad():
            logits = model(**enc).logits[0].float().cpu()
        problem_type = getattr(model.config, "problem_type", None)
        sequence_mode = SEQUENCE_MODE
        if sequence_mode == "auto":
            sequence_mode = (
                "sigmoid"
                if problem_type == "multi_label_classification" or logits.numel() == 1
                else "softmax"
            )
        if logits.numel() == 1:
            score = torch.sigmoid(logits[0]).item()
            outputs.append({"score": score})
            continue
        probs = (
            torch.sigmoid(logits)
            if sequence_mode == "sigmoid"
            else torch.softmax(logits, dim=-1)
        )
        id2label = getattr(model.config, "id2label", {}) or {}
        scores = [
            {"label": str(id2label.get(i, id2label.get(str(i), i))), "score": probs[i].item()}
            for i in range(probs.numel())
        ]
        best = max(scores, key=lambda row: row["score"]) if scores else {}
        outputs.append({"label": best.get("label"), "score": best.get("score"), "scores": scores})
    return outputs


def _load_zero_shot() -> None:
    if _loaded:
        return
    from transformers import pipeline

    device = 0 if _device() == "cuda" else -1
    t0 = time.time()
    pipe = pipeline(
        "zero-shot-classification",
        model=MODEL_NAME,
        revision=MODEL_REVISION,
        device=device,
    )
    _loaded.update({"pipeline": pipe, "device": device, "loaded_at": time.time(), "load_seconds": round(time.time() - t0, 3)})


def _labels(item: dict[str, Any]) -> list[str]:
    raw = item.get("labels", item.get("categories", item.get("classes", [])))
    if isinstance(raw, str):
        return [label.strip() for label in raw.split(",") if label.strip()]
    if isinstance(raw, list):
        return [str(label) for label in raw if str(label).strip()]
    return []


def _predict_zero_shot(inputs: list[dict[str, Any]]) -> list[Any]:
    _load_zero_shot()
    pipe = _loaded["pipeline"]
    outputs = []
    for item in inputs:
        text = str(item.get("text") or "")
        labels = _labels(item)
        if not labels:
            outputs.append({"label": None, "score": 0.0, "scores": []})
            continue
        result = pipe(text, labels)
        pairs = [
            {"label": label, "score": score}
            for label, score in zip(result.get("labels", []), result.get("scores", []))
        ]
        best = pairs[0] if pairs else {"label": None, "score": 0.0}
        outputs.append({"label": best["label"], "score": best["score"], "scores": pairs})
    return outputs


def _load_gliner() -> None:
    if _loaded:
        return
    from gliner import GLiNER

    t0 = time.time()
    model = GLiNER.from_pretrained(MODEL_NAME)
    _loaded.update({"model": model, "loaded_at": time.time(), "load_seconds": round(time.time() - t0, 3)})


def _predict_gliner(inputs: list[dict[str, Any]]) -> list[Any]:
    _load_gliner()
    model = _loaded["model"]
    outputs = []
    for item in inputs:
        text = str(item.get("text") or "")
        labels = _labels(item) or _labels({"labels": item.get("what", "")})
        threshold = float(item.get("threshold", 0.35))
        entities = model.predict_entities(text, labels, threshold=threshold) if labels else []
        outputs.append(entities)
    return outputs


def _json_or_csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(value) for value in parsed]
    except Exception:
        pass
    return [value.strip() for value in raw.split(",") if value.strip()]


def _json_env(name: str) -> Any:
    raw = os.environ.get(name, "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _download_hf_file(filename: str) -> str:
    if os.path.exists(filename):
        return filename
    from huggingface_hub import hf_hub_download

    return hf_hub_download(
        repo_id=MODEL_NAME,
        filename=filename,
        revision=MODEL_REVISION,
    )


def _load_tabular_model() -> None:
    if _loaded:
        return
    import joblib

    t0 = time.time()
    model_file = os.environ.get("RVBBIT_TABULAR_MODEL_FILE", "")
    candidates = [model_file] if model_file else []
    candidates.extend(["sklearn_model.joblib", "model.joblib", "model.pkl"])
    last_error: Exception | None = None
    model_path = None
    for filename in candidates:
        if not filename:
            continue
        try:
            model_path = _download_hf_file(filename)
            break
        except Exception as exc:
            last_error = exc
    if model_path is None:
        raise HTTPException(
            status_code=500,
            detail=f"could not download tabular model artifact: {last_error}",
        )

    config: dict[str, Any] = {}
    config_file = os.environ.get("RVBBIT_TABULAR_CONFIG_FILE", "")
    if config_file:
        try:
            with open(_download_hf_file(config_file), encoding="utf-8") as fh:
                config = json.load(fh)
        except Exception as exc:
            print(f"[rvbbit-capability] tabular config load failed: {exc}", flush=True)

    features = _json_or_csv_env("RVBBIT_TABULAR_FEATURES")
    if not features:
        features = [str(value) for value in config.get("features", [])]
    labels = _json_or_csv_env("RVBBIT_TABULAR_LABELS")
    target_mapping = _json_env("RVBBIT_TABULAR_TARGET_MAPPING")
    if target_mapping is None:
        target_mapping = config.get("target_mapping") or {}

    model = joblib.load(model_path)
    _loaded.update(
        {
            "model": model,
            "model_path": model_path,
            "config": config,
            "features": features,
            "labels": labels,
            "target_mapping": target_mapping,
            "column_prefix": os.environ.get("RVBBIT_TABULAR_COLUMN_PREFIX", ""),
            "loaded_at": time.time(),
            "load_seconds": round(time.time() - t0, 3),
            "device": "cpu",
        }
    )


def _row_from_item(item: dict[str, Any]) -> dict[str, Any]:
    row = item.get("row", item.get("record", item.get("features")))
    if row is None:
        reserved = {"query", "text", "labels", "categories", "classes", "threshold"}
        return {key: value for key, value in item.items() if key not in reserved}
    if isinstance(row, dict):
        return row
    if isinstance(row, str):
        try:
            parsed = json.loads(row)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    raise HTTPException(status_code=400, detail="tabular input must be a row object")


def _tabular_frame(inputs: list[dict[str, Any]]):
    import pandas as pd

    features: list[str] = _loaded["features"]
    column_prefix: str = _loaded["column_prefix"]
    rows = []
    for item in inputs:
        row = _row_from_item(item)
        if features:
            rows.append({feature: row.get(feature) for feature in features})
        else:
            rows.append(row)
    frame = pd.DataFrame(rows)
    if features:
        frame = frame[features]
    if column_prefix:
        frame.columns = [f"{column_prefix}{column}" for column in frame.columns]
    return frame


def _json_scalar(value: Any) -> Any:
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    return value


def _class_label(value: Any) -> str:
    value = _json_scalar(value)
    mapping = _loaded.get("target_mapping") or {}
    labels = _loaded.get("labels") or []
    key = str(value)
    if key in mapping:
        return str(mapping[key])
    if isinstance(value, int) and 0 <= value < len(labels):
        return str(labels[value])
    return str(value)


def _predict_tabular_classification(inputs: list[dict[str, Any]]) -> list[Any]:
    _load_tabular_model()
    model = _loaded["model"]
    frame = _tabular_frame(inputs)
    predictions = model.predict(frame)
    probabilities = model.predict_proba(frame) if hasattr(model, "predict_proba") else None
    classes = list(getattr(model, "classes_", []))
    outputs = []
    for idx, pred in enumerate(predictions):
        label = _class_label(pred)
        item = {"label": label, "prediction": _json_scalar(pred)}
        if probabilities is not None:
            probs = probabilities[idx]
            scores = [
                {"label": _class_label(cls), "score": float(score)}
                for cls, score in zip(classes, probs)
            ]
            item["scores"] = scores
            if scores:
                best = max(scores, key=lambda row: row["score"])
                item["label"] = best["label"]
                item["score"] = best["score"]
        outputs.append(item)
    return outputs


def _predict_tabular_regression(inputs: list[dict[str, Any]]) -> list[Any]:
    _load_tabular_model()
    frame = _tabular_frame(inputs)
    predictions = _loaded["model"].predict(frame)
    return [{"value": float(_json_scalar(value))} for value in predictions]


def _predict_tabular_foundation(inputs: list[dict[str, Any]]) -> list[Any]:
    """In-context tabular prediction: per request bundle {task, target, support,
    queries}, predict the queries from the labeled support set with NO persisted
    model / training run. This reference handler fits a small model per request
    (CPU). Swap the fit for a TabPFN-class forward pass when a GPU model is
    available — the request/response contract is unchanged."""
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

    outputs: list[Any] = []
    for item in inputs:
        bundle = item.get("bundle") if isinstance(item, dict) and "bundle" in item else item
        if not isinstance(bundle, dict):
            outputs.append({"predictions": [], "error": "bundle not an object"})
            continue
        task = str(bundle.get("task") or "classification")
        target = bundle.get("target")
        support = bundle.get("support") or []
        queries = bundle.get("queries") or []
        if not support or target is None:
            outputs.append({"predictions": [], "error": "empty support or missing target"})
            continue

        sdf = pd.DataFrame(support)
        qdf = pd.DataFrame(queries)
        feat_cols = [c for c in sdf.columns if c != target]
        for c in feat_cols:
            if c not in qdf.columns:
                qdf[c] = None
        # consistent encoding across support + query
        combined = pd.concat([sdf[feat_cols], qdf[feat_cols]], keys=["s", "q"])
        enc = pd.get_dummies(combined, dummy_na=True).fillna(0)
        x_s, x_q = enc.xs("s"), enc.xs("q")
        y = sdf[target]

        is_class = task.endswith("classification") or task == "classification"
        if not is_class:
            try:
                y = y.astype(float)
            except Exception:
                is_class = True
        model = (
            RandomForestClassifier(n_estimators=64, random_state=0)
            if is_class
            else RandomForestRegressor(n_estimators=64, random_state=0)
        )
        model.fit(x_s, y)
        preds = model.predict(x_q)

        if is_class:
            proba = model.predict_proba(x_q) if hasattr(model, "predict_proba") else None
            classes = list(getattr(model, "classes_", []))
            plist: list[Any] = []
            for i, p in enumerate(preds):
                d: dict[str, Any] = {"label": str(p), "prediction": _json_scalar(p)}
                if proba is not None:
                    d["scores"] = [{"label": str(c), "score": float(s)} for c, s in zip(classes, proba[i])]
                    if d["scores"]:
                        best = max(d["scores"], key=lambda r: r["score"])
                        d["label"], d["score"] = best["label"], best["score"]
                plist.append(d)
        else:
            plist = [{"value": float(_json_scalar(v))} for v in preds]
        outputs.append({"predictions": plist, "n_support": len(support), "n_queries": len(queries)})
    return outputs


def _predict_echo(inputs: list[dict[str, Any]]) -> list[Any]:
    outputs = []
    for item in inputs:
        text = str(item.get("text") or "")
        outputs.append(
            {
                "ok": True,
                "echo": text,
                "query": item.get("query"),
                "labels": _labels(item),
                "input": item,
            }
        )
    return outputs


def predict_batch(inputs: list[dict[str, Any]]) -> list[Any]:
    if HANDLER in {"echo", "smoke"}:
        return _predict_echo(inputs)
    if HANDLER == "embedding":
        return _predict_embedding(inputs)
    if HANDLER == "sequence_classification":
        return _predict_sequence_classification(inputs)
    if HANDLER == "zero_shot_classification":
        return _predict_zero_shot(inputs)
    if HANDLER == "gliner":
        return _predict_gliner(inputs)
    if HANDLER == "tabular_classification":
        return _predict_tabular_classification(inputs)
    if HANDLER == "tabular_regression":
        return _predict_tabular_regression(inputs)
    if HANDLER == "tabular_foundation":
        return _predict_tabular_foundation(inputs)
    raise HTTPException(
        status_code=501,
        detail=f"handler {HANDLER!r} needs custom predict_batch implementation",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_error
    if EAGER:
        try:
            predict_batch([{"text": "warmup", "labels": ["entity"]}])
        except Exception as exc:
            _startup_error = repr(exc)
            print(f"[rvbbit-capability] eager warmup failed: {exc}", flush=True)
    yield


app = FastAPI(lifespan=lifespan)


@app.post("/predict", response_model=PredictResponse)
def predict(
    req: PredictRequest,
    authorization: str | None = Header(default=None),
) -> PredictResponse:
    _check_auth(authorization)
    if not req.inputs:
        return PredictResponse(outputs=[])
    return PredictResponse(outputs=predict_batch(req.inputs))


@app.get("/health")
def health() -> dict[str, Any]:
    if _startup_error:
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "error": _startup_error,
                "model": MODEL_NAME,
                "revision": MODEL_REVISION,
                "handler": HANDLER,
            },
        )
    return {
        "ok": True,
        "model": MODEL_NAME,
        "revision": MODEL_REVISION,
        "handler": HANDLER,
        "loaded": bool(_loaded),
        "device": _loaded.get("device", _device()),
        "load_seconds": _loaded.get("load_seconds"),
    }


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    predict_batch([{"text": "warmup", "labels": ["entity"]}])
    return health()
