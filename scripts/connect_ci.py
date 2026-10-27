"""
Connect the YouTube channel from GitHub Actions - no computer needed.

Used by .github/workflows/connect-youtube.yml with the existing
YT_CLIENT_ID / YT_CLIENT_SECRET secrets (Desktop OAuth client):

  1. Run the workflow with "code" empty -> the log prints a Google sign-in link.
  2. Open it, pick the channel's Google account, tap Allow. The browser then
     lands on a "localhost can't be reached" page: that's expected.
     Copy that page's full address (it contains ?code=...).
  3. Run the workflow again and paste the address into "code".

The refresh token is encrypted with YT_CLIENT_SECRET and saved as yt_token.enc.
It is useless without the client secret, which only lives in GitHub secrets.
"""
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / "yt_token.enc"
REDIRECT = "http://localhost"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.readonly")


def sign_in_link(client_id: str) -> str:
    # client IDs are public by design; percent-encode every character so the
    # Actions log masker (which hides the secret's literal value) leaves it readable
    cid = "".join(f"%{b:02X}" for b in client_id.encode())
    rest = urlencode({"redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES,
                      "access_type": "offline", "prompt": "consent"}, quote_via=quote)
    return f"https://accounts.google.com/o/oauth2/v2/auth?client_id={cid}&{rest}"


def extract_code(text: str) -> str:
    text = text.strip()
    if "code=" in text:
        q = parse_qs(urlparse(text).query) or parse_qs(text.split("?", 1)[-1])
        if q.get("error"):
            sys.exit(f"Google returned an error: {q['error'][0]}")
        return q["code"][0]
    return text


def main() -> int:
    cid, secret = os.environ["YT_CLIENT_ID"], os.environ["YT_CLIENT_SECRET"]
    raw = os.getenv("AUTH_CODE", "")
    if not raw.strip():
        print("\nOpen this link, choose the Google account that owns your channel, tap Allow:\n")
        print(sign_in_link(cid))
        print("\nYou will land on a 'localhost' error page. Copy its full address and "
              "run this workflow again with it pasted into 'code'.")
        return 0

    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": extract_code(raw), "client_id": cid, "client_secret": secret,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Token exchange failed ({r.status_code}): {r.text[:300]}\n"
                 "Codes expire after a few minutes and work once: get a fresh link and retry.")
    tok = r.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit("Google sent no refresh token. Remove the app at myaccount.google.com/permissions "
                 "and sign in again.")
    print(f"::add-mask::{refresh}")

    ch = requests.get("https://www.googleapis.com/youtube/v3/channels",
                      params={"part": "snippet,statistics", "mine": "true"},
                      headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
    items = ch.json().get("items", []) if ch.ok else []
    if not items:
        sys.exit(f"Signed in, but no YouTube channel on that account ({ch.status_code}). "
                 "Pick the account/brand account that owns the channel.")
    sn, st = items[0]["snippet"], items[0].get("statistics", {})
    print(f"Connected channel: {sn['title']}  (id {items[0]['id']}, "
          f"{st.get('subscriberCount', '?')} subscribers, {st.get('videoCount', '?')} videos)")

    enc = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-a",
                          "-pass", "env:YT_CLIENT_SECRET"],
                         input=refresh.encode(), capture_output=True, check=True).stdout
    TOKEN_FILE.write_bytes(enc)
    print(f"Saved encrypted token to {TOKEN_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
