"""Document extraction sidecar — universal file → markdown.

Turns any staged file (pdf, docx, xlsx, pptx, html, csv, json, images→OCR, plain
text, …) into markdown the brain can chunk + embed. Backs the rvbbit.extract_doc
operator. Files are read from a SHARED STAGING VOLUME by path — the connector
sidecar downloads bytes there, rvbbit passes the path, we read it. No base64.

Distinct from sidecars/extract (GLiNER NER over text): this is whole-file → text.

rvbbit wire shape (rvbbit-native specialist):
  POST /predict
  {"inputs": [{"staged_path": "/staging/4/abc.pdf", "mime": "application/pdf"}, ...]}
  → {"outputs": ["# Extracted markdown…", "…"]}     (strings; same length/order)

A per-item failure returns "" — rvbbit's extract step maps "" → NULL → the file is
skipped (left un-ingested) rather than ingested as an error doc. Keep the backend's
batch_size small; extraction is CPU/IO-heavy.

Rvbbit-side wiring: migration 0047 (register_backend + create_operator), or install
the `doc_extractor` capability (upserts the deployed endpoint).
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

EXPECTED_TOKEN = os.environ.get("EXTRACT_TOKEN", "")
MAX_BYTES = int(os.environ.get("EXTRACT_MAX_BYTES", str(64 * 1024 * 1024)))  # 64 MiB guard
_TEXT_EXT = (".md", ".markdown", ".txt", ".rst", ".org", ".log", ".text")

_md = None


def _converter():
    """Lazy-init MarkItDown so container boot (and /health) stay fast."""
    global _md
    if _md is None:
        from markitdown import MarkItDown  # heavy import

        _md = MarkItDown(enable_plugins=False)
    return _md


app = FastAPI()


class PredictRequest(BaseModel):
    inputs: list[dict[str, Any]]


class PredictResponse(BaseModel):
    outputs: list[str]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


def _extract_one(item: dict[str, Any]) -> str:
    path = str((item or {}).get("staged_path") or "")
    if not path or not os.path.isfile(path):
        return ""  # nothing staged → skip
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return ""  # too large to extract inline (future: streamed path)
        mime = str((item or {}).get("mime") or "").lower()
        # Plain text / markdown: read directly — cheaper than a parser round-trip.
        if mime.startswith("text/") or path.lower().endswith(_TEXT_EXT):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        result = _converter().convert(path)
        return (result.text_content or "").strip()
    except Exception:
        return ""  # per-item failure → skip, never an error doc


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest, authorization: str = Header(default="")) -> PredictResponse:
    if EXPECTED_TOKEN and authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")
    return PredictResponse(outputs=[_extract_one(it) for it in req.inputs])
