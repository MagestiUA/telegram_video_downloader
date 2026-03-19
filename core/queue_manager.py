import asyncio
import logging
from pyrogram import Client
from pyrogram.types import Message
from core.downloader import download_video

logger = logging.getLogger(__name__)

class QueueManager:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def add_task(self, client: Client, message: Message, metadata: dict, status_msg: Message = None, reply_markup=None):
        """
        Adds a download task to the queue.
        """
        q_size = self.queue.qsize()
        await self.queue.put((client, message, metadata, status_msg, reply_markup))
        
        logger.info(f"Task added to queue. Current queue size: {q_size + 1}")
        
        if status_msg:
            # If there are already items in the queue (or currently processing), notify user
            # Even if queue is empty, the worker might be busy, but qsize doesn't count the active one usually?
            # actually qsize() is number of items IN queue, not including the one being processed.
            # So if qsize > 0, there is definitely a wait.
            # If qsize == 0, it might be picked up immediately OR wait if worker is busy.
            # Let's just say "Queued" if we can't be sure, but "Position" implies waiting.
            if q_size > 0:
                await status_msg.edit_text(f"⏳ Додано в чергу... Перед вами відео: {q_size}")
            else:
                await status_msg.edit_text("⏳ Додається в обробку...")

    async def worker(self):
        """
        Background worker that processes the queue sequentially.
        """
        logger.info("Download Queue Worker started.")
        while True:
            try:
                # Wait for a task
                client, message, metadata, status_msg, reply_markup = await self.queue.get()

                try:
                    if status_msg:
                        await status_msg.edit_text("🔄 Починаю завантаження...")

                    # Execute the download
                    await download_video(client, message, metadata, status_msg)

                    # After download: if queue is now empty and we have a keyboard → notify once
                    if self.queue.empty() and reply_markup is not None:
                        try:
                            await client.send_message(
                                message.chat.id,
                                "✅ Всі завантаження завершено!",
                                reply_markup=reply_markup
                            )
                        except Exception as notify_err:
                            logger.warning(f"Failed to send queue-done notification: {notify_err}")

                except Exception as e:
                    logger.error(f"Worker processing error: {e}")
                    if status_msg:
                        try:
                            await status_msg.edit_text(f"❌ Помилка під час обробки в черзі: {e}")
                        except:
                            pass
                finally:
                    # Mark task as done
                    self.queue.task_done()
                    
            except asyncio.CancelledError:
                logger.info("Worker cancelled.")
                break
            except Exception as e:
                logger.error(f"Critical Worker Error: {e}")
                await asyncio.sleep(5)  # Prevent tight loop on crash

# Global instance
queue_manager = QueueManager()
