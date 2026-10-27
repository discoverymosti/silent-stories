"""
Silent Stories -> YouTube pipeline (fully automatic, copyright-safe):
daily vertical Shorts, plus a weekly widescreen short film with VIDEO_FORMAT=film.

No voiceover. Original AI visuals + animated captions + original music, so it works
in every language and contains no third-party copyrighted material.

1. Take the next idea from stories.txt
2. Gemini (or Claude) writes the storyboard + SEO/SMO metadata
3. Each scene gets an ORIGINAL AI image (Gemini image -> Cloudflare FLUX -> Pollinations),
   or stock footage (Pixabay/Pexels) if VISUAL_MODE=stock or AI fails
4. Auto-edit: Ken Burns camera moves, crossfades, film look, animated captions,
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
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
# VIDEO_FORMAT=short: daily vertical Short (~50 s) from stories.txt
# VIDEO_FORMAT=film:  weekly widescreen short film (~3 min) from films.txt
FORMAT = os.getenv("VIDEO_FORMAT", "short").lower()
FILM = FORMAT == "film"
STORIES = ROOT / ("films.txt" if FILM else "stories.txt")
DONE = ROOT / "done.txt"
MUSIC_DIR = ROOT / "music"
OUT = ROOT / "out"

W, H, FPS = (1920, 1080, 30) if FILM else (1080, 1920, 30)
XF = 0.5  # crossfade seconds
FONT = os.getenv("CAPTION_FONT", "Noto Sans")
FONTS_DIR = os.getenv("FONTS_DIR", "")  # extra fonts folder (static ffmpeg can't rely on system fontconfig)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")  # empty = auto-pick newest
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "")  # empty = auto-pick newest
VISUAL_MODE = os.getenv("VISUAL_MODE", "ai").lower()  # ai | stock
PRIVACY = os.getenv("YT_PRIVACY", "private")
SKIP_UPLOAD = os.getenv("SKIP_UPLOAD", "false").lower() == "true"
CHANNEL_TAG = os.getenv("CHANNEL_HASHTAG", "#DiscoveryMosti")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "DiscoveryMosti")
# refuse to upload if more than this many scenes ended up as plain gradient filler
MAX_FALLBACK = int(os.getenv("MAX_FALLBACK_SCENES", "4" if FILM else "2"))
WATERMARK = os.getenv("WATERMARK", CHANNEL_NAME)
# FREE_ONLY (default) = never call anything that can bill: no Gemini image generation (paid-only
# for most keys, and billed if billing is ever enabled) and no Claude API. Set FREE_ONLY=false to allow.
FREE_ONLY = os.getenv("FREE_ONLY", "true").lower() != "false"

NO_TEXT = ("No text, no letters, no captions, no logos, no watermark, no brand names, "
           "no real celebrities, no copyrighted characters.")


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


def mark_done(idea: str, video_id: str | None) -> None:
    kept = [l for l in STORIES.read_text(encoding="utf-8").splitlines() if idea not in l]
    STORIES.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
    with DONE.open("a", encoding="utf-8") as f:
        base = "https://youtu.be/" if FILM else "https://youtube.com/shorts/"
        f.write(f"{idea} | {(base + video_id) if video_id else 'not uploaded'}\n")


REFILL_PROMPT = """You plan content for "{channel}", a worldwide YouTube channel of SILENT stories:
no voiceover, no dialogue, only cinematic images, short captions and music.
Write {n} NEW, original {kind} ideas for a global audience (any country; universal emotions).
Each idea: one sentence with a clear character, situation and emotional or surprising payoff.
{genres}
Do not repeat or closely resemble any of these existing ideas:
{existing}
Return ONLY JSON: {{"ideas": [{{"genre": str, "idea": str}}]}}"""


def refill_queue(min_left: int = 2 if FILM else 5) -> None:
    """Never run dry: when few ideas are left, ask the AI for fresh original ones."""
    lines = STORIES.read_text(encoding="utf-8").splitlines()
    left = [l for l in lines if l.strip() and not l.lstrip().startswith("#")]
    if len(left) >= min_left or not (os.getenv("GEMINI_API_KEY") or (os.getenv("ANTHROPIC_API_KEY") and not FREE_ONLY)):
        return
    existing = [l.split("|", 1)[-1].strip()[:90] for l in left]
    if DONE.exists():
        existing += [l.split("|")[0].strip()[:90] for l in DONE.read_text(encoding="utf-8").splitlines() if l.strip()]
    n = 4 if FILM else 10
    prompt = REFILL_PROMPT.format(
        channel=CHANNEL_NAME, n=n,
        kind="three-act short-film (about 3 minutes)" if FILM else "YouTube Shorts (about 50 seconds)",
        genres='genre is always "story".' if FILM else
        'Mix genres: about half "story" (emotional micro-drama), a quarter "explainer" (how something '
        'works) and a quarter "fact" (surprising true fact, must be accurate).',
        existing="\n".join(f"- {e}" for e in existing[-60:]) or "- (none)")
    try:
        text = ask_gemini(prompt) if os.getenv("GEMINI_API_KEY") else ask_claude(prompt)
        ideas = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))["ideas"]
    except (SystemExit, Exception) as e:  # refilling is best effort; the run goes on
        print(f"  could not refill {STORIES.name}: {e}")
        return
    new = [f"{(i.get('genre') or 'story').strip().lower()} | {' '.join(str(i['idea']).split())}"
           for i in ideas if isinstance(i, dict) and i.get("idea")]
    if new:
        STORIES.write_text("\n".join(lines).rstrip() + "\n" + "\n".join(new) + "\n", encoding="utf-8")
        print(f"  added {len(new)} fresh ideas to {STORIES.name}")


# ================================================================ storyboard + SEO

STORYBOARD_PROMPT = """You are the director and growth marketer of "{channel}", a YouTube
Shorts channel. Videos have NO voiceover and NO dialogue: they tell a story only through
cinematic images, very short on-screen captions and music, so anyone in any country can follow.
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
- Captions: max 7 simple words, present tense, no emojis. 2-3 scenes have "" (silent beats).
- "ending": shareable closing line, max 8 words.
- "music_mood": one of warm, sad, tense, curious, uplifting.

