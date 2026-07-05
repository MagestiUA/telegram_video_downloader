# Changelog

All notable changes are documented here.

---

## [2026-06-16] - Switch from Google Gemini to DeepSeek

### Changed
- **AI provider swapped from Google Gemini to DeepSeek** — metadata extraction now
  uses `deepseek-v4-flash` via the OpenAI-compatible API (`https://api.deepseek.com`)
- `analyzer/ai_cleaner.py` rewritten to use the `openai` SDK's `AsyncOpenAI` client
  with **JSON output mode** (`response_format={"type": "json_object"}`)
- `deepseek-v4-flash` is a reasoning model — **no `max_tokens` cap is sent**, because
  capping truncates the chain-of-thought and yields empty/invalid content. Processing
  is remote, so output length is left uncapped.
- Added a `_chat_json()` helper that retries (up to 3×) on the rare empty/malformed
  response before falling back to manual entry
- `config.py`: `DEEPSEEK_API_KEY` is now required; `GEMINI_API_KEY` kept optional (unused)
- `docker-compose.yml`, `.env.template`: replaced `GEMINI_API_KEY` with `DEEPSEEK_API_KEY`
  (plus CasaOS field description)

### Removed
- `google-genai` dependency (replaced with `openai`)
- `debug_models.py` — Gemini-specific model-listing helper, no longer relevant

---

## [2026-06-15] - Fix empty Gemini responses (reasoning model)

### Fixed
- `gemma-4-26b-a4b-it` is a reasoning model — it spent the entire
  `max_output_tokens` budget on internal "thinking" and hit MAX_TOKENS before
  emitting the JSON answer, so `response.text` came back None and metadata /
  episode extraction crashed (`'NoneType' object has no attribute 'strip'`).
  Raised `max_output_tokens` 1024 → 8192 so thinking + answer both fit.
  (`thinking_budget=0` is rejected by this model.)
- Hardened `extract_metadata` / `extract_episode` with `_extract_text()` —
  returns None gracefully and logs `finish_reason` / `prompt_feedback` when the
  model produces no text, so the manual-entry fallback kicks in cleanly.

### Added
- Committed the `dorama/` package, which was referenced by `main.py` in the
  previous release but accidentally omitted from version control.
- `.dockerignore` now tracked — prevents `.env`, sessions, venv and logs from
  being baked into the Docker image.

---

## [2026-06-03] - Dorama Mode, Gemini Model Migration & Fixes

### Added
- **🎬 Dorama Mode** — automatic tracking and downloading of series episodes from streaming sites
  - `/dorama {url}` — add a series to track (accepts both a series page and a first-episode URL)
  - `/dorama list` — list tracked series with inline **⏹ Stop** buttons
  - `/dorama help` — full mode documentation
  - Background checker runs immediately on add, then every **6 hours**
  - Series auto-expire after **~6 months** (182 days) of tracking
  - Downloads **all available episodes & seasons**; skips already-downloaded ones (tracked in SQLite)
  - **Dub-only policy** — downloads only Ukrainian dub / multi-voice-over (zetvideo.net); subtitle-only tracks are ignored until a dub appears
  - On success, **all authorized users** are notified (not just the one who added the series)
  - Confirm / rename / cancel inline buttons when adding a series
- **Pluggable site-handler architecture** (`dorama/sites/`) — `BaseSiteHandler` interface + registry keyed by domain; add a new site by dropping in one file and registering it
  - `uafix.net` handler supports **two URL formats**: per-episode pages (`.../season-01-episode-01/`) and whole-serial players (`.../serials/slug/` with embedded episode JSON)
- **SQLite persistence** (`sessions/dorama.db`) — `series` + `episodes` tables
- **`yt-dlp` + `ffmpeg`** for HLS (m3u8) downloads; **`httpx`** for page fetching
- **`DORAMA_PATH`** env var + `/data/dorama` volume (separate folder for series)
- **`tgcrypto`** added to dependencies — fixes slow download speeds (was missing, Pyrogram fell back to pure-Python crypto)
- Explicit **`workers=120`** on the Pyrogram client — prevents handler-task starvation when many videos arrive at once on low-core hosts (e.g. Raspberry Pi)

