import asyncio
import logging
import os
import re
from enum import Enum
from logging.handlers import RotatingFileHandler
from pyrogram import Client, idle, filters
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
from pyrogram.errors import FloodWait
from config.config import settings
from analyzer.mapper import mapper
from analyzer.ai_cleaner import extract_metadata, extract_episode, extract_watch_link
from core.queue_manager import queue_manager
from core.renamer import sanitize_title, scan_existing_episodes
from urllib.parse import quote
from anime_tracker import db as anime_db, checker as anime_checker, fixer as anime_fixer
from anime_tracker.sites import get_handler as get_site_handler, supported_domains
from anime_tracker.userbot import build_userbot_client

# Setup logging
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(
    'app.log',
    maxBytes=10*1024*1024,  # 10 MB
    backupCount=5,
    encoding='utf-8'
)
file_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

os.makedirs("sessions", exist_ok=True)

# --- Global State ---

# chat_id -> asyncio.Future: waiting for text reply from user
waiting_for_user_input: dict[int, asyncio.Future] = {}

class BotMode(Enum):
    NORMAL = "normal"
    BATCH  = "batch"

chat_modes:   dict[int, BotMode]       = {}
batch_states: dict[int, dict]          = {}  # {title, season, timer_task}
batch_locks:  dict[int, asyncio.Lock]  = {}


# --- Initialize Client ---
WORKERS = 120

if settings.SESSION_STRING:
    app = Client(
        "tg_downloader",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        session_string=settings.SESSION_STRING,
        in_memory=True,
        workers=WORKERS,
    )
elif settings.BOT_TOKEN:
    app = Client(
        "sessions/tg_downloader",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        bot_token=settings.BOT_TOKEN,
        workers=WORKERS,
    )
else:
    logger.warning("No BOT_TOKEN found. Running as Userbot!")
    app = Client(
        "sessions/tg_downloader",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        workers=WORKERS,
    )


# --- Access Control Filter ---
async def is_authorized(_, __, update):
    allowed = settings.allowed_users_set
    if not allowed:
        return True
    if update.from_user and update.from_user.id in allowed:
        return True
    return False

auth_filter = filters.create(is_authorized)


# --- Mode Helpers ---

def mode_keyboard(mode: BotMode = BotMode.NORMAL) -> InlineKeyboardMarkup:
    anime_row = [InlineKeyboardButton("🎬 Аніме", callback_data="mode_anime_list")]
    if mode == BotMode.BATCH:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Normal",       callback_data="mode_normal"),
                InlineKeyboardButton("✅ Batch",         callback_data="mode_batch"),
                InlineKeyboardButton("⏹ End Session",   callback_data="mode_end"),
            ],
            anime_row,
        ])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Normal", callback_data="mode_normal"),
            InlineKeyboardButton("📦 Batch",  callback_data="mode_batch"),
        ],
        anime_row,
    ])


async def end_batch_session(chat_id: int, notify_text: str = None):
    """Cleanup batch state and optionally notify the chat."""
    state = batch_states.pop(chat_id, {})
    task  = state.get("timer_task")
    if task and not task.done():
        task.cancel()
    batch_locks.pop(chat_id, None)
    chat_modes.pop(chat_id, None)
    if notify_text:
        try:
            await app.send_message(
                chat_id, notify_text,
                reply_markup=mode_keyboard(BotMode.NORMAL)
            )
        except Exception as e:
            logger.error(f"Failed to notify batch end: {e}")


async def batch_inactivity_timer(chat_id: int):
    """Fires after 30 min of inactivity and ends the batch session."""
    await asyncio.sleep(30 * 60)
    if chat_modes.get(chat_id) == BotMode.BATCH:
        await end_batch_session(
            chat_id,
            "⏰ Batch session expired (30 min inactivity). Back to Normal mode."
        )


def reset_batch_timer(chat_id: int):
    """Cancel the existing inactivity timer and start a fresh one."""
    state = batch_states.get(chat_id)
    if state is None:
        return
    old_task = state.get("timer_task")
    if old_task and not old_task.done():
        old_task.cancel()
    new_task = asyncio.create_task(batch_inactivity_timer(chat_id))
    state["timer_task"] = new_task


# --- Handlers ---

# 0. Global Logger (runs first via group=-1)
@app.on_message(group=-1)
async def log_all_messages(client, message):
    user = message.from_user
    user_id = user.id if user else "Unknown"
    name = user.first_name if user else "Unknown"
    # str() first: Pyrogram's Message text is a custom str subclass with
    # UTF-16-surrogate-aware slicing (for entity offset correctness) that can
    # raise UnicodeDecodeError if a slice lands mid-surrogate-pair (e.g. an
    # emoji right at the cutoff). Converting to plain str avoids that crash
    # for this purely-cosmetic log preview.
    text_preview = str(message.text or message.caption or "Media/Other")
    logger.info(f"📨 MSG | User: {name} ({user_id}) | Chat: {message.chat.id} | Content: {text_preview[:50]}")


# 1. Public Commands
@app.on_message(filters.command("start"))
async def start_handler(client, message):
    logger.info(f"Start command from {message.from_user.id}")
    await message.reply_text(
        f"👋 Welcome!\n\n"
        f"I'm a private video downloader bot.\n"
        f"Your User ID: `{message.from_user.id}`\n\n"
        f"Use /help to see available commands."
    )

