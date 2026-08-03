"""
Self-contained OAuth 2.1 Authorization Server for the rvbbit Warehouse MCP.

The MCP SDK mounts the protocol routes (/authorize, /token, /register, and the
.well-known metadata) and verifies PKCE; this module supplies the pieces it
delegates to us: client/code/token storage, the login page, and signed access
tokens. Net effect: Claude Desktop / Cowork's native "Add custom connector" flow
just works (paste the URL → log in → allow), no shared-key header needed.

Identity model (Phase 1, self-contained — no external IdP):
  * A shared login password gates access (WAREHOUSE_LOGIN_PASSWORD).
  * The user states an email; an optional allowlist (WAREHOUSE_ALLOWED_EMAILS)
    restricts who may log in. The email rides in the access-token `sub` so tool
    calls / receipts can attribute the caller.
  * Backwards-compat: a static WAREHOUSE_MCP_KEY bearer still authenticates (for
    Claude Code's --header path), so both routes work side by side.

Known limits (hardening for later): the email is self-asserted (the shared
password is the real gate) — per-user passwords / magic-link / a real IdP fix
that; revocation is best-effort because access tokens are stateless JWTs (a
denylist would make it hard); storage is in-memory, so a restart forces re-login.
"""
from __future__ import annotations
# pydantic model generics + list invariance trip Pyright's strict checks; correct at runtime.
# pyright: reportArgumentType=false
import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import urlencode

import jwt
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

# ── config ───────────────────────────────────────────────────────────────────
LOGIN_PASSWORD = os.environ.get("WAREHOUSE_LOGIN_PASSWORD", "")
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("WAREHOUSE_ALLOWED_EMAILS", "").split(",") if e.strip()}
STATIC_KEY = os.environ.get("WAREHOUSE_MCP_KEY", "")        # legacy shared-key (Claude Code)
# A shared bearer has no person-shaped identity of its own.  Keep its auth
# mechanism visible as client_id="static-key", but let an installation name
# the long-lived service principal used by legacy Hermes/Calliope traffic.
# OAuth access tokens never use this fallback: their verified `sub` remains the
# caller.  The default preserves existing installations byte-for-byte.
STATIC_CALLER = os.environ.get("WAREHOUSE_MCP_STATIC_CALLER", "").strip().lower() or "static-key"
# The JWT signing secret MUST be independent of STATIC_KEY: that key is *handed to
# users* (it rides in their Authorization header, so it's in client configs, shell
# history, proxy logs). Reusing it to sign HS256 would let any key-holder forge a
# token for any email and bypass the login password + allowlist. No fallback —
# validate_config() refuses to start OAuth mode without an independent secret.
JWT_SECRET = os.environ.get("WAREHOUSE_JWT_SECRET", "")
JWT_ALG = "HS256"


def _env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


ACCESS_TTL = _env_int("WAREHOUSE_ACCESS_TTL", 3600, maximum=24 * 3600)            # 1h
REFRESH_TTL = _env_int("WAREHOUSE_REFRESH_TTL", 30 * 24 * 3600)                  # 30d
CODE_TTL = 300                                                                   # 5m
SCOPE = "warehouse"
MAX_PENDING = 2000        # caps on the unauthenticated in-memory OAuth state (DoS backstop)
MAX_CLIENTS = 1000
# Persist the durable OAuth state (registered clients + refresh tokens) so a restart
# doesn't strand every connector with "client_id not found". Put it on a volume.
STATE_FILE = os.environ.get("WAREHOUSE_STATE_FILE", "")   # empty = in-memory only
LOGIN_MAX_FAILS = 5       # per-IP failed logins ...
LOGIN_WINDOW = 300        # ... within this many seconds → lockout
MIN_PASSWORD_LEN = 12
SESSION_COOKIE = "wh_session"
SESSION_TTL = _env_int("WAREHOUSE_SESSION_TTL", 12 * 3600, maximum=24 * 3600)   # browser view session


def validate_config() -> list[str]:
    """FATAL OAuth-mode misconfigurations (empty list = ok). Called at serve time."""
    errs = []
    if not JWT_SECRET:
        errs.append("WAREHOUSE_JWT_SECRET is required and must be independent of WAREHOUSE_MCP_KEY.")
    elif STATIC_KEY and hmac.compare_digest(JWT_SECRET, STATIC_KEY):
        errs.append("WAREHOUSE_JWT_SECRET must differ from WAREHOUSE_MCP_KEY (no credential reuse).")
    if AUTH_MODE == "pg":
        pass   # Burrow: credentials are Postgres accounts; no shared password.
    elif not LOGIN_PASSWORD and not google_enabled():
        errs.append("WAREHOUSE_LOGIN_PASSWORD is required (else no one can log in).")
    # FAIL CLOSED on the one configuration that silently opens the door to the
    # whole internet. With a shared password, an empty allowlist still leaves
    # the password as a gate. With Google and no domain/allowlist there is NO
    # gate at all: any Google account on earth satisfies "signed in with
    # Google". Demand an explicit audience.
    if google_enabled() and not GOOGLE_HD and not ALLOWED_EMAILS:
        errs.append(
            "Google sign-in is configured with no audience restriction — set "
            "WAREHOUSE_GOOGLE_HD (a Workspace domain, verified against the signed "
            "hd claim) and/or WAREHOUSE_ALLOWED_EMAILS. Without one, ANY Google "
            "account can log in.")
    if GOOGLE_ONLY and not google_enabled():
        errs.append("WAREHOUSE_GOOGLE_ONLY is set but Google sign-in is not configured "
                    "(needs WAREHOUSE_GOOGLE_CLIENT_ID + WAREHOUSE_GOOGLE_CLIENT_SECRET).")
    return errs


def config_warnings() -> list[str]:
    w = []
    if LOGIN_PASSWORD and len(LOGIN_PASSWORD) < MIN_PASSWORD_LEN and not GOOGLE_ONLY:
        w.append(f"WAREHOUSE_LOGIN_PASSWORD is short (<{MIN_PASSWORD_LEN} chars) — it's a shared, "
                 "internet-facing password; use a long random one.")
    if google_enabled() and LOGIN_PASSWORD and not GOOGLE_ONLY:
        w.append("Google sign-in is on but the shared password still works. Once users "
                 "have moved, set WAREHOUSE_GOOGLE_ONLY=1 — a permanent fallback keeps "
                 "the weakest credential permanently.")
    if GOOGLE_CLIENT_ID and AUTH_MODE == "pg":
        w.append("Burrow + Google: verified identities resolve to a PG role via "
                 "rvbbit.resolve_identity (an identity_map row, or a role named after the "
                 "email). Anyone unresolved lands on rvbbit_guest — which holds NO grants "
                 "by default — and is queued in rvbbit.identity_pending for provisioning.")
    return w


