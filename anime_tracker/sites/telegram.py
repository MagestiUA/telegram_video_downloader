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


# Documents that are clearly not an episode (subtitle files, archives, text,
# pictures) — a channel can attach those to the same caption format as the
# video, and they must never be listed as a "new episode" or downloaded over
# the real file.
_NON_VIDEO_EXTS = (".ass", ".srt", ".ssa", ".vtt", ".sub", ".zip", ".rar", ".7z", ".txt", ".nfo", ".jpg", ".png")


def _is_episode_media(msg) -> bool:
    if msg.video:
        return True
    doc = msg.document
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if mime.startswith(("text/", "image/", "audio/")) or name.endswith(_NON_VIDEO_EXTS):
        return False
    return True


def _fmt_dur(seconds) -> str:
    """m:ss for a duration. Pyrogram hands back a float for some videos, and a
    log line must never be able to take the listing down with it."""
    total = int(seconds or 0)
    return f"{total // 60}:{total % 60:02d}" if total else "?"


def _dedupe_episodes(episodes: list[dict], where: str) -> list[dict]:
    """
    Keep ONE message per (season, episode). A channel can hold the same episode
    in several messages (a re-upload, a second dub, a fixed encode — or a short
    bonus clip that happens to carry the same number). Every one of them
    resolves to the same target file name, so before this each extra copy was
    downloaded on top of the previous one: wasted traffic, a file whose
    content depended on download order, and duplicate DB rows.
    The LONGER video wins (a 2-5 minute special must never replace the real
    ~23 minute episode); on equal length the newest message does — a later
    post is normally the corrected/replacement upload. Every dropped copy is
    logged with caption, length and size, so a deliberate "two dubs" case is
    visible instead of silent.
    """
    def rank(e: dict) -> tuple[int, int]:
        return (e.get("duration", 0), e.get("message_id", 0))

    best: dict[tuple[int, int], dict] = {}
    dropped: list[tuple[dict, dict]] = []
    for ep in episodes:
        key = (ep["season"], ep["episode"])
        current = best.get(key)
        if current is None:
            best[key] = ep
        elif rank(ep) > rank(current):
            dropped.append((current, ep))
            best[key] = ep
        else:
            dropped.append((ep, current))
    for old, kept in dropped:
        logger.warning(
            f"[{where}] duplicate S{kept['season']:02d}E{kept['episode']:02d}: "
            f"keeping msg {kept.get('message_id')} {kept.get('caption')!r} "
            f"({_fmt_dur(kept.get('duration', 0))}, {kept.get('size', 0) // 1048576} MB); "
            f"skipping msg {old.get('message_id')} {old.get('caption')!r} "
            f"({_fmt_dur(old.get('duration', 0))}, {old.get('size', 0) // 1048576} MB)"
        )
    return [ep for ep in episodes if best[(ep["season"], ep["episode"])] is ep]


# A video shorter than this fraction of the channel's MEDIAN video length is
# treated as a bonus clip, not an episode. Channels name their specials however
# they like ("Монолог Маомао", "Спешл", just a number), so the title can't be
# trusted — the length can: a 2-5 minute clip next to ~23 minute episodes.
SHORT_CLIP_RATIO = 0.4

# The median of one or two videos says nothing about what a "normal" episode
# is here, so with fewer videos than this nothing is filtered.
MIN_VIDEOS_FOR_MEDIAN = 4


def _drop_short_specials(episodes: list[dict], where: str) -> list[dict]:
    """
    Drop clips much shorter than the source's typical episode. Specials are
    never wanted automatically, and — unlike duplicates of a real episode's
    number — they can carry a number of their own, so _dedupe_episodes
    alone wouldn't catch them. Median-relative on purpose: a channel of
    genuinely short-form episodes has a short median, so none of them are
    dropped. A document has no known duration (0) and is never dropped here.
    Every dropped clip is logged with its length.
    """
    durations = sorted(e["duration"] for e in episodes if e.get("duration"))
    if len(durations) < MIN_VIDEOS_FOR_MEDIAN:
        return episodes
    mid = len(durations) // 2
    median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
    threshold = median * SHORT_CLIP_RATIO

    kept = []
    for e in episodes:
        d = e.get("duration", 0)
        if d and d < threshold:
            logger.warning(
                f"[{where}] skipping short clip (likely a special): msg {e.get('message_id')} "
                f"{e.get('caption')!r} lasts {_fmt_dur(d)}, under {int(SHORT_CLIP_RATIO * 100)}% "
                f"of the typical {_fmt_dur(int(median))}"
            )
            continue
        kept.append(e)
    return kept


def _clean_listing(episodes: list[dict], where: str) -> list[dict]:
    """Specials out first, THEN one message per (season, episode)."""
    return _dedupe_episodes(_drop_short_specials(episodes, where), where)


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
            if _is_episode_media(msg):
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
            # Diagnostics only (nobody downstream depends on them) — what
            # _dedupe_episodes logs when two messages claim the same episode.
            "message_id": msg.id,
            "caption": caption[:80],
            "duration": int(getattr(msg.video, "duration", 0) or 0),
            "size": int(getattr(msg.video or msg.document, "file_size", 0) or 0),
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
            if not _is_episode_media(msg):
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
        return _clean_listing(episodes, f"list_episodes({chat})")

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
            if not _is_episode_media(msg):
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
        return _clean_listing(episodes, f"list_episodes(private {chat_id})")

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
