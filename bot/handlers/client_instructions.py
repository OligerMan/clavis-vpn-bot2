"""Client setup instructions handlers for Telegram bot."""

import logging
from telebot import TeleBot
from telebot.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import get_db_session
from database.models import User
from services import SubscriptionService
from message_templates import Messages
from bot.keyboards.markups import (
    android_instructions_keyboard,
    ios_instructions_keyboard,
    windows_instructions_keyboard,
    macos_instructions_keyboard,
    detailed_instructions_keyboard,
    other_connection_methods_keyboard,
    clipboard_import_keyboard
)
from config.settings import SUBSCRIPTION_BASE_URL

logger = logging.getLogger(__name__)


def register_client_instruction_handlers(bot: TeleBot) -> None:
    """Register all client instruction callback handlers."""

    @bot.callback_query_handler(func=lambda call: call.data.startswith('platform_'))
    def handle_platform_selection(call: CallbackQuery):
        """Handle platform selection callbacks."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == call.from_user.id).first()

                # Get v2rayTun deep link for all platforms
                v2raytun_deeplink = None
                if user:
                    subscription = SubscriptionService.get_active_subscription(db, user)
                    if subscription:
                        v2raytun_deeplink = SubscriptionService.get_v2raytun_deeplink(
                            subscription, SUBSCRIPTION_BASE_URL
                        )

                # Map callback data to messages and keyboards
                platform_map = {
                    'platform_android': (Messages.ANDROID_INSTRUCTIONS, android_instructions_keyboard(v2raytun_deeplink)),
                    'platform_ios': (Messages.IOS_INSTRUCTIONS, ios_instructions_keyboard(v2raytun_deeplink)),
                    'platform_windows': (Messages.WINDOWS_INSTRUCTIONS, windows_instructions_keyboard(v2raytun_deeplink)),
                    'platform_macos': (Messages.MACOS_INSTRUCTIONS, macos_instructions_keyboard(v2raytun_deeplink))
                }

                platform_data = platform_map.get(call.data)

                if platform_data:
                    instruction_message, keyboard = platform_data
                    bot.edit_message_text(
                        instruction_message,
                        call.message.chat.id,
                        call.message.id,
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                    bot.answer_callback_query(call.id)
                else:
                    bot.answer_callback_query(call.id, "Неизвестная платформа")

        except Exception as e:
            logger.error(f"Error in platform selection callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'add_subscription_to_client')
    def handle_add_subscription_to_client(call: CallbackQuery):
        """Handle add subscription to client callback - opens v2rayTun deep link."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                if not user:
                    bot.answer_callback_query(call.id, "Ошибка: пользователь не найден")
                    return

                # Get active subscription
                subscription = SubscriptionService.get_active_subscription(db, user)

                if not subscription:
                    bot.answer_callback_query(call.id, "У вас нет активной подписки")
                    bot.send_message(
                        call.message.chat.id,
                        Messages.NO_ACTIVE_SUBSCRIPTION,
                        parse_mode='Markdown'
                    )
                    return

                # Generate v2rayTun deep link
                v2raytun_deeplink = SubscriptionService.get_v2raytun_deeplink(
                    subscription,
                    SUBSCRIPTION_BASE_URL
                )

                # Create keyboard with deep link button
                keyboard = InlineKeyboardMarkup()
                keyboard.row(
                    InlineKeyboardButton("🚀 Открыть в v2rayTun", url=v2raytun_deeplink)
                )

                bot.answer_callback_query(call.id, "✅ Готово!")
                bot.send_message(
                    call.message.chat.id,
                    "✅ **Готово!**\n\nНажмите кнопку ниже, чтобы автоматически добавить подписку в v2rayTun:",
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in add subscription to client callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data.endswith('_detailed'))
    def handle_detailed_instructions(call: CallbackQuery):
        """Handle 'other connection methods' menu - shows intermediate menu."""
        try:
            # Map platform to message and platform name
            platform_map = {
                'android_detailed': (Messages.OTHER_METHODS_ANDROID, 'android'),
                'ios_detailed': (Messages.OTHER_METHODS_IOS, 'ios'),
                'windows_detailed': (Messages.OTHER_METHODS_WINDOWS, 'windows'),
                'macos_detailed': (Messages.OTHER_METHODS_MACOS, 'macos')
            }

            platform_data = platform_map.get(call.data)

            if platform_data:
                message, platform = platform_data
                bot.edit_message_text(
                    message,
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=other_connection_methods_keyboard(platform),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
            else:
                bot.answer_callback_query(call.id, "Неизвестная платформа")

        except Exception as e:
            logger.error(f"Error in other connection methods callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data.endswith('_other_methods'))
    def handle_back_to_other_methods(call: CallbackQuery):
        """Handle back button to other connection methods menu."""
        try:
            # Extract platform from callback data (e.g., "android_other_methods" -> "android")
            platform = call.data.replace('_other_methods', '')

            # Map platform to message
            message_map = {
                'android': Messages.OTHER_METHODS_ANDROID,
                'ios': Messages.OTHER_METHODS_IOS,
                'windows': Messages.OTHER_METHODS_WINDOWS,
                'macos': Messages.OTHER_METHODS_MACOS
            }

            message = message_map.get(platform)

            if message:
                bot.edit_message_text(
                    message,
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=other_connection_methods_keyboard(platform),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
            else:
                bot.answer_callback_query(call.id, "Неизвестная платформа")

        except Exception as e:
            logger.error(f"Error in back to other methods callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('clipboard_import_'))
    def handle_clipboard_import(call: CallbackQuery):
        """Handle clipboard import instructions."""
        try:
            # Extract platform from callback data
            platform = call.data.replace('clipboard_import_', '')

            # Map platform to message
            message_map = {
                'android': Messages.CLIPBOARD_IMPORT_ANDROID,
                'ios': Messages.CLIPBOARD_IMPORT_IOS,
                'windows': Messages.CLIPBOARD_IMPORT_WINDOWS,
                'macos': Messages.CLIPBOARD_IMPORT_MACOS
            }

            message = message_map.get(platform)

            if message:
                bot.edit_message_text(
                    message,
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=clipboard_import_keyboard(platform),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
            else:
                bot.answer_callback_query(call.id, "Неизвестная платформа")

        except Exception as e:
            logger.error(f"Error in clipboard import callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    logger.info("Client instruction handlers registered")
