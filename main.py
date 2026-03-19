import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pyrogram import Client, idle, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from config.config import settings
from analyzer.mapper import mapper
from analyzer.ai_cleaner import extract_metadata
from core.queue_manager import queue_manager
from urllib.parse import quote

# Setup logging
# Створюємо formatter для логів
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Налаштування для консолі
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

# Налаштування для файлу з ротацією (макс 10 МБ, 5 резервних файлів)
file_handler = RotatingFileHandler(
    'app.log',
    maxBytes=10*1024*1024,  # 10 MB
    backupCount=5,
    encoding='utf-8'
)
file_handler.setFormatter(log_formatter)

# Налаштування root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

# Ensure sessions directory exists
os.makedirs("sessions", exist_ok=True)

# Global dictionary to manage waiting states: chat_id -> asyncio.Future
waiting_for_user_input = {}

# --- Initialize Client (Global) ---
if settings.SESSION_STRING:
    app = Client(
        "tg_downloader",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        session_string=settings.SESSION_STRING,
        in_memory=True
    )
elif settings.BOT_TOKEN:
    app = Client(
        "sessions/tg_downloader",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        bot_token=settings.BOT_TOKEN
    )
else:
    # Userbot mode (Legacy or local testing without bot)
    logger.warning("No BOT_TOKEN found. Running as Userbot!")
    app = Client(
        "sessions/tg_downloader",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH
    )

# --- Access Control Filter ---
async def is_authorized(_, __, message: Message):
    allowed = settings.allowed_users_set
    if not allowed:
        return True # Configure config.py/env to restrict
    
    # Allow if from allowed user
    if message.from_user and message.from_user.id in allowed:
        return True
        
    return False

auth_filter = filters.create(is_authorized)


# --- Handlers ---

# 0. Global Logger (Group -1 runs first)
@app.on_message(group=-1)
async def log_all_messages(client, message):
    user = message.from_user
    user_id = user.id if user else "Unknown"
    name = user.first_name if user else "Unknown"
    text_preview = message.text or message.caption or "Media/Other"
    
    # Debug log to see everything
    logger.info(f"📨 MSG | User: {name} ({user_id}) | Chat: {message.chat.id} | Content: {text_preview[:50]}")


# 1. Public Commands
@app.on_message(filters.command("start"))
async def start_handler(client, message):
    logger.info(f"Start command from {message.from_user.id}")
    await message.reply_text(
        f"👋 Вітаю!\n\n"
        f"Я приватний бот-завантажувач.\n"
        f"Ваш User ID: `{message.from_user.id}`\n\n"
        f"Якщо ви власник, додайте цей ID в `ALLOWED_USERS`."
    )

@app.on_message(filters.command("id"))
async def id_handler(client, message):
    await message.reply_text(f"Your User ID is: `{message.from_user.id}`")


