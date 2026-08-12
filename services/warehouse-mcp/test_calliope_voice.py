"""Focused contracts for Calliope's governed spoken-response projection."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


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
    monkeypatch.setenv("RVBBIT_CLOVER_KEY", "clover-secret")
    monkeypatch.setenv("RVBBIT_CLOVER_OPENAI_BASE_URL", "https://clover.example/v1")
    monkeypatch.setenv("RVBBIT_CLOVER_REQUIRED_MODEL", "calliope")
    monkeypatch.delenv("WAREHOUSE_CALLIOPE_TTS_KEY", raising=False)
    monkeypatch.delenv("WAREHOUSE_CALLIOPE_TTS_VOICE_ID", raising=False)
    monkeypatch.delenv("WAREHOUSE_CALLIOPE_TTS_PREPARE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("WAREHOUSE_CALLIOPE_TTS_EXPRESSIVE_STABILITY", raising=False)

    config = calliope.CalliopeConfig.from_env()

    assert config.voice_enabled is True
    assert config.voice_api_key == "eleven-secret"
    assert config.voice_id == "voice-id"
    assert config.voice_fast_model == "eleven_flash_v2_5"
    assert config.voice_expressive_model == "eleven_v3"
    assert config.voice_expressive_stability == 0.3
    assert config.voice_sample_rate == 24_000
    assert config.voice_prepare_timeout_seconds == 30
    assert config.voice_rewrite_api_key == "clover-secret"
    assert config.voice_rewrite_base_url == "https://clover.example/v1"
    assert config.voice_rewrite_model == "calliope"

    monkeypatch.setenv("WAREHOUSE_CALLIOPE_TTS_KEY", "dedicated-secret")
    assert calliope.CalliopeConfig.from_env().voice_api_key == "dedicated-secret"
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_TTS_PREPARE_TIMEOUT_SECONDS", "2")
    assert calliope.CalliopeConfig.from_env().voice_prepare_timeout_seconds == 5
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_TTS_PREPARE_TIMEOUT_SECONDS", "999")
    assert calliope.CalliopeConfig.from_env().voice_prepare_timeout_seconds == 120
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_TTS_EXPRESSIVE_STABILITY", "0.18")
    assert calliope.CalliopeConfig.from_env().voice_expressive_stability == 0.18
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_TTS_EXPRESSIVE_STABILITY", "8")
    assert calliope.CalliopeConfig.from_env().voice_expressive_stability == 1.0
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_TTS_EXPRESSIVE_STABILITY", "not-a-number")
    assert calliope.CalliopeConfig.from_env().voice_expressive_stability == 0.3
    monkeypatch.delenv("ELEVENLABS_VOICE_ID")
    assert calliope.CalliopeConfig.from_env().voice_enabled is False


def test_voice_script_normalizer_preserves_expression_tags_markup_and_urls():
    source = (
        "Spoken version: [dryly amused] **Revenue is $12.4M.** "
        "[with a weary little sigh] Risk remains elevated. "
        "[laughs, then whispers] [direction 2] "
        "See [the dashboard](https://example.com/private)."
    )

    fast = calliope._normalize_voice_script(source, "fast")
    expressive = calliope._normalize_voice_script(source, "expressive")

    assert fast == expressive
    assert fast == source
    assert fast.startswith("Spoken version: [dryly amused]")
    assert "https://example.com/private" in fast
    assert "[dryly amused]" in expressive
    assert "[with a weary little sigh]" in expressive
    assert "[laughs, then whispers]" in expressive
    assert "[direction 2]" in expressive
    assert "**" in expressive


def test_voice_script_normalizer_only_removes_transport_invalid_controls():
    source = "\x00Spoken response:  First fact.\n\n[unusual_tag_2]   Final fact.\x7f"

    normalized = calliope._normalize_voice_script(source, "expressive")

    assert normalized == "Spoken response:  First fact.\n\n[unusual_tag_2]   Final fact."


def test_voice_script_normalizer_never_clips_a_complete_semantic_rewrite():
    source = " ".join(f"material-fact-{index}" for index in range(180))
    source += " FINAL_MATERIAL_FACT"

    cleaned = calliope._normalize_voice_script(source, "fast")

    assert cleaned == source
    assert len(cleaned.split()) == 181
    assert cleaned.endswith("FINAL_MATERIAL_FACT")
    assert not cleaned.endswith("…")


def test_voice_fallback_keeps_the_complete_canonical_answer():
    source = " ".join(f"source-fact-{index}." for index in range(100))
    source += " FINAL_SOURCE_FACT."

    fallback = calliope._fallback_voice_script(source, "fast")

    assert fallback.endswith("FINAL_SOURCE_FACT.")
    assert len(fallback.split()) == 101


def test_expressive_instruction_follows_personality_without_a_fixed_tag_script():
    expressive = calliope._voice_rewrite_instruction(
        "expressive",
        "Dry, wry, intimate, and willing to sound genuinely surprised",
    )
    fast = calliope._voice_rewrite_instruction(
        "fast",
        "Dry, wry, intimate, and willing to sound genuinely surprised",
    )

    assert "materially shape word choice, cadence, pacing, emphasis" in expressive
    assert "not a fixed vocabulary" in expressive
    assert 'VOICE_PERSONALITY="Dry, wry, intimate' in expressive
    assert "neutral corporate narration" in expressive
    assert "VOICE_PERSONALITY" not in fast
    assert "neutral conversational delivery" in fast


def test_voice_context_uses_spoken_history_with_canonical_fallback():
    previous_turns_newest_first = [
        {
            "user_message": "What changed after the promotion?",
            "assistant_message": "The full newest answer with tool detail.",
            "response_receipt": {
                "tools": [{"name": "warehouse_query", "arguments": {"secret": "nope"}}],
                "voice": {"script": "The promotion lifted revenue, but margin softened."},
            },
        },
        {
            "user_message": "How did the baseline look?",
            "assistant_message": "The baseline was steady at twelve million dollars.",
            "response_receipt": {"tools": [{"name": "warehouse_query"}]},
        },
        {
            "user_message": "Start with the quarterly trend.",
            "assistant_message": "The full oldest answer.",
            "response_receipt": {
                "voice": {"script": "Quarterly revenue rose in each of the last three periods."}
            },
        },
    ]

    messages = calliope._voice_context_messages(
        previous_turns_newest_first,
        "So what should we do next?",
    )

    assert messages == [
        {"role": "user", "content": "Start with the quarterly trend."},
        {
            "role": "assistant",
            "content": "Quarterly revenue rose in each of the last three periods.",
        },
        {"role": "user", "content": "How did the baseline look?"},
        {
            "role": "assistant",
            "content": "The baseline was steady at twelve million dollars.",
        },
        {"role": "user", "content": "What changed after the promotion?"},
        {
            "role": "assistant",
            "content": "The promotion lifted revenue, but margin softened.",
        },
        {"role": "user", "content": "So what should we do next?"},
    ]
    assert "warehouse_query" not in json.dumps(messages)
    assert "full newest answer" not in json.dumps(messages)


def test_voice_builder_uses_calliope_chat_without_any_output_cap(monkeypatch, tmp_path):
    captured = {}
    source = "SOURCE_OPEN " + ("source-data " * 2_100) + "FINAL_SOURCE_FACT"
    rewrite = " ".join(f"spoken-fact-{index}" for index in range(180))
    rewrite += " FINAL_SPOKEN_FACT"
    context = [
        {"role": "user", "content": "How did the first quarter look?"},
        {"role": "assistant", "content": "It was strong, with one caveat."},
        {"role": "user", "content": "What happened next?"},
    ]

    class Response:
        status_code = 200

        def json(self):
            return {
                "model": "calliope-voice-test",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": rewrite},
                }],
            }

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr(calliope.httpx, "Client", Client)
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=tmp_path,
        max_image_bytes=1024,
        voice_rewrite_api_key="clover-secret",
        voice_rewrite_base_url="https://clover.example/v1",
        voice_rewrite_model="calliope",
    )

    script, provider, model = calliope._generate_voice_script(
        config,
        source,
        "fast",
        "This must not enter fast mode",
        "hosted@example.com",
        context,
    )

    assert captured["url"] == "https://clover.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer clover-secret"
    assert "clover-secret" not in captured["url"]
    assert captured["json"]["model"] == "calliope"
    assert captured["json"]["messages"][1:4] == context
    assert captured["json"]["messages"][4] == {
        "role": "assistant",
        "content": source,
    }
    assert captured["json"]["messages"][5] == {
        "role": "user",
        "content": (
            "Rewrite Calliope's immediately preceding answer for spoken delivery. "
            "Return only the words to speak."
        ),
    }
    instruction = captured["json"]["messages"][0]["content"]
    assert "Completeness wins over a length target" in instruction
    assert "Use those earlier messages only for conversational continuity" in instruction
    assert "This must not enter fast mode" not in instruction
    assert "max_tokens" not in captured["json"]
    assert "max_output_tokens" not in captured["json"]
    assert script == rewrite
    assert script.endswith("FINAL_SPOKEN_FACT")
    assert provider == "clover"
    assert model == "calliope-voice-test"


def test_voice_builder_trusts_a_short_complete_clover_rewrite(monkeypatch, tmp_path):
    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, _url, **_kwargs):
            return types.SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "model": "calliope",
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "All clear."},
                    }],
                },
            )

    monkeypatch.setattr(calliope.httpx, "Client", Client)
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=tmp_path,
        max_image_bytes=1024,
        voice_rewrite_api_key="clover-secret",
        voice_rewrite_model="calliope",
    )

    script, provider, model = calliope._generate_voice_script(
        config,
        "The canonical answer is longer, but the actual conclusion is uncomplicated.",
        "fast",
        "",
        "hosted@example.com",
    )

    assert script == "All clear."
    assert provider == "clover"
    assert model == "calliope"


def test_voice_builder_rejects_provider_length_stop_and_uses_complete_answer(
    monkeypatch, tmp_path, capsys,
):
    source = "**First material fact.**\n\n[firmly] FINAL_SOURCE_FACT."

    class Response:
        status_code = 200

        def json(self):
            return {
                "model": "calliope",
                "choices": [{
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "Partial output that"},
                }],
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, _url, **_kwargs):
            return Response()

    monkeypatch.setattr(calliope.httpx, "Client", Client)
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=tmp_path,
        max_image_bytes=1024,
        voice_rewrite_api_key="clover-secret",
        voice_rewrite_model="calliope",
    )

    script, provider, model = calliope._generate_voice_script(
        config,
        source,
        "expressive",
        "Natural",
        "hosted@example.com",
    )

    assert script == source
    assert "Partial output" not in script
    assert provider == "canonical"
    assert model is None
    assert "incomplete voice rewrite (length)" in capsys.readouterr().err


def test_voice_builder_without_clover_key_uses_complete_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(
        calliope.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("Clover must not be called without its key"),
    )
    source = "Revenue reached $12.4M. Churn remains the main risk."
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=tmp_path,
        max_image_bytes=1024,
    )

    script, provider, model = calliope._generate_voice_script(
        config, source, "fast", "", "hosted@example.com"
    )

    assert script == source
    assert provider == "canonical"
    assert model is None


def test_saved_voice_receipt_keeps_hash_not_browser_personality(monkeypatch, tmp_path):
    turn_id = "c697caa5-f2b0-4e91-ab14-4ad8f666803c"
    session_id = "098567a7-d540-4d03-9094-b477223af0bc"
    stored = {}

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if "set_config('statement_timeout'" in query:
                return Result({"set_config": params[0]})
            if query.startswith("SELECT t.id"):
                return Result({
                    "id": turn_id,
                    "session_id": session_id,
                    "ordinal": 4,
                    "user_message": "Is that improvement durable?",
                    "assistant_message": "Revenue improved, but the result remains preliminary.",
                    "status": "complete",
                    "response_receipt": {"tools": [{"name": "metric", "count": 1}]},
                })
            if query.startswith("SELECT user_message"):
                return Result([{
                    "user_message": "How is revenue tracking?",
                    "assistant_message": "Revenue is improving, with one caveat.",
                    "response_receipt": {
                        "voice": {"script": "Revenue is up, although the evidence is early."}
                    },
                }])
            if query.startswith("UPDATE rvbbit.calliope_turns"):
                stored["json"] = params[0]
                stored["params"] = params
                return Result({"id": turn_id})
            raise AssertionError(query)

    generated = {}

    def generate_voice_script(*args):
        generated["context"] = args[5]
        return (
            "Revenue improved, though the result is still preliminary.",
            "clover",
            "calliope",
        )

    monkeypatch.setattr(calliope, "_generate_voice_script", generate_voice_script)
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
    assert render["version"] == 5
    assert render["mode"] == "expressive"
    assert render["tts_model"] == "eleven_v3"
    assert render["tts_stability"] == 0.3
    assert "personality_hash" in render
    assert "context_hash" in render
    assert render["context_message_count"] == 3
    assert generated["context"] == [
        {"role": "user", "content": "How is revenue tracking?"},
        {
            "role": "assistant",
            "content": "Revenue is up, although the evidence is early.",
        },
        {"role": "user", "content": "Is that improvement durable?"},
    ]
    assert render["rewrite_elapsed_ms"] >= 0
    assert "Sound like" not in stored["json"]
    assert json.loads(stored["json"])["script"] == render["script"]
    assert stored["params"][-1] == "pilot@example.com"


def test_stale_voice_render_contract_is_not_playable():
    turn_id = "c697caa5-f2b0-4e91-ab14-4ad8f666803c"
    render_id = "5b9914b4-87c7-443c-98b1-9f48c695b905"

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
            if "set_config('statement_timeout'" in query:
                return Result({"set_config": params[0]})
            if query.startswith("SELECT t.response_receipt"):
                return Result({
                    "response_receipt": {
                        "voice": {
                            "id": render_id,
                            "version": 1,
                            "mode": "expressive",
                            "script": "[warmly] Revenue improved.",
                        }
                    }
                })
            raise AssertionError(query)

    with pytest.raises(LookupError):
        calliope._voice_render_for_audio(
            Connection,
            "pilot@example.com",
            turn_id,
            render_id,
        )


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
    assert captured["json"]["voice_settings"] == {
        "stability": 0.4,
        "similarity_boost": 0.75,
    }
    assert captured["stream"] is True
    asyncio.run(client.aclose())

    expressive_client, expressive_response = asyncio.run(
        calliope._open_voice_provider_stream(config, {
            "script": "[quietly delighted] Revenue improved, with one material caveat.",
            "tts_model": "eleven_v3",
        })
    )
    assert expressive_response.status_code == 200
    assert captured["json"]["model_id"] == "eleven_v3"
    assert captured["json"]["voice_settings"] == {"stability": 0.3}
    asyncio.run(expressive_client.aclose())


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


def test_voice_provider_stream_surfaces_embedded_elevenlabs_error():
    with pytest.raises(calliope.VoiceProviderError) as raised:
        calliope._voice_provider_frame(json.dumps({
            "detail": {
                "status": "invalid_model",
                "message": "The selected model is not available for this voice.",
            }
        }))

    assert raised.value.code == "VOICE_PROVIDER_ERROR"
    assert str(raised.value) == (
        "ElevenLabs: The selected model is not available for this voice."
    )


def test_elevenlabs_http_error_preserves_safe_provider_detail(monkeypatch, tmp_path, capsys):
    class Response:
        status_code = 400
        headers = {"request-id": "provider-request-123"}

        async def aread(self):
            return b""

        async def aclose(self):
            return None

        @staticmethod
        def json():
            return {
                "detail": {
                    "status": "invalid_model",
                    "message": "The selected model cannot synthesize this request.",
                }
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def build_request(*_args, **_kwargs):
            return object()

        async def send(self, _request, stream=False):
            assert stream is True
            return Response()

        async def aclose(self):
            return None

    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=tmp_path,
        max_image_bytes=1024,
        voice_api_key="eleven-secret",
        voice_id="voice-id",
    )

    with pytest.raises(calliope.VoiceProviderError) as raised:
        asyncio.run(calliope._open_voice_provider_stream(config, {
            "script": "Revenue improved, with one material caveat.",
            "tts_model": "eleven_v3",
        }))

    assert raised.value.code == "VOICE_PROVIDER_ERROR"
    assert "selected model cannot synthesize" in str(raised.value)
    log = capsys.readouterr().err
    assert "status=400" in log
    assert "request_id=provider-request-123" in log


def test_elevenlabs_connection_error_is_classified_and_logged(monkeypatch, tmp_path, capsys):
    class Client:
        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def build_request(*_args, **_kwargs):
            return object()

        async def send(self, _request, stream=False):
            assert stream is True
            raise calliope.httpx.ConnectError("TLS connection failed")

        async def aclose(self):
            return None

    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=tmp_path,
        max_image_bytes=1024,
        voice_api_key="eleven-secret",
        voice_id="voice-id",
    )

    with pytest.raises(calliope.VoiceProviderError) as raised:
        asyncio.run(calliope._open_voice_provider_stream(config, {
            "script": "Revenue improved, with one material caveat.",
            "tts_model": "eleven_v3",
        }))

    assert raised.value.code == "VOICE_PROVIDER_UNAVAILABLE"
    assert "could not be reached" in str(raised.value)
    log = capsys.readouterr().err
    assert "ElevenLabs voice connection failed" in log
    assert "error=ConnectError" in log
    assert "detail=TLS connection failed" in log


def test_voice_preparation_deadline_reports_the_stalled_stage():
    backend_source = (HERE / "calliope.py").read_text(encoding="utf-8")

    assert '"prepare_timeout_seconds": config.voice_prepare_timeout_seconds' in backend_source
    assert '"model": config.voice_rewrite_model' in backend_source
    assert '"output_limit": None' in backend_source
    assert '"code": "VOICE_TEXT_TIMEOUT"' in backend_source
    assert '"stage": "text"' in backend_source
    assert backend_source.count('"code": "VOICE_AUDIO_TIMEOUT"') >= 2
    assert '"stage": "audio"' in backend_source
    assert "first_audio_deadline" in backend_source
    assert "asyncio.wait_for" in backend_source


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
    assert 'personality.disabled = voice.mode !== "expressive"' in theme_source
    assert "Expressive performs it in your speaking personality; Fast stays neutral." in theme_source
    assert "Expressive performs it in your speaking personality; Fast stays neutral." in theme_bundle
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
    assert "failures: new Map()" in calliope_source
    assert "voicePresentationPending" in calliope_source
    assert "text_to_speech?.render_version" in calliope_source
    assert r"\[[^\[\]\r\n]+\]" in calliope_source
    assert "Shaping the spoken version" in calliope_source
    assert "The complete answer is ready · making it conversational" in calliope_source
    assert "voicePreparationTimeoutMs" in calliope_source
    assert "preservingCurrentActivity" in calliope_source
    assert "liveVoiceTurn" in calliope_source
    select_source = calliope_source.split("async function selectSession", 1)[1].split(
        "async function createSession", 1
    )[0]
    assert select_source.index("if (!preservingCurrentActivity)") \
        < select_source.index("stopVoicePlayback()")
    assert "Audio generation failed · ${detail}" in calliope_source
    assert "voiceFailed.code" in calliope_source
    assert "rewriteElapsedMs >= voicePreparationTimeoutMs() * 0.6" in calliope_source
    assert 'timeoutError.code = "VOICE_AUDIO_TIMEOUT"' in calliope_source
    assert 'showVoiceFailure(liveVoiceTurn(turn), "text", error, timedOut)' in calliope_source
    assert 'showVoiceFailure(' in calliope_source
    assert '"audio",' in calliope_source
    assert "onFirstAudio: () => revealVoicePresentation(liveVoiceTurn(turn))" in calliope_source
    assert "turn.response_receipt" in calliope_source
    assert "state.voice.pendingTurns.add(String(pending.id))" in calliope_source
    assert "state.voice.pendingTurns.delete(String(turn.id))" in calliope_source
    assert "spoken rendering shaping" in calliope_source
    completion_source = calliope_source.split('event === "calliope.turn.completed"', 1)[1]
    assert completion_source.index("state.voice.pendingTurns.add(String(pending.id))") \
        < completion_source.index("renderChat()")
    assert "voice-word is-speaking" not in calliope_source
    assert 'els.voiceDialogScript.innerHTML = safeMarkdown(turn.assistant_message || "")' in calliope_source
    assert 'mode: state.voice.preferences.mode' in calliope_source
    assert 'personality: state.voice.preferences.personality' in calliope_source
    assert 'id="voice-dialog-script"' in calliope_page
    assert "the conversation follows the words Calliope speaks" in calliope_page
    assert "Copy full answer" in calliope_page

    calliope_css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    assert ".voice-preparing{" in calliope_css
    assert ".voice-fallback-note{" in calliope_css
    assert ".message.assistant.voice-presentation .message-body{min-height:86px" in calliope_css
    assert ".message.voice-reveal .voice-transcript" in calliope_css
    assert "@keyframes voice-transcript-arrive" in calliope_css
