"""User command handlers for Telegram bot."""

import logging
from datetime import datetime
from telebot import TeleBot
from telebot.types import Message, CallbackQuery

from database import get_db_session
from database.models import User
from services import SubscriptionService, KeyService
from message_templates import Messages
from bot.keyboards.markups import (
    main_menu_keyboard,
    test_key_confirmation_keyboard,
    platform_menu_keyboard,
    status_actions_keyboard,
    back_button_keyboard
)
from config.settings import SUBSCRIPTION_BASE_URL, DEVICE_LIMIT

logger = logging.getLogger(__name__)


def register_user_handlers(bot: TeleBot) -> None:
    """Register all user command handlers."""

    @bot.message_handler(commands=['start'])
    def handle_start(message: Message):
        """Handle /start command - show welcome message."""
        try:
            bot.send_message(
                message.chat.id,
                Messages.WELCOME,
                reply_markup=main_menu_keyboard(),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error in /start handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['menu'])
    def handle_menu(message: Message):
        """Handle /menu command - show all commands."""
        try:
            bot.send_message(
                message.chat.id,
                Messages.MENU,
                reply_markup=main_menu_keyboard(),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error in /menu handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['test_key'])
    def handle_test_key(message: Message):
        """Handle /test_key command - offer test subscription."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == message.from_user.id
                ).first()

                if not user:
                    bot.send_message(message.chat.id, Messages.ERROR_GENERIC)
                    return

                # Check if user already had a test
                if SubscriptionService.has_test_subscription(db, user):
                    bot.send_message(
                        message.chat.id,
                        Messages.TEST_KEY_ALREADY_USED,
                        parse_mode='Markdown'
                    )
                    return

                # Offer test key
                bot.send_message(
                    message.chat.id,
                    Messages.TEST_KEY_OFFER,
                    reply_markup=test_key_confirmation_keyboard(),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in /test_key handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['key'])
    def handle_key(message: Message):
        """Handle /key command - show subscription key."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == message.from_user.id
                ).first()

                if not user:
                    bot.send_message(message.chat.id, Messages.ERROR_GENERIC)
                    return

                # Get active subscription
                subscription = SubscriptionService.get_active_subscription(db, user)

                if not subscription:
                    bot.send_message(
                        message.chat.id,
                        Messages.NO_ACTIVE_SUBSCRIPTION,
                        parse_mode='Markdown'
                    )
                    return

                # Calculate days left
                days_left = (subscription.expires_at - datetime.utcnow()).days

                # Generate subscription URL and deep link
                subscription_url = SubscriptionService.get_subscription_url(
                    subscription,
                    SUBSCRIPTION_BASE_URL
                )
                v2raytun_deeplink = SubscriptionService.get_v2raytun_deeplink(
                    subscription,
                    SUBSCRIPTION_BASE_URL
                )

                # Send key
                bot.send_message(
                    message.chat.id,
                    Messages.KEY_SUCCESS.format(
                        subscription_url=subscription_url,
                        v2raytun_deeplink=v2raytun_deeplink,
                        expiry_date=subscription.expires_at.strftime('%d.%m.%Y %H:%M'),
                        days_left=days_left,
                        device_limit=DEVICE_LIMIT
                    ),
                    reply_markup=platform_menu_keyboard(),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in /key handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['status'])
    def handle_status(message: Message):
        """Handle /status command - show subscription status and traffic."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == message.from_user.id
                ).first()

                if not user:
                    bot.send_message(message.chat.id, Messages.ERROR_GENERIC)
                    return

                # Get active subscription
                subscription = SubscriptionService.get_active_subscription(db, user)

                if not subscription:
                    bot.send_message(
                        message.chat.id,
                        Messages.STATUS_NO_SUBSCRIPTION,
                        reply_markup=status_actions_keyboard(),
                        parse_mode='Markdown'
                    )
                    return

                # Get traffic stats
                traffic = KeyService.get_subscription_traffic(db, subscription)

                # Calculate days left
                days_left = (subscription.expires_at - datetime.utcnow()).days

                # Get device count (number of active keys)
                from database.models import Key
                device_count = db.query(Key).filter(
                    Key.subscription_id == subscription.id,
                    Key.is_active == True
                ).count()

                # Get renewal reminder
                renewal_reminder = SubscriptionService.get_renewal_reminder(subscription)

                # Determine subscription type
                subscription_type = "Тестовая" if subscription.is_test else "Платная"

                # Send status
                bot.send_message(
                    message.chat.id,
                    Messages.STATUS_INFO.format(
                        subscription_type=subscription_type,
                        expiry_date=subscription.expires_at.strftime('%d.%m.%Y %H:%M'),
                        days_left=days_left,
                        upload_gb=traffic['upload_gb'],
                        download_gb=traffic['download_gb'],
                        total_gb=traffic['total_gb'],
                        device_count=device_count,
                        device_limit=DEVICE_LIMIT,
                        renewal_reminder=renewal_reminder
                    ),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in /status handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['help'])
    def handle_help(message: Message):
        """Handle /help command - show help message."""
        try:
            bot.send_message(
                message.chat.id,
                Messages.HELP_MESSAGE.format(telegram_id=message.from_user.id),
                reply_markup=back_button_keyboard(),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error in /help handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['terms'])
    def handle_terms(message: Message):
        """Handle /terms command - show terms of service."""
        try:
            bot.send_message(
                message.chat.id,
                Messages.TERMS,
                reply_markup=back_button_keyboard(),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error in /terms handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['Android', 'IOS', 'Windows', 'MacOS'])
    def handle_platform_commands(message: Message):
        """Handle platform instruction commands."""
        try:
            command = message.text.lower().replace('/', '')

            message_map = {
                'android': Messages.ANDROID_INSTRUCTIONS,
                'ios': Messages.IOS_INSTRUCTIONS,
                'windows': Messages.WINDOWS_INSTRUCTIONS,
                'macos': Messages.MACOS_INSTRUCTIONS
            }

            instruction_message = message_map.get(command)

            if instruction_message:
                bot.send_message(
                    message.chat.id,
                    instruction_message,
                    reply_markup=back_button_keyboard(),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in platform command handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    # Callback query handler for test key confirmation
    @bot.callback_query_handler(func=lambda call: call.data == 'confirm_test_key')
    def handle_confirm_test_key(call: CallbackQuery):
        """Handle test key confirmation callback."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                if not user:
                    bot.answer_callback_query(call.id, "Ошибка: пользователь не найден")
                    return

                # Check again if user already had a test
                if SubscriptionService.has_test_subscription(db, user):
                    bot.answer_callback_query(call.id, "Вы уже использовали тест")
                    bot.edit_message_text(
                        Messages.TEST_KEY_ALREADY_USED,
                        call.message.chat.id,
                        call.message.id,
                        parse_mode='Markdown'
                    )
                    return

                # Create test subscription
                subscription = SubscriptionService.create_test_subscription(db, user)

                # Create keys
                try:
                    KeyService.create_subscription_keys(db, subscription, user.telegram_id)
                except ValueError as e:
                    logger.error(f"Error creating test key: {e}", exc_info=True)
                    bot.answer_callback_query(call.id, "Ошибка создания ключа")
                    bot.edit_message_text(
                        Messages.ERROR_KEY_CREATION,
                        call.message.chat.id,
                        call.message.id,
                        parse_mode='Markdown'
                    )
                    return

                # Generate subscription URL and deep link
                subscription_url = SubscriptionService.get_subscription_url(
                    subscription,
                    SUBSCRIPTION_BASE_URL
                )
                v2raytun_deeplink = SubscriptionService.get_v2raytun_deeplink(
                    subscription,
                    SUBSCRIPTION_BASE_URL
                )

                # Send success message
                bot.answer_callback_query(call.id, "Тестовый ключ создан!")
                bot.edit_message_text(
                    Messages.TEST_KEY_SUCCESS.format(
                        subscription_url=subscription_url,
                        v2raytun_deeplink=v2raytun_deeplink,
                        expiry_date=subscription.expires_at.strftime('%d.%m.%Y %H:%M')
                    ),
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=platform_menu_keyboard(),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in confirm_test_key callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")
            bot.send_message(call.message.chat.id, Messages.ERROR_GENERIC)

    # Callback handlers for navigation
    # Note: call.message.from_user is the bot, not the actual user.
    # We patch from_user so that command handlers can use message.from_user.id.
    def _patch_from_user(call: CallbackQuery) -> Message:
        """Return call.message with from_user set to the actual user."""
        call.message.from_user = call.from_user
        return call.message

    @bot.callback_query_handler(func=lambda call: call.data == 'get_test_key')
    def callback_get_test_key(call: CallbackQuery):
        """Handle get_test_key callback - same as /test_key command."""
        handle_test_key(_patch_from_user(call))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == 'get_key')
    def callback_get_key(call: CallbackQuery):
        """Handle get_key callback - same as /key command."""
        handle_key(_patch_from_user(call))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == 'status')
    def callback_status(call: CallbackQuery):
        """Handle status callback - same as /status command."""
        handle_status(_patch_from_user(call))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == 'help')
    def callback_help(call: CallbackQuery):
        """Handle help callback - same as /help command."""
        handle_help(_patch_from_user(call))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == 'back_to_menu')
    def callback_back_to_menu(call: CallbackQuery):
        """Handle back_to_menu callback - show main menu."""
        try:
            bot.edit_message_text(
                Messages.WELCOME,
                call.message.chat.id,
                call.message.id,
                reply_markup=main_menu_keyboard(),
                parse_mode='Markdown'
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in back_to_menu callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'cancel')
    def callback_cancel(call: CallbackQuery):
        """Handle cancel callback - delete message."""
        try:
            bot.delete_message(call.message.chat.id, call.message.id)
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in cancel callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id)

    logger.info("User handlers registered")