@app.on_message(filters.command("id"))
async def id_handler(client, message):
    await message.reply_text(f"Your User ID is: `{message.from_user.id}`")


# 2. Protected Commands
@app.on_message(auth_filter & filters.command("help"))
async def help_handler(client: Client, message: Message):
    mode = chat_modes.get(message.chat.id, BotMode.NORMAL)
    mode_str = "📦 Batch" if mode == BotMode.BATCH else "📥 Normal"
    await message.reply_text(
        "🤖 **Telegram Video Downloader**\n\n"
        "**Commands:**\n"
        "• /start — Welcome & your User ID\n"
        "• /id — Get your Telegram User ID\n"
        "• /help — This message\n"
        "• /mode — Switch operating mode\n"
        "• /anime — Авто-відстеження аніме за посиланням\n\n"
        "📥 **Normal Mode** _(default)_\n"
        "AI analyzes each video independently: extracts title, season & episode.\n"
        "Unknown titles → you confirm the official name → saved to DB.\n\n"
        "📦 **Batch Mode** _(30 min inactivity session)_\n"
        "Best for series where AI keeps misidentifying episodes.\n"
        "• Set title & season once for the whole session\n"
        "• Each video: AI extracts only the episode number (with context)\n"
        "• Isolated from DB — no reads or writes to mappings.json\n"
        "• Ends on 30 min inactivity or via 'End Session' button\n\n"
        "🎬 **Anime Mode**\n"
        "Скинь посилання на топік каналу — тайтл одразу додається до відстеження.\n"
        "Детальніше: `/anime help`\n\n"
        f"**Current mode:** {mode_str}",
        reply_markup=mode_keyboard(mode)
    )


@app.on_message(auth_filter & filters.command("mode"))
async def mode_handler(client: Client, message: Message):
    mode = chat_modes.get(message.chat.id, BotMode.NORMAL)
    mode_str = "📦 Batch" if mode == BotMode.BATCH else "📥 Normal"
    await message.reply_text(
        f"Current mode: **{mode_str}**\n\nSelect mode:",
        reply_markup=mode_keyboard(mode)
    )


@app.on_callback_query(auth_filter & filters.regex("^mode_"))
async def mode_callback(client: Client, query: CallbackQuery):
    chat_id = query.message.chat.id
    current = chat_modes.get(chat_id, BotMode.NORMAL)

    if query.data == "mode_normal":
        if current == BotMode.NORMAL:
            await query.answer("Already in Normal mode")
            return
        await end_batch_session(chat_id)
        chat_modes[chat_id] = BotMode.NORMAL
        await query.answer("Switched to Normal mode")
        try:
            await query.message.edit_text(
                "✅ **Normal Mode** activated.",
                reply_markup=mode_keyboard(BotMode.NORMAL)
            )
        except Exception:
            pass

    elif query.data == "mode_batch":
        if current == BotMode.BATCH:
            await query.answer("Already in Batch mode")
            return
        await end_batch_session(chat_id)
        chat_modes[chat_id] = BotMode.BATCH
        batch_states[chat_id] = {"title": None, "season": None, "timer_task": None}
        reset_batch_timer(chat_id)
        await query.answer("Switched to Batch mode")
        try:
            await query.message.edit_text(
                "✅ **Batch Mode** activated.\n\n"
                "Forward your videos — I'll ask for title & season on the first one.\n"
                "Session expires after 30 min of inactivity.",
                reply_markup=mode_keyboard(BotMode.BATCH)
            )
        except Exception:
            pass

    elif query.data == "mode_end":
        await query.answer("Session ended")
        try:
            await query.message.edit_text(
                "⏹ Batch session ended.",
                reply_markup=mode_keyboard(BotMode.NORMAL)
            )
        except Exception:
            pass
        await end_batch_session(
            chat_id,
            "✅ Batch session finished. Back to Normal mode."
        )

    elif query.data == "mode_anime_list":
        await query.answer()
        text, kb = _tracking_list_content("anime")
        try:
            await query.message.reply_text(text, reply_markup=kb)
        except Exception:
            pass


