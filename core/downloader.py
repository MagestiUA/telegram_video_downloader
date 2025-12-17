import os
import logging
import time
import asyncio
from pyrogram import Client
from pyrogram.types import Message
from config.config import settings
from core.renamer import get_target_path, generate_filename

logger = logging.getLogger(__name__)

WORKERS = 4  # Number of parallel chunks
CHUNK_SIZE = 1024 * 1024  # 1MB buffer for writing

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
            f"🚀 Downloading (Smart Multi-Thread)...\n"
            f"Progress: {percentage:.1f}%\n"
            f"Speed: {speed/1024/1024:.2f} MB/s\n"
            f"Elapsed: {elapsed_time}s"
        )
    except Exception:
        pass 

# ... imports

# Pyrogram uses 1MB chunks for stream_media offsets usually
PYRO_CHUNK_SIZE = 1024 * 1024

async def download_chunk(client: Client, message: Message, start_byte: int, length: int, file_handle, lock: asyncio.Lock, progress_callback):
    try:
        # stream_media takes offset in CHUNKS (1MB units), not bytes.
        # limit is also in CHUNKS.
        
        chunk_offset = start_byte // PYRO_CHUNK_SIZE
        # We calculate limit based on how many 1MB chunks we need to cover 'length'
        # e.g. length=1.5MB -> 2 chunks
        chunk_limit = (length + PYRO_CHUNK_SIZE - 1) // PYRO_CHUNK_SIZE
        
        current_pos = start_byte
        remaining = length
        
        # We must filter the stream to only write the bytes we belong to.
        # Because stream_media might give us a full 1MB chunk overlapping into the next worker's area.
        
        # Internal offset within the first chunk (should be 0 if we align correctly)
        internal_offset = start_byte % PYRO_CHUNK_SIZE
        
        async for chunk in client.stream_media(message, offset=chunk_offset, limit=chunk_limit):
            if not chunk:
                break
            
            # Slice chunk if needed (for start or end)
            # But simpler: just write everything to file at correct pos?
            # Access to file is random. 
            # The chunk from stream_media corresponds to `(chunk_offset + i) * 1MB`.
            # We just write it.
            # Wait, if multiple workers overlap, we might overwrite?
            # If we align starts to 1MB, then:
            # Worker 1: 0 - 25MB
            # Worker 2: 25MB - 50MB
            # ALL starts are aligned.
            # So `stream_media` yields exactly the blocks we need.
            # The only issue is the LAST block of a worker might exceed `length`.
            
            chunk_len = len(chunk)
            
            # If internal_offset > 0 (shouldn't happen with alignment), skip bytes?
            # We assume alignment.
            
            # Check if this chunk goes beyond our assigned length
            bytes_to_write = chunk
            if remaining < chunk_len:
                bytes_to_write = chunk[:remaining]
            
            write_len = len(bytes_to_write)
            
            async with lock:
                file_handle.seek(current_pos)
                file_handle.write(bytes_to_write)
            
            current_pos += write_len
            remaining -= write_len
            
            if progress_callback:
                progress_callback(write_len)
                
            if remaining <= 0:
                break
                
    except Exception as e:
        logger.error(f"Chunk download error: {e}")
        raise e

async def download_video(client: Client, message: Message, metadata: dict, status_msg: Message = None):
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
    
    logger.info(f"Starting Smart Download: {target_path} | Size: {file_size/1024/1024:.2f} MB")
    
    start_time = time.time()
    
    try:
        # Pre-allocate file
        with open(target_path, "wb") as f:
            f.seek(file_size - 1)
            f.write(b"\0")
            
        progress_lock = asyncio.Lock()
        file_lock = asyncio.Lock()
        downloaded_bytes = 0
        
        # Progress callback (thread_safe via lock logic implicitly in main loop)
        def update_progress(chunk_size):
            nonlocal downloaded_bytes
            downloaded_bytes += chunk_size
        
        # Monitor Task
        async def monitor():
            while downloaded_bytes < file_size:
                await progress_bar(downloaded_bytes, file_size, status_msg, start_time)
                await asyncio.sleep(2)
            await progress_bar(file_size, file_size, status_msg, start_time) 

        monitor_task = asyncio.create_task(monitor())

        # Split logic - ALIGN TO 1MB (PYRO_CHUNK_SIZE)
        # Each worker should start at a multiple of 1MB.
        
        # Rough chunk size per worker
        raw_chunk_size = file_size // WORKERS
        
        # Align to 1MB
        aligned_chunk_size = (raw_chunk_size // PYRO_CHUNK_SIZE) * PYRO_CHUNK_SIZE
        if aligned_chunk_size == 0:
            aligned_chunk_size = PYRO_CHUNK_SIZE # Minimum 1MB
            
        tasks = []
        
        with open(target_path, "r+b") as f:
            for i in range(WORKERS):
                start = i * aligned_chunk_size
                
                # If start is beyond file (small file), skip
                if start >= file_size:
                    break
                    
                # End is next start or file_size
                if i == WORKERS - 1:
                    length = file_size - start
                else:
                    # Logic: 
                    # Worker 0: 0 -> aligned_chunk
                    # Worker 1: aligned -> 2*aligned
                    # ...
                    length = aligned_chunk_size
                    
                    # Correction for last worker logic in loop
                    # If we set lengths strictly, we might leave a gap at the end if file is large?
                    # No, i=3 (last) takes everything.
                    # But wait, i=0,1,2 take `aligned_chunk_size`.
                    # i=3 starts at 3*aligned.
                    # Does 3*aligned + length cover whole file?
                    # 4 * aligned might be < file_size.
                    # So last worker takes `file_size - start`.
                    pass

                tasks.append(
                    download_chunk(client, message, start, length, f, file_lock, update_progress)
                )
            
            await asyncio.gather(*tasks)

        monitor_task.cancel()
        await progress_bar(file_size, file_size, status_msg, start_time)
        
        if status_msg:
            # Display Path Replacement (Docker -> Windows)
            display_path = target_path
            internal_root = settings.DOWNLOAD_PATH # e.g. /data/downloads
            windows_root = r"Z:\Video\Anime"
            
            if display_path.startswith(internal_root):
                display_path = display_path.replace(internal_root, windows_root, 1)
            
            # Normalize slashes for Windows look
            display_path = display_path.replace("/", "\\")
            
            await status_msg.edit_text(f"✅ Download Complete!\nSaved to: `{display_path}`")
            
        logger.info(f"Download completed: {target_path}")
        return target_path

    except Exception as e:
        logger.error(f"Download failed: {e}")
        if status_msg:
            await status_msg.edit_text(f"❌ Error during download: {e}")
        if os.path.exists(target_path):
            os.remove(target_path)
        return None
