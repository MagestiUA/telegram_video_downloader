import asyncio
import logging

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from analyzer.ai_cleaner import extract_total_episodes
from analyzer.anilist import get_episode_count as anilist_episode_count
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
# still unresolved, i.e. 0), look up the season's total episode count —
# most source channels only tag the actual finale as "N з N"; earlier
# episodes are posted as "N з XX" (unknown total), so that caption pattern
# alone leaves most titles never auto-stopping and requiring a manual stop.
TOTAL_EPISODES_PROBE_THRESHOLD = 10

# If neither AniList nor DeepSeek can answer, fall back to this as a
# reasonable default season length rather than leaving total_episodes at 0
# forever (which would mean re-asking on every single check cycle
# indefinitely). Only applied when downloaded_count hasn't already
# exceeded it — see _ensure_total_episodes.
DEFAULT_TOTAL_EPISODES_FALLBACK = 12


async def _lookup_total_episodes(title: str, season: int = 1) -> tuple[int | None, str]:
    """
    Two-stage lookup for a SEASON's total episode count, tried in order:
    1. AniList — a live, community-maintained anime database (free, no API
       key). Far more reliable than an LLM's memory, and often has the
       number even before a season finishes airing. Season N is reached
       through AniList's SEQUEL chain.
    2. DeepSeek's own training knowledge, as a fallback for titles AniList
       doesn't have (rare, but happens for very new or obscure releases).
    Returns (episode_count_or_None, source_name) for logging.
    """
    total = await anilist_episode_count(title, season)
    if total:
        return total, "AniList"
    total = await extract_total_episodes(title, season)
    if total:
        return total, "DeepSeek"
    return None, "жодне джерело"


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


async def release_source(handler, url: str, series_id: int, title: str = ""):
    """
    Leave the source (a dedicated private channel) once a series is done —
    UNLESS another active row still tracks the same URL. A multi-season
    channel has one row per chosen season, all sharing one URL: finishing
    season 1 must not make the userbot walk out of a channel season 2 and 3
    are still being downloaded from. Never raises.
    """
    if db.other_active_series_use_url(url, series_id):
        logger.info(f"[{title}] джерело ще потрібне іншим активним записам — не виходжу з нього.")
        return
    try:
        await handler.cleanup(url)
    except Exception as e:
        logger.warning(f"[{title}] cleanup() failed: {e}")


async def _ensure_total_episodes(series: db.sqlite3.Row, title: str, season: int, downloaded_count: int) -> int:
    """
    Resolve (and persist) the season's total episode count if it's still
    unknown and enough episodes are downloaded to be worth asking. Returns
    the known total, or 0 when it's still unknown. It only RESOLVES — the
    decision to stop lives in process_series, after the source has been
    listed, because whether a season is "the end" depends on what the source
    still holds.

    If AniList/DeepSeek don't know AND we haven't already downloaded more
    than the fallback would allow, use DEFAULT_TOTAL_EPISODES_FALLBACK so we
    don't re-ask every cycle. If we've already downloaded MORE than the
    fallback (a backlog title still clearly ongoing), do NOT assign a number
    — that would retroactively declare it "finished" at a count we've
    already exceeded. Leave it 0 and rely on the "N з N" caption pattern; the
    lookup retries next cycle in case a source can identify it later.

    `season` is the season this series row is about (season_filter for a
    per-season row, otherwise last_season) — episodes are counted for that
    season ONLY, and record_episode() resets total_episodes to 0 when a new
    season starts, so a resolved count never leaks into the next season.
    """
    series_id = series["id"]
    total = series["total_episodes"] or 0
    if total or downloaded_count < TOTAL_EPISODES_PROBE_THRESHOLD:
        return total

    found, source = await _lookup_total_episodes(title, season)
    if found:
        db.set_total_episodes(series_id, found)
        logger.info(f"[{title}] сезон {season}: {source} визначив кількість серій = {found}.")
        return found
    if downloaded_count < DEFAULT_TOTAL_EPISODES_FALLBACK:
        db.set_total_episodes(series_id, DEFAULT_TOTAL_EPISODES_FALLBACK)
        logger.info(
            f"[{title}] сезон {season}: {source} не знайшло — fallback = {DEFAULT_TOTAL_EPISODES_FALLBACK}."
        )
        return DEFAULT_TOTAL_EPISODES_FALLBACK
    logger.info(
        f"[{title}] сезон {season}: {source} не знайшло, а вже скачано {downloaded_count} "
        f"(> fallback {DEFAULT_TOTAL_EPISODES_FALLBACK}) — не встановлюю total_episodes, "
        f"покладаюсь на 'N з N' у підписі."
    )
    return 0


