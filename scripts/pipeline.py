"""
Silent Stories -> YouTube pipeline (fully automatic, copyright-safe):
daily vertical Shorts, plus a weekly widescreen short film with VIDEO_FORMAT=film.

Narrated in the channel owner's own voice (NARRATION: cloned from voice/sample.*, or recorded per
video; "off" = the old silent videos). Original AI cartoon / animation visuals (VISUAL_STYLE, no real or photorealistic
people) + animated captions + original music, so it works in every language and contains no
third-party copyrighted material.

1. Take the next idea from stories.txt
2. Gemini (or Claude) writes the storyboard + SEO/SMO metadata
3. Each scene gets an ORIGINAL AI image (Gemini image -> Cloudflare FLUX -> Pollinations),
   or stock footage (Pixabay/Pexels) if VISUAL_MODE=stock or AI fails
4. Auto-edit: camera moves with a gentle float, snappy transitions, bright cartoon look, pop-in captions,
   end card, synthesized music (original, no copyright)
5. AI thumbnail with title text
6. Upload to YouTube as a Short with SEO title/description/hashtags/tags + thumbnail
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
# VIDEO_FORMAT=short: daily vertical Short (~50 s) from stories.txt
# VIDEO_FORMAT=film:  weekly widescreen short film (~3 min) from films.txt
FORMAT = os.getenv("VIDEO_FORMAT", "short").lower()
FILM = FORMAT == "film"
# VIDEO_FORMAT=teaser: right after the weekly film is up, a vertical teaser Short cut from the
# film's own pictures (new hook and captions, links to the film): one more upload, no new images
TEASER = FORMAT == "teaser"
# CHANNEL=tech: the second channel, Discovery Mosti TECH (silent tech explainers). It keeps its own
# idea queue, upload log and YouTube connection in channels/tech/; the main channel uses the repo root.
CHANNEL = (os.getenv("CHANNEL") or "main").strip().lower()
TECH = CHANNEL == "tech"
CH_DIR = ROOT if CHANNEL == "main" else ROOT / "channels" / CHANNEL
STORIES = CH_DIR / ("films.txt" if FILM else "stories.txt")
DONE = CH_DIR / "done.txt"
MUSIC_DIR = ROOT / "music"
OUT = ROOT / "out"

W, H, FPS = (1920, 1080, 30) if FILM else (1080, 1920, 30)
XF = 0.5  # crossfade seconds
FONT = os.getenv("CAPTION_FONT", "Noto Sans")
FONTS_DIR = os.getenv("FONTS_DIR", "")  # extra fonts folder (static ffmpeg can't rely on system fontconfig)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-opus-5-5"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")  # empty = auto-pick newest
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "")  # empty = auto-pick newest
VISUAL_MODE = os.getenv("VISUAL_MODE", "ai").lower()  # ai | stock
PRIVACY = os.getenv("YT_PRIVACY") or "public"  # set the YT_PRIVACY variable to "private" to review first
SKIP_UPLOAD = os.getenv("SKIP_UPLOAD", "false").lower() == "true"
CHANNEL_TAG = os.getenv("CHANNEL_HASHTAG", "#DiscoveryMosti")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "DiscoveryMosti")
CAMERA_MOVES = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up")
# YouTube shows viewers the title/description in their language; the video itself has no speech
LANGS = [c.strip() for c in os.getenv("YT_LANGUAGES", "es,pt,hi,id,ar,fr,de,ja").split(",") if c.strip()]
# refuse to upload if more than this many scenes ended up as plain gradient filler
MAX_FALLBACK = int(os.getenv("MAX_FALLBACK_SCENES", "4" if FILM else "2"))
WATERMARK = os.getenv("WATERMARK", CHANNEL_NAME)
# AI tier: which models may be used.
#   free  - never call anything that can bill.
#   paid  - also use the paid models (Claude writes the stories, Gemini draws the pictures)
#           wherever their key / billing is set up; the free models remain the fallback.
#   auto  - (default) free until the channel is monetized and earned at least PAID_MIN_REVENUE_USD
#           in the last 28 days (YouTube Analytics), then paid by itself, and free again if it stops.
# Paid use never goes above PAID_BUDGET_USD per calendar month (estimated cost logged in spend.txt).
# FREE_ONLY=true is a kill switch that forces free; FREE_ONLY=false (older setting) means paid.
_FREE_ONLY = os.getenv("FREE_ONLY", "").strip().lower()
AI_TIER = "free" if _FREE_ONLY == "true" else (
    os.getenv("AI_TIER") or ("paid" if _FREE_ONLY == "false" else "auto")).strip().lower()
PAID_MIN_REVENUE = float(os.getenv("PAID_MIN_REVENUE_USD") or 20)
PAID_BUDGET = float(os.getenv("PAID_BUDGET_USD") or 10)
SPEND = ROOT / "spend.txt"
PAID = False  # decided once per run by decide_tier()
# $ per million input / output tokens, for the spend log (an unknown model is costed as the dearest)
CLAUDE_PRICES = {"claude-fable-5-1": (10.0, 50.0), "claude-opus-5-5": (4.0, 20.0),
                 "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
CLAUDE_FALLBACK_OK = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
GEMINI_IMAGE_USD = float(os.getenv("GEMINI_IMAGE_USD") or 0.07)  # cautious estimate per Gemini image

# Narration in the channel owner's own voice (YouTube's reused-content policy: original commentary).
#   clone    - (default) fully automatic: every video's narration is spoken by AI in YOUR voice, cloned
#              from one clip you provide once as voice/sample.<mp3|m4a|wav...> (scripts/clone_voice.py).
#              No sample, or the voice can't be made: no video is published (never a silent one).
#   record   - the pipeline writes a narration script for the next videos into voice/<story>/script.md,
#              you record each one and upload the audio file into the same folder; a video is published
#              only once its recording is there.
#   optional - like record, but without a recording the video goes out silent.
#   off      - the old silent videos.
NARRATION = (os.getenv("NARRATION") or "clone").strip().lower()
if NARRATION not in ("clone", "record", "optional"):
    NARRATION = "off"
VOICED = NARRATION != "off" and not TEASER
RECORDED = VOICED and NARRATION != "clone"
NARRATION_LANGUAGE = (os.getenv("NARRATION_LANGUAGE") or "English").strip()
VOICE_DIR = CH_DIR / "voice"
VOICE_AHEAD = max(1, int(os.getenv("VOICE_AHEAD") or 3))  # scripts kept ready, so several can be recorded in one go
VOICE_LEAD = 0.3  # seconds of picture before the first word
WORDS_PER_SECOND = 2.4  # calm storytelling pace

NO_TEXT = ("No text, no letters, no captions, no logos, no watermark, no brand names, "
           "no real celebrities, no copyrighted characters.")

# One animated look for the whole channel: every video is a cartoon, never real or photorealistic people.
# VISUAL_STYLE = one of the names below, your own style description, or "real" for the old live-action look.
STYLES = {
    "cartoon": "3D animated cartoon, stylized characters with big expressive eyes and simple rounded shapes, "
               "bold readable silhouettes, vibrant saturated colours, soft warm lighting, "
               "polished animated family film look",
    "2d": "2D cartoon illustration, bold clean outlines, flat cel shading, bright saturated colours, "
          "simple expressive characters, hand-drawn animated series look",
    "anime": "anime illustration, clean line art, cel shading, large expressive eyes, "
             "richly painted backgrounds, animated film look",
    "storybook": "children's storybook illustration, soft watercolour and gouache textures, "
                 "gentle rounded characters, warm cosy palette",
    "clay": "stop-motion claymation, handmade clay characters with simple faces, miniature handcrafted sets, "
            "soft studio lighting",
    "comic": "comic book illustration, bold ink outlines, halftone shading, dynamic poses, punchy flat colours",
}
VISUAL_STYLE = " ".join((os.getenv("VISUAL_STYLE") or "cartoon").split())
ANIMATED = VISUAL_STYLE.lower() != "real"
STYLE = STYLES.get(VISUAL_STYLE.lower(), VISUAL_STYLE) if ANIMATED else ""
TOON_ONLY = ("Fully animated artwork: every person, animal and object is drawn as a stylized cartoon character, "
             "nothing looks like a photo or live-action footage.")


# ================================================================ queue

def next_story() -> tuple[str, str] | None:
    for line in STORIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        genre, _, idea = line.partition("|")
        if not idea:
            genre, idea = "story", genre
        return genre.strip().lower(), idea.strip()
    return None


def queue() -> list[tuple[str, str]]:
    """Every (genre, idea) waiting in the queue, top first."""
    out = []
    for line in STORIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        genre, _, idea = line.partition("|")
        if not idea:
            genre, idea = "story", genre
        out.append((genre.strip().lower(), idea.strip()))
    return out


def mark_done(idea: str, video_id: str | None) -> None:
    kept = [l for l in STORIES.read_text(encoding="utf-8").splitlines() if idea not in l]
    STORIES.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
    with DONE.open("a", encoding="utf-8") as f:
        base = "https://youtu.be/" if FILM else "https://youtube.com/shorts/"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        f.write(f"{idea} | {(base + video_id) if video_id else 'not uploaded'} | {stamp} | {FORMAT}\n")


def uploaded_within(hours: float) -> bool:
    """True if a video of this format was uploaded in the last `hours` (done.txt timestamps)."""
    if not DONE.exists():
        return False
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    for line in reversed(DONE.read_text(encoding="utf-8").splitlines()):
        parts = [x.strip() for x in line.split(" | ")]
        if len(parts) < 4 or parts[-1] != FORMAT or not parts[-3].startswith("http"):
            continue
        try:
            when = datetime.strptime(parts[-2], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return when >= since  # newest upload of this format decides
    return False


REFILL_PROMPT = """You plan content for "{channel}", a worldwide YouTube channel of SILENT stories:
no voiceover, no dialogue, only {images}, short captions and music.
{learn}Write {n} NEW, original {kind} ideas for a global audience (any country; universal emotions).
Each idea: one sentence with a clear character, situation and emotional or surprising payoff.
{toon}{genres}
Do not repeat or closely resemble any of these existing ideas:
{existing}
Return ONLY JSON: {{"ideas": [{{"genre": str, "idea": str}}]}}"""


def add_tech_news() -> None:
    """Discovery Mosti TECH: put one fresh, widely discussed tech story (Hacker News front page, free,
    no key) at the top of the queue, at most one news Short a day."""
    since = datetime.now(timezone.utc) - timedelta(hours=20)
    log = DONE.read_text(encoding="utf-8") if DONE.exists() else ""
    for line in log.splitlines():
        parts = [x.strip() for x in line.split(" | ")]
        if len(parts) >= 4 and parts[0].startswith("Tech news:"):
            try:
                if datetime.strptime(parts[-2], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) >= since:
                    return
            except ValueError:
                pass
    queue = STORIES.read_text(encoding="utf-8")
    if "| Tech news:" in queue:
        return
    try:
        hn = "https://hacker-news.firebaseio.com/v0"
        found = []
        for sid in requests.get(f"{hn}/topstories.json", timeout=20).json()[:30]:
            it = requests.get(f"{hn}/item/{sid}.json", timeout=20).json() or {}
            title, url = " ".join(str(it.get("title", "")).replace("|", "/").split()), it.get("url", "")
            if (it.get("type") == "story" and url and it.get("score", 0) >= 200
                    and not re.match(r"(Ask|Show|Tell) HN", title) and title[:60] not in log):
                found.append((title, url))
        if not found:
            return
        # the front page also carries human-interest, politics and business drama: only a headline the
        # AI confirms is about technology itself gets on the TECH channel
        pick = json.loads(_strip_fences(ask_ai(TECH_NEWS_PROMPT.format(
            headlines="\n".join(f"{i}. {t}" for i, (t, _) in enumerate(found))), creative=False))).get("pick")
        if not isinstance(pick, int) or isinstance(pick, bool) or not 0 <= pick < len(found):
            print("  tech news: no clear technology story on the front page right now, skipping")
            return
        title, url = found[pick]
        STORIES.write_text(f"news | Tech news: {title} (source: {url})\n" + queue, encoding="utf-8")
        print(f"  tech news added: {title}")
    except (SystemExit, Exception) as e:  # noqa: BLE001 - news is a bonus; the queue has other ideas
        print(f"  tech news unavailable ({str(e)[:150]})")


TECH_NEWS_PROMPT = """These are today's most discussed headlines on a tech forum. A silent tech-explainer
channel wants ONE of them for a 50-second video that explains a technology.
Pick the first headline that is clearly about technology itself: software, hardware, AI, the internet,
security, gadgets, engineering or a technical launch, where a video can show HOW something works or what
changed for users.
Never pick human-interest or personal stories, interviews, politics, crime, lawsuits, executive or
company drama (hirings, firings, feuds), opinion essays, or headlines too vague to tell what they are about.
{headlines}
Return ONLY JSON: {{"pick": number}}  (the headline's number, or -1 if none qualifies)"""


TECH_RULES = """TECH CHANNEL RULES ("{channel}" explains technology to everyone, without words)
- Genres: "explainer" = how a technology works; "news" = a real recent tech story (state only what the
  headline says, add general background you are sure of, never invent numbers, dates, names or quotes);
  "tip" = a practical phone/PC/app/AI tip anyone can try; "product" = what a product does and why it matters;
  "list" = a numbered countdown of 4-6 items (e.g. free apps that replace paid ones): scene 1 promises the
  payoff in money or time saved ("5 free apps, $500 saved"), then one or two scenes per item whose first
  caption starts with its number ("#1 LibreOffice replaces Microsoft Office"), the paid thing shown crossed
  out before the free one appears; the last scene sums up. Name only real, well-known products you are sure
  are free, never star counts or prices you are not sure of.
- Accuracy first: leave out any detail you are not sure of. Captions explain step by step, like a smart friend.
- This is an EXPLAINER, never a drama: the technology is the star of every scene. No personal struggles,
  sad, romantic or tear-jerking plots, family or animal stories, heroes or villains. A "news" video shows
  what the technology is, what changed and why it matters to users, not the people or conflict behind it.
  The guide character only demonstrates and reacts with curiosity or delight; it has no story of its own.
- "music_mood" is curious, uplifting or tense, never sad or warm.
- Visuals: {tech_visuals}. Generic, unbranded devices only: no logos, brand marks,
  real product photos or screenshots of real apps.
- The description's last line invites viewers to subscribe to {channel} for tech explained without words.
"""



TECH_VISUALS = ("clean modern tech look (macro shots of chips and circuits, glowing data, hands using generic "
                "devices, simple diagram-like compositions)")
TECH_VISUALS_TOON = ("bright, friendly cartoon tech look: chunky stylized devices, cutaway views that show the "
                     "inside, data drawn as glowing dots and arrows, simple diagram-like compositions; a small "
                     "recurring cartoon guide character (a curious robot or a stylized person) reacts to each step")


def refill_queue(min_left: int = 2 if FILM else 5) -> None:
    """Never run dry: when few ideas are left, ask the AI for fresh original ones."""
    if TECH:
        add_tech_news()
    lines = STORIES.read_text(encoding="utf-8").splitlines()
    left = [l for l in lines if l.strip() and not l.lstrip().startswith("#")]
    if len(left) >= min_left:
        return
    existing = [l.split("|", 1)[-1].strip()[:90] for l in left]
    if DONE.exists():
        existing += [l.split("|")[0].strip()[:90] for l in DONE.read_text(encoding="utf-8").splitlines() if l.strip()]
    n = 4 if FILM else 10
    perf, learn = channel_performance(), ""
    if len(perf) >= 4 and perf[0][1] > 0:
        print("  top videos so far: " + "; ".join(f"{t[:40]} ({v})" for t, v in perf[:3]))
        half = len(perf) // 2  # best and worst lists never share a video
        learn = ("What our audience watched most (lean toward these themes, emotions and formats):\n"
                 + "\n".join(f"- {t} ({v} views)" for t, v in perf[:min(5, half)])
                 + "\nWhat worked least (avoid repeating these angles):\n"
                 + "\n".join(f"- {t} ({v} views)" for t, v in perf[-min(3, half):]) + "\n\n")
    prompt = REFILL_PROMPT.format(
        channel=CHANNEL_NAME, n=n, learn=learn, images="cartoon animation" if ANIMATED else "cinematic images",
        toon="The videos are CARTOONS: pick ideas that are fun to watch animated. Heroes can be people, animals, "
             "robots or everyday objects that come alive, with big visible emotions and a visual surprise.\n"
        if ANIMATED else "",
        kind="three-act short-film (about 3 minutes)" if FILM else "YouTube Shorts (about 50 seconds)",
        genres='genre is always "story".' if FILM else
        'Mix genres: about half "story" (emotional micro-drama), a quarter "explainer" (how something '
        'works) and a quarter "fact" (surprising true fact, must be accurate).',
        existing="\n".join(f"- {e}" for e in existing[-60:]) or "- (none)")
    if TECH:
        prompt = prompt.replace("a worldwide YouTube channel of SILENT stories", "a worldwide YouTube channel of "
                                "SILENT tech explainers").replace(
            "Each idea: one sentence with a clear character, situation and emotional or surprising payoff.",
            "Each idea: one sentence naming the technology and the surprising insight the viewer will get.").replace(
            "Mix genres: about half", "IGNORE: about half").replace(
            "(any country; universal emotions)", "(any country; things people use every day)").replace(
            "Heroes can be people, animals, "
             "robots or everyday objects that come alive, with big visible emotions and a visual surprise.",
            "The technology is the star (cutaway views, the invisible made visible); never a drama or "
            "an emotional character story.") + (
            '\nGenres for this tech channel: about half "explainer" (how a technology works), a fifth "tip" '
            '(a practical phone/PC/app/AI tip anyone can try), a fifth "list" (a numbered countdown of 4-6 '
            'items, e.g. free apps that replace paid ones, with the money or time saved) and the rest "product" '
            '(what a kind of product or a well-known launch does and why it matters). Only facts you are sure of.')
    try:
        ideas = json.loads(_strip_fences(ask_ai(prompt)))["ideas"]
    except (SystemExit, Exception) as e:  # refilling is best effort; the run goes on
        print(f"  could not refill {STORIES.name}: {e}")
        return
    new = [f"{(i.get('genre') or 'story').strip().lower()} | {' '.join(str(i['idea']).replace('|', '/').split())}"
           for i in ideas if isinstance(i, dict) and i.get("idea")]
    if new:
        STORIES.write_text("\n".join(lines).rstrip() + "\n" + "\n".join(new) + "\n", encoding="utf-8")
        print(f"  added {len(new)} fresh ideas to {STORIES.name}")


def channel_performance(limit: int = 50) -> list[tuple[str, int]]:
    """(title, views) of our recent uploads, most viewed first; [] if unavailable.
    Uses the read-only YouTube access granted when the channel was connected (1 quota unit)."""
    if not (DONE.exists() and os.getenv("YT_CLIENT_ID")):
        return []
    ids = re.findall(r"(?:shorts/|youtu\.be/)([\w-]{11})", DONE.read_text(encoding="utf-8"))[-limit:]
    if not ids:
        return []
    try:
        items = youtube_client().videos().list(part="snippet,statistics", id=",".join(ids),
                                               maxResults=50).execute().get("items", [])
    except (SystemExit, Exception) as e:  # stats are a bonus, never a reason to fail
        print(f"  channel stats unavailable: {e}")
        return []
    rows = [(i["snippet"]["title"], int(i.get("statistics", {}).get("viewCount", 0))) for i in items]
    return sorted(rows, key=lambda r: -r[1])


# ================================================================ AI tier (free now, paid once the channel earns)

def month_spend() -> float:
    """Estimated paid AI cost logged this calendar month (spend.txt lines: time | what | usd)."""
    if not SPEND.exists():
        return 0.0
    month, total = datetime.now(timezone.utc).strftime("%Y-%m"), 0.0
    for line in SPEND.read_text(encoding="utf-8").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) == 3 and parts[0].startswith(month):
            try:
                total += float(parts[2])
            except ValueError:
                pass
    return total


