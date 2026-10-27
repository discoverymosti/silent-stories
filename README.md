# Silent Stories 🎬

Automated YouTube Shorts channel. **No voice, no language barrier**: stories told with footage, short captions and music, so they work worldwide.

```
stories.txt ─► Gemini: storyboard + SEO/SMO ─► AI images per scene (Gemini → Cloudflare FLUX → Pollinations,
                                               stock footage fallback)
           ─► auto-edit: Ken Burns moves, crossfades, film look, captions, original music
           ─► AI thumbnail ─► YouTube Short with title/description/hashtags/tags (2 a day: 01:40 and 14:40 UTC)
```

## One-time setup (≈25 min)

### 1. API keys → GitHub secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Get it from | Cost |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com → Get API key | free tier |
| `ANTHROPIC_API_KEY` *(optional, instead of Gemini)* | console.anthropic.com → API Keys | ~₹1–2 per video |
| `CF_ACCOUNT_ID` / `CF_API_TOKEN` *(recommended: AI images when Gemini's free image quota is 0)* | dash.cloudflare.com → **Workers AI** → *Use REST API*: copy the Account ID, click *Create a Workers AI API Token* | free daily allowance |
| `POLLINATIONS_TOKEN` *(optional)* | auth.pollinations.ai (anonymous use now returns 402) | free tier |
| `PIXABAY_API_KEY` *(optional fallback)* | pixabay.com/api/docs (sign in, key is shown on the page) | free |
| `PEXELS_API_KEY` *(optional extra source)* | pexels.com/api | free |
| `YT_CLIENT_ID` / `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN` | step 2 | free |

### 2. Connect your YouTube channel
**No computer needed:** Actions → **Connect YouTube channel** → Run workflow (leave *code* empty). Open the sign-in link from the log, tap Allow, copy the `http://localhost/?code=...` address you land on, then run the workflow again with it pasted into *code*. The log shows your channel name; the token is saved encrypted as `yt_token.enc`. (Web OAuth clients need `http://localhost` under *Authorized redirect URIs*.)

**Or on your computer:**
```bash
git clone https://github.com/discoverymosti/silent-stories && cd silent-stories
pip install google-auth-oauthlib google-api-python-client
python scripts/connect_youtube.py
```
The file's header walks you through the Google Cloud console (enable YouTube Data API v3, create a Desktop OAuth client, **publish the consent screen**). The script prints your channel name plus the 3 secrets.

### 3. Preview
**Actions → Silent Stories - Publish Short → Run workflow** (keep *Preview only* ticked). After about 3 minutes, download `short-N` from the run's Artifacts.

### 4. Go live
- Run once with *Preview only* unticked. The Short uploads as **private**. Check it in YouTube Studio.
- Happy? Repo → Settings → Variables → add `YT_PRIVACY` = `public`. From then on it posts daily on its own.

## Weekly short film
Every **Sunday 16:10 UTC** (12:10 New York, 17:10 Paris, 21:40 India) the same workflow makes a **~3 minute widescreen (16:9) silent short film**: 26-34 AI scenes in three acts, captions, original score, watermark and thumbnail. It uploads as a normal video (not a Short), from the queue in `films.txt`.
Run one now: **Actions → Silent Stories - Publish Short → Run workflow → format: film**.

### How films stay consistent (film-crew workflow)
1. **Blueprint first:** the AI writes the story, then saves **cast** elements (fixed look, clothes) and **location** elements (fixed place, light, time of day).
2. **Structured shots:** every scene is labelled data: shot type, action, cast, location, lighting and camera move. It becomes a labelled prompt that repeats the exact saved descriptions.
3. **One colour grade per film** (warm, cool, teal-orange, golden, muted or noir) is applied to every shot so they cut together.

## Watermark
Every video carries a semi-transparent **DiscoveryMosti** watermark (top-left on Shorts, clear of the Shorts buttons; bottom-right on films). Change it with the `WATERMARK` variable (empty = none). For YouTube's clickable *subscribe* watermark, also upload your logo in YouTube Studio → Customisation → Branding → Video watermark.

## Daily use
- **Ideas refill themselves:** when `stories.txt` drops below 5 ideas (or `films.txt` below 2), the AI adds fresh original ones that don't repeat past videos. You can still **add your own** (`genre | idea`), editing on github.com works fine. Each upload removes the top line and logs the link in `done.txt`.
- **Better music:** drop royalty-free mp3s in `music/` (see `music/README.md`). Without them, a generated ambient pad is used.
- **Change schedule:** edit the `cron` line in `.github/workflows/publish.yml` (times are UTC; `40 14` = 14:40 UTC, `10 16 * * 0` = Sunday film).

## Optional variables
| Variable | Default |
|---|---|
| `YT_PRIVACY` | `private` |
| `GEMINI_MODEL` | auto (newest Gemini Flash) |
| `GEMINI_IMAGE_MODEL` | auto (newest Gemini image model) |
| `VISUAL_MODE` | `ai` (original AI images) or `stock` (Pixabay/Pexels) |
| `CHANNEL_NAME` | `DiscoveryMosti` (shown on end card) |
| `CHANNEL_HASHTAG` | `#DiscoveryMosti` |
| `WATERMARK` | `DiscoveryMosti` |
| `MAX_FALLBACK_SCENES` | `2` for Shorts, `4` for films: skip the upload if more scenes than this end up as plain gradient filler |

## Copyright-safe by design
- Visuals: original AI images (Gemini / Cloudflare FLUX / Pollinations), no stock or third-party clips unless AI fails.
- Quality gate: a video with too many filler scenes is not uploaded (protects monetisation from the low-effort/reused content policy).
- Music: synthesized in the pipeline (original), or your own royalty-free files in `music/`.
- AI disclosure: uploads set *altered or synthetic content* when AI visuals are used, as YouTube requires.
- Custom thumbnails need a phone-verified channel: youtube.com/verify

## Monthly cost: ₹0 / $0 with the current setup
| Service | Used for | Free allowance | This channel uses | Cost |
|---|---|---|---|---|
| GitHub Actions | rendering + uploading | unlimited minutes on a **public** repo | ~20 min/day + ~40 min/week | free |
| Gemini API (AI Studio, free tier) | storyboard, SEO title/description/tags | free-tier daily request limits | 1 request/video | free |
| Cloudflare Workers AI (FLUX) | AI images | 10,000 neurons/day | ~12 images/Short x2, ~32/film: under the limit | free |
| YouTube Data API | upload + thumbnail | 10,000 quota units/day | ~1,650 units/video | free |
| Google Cloud project (OAuth) | sign-in for uploads | free, no billing account needed | n/a | free |
| Music | original synthesized score | n/a | n/a | free |

**What would cost money (only if you turn it on):** enabling billing on the Gemini API key (Gemini images, about $0.04/image ≈ $15-20/month for daily Shorts), Cloudflare Workers Paid ($5/month, only if you exceed 10k neurons/day), making the repo **private** (GitHub Free includes 2,000 Actions minutes/month, enough for this; beyond that ~$0.008/min), or `ANTHROPIC_API_KEY` instead of Gemini (~₹1-2/video). Never add a payment method you don't intend to use.

## Good to know
- **Unverified API project:** YouTube may lock API uploads to *private* until your Google Cloud project passes the [YouTube API audit](https://support.google.com/youtube/contact/yt_api_form). Until then, flip each video to public in YouTube Studio (one click), or submit the audit form.
- **Quota:** 10,000 units/day; one upload = 1,600, so up to 6/day.
- **Reused content policy:** YouTube demonetises low-effort mass-produced content. Keep ideas original, check previews, and mix in some hand-made Shorts. Monetisation needs 1,000 subs + 10M Shorts views in 90 days.
- **Footage credit:** Pixabay and Pexels licences allow commercial use without attribution; the description still says "Footage: Pixabay/Pexels".
- **Cross-posting:** the same `short.mp4` works on Instagram Reels and TikTok. Download it from Artifacts.
