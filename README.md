# Telegram Video Downloader Bot

A Telegram userbot/bot that automatically downloads videos from channels and chats, uses Google Gemini AI to extract anime titles, and organizes files into a clean folder structure.

## Features

- **AI-powered title extraction** — Google Gemini analyzes messy filenames and captions to identify anime title, season, and episode number
- **Download queue** — sequential processing to avoid overloading the connection
- **Auto file organization** — creates per-show folders and renames files to `Show Title - S01E05.mp4`
- **Title mapper** — remembers user-confirmed title corrections in `mappings.json` for future use
- **Rate limiting** — token-bucket limiter keeps Gemini API calls under 10 req/min
- **Rotating log** — `app.log` with 10 MB cap and 5 backup files, mirrored to stdout for `docker logs`
- **Docker-ready** — single `docker-compose up -d` to run on a Linux server

## Architecture

```
tg_video_downloader/
├── main.py                 # Entry point: Pyrogram client, message handlers
├── config/
│   └── config.py          # Pydantic-settings configuration
├── core/
│   ├── downloader.py      # Pyrogram download_media wrapper + progress bar
│   ├── queue_manager.py   # Async download queue (sequential worker)
│   └── renamer.py         # Filename / folder path generation
├── analyzer/
│   ├── ai_cleaner.py      # Gemini API integration + rate limiter
│   └── mapper.py          # Persistent title mapping (JSON)
├── sessions/              # Pyrogram session files (git-ignored)
├── .env                   # Secrets (git-ignored)
└── .env.template          # Example env file
```

## Requirements

- Python 3.11+
- A Telegram account with API credentials from [my.telegram.org](https://my.telegram.org)
- A bot token from [@BotFather](https://t.me/BotFather)
- A Google Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey)

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

`ALLOWED_USERS` is a comma-separated list of Telegram user IDs permitted to use the bot. Send `/id` to the bot to get your ID.

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

## Usage

1. Add your Telegram user ID to `ALLOWED_USERS` (use `/id` to find it).
2. Send the bot a video, or forward one from a channel.
3. The bot will:
   - Analyze the caption/filename with Gemini AI
   - If the title is already in the mapper — download immediately
   - If the title is new — ask you to confirm the official title, save it, then download
   - If AI fails completely — prompt you to enter title, episode, and season manually
4. The file is saved to `DOWNLOAD_PATH/<Show Title>/<Show Title> - S01E05.mp4`

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