def log_spend(what: str, usd: float) -> None:
    with SPEND.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ} | {what} | {usd:.4f}\n")


def paid_ok(cost: float = 0.0) -> bool:
    """Paid models allowed for a call that may cost `cost`: paid tier on and the budget not reached."""
    global PAID
    if PAID and month_spend() + cost > PAID_BUDGET:
        print(f"  this month's paid AI budget (${PAID_BUDGET:g}) is reached: free models only from here")
        PAID = False
    return PAID


def google_token() -> dict:
    """Fresh access token for the connected channel (the reply also lists the granted scopes)."""
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": refresh_token(), "grant_type": "refresh_token"})
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.status_code}


def channel_monetized() -> bool | None:
    """True if the channel earned at least PAID_MIN_REVENUE_USD over the last 28 days of YouTube
    Analytics data, False if less, None if unknown. Never prints an amount: the Actions logs of a
    public repository are public."""
    try:
        tok = google_token()
    except Exception as e:  # noqa: BLE001 - unknown means free
        print(f"  earnings check: no YouTube token ({str(e)[:100]})")
        return None
    if "yt-analytics-monetary" not in tok.get("scope", ""):
        print("  earnings check: not allowed yet. When the channel is monetized, enable the YouTube "
              "Analytics API and run 'Connect YouTube channel' again (README: Paid AI later).")
        return None
    end = datetime.now(timezone.utc).date() - timedelta(days=3)  # revenue data arrives 2-3 days late
    r = requests.get("https://youtubeanalytics.googleapis.com/v2/reports", timeout=60,
                     headers={"Authorization": f"Bearer {tok['access_token']}"},
                     params={"ids": "channel==MINE", "startDate": str(end - timedelta(days=27)),
                             "endDate": str(end), "metrics": "estimatedRevenue", "currency": "USD"})
    if r.status_code != 200:
        hint = ("enable the YouTube Analytics API in Google Cloud" if "disabled" in r.text or
                "accessNotConfigured" in r.text else "is the channel in the YouTube Partner Program?")
        print(f"  earnings check failed ({r.status_code}): {hint}")
        return None
    rows = r.json().get("rows") or [[0]]
    return float(rows[0][0] or 0) >= PAID_MIN_REVENUE


def youtube_connection_check() -> None:
    """Stop before any AI work if the YouTube connection is dead (no free quota is spent on a video
    that can't be uploaded), and warn when Google is going to end it (consent screen in 'Testing')."""
    try:
        tok = google_token()
    except Exception as e:  # noqa: BLE001 - network trouble: the upload itself retries later
        print(f"  YouTube connection check skipped ({str(e)[:100]})")
        return
    if tok.get("error") == "invalid_grant":
        sys.exit("The YouTube connection has expired or was revoked. Run 'Connect YouTube channel' again, and "
                 "publish the OAuth consent screen in Google Cloud so Google stops expiring it (README).")
    if tok.get("refresh_token_expires_in"):
        print(f"WARNING: Google will end this YouTube connection in {int(tok['refresh_token_expires_in']) // 86400} "
              "days because the OAuth consent screen is still in 'Testing'. Publish it and reconnect (README).")


def decide_tier() -> None:
    """Free or paid models for this run (see AI_TIER at the top)."""
    global PAID
    PAID = False
    if AI_TIER == "free":
        why = "AI_TIER=free"
    elif month_spend() >= PAID_BUDGET:
        why = f"this month's paid AI budget (${PAID_BUDGET:g}) is used up"
    elif AI_TIER == "paid":
        PAID, why = True, "AI_TIER=paid"
    elif AI_TIER == "auto":
        earning = channel_monetized() if os.getenv("YT_CLIENT_ID") else None
        PAID = earning is True
        why = {True: "the channel is monetized and earning", False: "the channel is not earning enough yet",
               None: "channel earnings unknown"}[earning]
    else:
        why = f"unknown AI_TIER {AI_TIER!r}"
    if PAID:
        ready = [n for n, k in (("Claude stories", "ANTHROPIC_API_KEY"),
                                ("Gemini images if billing is on", "GEMINI_API_KEY")) if os.getenv(k)]
        why += f"; paid services set up: {', '.join(ready) or 'none (add ANTHROPIC_API_KEY)'}"
    print(f"AI tier: {'PAID' if PAID else 'FREE'} ({why})")


# ================================================================ storyboard + SEO

STORYBOARD_PROMPT = """You are the director and growth marketer of "{channel}", a YouTube
Shorts channel. Videos have NO voiceover and NO dialogue: they tell a story only through
{images}, very short on-screen captions and music, so anyone in any country can follow.
The audience is GLOBAL (Americas, Europe, Asia, Africa, Middle East): use universal emotions and
settings, characters of varied ethnicities and cultures, no country-specific festivals, slang or
symbols unless the idea itself needs them, and simple English any viewer can read.

Genre: {genre}   ("story" = emotional micro-drama / short film, "explainer" = how something
                  works, "fact" = surprising true fact told visually)
Idea: {idea}

STORY RULES
- 9 to 11 scenes, total 45-55 seconds, each scene 3.5-6 seconds.
- Scene 1 is the HOOK: the most striking image + a caption that creates a question.
- Clear arc: hook -> build -> turn -> emotional payoff.
- The final shot visually echoes the first shot, so the Short loops seamlessly when it replays.
- Captions: max 7 simple words, present tense, no emojis. 2-3 scenes have "" (silent beats).
- "ending": shareable closing line, max 8 words.
- "music_mood": one of warm, sad, tense, curious, uplifting.

VISUAL RULES (images are AI-generated; work like a film crew so every shot matches)
- First write the blueprint, then the shots. Every image is generated separately, so
  consistency comes ONLY from re-using the exact same saved descriptions.
{style_rule}
- "grade": one colour grade for the whole film: warm, cool, teal_orange, golden, muted or noir.
- "cast": saved character elements, {cast_rule}"}}].
  Same clothes all film unless the story says otherwise. Names must be distinct.
- "locations": saved location elements, 1-4 places:
  [{{"id": "L1", "look": "fixed description: place, era, materials, colours, time of day"}}].
- each scene, as structured fields:
  "shot": wide, medium, close-up, extreme close-up or over-the-shoulder (vary them),
  "image_prompt": the subject and action only, concrete and filmable,
  "cast": ids of characters visible (e.g. ["A"]) or [],
  "location": one location id,
  "lighting": e.g. "warm window light from the left, soft shadows",
  "camera": one of zoom_in, zoom_out, pan_left, pan_right, pan_up,
  "query": 2-4 word stock-video search fallback (e.g. "rain on window").
- Original characters only, no celebrities, brands, logos, readable text, or copyrighted characters.
{engage_rules}
SEO / SMO RULES
- "title": max 60 chars, main search keyword in the first 4 words, emotional curiosity,
  honest (no clickbait lies), no hashtags, no ALL CAPS.
- "description": 4-6 short lines: line 1 is a hook (max 100 chars) containing the main
  keyword; then 2-3 lines summarising the story naturally with related keywords; last line
  invites viewers to subscribe for more silent stories. No hashtags here.
- "hashtags": 3-5 hashtags, most relevant first (do not include #Shorts).
- "tags": 12-15 search keywords/phrases people actually type, broad to specific.
- "social_caption": an Instagram/Facebook caption (2-3 lines + 8-10 hashtags + 1 emoji max).
- "thumbnail_prompt": the single most eye-catching vertical image for this story,
  same style and cast (refer to them by id, e.g. "A smiling through tears"), strong
  emotion, clean background space at the top.
- "thumbnail_text": 2-4 words, punchy, uppercase, and NO word from the title: the thumbnail says
  what the title doesn't, so together they make one hook.

Return ONLY valid JSON with exactly these keys ("logline" and "beats" first: they are the plan
the scenes must follow):
{{"logline": str, "beats": [str],
  "title": str, "description": str, "hashtags": [str], "tags": [str],
  "social_caption": str, "thumbnail_prompt": str, "thumbnail_text": str,
  "style": str, "grade": str, "music_mood": str, "ending": str,
  "cast": [{{"id": str, "name": str, "look": str}}], "locations": [{{"id": str, "look": str}}],
  "scenes": [{{"shot": str, "image_prompt": str, "cast": [str], "location": str,
               "lighting": str, "camera": str, "query": str, "caption": str, "seconds": number}}]}}"""


STYLE_RULE_REAL = """- "style": one visual style for the whole film (e.g. "cinematic film still, 35mm lens,
  shallow depth of field, soft film grain")."""
STYLE_RULE_TOON = """- "style": write exactly "{style}". The whole video is ANIMATION in this one look:
  never real, photorealistic or live-action people, and no camera or lens words (no "photo", "35mm", "film still")."""
CAST_RULE_REAL = """1-3 people/animals:
  [{{"id": "A", "name": "short role noun, e.g. old kite maker", "look": "fixed visual
  description: age, ethnicity, build, hair, face, exact clothes and colours"""
CAST_RULE_TOON = """1-3 CARTOON characters (stylized people, animals, robots or
  everyday objects that come alive):
  [{{"id": "A", "name": "short role noun, e.g. old kite maker", "look": "fixed character design:
  what it is, age, body shape and size, skin/fur/material colour, hair, eyes, one signature
  feature that makes it recognisable at a glance, exact clothes and colours"""
ENGAGE_RULES_TOON = """
ANIMATION + ENGAGEMENT RULES (viewers decide in the first second whether to keep watching)
- Acting carries the story: every shot shows one clear, exaggerated emotion or action that reads with
  the sound off (wide eyes, jaw drop, slumped shoulders, a leap of joy). Never a neutral standing pose.
- Scene 1 opens in the middle of the action with the main character in a big, readable pose.
- Something visibly changes in every scene: a new shot size, place, emotion or a reveal. No two
  scenes in a row may share both shot size and location.
- Use cartoon freedom: things real footage cannot show (impossible scale, objects with feelings,
  cutaway views, visual metaphors, a funny or touching surprise). Put one surprise near the middle
  and the biggest one in the last two scenes.
- Simple, uncluttered backgrounds with one strong accent colour so the character pops on a phone.
- Prefer a bright "grade" (warm, golden, cool or teal_orange) unless the story is truly dark.
"""


ENGAGE_RULES_TECH = """
ANIMATION + ENGAGEMENT RULES (viewers decide in the first second whether to keep watching)
- The technology carries the video: every shot shows one clear step, part or effect of it that reads with
  the sound off (a cutaway, a signal travelling, a before/after). Never a character just standing or emoting.
- Scene 1 opens on the device or problem itself, big and readable, with a caption that asks the question.
- Something visibly changes in every scene: a new shot size, a deeper look inside, the next step or a
  reveal. No two scenes in a row may share both shot size and location.
- Use cartoon freedom to make the invisible visible (impossible scale, cutaway views, data as glowing
  dots and arrows, simple visual metaphors). Put the key "aha" in the last two scenes.
- Simple, uncluttered backgrounds with one strong accent colour so the subject pops on a phone.
- Use a bright "grade" (cool, teal_orange, warm or golden).
"""


SCREENPLAY_RULES = """SCREENPLAY RULES (write the screenplay first, then shoot it)
- "logline": one sentence: who wants what, what stands in the way, and how it ends.
- "beats": the whole screenplay, one short line per scene, in order. Every beat happens BECAUSE of
  the one before it ("so..." or "but...", never "and then..."). Then write exactly one scene per beat.
- One question is raised in scene 1 and answered only in the last two scenes; every scene in
  between moves the character closer to the answer or further from it. No scene repeats another.
- Pictures and captions share the work: a caption never describes what the picture already shows.
  It adds what a picture cannot: a thought, a time jump ("Every night, for a year"), what is at
  stake, a twist. Read alone from top to bottom, the captions still tell the story.
- Scene 1 always has a caption. Silent beats ("") belong on the strongest pictures (the turn, the
  reveal), never two in a row, and no caption is used twice.
- Each "image_prompt" says who does what, and shows the NEXT moment of the story, not the same
  moment again from another angle.
- A caption needs time to be read: about 1.5 seconds plus half a second per word.
"""
SCREENPLAY_RULES_TECH = """SCREENPLAY RULES (write the screenplay first, then shoot it)
- "logline": one sentence: the question the video asks and the answer the viewer leaves with.
- "beats": the whole screenplay, one short line per scene, in order. Every beat explains or
  follows from the one before it; nothing is skipped. Then write exactly one scene per beat.
- The question is asked in scene 1 and fully answered only in the last two scenes. No scene
  repeats another.
- Pictures and captions share the work: a caption never describes what the picture already shows.
  It names the step, the reason or the number the picture cannot. Read alone from top to bottom,
  the captions still explain the topic.
- Scene 1 always has a caption. Silent beats ("") belong on the clearest pictures, never two in a
  row, and no caption is used twice.
- Each "image_prompt" shows the NEXT step, not the same step again from another angle.
- A caption needs time to be read: about 1.5 seconds plus half a second per word.
"""

