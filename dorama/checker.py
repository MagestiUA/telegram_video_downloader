import asyncio
import logging

from dorama import db
from dorama.sites import get_handler
from config.config import settings

logger = logging.getLogger(__name__)

CHECK_INTERVAL_HOURS = 6


async def process_series(series: db.sqlite3.Row, client, initial_status_msg=None) -> bool:
    """
    Check and download all new (not yet downloaded) episodes for one series.
    Returns True if at least one episode was downloaded.

    `initial_status_msg` — optional Message to finalize with the check result
    (used only for the immediate check triggered right after adding a title,
    so the "⏳ Перевіряю доступні серії..." status doesn't hang forever if
    there turn out to be no new episodes).
    """
    series_id = series["id"]
    chat_id   = series["chat_id"]
    title     = series["title"]
    url       = series["base_url"]
    category  = series["category"]
    dest_path = settings.DOWNLOAD_PATH if category == "anime" else settings.DORAMA_PATH

    async def _finalize_status(text: str):
        if initial_status_msg:
            try:
                await initial_status_msg.edit_text(text)
            except Exception:
                pass

    handler = get_handler(url)
    if not handler:
        logger.error(f"No handler for url: {url}")
        await _finalize_status(f"❌ **{title}**: джерело не підтримується.")
        return False

    # Fetch all currently available DUB episodes
    available = await handler.list_episodes(url)
    if not available:
        logger.info(f"[{title}] немає доступних дубльованих епізодів.")
        await _finalize_status(f"⚠️ **{title}**: серій ще не знайдено.")
        return False

    done = db.get_downloaded_set(series_id)
    new_eps = sorted(
        (e for e in available if (e["season"], e["episode"]) not in done),
        key=lambda e: (e["season"], e["episode"])
    )

    if not new_eps:
        logger.info(f"[{title}] нових епізодів немає ({len(available)} вже завантажено).")
        await _finalize_status(
            f"✅ **{title}**: усі доступні серії вже завантажені ({len(available)})."
        )
        return False

    logger.info(f"[{title}] знайдено {len(new_eps)} нових епізодів.")
    await _finalize_status(
        f"✅ **{title}**: знайдено {len(new_eps)} нових серій — починаю завантаження..."
    )
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

        try:
            ok = await handler.download(
                source, title, season, episode,
                dest_path, notify_msg=notify_msg
            )
        except Exception as e:
            # handler.download() is expected to return False on failure, never
            # raise — but guard against it anyway so a bug in a handler can't
            # silently kill this task (asyncio.create_task is fire-and-forget
            # on the immediate-add path in main.py).
            logger.error(f"[{title}] download() raised unexpectedly: {e}", exc_info=True)
            ok = False

        if ok:
            db.record_episode(series_id, season, episode)
            downloaded_any = True
            is_finale = ep.get("is_finale", False)

            done_text = (
                f"✅ Завантажено: **{title}** S{season:02d}E{episode:02d}"
                + ("\n🏁 Це остання серія — знято з відстеження." if is_finale else "")
            )

            all_users = settings.allowed_users_set or {chat_id}
            for uid in all_users:
                try:
                    if uid == chat_id and notify_msg:
                        await notify_msg.edit_text(done_text)
                    else:
                        await client.send_message(uid, done_text)
                except Exception as e:
                    logger.warning(f"Failed to notify {uid}: {e}")

            if is_finale:
                db.stop_series(series_id)
                try:
                    await handler.cleanup(url)
                except Exception as e:
                    logger.warning(f"[{title}] cleanup() after finale failed: {e}")
                logger.info(f"[{title}] фінальна серія завантажена — відстеження зупинено.")
                break
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
    logger.info("🔁 Anime checker started.")

    while True:
        logger.info("⏰ Anime check cycle running...")
        try:
            db.deactivate_expired()
            active = db.get_active_series()

            if not active:
                logger.info("No active anime titles to check.")
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
