"""Focused contracts for Calliope's governed spoken-response projection."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
import types
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "warehouse_calliope_voice_test_module", HERE / "calliope.py"
)
calliope = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calliope
SPEC.loader.exec_module(calliope)


def test_voice_config_uses_server_only_elevenlabs_credentials(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_HERMES_URL", "http://hermes:8642")
    monkeypatch.setenv("WAREHOUSE_HERMES_API_KEY", "hermes-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-id")
    monkeypatch.delenv("WAREHOUSE_CALLIOPE_TTS_KEY", raising=False)
    monkeypatch.delenv("WAREHOUSE_CALLIOPE_TTS_VOICE_ID", raising=False)

    config = calliope.CalliopeConfig.from_env()

    assert config.voice_enabled is True
    assert config.voice_api_key == "eleven-secret"
    assert config.voice_id == "voice-id"
    assert config.voice_fast_model == "eleven_flash_v2_5"
    assert config.voice_expressive_model == "eleven_v3"
    assert config.voice_sample_rate == 24_000

    monkeypatch.setenv("WAREHOUSE_CALLIOPE_TTS_KEY", "dedicated-secret")
    assert calliope.CalliopeConfig.from_env().voice_api_key == "dedicated-secret"
    monkeypatch.delenv("ELEVENLABS_VOICE_ID")
    assert calliope.CalliopeConfig.from_env().voice_enabled is False


def test_voice_script_sanitizer_gates_expression_tags_and_markup():
    source = (
        "Spoken version: [excited] **Revenue is $12.4M.** "
        "[whispers] Risk remains elevated. [laughs] [angry] "
        "[unknown direction] See [the dashboard](https://example.com/private)."
    )

    fast = calliope._clean_voice_script(source, "fast")
    expressive = calliope._clean_voice_script(source, "expressive")

    assert "[" not in fast and "]" not in fast
    assert "https://" not in fast
    assert "Revenue is $12.4M" in fast
    assert expressive.count("[") == 2
    assert "[excited]" in expressive
    assert "[whispers]" in expressive
    assert "[unknown direction]" not in expressive
    assert "**" not in expressive


def test_current_three_argument_clover_operator_builds_the_digest_and_attributes_it():
    calls = []

    class Result:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))
            if "to_regprocedure" in query:
                return Result({
                    "clover3": True,
                    "clover2": False,
                    "summarize2": True,
                    "summarize1": False,
                })
            if "clover_llm_apply" in query:
                return Result({
                    "script": "Revenue reached $12.4 million, while churn remains the main risk. The full cohort detail is on screen."
                })
            if "UPDATE rvbbit.receipts" in query:
                return Result(rows=[])
            raise AssertionError(query)

    script, provider, model = calliope._generate_voice_script(
        Connection,
        "Revenue reached $12.4M. Churn is 4.2%. A long table follows.",
        "fast",
        "Warm and concise",
        "pilot@example.com",
    )

    assert provider == "clover"
    assert model == "clover_llm_apply"
    assert "$12.4 million" in script
    operator_call = next(item for item in calls if "SELECT rvbbit.clover_llm_apply" in item[0])
    assert "%s::jsonb" in operator_call[0]
    assert operator_call[1][2] == "{}"
    assert "VOICE_PREFERENCE=\"Warm and concise\"" in operator_call[1][1]
    receipt_call = next(item for item in calls if "UPDATE rvbbit.receipts" in item[0])
    assert receipt_call[1][0] == "pilot@example.com"
    assert "regexp_replace" in receipt_call[0]
    assert "VOICE_PREFERENCE" in receipt_call[0]
    assert "Warm and concise" not in json.dumps(receipt_call[1], default=str)


def test_saved_voice_receipt_keeps_hash_not_browser_personality(monkeypatch, tmp_path):
    turn_id = "c697caa5-f2b0-4e91-ab14-4ad8f666803c"
    session_id = "098567a7-d540-4d03-9094-b477223af0bc"
    stored = {}

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if query.startswith("SELECT t.id"):
                return Result({
                    "id": turn_id,
                    "session_id": session_id,
                    "assistant_message": "Revenue improved, but the result remains preliminary.",
                    "status": "complete",
                    "response_receipt": {"tools": [{"name": "metric", "count": 1}]},
                })
            if query.startswith("UPDATE rvbbit.calliope_turns"):
                stored["json"] = params[0]
                stored["params"] = params
                return Result({"id": turn_id})
            raise AssertionError(query)

    monkeypatch.setattr(
        calliope,
        "_generate_voice_script",
        lambda *_args: (
            "Revenue improved, though the result is still preliminary.",
            "clover",
            "clover_llm_apply",
        ),
    )
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=tmp_path,
        max_image_bytes=1024,
        voice_api_key="eleven-secret",
        voice_id="voice-id",
    )

    render, reused = calliope._voice_render(
        Connection,
        config,
        "pilot@example.com",
        session_id,
        turn_id,
        "expressive",
        "Sound like an incisive, wry analyst",
    )

    assert reused is False
    assert render["mode"] == "expressive"
    assert render["tts_model"] == "eleven_v3"
    assert "personality_hash" in render
    assert "Sound like" not in stored["json"]
    assert json.loads(stored["json"])["script"] == render["script"]
    assert stored["params"][-1] == "pilot@example.com"


def test_elevenlabs_stream_request_never_places_the_key_in_the_url(monkeypatch, tmp_path):
    captured = {}

    class Response:
        status_code = 200

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def build_request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return object()

        async def send(self, _request, stream=False):
            captured["stream"] = stream
            return Response()

        async def aclose(self):
            captured["closed"] = True

    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=tmp_path,
        max_image_bytes=1024,
        voice_api_key="eleven-secret",
        voice_id="voice/id",
    )

    client, response = asyncio.run(calliope._open_voice_provider_stream(config, {
        "script": "Revenue improved, with one material caveat.",
        "tts_model": "eleven_flash_v2_5",
    }))

    assert response.status_code == 200
    assert captured["url"].endswith("/v1/text-to-speech/voice%2Fid/stream/with-timestamps")
    assert "eleven-secret" not in captured["url"]
    assert captured["headers"]["xi-api-key"] == "eleven-secret"
    assert captured["headers"]["accept"] == "application/json"
    assert captured["params"] == {"output_format": "pcm_24000"}
    assert captured["json"]["model_id"] == "eleven_flash_v2_5"
    assert captured["stream"] is True
    asyncio.run(client.aclose())


def test_timed_voice_frame_prefers_original_character_alignment():
    audio = b"\x00\x01\x02\x03"
    original = {
        "characters": ["H", "i"],
        "character_start_times_seconds": [0.0, 0.08],
        "character_end_times_seconds": [0.08, 0.16],
    }
    normalized = {
        "characters": ["H", "e", "l", "l", "o"],
        "character_start_times_seconds": [0, 0.03, 0.06, 0.09, 0.12],
        "character_end_times_seconds": [0.03, 0.06, 0.09, 0.12, 0.16],
    }
    encoded = base64.b64encode(audio).decode("ascii")

    decoded, alignment = calliope._voice_provider_frame(json.dumps({
        "audio_base64": encoded,
        "alignment": original,
        "normalized_alignment": normalized,
    }))
    line = json.loads(calliope._voice_stream_line(
        decoded,
        audio_offset_seconds=1.25,
        alignment=alignment,
    ))

    assert decoded == audio
    assert alignment["characters"] == ["H", "i"]
    assert line["type"] == "audio"
    assert line["audio_offset_seconds"] == 1.25
    assert base64.b64decode(line["audio_base64"]) == audio
    assert line["alignment"]["character_end_times_seconds"] == [0.08, 0.16]


def test_voice_ui_keeps_the_original_turn_and_streams_pcm_in_the_browser():
    theme_source = (HERE / "theme" / "warehouse-theme.src.js").read_text(encoding="utf-8")
    theme_bundle = (HERE / "theme" / "warehouse-theme.js").read_text(encoding="utf-8")
    calliope_source = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    calliope_page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")

    assert "rvbbit-calliope-voice-v1" in theme_source
    # A fresh browser, unreadable storage, or an unknown stored value must all
    # fail closed: speech is opt-in and must never incur provider usage by
    # default.
    assert 'const mode = ["fast", "expressive"].includes(value?.mode) ? value.mode : "off";' in theme_source
    assert 'return { version: 1, mode: "off", personality: "" };' in theme_source
    assert 'preferences: { version: 1, mode: "off", personality: "" }' in calliope_source
    assert 'mode: ["fast", "expressive"].includes(parsed?.mode) ? parsed.mode : "off"' in calliope_source
    assert all(f'data-theme-voice-mode="{mode}"' in theme_source for mode in ("off", "fast", "expressive"))
    assert 'data-theme-voice-mode="off" aria-pressed="true"' in theme_source
    assert "data-theme-voice-personality" in theme_source
    assert "warehouse-voice-change" in theme_source
    assert "getVoice" in theme_source
    assert "rvbbit-calliope-voice-v1" in theme_bundle
    assert 'turn.assistant_message || ""' in calliope_source
    assert "response_receipt?.voice" in calliope_source
    assert "window.AudioContext || window.webkitAudioContext" in calliope_source
    assert "response.body.getReader()" in calliope_source
    assert "getInt16(index * 2, true)" in calliope_source
    assert 'protocol !== "timed-pcm-ndjson-v1"' in calliope_source
    assert "absorbVoiceAlignment" in calliope_source
    assert 'class="voice-transcript"' in calliope_source
    assert "pendingTurns: new Set()" in calliope_source
    assert "voicePresentationPending" in calliope_source
    assert "Shaping the spoken version" in calliope_source
    assert "The complete answer is ready · making it conversational" in calliope_source
    assert "state.voice.pendingTurns.add(String(pending.id))" in calliope_source
    assert "state.voice.pendingTurns.delete(String(turn.id))" in calliope_source
    assert "spoken cut shaping" in calliope_source
    completion_source = calliope_source.split('event === "calliope.turn.completed"', 1)[1]
    assert completion_source.index("state.voice.pendingTurns.add(String(pending.id))") \
        < completion_source.index("renderChat()")
    assert "voice-word is-speaking" not in calliope_source
    assert 'els.voiceDialogScript.innerHTML = safeMarkdown(turn.assistant_message || "")' in calliope_source
    assert 'mode: state.voice.preferences.mode' in calliope_source
    assert 'personality: state.voice.preferences.personality' in calliope_source
    assert 'id="voice-dialog-script"' in calliope_page
    assert "the conversation follows the concise words Calliope speaks" in calliope_page
    assert "Copy full answer" in calliope_page

    calliope_css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    assert ".voice-preparing{" in calliope_css
    assert ".message.assistant.voice-presentation .message-body{min-height:86px" in calliope_css
    assert ".message.voice-reveal .voice-transcript" in calliope_css
    assert "@keyframes voice-transcript-arrive" in calliope_css