NARRATION_RULES = """NARRATION RULES (the channel's owner reads the narration aloud in their own voice; it is the heart of the video)
- Every scene has a "narration": what the narrator SAYS while that picture is on screen, written in
  {lang} (in that language's own script, e.g. Devanagari for Hindi, in the everyday words people
  really speak, not formal or bookish language), 1-2 short spoken sentences ({per_scene} words). All scenes together: {total} words.
- It is one continuous piece of storytelling by a warm, personal narrator: it flows from scene to
  scene, and read aloud on its own it is a complete, satisfying {piece}.
- It adds what pictures cannot: what the character thinks or fears, why something matters, the
  narrator's own reflection or a little humour. Never a description of the picture ("we see a dog").
- Scene 1's narration is the hook: a line that makes a listener stop scrolling. The last scene's
  narration lands the meaning and is the line people remember.
- Spoken language: short words, natural rhythm, contractions, no lists, no brackets, no stage
  directions, no hashtags. Numbers written as words.
- "seconds" of each scene = its narration's word count / {wps} + 0.5.
- The on-screen captions stay as short as before; they highlight, they do not repeat the narration.
"""

# what the code checks after the AI answers (same numbers as the rules in the prompts)
SCENES_OK, TOTAL_OK, CAPTION_WORDS = ((16, 26), (150.0, 190.0), 8) if FILM else ((8, 12), (45.0, 55.0), 7)
# narrated videos: how long the voice track may be (a Short then ends at about 50 s with its closing
# line), and the words that fit into it at a storytelling pace
VOICE_SECONDS = 172.0 if FILM else 46.0
NARRATION_WORDS = (int(VOICE_SECONDS * 1.9), int(VOICE_SECONDS * 2.2))

SCREENPLAY_FIX_PROMPT = """You are the script editor of "{channel}", a channel of SILENT videos (no voice;
pictures, very short captions and music). Rewrite the scenes of this screenplay so that every
problem below is fixed. Keep what already works: the same idea, the same characters and places.

Idea: {idea}
Characters (use only these ids in "cast"): {cast}
Locations (use only these ids in "location"): {locations}

PROBLEMS TO FIX
{problems}

RULES
- {min_scenes} to {max_scenes} scenes that add up to {min_total:g}-{max_total:g} seconds.
- Captions: max {words} simple words, present tense, no emojis. Scene 1 has a caption. A few scenes
  have "" (silent beats), never two in a row, and no caption is used twice.
- A caption never describes what the picture already shows; it adds a thought, a time jump, the
  stakes or a twist. Read alone from top to bottom, the captions still tell it all.
- Every scene follows from the one before it and shows a new moment. No two scenes in a row share
  both "shot" and "location".
- Keep every field of every scene.

CURRENT SCENES
{scenes}

Return ONLY JSON: {{"beats": [str], "scenes": [{{"shot": str, "image_prompt": str, "cast": [str],
  "location": str, "lighting": str, "camera": str, "query": str, "caption": str, "seconds": number}}]}}"""

FILM_RULES = """STORY RULES (weekly widescreen SHORT FILM, not a Short)
- 18 to 24 scenes, total 150-190 seconds, each scene 6-10 seconds (let shots breathe).
- Three acts: setup (who, where, what they want) -> conflict / obstacle -> turning point
  -> emotional resolution. Give the main character a clear goal and a visible change.
- Scene 1-2 are the HOOK: a striking image + a caption that creates a question.
- Captions: max 8 simple words, present tense, no emojis. About a third of scenes have ""
  (silent beats) so the images breathe.
- "ending": shareable closing line, max 10 words.
- "music_mood": one of warm, sad, tense, curious, uplifting.
"""

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_model_cache: dict[str, str | None] = {}


def _version_key(name: str) -> tuple:
    nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)", name.split("/")[-1])[:1]] or [0.0]
    stable = 0 if re.search(r"preview|exp|experimental", name) else 1
    return (stable, nums[0], -len(name))


def gemini_candidates(kind: str) -> list[str]:
    """Ranked list of usable Gemini models ('text' or 'image') for this API key, newest
    stable first, so the pipeline survives model retirements and busy models."""
    if kind in _model_cache:
        return _model_cache[kind]
    wanted = GEMINI_MODEL if kind == "text" else GEMINI_IMAGE_MODEL
    try:
        r = requests.get(f"{GEMINI_BASE}/models", params={"pageSize": 1000},
                         headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=60)
        r.raise_for_status()
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
    except requests.RequestException as e:
        print(f"  could not list Gemini models ({e})")
        names = []
    if kind == "text":
        pool = [n for n in names if re.search(r"flash|pro", n)
                and not re.search(r"image|tts|audio|live|embed|thinking|computer|robotics", n)]
        newest = lambda xs: sorted(xs, key=_version_key, reverse=True)
        flash = newest([n for n in pool if "flash" in n and "lite" not in n])
        lite = newest([n for n in pool if "lite" in n])
        gemma = newest([n for n in names if re.fullmatch(r"gemma-[\d.]+-\d+b(?:-a\d+b)?-it", n)])  # 31B, then 26B MoE
        # every model has its own free quota: mix families so that when the Flash models (the
        # smartest free ones) are busy or used up for the day, Flash-Lite and Gemma still answer.
        # No Pro: Gemini 3.1 Pro / 4 are paid-only and 2.5 Pro is closed to new users (AI check, Oct 2026).
        pool = flash[:3] + lite[:3] + gemma[:2]
    else:
        pool = sorted([n for n in names if "image" in n and "gemini" in n], key=_version_key, reverse=True)[:5]
    if wanted:
        pool = [wanted] + [n for n in pool if n != wanted]
    print(f"  Gemini {kind} models: {pool}")
    _model_cache[kind] = pool
    return pool


def pick_gemini_model(kind: str) -> str | None:
    c = gemini_candidates(kind)
    return c[0] if c else None


def ask_gemini(prompt: str, models: list[str] | None = None, patient: bool = False) -> str:
    """patient=True (ideas, storyboards, titles): the two best models get 5 tries with growing waits
    when they are busy, so the video is written by the best free model, not by whichever weaker one
    happens to answer first. Translations stay quick."""
    models = models or gemini_candidates("text")
    if not models:
        raise SystemExit("No Gemini text model available for this API key.")
    last = ""
    for rank, model in enumerate(models):
        tries = 5 if patient and rank < 2 else 2  # weaker models: free backups exist, don't sit on a busy one
        for attempt in range(tries):
            try:
                r = requests.post(
                    f"{GEMINI_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                    json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.9} | ({} if model.startswith("gemma") else
                                                                     {"responseMimeType": "application/json"})},
                    timeout=180,
                )
            except requests.RequestException as e:
                last = str(e)
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                    json.loads(_strip_fences(text))
                    print(f"  answer by {model}")
                    return text
                except (KeyError, IndexError, ValueError) as e:
                    last = f"bad response: {e}"
                    continue
            last = f"{r.status_code}: {r.text[:300]}"
            print(f"  {model} -> {r.status_code}, attempt {attempt + 1}")
            if r.status_code in (500, 502, 503, 504):  # busy: wait (10 s, 20 s, ... up to 40 s), then the next model
                if attempt + 1 < tries:
                    time.sleep(10 * (attempt + 1))
                continue
            break  # 429: this model's free quota is used up; 400/403/404: unusable. Next model.
    raise SystemExit(f"Gemini failed on all models. Last error {last}")


# Free OpenAI-compatible backups for the story text, tried in order when Gemini is busy or
# out of quota. Each is used only if its key is present; all are free tiers without billing.
#   Groq:          GROQ_API_KEY       (console.groq.com, free plan, no card; run the AI check to
#                                     see its current models - Groq retires them often)
#   OpenRouter:    OPENROUTER_API_KEY (openrouter.ai, only ":free" models are requested)
FREE_TEXT_PROVIDERS = [
    ("groq", "https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY",
     ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]),  # each has its own free daily limit
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY",
     ["meta-llama/llama-3.3-70b-instruct:free", "deepseek/deepseek-chat-v3-0324:free"]),
]


def _strip_fences(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # reasoning models (Qwen) may think out loud first
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())


def text_providers() -> list[tuple[str, str, str, list[str]]]:
    """(name, url, key, models) of the free text backups usable in this run, in the order tried."""
    out = [(name, url, os.environ[env], models) for name, url, env, models in FREE_TEXT_PROVIDERS if os.getenv(env)]
    # Pollinations: works without a key (a free token from auth.pollinations.ai raises its limits)
    out.append(("pollinations", "https://text.pollinations.ai/openai", os.getenv("POLLINATIONS_TOKEN", ""),
                ["openai", "mistral"]))
    # Cloudflare Workers AI text: reuses the image key but spends the same daily free allowance,
    # so it is the last resort
    acct, token = os.getenv("CF_ACCOUNT_ID"), os.getenv("CF_API_TOKEN")
    if acct and token:
        out.append(("cloudflare", f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/v1/chat/completions",
                    token, ["@cf/meta/llama-3.3-70b-instruct-fp8-fast"]))
    return out


def ask_openai_compatible(name: str, url: str, key: str, models: list[str], prompt: str) -> str | None:
    headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {key}"} if key else {})
    for model in models:
        if name == "openrouter" and not model.endswith(":free"):
            continue  # never a paid OpenRouter model
        body = {"model": model, "temperature": 0.9, "max_tokens": 8000,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": "Reply with valid JSON only."},
                             {"role": "user", "content": prompt}]}
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
            if r.status_code == 400:  # some models reject JSON mode: ask again without it
                body.pop("response_format")
                r = requests.post(url, headers=headers, json=body, timeout=180)
        except requests.RequestException as e:
            print(f"  {name}/{model} error: {e}")
            continue
        if r.status_code != 200:
            print(f"  {name}/{model} -> {r.status_code}: {r.text[:150]}")
            if r.status_code in (401, 403):
                return None  # bad/missing permission: skip this provider
            continue
        if "json" not in r.headers.get("content-type", ""):
            print(f"  {name} answered {r.headers.get('content-type')} {r.text[:60]!r}, not an API reply; skipping")
            return None
        try:
            text = r.json()["choices"][0]["message"]["content"]
            json.loads(_strip_fences(text))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            print(f"  {name}/{model} bad response: {e} | {r.headers.get('content-type')} | {r.text[:300]!r}")
            continue
        print(f"  answer by {name}/{model}")
        return text
    return None


def ask_ai(prompt: str, creative: bool = True) -> str:
    """Text from the best provider that answers with valid JSON: the strongest free Gemini model is
    retried patiently for creative text before a weaker one is used. In the paid tier Claude writes
    the creative text (ideas, stories) first; translations always use the free models."""
    if creative and os.getenv("ANTHROPIC_API_KEY") and paid_ok():
        try:
            return ask_claude(prompt)
        except Exception as e:  # noqa: BLE001 - paid is a bonus: the free models take over
            print(f"  Claude unavailable ({str(e)[:150]}); using the free models")
    if os.getenv("GEMINI_API_KEY"):
        try:
            return ask_gemini(prompt, patient=creative)
        except SystemExit as e:
            print(f"  Gemini unavailable ({str(e)[:120]}); trying free backups")
    for name, url, key, models in text_providers():
        text = ask_openai_compatible(name, url, key, models, prompt)
        if text:
            return text
    raise SystemExit("No AI text provider answered (all busy or out of free quota). Add a free "
                     "GROQ_API_KEY backup, or wait for the daily reset.")


def ask_claude(prompt: str) -> str:
    """Claude (paid tier only). Thinking is always on in current models; effort "high" for story
    quality. If Claude declines a prompt, the API re-runs it on another Claude model ("default"
    server-side fallback) and the cost of what ran is logged."""
    import anthropic
    inp, out = CLAUDE_PRICES.get(CLAUDE_MODEL, (10.0, 50.0))
    if not paid_ok(16000 * out / 1e6):  # the dearest this call can be must fit in the month's budget
        raise RuntimeError("monthly paid budget reached")
    fallback = ({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
                if CLAUDE_MODEL in CLAUDE_FALLBACK_OK else {})
    msg = anthropic.Anthropic().beta.messages.create(
        model=CLAUDE_MODEL, max_tokens=16000, output_config={"effort": "high"},
        system="Reply with valid JSON only, no other text.",
        messages=[{"role": "user", "content": prompt}], **fallback)
    inp, out = CLAUDE_PRICES.get(msg.model, (inp, out))
    log_spend(f"claude {msg.model}", (msg.usage.input_tokens * inp + msg.usage.output_tokens * out) / 1e6)
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined this prompt")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    json.loads(_strip_fences(text))
    print(f"  answer by {msg.model}")
    return text


def storyboard_problem(sb) -> str:
    """Why a storyboard can't be used ("" if it can). Drops scenes without an image prompt."""
    if not isinstance(sb, dict):
        return "not a JSON object"
    if not str(sb.get("title") or "").strip():
        return "missing title"
    if not isinstance(sb.get("scenes"), list):
        return "no scenes"
    sb["scenes"] = [s for s in sb["scenes"] if isinstance(s, dict) and str(s.get("image_prompt") or "").strip()]
    need = 12 if FILM else 5
    if len(sb["scenes"]) < need:
        return f"only {len(sb['scenes'])} usable scenes (need {need})"
    return ""


def _caption(scene: dict) -> str:
    return " ".join(str(scene.get("caption") or "").split())


def screenplay_problems(sb: dict) -> list[str]:
    """What is wrong with the screenplay as written ([] if nothing): the rules the prompt asks for,
    checked in code, because the free models often ignore some of them."""
    scenes, out = sb["scenes"], []
    caps = [_caption(s) for s in scenes]
    if not SCENES_OK[0] <= len(scenes) <= SCENES_OK[1]:
        out.append(f"there are {len(scenes)} scenes; write {SCENES_OK[0]} to {SCENES_OK[1]}")
    long = [f"scene {i + 1} (\"{c}\")" for i, c in enumerate(caps) if len(c.split()) > CAPTION_WORDS]
    if long:
        out.append(f"captions longer than {CAPTION_WORDS} words: {', '.join(long)}")
    if not caps[0]:
        out.append("scene 1 has no caption; the hook needs a caption that raises a question")
    silent = sum(1 for c in caps if not c)
    if silent == 0 and not TECH:  # an explainer may name every step
        out.append("every scene has a caption; leave 2-3 strong pictures silent (\"\")")
    elif silent > len(scenes) // 2:
        out.append(f"{silent} of {len(scenes)} scenes have no caption; the story can't be followed. "
                   "Only about a third may be silent")
    if any(not a and not b for a, b in zip(caps, caps[1:])) and len(scenes) - silent >= 3:
        out.append("two silent scenes in a row; put a caption on one of them")
    seen, twice = set(), []
    for c in caps:
        if c and c.lower() in seen and c not in twice:
            twice.append(c)
        seen.add(c.lower())
    if twice:
        out.append("the same caption is used twice: " + ", ".join(f'"{c}"' for c in twice))
    same = [f"{i + 1}-{i + 2}" for i, (a, b) in enumerate(zip(scenes, scenes[1:]))
            if a.get("shot") and a.get("location") and a.get("shot") == b.get("shot")
            and a.get("location") == b.get("location")]
    if len(same) > (1 if FILM else 0) + len(scenes) // 6:  # a couple are fine, a row of them is a slideshow
        out.append(f"scenes {', '.join(same)} share the same shot size and location; vary the shots")
    spoken = sum(len(str(s.get("narration") or "").split()) for s in scenes) if VOICED else 0
    if spoken > NARRATION_WORDS[1] * 1.1:
        out.append(f"the narration has {spoken} words, too long to say in {VOICE_SECONDS:.0f} seconds; "
                   f"cut it to {NARRATION_WORDS[0]}-{NARRATION_WORDS[1]} words in all, keeping every scene's line")
    cast = {c.get("id") for c in sb.get("cast") or [] if isinstance(c, dict)}
    places = {l.get("id") for l in sb.get("locations") or [] if isinstance(l, dict)}
    bad = [str(i + 1) for i, s in enumerate(scenes)
           if (cast and any(not isinstance(c, str) or c not in cast for c in
                            ([s["cast"]] if isinstance(s.get("cast"), str) else s.get("cast") or []))
               and isinstance(s.get("cast"), (str, list)))
           or (places and isinstance(s.get("location"), str) and s["location"] and s["location"] not in places)]
    if bad:
        out.append(f"scenes {', '.join(bad)} use a character or location id that is not in the saved list")
    return out


def fix_screenplay(sb: dict, idea: str) -> None:
    """Check the screenplay and let the AI rewrite the scenes once when rules are broken (best effort:
    the rewrite is kept only if it is complete and has fewer problems)."""
    problems = screenplay_problems(sb)
    if not problems:
        print("  screenplay check: OK")
        return
    print(f"  screenplay check: {'; '.join(x[:160] for x in problems)}")
    keys = ("shot", "image_prompt", "cast", "location", "lighting", "camera", "query", "caption", "seconds") \
        + (("narration",) if VOICED else ())
    try:
        fix = json.loads(_strip_fences(ask_ai(SCREENPLAY_FIX_PROMPT.format(
            channel=CHANNEL_NAME, idea=idea, problems="\n".join(f"- {x}" for x in problems),
            cast=json.dumps([{k: c.get(k) for k in ("id", "name")} for c in sb.get("cast") or []
                             if isinstance(c, dict)], ensure_ascii=False) or "[]",
            locations=json.dumps([l.get("id") for l in sb.get("locations") or [] if isinstance(l, dict)]),
            min_scenes=SCENES_OK[0] + 1, max_scenes=SCENES_OK[1] - 1, min_total=TOTAL_OK[0], max_total=TOTAL_OK[1],
            words=CAPTION_WORDS,
            scenes=json.dumps([{k: s.get(k) for k in keys} for s in sb["scenes"]], ensure_ascii=False, indent=1))
            + ('\nEvery scene also keeps a "narration" (what the narrator says, 1-2 spoken sentences in '
               f'{NARRATION_LANGUAGE}); together the narrations stay one flowing piece.' if VOICED else ""))))
        new = dict(sb, scenes=fix["scenes"])
        if storyboard_problem(new):
            raise ValueError(storyboard_problem(new))
        if VOICED and any(not str(x.get("narration") or "").strip() for x in new["scenes"]):
            raise ValueError("the rewrite lost the narration")
    except (SystemExit, Exception) as e:  # noqa: BLE001 - the first draft is still a usable video
        print(f"  could not rewrite the screenplay ({str(e)[:100]}); keeping the first draft")
        return
    left = screenplay_problems(new)
    if len(left) < len(problems):
        sb["scenes"] = new["scenes"]
        if isinstance(fix.get("beats"), list):
            sb["beats"] = fix["beats"]
        print("  screenplay rewritten" + (f" (still: {'; '.join(x[:160] for x in left)})" if left else ": all fixed"))
    else:
        print("  the rewrite was no better; keeping the first draft")


def time_screenplay(scenes: list[dict]) -> None:
    """Scene lengths the edit can rely on: the whole video inside the planned running time, and every
    caption on screen long enough to be read."""
    lo, hi = 3.0, 10.0 if FILM else 7.0
    total = sum(s["seconds"] for s in scenes)
    if not TOTAL_OK[0] <= total <= TOTAL_OK[1]:
        want = min(max(total, TOTAL_OK[0]), TOTAL_OK[1])
        for s in scenes:
            s["seconds"] = max(lo, min(hi, s["seconds"] * want / total))
        print(f"  timing: scenes added up to {total:.0f} s, stretched/tightened to {sum(s['seconds'] for s in scenes):.0f} s")
    for s in scenes:
        words = len(s["caption"].split())
        if words:  # the caption is hidden during the crossfades at both ends of the scene
            s["seconds"] = max(s["seconds"], min(hi, 1.6 + 0.45 * words))
        s["seconds"] = round(s["seconds"], 2)


# Title + thumbnail checked as one pairing, as viewers see them. Rules adapted from the title linter
# of github.com/Jakeschincariol/youtube-agent-skill (MIT).
VAGUE_WORDS = {"amazing", "incredible", "insane", "crazy", "huge", "massive", "ultimate", "best",
               "powerful", "secret", "revolutionary", "mindblowing", "epic", "perfect", "complete",
               "everything", "shocking", "unbelievable"}
STOP_WORDS = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "are", "with", "your",
              "you", "my", "i", "this", "that", "it", "how", "what", "why", "when", "who", "his", "her"}

