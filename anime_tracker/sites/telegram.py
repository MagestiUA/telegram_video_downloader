import asyncio
import logging
import os
import re
import time

from anime_tracker import db as anime_db
from anime_tracker.sites.base import BaseSiteHandler
from anime_tracker.userbot import get_userbot_client
from anime_tracker.folder import join_and_file, unfile_and_leave
from analyzer.ai_cleaner import extract_metadata
from core.downloader import progress_bar
from core.renamer import sanitize_title

logger = logging.getLogger(__name__)

# https://t.me/RH_MediaLib/20835 — link to a forum topic's anchor message.
# One shared "media library" channel, many topics = many titles.
URL_RE = re.compile(r'^https://t\.me/([A-Za-z0-9_]+)/(\d+)/?$')

# https://t.me/+56k-vXXomGg0NjAy — invite link to a PRIVATE channel dedicated
# to a single title (e.g. Glass Moon's "ONLINE (озвучення)" hyperlink). The
# whole channel IS the title — no topic/anchor structure needed.
INVITE_RE = re.compile(r'^https://t\.me/\+[\w-]+/?$', re.IGNORECASE)

# Detects "N з N" / "N із N" / "N of N" (current == total) in a caption, e.g.
# "[12 з 12]" -> series finale. Requires the total to be actual digits, so
# placeholders like "04 з XX" / "04 з Х" never match (\d+ won't match letters).
FINALE_RE = re.compile(r'(\d+)\s*(?:з|із|из|of)\s*(\d+)\b', re.IGNORECASE)

# Caption markers that mean "skip this video entirely" — some channels post
# the SAME episode twice under different labels (e.g. Glass Moon posts both a
# full "- DUB" version and a smaller "- MINI" duplicate of the same episode).
# Add more markers here as new channels/cases turn up; case-insensitive
# substring match against the whole caption/message text.
IGNORED_CAPTION_MARKERS = [
    "MINI",  # Glass Moon: smaller/duplicate re-encode of the same episode
]

# download_media() occasionally hits a transient "Auth key not found in the
# system" (401 Unauthorized) on the per-DC media session Pyrogram opens for
# each download — observed to be a short-lived hiccup (the very next check
# cycle downloads fine with the SAME session/SESSION_STRING), not an actual
# session revocation. Retry a couple of times with a pause before giving up
# on the episode instead of failing the whole batch on one flaky connection.
DOWNLOAD_RETRY_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 8


def _is_ignored_variant(caption: str) -> bool:
    upper = caption.upper()
    return any(marker.upper() in upper for marker in IGNORED_CAPTION_MARKERS)


def _is_finale(caption: str) -> bool:
    m = FINALE_RE.search(caption)
    if not m:
        return False
    current, total = int(m.group(1)), int(m.group(2))
    return current == total > 0


