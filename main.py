import asyncio
import logging
import os
from enum import Enum
from logging.handlers import RotatingFileHandler
from pyrogram import Client, idle, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
from pyrogram.errors import FloodWait
from config.config import settings
from analyzer.mapper import mapper
from analyzer.ai_cleaner import extract_metadata, extract_episode
from core.queue_manager import queue_manager
from urllib.parse import quote
from dorama import db as dorama_db, checker as dorama_checker
from dorama.sites import get_handler as get_site_handler, supported_domains

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

# chat_id -> asyncio.Future: waiting for dorama confirm button press ("ok"|"rename"|"cancel")
waiting_for_dorama_confirm: dict[int, asyncio.Future] = {}

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
    if mode == BotMode.BATCH:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Normal",       callback_data="mode_normal"),
            InlineKeyboardButton("✅ Batch",         callback_data="mode_batch"),
            InlineKeyboardButton("⏹ End Session",   callback_data="mode_end"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Normal", callback_data="mode_normal"),
        InlineKeyboardButton("📦 Batch",  callback_data="mode_batch"),
    ]])


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
    text_preview = message.text or message.caption or "Media/Other"
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
        "• /dorama — Відстеження дорам / серіалів\n\n"
        "📥 **Normal Mode** _(default)_\n"
        "AI analyzes each video independently: extracts title, season & episode.\n"
        "Unknown titles → you confirm the official name → saved to DB.\n\n"
        "📦 **Batch Mode** _(30 min inactivity session)_\n"
        "Best for series where AI keeps misidentifying episodes.\n"
        "• Set title & season once for the whole session\n"
        "• Each video: AI extracts only the episode number (with context)\n"
        "• Isolated from DB — no reads or writes to mappings.json\n"
        "• Ends on 30 min inactivity or via 'End Session' button\n\n"
        "🎬 **Dorama Mode**\n"
        "Авто-завантаження нових епізодів кожні 6 годин.\n"
        "Детальніше: `/dorama help`\n\n"
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


