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
import re
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


def _score_seq_logits(logits, problem_type, id2label):
    """Turn one row's logits into the output dict — shared by the batched and
    per-row (mixed-mode) paths so they stay byte-for-byte identical."""
    import torch

    sequence_mode = SEQUENCE_MODE
    if sequence_mode == "auto":
        sequence_mode = (
            "sigmoid"
            if problem_type == "multi_label_classification" or logits.numel() == 1
            else "softmax"
        )
    if logits.numel() == 1:
        return {"score": torch.sigmoid(logits[0]).item()}
    probs = (
        torch.sigmoid(logits)
        if sequence_mode == "sigmoid"
        else torch.softmax(logits, dim=-1)
    )
    scores = [
        {"label": str(id2label.get(i, id2label.get(str(i), i))), "score": probs[i].item()}
        for i in range(probs.numel())
    ]
    best = max(scores, key=lambda row: row["score"]) if scores else {}
    return {"label": best.get("label"), "score": best.get("score"), "scores": scores}


def _predict_sequence_classification(inputs: list[dict[str, Any]]) -> list[Any]:
    """Batched cross-encoder / sequence-classification scoring (the reranker
    path). Previously this looped one forward pass per row, so a 16-pair request
    was 16 sequential GPU calls — the GPU sat idle between tiny calls. Now the
    whole request is tokenized and run as a single batched forward pass, which
    is what actually keeps the GPU busy (10x+ for cross-encoder rerank)."""
    import torch

    _load_sequence_classifier()
    tokenizer = _loaded["tokenizer"]
    model = _loaded["model"]
    device = _loaded["device"]
    if not inputs:
        return []

    texts = [str(item.get("text") or "") for item in inputs]
    queries = [item.get("query") for item in inputs]
    problem_type = getattr(model.config, "problem_type", None)
    id2label = getattr(model.config, "id2label", {}) or {}

    # Batch when every row shares a mode (all query/text pairs — the reranker
    # case — or all single texts). Mixed query/no-query requests are rare and
    # fall back to per-row encodes (still correct, just not batched).
    all_pairs = all(q is not None for q in queries)
    all_single = all(q is None for q in queries)

    # Cap the forward batch so a pathologically large request can't OOM the GPU;
    # the client already chunks by batch_size, so this is just a safety net.
    forward_batch = 64
    outputs: list[Any] = []
    for start in range(0, len(inputs), forward_batch):
        end = min(start + forward_batch, len(inputs))
        chunk_texts = texts[start:end]
        chunk_queries = queries[start:end]
        if all_pairs:
            enc = tokenizer(
                [str(q) for q in chunk_queries], chunk_texts,
                padding=True, truncation=True, max_length=512, return_tensors="pt",
            )
        elif all_single:
            enc = tokenizer(
                chunk_texts, padding=True, truncation=True, max_length=512, return_tensors="pt",
            )
        else:
            for q, t in zip(chunk_queries, chunk_texts):
                enc1 = (
                    tokenizer(str(q), t, truncation=True, max_length=512, return_tensors="pt")
                    if q is not None
                    else tokenizer(t, truncation=True, max_length=512, return_tensors="pt")
                )
                enc1 = {key: value.to(device) for key, value in enc1.items()}
                with torch.no_grad():
                    logits = model(**enc1).logits[0].float().cpu()
                outputs.append(_score_seq_logits(logits, problem_type, id2label))
            continue
        enc = {key: value.to(device) for key, value in enc.items()}
        with torch.no_grad():
            logits_batch = model(**enc).logits.float().cpu()
        for i in range(logits_batch.shape[0]):
            outputs.append(_score_seq_logits(logits_batch[i], problem_type, id2label))
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


def _load_question_answering() -> None:
    if _loaded:
        return
    from transformers import pipeline

    device = 0 if _device() == "cuda" else -1
    t0 = time.time()
    pipe = pipeline(
        "question-answering",
        model=MODEL_NAME,
        revision=MODEL_REVISION,
        device=device,
    )
    _loaded.update({"pipeline": pipe, "device": device, "loaded_at": time.time(), "load_seconds": round(time.time() - t0, 3)})


