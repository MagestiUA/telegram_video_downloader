import asyncio
import logging

from dorama import db
from dorama.sites import get_handler
from config.config import settings

logger = logging.getLogger(__name__)

CHECK_INTERVAL_HOURS = 6


async def process_series(series: db.sqlite3.Row, client) -> bool:
    """
    Check and download all new (not yet downloaded) episodes for one series.
    Returns True if at least one episode was downloaded.
    """
    series_id = series["id"]
    chat_id   = series["chat_id"]
    title     = series["title"]
    url       = series["base_url"]

    handler = get_handler(url)
    if not handler:
        logger.error(f"No handler for url: {url}")
        return False

    # Fetch all currently available DUB episodes
    available = await handler.list_episodes(url)
    if not available:
        logger.info(f"[{title}] немає доступних дубльованих епізодів.")
        return False

    done = db.get_downloaded_set(series_id)
    new_eps = sorted(
        (e for e in available if (e["season"], e["episode"]) not in done),
        key=lambda e: (e["season"], e["episode"])
    )

    if not new_eps:
        logger.info(f"[{title}] нових епізодів немає ({len(available)} вже завантажено).")
        return False

    logger.info(f"[{title}] знайдено {len(new_eps)} нових епізодів.")
    downloaded_any = False

    for ep in new_eps:
        season, episode, source = ep["season"], ep["episode"], ep["source"]

        notify_msg = None
        try:
            notify_msg = await client.send_message(
                chat_id,
                f"🎬 **{title}** S{season:02d}E{episode:02d}\n⏳ Починаю завантаження..."
            )
        except Exception as e:
            logger.warning(f"Notify failed: {e}")

        ok = await handler.download(
            source, title, season, episode,
            settings.DORAMA_PATH, notify_msg=notify_msg
        )

        if ok:
            db.record_episode(series_id, season, episode)
            downloaded_any = True

            all_users = settings.allowed_users_set or {chat_id}
            for uid in all_users:
                try:
                    if uid == chat_id and notify_msg:
                        await notify_msg.edit_text(
                            f"✅ Завантажено: **{title}** S{season:02d}E{episode:02d}"
                        )
                    else:
                        await client.send_message(
                            uid,
                            f"✅ Завантажено: **{title}** S{season:02d}E{episode:02d}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to notify {uid}: {e}")
        else:
            try:
                if notify_msg:
                    await notify_msg.edit_text(
                        f"❌ Помилка завантаження: **{title}** S{season:02d}E{episode:02d}"
                    )
            except Exception:
                pass
            # Stop on failure — retry this & remaining episodes next cycle
            break

    return downloaded_any


async def run_checker(client):
    """
    Background coroutine. Runs immediately on startup, then every CHECK_INTERVAL_HOURS.
    Checks all active series for new episodes.
    """
    logger.info("🔁 Dorama checker started.")

    while True:
        logger.info("⏰ Dorama check cycle running...")
        try:
            db.deactivate_expired()
            active = db.get_active_series()

            if not active:
                logger.info("No active dorama series to check.")
            else:
                logger.info(f"Checking {len(active)} active series...")
                results = await asyncio.gather(
                    *[process_series(s, client) for s in active],
                    return_exceptions=True
                )
                for s, r in zip(active, results):
                    if isinstance(r, Exception):
                        logger.error(f"Error processing '{s['title']}': {r}")

        except Exception as e:
            logger.error(f"Checker cycle error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)
