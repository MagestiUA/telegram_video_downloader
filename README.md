# Telegram Video Downloader Bot

> 🇺🇦 [Українська версія](README.uk.md)

A Telegram userbot/bot that automatically downloads videos from channels and chats, uses DeepSeek AI to extract anime metadata, and organizes files into a clean folder structure. It can also track web series on streaming sites and auto-download new episodes as they appear.

## Features

- **AI-powered metadata extraction** — DeepSeek analyzes messy filenames and captions to identify anime title, season, and episode number
- **Three operating modes** — Normal (per-video AI analysis), Batch (set title+season once, extract episodes in bulk), and Anime tracking (auto-download new episodes from a Telegram channel)
- **🎬 Anime tracking** — send a Telegram link (or just paste one — no command needed), the bot checks for new episodes every 6 hours and downloads the Ukrainian dub automatically
- **Pluggable site handlers** — add support for a new source by dropping in one file
- **Download queue** — sequential processing to avoid overloading the connection
- **Auto file organization** — creates per-show folders and renames files to `Show Title - S01E05.mp4`
- **Title mapper** — remembers user-confirmed title corrections in a SQLite DB (`sessions/mappings.db`) for future use, shared across Normal/Batch/Anime modes
- **Rate limiting** — token-bucket limiter throttles DeepSeek API calls (14 req/min)
- **Rotating log** — `app.log` with 10 MB cap and 5 backup files, mirrored to stdout for `docker logs`
- **Docker-ready** — single `docker-compose up -d` to run on a Linux server
- **CasaOS-compatible** — install and configure directly from the CasaOS UI without editing any files

## Operating Modes

### 📥 Normal Mode _(default)_
Each video is analyzed independently. AI extracts title, season, and episode from the caption or filename.
- If the title is already in the local title-mapper DB → downloads immediately
- If the title is new → bot asks for the official name, saves it, then downloads
- If AI fails completely → bot prompts for manual title / episode / season entry

### 📦 Batch Mode
Best for series where AI keeps misidentifying every episode as S01E01.
- Activate via `/mode` → **Batch** button
- **First video:** AI suggests title (you confirm or correct) → you set the season once
- **Each video:** AI extracts only the episode number using the known title+season context
- If episode extraction fails → bot asks for the episode number
- Completely isolated from the title-mapper DB — no reads or writes
- Session ends after **30 min of inactivity** or via the **⏹ End Session** button

### 🎬 Anime Mode
Tracks an anime on Telegram and automatically downloads new episodes as they're posted, using a second ("userbot") Pyrogram session since the Bot API can't read channel/topic history.
- **Add a title** — send `/anime {url}`, or just paste a `t.me/...` link directly (no command needed); the bot also scans a forwarded video's or photo-post's caption for a "watch online" link, including masked hyperlinks it can't see as plain text (falls back to an LLM to pick the right link out of donation/download/subscribe links when a plain regex can't)
- **Two source shapes supported**, auto-detected from the URL: a shared "media library" channel with one forum topic per title, or a channel dedicated to a single title (auto-joined via invite link, muted, and filed into your **"Аніме Тайтли"** Telegram folder — create that folder once and the bot does the rest)
- Bot resolves the raw/localized title into an official Romaji name via the shared title mapper (asks you to confirm once if unknown), so folder names stay consistent with Normal/Batch mode downloads
- A background checker runs immediately after adding, then **every 6 hours**; captions are cached after first parse so re-checking never re-spends an AI call on an already-known episode
- Downloads **only the Ukrainian dub**; known low-quality/duplicate variants (e.g. "MINI" re-encodes) are skipped
- Downloads **all available episodes and seasons**, skipping ones already on disk (including files downloaded manually before tracking started)
- Duplicate titles are rejected — the same anime can't be tracked twice
- On a successful download, **all authorized users** get a notification with the localized title
- A title stops being tracked (and its dedicated channel is left) when the caption says "N з N" (last episode), or manually via the stop button — either way, up to **~6 months** of tracking max
- `/anime list` (or the 🎬 **Аніме** button next to Normal/Batch) shows tracked titles with a single **🔄✅ Перевірити все** button to check everything on demand, and a **⏹** button per title to stop it; `/anime help` shows full docs

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
│   ├── ai_cleaner.py      # DeepSeek API: full metadata + episode-only extraction
│   └── mapper.py          # Persistent title mapping (SQLite)
├── anime_tracker/           # Anime Mode: series tracking
│   ├── db.py              # SQLite: series + episodes + caption cache
│   ├── checker.py         # Background checker (every 6h) + download orchestration
│   ├── userbot.py         # Second Pyrogram client (personal account, reads channel history)
│   ├── folder.py          # Auto-join / mute / file-into-Telegram-folder / leave
│   └── sites/             # Pluggable site handlers
│       ├── base.py        # BaseSiteHandler interface
│       ├── __init__.py    # Domain → handler registry
│       └── telegram.py    # t.me handler (forum topics + dedicated private channels)
├── sessions/              # Pyrogram sessions + anime.db + mappings.db (git-ignored)
├── .env                   # Secrets for local dev (git-ignored)
└── .env.template          # Example env file
```

### Adding a new source
Create `anime_tracker/sites/yoursite.py` with a subclass of `BaseSiteHandler` (implement `is_valid_url`, `get_series_title`, `list_episodes`, `download`, and optionally `cleanup`), then register it in `anime_tracker/sites/__init__.py`. No other code changes needed.

## Requirements

- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- Bot token from [@BotFather](https://t.me/BotFather)
- DeepSeek API key from [platform.deepseek.com](https://platform.deepseek.com/api_keys)

## Configuration

All settings are read from **environment variables**. Priority order:
1. OS environment (Docker `environment:` block, CasaOS UI) — highest priority
2. `.env` file — fallback for local development

| Variable | Required | Description |
|----------|----------|-------------|
| `API_ID` | ✅ | Telegram API ID from my.telegram.org |
| `API_HASH` | ✅ | Telegram API Hash from my.telegram.org |
| `BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek API key |
| `DOWNLOAD_PATH` | — | Anime download folder inside container (default: `/data/downloads`) |
| `DORAMA_PATH` | — | Legacy fallback path for pre-existing rows from the old Dorama Mode (kept for backward compatibility only; Anime Mode uses `DOWNLOAD_PATH`) |
| `ALLOWED_USERS` | — | Comma-separated Telegram user IDs allowed to use the bot (also recipients of Anime Mode notifications) |
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
| `/anime {url}` | Track an anime and auto-download new episodes |
| `/anime list` | List tracked titles (shared, with check-all + per-title stop buttons) |
| `/anime help` | Anime Mode documentation |

## File Naming

| Content | Format |
|---------|--------|
| Episode | `Anime Title - S01E05.mp4` |
| Movie / clip (no episode) | `Anime Title.mp4` |

## Tech Stack

| Library | Purpose |
|---------|---------|
| [Pyrofork](https://github.com/pyrofork/pyrofork) | Telegram MTProto client (Pyrogram fork) |
| [openai](https://pypi.org/project/openai/) | DeepSeek client (OpenAI-compatible API) |
| [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Environment-based configuration |
| [tgcrypto](https://github.com/pyrogram/tgcrypto) | Fast Telegram encryption |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | HLS (m3u8) downloads (legacy, kept for other site handlers) |
| [httpx](https://www.python-httpx.org/) | Async HTTP client for fetching streaming pages |
| SQLite | Anime Mode series/episode tracking + title mapper |

## License

MIT
