"""User command handlers for Telegram bot."""

import logging
from datetime import datetime
from telebot import TeleBot
from telebot.types import Message, CallbackQuery

from database import get_db_session
from database.models import User
from database.activity_log import log_activity
from services import SubscriptionService, KeyService
from message_templates import Messages
from bot.keyboards.markups import (
    start_menu_keyboard,
    full_menu_keyboard,
    test_key_confirmation_keyboard,
    key_actions_keyboard,
    key_platform_keyboard,
    platform_menu_keyboard,
    platform_detailed_menu_keyboard,
    status_actions_keyboard,
    status_with_sub_keyboard,
    back_button_keyboard,
    support_actions_keyboard,
    support_platform_keyboard,
    faq_keyboard,
    android_instructions_keyboard,
    ios_instructions_keyboard,
    windows_instructions_keyboard,
    macos_instructions_keyboard,
    old_keys_keyboard,
)
from config.settings import SUBSCRIPTION_BASE_URL, DEVICE_LIMIT, MAIN_DEVELOPER_ID, format_msk

logger = logging.getLogger(__name__)


def _handle_clavis_start(bot: TeleBot, message: Message, payload: str) -> None:
    """Handle /start clavis_login and /start clavis_sync for MAIN_DEVELOPER_ID.

    Called only after the admin-gate passes. Creates a login_token or sync_pair
    directly via the account service and sends the user a button with the
    HTTPS deep-link wrapper URL.
    """
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    from services.account_service import (
        create_login_token,
        create_sync_pair,
        ensure_implicit_account,
        upsert_telegram_device_for_user,
    )

    telegram_id = message.from_user.id
    base = SUBSCRIPTION_BASE_URL.rstrip('/')

    with get_db_session() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            # Middleware should have created this — fallback defensively.
            user = User(telegram_id=telegram_id, username=message.from_user.username)
            db.add(user)
            db.flush()
        # Ensure account + telegram device exist.
        account = ensure_implicit_account(db, user)
        tg_device = upsert_telegram_device_for_user(db, user)

        if payload == "clavis_login":
            row = create_login_token(db, account.id)
            login_url = f"{base}/login/{row.token}"
            kb = InlineKeyboardMarkup()
            kb.row(InlineKeyboardButton("🔒 Открыть в Clavis", url=login_url))
            bot.send_message(
                message.chat.id,
                "Нажмите кнопку, чтобы войти в Clavis на новом устройстве. Ссылка одноразовая, живёт 10 минут.",
                reply_markup=kb,
            )
            return

        if payload == "clavis_sync":
            pair = create_sync_pair(db, tg_device)
            sync_url = f"{base}/sync/{pair.pair_code}"

            # Render QR PNG and send as photo.
            try:
                import io
                import segno
                qr = segno.make(sync_url, error='h')
                buf = io.BytesIO()
                qr.save(buf, kind='png', scale=10, border=2)
                buf.seek(0)
                bot.send_photo(
                    message.chat.id,
                    buf,
                    caption=(
                        f"Код: `{pair.pair_code}`\n"
                        f"Отсканируй QR на пустом устройстве Clavis. Код живёт 2 минуты."
                    ),
                    parse_mode='Markdown',
                )
            except Exception as qr_err:
                logger.warning(f"Sync QR render failed: {qr_err}")
                bot.send_message(
                    message.chat.id,
                    f"Код синхронизации: `{pair.pair_code}`\n"
                    f"Открой: {sync_url}",
                    parse_mode='Markdown',
                )
            return


