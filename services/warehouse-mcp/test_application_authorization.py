"""Application authorization keeps credential actors and human subjects distinct."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import auth  # noqa: E402
import server  # noqa: E402


def _context(
    email="person@example.com",
    *,
    platform="google_chat",
    session_id="spaces/AAA/thread/person@example.com",
):
    envelope = {
        "source": "hermes",
        "platform": platform,
        "session_id": session_id,
    }
    if email is not None:
        envelope["user_id"] = email
    metadata = {server._HERMES_CALLER_META_KEY: envelope}
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta=metadata,
            request=SimpleNamespace(
                root=SimpleNamespace(params={"_meta": metadata})
            ),
        )
    )


def test_auth_provider_distinguishes_hermes_legacy_and_direct_oauth(monkeypatch):
    monkeypatch.setattr(auth, "STATE_FILE", "")
    monkeypatch.setattr(auth, "JWT_SECRET", "authorization-test-jwt")
    monkeypatch.setattr(auth, "STATIC_KEY", "legacy-shared-key")
    monkeypatch.setattr(auth, "STATIC_CALLER", "legacy-service@example.com")
    monkeypatch.setattr(auth, "HERMES_KEY", "hermes-only-key")
    monkeypatch.setattr(auth, "HERMES_CALLER", "calliope@example.com")
    provider = auth.WarehouseAuthProvider("https://warehouse.example")

    hermes = asyncio.run(provider.load_access_token("hermes-only-key"))
    legacy = asyncio.run(provider.load_access_token("legacy-shared-key"))
    now = int(time.time())
    direct_token = jwt.encode(
        {
            "iss": provider.public,
            "sub": "direct.user@example.com",
            "aud": provider.audience,
            "client_id": "codex-oauth-client",
            "scope": auth.SCOPE,
            "iat": now,
            "exp": now + 300,
        },
        auth.JWT_SECRET,
        algorithm=auth.JWT_ALG,
    )
    direct = asyncio.run(provider.load_access_token(direct_token))

    assert (hermes.client_id, hermes.email) == (
        auth.HERMES_CLIENT_ID,
        "calliope@example.com",
    )
    assert (legacy.client_id, legacy.email) == (
        "static-key",
        "legacy-service@example.com",
    )
    assert (direct.client_id, direct.email) == (
        "codex-oauth-client",
        "direct.user@example.com",
    )


def test_hermes_key_must_not_reuse_a_general_or_jwt_secret(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET", "jwt-secret")
    monkeypatch.setattr(auth, "STATIC_KEY", "shared-secret")
    monkeypatch.setattr(auth, "HERMES_KEY", "shared-secret")
    errors = auth.validate_config()
    assert any("must differ from WAREHOUSE_MCP_KEY" in error for error in errors)

    monkeypatch.setattr(auth, "HERMES_KEY", "jwt-secret")
    errors = auth.validate_config()
    assert any("must differ from WAREHOUSE_HERMES_MCP_KEY" in error for error in errors)


def test_calliope_deployment_warns_when_delegation_key_is_missing(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_HERMES_URL", "http://hermes:8642")
    monkeypatch.setattr(auth, "HERMES_KEY", "")
    assert any(
        "attribution-only" in warning for warning in auth.config_warnings()
    )


def test_direct_oauth_is_authoritative_over_forged_forwarding(monkeypatch):
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("direct.user@example.com", "codex-oauth-client"),
    )

    authorization = server._application_authorization_context(
        _context("forged.user@example.com"),
        caller_override="forged.browser@example.com",
    )

    assert authorization.actor == "direct.user@example.com"
    assert authorization.subject == "direct.user@example.com"
    assert authorization.attributed_subject == "direct.user@example.com"
    assert authorization.mode == "direct_oauth"
    assert authorization.delegated is False


def test_dedicated_hermes_key_delegates_verified_google_chat_sender(monkeypatch):
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", {"@example.com"})
    monkeypatch.setattr(auth, "GOOGLE_HD", "example.com")
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", server._HERMES_SERVICE_CLIENT_ID),
    )

    authorization = server._application_authorization_context(
        _context("Person@Example.com")
    )

    assert authorization.actor == "calliope@example.com"
    assert authorization.subject == "person@example.com"
    assert authorization.attributed_subject == "person@example.com"
    assert authorization.mode == "google_chat_delegation"
    assert authorization.assurance == "hermes_service_credential"
    assert authorization.delegated is True


def test_hermes_sender_outside_warehouse_audience_is_not_authorized(monkeypatch):
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", {"@example.com"})
    monkeypatch.setattr(auth, "GOOGLE_HD", "example.com")
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", server._HERMES_SERVICE_CLIENT_ID),
    )
    authorization = server._application_authorization_context(
        _context("external@outside.example")
    )

    assert authorization.subject is None
    assert authorization.attributed_subject == "external@outside.example"
    assert authorization.mode == "google_chat_sender_not_allowed"
    assert authorization.delegated is False
    token = server._AUTHORIZATION_CONTEXT.set(authorization)
    try:
        # Mutation/ownership helpers see the service actor, not the rejected
        # attributed sender. The activity receipt still retains attribution.
        assert server._caller() == (
            "calliope@example.com",
            server._HERMES_SERVICE_CLIENT_ID,
        )
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)


def test_legacy_shared_key_forwarding_stays_attribution_only(monkeypatch):
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", "static-key"),
    )

    authorization = server._application_authorization_context(
        _context("Person@Example.com")
    )

    assert authorization.actor == "calliope@example.com"
    assert authorization.subject is None
    assert authorization.attributed_subject == "person@example.com"
    assert authorization.mode == "legacy_hermes_attribution"
    with pytest.raises(PermissionError):
        token = server._AUTHORIZATION_CONTEXT.set(authorization)
        try:
            server._require_application_subject()
        finally:
            server._AUTHORIZATION_CONTEXT.reset(token)


def test_calliope_web_subject_comes_from_warehouse_session_not_envelope(monkeypatch):
    observed = {}
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", server._HERMES_SERVICE_CLIENT_ID),
    )

    def linked(_conn, session_ref):
        observed["session_ref"] = session_ref
        return {
            "owner": "Signed.Owner@Example.com",
            "calliope_session_id": "755303d5-7f91-42c3-bbea-b989e34a56c9",
            "kind": "session",
        }

    monkeypatch.setattr(server, "_calliope_activity_for_hermes_session", linked)
    authorization = server._application_authorization_context(
        _context(
            "forged.user@example.com",
            platform="api_server",
            session_id="calliope_opaque_123",
        ),
        conn=object(),
    )

    assert observed["session_ref"] == "calliope_opaque_123"
    assert authorization.actor == "calliope@example.com"
    assert authorization.subject == "signed.owner@example.com"
    assert authorization.attributed_subject == "signed.owner@example.com"
    assert authorization.mode == "calliope_session"
    assert authorization.assurance == "warehouse_session_ledger"
    assert authorization.delegated is True


def test_cron_and_unresolved_service_calls_never_become_people(monkeypatch):
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", server._HERMES_SERVICE_CLIENT_ID),
    )
    cron = server._application_authorization_context(
        _context(
            "forged.user@example.com",
            platform="cron",
            session_id="nightly-dreams",
        )
    )
    assert cron.subject is None
    assert cron.attributed_subject is None
    assert cron.mode == "hermes_automation"

    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("machine-client", "external-service-client"),
    )
    service = server._application_authorization_context(
        _context("forged.user@example.com")
    )
    assert service.actor == "machine-client"
    assert service.subject is None
    assert service.mode == "direct_service"


def test_signed_browser_owner_is_an_application_subject_without_mcp_token(monkeypatch):
    monkeypatch.setattr(server, "_authenticated_caller", lambda: (None, None))
    authorization = server._application_authorization_context(
        caller_override="Browser.User@Example.com"
    )
    assert authorization.actor == "browser.user@example.com"
    assert authorization.subject == "browser.user@example.com"
    assert authorization.mode == "browser_session"
    assert authorization.delegated is False


def test_logged_call_freezes_and_resets_authorization_context(monkeypatch):
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", {"@example.com"})
    monkeypatch.setattr(auth, "GOOGLE_HD", "example.com")
    metadata = _context("person@example.com").request_context.meta
    monkeypatch.setattr(server, "_mcp_request_metadata", lambda _ctx=None: metadata)
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", server._HERMES_SERVICE_CLIENT_ID),
    )
    recorded = {}
    monkeypatch.setattr(
        server,
        "_record",
        lambda *_args, **_kwargs: recorded.update(
            during_record=server._application_authorization_context()
        ),
    )

    result = server._logged(
        "authorization_probe",
        {},
        lambda: {
            "subject": server._require_application_subject().subject,
            "actor": server._application_authorization_context().actor,
        },
    )

    assert result == {
        "subject": "person@example.com",
        "actor": "calliope@example.com",
    }
    assert recorded["during_record"].subject == "person@example.com"
    assert server._AUTHORIZATION_CONTEXT.get() is None


def test_activity_receipt_persists_actor_subject_and_delegation(monkeypatch):
    class Connection:
        def __init__(self):
            self.inserts = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if query.startswith("INSERT INTO"):
                self.inserts.append((query, params))
            return SimpleNamespace()

    authorization = server.ApplicationAuthorizationContext(
        actor="calliope@example.com",
        subject="person@example.com",
        attributed_subject="person@example.com",
        client_id=server._HERMES_SERVICE_CLIENT_ID,
        mode="google_chat_delegation",
        assurance="hermes_service_credential",
        delegated=True,
        platform="google_chat",
        session_ref="spaces/AAA/thread/123",
    )
    connection = Connection()
    monkeypatch.setattr(server, "_conn", lambda: connection)
    monkeypatch.setattr(
        server,
        "_caller",
        lambda: ("person@example.com", server._HERMES_SERVICE_CLIENT_ID),
    )
    monkeypatch.setattr(
        server,
        "_initial_activity_context",
        lambda *_args, **_kwargs: {
            "channel": "google_chat",
            "client_app": "hermes",
            "session_ref": "spaces/AAA/thread/123",
            "provenance": {"source": "hermes", "platform": "google_chat"},
        },
    )
    monkeypatch.setattr(
        server,
        "_resolve_activity_context",
        lambda _conn, context, caller, _client_id, **_kwargs: (caller, context),
    )
    token = server._AUTHORIZATION_CONTEXT.set(authorization)
    try:
        server._record("list_dashboards", {}, {"dashboards": []}, None, 4)
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)

    assert len(connection.inserts) == 1
    query, params = connection.inserts[0]
    assert "actor,subject,auth_mode,delegated" in query
    assert params[:6] == (
        "person@example.com",
        server._HERMES_SERVICE_CLIENT_ID,
        "calliope@example.com",
        "person@example.com",
        "google_chat_delegation",
        True,
    )
    audit = json.loads(params[9])["authorization"]
    assert audit["actor"] == "calliope@example.com"
    assert audit["subject"] == "person@example.com"
    assert audit["delegated"] is True
