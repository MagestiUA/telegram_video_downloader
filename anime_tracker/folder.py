import logging

from pyrogram import raw
from pyrogram.errors import UserAlreadyParticipant

logger = logging.getLogger(__name__)

ANIME_FOLDER_TITLE = "Аніме Тайтли"

# Telegram's convention for "muted indefinitely" (max signed int32).
_MUTE_FOREVER = 2 ** 31 - 1


async def mute_chat(client, chat_id: int):
    """
    Mute a chat's notifications indefinitely. The userbot runs on the user's
    own personal account (not a separate bot number), so every auto-joined
    channel would otherwise push real notifications to their phone.
    """
    try:
        peer = await client.resolve_peer(chat_id)
        await client.invoke(
            raw.functions.account.UpdateNotifySettings(
                peer=raw.types.InputNotifyPeer(peer=peer),
                settings=raw.types.InputPeerNotifySettings(mute_until=_MUTE_FOREVER)
            )
        )
        logger.info(f"Muted notifications for chat {chat_id}.")
    except Exception as e:
        logger.warning(f"Could not mute chat {chat_id}: {e}")


async def add_chat_to_anime_folder(client, chat_id: int) -> bool:
    """
    File a chat into the shared "Аніме Тайтли" folder. Assumes the user has
    already created this folder manually in their Telegram client — we
    deliberately don't auto-create it (would mean guessing folder settings
    — included types, colors, etc. — the user never specified).
    """
    folders = await client.get_folders()
    if not folders:
        logger.warning(f"No folders exist — cannot file chat {chat_id} into '{ANIME_FOLDER_TITLE}'.")
        return False
    target = next((f for f in folders if f.title == ANIME_FOLDER_TITLE), None)
    if not target:
        logger.warning(f"Folder '{ANIME_FOLDER_TITLE}' not found — create it in Telegram first.")
        return False
    await target.include_chat(chat_id)
    logger.info(f"Filed chat {chat_id} into folder '{ANIME_FOLDER_TITLE}'.")
    return True


async def remove_chat_from_anime_folder(client, chat_id: int):
    """Best-effort: remove a chat from the folder (called before leaving it)."""
    try:
        folders = await client.get_folders()
        target = next((f for f in (folders or []) if f.title == ANIME_FOLDER_TITLE), None)
        if target:
            await target.remove_chat(chat_id)
    except Exception as e:
        logger.warning(f"Could not remove chat {chat_id} from folder: {e}")


async def join_and_file(client, invite_or_username: str) -> int | None:
    """
    Join a channel (invite link or public username) and file it into the
    anime folder. Returns the resolved chat's id, or None on failure.

    Safe to call repeatedly — the checker re-processes every tracked series
    on every 6-hour cycle (and again on a manual "Перевірити все"), so this
    path is hit repeatedly for the same channel forever, not just once.
    `join_chat` (messages.ImportChatInvite) is a heavily rate-limited method
    regardless of membership status, so we try the cheap `get_chat()` first —
    it resolves an invite link fine for a channel we're already in — and only
    fall back to an actual join_chat() call when that fails (i.e. we're
    genuinely not a member yet). This avoids hitting ImportChatInvite once
    per series on every single check cycle, which previously tripped
    FLOOD_WAIT once there were more than a couple of tracked private channels.
    """
    try:
        chat = await client.get_chat(invite_or_username)
        return chat.id
    except Exception:
        pass  # not resolvable without joining — fall through to join_chat

    try:
        chat = await client.join_chat(invite_or_username)
    except UserAlreadyParticipant:
        try:
            chat = await client.get_chat(invite_or_username)
        except Exception as e:
            logger.error(f"get_chat({invite_or_username}) failed after UserAlreadyParticipant: {e}")
            return None
    except Exception as e:
        logger.error(f"join_chat({invite_or_username}) failed: {e}")
        return None

    await mute_chat(client, chat.id)
    await add_chat_to_anime_folder(client, chat.id)
    return chat.id


async def unfile_and_leave(client, chat_id: int):
    """Remove from folder, then leave the chat. Best-effort, never raises."""
    await remove_chat_from_anime_folder(client, chat_id)
    try:
        await client.leave_chat(chat_id)
        logger.info(f"Left chat {chat_id}.")
    except Exception as e:
        logger.warning(f"leave_chat({chat_id}) failed: {e}")
