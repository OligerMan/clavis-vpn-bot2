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
    detailed_instructions_keyboard
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
        """Handle detailed instructions callbacks."""
        try:
            detailed_map = {
                'android_detailed': (Messages.ANDROID_INSTRUCTIONS_DETAILED, 'android'),
                'ios_detailed': (Messages.IOS_INSTRUCTIONS_DETAILED, 'ios'),
                'windows_detailed': (Messages.WINDOWS_INSTRUCTIONS_DETAILED, 'windows'),
                'macos_detailed': (Messages.MACOS_INSTRUCTIONS_DETAILED, 'macos')
            }

            detailed_data = detailed_map.get(call.data)

            if detailed_data:
                message, platform = detailed_data
                bot.edit_message_text(
                    message,
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=detailed_instructions_keyboard(platform),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
            else:
                bot.answer_callback_query(call.id, "Неизвестная платформа")

        except Exception as e:
            logger.error(f"Error in detailed instructions callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    logger.info("Client instruction handlers registered")
