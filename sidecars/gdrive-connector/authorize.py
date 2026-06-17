"""One-time OAuth helper — mint an `authorized_user` credential the gdrive connector reads.

Why this exists: Google now blocks the Drive scope on gcloud's shared default client, so ADC
(`gcloud auth application-default login --scopes=…drive…`) fails. You must use your OWN OAuth
client. This script does the consent flow with your client and writes the refresh-token JSON the
connector consumes via GDRIVE_SA_KEY — no gcloud, no cloud-platform scope, no admin.

Setup (any Google account; no Workspace admin):
  1. GCP Console → APIs & Services → Enable "Google Drive API".
  2. → OAuth consent screen → External → add yourself as a Test user → add scope .../auth/drive.readonly.
  3. → Credentials → Create OAuth client ID → "Desktop app" → download client_secret.json.

Run it (easiest on your LAPTOP, which has a browser):
  pip install google-auth-oauthlib
  python authorize.py client_secret.json gdrive_user.json
  # browser opens → consent → writes gdrive_user.json

Then copy the output to the server and point the connector at it (the token is portable):
  scp gdrive_user.json  server:/secrets/gdrive_user.json
  # connector env:  GDRIVE_SA_KEY=/secrets/gdrive_user.json

Headless box (no local browser): run on the box with --headless, SSH in with
`-L 8765:localhost:8765`, and open the printed URL in your laptop's browser:
  python authorize.py client_secret.json gdrive_user.json --headless

Note: a "Testing" consent screen expires the refresh token after ~7 days — fine for testing;
publish the consent screen (or switch to a service account) for the ongoing nightly job.
"""
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    headless = "--headless" in sys.argv
    if len(args) < 2:
        print("usage: python authorize.py <client_secret.json> <out_authorized_user.json> [--headless]")
        sys.exit(1)
    client_file, out_file = args[0], args[1]

    flow = InstalledAppFlow.from_client_secrets_file(client_file, scopes=SCOPES)
    # Fixed port so a headless box can be reached via `ssh -L 8765:localhost:8765`.
    creds = flow.run_local_server(port=8765, open_browser=not headless)
    # creds.to_json() omits "type"; stamp it so the file is a proper authorized_user credential.
    import json
    data = json.loads(creds.to_json())
    data["type"] = "authorized_user"
    with open(out_file, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"\nwrote {out_file} — set the connector's GDRIVE_SA_KEY to this file (or its JSON).")


if __name__ == "__main__":
    main()
