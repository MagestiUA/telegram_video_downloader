import asyncio
import logging

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from analyzer.ai_cleaner import extract_total_episodes
from anime_tracker import db
from anime_tracker.sites import get_handler
from config.config import settings

logger = logging.getLogger(__name__)

CHECK_INTERVAL_HOURS = 6

# Pause between titles when checking a batch (background cycle or manual
# "Перевірити все") — even fully sequential (non-parallel) back-to-back API
# calls from one account can add up fast enough to trip Telegram's flood
# control on sensitive methods (join_chat/ImportChatInvite,
# get_discussion_replies/GetReplies) once there are more than a couple of
# tracked titles. A few seconds of breathing room between titles is cheap
# against a 6-hour check interval.
INTER_SERIES_DELAY_SECONDS = 4

# Pause between individual episode downloads within the same series.
# Each download() opens its own per-DC media session (see anime_tracker/
# sites/telegram.py) — reopening these back-to-back with zero delay is the
# suspected trigger for the periodic "Auth key not found" 401 hiccup on the
# media session. A short breather between downloads is cheap insurance.
INTER_DOWNLOAD_DELAY_SECONDS = 5

# Once at least this many episodes are downloaded (and total_episodes is
# still unresolved, i.e. 0), ask DeepSeek for the season's total episode
# count — most source channels only tag the actual finale as "N з N";
# earlier episodes are posted as "N з XX" (unknown total), so that caption
# pattern alone leaves most titles never auto-stopping and requiring a
# manual stop.
TOTAL_EPISODES_PROBE_THRESHOLD = 10

# If DeepSeek isn't confident enough to answer (returns null), fall back to
# this as a reasonable default season length rather than leaving
# total_episodes at 0 forever (which would mean re-asking DeepSeek — and
# getting the same null — on every single check cycle indefinitely).
DEFAULT_TOTAL_EPISODES_FALLBACK = 12