# 2. Protected Handlers
@app.on_message(auth_filter & filters.text & ~filters.command(["start", "help", "id"]))
async def text_handler(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id in waiting_for_user_input:
        future = waiting_for_user_input[chat_id]
        if not future.done():
            future.set_result(message.text)
        return
    
    # Ignore other text messages
    pass

async def ask_user(chat_id: int, prompt: str, status_msg: Message, timeout=300):
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

@app.on_message(auth_filter & (filters.video | filters.document))
async def video_handler(client: Client, message: Message):
    # Filter for videos
    media = message.video or message.document
    if not media:
        return
    if message.document and "video" not in (message.document.mime_type or ""):
        return

    logger.info(f"New video detected from: {message.chat.title or message.chat.first_name}")
    
    text_to_analyze = message.caption or ""
    filename = media.file_name or "video.mp4"
    
    if len(text_to_analyze) < 5:
        text_to_analyze = filename

    status_msg = None
    try:
        status_msg = await message.reply_text(f"🧐 Processing: `{text_to_analyze[:100]}...`")
    except Exception as e:
        logger.warning(f"Could not reply: {e}")

    # Step A: AI Analysis
    ai_data = await extract_metadata(text_to_analyze)
    
    if not ai_data or not ai_data.get('title'):
        if not status_msg:
            return

        try:
            # 1️⃣ Title
            title = await ask_user(
                message.chat.id,
                "⚠️ AI failed.\n\nPlease reply with the **Official Romaji Title** (or `cancel`):",
                status_msg
            )
            if not title:
                await status_msg.edit_text("❌ Cancelled by user.")
                return

            # 2️⃣ Episode
            episode = await ask_user(
                message.chat.id,
                "📺 Enter **Episode number**:",
                status_msg
            )
            if not episode or not episode.isdigit():
                await status_msg.edit_text("❌ Invalid episode.")
                return

            # 3️⃣ Season
            season = await ask_user(
                message.chat.id,
                "📀 Enter **Season number**:",
                status_msg
            )
            if not season or not season.isdigit():
                await status_msg.edit_text("❌ Invalid season.")
                return

            ai_data = {
                "title": title.strip(),
                "episode": int(episode),
                "season": int(season)
            }

            await status_msg.edit_text(
                f"✅ Manual data set:\n"
                f"**{ai_data['title']}**\n"
                f"S{ai_data['season']}E{ai_data['episode']}"
            )

        except FloodWait as e:
            logger.warning(f"FloodWait: wait {e.value}s")
            return

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
        # Step C: Ask User
        if status_msg:
            # Generate Search Links
            search_query = quote(ai_data['title'])
            anitube_url = f"https://anitube.in.ua/index.php?do=search&subaction=search&story={search_query}"
            google_url = f"https://www.google.com/search?q={search_query}+anime"
            
            try:
                await status_msg.edit_text(
                    f"⚠️ Unknown Title: `{ai_data['title']}`.\n"
                    f"🔎 [Anitube]({anitube_url}) | [Google]({google_url})\n\n"
                    f"Please reply with the **Official Romaji Title** to save it (or 'cancel'):",
                    disable_web_page_preview=True
                )
            except FloodWait as e:
                logger.warning(f"FloodWait: need to wait {e.value}s. Skipping status update.")
                # Продовжуємо виконання, чекаємо на відповідь користувача
            except Exception as e:
                logger.debug(f"Failed to update status: {e}")
            
            # Wait for response
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            waiting_for_user_input[message.chat.id] = future
            
            try:
                user_reply = await asyncio.wait_for(future, timeout=300) # 5 min
                
                if user_reply.lower() == "cancel":
                    try:
                        await status_msg.edit_text("❌ Cancelled by user.")
                    except FloodWait as e:
                        logger.warning(f"FloodWait: need to wait {e.value}s")
                    except Exception as e:
                        logger.debug(f"Failed to update status: {e}")
                    del waiting_for_user_input[message.chat.id]
                    return
                    
                # Save
                mapper.add_mapping(ai_data['title'], user_reply)
                final_title = user_reply
                try:
                    await status_msg.edit_text(f"✅ Saved & Using: `{final_title}`")
                except FloodWait as e:
                    logger.warning(f"FloodWait: need to wait {e.value}s")
                except Exception as e:
                    logger.debug(f"Failed to update status: {e}")
                
            except asyncio.TimeoutError:
                try:
                    await status_msg.edit_text("❌ Timeout waiting for input.")
                except FloodWait as e:
                    logger.warning(f"FloodWait: need to wait {e.value}s")
                except Exception as e:
                    logger.debug(f"Failed to update status: {e}")
                return
            except Exception as e:
                logger.error(f"Error waiting for input: {e}")
                try:
                    await status_msg.edit_text(f"❌ Error: {e}")
                except FloodWait as fw:
                    logger.warning(f"FloodWait: need to wait {fw.value}s")
                except Exception as edit_err:
                    logger.debug(f"Failed to update status: {edit_err}")
                return
            finally:
                waiting_for_user_input.pop(message.chat.id, None)
        else:
            # Silent fallback
            logger.warning("Cannot ask user (no permission). Using raw AI title.")
            final_title = ai_data['title']

    # Final Metadata
    safe_canonical_name = "".join([c for c in final_title if c.isalnum() or c in " .()_-"]).strip()

    metadata = {
        "canonical_name": safe_canonical_name,
        "season": ai_data.get('season', 1),
        "episode": ai_data.get('episode')
    }
    
    # 3. Add to Queue
    await queue_manager.add_task(client, message, metadata, status_msg=status_msg)


if __name__ == "__main__":
    logger.info("Bot starting...")
    
    async def main():
        await app.start()
        
        # Start Queue Worker
        worker_task = asyncio.create_task(queue_manager.worker())
        logger.info("Queue worker started")
        
        await idle()
        
        # Cleanup
        worker_task.cancel()
        await app.stop()

    app.run(main())