PACKAGE_FIX_PROMPT = """Improve this YouTube video's title and thumbnail text. Fix exactly these problems:
{problems}
Story: {description}
Current title: {title}
Current thumbnail text: {thumb}
Rules: title max 60 characters, the main subject in the first 4 words, honest curiosity, no hashtags,
no ALL CAPS; thumbnail text 2-4 words, uppercase, adding what the title does not say and sharing no
word with it. Return ONLY JSON: {{"title": str, "thumbnail_text": str}}"""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def package_problems(title: str, thumb: str) -> list[str]:
    """What weakens the title + thumbnail pairing ([] if nothing)."""
    out = []
    if len(title) > 60:
        out.append(f"the title has {len(title)} characters; search results cut it after about 60")
    if not [w for w in _words(title)[:4] if w not in STOP_WORDS]:
        out.append("the first words of the title are filler; phones show only about 40 characters")
    caps = [w for w in title.split() if len(w) > 2 and w.isupper()]
    if len(caps) > 1:
        out.append("the title shouts in capitals")
    vague = sorted({w for w in _words(f"{title} {thumb}") if w in VAGUE_WORDS})
    if vague:
        out.append(f"vague hype words ({', '.join(vague)}); say something concrete instead")
    shared = sorted((set(_words(title)) & set(_words(thumb))) - STOP_WORDS)
    if shared:
        out.append(f"the thumbnail repeats the title ({', '.join(shared)}); it should add new words")
    if len(_words(thumb)) > 4:
        out.append(f"the thumbnail text has {len(_words(thumb))} words; 2-4 read at feed size")
    return out


def polish_package(sb: dict) -> None:
    """Check the title + thumbnail text and let the AI fix weak points once (best effort)."""
    thumb = str(sb.get("thumbnail_text") or "")
    problems = package_problems(sb["title"], thumb)
    if not problems:
        print("  title + thumbnail check: OK")
        return
    print(f"  title + thumbnail check: {'; '.join(problems)}")
    try:
        fix = json.loads(_strip_fences(ask_ai(PACKAGE_FIX_PROMPT.format(
            problems="\n".join(f"- {x}" for x in problems), description=sb["description"][:400],
            title=sb["title"], thumb=thumb))))
        title, new_thumb = " ".join(str(fix["title"]).split()), " ".join(str(fix["thumbnail_text"]).split()).upper()
    except (SystemExit, Exception) as e:  # noqa: BLE001 - polishing must never stop a video
        print(f"  could not polish the title ({str(e)[:100]})")
        return
    left = package_problems(title, new_thumb)
    if title and new_thumb and len(left) < len(problems):
        print(f"  improved: \"{title}\" + [{new_thumb}]" + (f" (still: {'; '.join(left)})" if left else ""))
        sb["title"], sb["thumbnail_text"] = title, new_thumb


TECH_WORDING = [
    ("they tell a story only through", "they explain technology only through"),
    ("use universal emotions and\nsettings", "use everyday situations and\nsettings"),
    ("- Clear arc: hook -> build -> turn -> emotional payoff.",
     "- Clear arc: hook (a question) -> how it works, step by step -> the \"aha\" takeaway. Not a drama."),
    ("emotional curiosity", "real curiosity about the technology"),
    ("summarising the story naturally", "summarising the topic naturally"),
    ("vertical image for this story", "vertical image for this topic"),
    ('(refer to them by id, e.g. "A smiling through tears"), strong\n  emotion,',
     '(refer to them by id, e.g. "A pointing at the open phone"), the technology shown\n  big and clear,'),
]


def narrated(prompt: str) -> str:
    """The prompts were written for silent videos: wording for videos the owner narrates."""
    for old, new in [
        ("Videos have NO voiceover and NO dialogue: they tell a story only through",
         "Videos are NARRATED by the channel's owner in their own voice, over"),
        ("Videos have NO voiceover and NO dialogue: they explain technology only through",
         "Videos are NARRATED by the channel's owner in their own voice, over"),
        ("very short on-screen captions and music, so anyone in any country can follow.",
         "very short on-screen captions and music. The pictures and captions alone must still be easy to follow."),
        ("subscribe for more silent stories", "subscribe for more narrated stories"),
        ("for tech explained without words", "for tech explained simply"),
        ("explains technology to everyone, without words", "explains technology to everyone, narrated by its owner"),
    ]:
        prompt = prompt.replace(old, new)
    return prompt


def storyboard(genre: str, idea: str) -> dict:
    prompt = STORYBOARD_PROMPT.format(
        genre=genre, idea=idea, channel=CHANNEL_NAME,
        images="cartoon animation images" if ANIMATED else "cinematic images",
        style_rule=STYLE_RULE_TOON.format(style=STYLE) if ANIMATED else STYLE_RULE_REAL,
        cast_rule=(CAST_RULE_TOON if ANIMATED else CAST_RULE_REAL).format(),
        engage_rules=(ENGAGE_RULES_TECH if TECH else ENGAGE_RULES_TOON) if ANIMATED else "")
    if FILM:
        start, end = prompt.index("STORY RULES"), prompt.index("VISUAL RULES")
        prompt = (prompt[:start] + FILM_RULES + "\n" + prompt[end:]).replace(
            "YouTube\nShorts channel", "YouTube\nchannel").replace("vertical image", "widescreen 16:9 image")
    prompt = prompt.replace("VISUAL RULES", (SCREENPLAY_RULES_TECH if TECH else SCREENPLAY_RULES) + "\nVISUAL RULES", 1)
    if TECH:
        prompt = prompt.replace('("story" = emotional micro-drama / short film, "explainer" = how something\n'
                                '                  works, "fact" = surprising true fact told visually)',
                                "(see TECH CHANNEL RULES)").replace(
            "VISUAL RULES", TECH_RULES.format(channel=CHANNEL_NAME, tech_visuals=TECH_VISUALS_TOON if ANIMATED else TECH_VISUALS) + "\nVISUAL RULES", 1).replace(
            "invites viewers to subscribe for more silent stories", f"invites viewers to subscribe to {CHANNEL_NAME}")
        # the shared prompt is written for emotional micro-dramas: TECH gets explainer wording instead
        for old, new in TECH_WORDING:
            if old not in prompt:
                print(f"  note: tech wording not applied ({old[:40]!r} missing from the prompt)")
            prompt = prompt.replace(old, new)
    if VOICED:
        lo, hi = TOTAL_OK
        prompt = narrated(prompt).replace("VISUAL RULES", NARRATION_RULES.format(
            lang=NARRATION_LANGUAGE, per_scene="10-22" if FILM else "7-13", wps=WORDS_PER_SECOND,
            total=f"{NARRATION_WORDS[0]}-{NARRATION_WORDS[1]} (never more: the video must stay "
                  f"{'under ' + str(int(hi)) if FILM else 'around 50'} seconds)",
            piece="explanation" if TECH else "story") + "\nVISUAL RULES", 1).replace(
            '"query": str, "caption": str, "seconds": number}]}', '"query": str, "caption": str, "narration": str, "seconds": number}]}')
    for attempt in range(2):  # an incomplete storyboard gets one fresh try
        try:
            sb = json.loads(_strip_fences(ask_ai(prompt)))
            problem = storyboard_problem(sb)
        except ValueError as e:
            sb, problem = None, f"not valid JSON ({e})"
        if not problem:
            break
        print(f"  storyboard rejected: {problem}" + ("; asking again" if attempt == 0 else ""))
    else:
        raise SystemExit(f"Storyboard unusable after 2 tries: {problem}")
    sb["title"] = " ".join(str(sb["title"]).split())
    sb["description"] = str(sb.get("description") or idea).strip()
    if (src := re.search(r"\(source: (\S+)\)", idea)) and src.group(1) not in sb["description"]:
        sb["description"] += f"\nSource: {src.group(1)}"
    sb["ending"] = str(sb.get("ending") or (
        ("Tech, explained simply." if TECH else "Every story has a heart.") if VOICED else
        ("Tech, explained without words." if TECH else "Some stories need no words."))).strip()
    if sb.get("music_mood") not in CHORDS:
        sb["music_mood"] = "warm"
    if TECH and sb["music_mood"] in ("sad", "warm"):  # explainer music, not drama music
        sb["music_mood"] = "curious"
    sb["cast"] = [c for c in sb.get("cast") or [] if isinstance(c, dict) and c.get("look")]
    sb["locations"] = [l for l in sb.get("locations") or [] if isinstance(l, dict) and l.get("look")]
    fix_screenplay(sb, idea)
    for s in sb["scenes"]:
        try:
            secs = float(s.get("seconds", 4.5))
        except (TypeError, ValueError):
            secs = 4.5
        s["seconds"] = max(3.0, min(10.0 if FILM else 7.0, secs))
        if s.get("camera") not in CAMERA_MOVES:
            s["camera"] = "zoom_in"
        s["caption"] = _caption(s)
        s["narration"] = " ".join(str(s.get("narration") or "").split()) if VOICED else ""
        s["image_prompt"] = str(s["image_prompt"])
        s.setdefault("query", s["image_prompt"][:40])
        if isinstance(s.get("cast"), str):
            s["cast"] = [s["cast"]]
    time_screenplay(sb["scenes"])
    if VOICED and sum(1 for s in sb["scenes"] if s["narration"]) < len(sb["scenes"]) * 0.7:
        raise SystemExit("Storyboard has no usable narration (the AI left most scenes without a line).")
    sb.setdefault("style", "cinematic film still, soft natural light, 35mm, shallow depth of field")
    if ANIMATED:
        sb["style"] = STYLE  # one channel look, whatever the storyboard answered
    sb.setdefault("characters", "")
    if sb.get("grade") not in GRADES:
        sb["grade"] = "teal_orange"
    sb.setdefault("hashtags", [])
    sb.setdefault("tags", [])
    polish_package(sb)
    return sb


# ================================================================ AI images

_gemini_img_ok = True


def full_prompt(sb: dict, scene: dict | str) -> str:
    """Labelled, structured prompt built from the saved cast/location elements, so every
    shot repeats the exact same character and place descriptions."""
    if isinstance(scene, str):
        scene = {"image_prompt": scene, "cast": [c["id"] for c in sb.get("cast", [])]}
    cast = {c["id"]: c["look"] for c in sb.get("cast", [])}
    names = {c["id"]: c.get("name") or "person" for c in sb.get("cast", [])}
    places = {l["id"]: l["look"] for l in sb.get("locations", [])}
    ids = [i for i in scene.get("cast") or [] if i in cast]
    action = scene.get("image_prompt", "")
    for i in ids:  # "A smiles" -> "the old kite maker smiles": no letters for the model to draw
        action = re.sub(rf"\b{re.escape(i)}\b", f"the {names[i]}", action)
    parts = [f"Shot: {scene.get('shot') or 'medium shot'}", f"Action: {action}"]
    if ANIMATED:  # the look comes first: image models weigh the start of a prompt most
        parts.insert(0, STYLE[0].upper() + STYLE[1:])
    if ids:
        parts.append("Characters: " + "; ".join(f"the {names[i]}: {cast[i]}" for i in ids))
    elif sb.get("characters"):
        parts.append(f"Characters: {sb['characters']}")
    if scene.get("location") in places:
        parts.append(f"Location: {places[scene['location']]}")
    if scene.get("lighting"):
        parts.append(f"Lighting: {scene['lighting']}")
    shape = "widescreen 16:9" if FILM else "vertical 9:16"
    if ANIMATED:
        parts += [f"Colour palette: {sb.get('grade', 'warm').replace('_', ' and ')} tones",
                  f"Format: {shape} frame from an animated film", TOON_ONLY]
    else:
        parts += [f"Style: {sb['style']}", f"Colour grade: {sb.get('grade', 'teal_orange').replace('_', '-')}",
                  f"Format: {shape} cinematic photo"]
    return ". ".join(x.rstrip(".") for x in parts) + f". {NO_TEXT}"


def gen_image_gemini(prompt: str, dest: Path) -> bool:
    global _gemini_img_ok
    if not (_gemini_img_ok and os.getenv("GEMINI_API_KEY") and paid_ok(GEMINI_IMAGE_USD)):
        return False
    model = pick_gemini_model("image")
    if not model:
        _gemini_img_ok = False
        return False
    try:
        r = requests.post(
            f"{GEMINI_BASE}/models/{model}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"responseModalities": ["IMAGE"],
                                       "imageConfig": {"aspectRatio": "16:9" if FILM else "9:16"}}},
            timeout=180,
        )
    except requests.RequestException as e:
        print(f"    gemini-image error: {e}")
        return False
    if r.status_code != 200:
        print(f"    gemini-image {r.status_code}: {r.text[:200]}")
        if r.status_code in (400, 403, 404, 429):
            _gemini_img_ok = False  # quota/model unavailable: stop trying this run
        return False
    for cand in r.json().get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            data = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
            if data:
                dest.write_bytes(base64.b64decode(data))
                log_spend(f"gemini image {model}", GEMINI_IMAGE_USD)
                return True
    return False


