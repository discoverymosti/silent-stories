"""
Connect the Facebook Page + Instagram account - run by the "Connect Facebook & Instagram" workflow.

Needs these GitHub secrets:
  META_APP_ID, META_APP_SECRET   your Meta app (developers.facebook.com)
  META_USER_TOKEN                a user token from the Graph API Explorer with
                                 pages_show_list, pages_read_engagement, pages_manage_posts,
                                 instagram_basic, instagram_content_publish, business_management
Optional variable META_PAGE_NAME picks the page when you manage several.

It swaps the short-lived token for a long-lived one, takes the page token (which does not
expire), finds the Instagram account linked to the page, and saves page id, page token and
Instagram id as meta_token.enc, encrypted with META_APP_SECRET. Afterwards META_USER_TOKEN
can be deleted.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
GRAPH = f"https://graph.facebook.com/{os.getenv('META_GRAPH_VERSION', 'v23.0')}"


def get(path: str, **params) -> dict:
    r = requests.get(f"{GRAPH}/{path}", params=params, timeout=30)
    if r.status_code >= 400:
        sys.exit(f"Meta API {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def main() -> int:
    app_id, secret = os.environ["META_APP_ID"], os.environ["META_APP_SECRET"]
    user = os.environ["META_USER_TOKEN"].strip()
    long_user = get("oauth/access_token", grant_type="fb_exchange_token", client_id=app_id,
                    client_secret=secret, fb_exchange_token=user)["access_token"]
    print(f"::add-mask::{long_user}")
    pages = get("me/accounts", access_token=long_user, fields="id,name,access_token").get("data", [])
    for p in pages:
        print(f"::add-mask::{p['access_token']}")
    if not pages:
        sys.exit("No Facebook Pages found. The token needs pages_show_list and you must be a Page admin.")
    wanted = os.getenv("META_PAGE_NAME", "").strip().lower()
    page = next((p for p in pages if p["name"].lower() == wanted), pages[0])
    print("Pages you manage: " + ", ".join(p["name"] for p in pages))
    ig = get(page["id"], fields="instagram_business_account{id,username}",
             access_token=page["access_token"]).get("instagram_business_account") or {}
    print(f"Connected Facebook Page: {page['name']} (id {page['id']})")
    print(f"Connected Instagram: @{ig['username']}" if ig else
          "No Instagram professional account is linked to this Page: Facebook only. "
          "Link one in Instagram -> Settings -> Account type and tools / Accounts Center.")
    data = json.dumps({"page_id": page["id"], "page_name": page["name"],
                       "page_token": page["access_token"], "ig_id": ig.get("id")}).encode()
    enc = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-a",
                          "-pass", "env:META_APP_SECRET"], input=data, capture_output=True, check=True).stdout
    (ROOT / "meta_token.enc").write_bytes(enc)
    print("Saved encrypted credentials to meta_token.enc. You can now delete the META_USER_TOKEN secret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
