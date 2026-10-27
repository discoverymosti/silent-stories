"""
Cross-post the video that was just uploaded to YouTube.

- Instagram: the Short as a Reel (caption + hashtags)
- Facebook Page: the Short as a Page Reel, a weekly film as a Page video
- X: a post with title, hashtags and the YouTube link

Runs after pipeline.py. Only posts when the YouTube upload is public (YT_PRIVACY=public),
so private review uploads never leak elsewhere. Each platform is optional: it is skipped
when its credentials are missing, and a failure on one never fails the run.

Credentials:
  Meta: meta_token.enc, made by the "Connect Facebook & Instagram" workflow (connect_meta.py),
        decrypted with the META_APP_SECRET secret.
  X:    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET secrets (app with Read and Write).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
GRAPH = f"https://graph.facebook.com/{os.getenv('META_GRAPH_VERSION', 'v23.0')}"
RUPLOAD = "https://rupload.facebook.com"


def caption(meta: dict, limit: int) -> str:
    text = (f"{meta['title']}\n\n{meta.get('social_caption') or ' '.join(meta['hashtags'])}\n\n"
            f"▶ Full video on YouTube: {meta['youtube_url']}")
    return text[:limit]


def meta_credentials() -> dict | None:
    enc = ROOT / "meta_token.enc"
    if not (enc.exists() and os.getenv("META_APP_SECRET")):
        return None
    out = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-a",
                          "-pass", "env:META_APP_SECRET"], input=enc.read_bytes(),
                         capture_output=True, check=True).stdout
    return json.loads(out)


def _ok(r: requests.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise RuntimeError(f"{what} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def _rupload(url: str, token: str, video: Path) -> None:
    data = video.read_bytes()
    _ok(requests.post(url, data=data, timeout=600, headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))}), "upload")


def instagram_reel(cred: dict, video: Path, text: str) -> str:
    ig, token = cred["ig_id"], cred["page_token"]
    c = _ok(requests.post(f"{GRAPH}/{ig}/media", timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": text,
        "share_to_feed": "true", "access_token": token}), "IG container")
    _rupload(c.get("uri") or f"{RUPLOAD}/ig-api-upload/{GRAPH.rsplit('/', 1)[-1]}/{c['id']}", token, video)
    for _ in range(60):  # Instagram transcodes before it can publish
        st = _ok(requests.get(f"{GRAPH}/{c['id']}", timeout=30, params={
            "fields": "status_code,status", "access_token": token}), "IG status")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"IG processing failed: {st.get('status')}")
        time.sleep(10)
    else:
        raise RuntimeError("IG processing timed out")
    pub = _ok(requests.post(f"{GRAPH}/{ig}/media_publish", timeout=60, data={
        "creation_id": c["id"], "access_token": token}), "IG publish")
    return pub["id"]


def facebook_reel(cred: dict, video: Path, text: str) -> str:
    page, token = cred["page_id"], cred["page_token"]
    start = _ok(requests.post(f"{GRAPH}/{page}/video_reels", timeout=60, data={
        "upload_phase": "start", "access_token": token}), "FB reel start")
    vid = start["video_id"]
    _rupload(start.get("upload_url") or f"{RUPLOAD}/video-upload/{GRAPH.rsplit('/', 1)[-1]}/{vid}", token, video)
    _ok(requests.post(f"{GRAPH}/{page}/video_reels", timeout=60, data={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
        "description": text, "access_token": token}), "FB reel finish")
    return vid


def facebook_video(cred: dict, video: Path, title: str, text: str) -> str:
    page, token = cred["page_id"], cred["page_token"]
    with video.open("rb") as f:
        r = requests.post(f"https://graph-video.facebook.com/{GRAPH.rsplit('/', 1)[-1]}/{page}/videos",
                          timeout=900, files={"source": (video.name, f, "video/mp4")},
                          data={"title": title, "description": text, "access_token": token})
    return _ok(r, "FB video")["id"]


def x_post(meta: dict) -> str:
    from requests_oauthlib import OAuth1
    auth = OAuth1(os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
                  os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_SECRET"])
    tags = " ".join(meta["hashtags"][:3])
    room = 280 - 24 - len(tags) - 4  # a link always counts as 23 characters on X
    text = f"{meta['title'][:room]}\n\n{meta['youtube_url']}\n{tags}"
    r = requests.post("https://api.x.com/2/tweets", json={"text": text}, auth=auth, timeout=30)
    return _ok(r, "X post")["data"]["id"]


def main() -> int:
    meta_file = OUT / "metadata.json"
    if not meta_file.exists():
        print("No metadata.json - nothing to cross-post.")
        return 0
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    if not meta.get("youtube_url"):
        print("Video was not uploaded to YouTube - nothing to cross-post.")
        return 0
    if os.getenv("YT_PRIVACY", "private") != "public" and os.getenv("CROSSPOST_PRIVATE") != "true":
        print("YouTube upload is private (review first) - skipping cross-posting.")
        return 0
    film = meta.get("format") == "film"
    video = OUT / "short.mp4"
    text = caption(meta, 2200)

    jobs = []
    cred = None
    try:
        cred = meta_credentials()
    except Exception as e:  # noqa: BLE001
        print(f"Meta credentials unreadable: {e}")
    if cred and cred.get("ig_id") and not film:  # Reels are vertical; films stay on YouTube/Facebook
        jobs.append(("Instagram Reel", lambda: instagram_reel(cred, video, text)))
    if cred and cred.get("page_id"):
        jobs.append(("Facebook Page " + ("video" if film else "Reel"),
                     (lambda: facebook_video(cred, video, meta["title"], text)) if film
                     else (lambda: facebook_reel(cred, video, text))))
    if all(os.getenv(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")):
        jobs.append(("X post", lambda: x_post(meta)))
    if not jobs:
        print("No social accounts connected - skipping cross-posting.")
    for name, job in jobs:
        try:
            print(f"{name}: posted ({job()})")
        except Exception as e:  # noqa: BLE001 - one platform failing must not stop the others
            print(f"{name}: FAILED - {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
