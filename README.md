<div align="center">

# FetchClip

**A Telegram bot that turns public media links into downloadable video or audio.**

[![Python](https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![FastAPI](https://img.shields.io/badge/FastAPI-webhook-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vercel](https://img.shields.io/badge/Deploy-Vercel-000000?logo=vercel&logoColor=white)](https://vercel.com/)

</div>

---

## What FetchClip does

FetchClip is a Telegram media-fetching bot. A user sends a public `http://` or
`https://` link, chooses **Video** or **Audio**, and receives the result in the
same chat.

The bot:

- Detects links in ordinary Telegram text messages.
- Shows the title, duration, available modes, estimated size, and source link.
- Fetches YouTube media through the configured RapidAPI direct-media service.
- Uses `yt-dlp` for other supported sites, including sites supported by the
  installed yt-dlp extractors.
- Selects a video stream at or below the configured height where possible.
- Selects a compact audio stream and uses FFmpeg to extract audio when needed.
- Sends files below Telegram's bot upload limit directly in the chat.
- Returns a direct download button when a file is too large to upload through
  Telegram.
- Cleans temporary download files after each processing attempt.

Only download content that you own or have permission to save. Availability and
access also depend on the source website, its terms, authentication, and
regional restrictions.

## Architecture

```text
                         Telegram user
                  sends link / taps a button
                                |
                                v
                   Telegram Bot API webhook
                                |
                                v
                 Vercel Python Function (app.py)
                 FastAPI + python-telegram-bot
                    /                  \
                   /                    \
                  v                      v
       YouTube: RapidAPI service       Other sites: yt-dlp
                  |                      |
                  |                 FFmpeg when required
                  \                      /
                   \                    /
                    v                  v
                 Direct Telegram upload or download URL
```

### Runtime components

| Component | Responsibility |
|---|---|
| **Telegram Bot API** | Receives webhook updates and delivers messages, buttons, media, and download links. |
| **FastAPI** | Exposes the health endpoint and the Telegram webhook endpoint. |
| **python-telegram-bot** | Builds the Telegram application and handles `/start`, text links, and inline-button callbacks. |
| **Vercel Functions** | Hosts `app.py` as a serverless ASGI function. The function timeout is configured for 300 seconds. |
| **RapidAPI YouTube service** | Provides direct video/audio URLs for YouTube links in the primary YouTube path. |
| **yt-dlp** | Extracts metadata and downloads supported non-YouTube media formats. |
| **FFmpeg / imageio-ffmpeg** | Merges separate video/audio streams and extracts M4A audio. |
| **httpx** | Calls RapidAPI and checks direct-media response metadata. |
| **Temporary filesystem** | Holds downloads during one job; output directories are deleted afterward. |

There is no application database. In-memory sets, dictionaries, and a bounded
update deque provide concurrency limits, cooldowns, and duplicate-update
protection for the lifetime of one running server instance. Serverless instances
are ephemeral, so these controls are not a durable shared queue or database.

## Request and download flow

### 1. Webhook reception

Telegram sends updates to `POST /api/webhook`. The handler:

1. Requires `BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`.
2. Compares Telegram's `X-Telegram-Bot-Api-Secret-Token` header with the
   configured secret using constant-time comparison.
3. Parses the JSON update and ignores duplicate update IDs seen by that
   instance.
4. Processes the update inline only when `INLINE_PROCESSING=1`; otherwise it
   schedules background processing and returns promptly to Telegram.

### 2. Link validation and inspection

When a user sends text, FetchClip extracts the first HTTP(S) URL and rejects:

- non-HTTP(S) schemes;
- localhost, private, loopback, link-local, reserved, or multicast IP targets;
- hosts that resolve to non-public addresses;
- hosts outside `ALLOWED_DOMAINS` when that allowlist is configured;
- requests made during the per-user inspection cooldown.

It then inspects the media and displays available Video and Audio buttons with
best-effort size and duration estimates.

### 3. Media selection

For YouTube, the callback uses the RapidAPI configuration and requests a direct
media URL. For other supported URLs, yt-dlp metadata is inspected again before
choosing a format:

- video prefers the highest available quality at or below `LOW_HEIGHT`;
- audio prefers an audio-only stream at or below the target bitrate;
- separate streams are combined only when FFmpeg is available;
- playlists are restricted to their first item;
- downloads are limited by `MAX_MB` through yt-dlp's `max_filesize` option.

### 4. Delivery

The bot checks the estimated or actual file size against Telegram's approximately
50 MB bot-upload limit. Smaller results are uploaded as video, audio, or a
document. Larger results receive a direct download button when a direct URL can
be obtained. Each temporary output directory is removed in a `finally` block.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Health/status response. |
| `GET` | `/api/health` | Same health/status response. |
| `POST` | `/api/webhook` | Receives authenticated Telegram updates. |

Example health response:

```json
{
  "ok": true,
  "bot_token_set": true,
  "webhook_secret_set": true,
  "ffmpeg": true
}
```

The health endpoint reports configuration presence, not whether a token is
valid or whether every source website is available.

## Project structure

```text
fetchclip_vercel/
├── app.py                 # FastAPI app, Telegram handlers, validation, extraction, and delivery
├── set_webhook.py         # Registers the Vercel webhook and bot command menu
├── run_local.py           # Runs the same handlers with local long polling
├── debug_page.py          # Optional manual HTML/media inspection helper
├── pyproject.toml         # Package metadata and production dependencies
├── requirements-local.txt # Local runner, dotenv, and Uvicorn dependencies
├── vercel.json            # Vercel function timeout configuration
├── .env.example           # Safe configuration template
├── .gitignore             # Prevents secrets, cookies, environments, and generated files from being committed
├── README.md              # This documentation
└── Readme(local).md       # Short local-deployment notes
```

`YT_COOKIES.txt` may exist locally for reference or export purposes, but it is
not part of the deployable source and is ignored by Git. The application reads
the optional `YT_COOKIES` environment variable and writes it to `/tmp/cookies.txt`
at startup.

## Configuration

Set production values in **Vercel → Project → Settings → Environment Variables**.
For local development, copy `.env.example` to `.env`. Never commit `.env`,
cookies, bot tokens, or API keys.

| Variable | Required | Default | Description |
|---|---:|---:|---|
| `BOT_TOKEN` | Yes | — | Telegram token from [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_WEBHOOK_SECRET` | Yes on Vercel | — | Long random value Telegram sends with each webhook request. |
| `RAPID_API_KEY` | YouTube path | — | RapidAPI key for the configured YouTube media service. |
| `RAPID_API_HOST` | No | `youtube-media-downloader.p.rapidapi.com` | RapidAPI host header. |
| `RAPID_API_URL` | No | `https://youtube-media-downloader.p.rapidapi.com/v2/video/details` | RapidAPI endpoint. |
| `MAX_MB` | No | `100` in code | yt-dlp download size ceiling. Keep this below Telegram's upload limit for direct uploads. |
| `LOW_HEIGHT` | No | `720` in code | Preferred maximum video height. |
| `MAX_PARALLEL` | No | `2` | Maximum concurrent extraction/download jobs per instance. |
| `USER_COOLDOWN` | No | `10` | Seconds between downloads for one user. |
| `CHECK_COOLDOWN` | No | `3` | Seconds between link inspections for one user. |
| `ALLOWED_DOMAINS` | No | empty | Comma-separated host allowlist, such as `youtube.com,reddit.com`. |
| `OWNER_ID` | No | `0` | Telegram user ID allowed to receive a short raw error detail for debugging. |
| `INLINE_PROCESSING` | No | `0` | Set to `1` temporarily to process before the webhook responds; useful for logs. |
| `YT_COOKIES` | No | empty | Netscape-format cookie text for sources that require authentication. Treat it as a secret. |

The code currently defaults `MAX_MB` to 100 and `LOW_HEIGHT` to 720, while the
example file suggests safer deployment values of 48 MB and 360p. Explicit
environment variables override the code defaults.

## Deploy to Vercel

### Prerequisites

- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A GitHub repository and a Vercel account.
- A RapidAPI account/key if YouTube downloads are required.
- Python 3.12 or 3.13 for local testing.

### 1. Import the repository

Import this repository into Vercel. Vercel detects `app.py` and uses the
top-level ASGI variable named `app`. `vercel.json` sets the function maximum
duration to 300 seconds.

### 2. Add environment variables

Add at least `BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`. Add `RAPID_API_KEY`
for the YouTube path and any optional tuning variables required by your
deployment. Redeploy after changing Vercel environment variables.

### 3. Register the webhook

From your own computer:

```powershell
Copy-Item .env.example .env
# Edit .env and provide BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET
python set_webhook.py https://YOUR-PROJECT.vercel.app
```

The script registers:

```text
https://YOUR-PROJECT.vercel.app/api/webhook
```

It also sets the `/start` command and prints Telegram's webhook status,
including the latest error message when one exists.

### 4. Verify

Open `https://YOUR-PROJECT.vercel.app/` and confirm the health response. Then
send `/start` to the bot and try a short public media link.

## Run locally

Use a separate test bot token when possible. Long polling removes the webhook
for the token it uses, so running this against a production token temporarily
stops the Vercel deployment from receiving updates.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-local.txt
Copy-Item .env.example .env
# Edit .env and provide at least BOT_TOKEN
python run_local.py
```

For a local health check, start Uvicorn in another terminal:

```powershell
uvicorn app:app --port 8000
```

Then open `http://127.0.0.1:8000/`. To return a production bot to Vercel
after local polling, run `set_webhook.py` again with the deployed URL.

## Security and privacy

- **Secrets:** `.env`, cookie files, virtual environments, Vercel state, and
  generated media are ignored. The repository previously contained a tracked
  `YT_COOKIES.txt`; it must remain untracked and should be rotated if it was
  ever used outside your control.
- **Webhook authentication:** requests without the configured Telegram secret
  header are rejected with HTTP 403.
- **URL safety:** public-address checks reduce SSRF risk, but they do not make
  arbitrary third-party content safe or guarantee source-site behavior.
- **Transport:** RapidAPI direct-media requests currently use HTTPS URLs. The
  implementation disables TLS verification for those `httpx` calls to match the
  existing integration; review this before using the service for sensitive
  workloads.
- **Cookies:** cookie values can grant access to accounts. Store them only in
  Vercel's encrypted environment variables or a protected local environment.
- **Temporary data:** media is processed in temporary storage and removed after
  the callback finishes, although platform logs, Telegram, source providers,
  and CDN links have their own retention policies.
- **Legal use:** respect copyright, website terms, authentication boundaries,
  and applicable law.

Ignoring a file does not remove a file that is already tracked. After cloning
or updating this repository, verify with:

```powershell
git status --short
git check-ignore -v .env YT_COOKIES.txt cookies.txt
```

## Troubleshooting

| Symptom | Checks |
|---|---|
| Health response shows `false` | Confirm the matching Vercel environment variable exists and redeploy. |
| Bot is silent | Check Vercel logs, run `set_webhook.py` again, and inspect `getWebhookInfo` output. |
| YouTube fails | Confirm `RAPID_API_KEY`, host, endpoint, quota, and API response format. |
| Other sites fail | Try a shorter public link, inspect Vercel logs, and check whether yt-dlp still supports the site. |
| FFmpeg is `false` | Confirm `imageio-ffmpeg` installed successfully; separate streams and audio extraction may be unavailable. |
| Telegram rejects an upload | Lower `MAX_MB` or `LOW_HEIGHT`; Telegram's bot upload limit is approximately 50 MB. |
| Local testing interrupts production | Use a separate test token, then restore the production webhook with `set_webhook.py`. |
| Authentication-required source fails | Provide valid `YT_COOKIES` only when you are authorized to access that content. |

## License and responsibility

This repository is a personal automation project. Add a license before
redistributing it. The operator is responsible for credentials, source-site
compliance, user privacy, and the media downloaded through the bot.

<div align="center">

Built with Python, FastAPI, Telegram, yt-dlp, FFmpeg, RapidAPI, and Vercel.

</div>