def _predict_question_answering(inputs: list[dict[str, Any]]) -> list[Any]:
    _load_question_answering()
    pipe = _loaded["pipeline"]
    outputs = []
    for item in inputs:
        question = str(item.get("question") or item.get("query") or "What is the answer?")
        context = str(item.get("context") or item.get("text") or "")
        if not context.strip():
            outputs.append({"answer": "", "score": 0.0, "start": None, "end": None})
            continue
        result = pipe(question=question, context=context)
        outputs.append(
            {
                "answer": result.get("answer", ""),
                "score": float(result.get("score", 0.0) or 0.0),
                "start": result.get("start"),
                "end": result.get("end"),
            }
        )
    return outputs


def _load_summarization() -> None:
    if _loaded:
        return
    from transformers import pipeline

    device = 0 if _device() == "cuda" else -1
    t0 = time.time()
    pipe = pipeline(
        "summarization",
        model=MODEL_NAME,
        revision=MODEL_REVISION,
        device=device,
    )
    _loaded.update({"pipeline": pipe, "device": device, "loaded_at": time.time(), "load_seconds": round(time.time() - t0, 3)})


def _predict_summarization(inputs: list[dict[str, Any]]) -> list[Any]:
    _load_summarization()
    pipe = _loaded["pipeline"]
    outputs = []
    for item in inputs:
        text = str(item.get("text") or item.get("row") or "")
        if not text.strip():
            outputs.append({"summary_text": ""})
            continue
        max_length = int(item.get("max_length") or 160)
        min_length = int(item.get("min_length") or 20)
        result = pipe(
            text,
            max_length=max_length,
            min_length=min_length,
            do_sample=False,
            truncation=True,
        )
        first = result[0] if result else {}
        outputs.append({"summary_text": str(first.get("summary_text") or "")})
    return outputs


def _load_token_classification() -> None:
    if _loaded:
        return
    from transformers import pipeline

    device = 0 if _device() == "cuda" else -1
    t0 = time.time()
    pipe = pipeline(
        "token-classification",
        model=MODEL_NAME,
        revision=MODEL_REVISION,
        device=device,
        aggregation_strategy="simple",
    )
    _loaded.update({"pipeline": pipe, "device": device, "loaded_at": time.time(), "load_seconds": round(time.time() - t0, 3)})


def _predict_token_classification(inputs: list[dict[str, Any]]) -> list[Any]:
    _load_token_classification()
    pipe = _loaded["pipeline"]
    outputs = []
    for item in inputs:
        text = str(item.get("text") or item.get("row") or "")
        if not text.strip():
            outputs.append([])
            continue
        result = pipe(text)
        rows = []
        for entity in result:
            phrase = str(entity.get("word") or entity.get("text") or "").strip()
            rows.append(
                {
                    "text": phrase,
                    "label": str(entity.get("entity_group") or entity.get("entity") or ""),
                    "score": float(entity.get("score", 0.0) or 0.0),
                    "start": entity.get("start"),
                    "end": entity.get("end"),
                }
            )
        outputs.append(rows)
    return outputs


def _table_frame(value: Any):
    import pandas as pd

    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        if all(isinstance(v, list) for v in value.values()):
            return pd.DataFrame(value).astype(str)
        rows = [value]
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
            return _table_frame(parsed)
        except Exception:
            rows = []
    else:
        rows = []
    return pd.DataFrame(rows).astype(str)


def _load_table_question_answering() -> None:
    if _loaded:
        return
    from transformers import pipeline

    device = 0 if _device() == "cuda" else -1
    t0 = time.time()
    pipe = pipeline(
        "table-question-answering",
        model=MODEL_NAME,
        revision=MODEL_REVISION,
        device=device,
    )
    _loaded.update({"pipeline": pipe, "device": device, "loaded_at": time.time(), "load_seconds": round(time.time() - t0, 3)})