def register_user_handlers(bot: TeleBot) -> None:
    """Register all user command handlers."""

    @bot.message_handler(commands=['start'])
    def handle_start(message: Message):
        """Handle /start command - show welcome message."""
        try:
            # Extract payload from /start deep link
            payload = ""
            if message.text and ' ' in message.text:
                payload = message.text.split(maxsplit=1)[1].strip()

            # Accept a unique-suffixed payload (clavis_login_<nonce>) as well as
            # the bare command. Telegram Desktop won't re-send an identical
            # /start when the bot chat is already open, so the app varies the
            # payload each launch; normalise it back to the base command here.
            clavis_cmd = None
            if payload == "clavis_login" or payload.startswith("clavis_login_"):
                clavis_cmd = "clavis_login"
            elif payload == "clavis_sync" or payload.startswith("clavis_sync_"):
                clavis_cmd = "clavis_sync"
            if clavis_cmd is not None:
                try:
                    _handle_clavis_start(bot, message, clavis_cmd)
                    return
                except Exception as app_err:
                    logger.error(f"Clavis /start {payload} failed: {app_err}", exc_info=True)
                    bot.send_message(
                        message.chat.id,
                        "Ошибка интеграции Clavis-app. Смотри логи.",
                        parse_mode=""
                    )
                    return

            ref_source = None
            if payload.startswith('ref_'):
                ref_source = payload[4:][:100]

            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == message.from_user.id
                ).first()

                # Backfill ref_source for existing users
                if user and ref_source and not user.ref_source:
                    user.ref_source = ref_source
                    db.commit()

                # Legacy or paid users get full menu right away
                if user:
                    has_legacy = KeyService.has_legacy_keys(db, user)
                    active_sub = SubscriptionService.get_active_subscription(db, user)
                    if has_legacy or (active_sub and not active_sub.is_test):
                        hide_test_key = active_sub is not None
                        show_invite = bool(active_sub and not active_sub.is_test and getattr(active_sub, 'plan_type', 'basic') not in ('free', None))
                        welcome_text = Messages.WELCOME_LEGACY if has_legacy else Messages.WELCOME
                        bot.send_message(
                            message.chat.id,
                            welcome_text,
                            reply_markup=full_menu_keyboard(hide_test_key, show_old_keys=has_legacy, show_invite=show_invite),
                            parse_mode='Markdown'
                        )
                        return

                bot.send_message(
                    message.chat.id,
                    Messages.WELCOME,
                    reply_markup=start_menu_keyboard(),
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"Error in /start handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['menu', 'help'])
    def handle_menu(message: Message):
        """Handle /menu command - show all commands."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == message.from_user.id
                ).first()

                # Hide test key button if user already used test OR has paid subscription
                hide_test_key = False
                show_old_keys = False
                show_invite = False
                if user:
                    # Check if already used test
                    if SubscriptionService.has_test_subscription(db, user):
                        hide_test_key = True
                    # Check if has active paid subscription
                    active_sub = SubscriptionService.get_active_subscription(db, user)
                    if active_sub and not active_sub.is_test:
                        hide_test_key = True
                        show_invite = getattr(active_sub, 'plan_type', 'basic') not in ('free', None)
                    # Check for legacy keys
                    show_old_keys = KeyService.has_legacy_keys(db, user)

                bot.send_message(
                    message.chat.id,
                    Messages.MENU,
                    reply_markup=full_menu_keyboard(hide_test_key, show_old_keys=show_old_keys, show_invite=show_invite),
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

                # Ensure keys exist for legacy/migrated users (no server-linked keys yet)
                if not KeyService.has_server_keys(db, subscription):
                    KeyService.ensure_keys_exist(db, subscription, user.telegram_id)

                # Calculate days left
                days_left = max(0, (subscription.expires_at - datetime.utcnow()).days)

                # Send key with platform selection
                bot.send_message(
                    message.chat.id,
                    Messages.KEY_WITH_PLATFORMS.format(
                        expiry_date=format_msk(subscription.expires_at),
                        days_left=days_left,
                    ),
                    reply_markup=key_platform_keyboard(),
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
                days_left = max(0, (subscription.expires_at - datetime.utcnow()).days)

                # Get renewal reminder
                renewal_reminder = SubscriptionService.get_renewal_reminder(subscription)

                # Determine subscription type
                if subscription.is_test:
                    subscription_type = "Тестовая"
                elif subscription.plan_type == 'unlimited':
                    subscription_type = "Безлимит"
                else:
                    subscription_type = "Стандарт"

                # Whitelist traffic section
                whitelist_section = ""
                try:
                    from services.traffic_limit_service import get_user_whitelist_traffic
                    wl = get_user_whitelist_traffic(db, user.id)
                    if wl:
                        if wl.get("is_unlimited"):
                            whitelist_section = "\n**Трафик Whitelist:**\n• Безлимит"
                        else:
                            lines = [
                                "\n**Трафик Whitelist:**",
                                f"• Использовано: {wl['used_gb']} из {wl['limit_gb']} ГБ",
                                f"• Осталось: {wl['remaining_gb']} ГБ",
                            ]
                            if wl["is_exceeded"]:
                                lines.append("• ⛔ Лимит превышен")
                            whitelist_section = "\n".join(lines)
                except Exception:
                    pass

                # Send status
                bot.send_message(
                    message.chat.id,
                    Messages.STATUS_INFO.format(
                        subscription_type=subscription_type,
                        expiry_date=format_msk(subscription.expires_at),
                        days_left=days_left,
                        upload_gb=traffic['upload_gb'],
                        download_gb=traffic['download_gb'],
                        total_gb=traffic['total_gb'],
                        whitelist_section=whitelist_section,
                        renewal_reminder=renewal_reminder
                    ),
                    reply_markup=status_with_sub_keyboard(),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in /status handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.message_handler(commands=['support'])
    def handle_support(message: Message):
        """Handle /support command - show support info with subscription status."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == message.from_user.id
                ).first()

                # Get subscription status
                subscription_status = "**Статус подписки:** Не оплачена"
                if user:
                    subscription = SubscriptionService.get_active_subscription(db, user)
                    if subscription:
                        days_left = max(0, (subscription.expires_at - datetime.utcnow()).days)
                        if subscription.is_test:
                            sub_type = "Тестовая"
                        elif subscription.plan_type == 'unlimited':
                            sub_type = "Безлимит"
                        else:
                            sub_type = "Стандарт"
                        subscription_status = f"**Статус подписки:** {sub_type} (осталось {days_left} дней)"

                bot.send_message(
                    message.chat.id,
                    Messages.SUPPORT_MESSAGE.format(
                        telegram_id=message.from_user.id,
                        subscription_status=subscription_status
                    ),
                    reply_markup=support_actions_keyboard(message.from_user.id),
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"Error in /support handler: {e}", exc_info=True)
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

            # Get deeplinks for the user if they have an active subscription
            v2raytun_deeplink = None
            happ_deeplink = None
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == message.from_user.id).first()
                if user:
                    subscription = SubscriptionService.get_active_subscription(db, user)
                    if subscription:
                        v2raytun_deeplink = SubscriptionService.get_v2raytun_deeplink(
                            subscription, SUBSCRIPTION_BASE_URL
                        )
                        happ_deeplink = SubscriptionService.get_happ_deeplink(
                            subscription, SUBSCRIPTION_BASE_URL
                        )

            platform_data = {
                'android': (Messages.ANDROID_INSTRUCTIONS, android_instructions_keyboard(v2raytun_deeplink)),
                'ios': (Messages.IOS_INSTRUCTIONS, ios_instructions_keyboard(happ_deeplink)),
                'windows': (Messages.WINDOWS_INSTRUCTIONS, windows_instructions_keyboard(v2raytun_deeplink)),
                'macos': (Messages.MACOS_INSTRUCTIONS, macos_instructions_keyboard(happ_deeplink))
            }

            data = platform_data.get(command)

            if data:
                instruction_message, keyboard = data
                bot.send_message(
                    message.chat.id,
                    instruction_message,
                    reply_markup=keyboard,
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

                # Create keys (lazy init — up to USER_SERVER_LIMIT servers)
                try:
                    KeyService.ensure_keys_exist(db, subscription, user.telegram_id)
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

                log_activity(db, call.from_user.id, "test_key", f"до {format_msk(subscription.expires_at)}")

                # Send success message with platform selection
                bot.answer_callback_query(call.id, "Тестовый ключ создан!")
                bot.edit_message_text(
                    Messages.TEST_KEY_SUCCESS.format(
                        expiry_date=format_msk(subscription.expires_at)
                    ),
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=key_platform_keyboard(),
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

    @bot.callback_query_handler(func=lambda call: call.data == 'support')
    def callback_support(call: CallbackQuery):
        """Handle support callback - same as /support command."""
        handle_support(_patch_from_user(call))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == 'faq')
    def callback_faq(call: CallbackQuery):
        """Handle FAQ callback - show frequently asked questions."""
        try:
            bot.edit_message_text(
                Messages.FAQ_MESSAGE,
                call.message.chat.id,
                call.message.id,
                reply_markup=faq_keyboard(call.from_user.id),
                parse_mode='Markdown'
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in FAQ callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'back_to_menu')
    def callback_back_to_menu(call: CallbackQuery):
        """Handle back_to_menu callback - show full menu."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                # Hide test key button if user already used test OR has paid subscription
                hide_test_key = False
                show_old_keys = False
                show_invite = False
                if user:
                    # Check if already used test
                    if SubscriptionService.has_test_subscription(db, user):
                        hide_test_key = True
                    # Check if has active paid subscription
                    active_sub = SubscriptionService.get_active_subscription(db, user)
                    if active_sub and not active_sub.is_test:
                        hide_test_key = True
                        show_invite = getattr(active_sub, 'plan_type', 'basic') not in ('free', None)
                    # Check for legacy keys
                    show_old_keys = KeyService.has_legacy_keys(db, user)

                bot.edit_message_text(
                    Messages.WELCOME,
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=full_menu_keyboard(hide_test_key, show_old_keys=show_old_keys, show_invite=show_invite),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in back_to_menu callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'old_keys')
    def callback_old_keys(call: CallbackQuery):
        """Handle old_keys callback - show legacy keys with deprecation notice."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                if not user:
                    bot.answer_callback_query(call.id, "Ошибка: пользователь не найден")
                    return

                legacy_keys = KeyService.get_legacy_keys(db, user)
                if not legacy_keys:
                    bot.answer_callback_query(call.id, "У вас нет старых ключей")
                    return

                # Format key list — each key as copyable code block
                lines = []
                for i, key in enumerate(legacy_keys, 1):
                    protocol = "Outline" if key.protocol == "outline" else "VLESS"
                    lines.append(f"{i}. *{protocol}(старый ключ):*\n`{key.key_data}`")

                keys_list = "\n\n".join(lines)

                bot.edit_message_text(
                    Messages.OLD_KEYS_INFO.format(keys_list=keys_list),
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=old_keys_keyboard(),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in old_keys callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'back_to_key')
    def callback_back_to_key(call: CallbackQuery):
        """Handle back_to_key callback - rebuild subscription info + OS selection."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                if not user:
                    bot.answer_callback_query(call.id, "Ошибка")
                    return

                subscription = SubscriptionService.get_active_subscription(db, user)

                if not subscription:
                    bot.edit_message_text(
                        Messages.NO_ACTIVE_SUBSCRIPTION,
                        call.message.chat.id,
                        call.message.id,
                        parse_mode='Markdown'
                    )
                    bot.answer_callback_query(call.id)
                    return

                days_left = max(0, (subscription.expires_at - datetime.utcnow()).days)

                bot.edit_message_text(
                    Messages.KEY_WITH_PLATFORMS.format(
                        expiry_date=format_msk(subscription.expires_at),
                        days_left=days_left,
                    ),
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=key_platform_keyboard(),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in back_to_key callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'back_to_support')
    def callback_back_to_support(call: CallbackQuery):
        """Handle back_to_support callback - rebuild support info + keyboard."""
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                # Get subscription status
                subscription_status = "**Статус подписки:** Не оплачена"
                if user:
                    subscription = SubscriptionService.get_active_subscription(db, user)
                    if subscription:
                        days_left = max(0, (subscription.expires_at - datetime.utcnow()).days)
                        if subscription.is_test:
                            sub_type = "Тестовая"
                        elif subscription.plan_type == 'unlimited':
                            sub_type = "Безлимит"
                        else:
                            sub_type = "Стандарт"
                        subscription_status = f"**Статус подписки:** {sub_type} (осталось {days_left} дней)"

                bot.edit_message_text(
                    Messages.SUPPORT_MESSAGE.format(
                        telegram_id=call.from_user.id,
                        subscription_status=subscription_status
                    ),
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=support_actions_keyboard(call.from_user.id),
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in back_to_support callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'show_platforms_support')
    def callback_show_platforms_support(call: CallbackQuery):
        """Handle show_platforms_support — platform selection within support flow."""
        try:
            bot.edit_message_text(
                "📲 **Инструкции по подключению**\n\nВыберите вашу платформу:",
                call.message.chat.id,
                call.message.id,
                reply_markup=support_platform_keyboard(),
                parse_mode='Markdown'
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in show_platforms_support callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'show_platforms')
    def callback_show_platforms(call: CallbackQuery):
        """Handle show_platforms callback - show platform selection."""
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.id,
                reply_markup=platform_menu_keyboard()
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in show_platforms callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'show_platforms_detailed')
    def callback_show_platforms_detailed(call: CallbackQuery):
        """Handle show_platforms_detailed — platform selection for 'other methods'."""
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.id,
                reply_markup=platform_detailed_menu_keyboard()
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in show_platforms_detailed callback: {e}", exc_info=True)
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

    @bot.callback_query_handler(func=lambda call: call.data == 'gen_referral')
    def callback_gen_referral(call: CallbackQuery):
        """Generate a referral invite link for the user to share with a friend."""
        import secrets
        import string
        from database.models import ReferralInvite
        from config.settings import SUBSCRIPTION_BASE_URL

        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                if not user:
                    bot.answer_callback_query(call.id, "Ошибка: пользователь не найден")
                    return

                # Only paid (non-test, non-free) subscribers can invite
                active_sub = SubscriptionService.get_active_subscription(db, user)
                if not active_sub or active_sub.is_test or getattr(active_sub, 'plan_type', 'basic') in ('free', None):
                    bot.answer_callback_query(call.id, "Приглашать друзей могут только платные пользователи")
                    return

                # Generate unique 10-char code
                alphabet = string.ascii_lowercase + string.digits
                for _ in range(10):
                    code = ''.join(secrets.choice(alphabet) for _ in range(10))
                    if not db.query(ReferralInvite).filter(ReferralInvite.code == code).first():
                        break

                invite = ReferralInvite(
                    code=code,
                    inviter_id=user.id,
                )
                db.add(invite)
                db.commit()
                log_activity(db, call.from_user.id, "invite_created", f"code={code}")

            base = SUBSCRIPTION_BASE_URL.rstrip('/')
            invite_url = f"{base}/invite/{code}"

            text = (
                "👥 *Ссылка для приглашения друга*\n\n"
                "Отправь другу эту ссылку\\. Он откроет её в браузере и получит "
                "бесплатный VPN на 3 дня — даже если Telegram не работает\\.\n\n"
                f"`{invite_url}`\n\n"
                "_Ссылка одноразовая и привязана к одному другу\\._"
            )
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                text,
                parse_mode='MarkdownV2',
            )

            # Send QR code as photo for showing to a friend in person
            try:
                import segno, io as _io
                qr = segno.make(invite_url, error='h')
                buf = _io.BytesIO()
                qr.save(buf, kind='png', scale=10, border=2)
                buf.seek(0)
                buf.name = 'invite_qr.png'
                bot.send_photo(
                    call.message.chat.id,
                    buf,
                    caption="📲 Покажи QR-код другу при встрече — он сканирует и получает VPN",
                )
            except Exception as qr_err:
                logger.warning(f"Failed to generate QR for invite {code}: {qr_err}")

            logger.info(f"Referral invite generated: code={code}, inviter={call.from_user.id}")

        except Exception as e:
            logger.error(f"Error in gen_referral callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    logger.info("User handlers registered")
