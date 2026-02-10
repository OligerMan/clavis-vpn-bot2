"""Client setup instructions handlers for Telegram bot."""

import logging
from telebot import TeleBot
from telebot.types import CallbackQuery

from message_templates import Messages
from bot.keyboards.markups import back_button_keyboard

logger = logging.getLogger(__name__)


def register_client_instruction_handlers(bot: TeleBot) -> None:
    """Register all client instruction callback handlers."""

    @bot.callback_query_handler(func=lambda call: call.data.startswith('platform_'))
    def handle_platform_selection(call: CallbackQuery):
        """Handle platform selection callbacks."""
        try:
            # Map callback data to messages
            platform_map = {
                'platform_android': Messages.ANDROID_INSTRUCTIONS,
                'platform_ios': Messages.IOS_INSTRUCTIONS,
                'platform_windows': Messages.WINDOWS_INSTRUCTIONS,
                'platform_macos': Messages.MACOS_INSTRUCTIONS
            }

            instruction_message = platform_map.get(call.data)

            if instruction_message:
                bot.edit_message_text(
                    instruction_message,
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=back_button_keyboard(),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
            else:
                bot.answer_callback_query(call.id, "Неизвестная платформа")

        except Exception as e:
            logger.error(f"Error in platform selection callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    logger.info("Client instruction handlers registered")
