# Silent Stories 🎬

Automated YouTube Shorts channel. **No voice, no language barrier**: stories told with footage, short captions and music, so they work worldwide.

```
stories.txt ─► Claude storyboard ─► Pexels vertical footage ─► ffmpeg 1080×1920
           (scenes, captions, mood)                         (fades, animated captions,
                                                             ambient music, end card)
                                                ─► YouTube Short (daily, 18:00 IST)
```

## One-time setup (≈25 min)

### 1. API keys → GitHub secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Get it from | Cost |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys | ~₹1–2 per video |
| `PEXELS_API_KEY` | pexels.com/api → "Get started" | free |
| `YT_CLIENT_ID` / `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN` | step 2 | free |

### 2. Connect your YouTube channel
On your computer:
```bash
git clone https://github.com/<you>/silent-stories && cd silent-stories
pip install google-auth-oauthlib google-api-python-client
python scripts/connect_youtube.py
```
The file's header walks you through the Google Cloud console (enable YouTube Data API v3, create a Desktop OAuth client, **publish the consent screen**). The script prints your channel name plus the 3 secrets.

### 3. Preview
**Actions → Silent Stories - Publish Short → Run workflow** (keep *Preview only* ticked). After about 3 minutes, download `short-N` from the run's Artifacts.

### 4. Go live
- Run once with *Preview only* unticked. The Short uploads as **private**. Check it in YouTube Studio.
- Happy? Repo → Settings → Variables → add `YT_PRIVACY` = `public`. From then on it posts daily on its own.

## Daily use
- **Add ideas** to `stories.txt` (`genre | idea`), editing it on github.com works fine. Each upload removes the top line and logs the link in `done.txt`.
- **Better music:** drop royalty-free mp3s in `music/` (see `music/README.md`). Without them, a generated ambient pad is used.
- **Change schedule:** edit the `cron` line in `.github/workflows/publish.yml` (times are UTC; `30 12` = 18:00 IST).

## Optional variables
| Variable | Default |
|---|---|
| `YT_PRIVACY` | `private` |
| `CHANNEL_NAME` | `Silent Stories` (shown on end card) |
| `CHANNEL_HASHTAG` | `#SilentStories` |

## Good to know
- **Unverified API project:** YouTube may lock API uploads to *private* until your Google Cloud project passes the [YouTube API audit](https://support.google.com/youtube/contact/yt_api_form). Until then, flip each video to public in YouTube Studio (one click), or submit the audit form.
- **Quota:** 10,000 units/day; one upload = 1,600, so up to 6/day.
- **Reused content policy:** YouTube demonetises low-effort mass-produced content. Keep ideas original, check previews, and mix in some hand-made Shorts. Monetisation needs 1,000 subs + 10M Shorts views in 90 days.
- **Cross-posting:** the same `short.mp4` works on Instagram Reels and TikTok. Download it from Artifacts.
