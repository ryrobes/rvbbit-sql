"""LLM-shaped synthetic data generator.

Designed to expose three things:

  1. Per-column ZSTD compression on the JSON column vs row-level TOAST.
  2. Scan throughput when only dimension columns are projected
     (heap pays TOAST detoast cost during scan even if response is NOT selected,
     because the toast pointer is on the heap tuple).
  3. Aggregate throughput on dimensions when the table is JSON-heavy.

Schema (kept in sync with bench/run.py load llm):

    id            bigserial PK
    ts            timestamptz                   -- monotonic-ish
    user_id       bigint                        -- ~user_card distinct
    model         text                          -- ~10 distinct
    tokens_in     int
    tokens_out    int
    latency_ms    int
    status        text                          -- 'ok'/'error'/'timeout'
    prompt        text                          -- 500-4000 chars
    response      jsonb                         -- ~2-10 KB structured
    metadata      jsonb                         -- ~500 B flat dict
"""
from __future__ import annotations

import json
import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

MODELS = ["opus-4-7", "sonnet-4-6", "haiku-4-5", "opus-4-6", "sonnet-4-5"]
STATUSES_WEIGHTED = [("ok", 95), ("error", 3), ("timeout", 2)]
STATUS_POOL = [s for s, w in STATUSES_WEIGHTED for _ in range(w)]

WORD_POOL = [
    "the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog",
    "model", "context", "token", "embedding", "attention", "head",
    "vector", "layer", "transformer", "parameter", "gradient",
    "completion", "response", "prompt", "query", "answer",
]


@dataclass
class GenConfig:
    rows: int
    seed: int = 42
    user_card: int = 100_000
    prompt_min: int = 500
    prompt_max: int = 4000
    response_min: int = 2_000
    response_max: int = 10_000
    start_ts: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _filler(rng: random.Random, min_len: int, max_len: int) -> str:
    """Pseudo-realistic prose. Repetitive enough that ZSTD actually shines."""
    target = rng.randint(min_len, max_len)
    parts: list[str] = []
    total = 0
    while total < target:
        word = rng.choice(WORD_POOL)
        parts.append(word)
        total += len(word) + 1
    return " ".join(parts)[:target]


def _response_payload(rng: random.Random, cfg: GenConfig) -> dict:
    """Mimic a chat-completion-style JSON response with nested structure
    and a chunk of text. Roughly response_min..response_max bytes after
    json.dumps."""
    text = _filler(rng, cfg.response_min, cfg.response_max)
    n_chunks = rng.randint(1, 4)
    chunk_len = len(text) // n_chunks
    return {
        "id": "msg_" + "".join(rng.choices(string.ascii_letters + string.digits, k=24)),
        "model": rng.choice(MODELS),
        "role": "assistant",
        "stop_reason": rng.choice(["end_turn", "max_tokens", "stop_sequence"]),
        "usage": {
            "input_tokens": rng.randint(100, 8000),
            "output_tokens": rng.randint(50, 4000),
        },
        "content": [
            {
                "type": "text",
                "text": text[i * chunk_len : (i + 1) * chunk_len],
            }
            for i in range(n_chunks)
        ],
    }


def _metadata_payload(rng: random.Random) -> dict:
    """Small flat JSON, ~500 bytes."""
    return {
        "client_ip": ".".join(str(rng.randint(1, 254)) for _ in range(4)),
        "region": rng.choice(["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1"]),
        "trace_id": "".join(rng.choices(string.hexdigits.lower(), k=32)),
        "api_version": rng.choice(["2026-01-01", "2025-10-15", "2025-06-01"]),
        "client": rng.choice(["claude-cli", "anthropic-sdk-py", "anthropic-sdk-ts", "curl"]),
        "client_version": f"{rng.randint(0, 5)}.{rng.randint(0, 30)}.{rng.randint(0, 99)}",
        "experiment_arms": rng.sample(
            ["fast_path", "alt_router", "cache_v2", "no_safety", "tool_v3"],
            k=rng.randint(0, 3),
        ),
    }


def rows_iter(cfg: GenConfig) -> Iterator[tuple]:
    """Yield tuples ready for COPY: (ts, user_id, model, tokens_in, tokens_out,
    latency_ms, status, prompt, response_json, metadata_json).

    Note: no `id` — Postgres assigns via bigserial."""
    rng = random.Random(cfg.seed)
    ts = cfg.start_ts
    for _ in range(cfg.rows):
        # advance ts by 50-500ms so timestamps are monotonic but not lockstep
        ts += timedelta(milliseconds=rng.randint(50, 500))
        yield (
            ts,
            rng.randint(1, cfg.user_card),
            rng.choice(MODELS),
            rng.randint(50, 8000),
            rng.randint(20, 4000),
            rng.randint(100, 30_000),
            rng.choice(STATUS_POOL),
            _filler(rng, cfg.prompt_min, cfg.prompt_max),
            json.dumps(_response_payload(rng, cfg)),
            json.dumps(_metadata_payload(rng)),
        )


_SCHEMA = """
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          timestamptz NOT NULL,
    user_id     bigint NOT NULL,
    model       text NOT NULL,
    tokens_in   int NOT NULL,
    tokens_out  int NOT NULL,
    latency_ms  int NOT NULL,
    status      text NOT NULL,
    prompt      text NOT NULL,
    response    jsonb NOT NULL,
    metadata    jsonb NOT NULL
"""

CREATE_DDL_HEAP = f"""
DROP TABLE IF EXISTS llm_events;
CREATE TABLE llm_events ({_SCHEMA});
"""

CREATE_DDL_RVBBIT = f"""
DROP TABLE IF EXISTS llm_events;
CREATE TABLE llm_events ({_SCHEMA}) USING rvbbit;
"""

COPY_COLUMNS = (
    "ts", "user_id", "model", "tokens_in", "tokens_out",
    "latency_ms", "status", "prompt", "response", "metadata",
)

