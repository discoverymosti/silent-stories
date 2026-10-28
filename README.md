# Silent Stories 🎬

Automated YouTube Shorts channel. **No voice, no language barrier**: stories told with footage, short captions and music, so they work worldwide.

```
stories.txt ─► Gemini: storyboard + SEO/SMO ─► AI images per scene (Gemini → Cloudflare FLUX → Pollinations,
                                               stock footage fallback)
           ─► auto-edit: Ken Burns moves, crossfades, film look, captions, original music
           ─► AI thumbnail ─► YouTube Short with title/description/hashtags/tags (2 a day Mon-Sat at 01:40 and 14:40 UTC, 1 on Sunday)
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

## Cross-posting: Instagram, Facebook Page, X
After each **public** YouTube upload the workflow also posts:
- **Instagram:** the Short as a Reel (films stay on YouTube/Facebook, Reels are vertical)
- **Facebook Page:** the Short as a Page Reel, the weekly film as a Page video
- **X:** title + hashtags + YouTube link

Nothing is cross-posted while `YT_PRIVACY` is `private`, so review uploads stay private. Each platform is optional and a failure on one never stops the others.

**Facebook Page + Instagram (one-time, ~15 min, free):**
1. Instagram must be a **Professional** account (Creator or Business) **linked to your Facebook Page** (Instagram → Settings → Accounts Center, or Page settings → Linked accounts).
2. developers.facebook.com → **My Apps → Create app** → use case *"Manage everything on your Page"* plus *"Manage messaging & content on Instagram"* (or type **Business**).
3. App settings → Basic: copy **App ID** and **App secret**. Add the privacy policy URL `https://github.com/discoverymosti/silent-stories/blob/main/PRIVACY.md`.
4. **Tools → Graph API Explorer:** choose your app, *Get User Access Token* with `pages_show_list, pages_read_engagement, pages_manage_posts, instagram_basic, instagram_content_publish, business_management`, then **Generate Access Token** and approve your Page + Instagram.
5. GitHub secrets: `META_APP_ID`, `META_APP_SECRET`, `META_USER_TOKEN` (the token from step 4). Optional variable `META_PAGE_NAME` if you manage several Pages.
6. **Actions → Connect Facebook & Instagram → Run workflow.** The log shows the Page and @instagram it connected. It saves a non-expiring Page token encrypted as `meta_token.enc`; you can then delete `META_USER_TOKEN`.
7. Switch the app to **Live** (top bar) so posts are public.

