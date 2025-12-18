import os
import logging
import time
from pyrogram import Client
from pyrogram.types import Message
from config.config import settings
from core.renamer import get_target_path, generate_filename

logger = logging.getLogger(__name__)

async def progress_bar(current, total, status_msg: Message, start_time):
    """
    Updates the progress bar in the Telegram message log.
    Optimization: Edit message only every 3 seconds or on completion.
    """
    if not status_msg:
        return

    now = time.time()
    if (now - start_time) < 3 and current != total:
        return

    percentage = current * 100 / total
    speed = current / (now - start_time) if (now - start_time) > 0 else 0
    elapsed_time = round(now - start_time)
    
    try:
        await status_msg.edit_text(
            f"🚀 Downloading...\n"
            f"Progress: {percentage:.1f}%\n"
            f"Speed: {speed/1024/1024:.2f} MB/s\n"
            f"Elapsed: {elapsed_time}s"
        )
    except Exception:
        pass 

async def download_video(client: Client, message: Message, metadata: dict, status_msg: Message = None):
    """
    Downloads video using Pyrogram's built-in download_media method.
    This is more stable than the custom multi-threaded downloader.
    """
    canonical_name = metadata['canonical_name']
    season = metadata.get('season') 
    episode = metadata.get('episode')
    
    media = message.video or message.document
    if not media:
        return None
        
    file_size = media.file_size
    original_file_name = media.file_name or "video.mp4"
    _, ext = os.path.splitext(original_file_name)
    if not ext:
        ext = ".mp4"
        
    new_filename = generate_filename(canonical_name, season, episode, ext)
    target_path = get_target_path(canonical_name, new_filename)
    
    logger.info(f"Starting download: {target_path} | Size: {file_size/1024/1024:.2f} MB")
    
    start_time = time.time()
    
    async def progress(current, total):
        """Progress callback для download_media"""
        await progress_bar(current, total, status_msg, start_time)
    
    try:
        # Використовуємо вбудований Pyrogram downloader
        downloaded_path = await client.download_media(
            message,
            file_name=target_path,
            progress=progress
        )
        
        # Final progress update
        await progress_bar(file_size, file_size, status_msg, start_time)
        
        if status_msg:
            # Display Path Replacement (Docker -> Windows)
            display_path = downloaded_path
            internal_root = settings.DOWNLOAD_PATH # e.g. /data/downloads
            windows_root = r"Z:\Video\Anime"
            
            if display_path.startswith(internal_root):
                display_path = display_path.replace(internal_root, windows_root, 1)
            
            # Normalize slashes for Windows look
            display_path = display_path.replace("/", "\\")
            
            await status_msg.edit_text(f"✅ Download Complete!\nSaved to: `{display_path}`")
            
        logger.info(f"Download completed: {downloaded_path}")
        return downloaded_path
        
    except Exception as e:
        logger.error(f"Download failed: {e}")
        if status_msg:
            await status_msg.edit_text(f"❌ Error during download: {e}")
        if os.path.exists(target_path):
            os.remove(target_path)
        return None
