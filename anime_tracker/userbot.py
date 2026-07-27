import logging

from pyrogram import Client

from config.config import settings

logger = logging.getLogger(__name__)

_userbot_client: Client | None = None


def build_userbot_client() -> Client | None:
    """
    Construct (but don't start) the userbot client used to read channels/topics
    the bot account itself has no access to (Bot API has no chat-history endpoints).
    Returns None if USERBOT_SESSION_STRING is not configured.
    """
    global _userbot_client
    if not settings.USERBOT_SESSION_STRING:
        logger.warning(
            "USERBOT_SESSION_STRING not set — Telegram-source anime tracking disabled."
        )
        return None
    _userbot_client = Client(
        "userbot_reader",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        session_string=settings.USERBOT_SESSION_STRING,
        in_memory=True,
    )
    return _userbot_client


def get_userbot_client() -> Client | None:
    """Return the running userbot client, or None if not configured/started."""
    return _userbot_client
