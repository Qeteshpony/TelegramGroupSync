import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (ApplicationBuilder, ChatJoinRequestHandler, ContextTypes, CommandHandler,
                          MessageHandler, filters)
from telegram.error import BadRequest

# load values from .env if available
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
MAIN_GROUP_ID = int(os.getenv('MAIN_GROUP_ID'))
SECONDARY_GROUP_ID = int(os.getenv('SECONDARY_GROUP_ID'))
LOG_LEVEL = os.getenv('LOG_LEVEL')
DECLINE_MESSAGE = os.getenv('DECLINE_MESSAGE')

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=LOG_LEVEL
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /start command when the bot is contacted directly.

    Replies with a message to inform the user that the bot is not meant for direct interaction.
    """
    user = update.effective_user
    await update.message.reply_text(
        f"Hello {user.first_name}! This bot does nothing when directly contacted. You can close this chat."
    )

async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle join requests to the secondary group.

    If the user is a member of the main group, approves the request.
    Otherwise, declines the request and notifies the user.
    """
    join_request = update.chat_join_request
    user = join_request.from_user
    group_id = join_request.chat.id
    user_chat_id = join_request.user_chat_id
    logger.debug(f"Join Request Handler group_id: {group_id}, user_id: {user.id}")

    try:
        # check if user is a member of main chat
        member = await context.bot.get_chat_member(chat_id=MAIN_GROUP_ID, user_id=user.id)
        logger.debug(f"member.status: {member.status}")
        if member.status in ["member", "administrator", "creator"]:
            # user is a member - approve the request
            await join_request.approve()
            logger.info(f"{user.full_name} ({user.username}) was approved")
        else:
            await context.bot.send_message(chat_id=user_chat_id, text=DECLINE_MESSAGE)
            await join_request.decline()
            logger.info(f"{user.full_name} ({user.username}) was declined")
    except BadRequest as e:
        await context.bot.send_message(chat_id=user_chat_id, text=DECLINE_MESSAGE)
        await join_request.decline()
        logger.info(f"{user.full_name} ({user.username}) was declined because of Error: {e}")

async def user_left_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the event when a user leaves the main group.

    If the user is also in the secondary group, they are removed from it.
    """
    user = update.message.left_chat_member
    group_id = update.message.chat.id
    if group_id == MAIN_GROUP_ID:
        try:
            member = await context.bot.get_chat_member(chat_id=SECONDARY_GROUP_ID, user_id=user.id)
            logger.debug(f"secondary group member.status: {member.status}")
            if member.status in ["member"]:
                await context.bot.unban_chat_member(chat_id=SECONDARY_GROUP_ID, user_id=user.id)
        except BadRequest as e:
            logger.error(e)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatJoinRequestHandler(join_request_handler, chat_id=SECONDARY_GROUP_ID))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, user_left_group))
    app.run_polling()


if __name__ == "__main__":
    main()
