import logging
import os
import re
import time

from dorama import db as dorama_db
from dorama.sites.base import BaseSiteHandler
from dorama.userbot import get_userbot_client
from analyzer.ai_cleaner import extract_metadata
from core.downloader import progress_bar
from core.renamer import sanitize_title

logger = logging.getLogger(__name__)

# https://t.me/RH_MediaLib/20835 — link to a forum topic's anchor message.
URL_RE = re.compile(r'^https://t\.me/([A-Za-z0-9_]+)/(\d+)/?$')

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
    Tracks a Telegram forum-topic (e.g. a "media library" channel where each
    topic is a series and every reply in the topic is one episode video).

    Requires a userbot session (USERBOT_SESSION_STRING) — the Bot API has no
    way to read channel/topic history, only a regular user account can.
    """
    DOMAINS = ["t.me"]

    def is_valid_url(self, url: str) -> bool:
        return bool(URL_RE.match(url.strip()))

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

    async def get_series_title(self, url: str) -> str | None:
        chat, anchor_id = self._parse(url)
        async for msg in self._iter_video_replies(chat, anchor_id):
            caption = str(msg.caption or msg.text or "")
            data = await extract_metadata(caption)
            return data.get("title") if data else None
        return None

    async def list_episodes(self, url: str) -> list[dict]:
        chat, anchor_id = self._parse(url)
        episodes: list[dict] = []
        cache_hits = 0
        cache_misses = 0
        async for msg in self._iter_video_replies(chat, anchor_id):
            caption = str(msg.caption or msg.text or "")

            if _is_ignored_variant(caption):
                logger.info(f"Skipping ignored variant (matched marker): {caption[:60]!r}")
                continue

            # A message's caption never changes after posting — once resolved,
            # never re-run DeepSeek on it again. This is what previously made
            # every 6-hour check cycle burn one API call PER EPISODE PER
            # SERIES, forever, even for episodes downloaded months ago.
            cached = dorama_db.get_cached_caption(chat, msg.id)
            if cached:
                cache_hits += 1
                season, episode = cached
            else:
                cache_misses += 1
                data = await extract_metadata(caption)
                if not data or data.get("episode") is None:
                    logger.warning(f"Could not parse episode from caption: {caption[:60]!r}")
                    continue
                season = data.get("season", 1)
                episode = data["episode"]
                dorama_db.cache_caption(chat, msg.id, season, episode)

            episodes.append({
                "season": season,
                "episode": episode,
                "source": f"{chat}:{msg.id}",
                "is_finale": _is_finale(caption),
            })
        logger.info(
            f"list_episodes({chat}): {len(episodes)} messages, "
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

            chat, msg_id_str = source.split(":", 1)
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

            downloaded_path = await client.download_media(
                message, file_name=target, progress=progress
            )
            return bool(downloaded_path)
        except Exception as e:
            logger.error(f"Telegram download failed: {e}", exc_info=True)
            return False