def _predict_table_question_answering(inputs: list[dict[str, Any]]) -> list[Any]:
    _load_table_question_answering()
    pipe = _loaded["pipeline"]
    outputs = []
    for item in inputs:
        question = str(item.get("question") or item.get("query") or "")
        table = _table_frame(item.get("table", item.get("rows", item.get("data", []))))
        if table.empty or not question.strip():
            outputs.append({"answer": "", "coordinates": [], "cells": [], "aggregator": None})
            continue
        result = pipe(table=table, query=question)
        outputs.append(
            {
                "answer": str(result.get("answer") or ""),
                "coordinates": result.get("coordinates") or [],
                "cells": [str(cell) for cell in (result.get("cells") or [])],
                "aggregator": result.get("aggregator"),
            }
        )
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


def _series_values(value: Any) -> list[float]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                return _series_values(json.loads(stripped))
            except Exception:
                return []
        values = []
        for token in stripped.split(","):
            try:
                values.append(float(token.strip()))
            except Exception:
                pass
        return values
    if isinstance(value, dict):
        for key in ("series", "values", "data", "rows"):
            if key in value:
                return _series_values(value[key])
        for key in ("value", "y", "target", "amount", "metric"):
            if key in value:
                try:
                    return [float(value[key])]
                except Exception:
                    return []
        return []
    if not isinstance(value, list):
        try:
            return [float(value)]
        except Exception:
            return []

    values: list[float] = []
    for row in value:
        if isinstance(row, dict):
            candidate = None
            for key in ("value", "y", "target", "amount", "metric"):
                if key in row:
                    candidate = row[key]
                    break
        else:
            candidate = row
        try:
            values.append(float(candidate))
        except Exception:
            continue
    return values


def _positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = default
    return max(1, min(maximum, parsed))


def _predict_time_series_forecast(inputs: list[dict[str, Any]]) -> list[Any]:
    """Small seasonal-trend baseline for BI pipelines."""
    import math

    outputs: list[Any] = []
    for item in inputs:
        values = _series_values(item.get("series", item.get("values", item.get("data", item))))
        horizon = _positive_int(item.get("horizon", item.get("steps", 1)), 1, 365)
        season_length = _positive_int(item.get("season_length", 1), 1, 365)
        if len(values) < 1:
            outputs.append({"forecast": [], "error": "empty series", "method": "seasonal_trend_baseline"})
            continue

        diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
        trend_window = diffs[-min(len(diffs), 12):] if diffs else []
        trend = sum(trend_window) / len(trend_window) if trend_window else 0.0
        last = values[-1]

        use_season = season_length > 1 and len(values) >= season_length
        last_cycle = values[-season_length:] if use_season else []
        cycle_mean = sum(last_cycle) / len(last_cycle) if last_cycle else 0.0

        residuals = [diff - trend for diff in trend_window]
        variance = (
            sum(residual * residual for residual in residuals) / len(residuals)
            if residuals
            else 0.0
        )
        sigma = math.sqrt(variance)

        forecast = []
        for step in range(1, horizon + 1):
            seasonal = (
                last_cycle[(step - 1) % len(last_cycle)] - cycle_mean
                if last_cycle
                else 0.0
            )
            value = last + trend * step + seasonal
            interval = 1.96 * sigma * math.sqrt(step)
            forecast.append(
                {
                    "step": step,
                    "value": float(value),
                    "lower": float(value - interval),
                    "upper": float(value + interval),
                }
            )
        outputs.append(
            {
                "forecast": forecast,
                "horizon": horizon,
                "n_observations": len(values),
                "season_length": season_length if use_season else None,
                "trend": float(trend),
                "method": "seasonal_trend_baseline",
            }
        )
    return outputs


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except Exception:
                return value
    return value


def _rows_from_payload(value: Any) -> list[dict[str, Any]]:
    value = _jsonish(value)
    if isinstance(value, dict):
        for key in ("rows", "table", "data", "_table", "collection", "records", "results"):
            if key in value:
                return _rows_from_payload(value[key])
        return [value]
    if isinstance(value, list):
        rows: list[dict[str, Any]] = []
        for row in value:
            row = _jsonish(row)
            if isinstance(row, dict):
                rows.append(row)
            else:
                rows.append({"value": row})
        return rows
    if value is None:
        return []
    return [{"value": value}]


