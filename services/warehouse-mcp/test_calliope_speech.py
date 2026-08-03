"""Focused contracts for review-first Calliope speech transcription."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "warehouse_calliope_speech_test_module", HERE / "calliope.py"
)
calliope = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calliope
SPEC.loader.exec_module(calliope)


def test_speech_config_uses_a_dedicated_override_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_HERMES_URL", "http://hermes:8642")
    monkeypatch.setenv("WAREHOUSE_HERMES_API_KEY", "hermes-key")
    monkeypatch.setenv("OPENAI_API_KEY", "shared-openai-key")
    monkeypatch.delenv("WAREHOUSE_CALLIOPE_STT_KEY", raising=False)
    monkeypatch.delenv("WAREHOUSE_CALLIOPE_STT_PROVIDER", raising=False)

    config = calliope.CalliopeConfig.from_env()
    assert config.transcription_enabled is True
    assert config.transcription_api_key == "shared-openai-key"
    assert config.transcription_model == "gpt-transcribe"
    assert config.max_audio_seconds == 120

    monkeypatch.setenv("WAREHOUSE_CALLIOPE_STT_KEY", "dedicated-speech-key")
    assert calliope.CalliopeConfig.from_env().transcription_api_key == "dedicated-speech-key"

    monkeypatch.setenv("WAREHOUSE_CALLIOPE_STT_PROVIDER", "off")
    assert calliope.CalliopeConfig.from_env().transcription_enabled is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"\x1aE\xdf\xa3" + b"\0" * 40, ("webm", "audio/webm")),
        (b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 32, ("wav", "audio/wav")),
        (b"\0\0\0\x18ftypM4A " + b"\0" * 32, ("m4a", "audio/mp4")),
        (b"ID3" + b"\0" * 40, ("mp3", "audio/mpeg")),
    ],
)
def test_audio_uploads_are_magic_byte_identified(payload, expected):
    assert calliope._transcription_audio_format(payload) == expected
    assert calliope._transcription_audio_format(b"not actually audio" * 3) is None


def test_openai_adapter_forwards_bounded_audio_without_exposing_the_key(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "Review ticket ENG-42 with Ada.", "languages": [{"code": "en"}]}

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)
    config = calliope.CalliopeConfig(
        hermes_url="http://hermes:8642",
        hermes_api_key="hermes-key",
        memory_key="company",
        file_root=Path("/tmp/calliope-speech-test"),
        max_image_bytes=1024,
        transcription_provider="openai",
        transcription_api_key="speech-secret",
        transcription_base_url="https://api.openai.com/v1",
        transcription_model="gpt-transcribe",
    )
    audio = b"\x1aE\xdf\xa3" + b"\0" * 80
    result = asyncio.run(calliope._transcribe_audio(
        config,
        audio,
        extension="webm",
        media_type="audio/webm",
        surface="daily_note",
    ))

    assert result == {
        "text": "Review ticket ENG-42 with Ada.",
        "languages": ["en"],
        "provider": "openai",
        "model": "gpt-transcribe",
    }
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["headers"] == {"Authorization": "Bearer speech-secret"}
    assert captured["data"]["model"] == "gpt-transcribe"
    assert "private Daily Brief note" in captured["data"]["prompt"]
    assert captured["files"]["file"] == (
        "calliope-recording.webm", audio, "audio/webm"
    )


def test_transcription_route_is_owner_gated_and_closes_the_upload(monkeypatch, tmp_path):
    routes = {}

    class MCP:
        @staticmethod
        def custom_route(path, methods):
            def register(handler):
                routes[(path, tuple(methods))] = handler
                return handler
            return register

    fake_auth = types.SimpleNamespace(
        read_session_full=lambda request: getattr(request, "session", None)
    )
    monkeypatch.setitem(sys.modules, "auth", fake_auth)
    monkeypatch.setattr(calliope, "ensure_tables", lambda _factory: None)
    monkeypatch.setenv("WAREHOUSE_HERMES_URL", "http://hermes:8642")
    monkeypatch.setenv("WAREHOUSE_HERMES_API_KEY", "hermes-key")
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_DIR", str(tmp_path))
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_STT_KEY", "speech-key")

    seen = {}

    async def fake_transcribe(_config, payload, **kwargs):
        seen["payload"] = payload
        seen.update(kwargs)
        return {
            "text": "A reviewable transcript.",
            "languages": ["en"],
            "provider": "openai",
            "model": "gpt-transcribe",
        }

    monkeypatch.setattr(calliope, "_transcribe_audio", fake_transcribe)
    assert calliope.register_calliope_routes(
        MCP(), lambda: None, "", lambda _slug: ""
    ) is True
    handler = routes[("/api/calliope/transcriptions", ("POST",))]

    class Upload:
        closed = False

        async def read(self, size):
            assert size > len(audio)
            return audio

        async def close(self):
            self.closed = True

    class Request:
        def __init__(self, session):
            self.session = session
            self.headers = {"content-length": str(len(audio) + 256)}
            self.upload = Upload()

        async def form(self):
            return {"file": self.upload, "surface": "chat"}

    audio = b"\x1aE\xdf\xa3" + b"\0" * 80
    unauthenticated = asyncio.run(handler(Request(None)))
    assert unauthenticated.status_code == 401

    request = Request({"identity": "Pilot@Example.com", "mapped": True})
    response = asyncio.run(handler(request))
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["text"] == "A reviewable transcript."
    assert body["retained_audio"] is False
    assert seen == {
        "payload": audio,
        "extension": "webm",
        "media_type": "audio/webm",
        "surface": "chat",
    }
    assert request.upload.closed is True


def test_speech_ui_is_review_first_and_available_in_chat_and_daily_notes():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    editor = (HERE / "calliope-editor" / "editor.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert '@mcp.custom_route("/api/calliope/transcriptions", methods=["POST"])' in backend
    assert '"retained_audio": False' in backend
    assert 'data-speech-record="chat"' in page
    assert 'data-speech-record="daily_note"' in script
    assert "navigator.mediaDevices.getUserMedia" in script
    assert "new MediaRecorder" in script
    assert 'fetch("/api/calliope/transcriptions"' in script
    assert 'els.input.setRangeText(insert, start, end, "end")' in script
    assert "Transcript inserted · review it before sending" in script
    assert "insertText(value" in editor
    assert ".speech-action.recording" in css
    assert "sendTurn();" not in script.split("async function transcribeSpeechRecording", 1)[1].split(
        "async function finishSpeechRecording", 1
    )[0]