class TelegramHandler(BaseSiteHandler):
    """
    Tracks anime episodes posted to Telegram, in either of two shapes:

    1. Forum-topic anchor link (t.me/{channel}/{msg_id}) — a shared "media
       library" channel where each forum topic is one title and every reply
       in the topic is one episode video (e.g. RH_MediaLib).

    2. Private-channel invite link (t.me/+{hash}) — a channel DEDICATED to a
       single title; the whole channel's history IS that title's episodes,
       no topic/anchor needed (e.g. Glass Moon's per-title "ONLINE
       (озвучення)" channels). Auto-joined on first use, filed into the
       user's "Аніме Тайтли" Telegram folder, and left when tracking ends.

    Requires a userbot session (USERBOT_SESSION_STRING) — the Bot API has no
    way to read channel/topic history, only a regular user account can.
    """
    DOMAINS = ["t.me"]

    def is_valid_url(self, url: str) -> bool:
        url = url.strip()
        return bool(URL_RE.match(url)) or bool(INVITE_RE.match(url))

    def _parse(self, url: str) -> tuple[str, int]:
        m = URL_RE.match(url.strip())
        if not m:
            raise ValueError(f"Cannot parse Telegram URL: {url}")
        return m.group(1), int(m.group(2))

    async def _iter_video_replies(self, chat: str, anchor_id: int):
        client = get_userbot_client()
        if not client:
            logger.error("Userbot client not configured (USERBOT_SESSION_STRING missing).")
            return
        async for msg in client.get_discussion_replies(chat, anchor_id):
            if msg.id == anchor_id:
                continue
            if msg.video or msg.document:
                yield msg

    async def _ensure_joined(self, invite_url: str) -> int | None:
        """Join the private per-title channel (filing it into the anime folder), returning its chat_id."""
        client = get_userbot_client()
        if not client:
            logger.error("Userbot client not configured (USERBOT_SESSION_STRING missing).")
            return None
        return await join_and_file(client, invite_url)

    async def _resolve_episode_from_message(self, chat_key: str, msg) -> tuple[dict | None, bool]:
        """
        Shared per-message resolution used by both the forum-topic and
        private-channel listing paths. Returns (episode_dict_or_None, was_cache_hit).
        """
        caption = str(msg.caption or msg.text or "")

        if _is_ignored_variant(caption):
            logger.info(f"Skipping ignored variant (matched marker): {caption[:60]!r}")
            return None, False

        # A message's caption never changes after posting — once resolved,
        # never re-run DeepSeek on it again. This is what previously made
        # every 6-hour check cycle burn one API call PER EPISODE PER SERIES,
        # forever, even for episodes downloaded months ago.
        cached = anime_db.get_cached_caption(chat_key, msg.id)
        if cached:
            season, episode = cached
            was_cached = True
        else:
            data = await extract_metadata(caption)
            if not data or data.get("episode") is None:
                logger.warning(f"Could not parse episode from caption: {caption[:60]!r}")
                return None, False
            season = data.get("season", 1)
            episode = data["episode"]
            anime_db.cache_caption(chat_key, msg.id, season, episode)
            was_cached = False

        episode_dict = {
            "season": season,
            "episode": episode,
            "source": f"{chat_key}:{msg.id}",
            "is_finale": _is_finale(caption),
        }
        return episode_dict, was_cached

    # ------------------------------------------------------------------ interface

    async def get_series_title(self, url: str) -> str | None:
        url = url.strip()
        if INVITE_RE.match(url):
            return await self._get_series_title_private(url)

        chat, anchor_id = self._parse(url)
        async for msg in self._iter_video_replies(chat, anchor_id):
            caption = str(msg.caption or msg.text or "")
            data = await extract_metadata(caption)
            return data.get("title") if data else None
        return None

    async def _get_series_title_private(self, invite_url: str) -> str | None:
        client = get_userbot_client()
        if not client:
            return None
        chat_id = await self._ensure_joined(invite_url)
        if not chat_id:
            return None
        async for msg in client.get_chat_history(chat_id):
            if not (msg.video or msg.document):
                continue
            caption = str(msg.caption or msg.text or "")
            if _is_ignored_variant(caption):
                continue
            data = await extract_metadata(caption)
            if data and data.get("title"):
                return data["title"]
        return None

    async def list_episodes(self, url: str) -> list[dict]:
        url = url.strip()
        if INVITE_RE.match(url):
            return await self._list_episodes_private(url)

        chat, anchor_id = self._parse(url)
        episodes: list[dict] = []
        cache_hits = cache_misses = 0
        async for msg in self._iter_video_replies(chat, anchor_id):
            ep, was_cached = await self._resolve_episode_from_message(chat, msg)
            if ep:
                episodes.append(ep)
                cache_hits += was_cached
                cache_misses += not was_cached
        logger.info(
            f"list_episodes({chat}): {len(episodes)} episodes, "
            f"{cache_hits} from cache, {cache_misses} newly resolved via DeepSeek."
        )
        return episodes

    async def _list_episodes_private(self, invite_url: str) -> list[dict]:
        client = get_userbot_client()
        if not client:
            logger.error("Userbot client not configured (USERBOT_SESSION_STRING missing).")
            return []
        chat_id = await self._ensure_joined(invite_url)
        if not chat_id:
            return []

        chat_key = str(chat_id)
        episodes: list[dict] = []
        cache_hits = cache_misses = 0
        async for msg in client.get_chat_history(chat_id):
            if not (msg.video or msg.document):
                continue
            ep, was_cached = await self._resolve_episode_from_message(chat_key, msg)
            if ep:
                episodes.append(ep)
                cache_hits += was_cached
                cache_misses += not was_cached
        logger.info(
            f"list_episodes(private {chat_id}): {len(episodes)} episodes, "
            f"{cache_hits} from cache, {cache_misses} newly resolved via DeepSeek."
        )
        return episodes

    async def download(self, source: str, title: str, season: int, episode: int,
                       path: str, notify_msg=None) -> bool:
        """
        Never raises — always returns bool, logging the reason on failure.
        This is a hard contract: the caller may run this from a fire-and-forget
        asyncio.create_task with no exception handler attached.
        """
        try:
            client = get_userbot_client()
            if not client:
                logger.error("Userbot client not available for download.")
                return False

            chat_str, msg_id_str = source.split(":", 1)
            # A private channel's chat_id is numeric with no username — must be
            # passed as an actual int, since resolve_peer() treats a numeric
            # STRING as a phone-number lookup, not a chat_id.
            try:
                chat = int(chat_str)
            except ValueError:
                chat = chat_str  # public username (forum-topic case)

            message = await client.get_messages(chat, int(msg_id_str))
            media = message.video or message.document
            if not media:
                logger.error(f"No media on message {source}")
                return False

            safe = sanitize_title(title)
            out_dir = os.path.join(path, safe)
            os.makedirs(out_dir, exist_ok=True)

            _, ext = os.path.splitext(media.file_name or "")
            if not ext:
                ext = ".mp4"
            target = os.path.join(out_dir, f"{safe} - S{season:02d}E{episode:02d}{ext}")

            start_time = time.time()

            async def progress(current, total):
                await progress_bar(current, total, notify_msg, start_time)

            downloaded_path = None
            last_error = None
            for attempt in range(1, DOWNLOAD_RETRY_ATTEMPTS + 1):
                try:
                    downloaded_path = await client.download_media(
                        message, file_name=target, progress=progress
                    )
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"download_media attempt {attempt}/{DOWNLOAD_RETRY_ATTEMPTS} "
                        f"failed for {source}: {type(e).__name__}: {e}"
                    )
                    if attempt < DOWNLOAD_RETRY_ATTEMPTS:
                        await asyncio.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)

            if downloaded_path is None:
                if last_error:
                    raise last_error
                return False
            return bool(downloaded_path)
        except Exception as e:
            logger.error(f"Telegram download failed: {e}", exc_info=True)
            return False

    async def cleanup(self, url: str) -> None:
        """Leave a dedicated per-title channel once tracking ends. No-op for
        forum-topic URLs — that's a shared media-library channel other
        tracked titles may still need."""
        url = url.strip()
        if not INVITE_RE.match(url):
            return
        client = get_userbot_client()
        if not client:
            return
        try:
            chat = await client.get_chat(url)
            await unfile_and_leave(client, chat.id)
        except Exception as e:
            logger.warning(f"cleanup({url}) failed: {e}")