# 3. Text input router (passes replies to ask_user futures)
@app.on_message(auth_filter & filters.text & ~filters.command(["start", "help", "id", "mode", "anime"]))
async def text_handler(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id in waiting_for_user_input:
        future = waiting_for_user_input[chat_id]
        if not future.done():
            future.set_result(message.text)
        return

    url = await _find_watch_link(message)
    if url:
        await _track_anime_url(client, message, url)
        return


# --- Shared Utilities ---

async def ask_user(chat_id: int, prompt: str, status_msg: Message, timeout: int = 300) -> str | None:
    """Asks a question by EDITING an existing status message."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    waiting_for_user_input[chat_id] = future
    try:
        await status_msg.edit_text(prompt)
        reply = await asyncio.wait_for(future, timeout=timeout)
        if reply.lower() == "cancel":
            return None
        return reply
    finally:
        waiting_for_user_input.pop(chat_id, None)


async def ask_user_fresh(chat_id: int, prompt: str, timeout: int = 300) -> str | None:
    """Asks a question by SENDING A NEW message (always appears at bottom of chat)."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    waiting_for_user_input[chat_id] = future
    try:
        await app.send_message(chat_id, prompt)
        reply = await asyncio.wait_for(future, timeout=timeout)
        if reply.lower() == "cancel":
            return None
        return reply
    except asyncio.TimeoutError:
        try:
            await app.send_message(chat_id, "❌ Timeout. No reply received.")
        except Exception:
            pass
        return None
    finally:
        waiting_for_user_input.pop(chat_id, None)


# --- Batch Mode Handler ---

async def handle_batch_video(client: Client, message: Message, status_msg: Message):
    chat_id = message.chat.id
    media   = message.video or message.document
    filename_hint = (media.file_name if media else "") or "video.mp4"

    if chat_id not in batch_locks:
        batch_locks[chat_id] = asyncio.Lock()

    # If lock is already held → show "in queue" immediately so the user knows bot is alive
    if batch_locks[chat_id].locked():
        try:
            await status_msg.edit_text(f"⏳ In queue: `{filename_hint[:60]}`")
        except Exception:
            pass

    async with batch_locks[chat_id]:
        # Mode may have changed while waiting for the lock
        if chat_modes.get(chat_id) != BotMode.BATCH:
            return

        # Reset the 30-min inactivity timer on every video
        reset_batch_timer(chat_id)

        state = batch_states.get(chat_id, {})

        # ── SETUP PHASE: get title & season (first video only) ──────────────
        # All questions are sent as NEW messages so they always appear at the
        # bottom of the chat and never get buried under incoming video messages.
        if not state.get("title"):
            try:
                await status_msg.edit_text(f"⚙️ `{filename_hint[:60]}` — analyzing title...")
            except Exception:
                pass

            text_to_analyze = message.caption or filename_hint
            ai_data   = await extract_metadata(text_to_analyze)
            raw_title = ai_data.get("title") if ai_data else None

            if raw_title:
                title_prompt = (
                    f"🔎 AI detected: `{raw_title}`\n\n"
                    f"Reply with the **Official Romaji Title** to confirm/correct\n"
                    f"_(or reply `cancel` to abort)_"
                )
            else:
                title_prompt = (
                    "⚠️ AI couldn't detect the title.\n\n"
                    "Reply with the **Official Romaji Title**\n"
                    "_(or reply `cancel` to abort)_"
                )

            # Fresh message → always at the bottom even if new videos arrived
            title = await ask_user_fresh(chat_id, title_prompt)
            if not title:
                try:
                    await status_msg.edit_text(f"❌ Cancelled: `{filename_hint[:60]}`")
                except Exception:
                    pass
                return

            season_str = await ask_user_fresh(
                chat_id,
                f"📀 Title: **{title}**\n\nReply with the **Season number**\n_(or `cancel`)_"
            )
            if not season_str or not season_str.isdigit():
                try:
                    await status_msg.edit_text(f"❌ Invalid season. Cancelled: `{filename_hint[:60]}`")
                except Exception:
                    pass
                return

            state["title"]  = title.strip()
            state["season"] = int(season_str)
            batch_states[chat_id] = state

            # Session summary — one permanent message, visible above all future videos
            try:
                await app.send_message(
                    chat_id,
                    f"✅ **Batch session ready**\n"
                    f"📺 {state['title']} — Season {state['season']}\n\n"
                    f"_Processing queued videos..._"
                )
            except Exception:
                pass

        title  = state["title"]
        season = state["season"]

        # Show per-video status while extracting episode
        try:
            await status_msg.edit_text(
                f"🔍 `{filename_hint[:60]}`\n"
                f"**{title}** S{season:02d} — detecting episode..."
            )
        except Exception:
            pass

        # ── EPISODE EXTRACTION ───────────────────────────────────────────────
        text    = message.caption or filename_hint
        episode = await extract_episode(text, title, season)

        if not episode:
            # Ask as a fresh message so it's always visible at the bottom
            episode_str = await ask_user_fresh(
                chat_id,
                f"📺 Episode not detected for:\n`{filename_hint[:80]}`\n\n"
                f"Reply with the **Episode number** _(or `cancel` to skip)_"
            )
            if not episode_str or not episode_str.isdigit():
                try:
                    await status_msg.edit_text(f"⏭ Skipped: `{filename_hint[:60]}`")
                except Exception:
                    pass
                return
            episode = int(episode_str)

        safe_title = sanitize_title(title)
        metadata = {
            "canonical_name": safe_title,
            "season":  season,
            "episode": episode,
        }
        await queue_manager.add_task(
            client, message, metadata,
            status_msg=status_msg,
            reply_markup=mode_keyboard(BotMode.BATCH)
        )


# Channel posts announcing a new episode are often a poster PHOTO with a
# caption (no video attached at all — the actual video is elsewhere/in the
# linked topic) — check the caption for a "watch all episodes" archive link
# the same way a forwarded video's caption is checked. Nothing to download
# here, this handler exists purely for the tracking-link check.
@app.on_message(auth_filter & filters.photo)
async def photo_handler(client: Client, message: Message):
    await _maybe_track_from_caption(client, message)


# --- Normal Mode Handler ---

@app.on_message(auth_filter & (filters.video | filters.document))
async def video_handler(client: Client, message: Message):
    media = message.video or message.document
    if not media:
        return
    if message.document and "video" not in (message.document.mime_type or ""):
        return

    logger.info(f"New video from: {message.chat.title or message.chat.first_name}")

    # Opportunistic: some channel posts forwarded together with a video
    # include a link to the full topic/archive in their caption (e.g.
    # "Онлайн в телеграмі: t.me/...") — if found, kick off anime tracking for
    # the whole title in the background. Best-effort and independent of the
    # single-video download below (already-downloaded episodes are skipped
    # via the existing-files scan, so this doesn't duplicate this video).
    asyncio.create_task(_maybe_track_from_caption(client, message))

    status_msg = None
    try:
        status_msg = await message.reply_text(
            f"⏳ Processing: `{(media.file_name or 'video.mp4')[:60]}`"
        )
    except Exception as e:
        logger.warning(f"Could not reply: {e}")

    # Branch: Batch mode
    if chat_modes.get(message.chat.id) == BotMode.BATCH:
        await handle_batch_video(client, message, status_msg)
        return

    # ── NORMAL MODE ──────────────────────────────────────────────────────────
    text_to_analyze = message.caption or ""
    filename = media.file_name or "video.mp4"
    if len(text_to_analyze) < 5:
        text_to_analyze = filename

    if status_msg:
        try:
            await status_msg.edit_text(f"🧐 Processing: `{text_to_analyze[:100]}`")
        except Exception:
            pass

    # Step A: AI Analysis
    ai_data = await extract_metadata(text_to_analyze)

    if not ai_data or not ai_data.get('title'):
        title = await ask_user_fresh(
            message.chat.id,
            "⚠️ AI failed.\n\nReply with the **Official Romaji Title** _(or `cancel`)_:"
        )
        if not title:
            if status_msg:
                try: await status_msg.edit_text("❌ Cancelled by user.")
                except Exception: pass
            return

        episode = await ask_user_fresh(message.chat.id, "📺 Enter **Episode number** _(or `cancel`)_:")
        if not episode or not episode.isdigit():
            if status_msg:
                try: await status_msg.edit_text("❌ Invalid episode.")
                except Exception: pass
            return

        season = await ask_user_fresh(message.chat.id, "📀 Enter **Season number** _(or `cancel`)_:")
        if not season or not season.isdigit():
            if status_msg:
                try: await status_msg.edit_text("❌ Invalid season.")
                except Exception: pass
            return

        ai_data = {
            "title":   title.strip(),
            "episode": int(episode),
            "season":  int(season),
        }
        if status_msg:
            try:
                await status_msg.edit_text(
                    f"✅ Manual data set:\n**{ai_data['title']}**\n"
                    f"S{ai_data['season']:02d}E{ai_data['episode']:02d}"
                )
            except Exception: pass

    logger.info(f"AI Extracted: {ai_data}")

    # Step B: Mapper check
    mapped_title = mapper.get_mapping(ai_data['title'])
    final_title = None

    if mapped_title:
        logger.info(f"Found known mapping: {ai_data['title']} -> {mapped_title}")
        final_title = mapped_title
        if status_msg:
            try:
                await status_msg.edit_text(f"✅ Found in DB: `{final_title}`")
            except FloodWait as e:
                logger.warning(f"FloodWait: need to wait {e.value}s. Skipping status update.")
            except Exception as e:
                logger.debug(f"Failed to update status: {e}")
    else:
        # Step C: Ask user for official title
        search_query = quote(ai_data['title'])
        anitube_url = f"https://anitube.in.ua/index.php?do=search&subaction=search&story={search_query}"
        google_url  = f"https://www.google.com/search?q={search_query}+anime"

        user_reply = await ask_user_fresh(
            message.chat.id,
            f"⚠️ Unknown Title: `{ai_data['title']}`\n"
            f"🔎 [Anitube]({anitube_url}) | [Google]({google_url})\n\n"
            f"Reply with the **Official Romaji Title** to save it _(or `cancel`)_:"
        )
        if not user_reply:
            if status_msg:
                try: await status_msg.edit_text("❌ Cancelled by user.")
                except Exception: pass
            return

        mapper.add_mapping(ai_data['title'], user_reply)
        final_title = user_reply
        if status_msg:
            try: await status_msg.edit_text(f"✅ Saved & Using: `{final_title}`")
            except Exception: pass

    # Step D: Queue download
    safe_canonical_name = sanitize_title(final_title)
    metadata = {
        "canonical_name": safe_canonical_name,
        "season":         ai_data.get('season', 1),
        "episode":        ai_data.get('episode'),
    }
    await queue_manager.add_task(client, message, metadata, status_msg=status_msg)


# ── ANIME TRACKING (Telegram-link auto-tracking) ─────────────────────────────

_CATEGORY_LABELS = {"anime": "Аніме"}
_CATEGORY_HINTS = {
    "anime": "`/anime https://t.me/КаналНазва/12345`",
}


def _tracking_list_content(category: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Build message text + keyboard for the shared anime tracking list — every
    authorized user tracks the same pool of titles (and gets notified of every
    download), so the list is shared too, not scoped to whoever added a title.
    """
    label = _CATEGORY_LABELS.get(category, category.capitalize())
    series_list = anime_db.get_all_active_series(category)
    if not series_list:
        return (
            f"📋 **{label} / відстеження**\n\n"
            f"Немає активних тайтлів.\n\n"
            f"Щоб додати:\n{_CATEGORY_HINTS.get(category, '')}",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔧 Виправити тайтл", callback_data="anime_fixlist")
            ]])
        )
    text = f"📋 **{label} / відстеження:**\n\n"
    buttons = [[
        InlineKeyboardButton("🔄✅ Перевірити все", callback_data=f"anime_checkall_{category}"),
        InlineKeyboardButton("🔧 Виправити тайтл", callback_data="anime_fixlist"),
    ]]
    for s in series_list:
        started = s["started_at"][:10]
        display = anime_db.resolve_display_title(s)  # backfills legacy rows via mapper.db reverse lookup

        text += (
            f"• **{display}**\n"
            f"  S{s['last_season']:02d}E{s['last_episode']:02d} | додано {started}\n\n"
        )
        buttons.append([
            InlineKeyboardButton(f"⏹ {display}", callback_data=f"anime_stopask_{s['id']}")
        ])
    return text, InlineKeyboardMarkup(buttons)


@app.on_callback_query(auth_filter & filters.regex("^anime_stopask_"))
async def anime_stopask_callback(client: Client, query: CallbackQuery):
    """First tap on ⏹ — ask for confirmation instead of stopping immediately,
    since a stray tap would otherwise silently unsubscribe/unfile a channel."""
    series_id = int(query.data.split("_")[-1])
    series = anime_db.get_series_by_id(series_id)
    title = anime_db.resolve_display_title(series) if series else f"#{series_id}"
    await query.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, зупинити", callback_data=f"anime_stopyes_{series_id}"),
        InlineKeyboardButton("❌ Скасувати", callback_data=f"anime_stopcancel_{series_id}"),
    ]])
    try:
        await query.message.edit_text(f"Зупинити відстеження **{title}**?", reply_markup=kb)
    except Exception:
        pass


@app.on_callback_query(auth_filter & filters.regex("^anime_stopyes_"))
async def anime_stopyes_callback(client: Client, query: CallbackQuery):
    series_id = int(query.data.split("_")[-1])
    series = anime_db.get_series_by_id(series_id)
    title = anime_db.resolve_display_title(series) if series else f"#{series_id}"
    category = series["category"] if series else "anime"

    anime_db.stop_series(series_id)
    await query.answer(f"⏹ Зупинено: {title}")

    if series:
        try:
            handler = get_site_handler(series["base_url"])
            if handler:
                await handler.cleanup(series["base_url"])
        except Exception as e:
            logger.warning(f"cleanup() on manual stop failed: {e}")

    # Refresh the list in-place (same category the stopped title belonged to)
    text, kb = _tracking_list_content(category)
    try:
        await query.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@app.on_callback_query(auth_filter & filters.regex("^anime_stopcancel_"))
async def anime_stopcancel_callback(client: Client, query: CallbackQuery):
    series_id = int(query.data.split("_")[-1])
    series = anime_db.get_series_by_id(series_id)
    category = series["category"] if series else "anime"
    await query.answer("Скасовано")

    text, kb = _tracking_list_content(category)
    try:
        await query.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


# ── ANIME MODE: "Виправити тайтл" — manually delete/redownload a specific
# already-downloaded episode. Needed for cases where the source channel
# posts the wrong variant first (e.g. subtitles-only) then later replaces it
# with the correct one (e.g. the dub) — the bot has already marked that
# episode downloaded and won't revisit it on its own. ──────────────────────

def _fix_series_label(s) -> str:
    status = "🟢" if s["active"] else "⏹"
    return f"{status} {anime_db.resolve_display_title(s)}"


@app.on_callback_query(auth_filter & filters.regex("^anime_fixlist$"))
async def anime_fixlist_callback(client: Client, query: CallbackQuery):
    series_list = anime_db.get_recent_series()
    await query.answer()
    if not series_list:
        try:
            await query.message.edit_text(
                "🔧 Немає тайтлів за останні ~6 місяців.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Назад", callback_data="anime_fixback")
                ]])
            )
        except Exception:
            pass
        return

    buttons = [
        [InlineKeyboardButton(_fix_series_label(s), callback_data=f"anime_fixsel_{s['id']}")]
        for s in series_list
    ]
    buttons.append([InlineKeyboardButton("⬅ Назад", callback_data="anime_fixback")])
    try:
        await query.message.edit_text(
            "🔧 **Виправити тайтл** — обери, з яким є проблема:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception:
        pass


@app.on_callback_query(auth_filter & filters.regex("^anime_fixback$"))
async def anime_fixback_callback(client: Client, query: CallbackQuery):
    await query.answer()
    text, kb = _tracking_list_content("anime")
    try:
        await query.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


async def _show_fix_episodes(query: CallbackQuery, series_id: int):
    series = anime_db.get_series_by_id(series_id)
    if not series:
        try:
            await query.message.edit_text("Тайтл не знайдено (можливо, видалений).")
        except Exception:
            pass
        return
    display = anime_db.resolve_display_title(series)
    episodes = anime_db.get_episodes(series_id)
    if not episodes:
        try:
            await query.message.edit_text(
                f"🔧 **{display}**: немає скачаних епізодів у базі.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Назад", callback_data="anime_fixlist")
                ]])
            )
        except Exception:
            pass
        return

    buttons = []
    for ep in episodes:
        label = f"S{ep['season']:02d}E{ep['episode']:02d}"
        buttons.append([
            InlineKeyboardButton(f"🗑 {label}", callback_data=f"anime_fixdelask_{series_id}_{ep['season']}_{ep['episode']}"),
            InlineKeyboardButton(f"🔄 {label}", callback_data=f"anime_fixredl_{series_id}_{ep['season']}_{ep['episode']}"),
        ])
    buttons.append([InlineKeyboardButton("⬅ Назад", callback_data="anime_fixlist")])
    try:
        await query.message.edit_text(
            f"🔧 **{display}** — скачані епізоди:\n"
            f"🗑 видалити (з диску і бази) · 🔄 перезавантажити (перекачати заново з каналу)",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception:
        pass


@app.on_callback_query(auth_filter & filters.regex("^anime_fixsel_"))
async def anime_fixsel_callback(client: Client, query: CallbackQuery):
    series_id = int(query.data.split("_")[-1])
    await query.answer()
    await _show_fix_episodes(query, series_id)


@app.on_callback_query(auth_filter & filters.regex("^anime_fixdelask_"))
async def anime_fixdelask_callback(client: Client, query: CallbackQuery):
    parts = query.data.split("_")
    series_id, season, episode = int(parts[-3]), int(parts[-2]), int(parts[-1])
    series = anime_db.get_series_by_id(series_id)
    display = anime_db.resolve_display_title(series) if series else f"#{series_id}"
    await query.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, видалити", callback_data=f"anime_fixdelyes_{series_id}_{season}_{episode}"),
        InlineKeyboardButton("❌ Скасувати", callback_data=f"anime_fixsel_{series_id}"),
    ]])
    try:
        await query.message.edit_text(
            f"Видалити **{display}** S{season:02d}E{episode:02d} з диску і з бази?\n"
            f"(наступна перевірка сама перекачає її знову, якщо серія все ще є в каналі)",
            reply_markup=kb
        )
    except Exception:
        pass


@app.on_callback_query(auth_filter & filters.regex("^anime_fixdelyes_"))
async def anime_fixdelyes_callback(client: Client, query: CallbackQuery):
    parts = query.data.split("_")
    series_id, season, episode = int(parts[-3]), int(parts[-2]), int(parts[-1])
    series = anime_db.get_series_by_id(series_id)
    if not series:
        await query.answer("Тайтл не знайдено.")
        return
    file_deleted = anime_fixer.delete_episode(series, season, episode)
    await query.answer("🗑 Видалено (файл + запис у базі)" if file_deleted else "🗑 Запис видалено (файл на диску не знайдено)")
    await _show_fix_episodes(query, series_id)


@app.on_callback_query(auth_filter & filters.regex("^anime_fixredl_"))
async def anime_fixredl_callback(client: Client, query: CallbackQuery):
    parts = query.data.split("_")
    series_id, season, episode = int(parts[-3]), int(parts[-2]), int(parts[-1])
    series = anime_db.get_series_by_id(series_id)
    if not series:
        await query.answer("Тайтл не знайдено.")
        return
    display = anime_db.resolve_display_title(series)
    await query.answer(f"🔄 Перезавантажую S{season:02d}E{episode:02d}...")

    status = None
    try:
        status = await query.message.reply_text(
            f"⏳ Перезавантажую **{display}** S{season:02d}E{episode:02d}..."
        )
    except Exception as e:
        logger.warning(f"Could not send redownload status message: {e}")

    ok = await anime_fixer.redownload_episode(series, season, episode)

    if status:
        try:
            await status.edit_text(
                f"✅ Перезавантажено: **{display}** S{season:02d}E{episode:02d}"
                if ok else
                f"❌ Не вдалось перезавантажити **{display}** S{season:02d}E{episode:02d} "
                f"(серію не знайдено в каналі, або сталась помилка завантаження — див. логи)"
            )
        except Exception:
            pass


async def _run_checkall(client: Client, series_list: list, status_msg: Message):
    """
    Run process_series for every title, then finalize the status message.
    Strictly sequential, WITH a pause between titles — not asyncio.gather.
    This all runs through ONE userbot account, so firing every title's
    Telegram API calls back-to-back (even non-concurrently, with no pause)
    was still enough to trip FLOOD_WAIT once there were more than a couple
    of tracked titles; there's no real parallelism to gain here anyway.
    """
    try:
        for i, s in enumerate(series_list):
            try:
                await anime_checker.process_series(s, client)
            except Exception as e:
                logger.error(f"checkall: error processing '{s['title']}': {e}")
            if i < len(series_list) - 1:
                await asyncio.sleep(anime_checker.INTER_SERIES_DELAY_SECONDS)
    finally:
        try:
            await status_msg.edit_text(f"✅ Перевірку завершено ({len(series_list)} тайтлів).")
        except Exception:
            pass


@app.on_callback_query(auth_filter & filters.regex("^anime_checkall_"))
async def anime_checkall_callback(client: Client, query: CallbackQuery):
    category = query.data.replace("anime_checkall_", "", 1)
    series_list = anime_db.get_all_active_series(category)
    if not series_list:
        await query.answer("Немає активних тайтлів.")
        return

    await query.answer(f"🔄 Перевіряю {len(series_list)} тайтлів...")
    status_msg = await query.message.reply_text(
        f"🔄 Перевірка розпочалась: {len(series_list)} тайтлів..."
    )
    asyncio.create_task(_run_checkall(client, series_list, status_msg))


# ── ANIME MODE (Telegram-link auto-tracking) ─────────────────────────────────

ANIME_HELP = (
    "🎬 **Аніме — авто-відстеження нових серій**\n\n"
    "Скинь посилання на топік у Telegram-каналі (медіатеці) — тайтл одразу "
    "додається до відстеження. Бот перевірятиме нові серії кожні **6 годин**.\n\n"
    "**Як додати:**\n"
    "Просто кинь посилання боту (можна без команди):\n"
    "`https://t.me/КаналНазва/12345`\n"
    "або: `/anime https://t.me/КаналНазва/12345`\n\n"
    "**Про завантаження:**\n"
    "• Додається одразу, без підтвердження — якщо назву не вдалось розпізнати, бот перепитає\n"
    "• Сезон/епізод кожної серії визначає AI з підпису повідомлення\n"
    "• Якщо підпис містить «N з N» (остання серія) — відстеження зупиняється автоматично\n"
    "• Відстежується до **6 місяців** від дати додавання\n"
    "• При успішному завантаженні — сповіщення отримують усі користувачі бота\n\n"
    "**Команди:**\n"
    "• `/anime {url}` — додати тайтл\n"
    "• `/anime list` — список активних тайтлів з кнопками зупинки\n"
    "• `/anime help` — ця довідка"
)


# Matches a Telegram topic/message link anywhere in a text message, with or
# without a URL scheme — "https://t.me/RH_MediaLib/20835" and bare
# "t.me/RH_MediaLib/20823" both match. The (?<![\w.]) guard prevents matching
# "t.me" embedded inside a longer word/domain (e.g. "not.me/xyz/123").
TG_LINK_RE = re.compile(r'(?:https?://)?(?<![\w.])t\.me/[A-Za-z0-9_]+/\d+', re.IGNORECASE)


def _normalize_url(url: str) -> str:
    """Prepend https:// if the URL was given without a scheme (bare t.me link)."""
    url = url.strip()
    if not re.match(r'^https?://', url, re.IGNORECASE):
        url = f"https://{url}"
    return url


def _extract_link_entities(message: Message) -> list[tuple[str, str]]:
    """
    Return [(visible_label, url), ...] for masked hyperlinks in a message's
    text or caption. Needed because some channels format their "watch online"
    link as a hyperlink where the visible label (e.g. "ONLINE (озвучення)")
    is all that appears in the text — the real URL is only in the entity
    metadata, invisible to both a plain-text regex and an LLM given text alone.
    """
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    links = []
    for e in entities:
        if e.type == MessageEntityType.TEXT_LINK and e.url:
            label = text[e.offset: e.offset + e.length]
            links.append((label, e.url))
    return links


async def _find_watch_link(message: Message) -> str | None:
    """
    Look for a "watch online with dub" Telegram topic link in a message's
    text or caption — a plain-text message, a forwarded video's caption, or a
    photo-post caption.

    Checks, in order:
    1. A bare regex match in the visible text (fast, free, no API call).
    2. DeepSeek, given the text and any masked-hyperlink candidates found via
       entity metadata (label -> url) — needed since some channels hide the
       real URL behind a label like "ONLINE (озвучення)" that never appears
       as plain text. Runs whenever hyperlinks were found, or the text is
       long enough to plausibly be a channel post (skips short chit-chat).
    """
    text = message.text or message.caption or ""
    if not text:
        return None

    m = TG_LINK_RE.search(text)
    if m:
        logger.info(f"[watch-link] regex hit: {m.group(0)!r}")
        return m.group(0)

    hyperlinks = _extract_link_entities(message)
    logger.info(
        f"[watch-link] no plain link in text (len={len(text)}), "
        f"{len(hyperlinks)} hyperlink entities found: "
        f"{[label for label, _ in hyperlinks]}"
    )
    if hyperlinks or len(text) >= 60:
        return await extract_watch_link(text, hyperlinks=hyperlinks or None)
    logger.info("[watch-link] text too short and no hyperlinks — skipping DeepSeek call.")
    return None


async def _maybe_track_from_caption(client: Client, message: Message):
    """
    Best-effort background check: does this video's caption also contain a
    "watch all episodes" topic link? If so, start anime tracking for it.
    Runs as a fire-and-forget task from video_handler — any failure here must
    never affect the primary single-video download.
    """
    try:
        url = await _find_watch_link(message)
        if url:
            await _track_anime_url(client, message, url)
    except Exception as e:
        logger.error(f"Caption-based tracking check failed: {e}", exc_info=True)


async def _track_anime_url(client: Client, message: Message, url: str):
    """
    Shared logic for adding a title to anime tracking.
    Used by both `/anime {url}` and a bare t.me link pasted directly
    (no command prefix needed — the bot reacts to the link itself).
    """
    url = _normalize_url(url)
    handler = get_site_handler(url)
    if not handler:
        domains = ", ".join(supported_domains())
        await message.reply_text(
            f"❌ Джерело не підтримується.\nЗараз доступно: `{domains}`"
        )
        return

    if not handler.is_valid_url(url):
        await message.reply_text("❌ Не вдалося розпізнати посилання.")
        return

    status = await message.reply_text("⏳ Отримую інформацію про тайтл...")
    chat_id = message.chat.id

    # Resolve the RAW caption-derived title (Ukrainian/localized) into the
    # OFFICIAL Romaji title via the same mapper.json used by Normal/Batch mode.
    # This keeps the folder name identical to manually-forwarded downloads and
    # avoids using long localized titles as filesystem folder names directly.
    raw_title = await handler.get_series_title(url)

    if raw_title:
        mapped_title = mapper.get_mapping(raw_title)
        if mapped_title:
            title = mapped_title  # known title — zero friction
        else:
            search_query = quote(raw_title)
            anitube_url = f"https://anitube.in.ua/index.php?do=search&subaction=search&story={search_query}"
            google_url  = f"https://www.google.com/search?q={search_query}+anime"
            title = await ask_user_fresh(
                chat_id,
                f"⚠️ Невідомий тайтл: `{raw_title}`\n"
                f"🔎 [Anitube]({anitube_url}) | [Google]({google_url})\n\n"
                f"Введіть **офіційну Romaji назву** для збереження _(або `cancel`)_:"
            )
            if not title:
                try: await status.edit_text("❌ Скасовано.")
                except Exception: pass
                return
            mapper.add_mapping(raw_title, title)
    else:
        title = await ask_user_fresh(
            chat_id,
            "⚠️ Не вдалося розпізнати назву.\nВведіть назву тайтлу _(або `cancel`)_:"
        )
        if not title:
            try: await status.edit_text("❌ Скасовано.")
            except Exception: pass
            return

    # Prevent adding the same title twice (e.g. via two different channels'
    # links for the same anime) — check by the resolved official title.
    existing_series = anime_db.find_active_series_by_title(title, category="anime")
    if existing_series:
        try:
            await status.edit_text(
                f"⚠️ **{title}** вже відстежується (додано {existing_series['started_at'][:10]})."
            )
        except Exception:
            pass
        return

    display_title = raw_title or title
    series_id = anime_db.add_series(chat_id, title, url, category="anime", display_title=display_title)
    series_row = anime_db.get_series_by_id(series_id)

    # Pre-seed episodes already present on disk (e.g. from earlier manual
    # Normal/Batch downloads of this same anime) so the checker doesn't
    # re-download from scratch — a plain filename scan, no AI needed since
    # the naming convention ("... - SxxExx.ext") is fixed and unambiguous.
    existing_folder = os.path.join(settings.DOWNLOAD_PATH, sanitize_title(title))
    existing_episodes = scan_existing_episodes(existing_folder)
    if existing_episodes:
        anime_db.seed_downloaded_episodes(series_id, existing_episodes)
        logger.info(
            f"[{title}] знайдено {len(existing_episodes)} вже наявних серій на диску."
        )

    try:
        skip_note = (
            f"📁 Знайдено {len(existing_episodes)} вже наявних серій — пропускаю їх.\n"
            if existing_episodes else ""
        )
        await status.edit_text(
            f"✅ Додано до відстеження: **{title}**\n"
            f"{skip_note}"
            f"⏳ Перевіряю доступні серії..."
        )
    except Exception:
        pass

    asyncio.create_task(
        anime_checker.process_series(series_row, client, initial_status_msg=status)
    )


@app.on_message(auth_filter & filters.command("anime"))
async def anime_command(client: Client, message: Message):
    parts = message.text.strip().split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if arg == "help":
        await message.reply_text(ANIME_HELP)
        return

    if not arg or arg == "list":
        text, kb = _tracking_list_content("anime")
        await message.reply_text(text, reply_markup=kb)
        return

    await _track_anime_url(client, message, arg)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Bot starting...")

    async def main():
        anime_db.init_db()

        await app.start()

        userbot = build_userbot_client()
        if userbot:
            await userbot.start()
            logger.info("Userbot client started (Telegram-source anime tracking enabled)")

        await app.set_bot_commands([
            BotCommand("start", "Welcome & your User ID"),
            BotCommand("id", "Get your Telegram User ID"),
            BotCommand("help", "This message"),
            BotCommand("mode", "Switch operating mode"),
            BotCommand("anime", "Авто-відстеження аніме за посиланням"),
        ])
        logger.info("Bot commands registered")

        worker_task  = asyncio.create_task(queue_manager.worker())
        checker_task = asyncio.create_task(anime_checker.run_checker(app))
        logger.info("Queue worker started")
        logger.info("Anime checker started")

        await idle()

        worker_task.cancel()
        checker_task.cancel()
        if userbot:
            await userbot.stop()
        await app.stop()

    app.run(main())