**X (one-time, ~10 min):**
1. developer.x.com → sign up for the **Free** tier → your Project's App → **User authentication settings**: *Read and write*.
2. **Keys and tokens:** copy *API Key*, *API Key Secret*, then generate *Access Token* and *Secret* (after setting Read and write).
3. GitHub secrets: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`.

## Weekly short film
Every **Sunday 16:10 UTC** (12:10 New York, 17:10 Paris, 21:40 India) the same workflow makes a **~3 minute widescreen (16:9) silent short film**: 18-24 AI scenes in three acts, captions, original score, watermark and thumbnail. It uploads as a normal video (not a Short), from the queue in `films.txt`.
Run one now: **Actions → Silent Stories - Publish Short → Run workflow → format: film**.

### How films stay consistent (film-crew workflow)
1. **Blueprint first:** the AI writes the story, then saves **cast** elements (fixed look, clothes) and **location** elements (fixed place, light, time of day).
2. **Structured shots:** every scene is labelled data: shot type, action, cast, location, lighting and camera move. It becomes a labelled prompt that repeats the exact saved descriptions.
3. **One colour grade per film** (warm, cool, teal-orange, golden, muted or noir) is applied to every shot so they cut together.

## Growth features
- **8 languages:** each upload carries its title and description in Spanish, Portuguese, Hindi, Indonesian, Arabic, French, German and Japanese (YouTube localizations). Viewers see them in their own language; the videos have no speech, so this removes the last language barrier. Change the list with the `YT_LANGUAGES` variable (e.g. `es,pt,hi`), empty = English only.
- **Loop-friendly Shorts:** the first frame is the hook image (no fade from black) and the Short ends on its last image with the closing line, not a black card, so replays loop smoothly. The final shot echoes the first.
- **Learns from your stats:** when new ideas are generated, the AI sees your most and least viewed uploads (read-only YouTube access, 1 quota unit) and leans toward what your audience watches.
- **Right category:** explainers and facts upload as *Education*, stories as *Film & Animation* (override with `YT_CATEGORY`).
- **Self-repairing storyboards:** an incomplete AI answer is repaired or asked for again instead of failing the run.

## Watermark
Every video carries a semi-transparent **DiscoveryMosti** watermark (top-left on Shorts, clear of the Shorts buttons; bottom-right on films). Change it with the `WATERMARK` variable (empty = none). For YouTube's clickable *subscribe* watermark, also upload your logo in YouTube Studio → Customisation → Branding → Video watermark.

## Daily use
- **Ideas refill themselves:** when `stories.txt` drops below 5 ideas (or `films.txt` below 2), the AI adds fresh original ones that don't repeat past videos. You can still **add your own** (`genre | idea`), editing on github.com works fine. Each upload removes the top line and logs the link in `done.txt`.
- **Better music:** drop royalty-free mp3s in `music/` (see `music/README.md`). Without them, a generated ambient pad is used.
- **Never misses, never doubles:** GitHub can delay or drop scheduled runs when it is busy, so each slot has a catch-up run 2 hours later. Every scheduled run first checks `done.txt` and skips if the same kind of video was uploaded in the last 4 hours.
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
| `YT_LANGUAGES` | `es,pt,hi,id,ar,fr,de,ja` (translated titles/descriptions) |
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
| Cloudflare Workers AI (FLUX) | AI images | 10,000 neurons/day (resets 00:00 UTC) | ~12 images/Short, ~22/film; Sunday has one Short + the film so the day stays inside the allowance | free |
| YouTube Data API | upload + thumbnail | 10,000 quota units/day | ~1,650 units/video | free |
| Google Cloud project (OAuth) | sign-in for uploads | free, no billing account needed | n/a | free |
| Music | original synthesized score | n/a | n/a | free |
| Meta Graph API | Instagram Reels + Facebook Page | free (rate limits: 50 IG posts/day) | 1-2 posts/day | free |
| X API (Free tier) | posts with the YouTube link | free write tier (check developer.x.com for the current monthly post cap) | ~2 posts/day | free |

### Free AI backups (optional, all free tiers, no card)
If the main provider is busy or out of free quota, the next one takes over automatically. Add any of these as repository secrets; leave out the ones you don't want.

| Secret | Used for | Where to get it (free) |
|---|---|---|
| `GROQ_API_KEY` | story text (fast Llama) | console.groq.com → API Keys |
| `OPENROUTER_API_KEY` | story text (only `:free` models) | openrouter.ai → Keys |
| `HF_TOKEN` | images (FLUX, small monthly free credits) | huggingface.co → Settings → Access Tokens (Read) |
| `TOGETHER_API_KEY` | images (`FLUX.1-schnell-Free`) | api.together.ai → API Keys |
| `POLLINATIONS_TOKEN` | images and text (daily free allowance; without a token only ~1 image) | pollinations.ai → sign in → API token |
| `AI_HORDE_KEY` | images: queue priority on AI Horde's free community GPUs (works without a key too) | aihorde.net → Register (free) |

Built in, no signup: **Pollinations** text and images, and **Cloudflare text models** (reuse your Cloudflare key, last resort because they share the image allowance). Gemini tries Flash, Flash-Lite and Pro models, each with its own free quota.

Order: text = Gemini → Groq → OpenRouter → Pollinations → Cloudflare; images = Cloudflare **FLUX.2 [klein] 4B** (true 9:16 / 16:9 frames; FLUX.1 [schnell] if klein refuses a prompt) → Hugging Face → Together → Pollinations → **AI Horde** (free volunteer GPUs, no signup; every image still missing is sent at once and they run in parallel, Z-Image-Turbo / FLUX.1-schnell / SDXL, commercial-use licences only) → (stock footage if `PIXABAY_API_KEY`/`PEXELS_API_KEY`). Run **Actions → Check free AI providers** to see which ones work.

**Free-only mode is ON by default** (`FREE_ONLY` variable, default `true`): the pipeline never calls Gemini image generation or the Claude API, the only two paths here that can bill. Cloudflare's free plan simply stops for the day at its limit (the video waits for tomorrow, nothing is charged), and GitHub, YouTube and Google sign-in are free. Keep it this way for a guaranteed ₹0.

**What would cost money (only if you turn it on):** enabling billing on the Gemini API key (Gemini images, about $0.04/image ≈ $15-20/month for daily Shorts), Cloudflare Workers Paid ($5/month, only if you want more than ~10k neurons/day, e.g. more videos per day), making the repo **private** (GitHub Free includes 2,000 Actions minutes/month, enough for this; beyond that ~$0.008/min), or `ANTHROPIC_API_KEY` instead of Gemini (~₹1-2/video). Never add a payment method you don't intend to use.

## Good to know
- **Unverified API project:** YouTube may lock API uploads to *private* until your Google Cloud project passes the [YouTube API audit](https://support.google.com/youtube/contact/yt_api_form). Until then, flip each video to public in YouTube Studio (one click), or submit the audit form.
- **Quota:** 10,000 units/day; one upload = 1,600, so up to 6/day.
- **Reused content policy:** YouTube demonetises low-effort mass-produced content. Keep ideas original, check previews, and mix in some hand-made Shorts. Monetisation needs 1,000 subs + 10M Shorts views in 90 days.
- **Footage credit:** Pixabay and Pexels licences allow commercial use without attribution; the description still says "Footage: Pixabay/Pexels".
- **Cross-posting:** the same `short.mp4` works on Instagram Reels and TikTok. Download it from Artifacts.