VISUAL RULES (images are AI-generated; work like a film crew so every shot matches)
- First write the blueprint, then the shots. Every image is generated separately, so
  consistency comes ONLY from re-using the exact same saved descriptions.
- "style": one visual style for the whole film (e.g. "cinematic film still, 35mm lens,
  shallow depth of field, soft film grain").
- "grade": one colour grade for the whole film: warm, cool, teal_orange, golden, muted or noir.
- "cast": saved character elements, 1-3 people/animals:
  [{{"id": "A", "name": "short role noun, e.g. old kite maker", "look": "fixed visual
  description: age, ethnicity, build, hair, face, exact clothes and colours"}}].
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
- Original people only, no celebrities, brands, logos, readable text, or copyrighted characters.

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
- "thumbnail_text": 2-4 words, punchy, uppercase.

Return ONLY valid JSON with exactly these keys:
{{"title": str, "description": str, "hashtags": [str], "tags": [str],
  "social_caption": str, "thumbnail_prompt": str, "thumbnail_text": str,
  "style": str, "grade": str, "music_mood": str, "ending": str,
  "cast": [{{"id": str, "name": str, "look": str}}], "locations": [{{"id": str, "look": str}}],
  "scenes": [{{"shot": str, "image_prompt": str, "cast": [str], "location": str,
               "lighting": str, "camera": str, "query": str, "caption": str, "seconds": number}}]}}"""


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
        # prefer flash, then flash-lite, then pro; newest stable first within each
        rank = lambda n: (0 if "flash" in n and "lite" not in n else 1 if "lite" in n else 2,)
        pool.sort(key=lambda n: (rank(n), tuple(-x if isinstance(x, (int, float)) else x for x in _version_key(n))))
    else:
        pool = sorted([n for n in names if "image" in n and "gemini" in n], key=_version_key, reverse=True)
    if wanted:
        pool = [wanted] + [n for n in pool if n != wanted]
    pool = pool[:5]
    print(f"  Gemini {kind} models: {pool}")
    _model_cache[kind] = pool
    return pool


def pick_gemini_model(kind: str) -> str | None:
    c = gemini_candidates(kind)
    return c[0] if c else None


def ask_gemini(prompt: str) -> str:
    models = gemini_candidates("text")
    if not models:
        raise SystemExit("No Gemini text model available for this API key.")
    last = ""
    for model in models:
        for attempt in range(4):
            try:
                r = requests.post(
                    f"{GEMINI_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                    json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}},
                    timeout=180,
                )
            except requests.RequestException as e:
                last = str(e)
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts)
                    json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
                    print(f"  answer by {model}")
                    return text
                except (KeyError, IndexError, ValueError) as e:
                    last = f"bad response: {e}"
                    continue
            last = f"{r.status_code}: {r.text[:300]}"
            print(f"  {model} -> {r.status_code}, attempt {attempt + 1}")
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(15 * (attempt + 1))
                continue
            break  # 400/403/404: try next model
    raise SystemExit(f"Gemini failed on all models. Last error {last}")


def ask_claude(prompt: str) -> str:
    import anthropic
    msg = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
    return msg.content[0].text


def storyboard(genre: str, idea: str) -> dict:
    prompt = STORYBOARD_PROMPT.format(genre=genre, idea=idea, channel=CHANNEL_NAME)
    if FILM:
        start, end = prompt.index("STORY RULES"), prompt.index("VISUAL RULES")
        prompt = (prompt[:start] + FILM_RULES + "\n" + prompt[end:]).replace(
            "YouTube\nShorts channel", "YouTube\nchannel").replace("vertical image", "widescreen 16:9 image")
    if os.getenv("GEMINI_API_KEY"):
        text = ask_gemini(prompt)
    elif os.getenv("ANTHROPIC_API_KEY") and not FREE_ONLY:
        text = ask_claude(prompt)
    else:
        raise SystemExit("Set GEMINI_API_KEY (free tier). Claude needs FREE_ONLY=false (paid API).")
    sb = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
    for s in sb["scenes"]:
        s["seconds"] = max(3.0, min(10.0 if FILM else 7.0, float(s.get("seconds", 4.5))))
        s.setdefault("camera", "zoom_in")
        s.setdefault("query", s.get("image_prompt", "")[:40])
        s.setdefault("caption", "")
        if isinstance(s.get("cast"), str):
            s["cast"] = [s["cast"]]
    sb.setdefault("style", "cinematic film still, soft natural light, 35mm, shallow depth of field")
    sb.setdefault("characters", "")
    sb["cast"] = [c for c in sb.get("cast") or [] if isinstance(c, dict) and c.get("look")]
    sb["locations"] = [l for l in sb.get("locations") or [] if isinstance(l, dict) and l.get("look")]
    if sb.get("grade") not in GRADES:
        sb["grade"] = "teal_orange"
    sb.setdefault("hashtags", [])
    sb.setdefault("tags", [])
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
    if ids:
        parts.append("Characters: " + "; ".join(f"the {names[i]}: {cast[i]}" for i in ids))
    elif sb.get("characters"):
        parts.append(f"Characters: {sb['characters']}")
    if scene.get("location") in places:
        parts.append(f"Location: {places[scene['location']]}")
    if scene.get("lighting"):
        parts.append(f"Lighting: {scene['lighting']}")
    parts += [f"Style: {sb['style']}", f"Colour grade: {sb.get('grade', 'teal_orange').replace('_', '-')}",
              f"Format: {'widescreen 16:9' if FILM else 'vertical 9:16'} cinematic photo"]
    return ". ".join(parts) + f". {NO_TEXT}"


def gen_image_gemini(prompt: str, dest: Path) -> bool:
    global _gemini_img_ok
    if FREE_ONLY or not (_gemini_img_ok and os.getenv("GEMINI_API_KEY")):
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
                return True
    return False


_cf_ok = True


def gen_image_cloudflare(prompt: str, dest: Path, seed: int) -> bool:
    """Cloudflare Workers AI FLUX.1 schnell (free daily allowance)."""
    global _cf_ok
    acct, token = os.getenv("CF_ACCOUNT_ID"), os.getenv("CF_API_TOKEN")
    if not (_cf_ok and acct and token):
        return False
    try:
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/black-forest-labs/flux-1-schnell",
            headers={"Authorization": f"Bearer {token}"},
            json={"prompt": f"{prompt} Centered composition, subject in the middle third."[:2048],
                  "steps": 4},  # schnell's native step count; cost scales with steps
            timeout=120,
        )
    except requests.RequestException as e:
        print(f"    cloudflare error: {e}")
        return False
    if r.status_code != 200:
        print(f"    cloudflare {r.status_code}: {r.text[:200]}")
        if r.status_code in (401, 403, 429):
            _cf_ok = False  # bad token or daily allowance used up: stop trying this run
        return False
    data = (r.json().get("result") or {}).get("image")
    if not data:
        return False
    dest.write_bytes(base64.b64decode(data))
    return True


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


def gen_image(prompt: str, dest: Path, seed: int) -> str | None:
    if dest.exists():
        return "cached"
    if gen_image_gemini(prompt, dest):
        return "gemini"
    if gen_image_cloudflare(prompt, dest, seed):
        return "cloudflare"
    if gen_image_pollinations(prompt, dest, seed):
        time.sleep(3)  # be polite to the free endpoint
        return "pollinations"
    return None


# ================================================================ stock footage (fallback)

def pexels_search(query: str, used: set[int]) -> str | None:
    key = os.getenv("PEXELS_API_KEY")
    if not key:
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
                     params={"key": key, "q": query[:100], "per_page": 20, "safesearch": "true"}, timeout=30)
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
_BASE_LOOK = "eq=contrast=1.06:saturation=1.03,vignette=PI/5,noise=alls=3:allf=t"
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
    z = 0.14
    moves = {
        "zoom_in":   (f"1+{z}*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        "zoom_out":  (f"1+{z}-{z}*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        "pan_left":  (f"1+{z}", f"(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),
        "pan_right": (f"1+{z}", f"(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),
        "pan_up":    (f"1+{z}", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-on/{n})"),
    }
    zz, xx, yy = moves.get(motion, moves["zoom_in"])
    # upscale first so the slow zoom stays sharp and jitter-free
    vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
          f"zoompan=z='{zz}':x='{xx}':y='{yy}':d={n}:s={W}x{H}:fps={FPS},setsar=1,{LOOK}")
    ff("-i", str(img), "-vf", vf, "-frames:v", str(n), *ENC, str(dest))


def clip_scene(src: Path, seconds: float, dest: Path) -> None:
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},{LOOK}"
    ff("-ss", "0.3", "-t", f"{seconds:.2f}", "-i", str(src), "-vf", vf, "-an", *ENC, str(dest))


def gradient_scene(seconds: float, dest: Path, idx: int) -> None:
    hue = (idx * 37) % 360
    vf = (f"geq=r='40+30*sin(2*PI*(X/W+T/8))':g='30+20*sin(2*PI*(Y/H+T/9))':b='60+40*cos(2*PI*(X/W-T/7))',"
          f"hue=h={hue},scale={W}:{H}:flags=bicubic")
    ff("-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"color=c=black:s={W // 10}x{H // 10}:r={FPS}",
       "-vf", vf, *ENC, str(dest))


def end_card(seconds: float, dest: Path) -> None:
    ff("-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"color=c=0x0b0b0f:s={W}x{H}:r={FPS}",
       "-vf", "vignette=PI/4", *ENC, str(dest))


def crossfade_all(parts: list[Path], lengths: list[float], dest: Path) -> list[float]:
    """Join clips with crossfades. Returns the start time of each clip in the output."""
    starts, t = [], 0.0
    for i, L in enumerate(lengths):
        starts.append(t)
        t += L - XF
    inputs = sum((["-i", str(p)] for p in parts), [])
    chain, prev = [], "[0:v]"
    for i in range(1, len(parts)):
        trans = "fadeblack" if i == len(parts) - 1 else random.choice(["fade", "fade", "dissolve", "smoothleft"])
        out = f"[v{i}]"
        chain.append(f"{prev}[{i}:v]xfade=transition={trans}:duration={XF}:offset={starts[i]:.3f}{out}")
        prev = out
    chain.append(f"{prev}fade=t=in:st=0:d=0.4,format=yuv420p[vout]")
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
Style: End,{FONT},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,5,110,110,0,1
Style: Title,{FONT},96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,0,5,160,160,0,1
Style: Tag,{FONT},44,&H00B4B4B4,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,4,0,1,0,0,2,90,90,{220 if FILM else 300},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if title:  # film opening card: title, then the channel credit
        lines.append(f"Dialogue: 0,{ass_time(0.4)},{ass_time(title_len - 0.2)},Title,,0,0,0,,"
                     f"{{\\fad(700,500)}}{ass_escape(title)}")
        lines.append(f"Dialogue: 0,{ass_time(1.1)},{ass_time(title_len - 0.2)},Tag,,0,0,0,,"
                     f"{{\\fad(700,500)}}{ass_escape(('a ' + CHANNEL_NAME + ' silent film').upper())}")
    for s, t in zip(scenes, starts):
        cap = ass_escape(s.get("caption", "").strip())
        if cap:
            a, b = t + XF * 0.6, t + s["seconds"] - XF
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,"
                         f"{{\\fad(280,220)\\t(0,280,\\fscx104\\fscy104)\\t(280,500,\\fscx100\\fscy100)}}{cap}")
    a, b = end_start + 0.5, end_start + end_len - 0.3
    lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},End,,0,0,0,,{{\\fad(600,400)}}{ass_escape(ending)}")
    lines.append(f"Dialogue: 0,{ass_time(a + 0.8)},{ass_time(b)},Tag,,0,0,0,,{{\\fad(600,400)}}"
                 f"{ass_escape(CHANNEL_NAME.upper())}")
    dest.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


_C, _G, _AM, _F = (261.6, 329.6, 392.0), (196.0, 246.9, 293.7), (220.0, 261.6, 329.6), (174.6, 220.0, 261.6)
_DM, _E, _EM, _D = (146.8, 174.6, 220.0), (164.8, 207.7, 246.9), (164.8, 196.0, 246.9), (146.8, 185.0, 220.0)
CHORDS = {  # 4-chord progression per mood (original synthesized music, no copyright)
    "warm":      [_C, _G, _AM, _F],
    "sad":       [_AM, _F, _C, _G],
    "tense":     [_AM, _F, _DM, _E],
    "curious":   [_F, _G, _EM, _AM],
    "uplifting": [_G, _D, _EM, _C],
}


def make_music(mood: str, seconds: float, dest: Path) -> str:
    """Royalty-free track from music/ if provided, else an original synthesized pad."""
    pool = list((MUSIC_DIR / mood).glob("*.mp3")) + list(MUSIC_DIR.glob("*.mp3"))
    fade_out = max(0.0, seconds - 2.5)
    if pool:
        track = random.choice(pool)
        ff("-stream_loop", "-1", "-i", str(track), "-t", f"{seconds:.2f}",
           "-af", f"afade=t=in:d=1.5,afade=t=out:st={fade_out:.2f}:d=2.5,volume=0.8",
           "-c:a", "aac", "-b:a", "192k", str(dest))
        return track.name
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

def build_video(sb: dict, work: Path) -> tuple[Path, dict]:
    work.mkdir(parents=True, exist_ok=True)
    used: set[int] = set()
    parts, lengths, report = [], [], {"ai": 0, "stock": 0, "fallback": 0}
    base_seed = random.randint(1, 10_000_000)
    set_grade(sb.get("grade", "teal_orange"))
    intro_len = 3.5 + XF if FILM else 0.0
    if FILM:
        intro = work / "intro.mp4"
        end_card(intro_len, intro)
        parts.append(intro)
        lengths.append(intro_len)
    for i, s in enumerate(sb["scenes"]):
        L = s["seconds"] + XF  # extra overlap for crossfade
        part = work / f"scene_{i:02d}.mp4"
        img = work / f"img_{i:02d}.png"
        raw = work / f"raw_{i:02d}.mp4"
        src = None
        if VISUAL_MODE == "ai":
            src = gen_image(full_prompt(sb, s), img, base_seed + i)
        if src:
            kenburns(img, L, s["camera"], part)
            report["ai"] += 1
        elif fetch_clip(s["query"], raw, used):
            clip_scene(raw, L, part)
            src = "stock"
            report["stock"] += 1
        else:
            gradient_scene(L, part, i)
            src = "fallback"
            report["fallback"] += 1
        print(f"  scene {i + 1:2d} [{src:12}] {s['camera']:9} {str(s.get('shot', ''))[:11]:11} | {s.get('caption', '')}")
        parts.append(part)
        lengths.append(L)

    end_len = 4.0
    end = work / "end.mp4"
    end_card(end_len + XF, end)
    parts.append(end)
    lengths.append(end_len + XF)

    joined = work / "joined.mp4"
    starts = crossfade_all(parts, lengths, joined)
    total = starts[-1] + lengths[-1]
    subs = work / "captions.ass"
    scene_starts = starts[1:] if FILM else starts
    write_captions(sb["scenes"], scene_starts, sb["ending"], starts[-1], lengths[-1], subs,
                   title=sb["title"] if FILM else "", title_len=intro_len)
    music = work / "music.m4a"
    report["music"] = make_music(sb.get("music_mood", "warm"), total, music)

    final = work / "short.mp4"
    ff("-i", str(joined), "-i", str(music), "-vf", f"ass={subs.as_posix()}" + (f":fontsdir={FONTS_DIR}" if FONTS_DIR else "") + watermark_filter(), *ENC,
       "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(final))

    thumb_img = work / "thumb_src.png"
    if VISUAL_MODE == "ai" and sb.get("thumbnail_prompt"):
        if not gen_image(full_prompt(sb, sb["thumbnail_prompt"]), thumb_img, base_seed + 999):
            thumb_img = work / "img_00.png"
    else:
        thumb_img = work / "img_00.png"
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


def build_metadata(sb: dict, report: dict) -> dict:
    hashtags = [("#" + re.sub(r"[^\w]", "", h.lstrip("#"))) for h in sb.get("hashtags", []) if h.strip()]
    hashtags = list(dict.fromkeys(hashtags + [CHANNEL_TAG, "#ShortFilm" if FILM else "#Shorts"]))[:6]
    tags, total = [], 0
    for t in [clean_tag(x) for x in sb.get("tags", [])] + [CHANNEL_NAME, "silent stories", "no words story", "short film"]:
        if t and t.lower() not in (x.lower() for x in tags) and total + len(t) + 3 < 480:
            tags.append(t)
            total += len(t) + 3
    visuals = "AI-generated (original)" if report.get("ai") else "Pixabay / Pexels stock (royalty-free)"
    description = (f"{sb['description'].strip()}\n\n"
                   f"{' '.join(hashtags)}\n\n"
                   f"🎬 {CHANNEL_NAME}: stories without words, for everyone, everywhere.\n"
                   f"Visuals: {visuals} · Music: {report.get('music', 'original')}")
    title = sb["title"].strip()[:90]
    return {
        "title": title,
        "description": description[:4900],
        "tags": tags,
        "hashtags": hashtags,
        "social_caption": sb.get("social_caption", ""),
        "synthetic": bool(report.get("ai")),
    }


# ================================================================ upload

def refresh_token() -> str:
    """YT_REFRESH_TOKEN secret, else the token saved by the Connect YouTube workflow."""
    if os.getenv("YT_REFRESH_TOKEN"):
        return os.environ["YT_REFRESH_TOKEN"]
    enc = ROOT / "yt_token.enc"
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


def upload(video: Path, thumb: Path, meta: dict) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    yt = youtube_client()
    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": os.getenv("YT_CATEGORY", "1"),  # 1 = Film & Animation
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
    req = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True))
    resp, retries = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except (HttpError, OSError) as e:
            status = getattr(e, "status_code", None) or getattr(getattr(e, "resp", None), "status", None)
            if retries >= 5 or (status is not None and int(status) < 500):
                raise
            retries += 1
            print(f"  upload hiccup ({e}); retry {retries}/5")
            time.sleep(2 ** retries)
    vid = resp["id"]
    link = f"https://youtu.be/{vid}" if FILM else f"https://youtube.com/shorts/{vid}"
    print(f"Uploaded as {resp['status']['privacyStatus']}: {link}")
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
        print("Thumbnail set.")
    except HttpError as e:
        print(f"Thumbnail not set ({e.status_code}). Verify your channel phone number at "
              f"youtube.com/verify to enable custom thumbnails.")
    return vid


# ================================================================ main

def main() -> int:
    refill_queue()
    item = next_story()
    if not item:
        print(f"{STORIES.name} is empty - add more ideas.")
        return 0
    genre, idea = item
    print(f"[{genre}] {idea}")

    OUT.mkdir(exist_ok=True)
    sb = storyboard(genre, idea)
    (OUT / "storyboard.json").write_text(json.dumps(sb, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Title: {sb['title']}  ({len(sb['scenes'])} scenes, mood {sb.get('music_mood')})")

    video, report = build_video(sb, OUT / "work")
    final, thumb = OUT / "short.mp4", OUT / "thumbnail.jpg"
    shutil.move(video, final)
    shutil.move(OUT / "work" / "thumbnail.jpg", thumb)
    meta = build_metadata(sb, report)
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
    # crosspost.py (next workflow step) shares this upload on Instagram / Facebook / X
    meta.update(report=report, format=FORMAT, privacy=PRIVACY,
                youtube_url=f"https://youtu.be/{vid}" if FILM else f"https://youtube.com/shorts/{vid}")
    (OUT / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