### Changed
- **Gemini model migration** — Google removed `gemma-3-27b-it` from the API; switched to `gemma-4-26b-a4b-it` (unlimited TPM, 1.5K req/day free tier)
- Rate limiter raised from 10 → 14 req/min to match the new model's limits
- `debug_models.py` updated to the new `google-genai` SDK for listing available models
- `mappings.json` is now persistent — added `/app` bind mount so it survives container rebuilds

### Fixed
- **`MESSAGE_NOT_MODIFIED` crash** in mode switching — added "already in this mode" guard for Normal mode and wrapped `edit_text` calls in try/except

---

## [2026-03-19] - CasaOS Support, UX Improvements & Batch Mode

### Added
- **CasaOS compatibility** — `docker-compose.yml` now includes `x-casaos` labels with field descriptions; all settings are configurable via CasaOS UI without editing any files
- **`.dockerignore`** — prevents `.env`, sessions, venv, and logs from being baked into the Docker image
- **Queue-done notification in Batch mode** — single "✅ Всі завантаження завершено!" message with mode keyboard is sent once after the last download completes (replaces per-message buttons)

### Changed
- `docker-compose.yml`: replaced `env_file: .env` with an explicit `environment:` block (empty defaults) — CasaOS/Docker fill values at runtime; `.env` file remains the local dev fallback via pydantic-settings priority
- Normal mode: all user prompts (AI failure fallback, unknown title confirmation) now sent as new messages via `ask_user_fresh()` — consistent UX with Batch mode, questions always visible at the bottom of chat

### Fixed
- `.env` file no longer leaks into Docker image — `COPY . .` is now guarded by `.dockerignore`

---

## [2026-03-19] - Batch Mode, Help Command & Mode Switching

### Added
- **Batch Mode** — new operating mode for downloading groups of poorly-named episodes
  - Title and season are set once per session, then each video gets episode-only AI extraction
  - Completely isolated from `mappings.json` (no reads or writes) to prevent garbage accumulation
  - 30-minute inactivity timer resets on each new video; session also ends via button
  - Sequential per-chat processing via `asyncio.Lock`
- **`/help` command** — describes both modes with inline keyboard for quick switching
- **`/mode` command** — inline keyboard to switch between Normal and Batch mode
- **`⏹ End Session` button** — manually ends Batch session and returns to Normal mode
- **`extract_episode()` in `ai_cleaner.py`** — focused AI prompt that extracts only episode number given a known title and season

### Changed
- `video_handler` now branches on current mode before processing
- Initial status message shows filename instead of raw text-to-analyze
- Access control filter (`is_authorized`) now typed as generic `update` to support both `Message` and `CallbackQuery`

### Fixed
- Duplicate `if not ai_data` condition in `video_handler` (dead inner check removed)
- Stale blank lines in `video_handler` after condition cleanup

---

## [2025-12-18] - Race Condition Fix & Improvements

### Fixed
- **Race condition in Pyrogram** — replaced custom multi-threaded downloader with built-in `download_media()`
  - Eliminated: `RuntimeError: read() called while another coroutine is already waiting for incoming data`
  - Simplified `core/downloader.py` from 231 to 102 lines (−56%)

### Added
- **Rotating file log** — `app.log`, max 10 MB, 5 backups, mirrored to stdout
- **`.gitignore`** — excludes logs, sessions, `.env`, `.venv`, `.idea`, debug scripts

### Removed
- Unused `download_video` import in `main.py`
- Duplicate `GEMINI_API_KEY` field in `config.py`
- `download_chunk()` function and all multi-threaded downloader constants

---

## [2025-12-17] - Rate Limiting, Download Queue, AI SDK Migration

### Added
- Rate limiter for Google Gemini API (10 req/min, token-bucket algorithm)
- `QueueManager` — sequential download queue to avoid concurrent downloads
- Migrated from `google-generativeai` to `google-genai` SDK
