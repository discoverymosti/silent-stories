"""
Silent Stories -> YouTube Shorts pipeline.

No voiceover. Visuals + animated captions + music only, so it works in every language.

1. Take the next idea from stories.txt
2. Claude writes a storyboard (scenes, captions, music mood, title, tags)
3. Pexels supplies vertical stock footage for each scene
4. ffmpeg builds a 1080x1920 video with fades, captions and an ambient soundtrack
5. YouTube Data API uploads it as a Short
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
STORIES = ROOT / "stories.txt"
DONE = ROOT / "done.txt"
MUSIC_DIR = ROOT / "music"
OUT = ROOT / "out"

W, H, FPS = 1080, 1920, 30
FONT = os.getenv("CAPTION_FONT", "Noto Sans")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
PRIVACY = os.getenv("YT_PRIVACY", "private")
SKIP_UPLOAD = os.getenv("SKIP_UPLOAD", "false").lower() == "true"
CHANNEL_TAG = os.getenv("CHANNEL_HASHTAG", "#SilentStories")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Silent Stories")


# ---------------------------------------------------------------- queue

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
    kept = [l for l in STORIES.read_text(encoding="utf-8").splitlines()
            if idea not in l]
    STORIES.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
    with DONE.open("a", encoding="utf-8") as f:
        f.write(f"{idea} | {('https://youtu.be/' + video_id) if video_id else 'not uploaded'}\n")


# ---------------------------------------------------------------- storyboard

STORYBOARD_PROMPT = """You are the director of "Silent Stories", a YouTube Shorts channel.
Videos have NO voiceover and NO dialogue. They tell a story only through stock footage,
very short on-screen captions and music, so anyone in any country can follow them.

Genre: {genre}   ("story" = emotional micro-drama, "explainer" = how something works,
                  "fact" = surprising true fact told visually)
Idea: {idea}

Rules:
- 9 to 12 scenes, total 45-55 seconds. Each scene 3-6 seconds.
- Scene 1 is the hook: an intriguing image + a caption that creates a question.
- Captions: at most 7 simple words, present tense, no emojis. Some scenes should have
  an empty caption ("") to create silent beats.
- Every "query" is a concrete Pexels stock-video search of 2-4 English words describing
  something a camera can film (e.g. "crowded city bus", "old man hands", "rain on window").
  No named people, brands, or fantasy elements. Reuse a query only if the story needs it.
- "ending" is a short, shareable closing line (max 8 words) shown on a dark card.
- "music_mood" is one of: warm, sad, tense, curious, uplifting.
- Title: under 60 characters, curiosity-driven, no clickbait lies. No hashtags in title.

