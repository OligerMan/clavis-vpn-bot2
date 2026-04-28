"""User auto-registration middleware for Telegram bot."""

import logging
from telebot import TeleBot
from telebot.types import Message

from config.settings import MAIN_DEVELOPER_ID
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

        # Extract ref_source from /start deep link payload
        ref_source = None
        if message.text and message.text.startswith('/start '):
            payload = message.text.split(maxsplit=1)[1].strip()
            if payload.startswith('ref_'):
                ref_source = payload[4:][:100]  # Strip prefix, limit length

        try:
            with get_db_session() as db:
                # Check if user exists
                user = db.query(User).filter(User.telegram_id == telegram_id).first()

                if not user:
                    # Create new user
                    user = User(
                        telegram_id=telegram_id,
                        username=username,
                        ref_source=ref_source,
                    )
                    db.add(user)
                    details_parts = []
                    if username:
                        details_parts.append(f"@{username}")
                    if ref_source:
                        details_parts.append(f"ref={ref_source}")
                    log_activity(db, telegram_id, "new_user", ", ".join(details_parts) or None)
                    db.commit()
                    db.refresh(user)

                    logger.info(
                        f"Auto-registered new user: {telegram_id} (@{username})"
                        + (f" ref={ref_source}" if ref_source else "")
                    )

                # Implicit Clavis account creation — main-developer-only during rollout.
                # Wrapped in inner try so app-integration failures never break bot flow.
                if telegram_id == MAIN_DEVELOPER_ID:
                    try:
                        from services.account_service import ensure_implicit_account
                        ensure_implicit_account(db, user)
                    except Exception as app_err:
                        logger.warning(
                            f"Clavis implicit account skipped for {telegram_id}: {app_err}"
                        )

        except Exception as e:
            logger.error(f"Error in user registration middleware: {e}", exc_info=True)

    logger.info("User registration middleware registered")
