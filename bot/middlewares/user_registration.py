"""User auto-registration middleware for Telegram bot."""

import logging
from telebot import TeleBot
from telebot.types import Message

from database import get_db_session
from database.models import User
from database.activity_log import log_activity

logger = logging.getLogger(__name__)


def register_user_middleware(bot: TeleBot) -> None:
    """
    Register middleware to auto-register users in database.

    This middleware ensures every user is in the database before
    any command is processed.

    Args:
        bot: TeleBot instance
    """

    @bot.middleware_handler(update_types=['message'])
    def handle_user_registration(bot_instance, message: Message):
        """
        Check if user exists in database, create if not.

        Args:
            bot_instance: TeleBot instance
            message: Telegram message object
        """
        if not message.from_user:
            return

        telegram_id = message.from_user.id
        username = message.from_user.username

        try:
            with get_db_session() as db:
                # Check if user exists
                user = db.query(User).filter(User.telegram_id == telegram_id).first()

                if not user:
                    # Create new user
                    user = User(
                        telegram_id=telegram_id,
                        username=username
                    )
                    db.add(user)
                    log_activity(db, telegram_id, "new_user", f"@{username}" if username else None)
                    db.commit()

                    logger.info(f"Auto-registered new user: {telegram_id} (@{username})")

        except Exception as e:
            logger.error(f"Error in user registration middleware: {e}", exc_info=True)

    logger.info("User registration middleware registered")
