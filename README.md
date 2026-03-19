# Telegram Video Downloader Bot

> 🇺🇦 [Українська версія](README.uk.md)

A Telegram userbot/bot that automatically downloads videos from channels and chats, uses Google Gemini AI to extract anime metadata, and organizes files into a clean folder structure.

## Features

- **AI-powered metadata extraction** — Google Gemini analyzes messy filenames and captions to identify anime title, season, and episode number
- **Two operating modes** — Normal (per-video AI analysis) and Batch (set title+season once, extract episodes in bulk)
- **Download queue** — sequential processing to avoid overloading the connection
- **Auto file organization** — creates per-show folders and renames files to `Show Title - S01E05.mp4`
- **Title mapper** — remembers user-confirmed title corrections in `mappings.json` for future use _(Normal mode only)_
- **Rate limiting** — token-bucket limiter keeps Gemini API calls under 10 req/min
- **Rotating log** — `app.log` with 10 MB cap and 5 backup files, mirrored to stdout for `docker logs`
- **Docker-ready** — single `docker-compose up -d` to run on a Linux server
- **CasaOS-compatible** — install and configure directly from the CasaOS UI without editing any files

## Operating Modes

### 📥 Normal Mode _(default)_
Each video is analyzed independently. AI extracts title, season, and episode from the caption or filename.
- If the title is already in the local DB (`mappings.json`) → downloads immediately
- If the title is new → bot asks for the official name, saves it, then downloads
- If AI fails completely → bot prompts for manual title / episode / season entry

### 📦 Batch Mode
Best for series where AI keeps misidentifying every episode as S01E01.
- Activate via `/mode` → **Batch** button
- **First video:** AI suggests title (you confirm or correct) → you set the season once
- **Each video:** AI extracts only the episode number using the known title+season context
- If episode extraction fails → bot asks for the episode number
- Completely isolated from `mappings.json` — no reads or writes
- Session ends after **30 min of inactivity** or via the **⏹ End Session** button

## Architecture

```
tg_video_downloader/
├── main.py                 # Entry point: Pyrogram client, handlers, mode logic
├── config/
│   └── config.py          # Pydantic-settings configuration
├── core/
│   ├── downloader.py      # Pyrogram download_media wrapper + progress bar
│   ├── queue_manager.py   # Async download queue (sequential worker)
│   └── renamer.py         # Filename / folder path generation
├── analyzer/
│   ├── ai_cleaner.py      # Gemini API: full metadata + episode-only extraction
│   └── mapper.py          # Persistent title mapping (JSON)
├── sessions/              # Pyrogram session files (git-ignored)
├── .env                   # Secrets for local dev (git-ignored)
└── .env.template          # Example env file
```

## Requirements

- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- Bot token from [@BotFather](https://t.me/BotFather)
- Google Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey)

## Configuration

All settings are read from **environment variables**. Priority order:
1. OS environment (Docker `environment:` block, CasaOS UI) — highest priority
2. `.env` file — fallback for local development

| Variable | Required | Description |
|----------|----------|-------------|
| `API_ID` | ✅ | Telegram API ID from my.telegram.org |
| `API_HASH` | ✅ | Telegram API Hash from my.telegram.org |
| `BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `GEMINI_API_KEY` | ✅ | Google Gemini API key |
| `DOWNLOAD_PATH` | — | Download folder inside container (default: `/data/downloads`) |
| `ALLOWED_USERS` | — | Comma-separated Telegram user IDs allowed to use the bot |
| `SESSION_STRING` | — | Pyrogram session string — required for Docker (avoids interactive login) |

`ALLOWED_USERS`: send `/id` to the bot to find your Telegram user ID.

## Setup

### Option A: CasaOS (recommended for home servers)

1. In CasaOS → **App Store** → **Custom Install** → paste the contents of `docker-compose.yml`
2. Fill in credentials directly in the CasaOS UI — Volumes and Environment Variables sections
3. Click **Submit**

All fields have inline descriptions. No file editing required.

### Option B: Docker (standard)

Edit `docker-compose.yml` — fill in the empty `environment:` values, then:

```bash
docker-compose build
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Option C: Local development

Copy `.env.template` to `.env` and fill in your values:

```bash
cp .env.template .env
```

```bash
python -m venv .venv

# Linux/macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and your User ID |
| `/id` | Get your Telegram User ID |
| `/help` | Show help and mode descriptions |
| `/mode` | Switch between Normal and Batch mode |

## File Naming

| Content | Format |
|---------|--------|
| Episode | `Anime Title - S01E05.mp4` |
| Movie / clip (no episode) | `Anime Title.mp4` |

## Tech Stack

| Library | Purpose |
|---------|---------|
| [Pyrofork](https://github.com/pyrofork/pyrofork) | Telegram MTProto client (Pyrogram fork) |
| [google-genai](https://pypi.org/project/google-genai/) | Google Gemini AI SDK |
| [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Environment-based configuration |
| [tgcrypto](https://github.com/pyrogram/tgcrypto) | Fast Telegram encryption |

## License

MIT