def _values_from_payload(value: Any, preferred_key: str = "value") -> list[Any]:
    value = _jsonish(value)
    if isinstance(value, dict):
        for key in ("values", "series", "collection", "rows", "data", "records"):
            if key in value:
                return _values_from_payload(value[key], preferred_key)
        if preferred_key in value:
            return [value.get(preferred_key)]
        if len(value) == 1:
            return [next(iter(value.values()))]
        return [value]
    if isinstance(value, list):
        values: list[Any] = []
        for item in value:
            item = _jsonish(item)
            if isinstance(item, dict):
                if preferred_key in item:
                    values.append(item.get(preferred_key))
                elif len(item) == 1:
                    values.append(next(iter(item.values())))
                else:
                    values.append(item)
            else:
                values.append(item)
        return values
    return [] if value is None else [value]


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[$,%]", "", text)
    try:
        return float(text)
    except Exception:
        return None


def _semantic_kind(value: Any, column: str = "") -> str:
    if _missing(value):
        return "null"
    text = str(value).strip()
    lowered = text.lower()
    col = column.lower()
    if lowered in {"true", "false", "t", "f", "yes", "no", "y", "n", "0", "1"} and (
        col.startswith("is_") or col.startswith("has_") or col.endswith("_flag") or len(text) <= 5
    ):
        return "boolean"
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        return "email"
    if re.fullmatch(r"https?://\S+", text):
        return "url"
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text):
        return "uuid"
    if re.fullmatch(r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}", text):
        return "phone"
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", text):
        return "ip_address"
    if ("zip" in col or "postal" in col) and re.fullmatch(r"\d{5}(?:-\d{4})?", text):
        return "postal_code"
    if re.fullmatch(r"[$]?\d+(?:,\d{3})*(?:\.\d+)?%?", text):
        return "integer" if re.fullmatch(r"\d+", text) else "number"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", text) or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", text):
        return "date"
    return "string"


def _profile_values(values: list[Any], column: str = "") -> dict[str, Any]:
    count = len(values)
    non_null = [value for value in values if not _missing(value)]
    kind_counts: dict[str, int] = {}
    for value in non_null:
        kind = _semantic_kind(value, column)
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    distinct_values = sorted({str(value) for value in non_null})[:50]
    primary_kind = max(kind_counts.items(), key=lambda row: row[1])[0] if kind_counts else "unknown"
    numbers = [_to_number(value) for value in non_null]
    numbers = [value for value in numbers if value is not None]
    avg_len = (
        sum(len(str(value)) for value in non_null) / len(non_null)
        if non_null
        else 0.0
    )
    distinct_count = len({str(value) for value in non_null})
    distinct_ratio = distinct_count / len(non_null) if non_null else 0.0
    semantic_type = primary_kind
    if primary_kind == "string" and non_null:
        if distinct_count <= min(25, max(3, int(len(non_null) * 0.25))):
            semantic_type = "enum"
        elif avg_len >= 80:
            semantic_type = "long_text"
        else:
            semantic_type = "text"
    pii_types = {"email", "phone", "ip_address"}
    risk = "high" if primary_kind in pii_types else "low"
    return {
        "count": count,
        "non_null_count": len(non_null),
        "null_count": count - len(non_null),
        "null_pct": round(((count - len(non_null)) / count) if count else 0.0, 4),
        "distinct_count": distinct_count,
        "distinct_ratio": round(distinct_ratio, 4),
        "semantic_type": semantic_type,
        "kind_counts": kind_counts,
        "examples": distinct_values[:5],
        "min": min(numbers) if numbers else None,
        "max": max(numbers) if numbers else None,
        "avg_length": round(avg_len, 2),
        "pii_risk": risk,
    }