_cf_ok = True


CF_IMAGE_MODEL = os.getenv("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b")
# The thumbnail decides whether people click, so it gets the best model the free allowance can carry:
# klein 9B costs ~1,360 of the 10,000 free daily neurons per image, klein 4B ~100-160
# (FLUX.2 [dev] would be ~4,000-6,000: too much for a daily channel).
CF_BEST_MODEL = os.getenv("CF_BEST_MODEL", "@cf/black-forest-labs/flux-2-klein-9b")


def gen_image_cloudflare(prompt: str, dest: Path, seed: int, best: bool = False) -> bool:
    """Cloudflare Workers AI (free daily allowance): FLUX.2 [klein] 4B at the true frame shape
    (9B first when best=True), falling back to FLUX.1 [schnell] (square, cropped by the edit)
    if klein refuses a prompt."""
    global _cf_ok
    acct, token = os.getenv("CF_ACCOUNT_ID"), os.getenv("CF_API_TOKEN")
    if not (_cf_ok and acct and token):
        return False
    base = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run"
    w, h = _gen_size()
    attempts = []
    for model in dict.fromkeys(([CF_BEST_MODEL] if best else []) + [CF_IMAGE_MODEL]):
        if "flux-2" in model:  # FLUX.2 models take multipart form fields, steps fixed at 4
            attempts.append((model, {"files": {"prompt": (None, prompt[:2048]),
                                               "width": (None, str(w)), "height": (None, str(h))}}))
    attempts.append(("@cf/black-forest-labs/flux-1-schnell", {"json": {
        "prompt": f"{prompt} Centered composition, subject in the middle third."[:2048], "steps": 4}}))
    for model, body in attempts:
        try:
            # klein sometimes needs more than 2 minutes when Cloudflare is busy (4 of 11 timed out at 120 s)
            r = requests.post(f"{base}/{model}", headers={"Authorization": f"Bearer {token}"},
                              timeout=180 if "flux-2" in model else 120, **body)
        except requests.RequestException as e:
            print(f"    cloudflare {model.rsplit('/', 1)[-1]} error: {e}")
            continue
        if r.status_code == 200:
            data = (r.json().get("result") or {}).get("image")
            if data:
                dest.write_bytes(base64.b64decode(data))
                return True
        print(f"    cloudflare {model.rsplit('/', 1)[-1]} {r.status_code}: {r.text[:200]}")
        if r.status_code in (401, 403, 429):
            _cf_ok = False  # bad token or daily allowance used up: stop trying this run
            return False
    return False


# free image backups (each optional, each stops itself for the run on auth/quota errors)
_hf_ok = True
_together_ok = True


def _gen_size() -> tuple[int, int]:
    return (1344, 768) if FILM else (768, 1344)  # FLUX-friendly, upscaled by the edit


def gen_image_hf(prompt: str, dest: Path) -> bool:
    """Hugging Face Inference (HF_TOKEN): free monthly credits, then it just refuses."""
    global _hf_ok
    token = os.getenv("HF_TOKEN")
    if not (_hf_ok and token):
        return False
    w, h = _gen_size()
    try:
        r = requests.post("https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"inputs": prompt[:1500], "parameters": {"width": w, "height": h,
                                                                       "num_inference_steps": 4}},
                          timeout=180)
    except requests.RequestException as e:
        print(f"    huggingface error: {e}")
        return False
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
        dest.write_bytes(r.content)
        return True
    print(f"    huggingface {r.status_code}: {r.text[:150]}")
    if r.status_code in (401, 402, 403, 429):
        _hf_ok = False
    return False


def gen_image_together(prompt: str, dest: Path) -> bool:
    """Together AI's free FLUX.1-schnell endpoint (TOGETHER_API_KEY); only the -Free model."""
    global _together_ok
    key = os.getenv("TOGETHER_API_KEY")
    if not (_together_ok and key):
        return False
    w, h = _gen_size()
    try:
        r = requests.post("https://api.together.xyz/v1/images/generations",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": "black-forest-labs/FLUX.1-schnell-Free", "prompt": prompt[:1500],
                                "width": w, "height": h, "steps": 4, "n": 1, "response_format": "b64_json"},
                          timeout=180)
    except requests.RequestException as e:
        print(f"    together error: {e}")
        return False
    if r.status_code == 200:
        try:
            dest.write_bytes(base64.b64decode(r.json()["data"][0]["b64_json"]))
            return True
        except (KeyError, IndexError, ValueError):
            pass
    print(f"    together {r.status_code}: {r.text[:150]}")
    if r.status_code in (401, 402, 403, 404, 429):
        _together_ok = False
    return False


_poll_ok = True


def gen_image_pollinations(prompt: str, dest: Path, seed: int) -> bool:
    global _poll_ok
    if not _poll_ok:
        return False
    url = (f"https://image.pollinations.ai/prompt/{quote(prompt[:900])}"
           f"?width={W}&height={H}&seed={seed}&nologo=true&model=flux")
    token = os.getenv("POLLINATIONS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=180)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                dest.write_bytes(r.content)
                return True
            print(f"    pollinations {r.status_code}")
            if r.status_code in (401, 402, 403):
                _poll_ok = False  # needs a token/paid tier: stop trying this run
                return False
        except requests.RequestException as e:
            print(f"    pollinations error: {e}")
        time.sleep(6 * (attempt + 1))
    return False


# AI Horde: free community GPUs (aihorde.net), no signup. Slow per image, so every image the fast
# sources couldn't make is sent at once and they run in parallel. Only models whose licence allows
# commercial use, and one model per video so the style stays consistent.
HORDE = "https://aihorde.net/api/v2"
HORDE_PARALLEL = int(os.getenv("AI_HORDE_PARALLEL", "4"))
HORDE_MODELS = [  # best first: (name, sampler settings)
    ("Z-Image-Turbo", {"sampler_name": "k_euler", "cfg_scale": 1.0, "steps": 8}),             # Apache-2.0
    ("Flux.1-Schnell fp8 (Compact)", {"sampler_name": "k_euler", "cfg_scale": 1.0, "steps": 4}),  # Apache-2.0
    ("AlbedoBase XL 3.1", {"sampler_name": "k_dpmpp_2m", "cfg_scale": 5.0, "steps": 25}),     # OpenRAIL++
    ("SDXL 1.0", {"sampler_name": "k_dpmpp_2m", "cfg_scale": 6.0, "steps": 25}),              # OpenRAIL++
]


def _horde_headers() -> dict:
    # anonymous key works; a free account key (AI_HORDE_KEY) gets queue priority
    return {"apikey": os.getenv("AI_HORDE_KEY") or "0000000000",
            "Client-Agent": "silent-stories:1.0:github.com/discoverymosti/silent-stories"}


def _horde_fetch(job_id: str, dest: Path, hdr: dict) -> bool:
    try:
        gens = requests.get(f"{HORDE}/generate/status/{job_id}", headers=hdr, timeout=60).json().get("generations") or []
        if not gens or gens[0].get("censored") or not gens[0].get("img"):
            return False
        img = requests.get(gens[0]["img"], timeout=120)
        img.raise_for_status()
    except (requests.RequestException, ValueError):
        return False
    raw = dest.with_suffix(".webp")
    raw.write_bytes(img.content)
    try:
        ff("-i", str(raw), str(dest))
    except subprocess.CalledProcessError:
        return False
    return dest.exists()


def horde_batch(jobs: dict, minutes: float) -> set:
    """jobs = {key: (prompt, dest)}: generate on AI Horde, a few at a time. Returns the keys done."""
    if not jobs or os.getenv("AI_HORDE", "true") == "false":
        return set()
    hdr = _horde_headers()
    try:
        online = {m["name"]: m for m in requests.get(f"{HORDE}/status/models", params={"type": "image"},
                                                     headers=hdr, timeout=60).json()}
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        print(f"  AI Horde unreachable: {e}")
        return set()
    usable = [(n, prm) for n, prm in HORDE_MODELS
              if online.get(n, {}).get("count", 0) > 0 and online[n].get("eta", 99999) < 900]
    if not usable:
        print("  AI Horde: no suitable model online right now")
        return set()
    model, params = usable[0]
    w, h = (1152, 640) if FILM else (640, 1152)  # within the anonymous size limit
    print(f"  AI Horde: {len(jobs)} image(s) on {model} (free community GPUs, can take several minutes)")
    pending, active, done = list(jobs), {}, set()
    deadline = time.time() + minutes * 60
    while (pending or active) and time.time() < deadline:
        while pending and len(active) < HORDE_PARALLEL:
            key, job_id, status = pending[0], None, None
            try:
                r = requests.post(f"{HORDE}/generate/async", headers=hdr, timeout=60, json={
                    "prompt": jobs[key][0][:1000], "params": {**params, "width": w, "height": h, "n": 1},
                    "models": [model], "nsfw": False, "censor_nsfw": True, "r2": True, "slow_workers": True})
                status = r.status_code
                job_id = r.json().get("id") if status in (200, 202) else None
            except (requests.RequestException, ValueError):
                pass
            if not job_id:
                print(f"    AI Horde refused a request ({status}); trying again shortly")
                break  # e.g. too many requests at once: wait for running ones
            active[pending.pop(0)] = job_id
        time.sleep(10)
        for key, job_id in list(active.items()):
            try:
                chk = requests.get(f"{HORDE}/generate/check/{job_id}", headers=hdr, timeout=60).json()
            except (requests.RequestException, ValueError):
                continue
            if chk.get("done") or chk.get("faulted") or chk.get("is_possible") is False:
                del active[key]
                if _horde_fetch(job_id, jobs[key][1], hdr):
                    done.add(key)
    for job_id in active.values():  # out of time: cancel so the volunteers' queue is freed
        try:
            requests.delete(f"{HORDE}/generate/status/{job_id}", headers=hdr, timeout=30)
        except requests.RequestException:
            pass
    print(f"  AI Horde: {len(done)}/{len(jobs)} image(s) ready")
    return done


def gen_image(prompt: str, dest: Path, seed: int, best: bool = False) -> str | None:
    if dest.exists():
        return "cached"
    if gen_image_gemini(prompt, dest):
        return "gemini"
    if gen_image_cloudflare(prompt, dest, seed, best):
        return "cloudflare"
    if gen_image_hf(prompt, dest):
        return "huggingface"
    if gen_image_together(prompt, dest):
        return "together"
    if gen_image_pollinations(prompt, dest, seed):
        time.sleep(3)  # be polite to the free endpoint
        return "pollinations"
    return None


# ================================================================ stock footage (fallback)

def pexels_search(query: str, used: set[int]) -> str | None:
    key = os.getenv("PEXELS_API_KEY")
    if not key or ANIMATED:  # Pexels has no animation filter: its clips show real people
        return None
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                     params={"query": query, "orientation": "landscape" if FILM else "portrait", "per_page": 15}, timeout=30)
    if r.status_code != 200:
        return None
    for v in r.json().get("videos", []):
        if v["id"] in used or v.get("duration", 0) < 3:
            continue
        files = [f for f in v["video_files"] if f.get("height") and f.get("width")
                 and (f["width"] >= f["height"]) == FILM and max(f["width"], f["height"]) >= 1280
                 and f.get("file_type") == "video/mp4"]
        if files:
            used.add(v["id"])
            return min(files, key=lambda f: abs(max(f["width"], f["height"]) - 1920))["link"]
    return None


def pixabay_search(query: str, used: set[int]) -> str | None:
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        return None
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": key, "q": query[:100], "per_page": 20, "safesearch": "true",
                             "video_type": "animation" if ANIMATED else "all"}, timeout=30)
    if r.status_code != 200:
        return None
    hits = [h for h in r.json().get("hits", []) if h["id"] not in used and h.get("duration", 0) >= 3]
    # prefer clips whose orientation matches the frame
    hits.sort(key=lambda h: (h["videos"]["medium"].get("height", 0) > h["videos"]["medium"].get("width", 1)) == FILM)
    for h in hits:
        for size in ("large", "medium"):
            v = h["videos"].get(size) or {}
            if v.get("url") and min(v.get("width", 0), v.get("height", 0)) >= 720:
                used.add(h["id"])
                return v["url"]
    return None


def fetch_clip(query: str, dest: Path, used: set[int]) -> bool:
    if dest.exists():
        return True
    words = query.split() or ["nature"]
    for q in dict.fromkeys((query, " ".join(words[-2:]), words[-1])):
        url = pixabay_search(q, used) or pexels_search(q, used)
        if url:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            return True
    return False


# ================================================================ render helpers

def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS)]
GRADES = {  # one grade per film keeps every shot's colour consistent
    "warm":        "colorbalance=rs=.05:gs=.02:bs=-.05:rm=.04:bm=-.04",
    "cool":        "colorbalance=rs=-.04:bs=.06:rm=-.03:bm=.05",
    "teal_orange": "colorbalance=rs=-.05:bs=.07:rh=.07:gh=.02:bh=-.07",
    "golden":      "colorbalance=rm=.06:gm=.03:bm=-.06:rh=.05:bh=-.05",
    "muted":       "eq=saturation=0.82",
    "noir":        "hue=s=0,eq=contrast=1.12",
}
# live action gets a film look (vignette, grain); animation stays clean, bright and saturated
_BASE_LOOK = ("eq=contrast=1.05:saturation=1.16" if ANIMATED
              else "eq=contrast=1.06:saturation=1.03,vignette=PI/5,noise=alls=3:allf=t")
LOOK = _BASE_LOOK


def set_grade(grade: str) -> None:
    global LOOK
    LOOK = f"{GRADES.get(grade, GRADES['teal_orange'])},{_BASE_LOOK}"


