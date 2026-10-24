"""
Connect your YouTube channel - run ONCE on your own computer.

Before running:
  1. https://console.cloud.google.com -> New project "silent-stories"
  2. APIs & Services -> Library -> enable "YouTube Data API v3"
  3. OAuth consent screen -> External -> app name "Silent Stories" ->
     add your Gmail under Test users -> then click "PUBLISH APP"
     (in Testing mode the token dies after 7 days)
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app
     -> Download JSON -> save it next to this file as client_secret.json
  5. pip install google-auth-oauthlib

Then:  python scripts/connect_youtube.py
A browser opens: pick the Google account that owns the channel, choose the
channel (brand account) if asked, click Continue past the "unverified app" warning.
"""
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
secret = Path(__file__).with_name("client_secret.json")
if not secret.exists():
    sys.exit(f"Missing {secret}. Follow the steps at the top of this file.")

flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

from googleapiclient.discovery import build  # noqa: E402
yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
ch = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
name = ch[0]["snippet"]["title"] if ch else "(no channel found on this account!)"

cfg = json.loads(secret.read_text())
cfg = cfg.get("installed") or cfg.get("web")
print(f"\nConnected channel: {name}\n")
print("Add these at GitHub -> repo -> Settings -> Secrets and variables -> Actions:\n")
print(f"  YT_CLIENT_ID      {cfg['client_id']}")
print(f"  YT_CLIENT_SECRET  {cfg['client_secret']}")
print(f"  YT_REFRESH_TOKEN  {creds.refresh_token}")
print("\nOr with GitHub CLI (from the repo folder):")
print(f"  gh secret set YT_CLIENT_ID -b \"{cfg['client_id']}\"")
print(f"  gh secret set YT_CLIENT_SECRET -b \"{cfg['client_secret']}\"")
print(f"  gh secret set YT_REFRESH_TOKEN -b \"{creds.refresh_token}\"")
