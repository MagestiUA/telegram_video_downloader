import glob
import logging
import os

from anime_tracker import db
from anime_tracker.sites import get_handler
from config.config import settings
from core.renamer import sanitize_title

logger = logging.getLogger(__name__)


def _dest_path(series: db.sqlite3.Row) -> str:
    return settings.DOWNLOAD_PATH if series["category"] == "anime" else settings.DORAMA_PATH


def delete_episode(series: db.sqlite3.Row, season: int, episode: int) -> bool:
    """
    Remove a downloaded episode's file(s) from disk AND its DB record, so the
    next check cycle treats it as not-yet-downloaded again. Used to fix a
    wrongly-downloaded episode (e.g. a subtitles-only release that later got
    replaced with the dub in the source channel/topic) — delete the bad
    file+record, then either let the next automatic check re-fetch it, or
    call redownload_episode() directly for an immediate retry.

    Best-effort on the file: a missing file is not an error (the DB record
    is always removed if present). Returns True only if a file was actually
    found and deleted, so the caller can tell the two cases apart.
    """
    safe = sanitize_title(series["title"])
    pattern = os.path.join(_dest_path(series), safe, f"{safe} - S{season:02d}E{episode:02d}.*")
    removed_any = False
    for path in glob.glob(pattern):
        try:
            os.remove(path)
            removed_any = True
            logger.info(f"Deleted file: {path}")
        except Exception as e:
            logger.warning(f"Could not delete {path}: {e}")
    db.delete_episode(series["id"], season, episode)
    return removed_any


async def redownload_episode(series: db.sqlite3.Row, season: int, episode: int) -> bool:
    """
    Re-resolve the CURRENT source message for (season, episode) in the
    tracked channel/topic and download it again, overwriting whatever's on
    disk. Does NOT touch the DB episode record — the season/episode number
    is still correct, only the file content is being replaced — so this
    works standalone to fix a wrong-variant download without needing
    delete_episode() first.

    Returns False if no matching episode is currently found in the source
    (e.g. it was deleted rather than replaced with a corrected upload).
    """
    handler = get_handler(series["base_url"])
    if not handler:
        return False
    available = await handler.list_episodes(series["base_url"])
    candidates = [e for e in available if e["season"] == season and e["episode"] == episode]
    if not candidates:
        logger.warning(
            f"redownload_episode: no current source for S{season:02d}E{episode:02d} "
            f"of '{series['title']}'."
        )
        return False
    # If more than one message currently resolves to this (season, episode) —
    # e.g. the old wrong upload wasn't actually deleted, just superseded —
    # prefer the last one listed (channels are iterated oldest-first, so this
    # is the most recently posted candidate).
    source = candidates[-1]["source"]
    return await handler.download(source, series["title"], season, episode, _dest_path(series))
