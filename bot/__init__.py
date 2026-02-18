"""Bot initialization and handler registration for Clavis VPN Bot v2."""

import logging
from telebot import TeleBot, apihelper

from config.settings import BOT_TOKEN
from bot.middlewares import register_user_middleware
from bot.handlers.user import register_user_handlers
from bot.handlers.payment import register_payment_handlers
from bot.handlers.client_instructions import register_client_instruction_handlers
from bot.handlers.admin import register_admin_handlers
from bot.handlers.broadcast import register_broadcast_handlers

logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN not found in environment variables. "
        "Set BOT_TOKEN in .env file or environment before starting the bot."
    )

# Enable middleware support before creating bot instance
apihelper.ENABLE_MIDDLEWARE = True

# Create bot instance
bot = TeleBot(BOT_TOKEN, parse_mode='Markdown')


def get_bot() -> TeleBot:
    """Get the bot instance."""
    return bot


def register_handlers() -> None:
    """Register all bot handlers and middlewares."""
    logger.info("Registering bot handlers...")

    # Register middleware
    register_user_middleware(bot)

    # Register handlers
    register_user_handlers(bot)
    register_payment_handlers(bot)
    register_client_instruction_handlers(bot)
    register_admin_handlers(bot)
    register_broadcast_handlers(bot)

    logger.info("All handlers registered successfully")


def start_polling() -> None:
    """Start bot polling loop."""
    logger.info("Starting bot polling...")
    try:
        bot.infinity_polling(
            timeout=60,
            long_polling_timeout=60,
            allowed_updates=["message", "callback_query", "pre_checkout_query"]
        )
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Error in bot polling: {e}", exc_info=True)
        raise