def _contract_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    columns = sorted({key for row in rows for key in row.keys()})
    column_profiles = []
    checks = []
    for column in columns:
        values = [row.get(column) for row in rows]
        profile = _profile_values(values, column)
        column_checks = []
        if rows and profile["null_count"] == 0:
            column_checks.append({"type": "not_null"})
        if profile["non_null_count"] > 1 and profile["distinct_count"] == profile["non_null_count"]:
            column_checks.append({"type": "unique"})
        if profile["semantic_type"] in {"email", "phone", "url", "uuid", "postal_code"}:
            column_checks.append({"type": "format", "format": profile["semantic_type"]})
        if profile["semantic_type"] == "enum" and profile["distinct_count"] <= 25:
            column_checks.append({"type": "accepted_values", "values": profile["examples"]})
        if profile["min"] is not None and profile["max"] is not None:
            column_checks.append({"type": "range", "min": profile["min"], "max": profile["max"]})
        column_profiles.append({"column": column, **profile, "suggested_checks": column_checks})
        for check in column_checks:
            checks.append({"column": column, **check})
    return {
        "row_count": len(rows),
        "column_count": len(columns),
        "columns": column_profiles,
        "suggested_checks": checks,
    }


def _predict_data_contract_miner(inputs: list[dict[str, Any]]) -> list[Any]:
    outputs = []
    for item in inputs:
        if "value" in item and "rows" not in item and "table" not in item:
            values = _values_from_payload(item.get("value"))
            outputs.append({"column": _profile_values(values), "suggested_checks": _contract_for_rows([{"value": v} for v in values])["suggested_checks"]})
            continue
        rows = _rows_from_payload(item.get("rows", item.get("table", item.get("data", item.get("collection", item)))))
        outputs.append(_contract_for_rows(rows))
    return outputs


def _redact_text(text: str, entities: list[dict[str, Any]]) -> str:
    redacted = text
    for entity in sorted(entities, key=lambda row: row.get("start", 0), reverse=True):
        start = entity.get("start")
        end = entity.get("end")
        label = entity.get("label", "PII")
        if isinstance(start, int) and isinstance(end, int):
            redacted = redacted[:start] + f"[{label}]" + redacted[end:]
    return redacted


