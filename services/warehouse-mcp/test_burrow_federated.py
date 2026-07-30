#!/usr/bin/env python3
"""Burrow + Google, end to end through the REAL routes.

    python3 test_burrow_federated.py     (needs a reachable WAREHOUSE_DSN)

A mapped identity resolves to its Postgres role and lands in the app; an
unmapped one — verified by the IdP, unknown to the database — gets
rvbbit_guest, the access-pending page, and a row in the provisioning queue.
The rejections are the point: an unmapped viewer must not reach DataRabbit,
must not see artifact titles, and must not be handed a role named after
their email just because one could be spelled."""
import os, sys, time, json, threading, asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

os.environ.update(
    WAREHOUSE_PUBLIC_URL="http://localhost:8765",
    WAREHOUSE_JWT_SECRET="burrow-e2e-secret",
    WAREHOUSE_AUTH="pg",
    WAREHOUSE_GOOGLE_CLIENT_ID="e2e-client.apps.googleusercontent.com",
    WAREHOUSE_GOOGLE_CLIENT_SECRET="e2e-secret",
    WAREHOUSE_GOOGLE_HD="acme.com",
    WAREHOUSE_DSN="host=127.0.0.1 port=55433 dbname=bench user=postgres password=rvbbit",
    LENS_PUBLIC_URL="https://dr.example.com",
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIV = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                         serialization.NoEncryption())
PUB = key.public_key()

import auth, server
auth._JWKS_CLIENT = type("J", (), {"get_signing_key_from_jwt":
                                   lambda s, t: type("K", (), {"key": PUB})()})()
STATE = {"nonce": "", "email": ""}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        now = int(time.time())
        body = json.dumps({"id_token": jwt.encode(
            {"iss": "https://accounts.google.com", "aud": "e2e-client.apps.googleusercontent.com",
             "sub": "1", "email": STATE["email"], "email_verified": True, "hd": "acme.com",
             "nonce": STATE["nonce"], "iat": now, "exp": now + 600}, PRIV, algorithm="RS256")}).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a): pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
auth._GOOGLE_TOKEN_URI = f"http://127.0.0.1:{srv.server_port}/token"
app = server._build_mcp_oauth("http://localhost:8765").streamable_http_app()

import httpx
ok = []


async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost:8765", follow_redirects=False) as c:
        async def login(email):
            STATE["email"] = email
            r = await c.get("/auth/google/start", params={"next": "/"})
            q = parse_qs(urlparse(r.headers["location"]).query)
            STATE["nonce"] = q["nonce"][0]
            cb = await c.get("/auth/google/callback",
                             params={"state": q["state"][0], "code": "x"})
            cookie = ""
            for k, v in cb.headers.multi_items():
                if k.lower() == "set-cookie" and "wh_session" in v:
                    cookie = v.split(";")[0]
            return cb, cookie

        def check(label, cond, detail=""):
            ok.append(cond)
            print(f"  {'PASS' if cond else '*** FAIL ***':12} {label:44} {detail}")

        print("mapped identity (a PG role named after the email)")
        cb, cookie = await login("mapped@acme.com")
        who = await c.get("/auth/whoami", headers={"cookie": cookie})
        j = who.json()
        check("lands on / (into the app)", cb.headers.get("location") == "/", cb.headers.get("location"))
        check("executes as the mapped role", j.get("sub") == "mapped@acme.com", j.get("sub"))
        check("identity preserved for attribution", j.get("identity") == "mapped@acme.com")
        check("mapped = true", j.get("mapped") is True)
        idx = await c.get("/", headers={"cookie": cookie})
        check("index renders the gallery", 'class="card"' in idx.text, f"{idx.text.count('class=\"card\"')} cards")
        check("index offers DataRabbit", "Open DataRabbit" in idx.text)

        print("\nunmapped identity (verified by Google, unknown to Postgres)")
        cb, cookie = await login("stranger@acme.com")
        who = await c.get("/auth/whoami", headers={"cookie": cookie})
        j = who.json()
        check("redirected to /gallery, not the app", cb.headers.get("location") == "/gallery",
              cb.headers.get("location"))
        check("executes as rvbbit_guest", j.get("sub") == "rvbbit_guest", j.get("sub"))
        check("identity still recorded", j.get("identity") == "stranger@acme.com")
        check("mapped = false", j.get("mapped") is False)
        idx = await c.get("/gallery", headers={"cookie": cookie})
        check("shows access-pending, not artifacts", "Access pending" in idx.text
              and 'class="card"' not in idx.text)
        check("withholds the DataRabbit rung", "Open DataRabbit" not in idx.text)
        check("no artifact titles leak", "Bigfoot" not in idx.text)


asyncio.run(run())
import psycopg
with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn:
    rows = conn.execute("SELECT identity, attempts, via, role_now_exists "
                        "FROM rvbbit.identity_pending").fetchall()
print("\nprovisioning queue (rvbbit.identity_pending)")
for r in rows:
    print(f"    {r[0]:24} attempts={r[1]} via={r[2]} role_exists={r[3]}")
ok.append(any(r[0] == "stranger@acme.com" for r in rows))
print(f"  {'PASS' if ok[-1] else '*** FAIL ***':12} unmapped login queued for provisioning")

print(f"\n{sum(ok)}/{len(ok)} checks passed")
srv.shutdown()
sys.exit(0 if all(ok) else 1)
