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
import hmac
import html
import json
import os
import secrets
import sys
import time

import jwt
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

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
    if not LOGIN_PASSWORD:
        errs.append("WAREHOUSE_LOGIN_PASSWORD is required (else no one can log in).")
    return errs


def config_warnings() -> list[str]:
    w = []
    if LOGIN_PASSWORD and len(LOGIN_PASSWORD) < MIN_PASSWORD_LEN:
        w.append(f"WAREHOUSE_LOGIN_PASSWORD is short (<{MIN_PASSWORD_LEN} chars) — it's a shared, "
                 "internet-facing password; use a long random one.")
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
            return _AccessToken(token=token, client_id="static-key", scopes=[SCOPE], expires_at=None, email="static-key")
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


def _creds_ok(email: str, password: str) -> bool:
    good_pw = bool(LOGIN_PASSWORD) and hmac.compare_digest(password, LOGIN_PASSWORD)
    return bool(good_pw and _email_allowed(email) and email and "@" in email)


# ── browser view session (cookie, for /d/<slug> dashboards) ──────────────────

def set_session(resp, email: str, secure: bool) -> None:
    """Sign an email into the wh_session cookie (same JWT secret as the OAuth tokens)."""
    now = int(time.time())
    tok = jwt.encode({"sub": email, "typ": "session", "iat": now, "exp": now + SESSION_TTL},
                     JWT_SECRET, algorithm=JWT_ALG)
    resp.set_cookie(SESSION_COOKIE, tok, max_age=SESSION_TTL, httponly=True,
                    secure=secure, samesite="lax", path="/")


def read_session(request: Request) -> str | None:
    """The authenticated viewer email from the wh_session cookie, or None."""
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        return None
    try:
        c = jwt.decode(tok, JWT_SECRET, algorithms=[JWT_ALG])
        return c.get("sub") if c.get("typ") == "session" else None
    except Exception:   # noqa: BLE001
        return None


def _safe_next(nxt: str) -> str:
    """Open-redirect guard: only same-site absolute paths."""
    return nxt if (nxt.startswith("/") and not nxt.startswith("//")) else "/"


# ── login page ───────────────────────────────────────────────────────────────

def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>rvbbit warehouse</title>
<style>
 body{{background:#15110d;color:#f0e6d8;font:15px/1.5 ui-monospace,Menlo,monospace;display:grid;place-items:center;min-height:100vh;margin:0}}
 .card{{background:#1e1813;border:1px solid #3a2f24;border-radius:12px;padding:28px 30px;max-width:360px;width:90%;box-shadow:0 10px 40px #0008}}
 h1{{font-size:16px;margin:0 0 4px;color:#e8b572}} p.sub{{margin:0 0 18px;color:#a99}}
 label{{display:block;font-size:12px;color:#bba;margin:12px 0 4px}}
 input{{width:100%;box-sizing:border-box;background:#15110d;border:1px solid #4a3d2e;border-radius:7px;color:#f0e6d8;padding:9px 11px;font:inherit}}
 input:focus{{outline:none;border-color:#e8b572}}
 button{{width:100%;margin-top:20px;background:#e8b572;color:#1a1206;border:0;border-radius:7px;padding:10px;font:inherit;font-weight:600;cursor:pointer}}
 .err{{background:#3a1f1c;border:1px solid #6a3530;color:#f0b8b0;border-radius:7px;padding:8px 11px;font-size:13px;margin-top:14px}}
</style>
<div class=card>{body}</div>""", status_code=status)


def _login_form(hidden: dict, error: str | None = None,
                cta: str = "Sign in", sub: str = "Sign in to your warehouse.") -> HTMLResponse:
    err = f'<div class=err>{html.escape(error)}</div>' if error else ""
    fields = "".join(f'<input type=hidden name="{html.escape(k)}" value="{html.escape(str(v))}">'
                     for k, v in hidden.items() if v)
    return _page(
        f"""<h1>Data Warehouse</h1><p class=sub>{html.escape(sub)}</p>
<form method=post action=/login>
 {fields}
 <label>Email</label><input name=email type=email autocomplete=username autofocus required>
 <label>Access password</label><input name=password type=password autocomplete=current-password required>
 <button type=submit>{html.escape(cta)}</button>{err}
</form>""", status=401 if error else 200)


_EXPIRED = "<h1>Session expired</h1><p class=sub>Re-launch the connector from Claude to try again.</p>"


def register_login_route(mcp, provider: WarehouseAuthProvider):
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
                hidden = {"txn": txn} if txn else {"next": nxt}
                return _login_form(hidden, error="Invalid email or password.",
                                   cta="Authorize Claude" if txn else "Sign in")
            _LIMITER.record_success(ip)

        if txn:   # OAuth: mint the code, redirect back to Claude
            target = provider.complete_login(txn, email)
            return RedirectResponse(target, status_code=302) if target else _page(_EXPIRED, 400)
        # browser session: set the cookie, go where they were headed
        resp = RedirectResponse(nxt, status_code=302)
        set_session(resp, email, secure=request.url.scheme == "https")
        return resp

    return login