def font_file(bold: bool = True) -> str:
    if FONTS_DIR:
        name = FONT.replace(" ", "")
        for cand in (f"{name}-{'Bold' if bold else 'Regular'}.ttf", f"{name}{'-Bold' if bold else ''}.ttf"):
            if (Path(FONTS_DIR) / cand).exists():
                return str(Path(FONTS_DIR) / cand)
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", f"{FONT}:{'bold' if bold else 'regular'}"],
                             capture_output=True, text=True, check=True).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.CalledProcessError):
        pass
    for pattern in ("NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "*Bold*.ttf", "*.ttf"):
        hits = sorted(Path("/usr/share/fonts").rglob(pattern))
        if hits:
            return str(hits[0])
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def kenburns(img: Path, seconds: float, motion: str, dest: Path) -> None:
    n = int(round(seconds * FPS))
    z, z0 = (0.18, 1.03) if ANIMATED else (0.14, 1.0)
    moves = {
        "zoom_in":   (f"{z0}+{z}*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        "zoom_out":  (f"{z0}+{z}-{z}*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        "pan_left":  (f"1+{z}", f"(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),
        "pan_right": (f"1+{z}", f"(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),
        "pan_up":    (f"1+{z}", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-on/{n})"),
    }
    zz, xx, yy = moves.get(motion, moves["zoom_in"])
    if ANIMATED:  # a slow float across the move's free axis, so a still picture feels alive
        bob = f"+{W // 90}*sin(2*PI*on/{int(FPS * 2.6)})"
        if motion == "pan_up":
            xx += bob
        else:
            yy += bob
    # upscale first so the slow zoom stays sharp and jitter-free
    vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
          f"zoompan=z='{zz}':x='{xx}':y='{yy}':d={n}:s={W}x{H}:fps={FPS},setsar=1,{LOOK}")
    ff("-i", str(img), "-vf", vf, "-frames:v", str(n), *ENC, str(dest))


def clip_scene(src: Path, seconds: float, dest: Path) -> None:
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},{LOOK}"
    ff("-ss", "0.3", "-t", f"{seconds:.2f}", "-i", str(src), "-vf", vf, "-an", *ENC, str(dest))


def reframe_scene(img: Path, seconds: float, idx: int, dest: Path) -> None:
    """A missing shot rebuilt from a neighbouring image: a punch-in on another part of the frame
    with its own camera move (a standard edit), instead of abstract filler."""
    x, y = [("0", "0"), ("iw*0.3", "ih*0.15"), ("iw*0.15", "ih*0.3")][idx % 3]
    crop = dest.with_suffix(".png")
    ff("-i", str(img), "-vf", f"crop=iw*0.7:ih*0.7:{x}:{y}", str(crop))
    kenburns(crop, seconds, CAMERA_MOVES[(idx + 2) % len(CAMERA_MOVES)], dest)


def gradient_scene(seconds: float, dest: Path, idx: int) -> None:
    hue = (idx * 37) % 360
    vf = (f"geq=r='40+30*sin(2*PI*(X/W+T/8))':g='30+20*sin(2*PI*(Y/H+T/9))':b='60+40*cos(2*PI*(X/W-T/7))',"
          f"hue=h={hue},scale={W}:{H}:flags=bicubic")
    ff("-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"color=c=black:s={W // 10}x{H // 10}:r={FPS}",
       "-vf", vf, *ENC, str(dest))


def end_card(seconds: float, dest: Path) -> None:
    ff("-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"color=c=0x0b0b0f:s={W}x{H}:r={FPS}",
       "-vf", "vignette=PI/4", *ENC, str(dest))


def crossfade_all(parts: list[Path], lengths: list[float], dest: Path, fade_ends: bool = True) -> list[float]:
    """Join clips with crossfades. Returns the start time of each clip in the output.
    fade_ends=False (Shorts): no fade from/to black, so frame 1 is the hook and replays loop."""
    starts, t = [], 0.0
    for i, L in enumerate(lengths):
        starts.append(t)
        t += L - XF
    inputs = sum((["-i", str(p)] for p in parts), [])
    chain, prev = [], "[0:v]"
    for i in range(1, len(parts)):
        trans = "fadeblack" if fade_ends and i == len(parts) - 1 else random.choice(
            ["fade", "slideleft", "slideup", "smoothleft", "circleopen"] if ANIMATED and not FILM
            else ["fade", "fade", "dissolve", "smoothleft"])
        out = f"[v{i}]"
        chain.append(f"{prev}[{i}:v]xfade=transition={trans}:duration={XF}:offset={starts[i]:.3f}{out}")
        prev = out
    chain.append(f"{prev}{'fade=t=in:st=0:d=0.4,' if fade_ends else ''}format=yuv420p[vout]")
    ff(*inputs, "-filter_complex", ";".join(chain), "-map", "[vout]", *ENC, str(dest))
    return starts


def ass_time(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_captions(scenes: list[dict], starts: list[float], ending: str,
                   end_start: float, end_len: float, dest: Path, title: str = "",
                   title_len: float = 0.0) -> None:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},{64 if FILM else 78},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,5,3,2,90,90,{90 if FILM else 520},1
Style: End,{FONT},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,6,2,5,110,110,0,1
Style: Title,{FONT},96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,0,5,160,160,0,1
Style: Tag,{FONT},44,&H00B4B4B4,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,4,0,1,3,0,2,90,90,{220 if FILM else 300},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if title:  # film opening card: title, then the channel credit
        lines.append(f"Dialogue: 0,{ass_time(0.4)},{ass_time(title_len - 0.2)},Title,,0,0,0,,"
                     f"{{\\fad(700,500)}}{ass_escape(title)}")
        lines.append(f"Dialogue: 0,{ass_time(1.1)},{ass_time(title_len - 0.2)},Tag,,0,0,0,,"
                     f"{{\\fad(700,500)}}{ass_escape(('a ' + CHANNEL_NAME + (' film' if VOICED else ' silent film')).upper())}")
    for s, t in zip(scenes, starts):
        cap = ass_escape(s.get("caption", "").strip())
        if cap:
            a, b = t + XF * 0.6, t + s["seconds"] - XF
            fx = ("\\fad(90,180)\\fscx55\\fscy55\\t(0,170,\\fscx114\\fscy114)\\t(170,300,\\fscx100\\fscy100)"
                  if ANIMATED else  # cartoon captions pop in with a bounce
                  "\\fad(280,220)\\t(0,280,\\fscx104\\fscy104)\\t(280,500,\\fscx100\\fscy100)")
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,{{{fx}}}{cap}")
    lines += progress_dots(scenes, starts, end_start)
    a, b = end_start + 0.5, end_start + end_len - 0.3
    lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},End,,0,0,0,,{{\\fad(600,400)}}{ass_escape(ending)}")
    lines.append(f"Dialogue: 0,{ass_time(a + 0.8)},{ass_time(b)},Tag,,0,0,0,,{{\\fad(600,400)}}"
                 f"{ass_escape(CHANNEL_NAME.upper())}")
    dest.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def progress_dots(scenes: list[dict], starts: list[float], end_start: float) -> list[str]:
    """Countdown ("list") videos: a row of pills under the captions, one per numbered item ("#1 ...");
    items reached so far are filled, so viewers see how many are left and stay for the last one."""
    nums = [int(m.group(1)) if (m := re.match(r"\s*#\s*0*(\d{1,2})\b", s.get("caption", ""))) else 0
            for s in scenes]
    n = max(nums, default=0)
    if not 3 <= n <= 12 or set(nums) - {0} != set(range(1, n + 1)):
        return []  # not a clean countdown: no bar rather than a wrong one
    w, h, gap = (48, 16, 14) if FILM else (64, 20, 18)
    x0, y = (W - (n * w + (n - 1) * gap)) // 2, (H - 70) if FILM else (H - 470)
    first = next(i for i, x in enumerate(nums) if x)
    lines, seen = [], set()
    for i in range(first, len(scenes)):
        seen.add(nums[i] or next(iter(seen)))  # counts up or down alike: fill one pill per item shown
        cur = len(seen)
        a = starts[i] + (XF * 0.6 if i > first else 0)
        b = starts[i + 1] + XF * 0.6 if i + 1 < len(scenes) else end_start
        for k in range(n):
            fill = "\\1c&H33E6FF&\\1a&H00&" if k < cur else "\\1c&HFFFFFF&\\1a&H90&"
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,{{\\an7\\pos({x0 + k * (w + gap)},{y})"
                         f"\\bord3\\shad0\\3c&H000000&{fill}\\p1}}m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}")
    return lines


_C, _G, _AM, _F = (261.6, 329.6, 392.0), (196.0, 246.9, 293.7), (220.0, 261.6, 329.6), (174.6, 220.0, 261.6)
_DM, _E, _EM, _D = (146.8, 174.6, 220.0), (164.8, 207.7, 246.9), (164.8, 196.0, 246.9), (146.8, 185.0, 220.0)
CHORDS = {  # 4-chord progression per mood (original synthesized music, no copyright)
    "warm":      [_C, _G, _AM, _F],
    "sad":       [_AM, _F, _C, _G],
    "tense":     [_AM, _F, _DM, _E],
    "curious":   [_F, _G, _EM, _AM],
    "uplifting": [_G, _D, _EM, _C],
}


AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}


def bgm_pool(mood: str) -> list[Path]:
    """Your own tracks for this mood: music/<mood>/ first, the tracks in music/ itself only when that
    mood has none (so a sad story never gets the upbeat track just because both are there)."""
    def tracks(folder: Path) -> list[Path]:
        return sorted(f for f in folder.glob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXT) \
            if folder.is_dir() else []
    return tracks(MUSIC_DIR / mood) or tracks(MUSIC_DIR)


def audio_seconds(track: Path) -> float:
    """Length of a track's audio in seconds (0 if it can't be read)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "format=duration", "-of", "csv=p=0", str(track)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


def bgm_track(track: Path, seconds: float, dest: Path) -> None:
    """Cut one of your tracks to the video: audio only (downloaded tracks often carry cover art),
    silence at its start skipped, looped if it is shorter than the video, levelled to one loudness
    so every upload sounds the same, faded in and out."""
    length = audio_seconds(track)
    if length < 5:
        raise ValueError("no usable audio (shorter than 5 s)")
    # a long track doesn't always start at its intro: any point that still leaves enough music
    start = random.uniform(0, length - seconds - 5) if length > seconds + 20 else 0.0
    loop = ["-stream_loop", "-1"] if length - start < seconds + 1 else []
    af = ("silenceremove=start_periods=1:start_threshold=-50dB,"  # no dead air before the first note
          f"atrim=duration={seconds:.2f},loudnorm=I=-16:TP=-1.5:LRA=11,"
          f"afade=t=in:d={0.3 if start == 0 else 1.2},afade=t=out:st={max(0.0, seconds - 2.5):.2f}:d=2.5,"
          f"apad=whole_dur={seconds:.2f}")  # never shorter than the picture
    ff(*loop, "-ss", f"{start:.2f}", "-i", str(track), "-map", "0:a:0", "-vn", "-af", af,
       "-t", f"{seconds:.2f}", "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "192k", str(dest))
    if audio_seconds(dest) < seconds - 1.5:
        raise ValueError("the cut came out shorter than the video")


def make_music(mood: str, seconds: float, dest: Path) -> str:
    """Background music: one of your royalty-free tracks from music/ if there are any, else an
    original synthesized pad. A track that can't be used is skipped, never a reason to lose the video."""
    pool = bgm_pool(mood)
    random.shuffle(pool)
    for track in pool[:4]:
        try:
            bgm_track(track, seconds, dest)
            return track.name
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            print(f"  music: {track.name} can't be used ({str(e)[:120]}); trying another")
    if pool:
        print("  music: none of the tracks in music/ worked; using the built-in score")
    fade_out = max(0.0, seconds - 2.5)
    chords = CHORDS.get(mood, CHORDS["warm"])
    bar, step = 4.0, 0.5  # 4 s per chord; arpeggio note every 0.5 s
    cycle = bar * len(chords)
    tones = []
    for k, chord in enumerate(chords):
        gate = f"between(mod(t,{cycle}),{k * bar},{(k + 1) * bar - 0.001})"
        for n, f in enumerate(chord):
            tones.append(f"0.07*sin(2*PI*{f / 2:.2f}*t)*{gate}")  # warm pad, an octave down
            tones.append(f"0.03*sin(2*PI*{f:.2f}*t+0.3*sin(2*PI*0.2*t))*{gate}")
            # soft plucked arpeggio an octave up, cycling through the chord tones
            tones.append(f"0.035*sin(2*PI*{f * 2:.2f}*t)*exp(-5*mod(t,{step}))"
                         f"*eq(mod(floor(t/{step}),3),{n})*{gate}")
    af = (f"lowpass=f=1800,aecho=0.8:0.85:500|900:0.35|0.25,"
          f"tremolo=f=0.25:d=0.3,afade=t=in:d=2,afade=t=out:st={fade_out:.2f}:d=2.5,volume=1.6")
    ff("-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"aevalsrc='{'+'.join(tones)}':s=44100:c=stereo",
       "-af", af, "-c:a", "aac", "-b:a", "192k", str(dest))
    return "original synthesized score"


# ================================================================ narration (the owner's recorded voice)

VOICE_EXT = AUDIO_EXT | {".webm", ".3gp", ".amr", ".mp4", ".mov", ".caf", ".wma", ".aiff"}  # phone recorders


def slug_for(fmt: str, idea: str) -> str:
    """Folder name of a story's script: readable start + a short fingerprint of the idea."""
    import hashlib
    words = re.sub(r"[^a-z0-9]+", "-", idea.lower()).strip("-")[:36].strip("-")
    return f"{fmt}-{words}-{hashlib.sha1(idea.encode('utf-8')).hexdigest()[:6]}"


def voice_recording(folder: Path) -> Path | None:
    files = sorted(f for f in folder.glob("*") if f.is_file() and f.suffix.lower() in VOICE_EXT)
    return files[0] if files else None


def script_words(sb: dict) -> int:
    return sum(len(s.get("narration", "").split()) for s in sb["scenes"])


def write_script(folder: Path, sb: dict, genre: str, idea: str) -> None:
    """The narration to record, as a page that reads well on github.com, plus the saved storyboard
    (the video is later built from exactly this storyboard, so the pictures match what was read)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "storyboard.json").write_text(json.dumps(
        {"format": FORMAT, "genre": genre, "idea": idea, "storyboard": sb}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    secs = script_words(sb) / WORDS_PER_SECOND
    lines = [f"{i}. {s['narration']}" for i, s in enumerate((x for x in sb["scenes"] if x["narration"]), 1)]
    (folder / "script.md").write_text(f"""# {sb['title']}

**Record this in your own voice** ({'weekly film' if FILM else 'Short'}, about {secs:.0f} seconds at a calm pace).

How to record
- One recording, read from line 1 to the last line, in this order. Any phone voice recorder works.
- Tell it like a story to a friend. Take a short breath between lines; that pause is where the picture changes.
- You may change words to sound like yourself. Keep the order of the lines and roughly their length.
- A quiet room, the phone a hand's width from your mouth. No music: the pipeline adds it.

Then upload the audio file **into this folder** (on github.com: open this folder → *Add file* → *Upload files*).
The next publishing run builds the video around your voice and uploads it.

## Script

{chr(10).join(lines)}

---
Story idea: {idea}
""", encoding="utf-8")


def detect_pauses(voice: Path) -> list[float]:
    """Middle of every pause (0.22 s or longer) in the cleaned voice track."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(voice), "-af", "silencedetect=noise=-32dB:d=0.22",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", err)]
    return [(a + b) / 2 for a, b in zip(starts, ends)]


def prepare_voice(recording: Path, sb: dict, work: Path) -> Path:
    """Clean the recording (silence at both ends cut, rumble and hiss reduced, one loudness) and time
    every scene to it: each scene lasts as long as its lines take to say, with the cut moved onto the
    nearest pause in the voice. Raises ValueError when the file can't be this script's recording."""
    work.mkdir(parents=True, exist_ok=True)
    voice = work / "voice.wav"
    trim = "silenceremove=start_periods=1:start_threshold=-42dB:start_silence=0.05"
    ff("-i", str(recording), "-map", "0:a:0", "-vn", "-af",
       f"highpass=f=80,afftdn=nf=-28,{trim},areverse,{trim},areverse,loudnorm=I=-16:TP=-1.5:LRA=11",
       "-ar", "44100", "-ac", "2", str(voice))
    length, expected = audio_seconds(voice), script_words(sb) / WORDS_PER_SECOND
    if not expected * 0.4 <= length <= expected * 2.6:
        raise ValueError(f"it is {length:.0f} s long, but this script takes about {expected:.0f} s to read: "
                         "is it the right recording, complete, and without long silences?")
    scenes = sb["scenes"]
    # share of the recording per scene: its letters, plus a breath; a scene without a line gets a short beat
    weight = [len(s["narration"]) + 8 if s["narration"] else 22 for s in scenes]
    cuts, t = [], 0.0
    for w in weight[:-1]:
        t += length * w / sum(weight)
        cuts.append(t)
    pauses, moved = detect_pauses(voice), 0
    for i, c in enumerate(cuts):  # put each cut on a real pause when one is close
        lo = (cuts[i - 1] if i else 0.0) + 1.2
        hi = (cuts[i + 1] if i + 1 < len(cuts) else length) - 1.2
        near = [x for x in pauses if abs(x - c) <= 1.1 and lo <= x <= hi]
        if near:
            cuts[i] = min(near, key=lambda x: abs(x - c))
            moved += 1
    edges = [0.0] + cuts + [length]
    for i, s in enumerate(scenes):
        s["seconds"] = round(max(1.5, edges[i + 1] - edges[i]) + (VOICE_LEAD if i == 0 else 0.0), 2)
    print(f"  voice: {recording.name}, {length:.1f} s (script about {expected:.0f} s); "
          f"{moved} of {len(cuts)} picture changes sit on a pause in your voice")
    return voice


def voice_sample() -> Path | None:
    """The clip of the owner's voice that every narration is cloned from: voice/sample.* of this
    channel, else the main channel's."""
    for folder in dict.fromkeys([VOICE_DIR, ROOT / "voice"]):
        files = sorted(f for f in folder.glob("sample.*") if f.is_file() and f.suffix.lower() in VOICE_EXT)
        if files:
            return files[0]
    return None


def speak_lines(lines: list[str], sample: Path, out: Path) -> None:
    """Every line spoken in the sample's voice: out/line_NN.wav (scripts/clone_voice.py)."""
    (out / "lines.json").write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    subprocess.run([sys.executable, str(Path(__file__).with_name("clone_voice.py")), str(sample),
                    str(out / "lines.json"), str(out), NARRATION_LANGUAGE], check=True, timeout=5400)