# 3. Text input router (passes replies to ask_user futures)
@app.on_message(auth_filter & filters.text & ~filters.command(["start", "help", "id", "mode", "dorama"]))
async def text_handler(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id in waiting_for_user_input:
        future = waiting_for_user_input[chat_id]
        if not future.done():
            future.set_result(message.text)
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

        safe_title = "".join(c for c in title if c.isalnum() or c in " .()_-").strip()
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


# --- Normal Mode Handler ---

@app.on_message(auth_filter & (filters.video | filters.document))
async def video_handler(client: Client, message: Message):
    media = message.video or message.document
    if not media:
        return
    if message.document and "video" not in (message.document.mime_type or ""):
        return

    logger.info(f"New video from: {message.chat.title or message.chat.first_name}")

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
    safe_canonical_name = "".join(c for c in final_title if c.isalnum() or c in " .()_-").strip()
    metadata = {
        "canonical_name": safe_canonical_name,
        "season":         ai_data.get('season', 1),
        "episode":        ai_data.get('episode'),
    }
    await queue_manager.add_task(client, message, metadata, status_msg=status_msg)


# ── DORAMA MODE ──────────────────────────────────────────────────────────────

def _dorama_list_content(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build message text + keyboard for the dorama tracking list."""
    series_list = dorama_db.get_series_by_chat(chat_id)
    if not series_list:
        return (
            "📋 **Дорами / відстеження**\n\n"
            "Немає активних серіалів.\n\n"
            "Щоб додати:\n`/dorama https://uafix.net/serials/.../season-01-episode-01/`",
            None
        )
    text = "📋 **Дорами / відстеження:**\n\n"
    buttons = []
    for s in series_list:
        started = s["started_at"][:10]
        text += (
            f"• **{s['title']}**\n"
            f"  S{s['last_season']:02d}E{s['last_episode']:02d} | додано {started}\n\n"
        )
        buttons.append([
            InlineKeyboardButton(f"⏹ {s['title']}", callback_data=f"dorama_stop_{s['id']}")
        ])
    return text, InlineKeyboardMarkup(buttons)


DORAMA_HELP = (
    "🎬 **Доrama — автоматичне відстеження серіалів**\n\n"
    "Бот перевіряє наявність нових епізодів кожні **6 годин** "
    "і автоматично завантажує їх у твою колекцію.\n\n"
    "**Як додати серіал:**\n"
    "Скопіюй посилання з uafix.net — підходить і сторінка серіалу, "
    "і сторінка першого епізоду:\n"
    "`/dorama https://uafix.net/serials/назва/`\n"
    "`/dorama https://uafix.net/serials/назва/season-01-episode-01/`\n\n"
    "**Підтримувані сайти:** `uafix.net`\n\n"
    "**Про завантаження:**\n"
    "• Завантажується тільки **дубляж / багатоголосий** (zetvideo.net)\n"
    "• Якщо доступні лише субтитри — бот чекає появи дубляжу\n"
    "• Завантажуються всі доступні серії й сезони\n"
    "• Серіал відстежується до **6 місяців** від дати додавання\n"
    "• При успішному завантаженні — сповіщення отримують усі користувачі бота\n\n"
    "**Команди:**\n"
    "• `/dorama {url}` — додати серіал\n"
    "• `/dorama list` — список активних серіалів з кнопками зупинки\n"
    "• `/dorama help` — ця довідка"
)


@app.on_message(auth_filter & filters.command("dorama"))
async def dorama_command(client: Client, message: Message):
    parts = message.text.strip().split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    # /dorama help
    if arg == "help":
        await message.reply_text(DORAMA_HELP)
        return

    # /dorama  or  /dorama list  → show list
    if not arg or arg == "list":
        text, kb = _dorama_list_content(message.chat.id)
        await message.reply_text(text, reply_markup=kb)
        return

    url = arg
    handler = get_site_handler(url)
    if not handler:
        domains = ", ".join(supported_domains())
        await message.reply_text(
            f"❌ Сайт не підтримується.\nЗараз доступно: `{domains}`"
        )
        return

    # Validate URL
    if not handler.is_valid_url(url):
        await message.reply_text(
            "❌ Не вдалося розпізнати URL.\n"
            "Підтримуються посилання на серіал або на перший епізод uafix.net."
        )
        return

    status = await message.reply_text("⏳ Отримую інформацію про серіал...")

    # Fetch title from the page
    auto_title = await handler.get_series_title(url)
    chat_id = message.chat.id

    if auto_title:
        # Show confirm / rename / cancel buttons
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiting_for_dorama_confirm[chat_id] = future
        try:
            await status.edit_text(
                f"📺 Знайдено серіал:\n**{auto_title}**",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Підтвердити",  callback_data="dorama_confirm_ok"),
                    InlineKeyboardButton("✏️ Ввести назву", callback_data="dorama_confirm_rename"),
                    InlineKeyboardButton("❌ Відмінити",    callback_data="dorama_confirm_cancel"),
                ]])
            )
            choice = await asyncio.wait_for(future, timeout=120)
        except asyncio.TimeoutError:
            choice = "cancel"
        finally:
            waiting_for_dorama_confirm.pop(chat_id, None)
    else:
        choice = "rename"

    if choice == "cancel":
        try: await status.edit_text("❌ Скасовано.")
        except Exception: pass
        return

    if choice == "ok":
        title = auto_title
    else:  # rename
        title = await ask_user_fresh(chat_id, "✏️ Введіть назву серіалу _(або `cancel`)_:")
        if not title:
            try: await status.edit_text("❌ Скасовано.")
            except Exception: pass
            return

    # Save to DB
    series_id = dorama_db.add_series(message.chat.id, title, url)
    series_row = dorama_db.get_series_by_id(series_id)

    try:
        await status.edit_text(
            f"✅ Додано до відстеження: **{title}**\n"
            f"⏳ Перевіряю доступні епізоди..."
        )
    except Exception:
        pass

    # Trigger immediate download of available episodes
    asyncio.create_task(dorama_checker.process_series(series_row, client))


@app.on_callback_query(auth_filter & filters.regex("^dorama_confirm_"))
async def dorama_confirm_callback(client: Client, query: CallbackQuery):
    chat_id = query.message.chat.id
    action = query.data.replace("dorama_confirm_", "")  # "ok" | "rename" | "cancel"

    future = waiting_for_dorama_confirm.get(chat_id)
    if future and not future.done():
        future.set_result(action)

    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@app.on_callback_query(auth_filter & filters.regex("^dorama_stop_"))
async def dorama_stop_callback(client: Client, query: CallbackQuery):
    series_id = int(query.data.split("_")[-1])
    series = dorama_db.get_series_by_id(series_id)
    title = series["title"] if series else f"#{series_id}"

    dorama_db.stop_series(series_id)
    await query.answer(f"⏹ Зупинено: {title}")

    # Refresh the list in-place
    text, kb = _dorama_list_content(query.message.chat.id)
    try:
        await query.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Bot starting...")

    async def main():
        dorama_db.init_db()

        await app.start()

        await app.set_bot_commands([
            BotCommand("start", "Welcome & your User ID"),
            BotCommand("id", "Get your Telegram User ID"),
            BotCommand("help", "This message"),
            BotCommand("mode", "Switch operating mode"),
            BotCommand("dorama", "Відстеження дорам / серіалів"),
        ])
        logger.info("Bot commands registered")

        worker_task  = asyncio.create_task(queue_manager.worker())
        checker_task = asyncio.create_task(dorama_checker.run_checker(app))
        logger.info("Queue worker started")
        logger.info("Dorama checker started")

        await idle()

        worker_task.cancel()
        checker_task.cancel()
        await app.stop()

    app.run(main())