async def _stop_season_complete(
    series: db.sqlite3.Row, client, handler, url: str, title: str, display: str,
    season: int, done_count: int, total: int,
):
    """Auto-stop a series whose (known-length) season is fully downloaded, and tell everyone."""
    series_id = series["id"]
    logger.info(f"[{title}] сезон {season}: завантажено {done_count}/{total} — відстеження зупинено.")
    db.stop_series(series_id)
    await release_source(handler, url, series_id, title)

    done_text = (
        f"🏁 **{display}** (сезон {season}): завантажено всі "
        f"{done_count}/{total} серій — знято з відстеження."
    )
    reply_markup = _renew_tracking_keyboard(series_id)
    all_users = settings.allowed_users_set or {series["chat_id"]}
    for uid in all_users:
        try:
            await client.send_message(uid, done_text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Failed to notify {uid}: {e}")


async def process_series(series: db.sqlite3.Row, client, initial_status_msg=None) -> bool:
    """
    Run one check of a series and never let an error escape unseen. Callers
    start this as a fire-and-forget task right after a title is added, so an
    exception used to vanish into "Task exception was never retrieved" while
    the user's "⏳ Перевіряю доступні серії..." message stayed on screen
    forever. The failure is now logged with its traceback and shown on that
    message; the periodic cycle simply tries again next time.
    """
    try:
        return await _process_series(series, client, initial_status_msg)
    except Exception as e:
        logger.error(f"[{series['title']}] перевірку перервано помилкою: {e}", exc_info=True)
        if initial_status_msg:
            try:
                await initial_status_msg.edit_text(
                    f"❌ **{series['title']}**: перевірку перервано помилкою — "
                    f"`{type(e).__name__}: {e}`\nДеталі в логах бота."
                )
            except Exception:
                pass
        return False


async def _process_series(series: db.sqlite3.Row, client, initial_status_msg=None) -> bool:
    """
    Check and download all new (not yet downloaded) episodes for one series.
    Returns True if at least one episode was downloaded.

    `initial_status_msg` — optional Message to finalize with the check result
    (used only for the immediate check triggered right after adding a title,
    so the "⏳ Перевіряю доступні серії..." status doesn't hang forever if
    there turn out to be no new episodes).

    A series row either follows its source through whatever seasons it airs
    (season_filter is NULL — last_season just moves forward), or, for a
    source that holds several seasons at once, is pinned to ONE season
    (season_filter = N) and only ever sees that season's episodes.
    """
    series_id = series["id"]
    chat_id   = series["chat_id"]
    title     = series["title"]  # canonical Romaji — used for folder/file naming
    display   = db.resolve_display_title(series)  # localized name shown to users (backfills legacy rows)
    url       = series["base_url"]
    category  = series["category"]
    dest_path = settings.DOWNLOAD_PATH if category == "anime" else settings.DORAMA_PATH
    season_filter = series["season_filter"]
    current_season = season_filter or series["last_season"]

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

    # Resolve the season's length (needs only the DB + AniList, no Telegram).
    # Episodes are counted for the current season ALONE — one row spans every
    # season a title airs, so a combined count would be meaningless.
    done = db.get_downloaded_set(series_id)
    done_count = sum(1 for (s, _e) in done if s == current_season)
    known_total = await _ensure_total_episodes(series, title, current_season, done_count)

    # Fetch all currently available DUB episodes
    available = await handler.list_episodes(url)
    if season_filter is not None:
        available = [e for e in available if e["season"] == season_filter]
    if not available:
        logger.info(f"[{title}] немає доступних дубльованих епізодів.")
        await _finalize_status(f"⚠️ **{display}**: серій ще не знайдено.")
        return False

    new_eps = sorted(
        (e for e in available if (e["season"], e["episode"]) not in done),
        key=lambda e: (e["season"], e["episode"])
    )

    if not new_eps:
        # Season complete = its known length is downloaded AND the source holds
        # nothing later. A source with later seasons (a channel that carries
        # S1+S2+S3 together) is NOT finished just because one season is — that
        # was the 24/24 trap: stop after S1E24 and never touch S2/S3. Rows
        # pinned to one season never look at other seasons (they have rows of
        # their own).
        later_seasons_in_source = season_filter is None and any(e["season"] > current_season for e in available)
        if known_total > 0 and done_count >= known_total and not later_seasons_in_source:
            await _stop_season_complete(series, client, handler, url, title, display, current_season, done_count, known_total)
            await _finalize_status(f"🏁 **{display}**: усі серії вже завантажені — знято з відстеження.")
            return False
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

            # Two independent finale signals: the caption-based "N з N"
            # pattern, OR this episode itself reaching the already-known
            # total_episodes of ITS season. Either one only counts when the
            # source holds nothing LATER than this episode — a channel that
            # carries every season at once has its S1 finale ("24 з 24")
            # long before the end of the title, and stopping there would
            # abandon S2/S3 (and walk out of the channel).
            has_later = any((e["season"], e["episode"]) > (season, episode) for e in available)
            reached_end = ep.get("is_finale", False) or (
                known_total > 0 and season == current_season and episode >= known_total
            )
            is_finale = reached_end and not has_later

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
                await release_source(handler, url, series_id, title)
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


async def process_many(series_ids: list[int], client):
    """
    Process several series strictly one after another, with the usual pause
    between them. Used when one action creates several rows at once (the
    per-season rows of a multi-season channel): they share ONE userbot
    account and one channel, so running them in parallel would only fight
    over Telegram's per-account rate limits for no gain.
    """
    for i, series_id in enumerate(series_ids):
        row = db.get_series_by_id(series_id)
        if row and row["active"]:
            try:
                await process_series(row, client)
            except Exception as e:
                logger.error(f"Error processing series #{series_id}: {e}", exc_info=True)
        if i < len(series_ids) - 1:
            await asyncio.sleep(INTER_SERIES_DELAY_SECONDS)


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