class _AuthCode(AuthorizationCode):
    email: str        # carry the authenticated user from /login → token exchange


class _AccessToken(AccessToken):
    email: str | None = None


def make_auth_settings(public: str) -> AuthSettings:
    public = public.rstrip("/")
    return AuthSettings(
        issuer_url=AnyHttpUrl(public),
        resource_server_url=AnyHttpUrl(f"{public}/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        required_scopes=[],
    )


class WarehouseAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider (structural)."""

    def __init__(self, public: str):
        self.public = public.rstrip("/")
        self.audience = f"{self.public}/mcp"
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, tuple[str, AuthorizationParams, float]] = {}  # txn -> (client_id, params, created)
        self._codes: dict[str, _AuthCode] = {}
        self._refresh: dict[str, dict] = {}                            # rt -> {client_id, email, exp}
        self._load()

    # — persistence (durable: registered clients + refresh tokens) —
    def _load(self) -> None:
        if not STATE_FILE or not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            self._clients = {k: OAuthClientInformationFull.model_validate(v)
                             for k, v in data.get("clients", {}).items()}
            self._refresh = {k: v for k, v in data.get("refresh", {}).items() if v.get("exp", 0) > time.time()}
            print(f"loaded {len(self._clients)} clients / {len(self._refresh)} refresh tokens from {STATE_FILE}",
                  file=sys.stderr)
        except Exception as e:   # noqa: BLE001 — a bad/old state file must not break auth
            print(f"WARNING: could not load OAuth state from {STATE_FILE}: {e}", file=sys.stderr)

    def _persist(self) -> None:
        if not STATE_FILE:
            return
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            data = {"clients": {k: v.model_dump(mode="json") for k, v in self._clients.items()},
                    "refresh": self._refresh}
            tmp = f"{STATE_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, STATE_FILE)   # atomic
        except Exception as e:   # noqa: BLE001 — persistence is best-effort
            print(f"WARNING: could not persist OAuth state: {e}", file=sys.stderr)

    def _sweep(self) -> None:
        """Evict expired/abandoned state so the unauthenticated dicts can't grow without
        bound (open DCR + /authorize). Cheap; run opportunistically on writes."""
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v[2] < CODE_TTL}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}
        self._refresh = {k: v for k, v in self._refresh.items() if v["exp"] > now}

    @staticmethod
    def _cap(d: dict, limit: int) -> None:
        while len(d) >= limit:        # evict oldest (dicts preserve insertion order)
            d.pop(next(iter(d)))

    # — dynamic client registration —
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._cap(self._clients, MAX_CLIENTS)
        self._clients[client_info.client_id] = client_info
        self._persist()

    # — /authorize: hand the browser to our own login page —
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._sweep()
        self._cap(self._pending, MAX_PENDING)
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = (client.client_id, params, time.time())
        return f"{self.public}/login?txn={txn}"

    def has_pending(self, txn: str) -> bool:
        e = self._pending.get(txn)
        return bool(e) and (time.time() - e[2] < CODE_TTL)

    def complete_login(self, txn: str, email: str) -> str | None:
        """Called by POST /login after the user authenticates; mints the auth code and
        returns the client redirect URL (code + state), or None if the txn expired."""
        entry = self._pending.pop(txn, None)
        if entry is None:
            return None
        client_id, params, _ = entry
        code = secrets.token_urlsafe(32)   # > 160 bits of entropy (RFC 6749 §10.10)
        self._codes[code] = _AuthCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            email=email,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    # — code exchange (SDK has already validated PKCE + redirect_uri) —
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        c = self._codes.get(authorization_code)
        if not c or c.client_id != client.client_id:
            return None
        if c.expires_at < time.time():
            self._codes.pop(authorization_code, None)
            return None
        return c

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: _AuthCode) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)   # single-use
        return self._issue(client.client_id, authorization_code.email, list(authorization_code.scopes))

    # — refresh —
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        r = self._refresh.get(refresh_token)
        if not r or r["client_id"] != client.client_id or r["exp"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=[SCOPE], expires_at=int(r["exp"]))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        r = self._refresh.pop(refresh_token.token, None)   # rotate (drop the old one)
        if not r or r["exp"] < time.time():
            raise TokenError("invalid_grant", "refresh token is unknown or expired")
        return self._issue(client.client_id, r["email"], scopes or [SCOPE])

    # — access-token validation, called on every /mcp request —
    async def load_access_token(self, token: str):
        if STATIC_KEY and hmac.compare_digest(token, STATIC_KEY):
            return _AccessToken(token=token, client_id="static-key", scopes=[SCOPE],
                                expires_at=None, email=STATIC_CALLER)
        try:
            claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG], audience=self.audience, issuer=self.public)
        except Exception:   # noqa: BLE001 — any decode/signature/expiry failure → unauthenticated
            return None
        scopes = (claims.get("scope") or "").split() or [SCOPE]
        return _AccessToken(token=token, client_id=claims.get("client_id", "?"),
                            scopes=scopes, expires_at=claims.get("exp"), email=claims.get("sub"))

    async def revoke_token(self, token) -> None:
        # best-effort: drop refresh tokens (access JWTs are stateless and expire).
        self._refresh.pop(getattr(token, "token", token), None)
        self._persist()

    # — helpers —
    def _issue(self, client_id: str, email: str, scopes: list[str]) -> OAuthToken:
        self._sweep()                 # purge expired refresh tokens before adding one
        self._cap(self._refresh, 100_000)
        now = int(time.time())
        access = jwt.encode(
            {"iss": self.public, "sub": email, "aud": self.audience, "client_id": client_id,
             "scope": " ".join(scopes), "iat": now, "exp": now + ACCESS_TTL},
            JWT_SECRET, algorithm=JWT_ALG)
        rt = secrets.token_urlsafe(32)
        self._refresh[rt] = {"client_id": client_id, "email": email, "exp": time.time() + REFRESH_TTL}
        self._persist()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=rt)


# ── login rate limiting ──────────────────────────────────────────────────────

class _RateLimiter:
    """Per-IP failed-attempt lockout + a global lock that serializes credential checks,
    so parallel guesses can't bypass the per-attempt cost (a bare async sleep can't)."""

    def __init__(self):
        self._fails: dict[str, list[float]] = {}
        self.lock = asyncio.Lock()

    def blocked(self, key: str) -> bool:
        now = time.time()
        hits = [t for t in self._fails.get(key, []) if now - t < LOGIN_WINDOW]
        if hits:
            self._fails[key] = hits
        else:
            self._fails.pop(key, None)
        return len(hits) >= LOGIN_MAX_FAILS

    def record_fail(self, key: str) -> None:
        self._fails.setdefault(key, []).append(time.time())
        if len(self._fails) > 10_000:   # bound the limiter's own map
            now = time.time()
            self._fails = {k: v for k, v in self._fails.items()
                           if any(now - t < LOGIN_WINDOW for t in v)}

    def record_success(self, key: str) -> None:
        self._fails.pop(key, None)


_LIMITER = _RateLimiter()


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")   # set by the fronting proxy
    return (xff.split(",")[0].strip() if xff else "") or (request.client.host if request.client else "?")


def _email_allowed(email: str) -> bool:
    """WAREHOUSE_ALLOWED_EMAILS entries match exactly, OR — if an entry begins with '@' — any address in
    that domain (e.g. '@acceleratedacademy.us' allows everyone @acceleratedacademy.us with the shared
    password). Empty allowlist = any email. (Interim domain gate; per-user identity comes later.)"""
    e = (email or "").strip().lower()
    if not ALLOWED_EMAILS:
        return True
    return any(e == a or (a.startswith("@") and e.endswith(a)) for a in ALLOWED_EMAILS)


def _creds_ok_shared(email: str, password: str) -> bool:
    good_pw = bool(LOGIN_PASSWORD) and hmac.compare_digest(password, LOGIN_PASSWORD)
    return bool(good_pw and _email_allowed(email) and email and "@" in email)


# ── Postgres-as-IdP (Burrow mode, docs/BURROW_PLAN.md) ──────────────────────
# WAREHOUSE_AUTH=pg: credentials ARE a Postgres role + password. We verify by
# attempting a real connection as that role (scram), then exchange it for our
# own JWTs on the spot — the PG password is never stored or re-sent. The
# subject becomes the ROLE NAME, and downstream surfaces run SET LOCAL ROLE
# under it, so the DBA's GRANTs/RLS are the app's permission system.
# Optional gate: WAREHOUSE_PG_LOGIN_ROLE (default rvbbit_users) — when that
# role exists, only its members may log in; manage the allowlist with GRANT.
AUTH_MODE = os.environ.get("WAREHOUSE_AUTH", "shared").strip().lower()
PG_LOGIN_ROLE = os.environ.get("WAREHOUSE_PG_LOGIN_ROLE", "rvbbit_users")
# lens = the login PAGE is rendered by lens on the unified origin (POSTs land
# here; failures PRG back to /login?err=1). Default keeps the built-in form.
LOGIN_UI = os.environ.get("WAREHOUSE_LOGIN_UI", "builtin").strip().lower()
# Role names may be EMAIL-SHAPED: Azure Entra and Cloud SQL IAM both name the
# database role after the principal, so "ryan@acme.com" is a legitimate role
# and federated login maps to it with no mapping table at all. Both SET ROLE
# call sites quote and double embedded quotes; excluding '"' here is the belt
# to that's suspenders. 63 bytes is Postgres's identifier limit — anything
# longer gets silently TRUNCATED by the server, so it must never get this far.
_ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$@.\-]{0,62}$")
# Authenticated by the IdP, unknown to the database. Holds no grants by design
# (migration 0221) — surfaces show a request-access state, not a broken one.
GUEST_ROLE = "rvbbit_guest"
# Where a viewer the database can't place gets sent. /gallery is the artifact
# index — it exists on every install and, unlike DataRabbit, degrades honestly
# when the session has no grants.
UNMAPPED_LANDING = "/gallery"


def _creds_ok_pg(username: str, password: str) -> bool:
    if not _ROLE_NAME_RE.fullmatch(username or "") or not password:
        return False
    import psycopg
    from psycopg import conninfo
    base = os.environ.get(
        "WAREHOUSE_DSN", "host=localhost port=55433 dbname=bench user=postgres password=rvbbit")
    try:
        dsn = conninfo.make_conninfo(base, user=username, password=password)
        with psycopg.connect(dsn, connect_timeout=5) as c:
            row = c.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) "
                "OR pg_has_role(current_user, %s, 'member')",
                (PG_LOGIN_ROLE, PG_LOGIN_ROLE)).fetchone()
            return bool(row and row[0])
    except Exception:   # noqa: BLE001 — bad creds and unreachable DB read the same: no.
        return False


def _creds_ok(email: str, password: str) -> bool:
    if AUTH_MODE == "pg":
        return _creds_ok_pg(email, password)
    # GOOGLE_ONLY has to be enforced HERE, not just by hiding the form: the
    # login page is a courtesy, POST /login is the door. Anyone can still post
    # to it directly, so a retired password must actually stop working.
    if GOOGLE_ONLY and google_enabled():
        return False
    return _creds_ok_shared(email, password)


# ── Google Sign-In (federated IdP) ───────────────────────────────────────────
# We are already an OAuth *server* (to Claude); this additionally makes us an
# OAuth *client* to Google. The two never meet in _creds_ok() — that's a
# form-post credential check and Google is a redirect round-trip. They meet at
# _finish_login(), so a Google-verified email reaches the access token's `sub`
# (and therefore mcp_activity.caller) by exactly the same path a typed one does.
#
# This is a REAL identity upgrade, not just convenience: in shared mode the
# email is self-asserted and the password is the only gate, so any key-holder
# can own any name in the audit log. Google's email_verified claim ends that.
#
# Deliberately NOT wired into AUTH_MODE=pg (Burrow): there the session subject
# IS a Postgres role name (surfaces run SET LOCAL ROLE under it, and
# _ROLE_NAME_RE rejects '@' and '.'), so an email cannot be the subject.
# Federating Burrow needs an email->role mapping — its own design pass.
GOOGLE_CLIENT_ID = os.environ.get("WAREHOUSE_GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("WAREHOUSE_GOOGLE_CLIENT_SECRET", "").strip()
# Workspace domain gate, e.g. "acme.com". Google treats `hd` on the AUTHORIZATION
# REQUEST as an account-chooser hint only — the user can edit it out of the URL —
# so it is NEVER a security boundary there. We send it for the UX and verify the
# signed `hd` CLAIM on the returned id_token, which is the actual gate.
GOOGLE_HD = os.environ.get("WAREHOUSE_GOOGLE_HD", "").strip().lower()
# Retire the shared password once everyone has moved over. A fallback that lives
# forever means the weakest credential lives forever.
GOOGLE_ONLY = os.environ.get("WAREHOUSE_GOOGLE_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}

_GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def google_enabled() -> bool:
    """Available in BOTH modes. In shared mode the verified email is the whole
    identity; in Burrow it is resolved to a Postgres role (rvbbit.resolve_identity,
    migration 0221) so the IdP proves WHO you are and Postgres still decides WHAT
    you may touch."""
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def resolve_identity(identity: str, via: str) -> str | None:
    """Burrow: verified email -> PG role, or None when the database has no
    account for them (they land on rvbbit_guest and get queued for
    provisioning). Failing closed on any error is deliberate: an unreachable
    database must not silently promote someone to a role."""
    import psycopg
    dsn = os.environ.get(
        "WAREHOUSE_DSN", "host=localhost port=55433 dbname=bench user=postgres password=rvbbit")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as c:
            row = c.execute("SELECT rvbbit.resolve_identity(%s, %s)", (identity, via)).fetchone()
            return row[0] if row and row[0] else None
    except Exception as e:   # noqa: BLE001
        print(f"resolve_identity({identity}): {e}", file=sys.stderr)
        return None


def session_subject(identity: str, via: str) -> tuple[str, bool]:
    """(what we execute as, whether the database knows them).

    Shared mode has no roles, so the identity IS the subject and 'mapped' is
    vacuously true. Burrow resolves: a password login already proved
    possession of the role itself; a federated login has to be looked up.
    """
    if AUTH_MODE != "pg":
        return identity, True
    if via == "pg":
        return identity, True          # they authenticated AS the role
    role = resolve_identity(identity, via)
    return (role, True) if role else (GUEST_ROLE, False)


def google_redirect_uri(public: str) -> str:
    """Must match a registered redirect URI in the Google Cloud console EXACTLY."""
    return f"{public.rstrip('/')}/auth/google/callback"


class _GoogleFlows:
    """Pending Google round-trips: state -> what to resume when the user returns.

    Same discipline as the OAuth `_pending` map (TTL'd, capped, swept on write)
    because these entries are created by UNAUTHENTICATED requests. `state` is
    generated and stored server-side and consumed single-use — the txn is never
    round-tripped through the browser as something we'd trust on the way back.
    """

    def __init__(self):
        self._m: dict[str, tuple[str, str, str, float]] = {}   # state -> (txn, next, nonce, created)

    def begin(self, txn: str, nxt: str) -> tuple[str, str]:
        self._sweep()
        while len(self._m) >= MAX_PENDING:
            self._m.pop(next(iter(self._m)))       # evict oldest
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
        self._m[state] = (txn, nxt, nonce, time.time())
        return state, nonce

    def take(self, state: str) -> tuple[str, str, str] | None:
        entry = self._m.pop(state, None)           # single-use: a replay finds nothing
        if not entry or time.time() - entry[3] > CODE_TTL:
            return None
        return entry[0], entry[1], entry[2]

    def _sweep(self) -> None:
        now = time.time()
        self._m = {k: v for k, v in self._m.items() if now - v[3] < CODE_TTL}


_GFLOWS = _GoogleFlows()


class _GoogleCalendarFlows:
    """Single-use state for an authenticated, incremental Calendar grant.

    The owner is captured from the signed Warehouse session before leaving for
    Google, then compared with the verified Google email on return.  This keeps
    a second Google account in the browser from attaching its calendar to the
    first user's private Calliope context.
    """

    def __init__(self):
        self._m: dict[str, tuple[str, str, str, float]] = {}

    def begin(self, owner: str, nxt: str) -> tuple[str, str]:
        self._sweep()
        while len(self._m) >= MAX_PENDING:
            self._m.pop(next(iter(self._m)))
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
        self._m[state] = (owner, nxt, nonce, time.time())
        return state, nonce

    def take(self, state: str) -> tuple[str, str, str] | None:
        entry = self._m.pop(state, None)
        if not entry or time.time() - entry[3] > CODE_TTL:
            return None
        return entry[0], entry[1], entry[2]

    def _sweep(self) -> None:
        now = time.time()
        self._m = {key: value for key, value in self._m.items() if now - value[3] < CODE_TTL}


_GCAL_FLOWS = _GoogleCalendarFlows()
_JWKS_CLIENT = None


def _google_jwks():
    global _JWKS_CLIENT
    if _JWKS_CLIENT is None:
        from jwt import PyJWKClient
        _JWKS_CLIENT = PyJWKClient(_GOOGLE_JWKS_URI)   # caches keys across logins
    return _JWKS_CLIENT


def verify_google_id_token(id_token: str, nonce: str) -> dict:
    """VERIFY the token, never merely decode it: RS256 signature against Google's
    published JWKS, audience == our client id, issuer, expiry, and the nonce we
    planted. Raises on anything less. Blocking (may fetch JWKS) — call it off
    the event loop."""
    key = _google_jwks().get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(
        id_token, key, algorithms=["RS256"], audience=GOOGLE_CLIENT_ID,
        options={"require": ["exp", "iat", "aud", "iss", "sub"]})
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise ValueError("unexpected issuer")
    if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise ValueError("nonce mismatch")          # replayed or injected callback
    if not claims.get("email"):
        raise ValueError("no email claim")
    if claims.get("email_verified") not in (True, "true"):
        raise ValueError("Google has not verified this address")
    return claims


def google_domain_ok(claims: dict) -> bool:
    """The real domain gate: the SIGNED hd claim, not the request parameter.
    A consumer Google account cannot present an `hd`, so this is strictly
    stronger than matching the email's suffix."""
    if not GOOGLE_HD:
        return True
    return (claims.get("hd") or "").strip().lower() == GOOGLE_HD


# ── browser view session (cookie, for /d/<slug> dashboards) ──────────────────

def set_session(resp, sub: str, secure: bool, identity: str | None = None,
                mapped: bool = True, via: str = "password") -> None:
    """Sign the session into wh_session (same JWT secret as the OAuth tokens).

    `sub` keeps its established meaning — what surfaces SET ROLE as — so every
    existing reader is unchanged. `idt` carries the HUMAN behind it, which in
    Burrow is no longer the same string: ryan@acme.com may execute as role
    `ryan`, or as rvbbit_guest when the database doesn't know them yet.
    """
    now = int(time.time())
    tok = jwt.encode({"sub": sub, "idt": identity or sub, "mpd": bool(mapped), "via": via,
                      "typ": "session", "iat": now, "exp": now + SESSION_TTL},
                     JWT_SECRET, algorithm=JWT_ALG)
    resp.set_cookie(SESSION_COOKIE, tok, max_age=SESSION_TTL, httponly=True,
                    secure=secure, samesite="lax", path="/")


def read_session_full(request: Request) -> dict | None:
    """{sub, identity, mapped, via} or None. Sessions minted before 4.2.1 carry
    only `sub`; they read back as mapped, which is what they were."""
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        return None
    try:
        c = jwt.decode(tok, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:   # noqa: BLE001
        return None
    if c.get("typ") != "session" or not c.get("sub"):
        return None
    return {"sub": c["sub"], "identity": c.get("idt") or c["sub"],
            "mapped": c.get("mpd", True), "via": c.get("via", "password")}


def read_session(request: Request) -> str | None:
    """The subject surfaces execute as (a PG role in Burrow), or None."""
    s = read_session_full(request)
    return s["sub"] if s else None


def _safe_next(nxt: str) -> str:
    """Open-redirect guard: only same-site absolute paths."""
    return nxt if (nxt.startswith("/") and not nxt.startswith("//")) else "/"


def _finish_login(request: Request, provider, identity: str, txn: str, nxt: str,
                  via: str = "password"):
    """THE one place a verified identity becomes a session.

    Every authentication path — typed password, Postgres role, Google — lands
    here, so the identity reaches the OAuth token and the browser cookie
    identically no matter how it was proven. This is also where federation
    resolves: in Burrow the IdP has told us WHO, and Postgres now decides what
    role that person executes as (or rvbbit_guest, if it has never heard of
    them). Two exits: an OAuth txn continues back to Claude with a fresh code;
    anything else gets the session cookie and goes where it was headed.
    """
    sub, mapped = session_subject(identity, via)
    if txn:
        target = provider.complete_login(txn, sub)
        return RedirectResponse(target, status_code=302) if target else _page(_EXPIRED, 400)
    # An unmapped viewer has nothing to do inside DataRabbit — every query
    # there would fail — so send them to the artifact index regardless of
    # where they were headed. The index shows them how to ask for access.
    if not mapped:
        nxt = UNMAPPED_LANDING
    resp = RedirectResponse(nxt, status_code=302)
    set_session(resp, sub, secure=request.url.scheme == "https",
                identity=identity, mapped=mapped, via=via)
    return resp


# ── backgrounds ──────────────────────────────────────────────────────────────
# Scenes lifted from the DataRabbit wallpaper set (rvbbit-lens
# public/wallpapers/4k), downscaled for the web. They're chosen to already sit
# in the warm near-black palette, so they blend rather than fight the chrome.
# Authenticated shells pass the viewer identity as a stable scene key so a
# Gallery -> Calliope navigation never swaps the room under their feet.  The
# unauthenticated login page still gets a fresh scene on each visit.
#
# Lives here rather than in server.py because the LOGIN page needs it before
# any session exists, and auth.py is the module server.py imports (not the
# other way round). The serving route is deliberately unauthenticated: it's the
# backdrop of the sign-in page, so gating it would be circular.
BACKGROUNDS = ("the_flooded_core", "dead_zone", "the_black_site",
               "fallout_bunker", "deep_sea_vessel")
_BG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backgrounds")


def pick_background(scene_key: str | None = None) -> str:
    """Choose a stable per-viewer scene, or a fresh one when no viewer exists."""
    if scene_key:
        digest = hashlib.sha256(scene_key.strip().casefold().encode("utf-8")).digest()
        return BACKGROUNDS[int.from_bytes(digest[:8], "big") % len(BACKGROUNDS)]
    return secrets.choice(BACKGROUNDS)


def background_layer(image_opacity: float, veil: str, scene_key: str | None = None) -> str:
    """The shared desktop backdrop: photo plus the scrim that pushes it back.

    The named wrapper lets cross-document view transitions keep this layer in
    place while page chrome/content enters independently.  Callers still set
    how loud it is — the login page can afford atmosphere, an index full of
    content cannot.
    """
    bg = pick_background(scene_key)
    return ('<div class="warehouse-desktop-background" aria-hidden="true" '
            f'data-warehouse-background-url="/bg/{bg}.jpg" '
            f'data-warehouse-background-opacity="{image_opacity}">'
            f'<div class=bg style="background-image:url(/bg/{bg}.jpg);opacity:{image_opacity}"></div>'
            f'<div class=veil style="background:{veil}"></div>'
            '</div>')


_BG_CSS = """
 .bg{position:fixed;inset:0;z-index:-2;background-position:center;background-size:cover;
   background-repeat:no-repeat;filter:saturate(.75) contrast(1.04);pointer-events:none}
 .veil{position:fixed;inset:0;z-index:-1;pointer-events:none}
"""


# ── login page ───────────────────────────────────────────────────────────────

_LOGIN_RABBIT_SVG = ""


def _page(body: str, status: int = 200) -> HTMLResponse:
    import warehouse_theme
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=color-scheme content="dark light">
<title>rvbbit warehouse</title>
<style>
 /* Base colour on <html>, body transparent — a negative-z-index layer paints
    BEFORE in-flow backgrounds, so an opaque body would hide .bg entirely.
    (This page only ever worked because it had no html background and body's
    propagated to the canvas; don't leave that to luck.) */
 html{{background:#15110d}}
 body{{background:transparent;color:#f0e6d8;font:15px/1.5 ui-monospace,Menlo,monospace;display:grid;place-items:center;min-height:100vh;margin:0;padding-top:56px;box-sizing:border-box}}
{_BG_CSS}
 .loginbar{{position:fixed;z-index:20;inset:0 0 auto;height:56px;display:flex;align-items:center;
   padding:0 max(20px,4vw);border-bottom:1px solid #3a2f24;background:rgba(21,17,13,.82);
   backdrop-filter:blur(18px);box-sizing:border-box}}
 .loginbrand{{display:flex;align-items:center;color:#f0e6d8;font:700 12px/1 ui-monospace,Menlo,monospace;
   letter-spacing:.14em;text-decoration:none}}
 .loginbrand .mark{{display:block;width:auto;height:15px;margin-right:12px;flex:none;
   color:var(--amber,#e8b572)}}
 .loginbrand small{{margin-left:10px;padding-left:10px;border-left:1px solid #3a2f24;
   color:#8a8078;font-size:9px;font-weight:400;letter-spacing:.16em}}
 .loginbar [data-warehouse-theme-anchor]{{margin-left:auto}}
 .card{{box-sizing:border-box;background:rgba(30,24,19,.86);backdrop-filter:blur(3px);border:1px solid #3a2f24;border-radius:12px;padding:28px 30px;max-width:360px;width:90%;box-shadow:0 10px 40px #000a}}
 h1{{font-size:16px;margin:0 0 4px;color:#e8b572}} p.sub{{margin:0 0 18px;color:#a99}}
 label{{display:block;font-size:12px;color:#bba;margin:12px 0 4px}}
 input{{width:100%;box-sizing:border-box;background:#15110d;border:1px solid #4a3d2e;border-radius:7px;color:#f0e6d8;padding:9px 11px;font:inherit}}
 input:focus{{outline:none;border-color:#e8b572}}
 button{{width:100%;margin-top:20px;background:#e8b572;color:#1a1206;border:0;border-radius:7px;padding:10px;font:inherit;font-weight:600;cursor:pointer}}
 .err{{background:#3a1f1c;border:1px solid #6a3530;color:#f0b8b0;border-radius:7px;padding:8px 11px;font-size:13px;margin-top:14px}}
 .goog{{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;margin-top:14px;
   background:#f0e6d8;color:#1a1206;border-radius:7px;padding:10px;font:inherit;font-weight:600;
   text-decoration:none;box-sizing:border-box}}
 .goog:hover{{background:#fff}}
 .goog svg{{width:17px;height:17px;display:block}}
 .or{{display:flex;align-items:center;gap:10px;margin:18px 0 4px;color:#8a8078;font-size:11px}}
 .or::before,.or::after{{content:"";flex:1;height:1px;background:#3a2f24}}
</style>
{warehouse_theme.head_assets()}
</head><body>
{background_layer(0.62, "radial-gradient(1000px 700px at 50% 45%, rgba(21,17,13,.34) 0%, rgba(21,17,13,.70) 55%, rgba(21,17,13,.93) 100%)")}
<nav class=loginbar data-warehouse-header>
 <span class=loginbrand>{_LOGIN_RABBIT_SVG}DATA RABBIT<small>WAREHOUSE</small></span>
 <span data-warehouse-theme-anchor></span>
</nav>
<div class=card>{body}</div>
</body></html>""", status_code=status)


# Google's mark, inlined — the login page must render before any external host
# is reachable (and a CDN <img> here would leak every login to a third party).
_G_SVG = ('<svg viewBox="0 0 48 48" aria-hidden="true">'
          '<path fill="#4285F4" d="M45.1 24.5c0-1.6-.1-2.8-.4-4H24v7.3h12.1c-.2 2-1.6 5-4.5 7l-.1.3 6.6 5 .4.1c4.2-3.9 6.6-9.6 6.6-15.7"/>'
          '<path fill="#34A853" d="M24 46c6 0 11-2 14.6-5.4l-7-5.4c-1.8 1.3-4.3 2.2-7.6 2.2-5.8 0-10.7-3.8-12.5-9.1l-.3.1-6.8 5.3-.1.3C8 41.1 15.4 46 24 46"/>'
          '<path fill="#FBBC05" d="M11.5 28.3c-.5-1.4-.8-2.9-.8-4.3s.3-3 .7-4.3v-.4l-6.9-5.4-.2.1C2.8 16.8 2 20.3 2 24s.8 7.2 2.3 10.3z"/>'
          '<path fill="#EB4335" d="M24 9.9c4.1 0 6.9 1.8 8.5 3.3l6.2-6C34.9 3.7 30 1.5 24 1.5 15.4 1.5 8 6.4 4.3 13.6l7.1 5.5C13.3 13.8 18.2 9.9 24 9.9"/>'
          '</svg>')


def _google_button(hidden: dict) -> str:
    """A link, not a form post: /auth/google/start owns the redirect. Carrying
    txn/next through means a Google login resumes whatever flow sent us here."""
    if not google_enabled():
        return ""
    q = urlencode({k: str(v) for k, v in hidden.items() if v})
    href = f"/auth/google/start?{q}" if q else "/auth/google/start"
    return f'<a class=goog href="{html.escape(href)}">{_G_SVG}Sign in with Google</a>'


def _login_form(hidden: dict, error: str | None = None,
                cta: str = "Sign in", sub: str = "Sign in to your warehouse.") -> HTMLResponse:
    err = f'<div class=err>{html.escape(error)}</div>' if error else ""
    fields = "".join(f'<input type=hidden name="{html.escape(k)}" value="{html.escape(str(v))}">'
                     for k, v in hidden.items() if v)
    goog = _google_button(hidden)
    head = f'<h1>Data Warehouse</h1><p class=sub>{html.escape(sub)}</p>'
    # Password retired on this box: Google is the only door, so don't render a
    # form that cannot succeed.
    if GOOGLE_ONLY and google_enabled():
        return _page(f"{head}{goog}{err}", status=401 if error else 200)
    # Burrow (pg) mode: credentials are a Postgres role, not an email.
    ident = ('<label>Username</label><input name=email autocomplete=username autofocus required>'
             if AUTH_MODE == "pg" else
             '<label>Email</label><input name=email type=email autocomplete=username autofocus required>')
    pw_label = "Password" if AUTH_MODE == "pg" else "Access password"
    return _page(
        f"""{head}{goog}{'<div class=or>or</div>' if goog else ''}
<form method=post action=/login>
 {fields}
 {ident}
 <label>{pw_label}</label><input name=password type=password autocomplete=current-password required>
 <button type=submit>{html.escape(cta)}</button>{err}
</form>""", status=401 if error else 200)


_EXPIRED = "<h1>Session expired</h1><p class=sub>Re-launch the connector from Claude to try again.</p>"


def register_login_route(
    mcp,
    provider: WarehouseAuthProvider,
    rabbit_svg: str = "",
    google_calendar_grant=None,
):
    global _LOGIN_RABBIT_SVG
    _LOGIN_RABBIT_SVG = str(rabbit_svg or "")

    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request):
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            if txn:   # OAuth (Claude) flow
                return _login_form({"txn": txn}, cta="Authorize Claude",
                                   sub="Sign in to connect Claude to your warehouse.") \
                    if provider.has_pending(txn) else _page(_EXPIRED, 400)
            # browser view session (a dashboard sent us here with ?next=)
            nxt = _safe_next(request.query_params.get("next", "/"))
            return _login_form({"next": nxt}, cta="Sign in", sub="Sign in to view your dashboards.")

        form = await request.form()
        txn = str(form.get("txn", ""))
        nxt = _safe_next(str(form.get("next", "/")))
        email = str(form.get("email", "")).strip().lower()
        password = str(form.get("password", ""))
        ip = _client_ip(request)
        if _LIMITER.blocked(ip):
            return _page("<h1>Too many attempts</h1><p class=sub>Wait a few minutes, then try again.</p>", 429)
        if txn and not provider.has_pending(txn):
            return _page(_EXPIRED, 400)

        # Serialize credential checks: parallel guesses queue on this lock, so the
        # per-attempt cost (and the per-IP counter) actually rate-limits brute force.
        async with _LIMITER.lock:
            if not _creds_ok(email, password):
                _LIMITER.record_fail(ip)
                await asyncio.sleep(1.0)
                # Lens-rendered login (BURROW_PLAN §5 P4): fail via PRG back to
                # the pretty page — the ingress routes GET /login to lens, so an
                # inline form here would break out of the branded flow.
                if LOGIN_UI == "lens":
                    q = f"err=1&txn={txn}" if txn else f"err=1&next={nxt}"
                    return RedirectResponse(f"/login?{q}", status_code=303)
                hidden = {"txn": txn} if txn else {"next": nxt}
                return _login_form(hidden, error="Invalid email or password.",
                                   cta="Authorize Claude" if txn else "Sign in")
            _LIMITER.record_success(ip)

        return _finish_login(request, provider, email, txn, nxt,
                             via="pg" if AUTH_MODE == "pg" else "password")

    def _bounce(txn: str, nxt: str, msg: str):
        """Send a failed Google attempt back to the login page. PRG when lens
        renders it (an inline form here would break out of the branded flow)."""
        if LOGIN_UI == "lens":
            q = f"err=1&txn={txn}" if txn else f"err=1&next={nxt}"
            return RedirectResponse(f"/login?{q}", status_code=303)
        return _login_form({"txn": txn} if txn else {"next": nxt}, error=msg,
                           cta="Authorize Claude" if txn else "Sign in")

    def _calendar_return(nxt: str, result: str):
        target = _safe_next(nxt or "/calliope")
        separator = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{separator}{urlencode({'calendar': result})}",
            status_code=303,
        )

    @mcp.custom_route("/auth/google/start", methods=["GET"])
    async def google_start(request: Request):
        if not google_enabled():
            return _page("<h1>Google sign-in is not configured</h1>", 404)
        txn = request.query_params.get("txn", "")
        nxt = _safe_next(request.query_params.get("next", "/"))
        if txn and not provider.has_pending(txn):
            return _page(_EXPIRED, 400)
        state, nonce = _GFLOWS.begin(txn, nxt)
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": google_redirect_uri(provider.public),
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "prompt": "select_account",
        }
        if GOOGLE_HD:
            params["hd"] = GOOGLE_HD     # chooser hint ONLY — the claim check below is the gate
        return RedirectResponse(f"{_GOOGLE_AUTH_URI}?{urlencode(params)}", status_code=302)

    # This route does not exist at all unless Calliope supplied a grant sink.
    # Non-Google and non-Calliope installations therefore expose no Calendar
    # affordance or half-configured endpoint.
    if google_calendar_grant is not None:
        @mcp.custom_route("/auth/google/calendar/start", methods=["GET"])
        async def google_calendar_start(request: Request):
            if not google_enabled():
                return _page("<h1>Google Calendar is not configured</h1>", 404)
            session = read_session_full(request)
            if not session:
                nxt = _safe_next(request.query_params.get("next", "/calliope"))
                return RedirectResponse(
                    f"/login?{urlencode({'next': nxt})}", status_code=302
                )
            if not session.get("mapped", True):
                return _page("<h1>Your account is still awaiting access</h1>", 403)
            owner = str(session.get("identity") or "").strip().lower()
            if not owner:
                return _page("<h1>Your signed-in identity is unavailable</h1>", 401)
            nxt = _safe_next(request.query_params.get("next", "/calliope"))
            state, nonce = _GCAL_FLOWS.begin(owner, nxt)
            params = {
                "client_id": GOOGLE_CLIENT_ID,
                "redirect_uri": google_redirect_uri(provider.public),
                "response_type": "code",
                "scope": (
                    "openid email "
                    "https://www.googleapis.com/auth/calendar.events.owned.readonly"
                ),
                "state": state,
                "nonce": nonce,
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "login_hint": owner,
            }
            if GOOGLE_HD:
                params["hd"] = GOOGLE_HD
            return RedirectResponse(
                f"{_GOOGLE_AUTH_URI}?{urlencode(params)}", status_code=302
            )

    @mcp.custom_route("/auth/google/callback", methods=["GET"])
    async def google_callback(request: Request):
        if not google_enabled():
            return _page("<h1>Google sign-in is not configured</h1>", 404)
        state_value = request.query_params.get("state", "")
        calendar_entry = (
            _GCAL_FLOWS.take(state_value) if google_calendar_grant is not None else None
        )
        entry = None if calendar_entry else _GFLOWS.take(state_value)
        if not entry and not calendar_entry:
            # Unknown/expired/replayed state. We have no trustworthy txn or
            # next to resume, so stop here rather than guess.
            return _page("<h1>Sign-in expired</h1>"
                         "<p class=sub>Start again from the login page.</p>", 400)
        calendar_owner = calendar_entry[0] if calendar_entry else ""
        if calendar_entry:
            _, nxt, nonce = calendar_entry
            txn = ""
        else:
            txn, nxt, nonce = entry
        if txn and not provider.has_pending(txn):
            return _page(_EXPIRED, 400)
        if request.query_params.get("error"):
            if calendar_entry:
                return _calendar_return(nxt, "cancelled")
            return _bounce(txn, nxt, "Google sign-in was cancelled.")
        code = request.query_params.get("code", "")
        if not code:
            if calendar_entry:
                return _calendar_return(nxt, "error")
            return _bounce(txn, nxt, "Google sign-in returned no authorization code.")

        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as cli:
                r = await cli.post(_GOOGLE_TOKEN_URI, data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": google_redirect_uri(provider.public),
                    "grant_type": "authorization_code",
                })
            r.raise_for_status()
            token_payload = r.json() or {}
            id_token = token_payload.get("id_token")
            if not id_token:
                raise ValueError("no id_token in Google's token response")
            # Verification fetches/caches JWKS and does RSA work — keep it off
            # the event loop.
            claims = await asyncio.to_thread(verify_google_id_token, id_token, nonce)
        except Exception as e:   # noqa: BLE001 — any failure is a failed login, never a partial one
            print(f"google callback: {type(e).__name__}: {e}", file=sys.stderr)
            if calendar_entry:
                return _calendar_return(nxt, "error")
            return _bounce(txn, nxt, "Could not verify your Google sign-in.")

        email = str(claims.get("email", "")).strip().lower()
        ip = _client_ip(request)
        if not google_domain_ok(claims):
            _LIMITER.record_fail(ip)
            if calendar_entry:
                return _calendar_return(nxt, "account_mismatch")
            return _bounce(txn, nxt, f"That account is not in the {GOOGLE_HD} organization.")
        if not _email_allowed(email):
            _LIMITER.record_fail(ip)
            if calendar_entry:
                return _calendar_return(nxt, "account_mismatch")
            return _bounce(txn, nxt, "That account is not allowed to sign in here.")
        if calendar_entry:
            if not hmac.compare_digest(email, calendar_owner):
                _LIMITER.record_fail(ip)
                return _calendar_return(nxt, "account_mismatch")
            try:
                await google_calendar_grant(calendar_owner, token_payload)
            except Exception as e:  # noqa: BLE001 — grant is all-or-nothing
                print(
                    f"google calendar grant: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return _calendar_return(nxt, "error")
            _LIMITER.record_success(ip)
            return _calendar_return(nxt, "connected")
        _LIMITER.record_success(ip)
        # via="google" matters: it is what tells _finish_login this identity was
        # proven by the IdP and still needs resolving to a Postgres role. A
        # password login in pg mode proved possession of the role itself and
        # skips resolution — conflating the two would hand every federated user
        # a role named after their email whether or not it exists.
        return _finish_login(request, provider, email, txn, nxt, via="google")

    # Session introspection for sibling services (lens gates on this in
    # Burrow mode — no JWT-secret sharing, just a cookie round-trip on the
    # unified origin).
    @mcp.custom_route("/auth/whoami", methods=["GET"])
    async def whoami(request: Request):
        s = read_session_full(request)
        sub = s["sub"] if s else None
        # pg mode: the subject must BE a role name. A session minted under a
        # previous auth mode (an email sub) is stale here — treat it as
        # signed out so every surface converges on re-login.
        if sub and AUTH_MODE == "pg" and not _ROLE_NAME_RE.fullmatch(sub):
            sub = None
        if not sub or not s:
            return HTMLResponse('{"ok":false}', status_code=401, media_type="application/json")
        # `sub` keeps its meaning (what to SET ROLE as) so existing readers are
        # untouched; `identity` is the human, and `mapped` says whether the
        # database actually knows them or they are riding the guest role.
        return HTMLResponse(json.dumps({"ok": True, "sub": sub, "mode": AUTH_MODE,
                                        "identity": s["identity"], "mapped": s["mapped"],
                                        "via": s["via"]}),
                            media_type="application/json")

    @mcp.custom_route("/bg/{name}.jpg", methods=["GET"])
    async def background(request: Request):
        """Backdrop art. Unauthenticated on purpose — it's the wallpaper behind
        the SIGN-IN page, so gating it on a session would be circular. The name
        is matched against a fixed tuple rather than sanitised into a path, so
        this can never be coaxed into reading anything else off the disk."""
        name = request.path_params["name"]
        if name not in BACKGROUNDS:
            return HTMLResponse("not found", status_code=404)
        try:
            data = open(os.path.join(_BG_DIR, f"{name}.jpg"), "rb").read()
        except OSError:
            return HTMLResponse("not found", status_code=404)
        return Response(data, media_type="image/jpeg",
                        headers={"cache-control": "public, max-age=604800, immutable"})

    @mcp.custom_route("/auth/config", methods=["GET"])
    async def auth_config(request: Request):   # noqa: ARG001
        """What the login page should render. Unauthenticated by necessity (it
        IS the pre-login state) and deliberately says nothing secret — which
        buttons to draw, never the client id or secret. Lets a sibling-rendered
        login page (WAREHOUSE_LOGIN_UI=lens) stay in sync with this server's
        config instead of duplicating env vars across two repos."""
        return HTMLResponse(json.dumps({
            "mode": AUTH_MODE,
            "google": google_enabled(),
            "password": not (GOOGLE_ONLY and google_enabled()),
        }), media_type="application/json", headers={"cache-control": "no-store"})

    @mcp.custom_route("/auth/logout", methods=["GET", "POST"])
    async def logout(request: Request):   # noqa: ARG001
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    return login