def cloned_voice(sb: dict, sample: Path, work: Path) -> Path:
    """The narration spoken in the owner's cloned voice, one take per scene, and every scene timed to
    its own line: the picture changes exactly when the next sentence starts. Raises ValueError /
    CalledProcessError when the voice can't be made."""
    out = work / "voice"
    out.mkdir(parents=True, exist_ok=True)
    clean = out / "sample.wav"  # mono, at most 30 s, rumble and hiss reduced: what the model listens to
    ff("-i", str(sample), "-map", "0:a:0", "-vn", "-af",
       "highpass=f=80,afftdn=nf=-28,silenceremove=start_periods=1:start_threshold=-42dB:"
       "stop_periods=-1:stop_duration=0.7:stop_threshold=-42dB,loudnorm=I=-18:TP=-2",  # long gaps closed up
       "-t", "30", "-ar", "44100", "-ac", "1", str(clean))
    if audio_seconds(clean) < 6:
        raise ValueError(f"the voice sample {sample.name} has under 6 seconds of speech; 15-30 seconds work best")
    scenes = sb["scenes"]
    speak_lines([s["narration"] for s in scenes], clean, out)
    trim = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.03"
    # every take: hiss and rumble reduced, silence cut off both ends, and long pauses inside the
    # line shortened to a natural breath
    clean_take = (f"highpass=f=70,afftdn=nf=-30,{trim},areverse,{trim},areverse,"
                  "silenceremove=stop_periods=-1:stop_duration=0.3:stop_threshold=-45dB")  # pauses: 0.3 s at most
    pre, post, takes = 0.05, 0.2, {}
    for i, s in enumerate(scenes):
        if s["narration"]:
            takes[i] = out / f"take_{i:02d}.wav"
            ff("-i", str(out / f"line_{i:02d}.wav"), "-af", clean_take, "-ar", "44100", "-ac", "1", str(takes[i]))
            if audio_seconds(takes[i]) < 0.4:
                raise ValueError(f"line {i + 1} came out empty")
    # fit the whole narration into the planned running time: a slightly brisker delivery (same pitch)
    # when the takes run long, never more than 18% faster
    raw = sum(audio_seconds(t) + pre + post for t in takes.values()) + 2.5 * (len(scenes) - len(takes))
    tempo = min(1.18, max(1.0, raw / VOICE_SECONDS))
    if tempo > 1.02:
        print(f"  voice: the takes add up to {raw:.0f} s; spoken {round((tempo - 1) * 100)}% brisker to fit")
    pieces = []
    for i, s in enumerate(scenes):
        piece = out / f"scene_{i:02d}.wav"
        if i in takes:
            spoken = audio_seconds(takes[i]) / (tempo if tempo > 1.02 else 1.0)
            s["seconds"] = round(max(1.8, pre + spoken + post), 2)
            fx = (f"atempo={tempo:.3f}," if tempo > 1.02 else "") + (
                f"afade=t=in:d=0.02,afade=t=out:st={max(0.0, spoken - 0.05):.2f}:d=0.05,"
                f"adelay={int(pre * 1000)},apad=whole_dur={s['seconds']:.2f}")
            ff("-i", str(takes[i]), "-af", fx, "-t", f"{s['seconds']:.2f}", str(piece))
        else:  # a scene without a line: a short beat of music only
            s["seconds"] = round(min(s["seconds"], 2.5), 2)
            ff("-f", "lavfi", "-t", f"{s['seconds']:.2f}", "-i", "anullsrc=r=44100:cl=mono", str(piece))
        pieces.append(piece)
    scenes[0]["seconds"] = round(scenes[0]["seconds"] + VOICE_LEAD, 2)  # the voice track starts VOICE_LEAD in
    voice = work / "voice.wav"
    ff(*sum((["-i", str(x)] for x in pieces), []), "-filter_complex",
       "".join(f"[{i}:a]" for i in range(len(pieces))) + f"concat=n={len(pieces)}:v=0:a=1,"
       "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "2", str(voice))
    print(f"  voice: {sum(1 for s in scenes if s['narration'])} lines in your cloned voice, "
          f"{audio_seconds(voice):.1f} s (sample: {sample.name})")
    return voice


def mix_voice(voice: Path, music: Path, lead: float, total: float, dest: Path) -> None:
    """Voice over the music: the music drops while you speak and comes back up in the gaps and at the end."""
    ms = int(lead * 1000)
    graph = (f"[0:a]adelay={ms}|{ms},apad=whole_dur={total:.2f},asplit[v][key];"
             f"[1:a]volume=0.5[m];[m][key]sidechaincompress=threshold=0.05:ratio=4:attack=40:release=700[duck];"
             f"[duck][v]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    ff("-i", str(voice), "-i", str(music), "-filter_complex", graph, "-map", "[a]", "-t", f"{total:.2f}",
       "-c:a", "aac", "-b:a", "192k", str(dest))


def watermark_filter() -> str:
    """Semi-transparent channel name. Shorts: top-left, clear of the Shorts UI; films: bottom-right."""
    if not WATERMARK:
        return ""
    pos = "x=w-tw-44:y=h-th-36" if FILM else "x=48:y=90"
    return (f",drawtext=fontfile='{font_file(True)}':text='{drawtext_escape(WATERMARK)}':"
            f"fontsize={40 if FILM else 46}:fontcolor=white@0.62:borderw=2:bordercolor=black@0.45:{pos}")


def drawtext_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def make_thumbnail(sb: dict, src: Path | None, dest: Path) -> None:
    """Frame-sized JPEG (1080x1920 Short / 1920x1080 film): AI image + big punchy text with dark gradient for readability."""
    text = (sb.get("thumbnail_text") or sb["title"]).upper()
    words, lines, cur = text.split(), [], ""
    for w in words:  # wrap ~12 chars per line
        if len(cur) + len(w) + 1 > (18 if FILM else 12) and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    ffile = font_file(True)
    draws = []
    for i, line in enumerate(lines[:3]):
        y = 220 + i * 170
        draws.append(f"drawtext=fontfile='{ffile}':text='{drawtext_escape(line)}':fontsize=150:"
                     f"fontcolor=white:borderw=10:bordercolor=black@0.85:x=(w-text_w)/2:y={y}")
    grad = "drawbox=x=0:y=0:w=iw:h=760:color=black@0.35:t=fill"
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},eq=contrast=1.1:saturation=1.15,{grad},{','.join(draws)}"
    if src and src.exists():
        ff("-i", str(src), "-vf", vf, "-frames:v", "1", "-q:v", "3", str(dest))
    else:
        ff("-f", "lavfi", "-i", f"color=c=0x1e1b4b:s={W}x{H}", "-vf", vf, "-frames:v", "1", "-q:v", "3", str(dest))
    while dest.stat().st_size > 1_900_000:  # YouTube thumbnail limit 2 MB
        ff("-i", str(dest), "-q:v", "6", str(dest.with_suffix(".tmp.jpg")))
        dest.with_suffix(".tmp.jpg").replace(dest)


# ================================================================ build

def finish_video(sb: dict, parts: list[Path], lengths: list[float], work: Path, report: dict,
                 intro_len: float = 0.0, voice: Path | None = None) -> tuple[Path, float]:
    """Crossfade the scene clips, then add captions, original music and the watermark."""
    joined = work / "joined.mp4"
    starts = crossfade_all(parts, lengths, joined, fade_ends=FILM)
    total = starts[-1] + lengths[-1]
    subs = work / "captions.ass"
    scene_starts = starts[1:] if FILM else starts
    end_start = starts[-1] if FILM else starts[-1] + sb["scenes"][-1]["seconds"] - 0.2
    write_captions(sb["scenes"], scene_starts, sb["ending"], end_start, total - end_start, subs,
                   title=sb["title"] if FILM else "", title_len=intro_len)
    music = work / "music.m4a"
    report["music"] = make_music(sb.get("music_mood", "warm"), total, music)
    if voice:  # the first scene starts where the film's title card ends
        mixed = work / "audio.m4a"
        mix_voice(voice, music, scene_starts[0] + VOICE_LEAD, total, mixed)
        music, report["voice"] = mixed, sb.get("voice", "owner's own narration")

    final = work / "short.mp4"
    ff("-i", str(joined), "-i", str(music), "-map", "0:v:0", "-map", "1:a:0",
       "-vf", f"ass={subs.as_posix()}" + (f":fontsdir={FONTS_DIR}" if FONTS_DIR else "") + watermark_filter(), *ENC,
       "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(final))
    return final, total


def portrait_scene(img: Path, seconds: float, dest: Path) -> None:
    """A widescreen film picture in a vertical frame: the whole picture, sharp, slowly zooming in
    the middle, over a blurred and darkened copy of itself (captions sit on the blur below)."""
    n = int(round(seconds * FPS))
    fh = int(W * 768 / 1344) // 2 * 2
    graph = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
             f"boxblur=40:3,eq=brightness=-0.16:saturation=0.85[bg];"
             f"[b]scale={W * 2}:{fh * 2},zoompan=z='1+0.07*on/{n}':x='iw/2-(iw/zoom/2)':"
             f"y='ih/2-(ih/zoom/2)':d=1:s={W}x{fh}:fps={FPS}[fg];"
             f"[bg][fg]overlay=0:(H-h)/2-120,setsar=1,{LOOK}")
    ff("-loop", "1", "-framerate", str(FPS), "-t", f"{seconds:.3f}", "-i", str(img),
       "-filter_complex", graph, "-frames:v", str(n), *ENC, str(dest))


def build_video(sb: dict, work: Path, recording: Path | None = None,
                sample: Path | None = None) -> tuple[Path, dict]:
    """recording: the owner's own reading of this storyboard's narration; sample: the clip their
    voice is cloned from (the narration is then spoken by AI in that voice)."""
    work.mkdir(parents=True, exist_ok=True)
    voice = prepare_voice(recording, sb, work) if recording else cloned_voice(sb, sample, work) if sample else None
    used: set[int] = set()
    parts, lengths, report = [], [], {"ai": 0, "stock": 0, "reused": 0, "fallback": 0}
    base_seed = random.randint(1, 10_000_000)
    set_grade(sb.get("grade", "teal_orange"))
    intro_len = 3.5 + XF if FILM else 0.0
    if FILM:
        intro = work / "intro.mp4"
        end_card(intro_len, intro)
        parts.append(intro)
        lengths.append(intro_len)
    scenes = sb["scenes"]
    imgs = [work / f"img_{i:02d}.png" for i in range(len(scenes))]
    thumb_img = work / "thumb_src.png"
    sources: dict = {}
    if VISUAL_MODE == "ai":  # all images first: fast sources one by one, then AI Horde in one batch
        for i, s in enumerate(scenes):
            sources[i] = gen_image(full_prompt(sb, s), imgs[i], base_seed + i)
        if sb.get("thumbnail_prompt"):
            sources["thumb"] = gen_image(full_prompt(sb, sb["thumbnail_prompt"]), thumb_img, base_seed + 999,
                                         best=True)
        missing = {k: (full_prompt(sb, scenes[k] if k != "thumb" else sb["thumbnail_prompt"]),
                       imgs[k] if k != "thumb" else thumb_img) for k, v in sources.items() if not v}
        for k in horde_batch(missing, minutes=80 if FILM else 45):
            sources[k] = "ai-horde"
    hold = 0.0 if FILM else 2.8  # Shorts hold the last image for the closing line instead of a black card
    for i, s in enumerate(scenes):
        L = s["seconds"] + XF  # extra overlap for crossfade
        if i == len(scenes) - 1:
            L += hold
        part = work / f"scene_{i:02d}.mp4"
        raw = work / f"raw_{i:02d}.mp4"
        src = sources.get(i)
        if src:
            kenburns(imgs[i], L, s["camera"], part)
            report["ai"] += 1
        elif fetch_clip(s.get("query") or s["image_prompt"][:40], raw, used):
            clip_scene(raw, L, part)
            src = "stock"
            report["stock"] += 1
        elif (near := next((j for d in range(1, len(scenes)) for j in (i - d, i + d)
                            if 0 <= j < len(scenes) and sources.get(j) and imgs[j].exists()), None)) is not None \
                and report["reused"] < max(1, len(scenes) // 4):  # at most about a quarter of the shots
            reframe_scene(imgs[near], L, i, part)
            src = f"reframe {near + 1}"
            report["reused"] += 1
        else:
            gradient_scene(L, part, i)
            src = "fallback"
            report["fallback"] += 1
        print(f"  scene {i + 1:2d} [{src:12}] {s['camera']:9} {str(s.get('shot', ''))[:11]:11} | {s.get('caption', '')}")
        parts.append(part)
        lengths.append(L)

    if FILM:  # films close on a dark end card; Shorts end on the image so a replay loops
        end = work / "end.mp4"
        end_card(4.0 + XF, end)
        parts.append(end)
        lengths.append(4.0 + XF)

    final, total = finish_video(sb, parts, lengths, work, report, intro_len, voice)
    if not (sources.get("thumb") and thumb_img.exists()):
        thumb_img = imgs[0]
    try:
        make_thumbnail(sb, thumb_img if thumb_img.exists() else None, work / "thumbnail.jpg")
    except subprocess.CalledProcessError as e:
        print(f"  thumbnail text failed ({e}); using plain first frame")
        ff("-ss", "1", "-i", str(final), "-frames:v", "1", "-q:v", "3", str(work / "thumbnail.jpg"))
    report["duration"] = round(total, 1)
    return final, report


# ================================================================ metadata

def clean_tag(t: str) -> str:
    return re.sub(r"[<>\"]", "", t.lstrip("#")).strip()


def build_metadata(sb: dict, report: dict, genre: str = "story") -> dict:
    hashtags = [("#" + re.sub(r"[^\w]", "", h.lstrip("#"))) for h in sb.get("hashtags", []) if h.strip()]
    hashtags = list(dict.fromkeys(hashtags + [CHANNEL_TAG, "#ShortFilm" if FILM else "#Shorts"]))[:6]
    tags, total = [], 0
    extra = ["tech explained", "technology", "tech shorts"] if TECH else (["narrated story", "storytelling", "short film"] if VOICED else ["silent stories", "no words story", "short film"])
    if ANIMATED:
        extra += ["animation", "cartoon", "animated explainer" if TECH else "animated story"]
    for t in [clean_tag(x) for x in sb.get("tags", [])] + [CHANNEL_NAME] + extra:
        if t and t.lower() not in (x.lower() for x in tags) and total + len(t) + 3 < 480:
            tags.append(t)
            total += len(t) + 3
    description = f"{sb['description'].strip()}\n\n{' '.join(hashtags)}"
    title = sb["title"].strip()[:90]
    return {
        "title": title,
        "description": description[:4900],
        "tags": tags,
        "hashtags": hashtags,
        "social_caption": sb.get("social_caption", ""),
        "synthetic": bool(report.get("ai")),
        # one category per channel: TECH = Science & Technology (28), main = Entertainment (24)
        "category_id": os.getenv("YT_CATEGORY") or ("28" if TECH else "24"),
    }


TRANSLATE_PROMPT = """Translate this YouTube video title and description for viewers in other countries.
Keep the meaning, emotion and curiosity, and sound natural to native speakers (not word for word).
Titles under 90 characters. Keep the description's line breaks. Keep names and numbers unchanged.
Do not add anything.
Language codes: {langs}
TITLE: {title}
DESCRIPTION:
{description}
Return ONLY JSON with exactly these codes as keys:
{{"<code>": {{"title": str, "description": str}}}}"""


def localize(title: str, description: str, tail: str) -> dict:
    """YouTube localizations: viewers see the title/description in their own language.
    `tail` (hashtags + channel line) is appended untranslated. Best effort: {} on any problem."""
    if not LANGS:
        return {}
    try:
        data = json.loads(_strip_fences(ask_ai(TRANSLATE_PROMPT.format(
            langs=", ".join(LANGS), title=title, description=description), creative=False)))
    except (SystemExit, Exception) as e:  # noqa: BLE001 - translations must never stop an upload
        print(f"  translations skipped: {e}")
        return {}
    out = {}
    for code in LANGS:
        v = data.get(code) if isinstance(data, dict) else None
        if isinstance(v, dict) and str(v.get("title") or "").strip():
            out[code] = {"title": " ".join(str(v["title"]).split())[:100],
                         "description": (str(v.get("description") or "").strip() + tail)[:4900]}
    print(f"  title/description translated: {', '.join(out) or 'none'}")
    return out


# ================================================================ upload

def refresh_token() -> str:
    """YT_REFRESH_TOKEN secret, else the token saved by the Connect YouTube workflow."""
    if os.getenv("YT_REFRESH_TOKEN"):
        return os.environ["YT_REFRESH_TOKEN"]
    enc = CH_DIR / "yt_token.enc"
    if not enc.exists():
        sys.exit("YouTube not connected: run the 'Connect YouTube channel' workflow "
                 "or set the YT_REFRESH_TOKEN secret.")
    return subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-a",
                           "-pass", "env:YT_CLIENT_SECRET"], input=enc.read_bytes(),
                          capture_output=True, check=True).stdout.decode().strip()


