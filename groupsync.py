import asyncio
import inspect
import json
import logging
import os
import sys
import time
from datetime import timedelta

from dotenv import load_dotenv
from telegram import ChatMember, ChatPermissions, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (ApplicationBuilder, ChatJoinRequestHandler, ChatMemberHandler, CommandHandler,
                          ContextTypes, filters)

# load values from .env if available
load_dotenv()


def require_chat_id(name: str) -> int:
    """Read a mandatory integer chat id from the environment or exit with a clear message."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        sys.exit(f"{name} must be set to an integer chat id")


BOT_TOKEN = os.getenv('BOT_TOKEN') or sys.exit("BOT_TOKEN must be set")
MAIN_GROUP_ID = require_chat_id('MAIN_GROUP_ID')
SECONDARY_GROUP_ID = require_chat_id('SECONDARY_GROUP_ID')
DECLINE_MESSAGE = os.getenv('DECLINE_MESSAGE') or "You are not allowed to join this group."
STATE_FILE = os.getenv('STATE_FILE') or "state.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=os.getenv('LOG_LEVEL') or "INFO"
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# all permission flags known to the installed library version, e.g. can_send_messages
PERMISSION_NAMES = [name for name in inspect.signature(ChatPermissions).parameters if name.startswith("can_")]

# restrictions the bot applied in the secondary group, keyed by user id (as string):
# {"target": what the bot asked for, "applied": what Telegram reported afterwards,
#  "manual": the restriction that was there before, or None}
state: dict[str, dict] = {}


class LookupFailed(Exception):
    """The membership of a user could not be determined."""


def load_state() -> None:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as file:
            state.update(json.load(file))


def save_state() -> None:
    # write to a temporary file first so a crash never leaves a broken state file
    with open(STATE_FILE + ".tmp", "w") as file:
        json.dump(state, file)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def is_member(member: ChatMember | None) -> bool:
    """Return True if the user is currently in the chat."""
    if member is None:
        return False
    if member.status == ChatMemberStatus.RESTRICTED:
        return member.is_member
    return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)


def is_expired(until: int) -> bool:
    """Return True if a restriction end date has passed or is too close for Telegram to accept (0 means never)."""
    return bool(until) and until < time.time() + 35


def get_restriction(member: ChatMember | None) -> dict | None:
    """Return the active restriction of a chat member as a plain dict, or None if there is none."""
    if member is None or member.status != ChatMemberStatus.RESTRICTED:
        return None
    # Telegram reports 0 (start of the epoch) for restrictions that never expire
    until = max(int(member.until_date.timestamp()), 0) if member.until_date else 0
    if is_expired(until):
        return None
    return {
        "permissions": {name: bool(getattr(member, name, True)) for name in PERMISSION_NAMES},
        "until": until,
    }


def merge_restrictions(first: dict, second: dict | None) -> dict:
    """Combine two restrictions into the stricter one."""
    if second is None:
        return first
    return {
        "permissions": {name: first["permissions"].get(name, True) and second["permissions"].get(name, True)
                        for name in PERMISSION_NAMES},
        "until": 0 if 0 in (first["until"], second["until"]) else max(first["until"], second["until"]),
    }


async def fetch_member(bot, chat_id: int, user_id: int) -> ChatMember | None:
    """
    Get the chat member with a few retries.

    Returns None if Telegram does not know the user in this chat.
    Raises LookupFailed if the membership could not be determined.
    """
    error = None
    for _ in range(3):
        try:
            return await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        except RetryAfter as e:
            error = e
            delay = e.retry_after.total_seconds() if isinstance(e.retry_after, timedelta) else e.retry_after
            if delay > 30:
                break
            await asyncio.sleep(delay + 1)
        except BadRequest as e:
            if any(text in e.message.lower() for text in ("user not found", "participant_id_invalid",
                                                           "member not found")):
                return None
            raise LookupFailed(e) from e
        except NetworkError as e:
            error = e
            await asyncio.sleep(2)
        except TelegramError as e:
            raise LookupFailed(e) from e
    raise LookupFailed(error)


async def set_restriction(bot, user_id: int, restriction: dict | None) -> None:
    """Apply a restriction in the secondary group, None lifts all restrictions."""
    permissions = restriction["permissions"] if restriction else dict.fromkeys(PERMISSION_NAMES, True)
    await bot.restrict_chat_member(
        chat_id=SECONDARY_GROUP_ID,
        user_id=user_id,
        permissions=ChatPermissions(**permissions),
        until_date=restriction["until"] if restriction and restriction["until"] else None,
        use_independent_chat_permissions=True,
    )


async def sync_user(bot, user_id: int) -> None:
    """
    Bring the secondary group in line with the main group for one user.

    Always works on the current state of both groups, so it is safe to call at any time and for outdated updates.
    Nobody is removed unless Telegram explicitly reports that the user is not in the main group.
    """
    key = str(user_id)
    try:
        main = await fetch_member(bot, MAIN_GROUP_ID, user_id)
        secondary = await fetch_member(bot, SECONDARY_GROUP_ID, user_id)
    except LookupFailed as e:
        logger.error(f"Could not check user {user_id}, nothing was changed: {e}")
        return

    if secondary is None or secondary.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        if state.pop(key, None):
            save_state()
        return
    if not is_member(secondary):
        # restricted but currently not in the group, Telegram keeps the restriction for a later rejoin
        return
    if secondary.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
        if not is_member(main):
            logger.warning(f"User {user_id} is not in the main group but cannot be removed: admin of secondary group")
        return

    if main is None:
        logger.warning(f"User {user_id} is unknown in the main group, not removed without an explicit status")
        return
    if not is_member(main):
        # unban on a member removes the user without banning them
        await bot.unban_chat_member(chat_id=SECONDARY_GROUP_ID, user_id=user_id)
        if state.pop(key, None):
            save_state()
        logger.info(f"User {user_id} is not in the main group ({main.status}), removed from secondary group")
        return

    wanted = get_restriction(main)
    current = get_restriction(secondary)
    record = state.get(key)
    # only a restriction that still looks exactly like the one the bot set is treated as the bot's own
    untouched = record is not None and current == record["applied"]

    if wanted is None:
        if record is None:
            return
        if untouched:
            # put back what was there before the bot mirrored the main group
            manual = record["manual"]
            await set_restriction(bot, user_id, None if manual is None or is_expired(manual["until"]) else manual)
            logger.info(f"Restriction lifted in main group, restored previous state of user {user_id}")
        else:
            logger.info(f"Restriction of user {user_id} was changed by an admin in secondary group, left untouched")
        del state[key]
        save_state()
        return

    # everything the bot did not set itself counts as set by an admin and is preserved
    manual = record["manual"] if untouched else current
    target = merge_restrictions(wanted, None if manual is None or is_expired(manual["until"]) else manual)
    if untouched and target == record["target"]:
        return
    if target != current:
        await set_restriction(bot, user_id, target)
        try:
            current = get_restriction(await fetch_member(bot, SECONDARY_GROUP_ID, user_id))
        except LookupFailed:
            current = target
        logger.info(f"Applied restriction from main group to user {user_id}")
    state[key] = {"target": target, "applied": current, "manual": manual}
    save_state()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /start command when the bot is contacted directly.

    Replies with a message to inform the user that the bot is not meant for direct interaction.
    """
    await update.effective_message.reply_text(
        f"Hello {update.effective_user.first_name}! "
        "This bot does nothing when directly contacted. You can close this chat."
    )


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle join requests to the secondary group.

    Members of the main group are approved, everyone else is declined and notified.
    If the membership cannot be determined the request stays pending.
    """
    join_request = update.chat_join_request
    user = join_request.from_user
    name = f"{user.full_name} ({user.username}, {user.id})"

    try:
        member = await fetch_member(context.bot, MAIN_GROUP_ID, user.id)
    except LookupFailed as e:
        logger.error(f"Could not check {name}, join request stays pending: {e}")
        return
    logger.debug(f"Join request of {name}, status in main group: {member.status if member else None}")

    try:
        if is_member(member):
            await join_request.approve()
        else:
            await join_request.decline()
    except BadRequest as e:
        # usually the request was already handled by an admin or the user is already in the group
        logger.warning(f"Join request of {name} could not be processed: {e}")
        return

    if is_member(member):
        logger.info(f"{name} was approved")
        await sync_user(context.bot, user.id)
        return
    logger.info(f"{name} was declined")
    try:
        await context.bot.send_message(chat_id=join_request.user_chat_id, text=DECLINE_MESSAGE)
    except (BadRequest, Forbidden) as e:
        logger.info(f"Could not send decline message to {name}: {e}")


async def member_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every membership or restriction change in the main or the secondary group."""
    user = update.chat_member.new_chat_member.user
    if not user.is_bot:
        await sync_user(context.bot, user.id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing an update", exc_info=context.error)


def main():
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(ChatJoinRequestHandler(join_request_handler, chat_id=SECONDARY_GROUP_ID))
    app.add_handler(ChatMemberHandler(member_update_handler, ChatMemberHandler.CHAT_MEMBER,
                                      chat_id=[MAIN_GROUP_ID, SECONDARY_GROUP_ID]))
    app.add_error_handler(error_handler)
    # chat_member updates are only delivered when explicitly requested
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CHAT_MEMBER, Update.CHAT_JOIN_REQUEST])


if __name__ == "__main__":
    main()
