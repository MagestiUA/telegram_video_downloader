# Changelog

All notable changes are documented here.

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