def _detect_pii_entities(text: str) -> list[dict[str, Any]]:
    patterns = [
        ("EMAIL", r"[^@\s]+@[^@\s]+\.[^@\s]+"),
        ("PHONE", r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}"),
        ("SSN", r"\b\d{3}-\d{2}-\d{4}\b"),
        ("CREDIT_CARD", r"\b(?:\d[ -]*?){13,16}\b"),
        ("IP_ADDRESS", r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    ]
    entities = []
    for label, pattern in patterns:
        for match in re.finditer(pattern, text):
            entities.append(
                {
                    "label": label,
                    "text": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                    "score": 1.0,
                }
            )
    return sorted(entities, key=lambda row: row["start"])


def _predict_semantic_column_type(inputs: list[dict[str, Any]]) -> list[Any]:
    outputs = []
    for item in inputs:
        if "text" in item and not any(key in item for key in ("values", "rows", "collection")):
            text = str(item.get("text") or "")
            entities = _detect_pii_entities(text)
            outputs.append(
                {
                    "entities": entities,
                    "redacted": _redact_text(text, entities),
                    "pii_risk": "high" if entities else "low",
                }
            )
            continue
        values = _values_from_payload(item.get("values", item.get("collection", item.get("rows", item))))
        column = str(item.get("column") or "")
        profile = _profile_values(values, column)
        outputs.append(
            {
                "semantic_type": profile["semantic_type"],
                "pii_risk": profile["pii_risk"],
                "profile": profile,
            }
        )
    return outputs


def _normalize_key(value: Any) -> str:
    return str(value).strip().lower()


def _column_values(rows: list[dict[str, Any]], column: str) -> list[str]:
    return [_normalize_key(row.get(column)) for row in rows if not _missing(row.get(column))]


def _column_name_score(left: str, right: str) -> float:
    def norm(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    l_norm, r_norm = norm(left), norm(right)
    if left.lower() == right.lower():
        return 1.0
    if l_norm == r_norm:
        return 0.95
    if l_norm.endswith("id") and r_norm.endswith("id") and (l_norm in r_norm or r_norm in l_norm):
        return 0.85
    l_tokens = {token for token in re.split(r"[^a-z0-9]+", left.lower()) if token}
    r_tokens = {token for token in re.split(r"[^a-z0-9]+", right.lower()) if token}
    if l_tokens and r_tokens:
        return len(l_tokens & r_tokens) / len(l_tokens | r_tokens)
    return 0.0


def _join_score(left_values: list[Any], right_values: list[Any], left_name: str = "", right_name: str = "") -> dict[str, Any]:
    left = [_normalize_key(value) for value in left_values if not _missing(value)]
    right = [_normalize_key(value) for value in right_values if not _missing(value)]
    if not left or not right:
        return {"score": 0.0, "overlap_ratio": 0.0, "left_coverage": 0.0, "right_coverage": 0.0}
    l_set, r_set = set(left), set(right)
    overlap = l_set & r_set
    overlap_ratio = len(overlap) / max(1, min(len(l_set), len(r_set)))
    left_coverage = sum(1 for value in left if value in r_set) / len(left)
    right_coverage = sum(1 for value in right if value in l_set) / len(right)
    left_unique = len(l_set) / len(left)
    right_unique = len(r_set) / len(right)
    name_score = _column_name_score(left_name, right_name)
    score = (
        0.5 * overlap_ratio
        + 0.2 * min(left_coverage, right_coverage)
        + 0.15 * min(left_unique, right_unique)
        + 0.15 * name_score
    )
    return {
        "score": round(min(1.0, score), 4),
        "overlap_ratio": round(overlap_ratio, 4),
        "left_coverage": round(left_coverage, 4),
        "right_coverage": round(right_coverage, 4),
        "left_unique_ratio": round(left_unique, 4),
        "right_unique_ratio": round(right_unique, 4),
        "name_score": round(name_score, 4),
        "examples": sorted(overlap)[:5],
    }


def _predict_join_detective(inputs: list[dict[str, Any]]) -> list[Any]:
    outputs = []
    for item in inputs:
        if "left_values" in item or "right_values" in item:
            left_values = _values_from_payload(item.get("left_values", []))
            right_values = _values_from_payload(item.get("right_values", []))
            outputs.append(_join_score(left_values, right_values))
            continue
        left_rows = _rows_from_payload(item.get("left_rows", item.get("left", [])))
        right_rows = _rows_from_payload(item.get("right_rows", item.get("right", [])))
        left_cols = sorted({key for row in left_rows for key in row.keys()})
        right_cols = sorted({key for row in right_rows for key in row.keys()})
        candidates = []
        for left_col in left_cols:
            l_values = _column_values(left_rows, left_col)
            for right_col in right_cols:
                r_values = _column_values(right_rows, right_col)
                stats = _join_score(l_values, r_values, left_col, right_col)
                if stats["score"] <= 0:
                    continue
                candidates.append(
                    {
                        "left_column": left_col,
                        "right_column": right_col,
                        "condition": f"{left_col} = {right_col}",
                        **stats,
                    }
                )
        candidates.sort(key=lambda row: row["score"], reverse=True)
        outputs.append(
            {
                "candidates": candidates[:10],
                "left_columns": left_cols,
                "right_columns": right_cols,
                "left_row_count": len(left_rows),
                "right_row_count": len(right_rows),
            }
        )
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
    if HANDLER == "question_answering":
        return _predict_question_answering(inputs)
    if HANDLER == "summarization":
        return _predict_summarization(inputs)
    if HANDLER == "token_classification":
        return _predict_token_classification(inputs)
    if HANDLER == "table_question_answering":
        return _predict_table_question_answering(inputs)
    if HANDLER == "gliner":
        return _predict_gliner(inputs)
    if HANDLER == "tabular_classification":
        return _predict_tabular_classification(inputs)
    if HANDLER == "tabular_regression":
        return _predict_tabular_regression(inputs)
    if HANDLER == "tabular_foundation":
        return _predict_tabular_foundation(inputs)
    if HANDLER == "time_series_forecast":
        return _predict_time_series_forecast(inputs)
    if HANDLER == "data_contract_miner":
        return _predict_data_contract_miner(inputs)
    if HANDLER == "semantic_column_type":
        return _predict_semantic_column_type(inputs)
    if HANDLER == "join_detective":
        return _predict_join_detective(inputs)
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