Return ONLY valid JSON:
{{"title": str, "description": str (2 short sentences), "tags": [8-12 str],
  "music_mood": str, "ending": str,
  "scenes": [{{"query": str, "caption": str, "seconds": number}}]}}"""


def storyboard(genre: str, idea: str) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2500,
        messages=[{"role": "user", "content": STORYBOARD_PROMPT.format(genre=genre, idea=idea)}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    sb = json.loads(text)
    for s in sb["scenes"]:
        s["seconds"] = max(2.5, min(7.0, float(s["seconds"])))
    return sb


# ---------------------------------------------------------------- footage

def pexels_search(query: str, used: set[int]) -> str | None:
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        return None
    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": key},
        params={"query": query, "orientation": "portrait", "per_page": 15, "size": "medium"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  pexels {r.status_code} for '{query}'")
        return None
    for video in r.json().get("videos", []):
        if video["id"] in used or video.get("duration", 0) < 3:
            continue
        files = [f for f in video["video_files"]
                 if f.get("height") and f.get("width") and f["height"] >= f["width"]
                 and f["height"] >= 1280 and f.get("file_type") == "video/mp4"]
        if not files:
            continue
        best = min(files, key=lambda f: abs(f["height"] - 1920))
        used.add(video["id"])
        return best["link"]
    return None


def fetch_clip(query: str, dest: Path, used: set[int]) -> bool:
    words = query.split()
    for q in (query, " ".join(words[-2:]), words[-1]):
        url = pexels_search(q, used)
        if url:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            return True
    return False


# ---------------------------------------------------------------- render helpers

def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS)]


def render_scene(src: Path | None, seconds: float, dest: Path, idx: int) -> None:
    fade = f"fade=t=in:st=0:d=0.35,fade=t=out:st={seconds - 0.35:.2f}:d=0.35"
    if src and src.exists():
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"setsar=1,fps={FPS},eq=contrast=1.05:saturation=0.92,vignette=PI/5,{fade}")
        ff("-ss", "0.3", "-t", f"{seconds:.2f}", "-i", str(src), "-vf", vf, "-an", *ENC, str(dest))
    else:
        # fallback: slow moving dark gradient so the video still renders
        hue = (idx * 37) % 360
        # computed at 1/10 resolution then upscaled - smooth and fast
        vf = (f"geq=r='40+30*sin(2*PI*(X/W+T/8))':g='30+20*sin(2*PI*(Y/H+T/9))':b='60+40*cos(2*PI*(X/W-T/7))',"
              f"hue=h={hue},scale={W}:{H}:flags=bicubic,{fade}")
        ff("-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"color=c=black:s={W // 10}x{H // 10}:r={FPS}",
           "-vf", vf, *ENC, str(dest))


def render_end_card(seconds: float, dest: Path) -> None:
    vf = f"vignette=PI/4,fade=t=in:st=0:d=0.5,fade=t=out:st={seconds - 0.6:.2f}:d=0.6"
    ff("-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"color=c=0x0b0b0f:s={W}x{H}:r={FPS}",
       "-vf", vf, *ENC, str(dest))


def ass_time(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_captions(scenes: list[dict], ending: str, end_start: float, end_len: float, dest: Path) -> None:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,5,3,2,90,90,520,1
Style: End,{FONT},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,5,110,110,0,1
Style: Tag,{FONT},44,&H00B4B4B4,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,4,0,1,0,0,2,90,90,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines, t = [], 0.0
    for s in scenes:
        cap = ass_escape(s.get("caption", "").strip())
        if cap:
            a, b = t + 0.25, t + s["seconds"] - 0.2
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,"
                         f"{{\\fad(280,220)\\t(0,280,\\fscx104\\fscy104)\\t(280,500,\\fscx100\\fscy100)}}{cap}")
        t += s["seconds"]
    a, b = end_start + 0.4, end_start + end_len - 0.4
    lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},End,,0,0,0,,{{\\fad(600,500)}}{ass_escape(ending)}")
    lines.append(f"Dialogue: 0,{ass_time(a + 0.8)},{ass_time(b)},Tag,,0,0,0,,{{\\fad(600,500)}}"
                 f"{ass_escape(CHANNEL_NAME.upper())}")
    dest.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


CHORDS = {  # root frequencies of a slow 2-chord loop per mood
    "warm":      [(261.6, 329.6, 392.0), (220.0, 261.6, 329.6)],
    "sad":       [(220.0, 261.6, 329.6), (174.6, 220.0, 261.6)],
    "tense":     [(196.0, 233.1, 293.7), (185.0, 233.1, 277.2)],
    "curious":   [(293.7, 349.2, 440.0), (261.6, 329.6, 392.0)],
    "uplifting": [(261.6, 329.6, 392.0), (293.7, 370.0, 440.0)],
}


def make_music(mood: str, seconds: float, dest: Path) -> None:
    """Use a track from music/<mood>/ or music/ if present, else synthesise an ambient pad."""
    pool = list((MUSIC_DIR / mood).glob("*.mp3")) + list(MUSIC_DIR.glob("*.mp3"))
    fade_out = max(0.0, seconds - 2.5)
    if pool:
        track = random.choice(pool)
        ff("-stream_loop", "-1", "-i", str(track), "-t", f"{seconds:.2f}",
           "-af", f"afade=t=in:d=1.5,afade=t=out:st={fade_out:.2f}:d=2.5,volume=0.8",
           "-c:a", "aac", "-b:a", "192k", str(dest))
        return
    chords = CHORDS.get(mood, CHORDS["warm"])
    half = 4.0
    tones = []
    for f1, f2 in zip(*chords):
        for f, lo in ((f1, 0), (f2, half)):
            gate = f"between(mod(t,{half * 2}),{lo},{lo + half})"
            tones.append(f"0.09*sin(2*PI*{f / 2:.2f}*t)*{gate}")
            tones.append(f"0.04*sin(2*PI*{f:.2f}*t+0.3*sin(2*PI*0.2*t))*{gate}")
    expr = "+".join(tones)
    af = (f"lowpass=f=1800,aecho=0.8:0.85:500|900:0.35|0.25,"
          f"tremolo=f=0.25:d=0.3,afade=t=in:d=2,afade=t=out:st={fade_out:.2f}:d=2.5,volume=1.6")
    ff("-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"aevalsrc='{expr}':s=44100:c=stereo",
       "-af", af, "-c:a", "aac", "-b:a", "192k", str(dest))


def build_video(sb: dict, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    used: set[int] = set()
    parts = []
    for i, s in enumerate(sb["scenes"]):
        raw = work / f"raw_{i:02d}.mp4"
        ok = raw.exists() or fetch_clip(s["query"], raw, used)
        print(f"  scene {i + 1:2d}: {s['query']!r:32} {'footage' if ok else 'fallback'}")
        part = work / f"scene_{i:02d}.mp4"
        render_scene(raw if ok else None, s["seconds"], part, i)
        parts.append(part)

    end_len = 4.0
    end_card = work / "end.mp4"
    render_end_card(end_len, end_card)
    parts.append(end_card)

    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    joined = work / "joined.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(joined))

    body_len = sum(s["seconds"] for s in sb["scenes"])
    total = body_len + end_len
    subs = work / "captions.ass"
    write_captions(sb["scenes"], sb["ending"], body_len, end_len, subs)
    music = work / "music.m4a"
    make_music(sb.get("music_mood", "warm"), total, music)

    final = work / "short.mp4"
    ff("-i", str(joined), "-i", str(music),
       "-vf", f"ass={subs.as_posix()}", *ENC,
       "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(final))
    return final


# ---------------------------------------------------------------- upload

def upload(video: Path, sb: dict) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    tags = [t.lstrip("#") for t in sb.get("tags", [])][:12]
    description = (f"{sb['description']}\n\n{CHANNEL_TAG} #Shorts #NoWords "
                   f"#{tags[0].replace(' ', '') if tags else 'Story'}\n\nFootage: Pexels")
    body = {
        "snippet": {
            "title": f"{sb['title'][:85]} #Shorts",
            "description": description,
            "tags": tags + ["silent stories", "no talking", "shorts"],
            "categoryId": os.getenv("YT_CATEGORY", "1"),  # 1 = Film & Animation
        },
        "status": {
            "privacyStatus": PRIVACY,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": False,  # real stock footage, no AI faces/voices
        },
    }
    req = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True),
    )
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ---------------------------------------------------------------- main

def main() -> int:
    item = next_story()
    if not item:
        print("stories.txt is empty - add more ideas.")
        return 0
    genre, idea = item
    print(f"[{genre}] {idea}")

    OUT.mkdir(exist_ok=True)
    sb = storyboard(genre, idea)
    (OUT / "storyboard.json").write_text(json.dumps(sb, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Title: {sb['title']}  ({len(sb['scenes'])} scenes, mood {sb.get('music_mood')})")

    video = build_video(sb, OUT / "work")
    final = OUT / "short.mp4"
    video.replace(final)
    print(f"Rendered {final}")

    video_id = None
    if SKIP_UPLOAD:
        print("SKIP_UPLOAD=true - download the video from the workflow artifacts.")
    else:
        video_id = upload(final, sb)
        print(f"Uploaded: https://youtube.com/shorts/{video_id}")
        mark_done(idea, video_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
