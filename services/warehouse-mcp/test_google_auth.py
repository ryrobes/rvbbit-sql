#!/usr/bin/env python3
"""Regression tests for the Google Sign-In gate. No DB, no network, no pytest:

    python3 test_google_auth.py

Signs real RS256 tokens with a throwaway key and points the JWKS lookup at it,
so every rejection path is exercised for real rather than stubbed. The point of
this file is the REJECTIONS — a test that only proved the happy path would let a
forged or wrong-audience token through unnoticed.
"""
from __future__ import annotations
# pyright: reportMissingImports=false
import importlib
import os
import sys
import time

BASE_ENV = dict(
    WAREHOUSE_GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com",
    WAREHOUSE_GOOGLE_CLIENT_SECRET="test-secret",
    WAREHOUSE_GOOGLE_HD="acme.com",
    WAREHOUSE_GOOGLE_ONLY="",
    WAREHOUSE_JWT_SECRET="unit-test-jwt-secret",
    WAREHOUSE_LOGIN_PASSWORD="unit-test-password-long",
    WAREHOUSE_ALLOWED_EMAILS="",
    WAREHOUSE_AUTH="shared",
)
os.environ.update(BASE_ENV)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jwt                                                    # noqa: E402
from cryptography.hazmat.primitives import serialization      # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa     # noqa: E402

import auth                                                   # noqa: E402

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIV = _key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
PUB = _key.public_key()
NONCE = "the-nonce-we-planted"
results: list[bool] = []


class _FakeJWKS:
    def get_signing_key_from_jwt(self, _token):
        return type("K", (), {"key": PUB})()


auth._JWKS_CLIENT = _FakeJWKS()


def token(**over) -> str:
    now = int(time.time())
    c = {"iss": "https://accounts.google.com",
         "aud": "test-client.apps.googleusercontent.com", "sub": "1234567890",
         "email": "ryan@acme.com", "email_verified": True, "hd": "acme.com",
         "nonce": NONCE, "iat": now, "exp": now + 3600}
    c.update(over)
    for k in [k for k, v in c.items() if v is None]:
        del c[k]
    return jwt.encode(c, PRIV, algorithm="RS256")


def check(label: str, tok: str, nonce: str = NONCE, want_ok: bool = False) -> None:
    try:
        claims = auth.verify_google_id_token(tok, nonce)
        ok, why = True, "verified"
        if not auth.google_domain_ok(claims):
            ok, why = False, "rejected by hd gate"
    except Exception as e:   # noqa: BLE001
        ok, why = False, f"{type(e).__name__}: {str(e)[:56]}"
    results.append(ok == want_ok)
    print(f"  {'PASS' if ok == want_ok else '*** FAIL ***':12} {label:40} -> {why}")


def cfg_check(label: str, want_err: bool, **env) -> None:
    os.environ.update({**BASE_ENV, "WAREHOUSE_GOOGLE_HD": "", **env})
    importlib.reload(auth)
    errs = auth.validate_config()
    got = any("ANY Google account" in e or "GOOGLE_ONLY" in e for e in errs)
    results.append(got == want_err)
    print(f"  {'PASS' if got == want_err else '*** FAIL ***':12} {label:40} -> "
          f"{'refused to start' if got else 'accepted'}")


print("id_token verification + domain gate")
check("valid Workspace token", token(), want_ok=True)
check("audience of a DIFFERENT app", token(aud="other.apps.googleusercontent.com"))
check("wrong issuer", token(iss="https://evil.example.com"))
check("expired", token(exp=int(time.time()) - 10, iat=int(time.time()) - 3600))
check("nonce mismatch (replay/injection)", token(), nonce="different")
check("nonce missing from token", token(nonce=None))
check("email_verified = false", token(email_verified=False))
check("no email claim", token(email=None))
check("consumer gmail (no hd)", token(email="a@gmail.com", hd=None))
check("hd of a different workspace", token(email="x@evil.com", hd="evil.com"))
check("email acme.com but hd evil.com", token(hd="evil.com"))
check("alg=none forgery", jwt.encode({"email": "x@acme.com"}, "", algorithm="none"))

print("\nfail-closed configuration")
cfg_check("google + no hd + no allowlist", True)
cfg_check("google + hd", False, WAREHOUSE_GOOGLE_HD="acme.com")
cfg_check("google + allowlist", False, WAREHOUSE_ALLOWED_EMAILS="@acme.com")
cfg_check("GOOGLE_ONLY without google configured", True,
          WAREHOUSE_GOOGLE_CLIENT_ID="", WAREHOUSE_GOOGLE_CLIENT_SECRET="",
          WAREHOUSE_GOOGLE_ONLY="1")
cfg_check("google ignored under burrow/pg", False,
          WAREHOUSE_AUTH="pg", WAREHOUSE_GOOGLE_HD="acme.com")

print("\nGOOGLE_ONLY actually closes the password door")
os.environ.update({**BASE_ENV, "WAREHOUSE_GOOGLE_ONLY": "1"})
importlib.reload(auth)
retired = not auth._creds_ok("ryan@acme.com", "unit-test-password-long")
results.append(retired)
print(f"  {'PASS' if retired else '*** FAIL ***':12} "
      f"{'valid password refused when retired':40} -> "
      f"{'refused' if retired else 'STILL ACCEPTED'}")
os.environ.update({**BASE_ENV, "WAREHOUSE_GOOGLE_ONLY": ""})
importlib.reload(auth)
still = auth._creds_ok("ryan@acme.com", "unit-test-password-long")
results.append(still)
print(f"  {'PASS' if still else '*** FAIL ***':12} "
      f"{'password still works when not retired':40} -> "
      f"{'accepted' if still else 'BROKEN'}")

print("\nsingle-use state store")
flows = auth._GoogleFlows()
state, nonce = flows.begin("txn-1", "/gallery")
first, second = flows.take(state), flows.take(state)
for label, ok in (("state resolves to (txn, next, nonce)", first == ("txn-1", "/gallery", nonce)),
                  ("replayed state is consumed", second is None)):
    results.append(ok)
    print(f"  {'PASS' if ok else '*** FAIL ***':12} {label:40}")

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
