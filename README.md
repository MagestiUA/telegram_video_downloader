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
├── .env                   # Secrets (git-ignored)
└── .env.template          # Example env file
```

## Requirements

- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- Bot token from [@BotFather](https://t.me/BotFather)
- Google Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey)

## Setup

### 1. Environment variables

Copy `.env.template` to `.env` and fill in your values:

```env
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
BOT_TOKEN=your_bot_token

GEMINI_API_KEY=your_gemini_api_key

DOWNLOAD_PATH=/data/downloads
ALLOWED_USERS=123456789,987654321

# Optional: session string for Docker (avoids interactive login)
SESSION_STRING=
```

`ALLOWED_USERS` is a comma-separated list of Telegram user IDs permitted to use the bot. Send `/id` to the bot to find yours.

### 2. Run with Docker (recommended)

Edit the volume paths in `docker-compose.yml` to match your setup, then:

```bash
docker-compose build
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### 3. Run locally

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