def _renew_tracking_keyboard(series_id: int) -> InlineKeyboardMarkup:
    """
    Attached to an auto-stop notification (finale detected via caption OR
    resolved total_episodes) — lets the user undo a wrong auto-stop without
    re-adding the title from scratch (which would lose episode/display_title
    history). main.py's anime_renewask_/anime_renewyes_/anime_renewcancel_
    callbacks handle the actual confirm-then-reactivate flow.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Поновити відстеження", callback_data=f"anime_renewask_{series_id}")
    ]])


async def _resolve_total_episodes_and_maybe_stop(
    series: db.sqlite3.Row, client, handler, url: str, title: str, display: str
) -> bool:
    """
    Run BEFORE contacting Telegram at all, on every check cycle:
    1. Once >= TOTAL_EPISODES_PROBE_THRESHOLD episodes of the CURRENT
       season are already downloaded and total_episodes is still 0
       (unresolved), ask DeepSeek for the season's real length. If it
       doesn't know AND we haven't already downloaded more than the
       fallback would allow, use DEFAULT_TOTAL_EPISODES_FALLBACK so we
       don't re-ask every cycle. If we've already downloaded MORE than the
       fallback (a backlog title crossing the threshold for the first time
       right when this feature shipped, still clearly ongoing), do NOT
       assign a number — that would retroactively declare it "finished" at
       a count we've already exceeded. Leave total_episodes at 0 and keep
       relying on the "N з N" caption pattern for such titles; the probe
       retries next cycle in case DeepSeek can identify it later.
    2. If total_episodes is known (> 0) and we've already downloaded that
       many episodes of the current season, the season is done — auto-stop
       tracking right here, without ever hitting Telegram this cycle.

    One series row spans every season a title airs (last_season/
    last_episode just move forward) — episodes are counted for
    series["last_season"] ONLY, never across all seasons combined, and
    record_episode() resets total_episodes to 0 the moment a new season's
    first episode is recorded, so a resolved count never leaks into the
    next season.

    Returns True if tracking was just auto-stopped (caller should skip the
    rest of this cycle's Telegram check for this series).
    """
    series_id = series["id"]
    chat_id = series["chat_id"]
    current_season = series["last_season"]
    downloaded_count = sum(
        1 for (season, _episode) in db.get_downloaded_set(series_id) if season == current_season
    )
    total_episodes = series["total_episodes"] or 0

    if total_episodes == 0 and downloaded_count >= TOTAL_EPISODES_PROBE_THRESHOLD:
        total = await extract_total_episodes(title)
        if total:
            total_episodes = total
            db.set_total_episodes(series_id, total_episodes)
            logger.info(
                f"[{title}] сезон {current_season}: DeepSeek визначив кількість серій = {total_episodes}."
            )
        elif downloaded_count < DEFAULT_TOTAL_EPISODES_FALLBACK:
            total_episodes = DEFAULT_TOTAL_EPISODES_FALLBACK
            db.set_total_episodes(series_id, total_episodes)
            logger.info(
                f"[{title}] сезон {current_season}: DeepSeek не впевнений, fallback = {total_episodes}."
            )
        else:
            logger.info(
                f"[{title}] сезон {current_season}: DeepSeek не впевнений, а вже скачано "
                f"{downloaded_count} (> fallback {DEFAULT_TOTAL_EPISODES_FALLBACK}) — "
                f"не встановлюю total_episodes, покладаюсь на 'N з N' у підписі."
            )

    if total_episodes > 0 and downloaded_count >= total_episodes:
        logger.info(
            f"[{title}] сезон {current_season}: завантажено {downloaded_count}/{total_episodes} — "
            f"відстеження зупинено (без звернення до ТГ цього циклу)."
        )
        db.stop_series(series_id)
        try:
            await handler.cleanup(url)
        except Exception as e:
            logger.warning(f"[{title}] cleanup() after auto-stop failed: {e}")

        done_text = (
            f"🏁 **{display}** (сезон {current_season}): завантажено всі "
            f"{downloaded_count}/{total_episodes} серій — знято з відстеження."
        )
        reply_markup = _renew_tracking_keyboard(series_id)
        all_users = settings.allowed_users_set or {chat_id}
        for uid in all_users:
            try:
                await client.send_message(uid, done_text, reply_markup=reply_markup)
            except Exception as e:
                logger.warning(f"Failed to notify {uid}: {e}")
        return True

    return False


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
    title     = series["title"]  # canonical Romaji — used for folder/file naming
    display   = db.resolve_display_title(series)  # localized name shown to users (backfills legacy rows)
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
        await _finalize_status(f"❌ **{display}**: джерело не підтримується.")
        return False

    # Season-length check: resolve total_episodes (if not yet known and
    # we've downloaded enough to ask), and auto-stop right here if the
    # known total is already fully downloaded — BEFORE spending any
    # Telegram API calls this cycle.
    stopped = await _resolve_total_episodes_and_maybe_stop(series, client, handler, url, title, display)
    if stopped:
        await _finalize_status(f"🏁 **{display}**: усі серії вже завантажені — знято з відстеження.")
        return False

    # Fetch all currently available DUB episodes
    available = await handler.list_episodes(url)
    if not available:
        logger.info(f"[{title}] немає доступних дубльованих епізодів.")
        await _finalize_status(f"⚠️ **{display}**: серій ще не знайдено.")
        return False

    done = db.get_downloaded_set(series_id)
    new_eps = sorted(
        (e for e in available if (e["season"], e["episode"]) not in done),
        key=lambda e: (e["season"], e["episode"])
    )

    if not new_eps:
        logger.info(f"[{title}] нових епізодів немає ({len(available)} вже завантажено).")
        await _finalize_status(
            f"✅ **{display}**: усі доступні серії вже завантажені ({len(available)})."
        )
        return False

    logger.info(f"[{title}] знайдено {len(new_eps)} нових епізодів.")
    await _finalize_status(
        f"✅ **{display}**: знайдено {len(new_eps)} нових серій — починаю завантаження..."
    )
    downloaded_any = False

    for i, ep in enumerate(new_eps):
        season, episode, source = ep["season"], ep["episode"], ep["source"]

        if i > 0:
            await asyncio.sleep(INTER_DOWNLOAD_DELAY_SECONDS)

        notify_msg = None
        try:
            notify_msg = await client.send_message(
                chat_id,
                f"🎬 **{display}** S{season:02d}E{episode:02d}\n⏳ Починаю завантаження..."
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

            # Caption-based finale detection ("N з N") — independent of, and
            # complementary to, the total_episodes-based auto-stop, which
            # runs as a pre-check at the top of the NEXT cycle instead of
            # here (see _resolve_total_episodes_and_maybe_stop).
            is_finale = ep.get("is_finale", False)

            done_text = (
                f"✅ Завантажено: **{display}** S{season:02d}E{episode:02d}"
                + ("\n🏁 Це остання серія — знято з відстеження." if is_finale else "")
            )
            reply_markup = _renew_tracking_keyboard(series_id) if is_finale else None

            all_users = settings.allowed_users_set or {chat_id}
            for uid in all_users:
                try:
                    if uid == chat_id and notify_msg:
                        await notify_msg.edit_text(done_text, reply_markup=reply_markup)
                    else:
                        await client.send_message(uid, done_text, reply_markup=reply_markup)
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
                        f"❌ Помилка завантаження: **{display}** S{season:02d}E{episode:02d}"
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
                # Strictly sequential, WITH a pause between titles: this all
                # runs through ONE userbot account, and Telegram rate-limits
                # per-account regardless of how many titles we're checking —
                # firing every series' API calls back-to-back (even
                # non-concurrently) was still enough to trip FLOOD_WAIT once
                # there were more than a couple of tracked titles.
                for i, s in enumerate(active):
                    try:
                        await process_series(s, client)
                    except Exception as e:
                        logger.error(f"Error processing '{s['title']}': {e}")
                    if i < len(active) - 1:
                        await asyncio.sleep(INTER_SERIES_DELAY_SECONDS)

        except Exception as e:
            logger.error(f"Checker cycle error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)