def youtube_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None,
        refresh_token=refresh_token(),
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _http_status(e: Exception) -> int | None:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "resp", None), "status", None)
    return int(status) if status is not None else None


def upload(video: Path, thumb: Path, meta: dict) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    yt = youtube_client()
    if TECH:  # never post a TECH video on the main channel (wrong account picked when connecting)
        mine = yt.channels().list(part="snippet", mine=True).execute().get("items") or [{}]
        name = mine[0].get("snippet", {}).get("title", "?")
        if "tech" not in name.lower():
            sys.exit(f"The connected channel is '{name}', not Discovery Mosti TECH: run 'Connect YouTube "
                     "channel' with channel=tech and pick the TECH channel. Nothing was uploaded.")
    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": meta.get("category_id", "24"),  # 24 = Entertainment, 28 = Science & Technology
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": PRIVACY,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
            # YouTube policy: disclose realistic AI-generated visuals
            "containsSyntheticMedia": meta["synthetic"],
        },
    }
    def insert(parts: str) -> dict:
        req = yt.videos().insert(
            part=parts, body=body,
            media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True))
        resp, retries = None, 0
        while resp is None:
            try:
                _, resp = req.next_chunk()
            except (HttpError, OSError) as e:
                status = _http_status(e)
                if retries >= 5 or (status is not None and status < 500):
                    raise
                retries += 1
                print(f"  upload hiccup ({e}); retry {retries}/5")
                time.sleep(2 ** retries)
        return resp

    if meta.get("localizations"):
        body["localizations"] = meta["localizations"]
        try:
            resp = insert("snippet,status,localizations")
        except HttpError as e:
            if not 400 <= (_http_status(e) or 0) < 500:
                raise
            print(f"  YouTube refused the translations ({e}); uploading without them")
            body.pop("localizations")
            resp = insert("snippet,status")
    else:
        resp = insert("snippet,status")
    vid = resp["id"]
    link = f"https://youtu.be/{vid}" if FILM else f"https://youtube.com/shorts/{vid}"
    print(f"Uploaded as {resp['status']['privacyStatus']}: {link}")
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
        print("Thumbnail set.")
    except HttpError as e:
        print(f"Thumbnail not set ({e.status_code}). Verify your channel phone number at "
              f"youtube.com/verify to enable custom thumbnails.")
    # what YouTube really did: uploads from a Google Cloud project that hasn't passed YouTube's API
    # audit are locked to private whatever was asked
    meta["privacy"] = resp["status"]["privacyStatus"]
    try:
        items = yt.videos().list(part="status", id=vid).execute().get("items") or []
        meta["privacy"] = items[0]["status"]["privacyStatus"] if items else meta["privacy"]
    except (HttpError, KeyError) as e:
        print(f"  could not re-read the video's status ({e})")
    if meta["privacy"] != PRIVACY:
        print(f"WARNING: YouTube keeps this video {meta['privacy']} although {PRIVACY} was asked. Usually the "
              "Google Cloud project still needs YouTube's API audit (README: Good to know); until then, "
              "publish each video in YouTube Studio.")
    return vid


# ================================================================ main

# ================================================================ film teaser Short

TEASER_PROMPT = """You cut a vertical YouTube Short (a TEASER) from the silent short film below, made by
"{channel}". Viewers in every country watch it with no sound or words, only pictures, short captions
and music. The teaser must make them want to watch the full film, without giving the ending away.

Film title: {title}
Film story: {description}
Film scenes you can use (index: what we see | the film's caption):
{scenes}

RULES
- Pick 6 to 8 scene indexes, in any order. Scene 1 of the teaser is the HOOK: the most striking
  picture with a caption that raises a question. Never show the final resolution.
- Write NEW captions (max 7 simple words, present tense, no emojis), not the film's; 1-2 may be "".
- "seconds": 3.5 to 5.5 per scene.
- "ending": max 8 words inviting viewers to watch the full film on the channel.
- "title": max 60 characters, different from the film's title, honest curiosity, no hashtags, no ALL CAPS.
- "description": 3 short lines; say it is a teaser of the full silent short film.
- "hashtags": 3-5 (no #Shorts). "tags": 8-12 search phrases.
- "thumbnail_text": 2-4 words, uppercase, sharing no word with the title.
- "music_mood": one of warm, sad, tense, curious, uplifting.

Return ONLY JSON: {{"title": str, "description": str, "hashtags": [str], "tags": [str],
  "thumbnail_text": str, "ending": str, "music_mood": str,
  "scenes": [{{"index": int, "caption": str, "seconds": number}}]}}"""


def teaser_main() -> int:
    """After the weekly film upload (same workflow job): a vertical teaser Short made from the
    film's pictures, linking to the film. Reuses the film's out/ folder; writes to out/teaser/."""
    if NARRATION != "off":
        print("Narration is on - no silent teaser (it would reuse the film's pictures without your voice).")
        return 0
    film_meta, film_sb, film_work = OUT / "metadata.json", OUT / "storyboard.json", OUT / "work"
    if not (film_meta.exists() and film_sb.exists()):
        print("No film from this run - no teaser.")
        return 0
    film = json.loads(film_sb.read_text(encoding="utf-8"))
    film_url = json.loads(film_meta.read_text(encoding="utf-8")).get("youtube_url")
    if not film_url and not SKIP_UPLOAD:
        print("The film was not uploaded - no teaser.")
        return 0
    usable = {i: s for i, s in enumerate(film["scenes"]) if (film_work / f"img_{i:02d}.png").exists()}
    if len(usable) < 6:
        print(f"Only {len(usable)} film pictures available - no teaser.")
        return 0
    decide_tier()
    lines = "\n".join(f"{i}: {s['image_prompt'][:140]} | {s.get('caption', '')}" for i, s in usable.items())
    sb = None
    for attempt in range(2):
        try:
            sb = json.loads(_strip_fences(ask_ai(TEASER_PROMPT.format(
                channel=CHANNEL_NAME, title=film["title"], description=film["description"][:600], scenes=lines))))
            sb["scenes"] = [{"index": int(x["index"]), "caption": str(x.get("caption") or ""),
                             "seconds": max(3.0, min(6.0, float(x.get("seconds") or 4.5)))}
                            for x in sb.get("scenes") or [] if int(x.get("index", -1)) in usable][:9]
            if len(sb["scenes"]) >= 5 and str(sb.get("title") or "").strip():
                break
            print(f"  teaser plan rejected ({len(sb['scenes'])} usable scenes)")
        except (SystemExit, Exception) as e:  # noqa: BLE001
            print(f"  teaser plan failed ({str(e)[:120]})")
        sb = None
    if not sb:
        print("No usable teaser plan - skipping the teaser (the film is up).")
        return 0
    sb["title"] = " ".join(str(sb["title"]).split())
    sb["description"] = (str(sb.get("description") or "").strip()
                         + (f"\n\n▶ Full film: {film_url}" if film_url else ""))
    sb["ending"] = str(sb.get("ending") or "Watch the full film on our channel").strip()
    if sb.get("music_mood") not in CHORDS:
        sb["music_mood"] = film.get("music_mood", "warm")
    sb.setdefault("hashtags", film.get("hashtags", []))
    sb.setdefault("tags", film.get("tags", []))
    polish_package(sb)

    out = OUT / "teaser"
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    set_grade(film.get("grade", "teal_orange"))
    parts, lengths, report = [], [], {"ai": 0, "stock": 0, "reused": 0, "fallback": 0, "from_film": True}
    for k, s in enumerate(sb["scenes"]):
        L = s["seconds"] + XF + (2.8 if k == len(sb["scenes"]) - 1 else 0.0)
        part = work / f"scene_{k:02d}.mp4"
        portrait_scene(film_work / f"img_{s['index']:02d}.png", L, part)
        report["ai"] += 1
        print(f"  teaser scene {k + 1} <- film scene {s['index'] + 1} | {s['caption']}")
        parts.append(part)
        lengths.append(L)
    final, total = finish_video(sb, parts, lengths, work, report)
    report["duration"] = round(total, 1)
    thumb = out / "thumbnail.jpg"
    make_thumbnail(sb, film_work / f"img_{sb['scenes'][0]['index']:02d}.png", thumb)
    meta = build_metadata(sb, report, "story")
    main_text = sb["description"].strip()
    if meta["description"].startswith(main_text):
        meta["localizations"] = localize(meta["title"], main_text, meta["description"][len(main_text):])
    (out / "metadata.json").write_text(json.dumps({**meta, "report": report}, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
    print(f"Rendered teaser {final} ({report})")
    if SKIP_UPLOAD:
        print("Preview only - the teaser is in the run's artifacts.")
        return 0
    vid = upload(final, thumb, meta)
    with DONE.open("a", encoding="utf-8") as f:
        f.write(f"Teaser of: {film['title']} | https://youtube.com/shorts/{vid} | "
                f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ} | teaser\n")
    return 0


def voice_folders() -> list[tuple[Path, dict]]:
    """Script folders of this format whose story is still in the queue, in queue order. Folders of
    stories that left the queue are removed (git history still has them)."""
    order = {idea: n for n, (_, idea) in reversed(list(enumerate(queue())))}
    found = []
    for folder in sorted(VOICE_DIR.glob(f"{FORMAT}-*")) if VOICE_DIR.is_dir() else []:
        try:
            data = json.loads((folder / "storyboard.json").read_text(encoding="utf-8"))
            data["storyboard"]["scenes"][0]["narration"], data["genre"]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            if folder.is_dir() and not voice_recording(folder):
                shutil.rmtree(folder, ignore_errors=True)  # a broken or half-written script: write it again
            continue
        if data.get("idea") in order:
            found.append((order[data["idea"]], folder, data))
        elif voice_recording(folder):  # never throw a recording away: the idea's line may only have been edited
            print(f"  {folder.name}: its story is no longer in {STORIES.name} (line edited or removed?); "
                  "the folder holds a recording, so it is kept. Delete it if it is not needed.")
        else:
            print(f"  {folder.name}: its story is no longer in {STORIES.name}; folder removed")
            shutil.rmtree(folder, ignore_errors=True)
    return [(f, d) for _, f, d in sorted(found, key=lambda x: x[0])]


def voice_job() -> tuple[Path, dict, Path] | None:
    """The first story in the queue whose recording has been uploaded."""
    for folder, data in voice_folders():
        rec = voice_recording(folder)
        if rec:
            return folder, data, rec
    return None


def prepare_scripts() -> None:
    """Keep narration scripts ready for the next VOICE_AHEAD stories of the queue (best effort)."""
    have = {d["idea"] for _, d in voice_folders()}
    for genre, idea in queue()[:VOICE_AHEAD]:
        if idea in have:
            continue
        folder = VOICE_DIR / slug_for(FORMAT, idea)
        if folder.exists() and voice_recording(folder):
            print(f"  {folder.name} holds a recording but no readable script; left as it is")
            continue
        print(f"Writing the narration script for: {idea[:90]}")
        try:
            write_script(folder, storyboard(genre, idea), genre, idea)
            print(f"  script ready to record: {folder.relative_to(ROOT)}/script.md")
        except (SystemExit, Exception) as e:  # noqa: BLE001 - the next run tries again
            print(f"  no script this time ({str(e)[:150]})")
            return


def main() -> int:
    if TEASER:
        return teaser_main()
    # Scheduled runs skip if this format was uploaded in the last few hours. With a catch-up
    # cron 2 h after each slot, a run GitHub delayed or dropped is made up for, never doubled.
    recent = float(os.getenv("SKIP_IF_UPLOADED_WITHIN_HOURS") or 0)
    if recent > 0 and uploaded_within(recent):
        print(f"A {FORMAT} was already uploaded in the last {recent:g} h - nothing to do.")
        return 0
    if not SKIP_UPLOAD and os.getenv("YT_CLIENT_ID"):
        youtube_connection_check()
    decide_tier()
    refill_queue()
    sample = voice_sample() if NARRATION == "clone" else None
    if NARRATION == "clone" and not sample:
        note = ("No video made: narration is on, but there is no sample of your voice yet. Upload one clip "
                "of 15-30 seconds of you speaking (quiet room, no music) as voice/sample.mp3 (or .m4a, .wav) "
                "and every video will be narrated in your voice. NARRATION=off brings back silent videos.")
        print(note)
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(f"\n**Waiting for your voice sample.** {note}\n")
        return 0
    job = voice_job() if RECORDED else None
    if RECORDED and not job:
        prepare_scripts()
        waiting = [f.name for f, _ in voice_folders()]
        if NARRATION == "record" or not waiting:
            note = (f"No video made: no recording yet. Scripts waiting for your voice in "
                    f"{VOICE_DIR.relative_to(ROOT)}/: {', '.join(waiting) or 'none (no script could be written)'}. "
                    "Open a folder's script.md, record it and upload the audio file into that folder.")
            print(note)
            if os.getenv("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                    f.write(f"\n**Waiting for your voice.** {note}\n")
            if not (SKIP_UPLOAD and waiting):
                return 0
        print("No recording - " + ("preview without narration" if SKIP_UPLOAD else "this video goes out silent")
              + " (NARRATION=optional)." if NARRATION == "optional" else "Preview without your voice.")
        folder, data = voice_folders()[0]
        job = (folder, data, None)
    if job:
        folder, data, recording = job
        genre, idea, sb = data["genre"], data["idea"], data["storyboard"]
    else:
        item = next_story()
        if not item:
            print(f"{STORIES.name} is empty - add more ideas.")
            return 0
        genre, idea = item
        folder = recording = sb = None
    print(f"[{genre}] {idea}")

    OUT.mkdir(exist_ok=True)
    sb = sb or storyboard(genre, idea)
    (OUT / "storyboard.json").write_text(json.dumps(sb, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Title: {sb['title']}  ({len(sb['scenes'])} scenes, mood {sb.get('music_mood')})")

    if sample:
        sb["voice"] = "owner's voice, AI-cloned from their own sample"
    try:
        video, report = build_video(sb, OUT / "work", recording, sample)
    except (ValueError, subprocess.SubprocessError) as e:  # no voice: nothing is uploaded, the story stays queued
        if not (recording or sample):
            raise
        note = (f"The recording {recording.name} in {folder.relative_to(ROOT)}/ can't be used: {e} "
                "Replace the file and run again." if recording else
                f"The narration could not be spoken in your voice ({str(e)[:300]}). Nothing was uploaded; "
                "the story stays at the top of the queue and the next run tries again.")
        print(note)
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(f"\n**Voice problem.** {note}\n")
        return 1
    final, thumb = OUT / "short.mp4", OUT / "thumbnail.jpg"
    shutil.move(video, final)
    shutil.move(OUT / "work" / "thumbnail.jpg", thumb)
    meta = build_metadata(sb, report, genre)
    main_text = sb["description"].strip()
    if meta["description"].startswith(main_text):
        meta["localizations"] = localize(meta["title"], main_text, meta["description"][len(main_text):])
    (OUT / "metadata.json").write_text(json.dumps({**meta, "report": report}, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
    (OUT / "social.txt").write_text(f"{meta['title']}\n\n{meta['social_caption']}\n", encoding="utf-8")
    print(f"Rendered {final} ({report})")

    if report["fallback"] > MAX_FALLBACK:
        print(f"WARNING: {report['fallback']} of {len(sb['scenes'])} scenes have no real visuals "
              f"(limit {MAX_FALLBACK}). Usually the free AI image allowance is used up for today "
              f"(Cloudflare resets 00:00 UTC) or CF_ACCOUNT_ID/CF_API_TOKEN are missing.")
        if not SKIP_UPLOAD:
            print("Not uploading: low-effort videos risk YouTube's reused/repetitive content policy. "
                  "The story stays at the top of the queue.")
            return 1

    if SKIP_UPLOAD:
        print("Preview only - download short.mp4, thumbnail.jpg and metadata.json from the run's artifacts.")
        return 0
    vid = upload(final, thumb, meta)
    mark_done(idea, vid)
    if folder:  # script and recording are used up; get the next scripts ready to record
        shutil.rmtree(folder, ignore_errors=True)
        prepare_scripts()
    # crosspost.py (next workflow step) shares this upload on Instagram / Facebook / X
    meta.update(report=report, format=FORMAT, privacy=meta.get("privacy", PRIVACY),
                youtube_url=f"https://youtu.be/{vid}" if FILM else f"https://youtube.com/shorts/{vid}")
    (OUT / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
