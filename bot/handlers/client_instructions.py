"""Client setup instructions handlers for Telegram bot."""

import logging
from telebot import TeleBot
from telebot.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import get_db_session
from database.models import User, Key, Subscription, Server
from services import SubscriptionService
from message_templates import Messages
from bot.keyboards.markups import (
    android_instructions_keyboard,
    ios_instructions_keyboard,
    windows_instructions_keyboard,
    macos_instructions_keyboard,
    detailed_instructions_keyboard,
    other_connection_methods_keyboard,
    clipboard_import_keyboard,
    vless_keys_keyboard,
    outline_key_keyboard,
)
from config.settings import SUBSCRIPTION_BASE_URL

logger = logging.getLogger(__name__)


def _has_outline_keys(telegram_id: int) -> bool:
    """Check if user has any active Outline (legacy) keys."""
    with get_db_session() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            return False
        return (
            db.query(Key)
            .join(Subscription)
            .filter(
                Subscription.user_id == user.id,
                Key.protocol == "outline",
                Key.is_active == True,
            )
            .count()
            > 0
        )


def register_client_instruction_handlers(bot: TeleBot) -> None:
    """Register all client instruction callback handlers."""

    @bot.callback_query_handler(func=lambda call: call.data.startswith('platform_'))
    def handle_platform_selection(call: CallbackQuery):
        """Handle platform selection callbacks.

        Callback format: platform_<name> or platform_<name>:<source>
        where source is 'key' or 'support'. Defaults to 'key'.
        """
        try:
            # Parse callback data: "platform_android:key" → platform="platform_android", source="key"
            raw = call.data
            if ':' in raw:
                platform_key, source = raw.split(':', 1)
            else:
                platform_key, source = raw, 'key'

            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == call.from_user.id).first()

                # Get deep links for all platforms
                v2raytun_deeplink = None
                happ_deeplink = None
                if user:
                    subscription = SubscriptionService.get_active_subscription(db, user)
                    if subscription:
                        v2raytun_deeplink = SubscriptionService.get_v2raytun_deeplink(
                            subscription, SUBSCRIPTION_BASE_URL
                        )
                        happ_deeplink = SubscriptionService.get_happ_deeplink(
                            subscription, SUBSCRIPTION_BASE_URL
                        )

                # Map callback data to messages and keyboards
                platform_map = {
                    'platform_android': (Messages.ANDROID_INSTRUCTIONS, android_instructions_keyboard(v2raytun_deeplink, source=source)),
                    'platform_ios': (Messages.IOS_INSTRUCTIONS, ios_instructions_keyboard(happ_deeplink, source=source)),
                    'platform_windows': (Messages.WINDOWS_INSTRUCTIONS, windows_instructions_keyboard(v2raytun_deeplink, source=source)),
                    'platform_macos': (Messages.MACOS_INSTRUCTIONS, macos_instructions_keyboard(happ_deeplink, source=source))
                }

                platform_data = platform_map.get(platform_key)

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
                show_outline = _has_outline_keys(call.from_user.id)
                bot.edit_message_text(
                    message,
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=other_connection_methods_keyboard(
                        platform,
                        show_outline=show_outline,
                        back_callback="show_platforms_detailed",
                    ),
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
                show_outline = _has_outline_keys(call.from_user.id)
                bot.edit_message_text(
                    message,
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=other_connection_methods_keyboard(platform, show_outline=show_outline),
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
        """Handle clipboard import instructions with user's subscription link."""
        try:
            platform = call.data.replace('clipboard_import_', '')

            platform_names = {
                'android': 'Android',
                'ios': 'iOS',
                'windows': 'Windows',
                'macos': 'macOS'
            }

            platform_name = platform_names.get(platform)
            if not platform_name:
                bot.answer_callback_query(call.id, "Неизвестная платформа")
                return

            # Get user's subscription link
            sub_url = None
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == call.from_user.id).first()
                if user:
                    subscription = SubscriptionService.get_active_subscription(db, user)
                    if subscription:
                        sub_url = subscription.get_subscription_url(SUBSCRIPTION_BASE_URL)

            if sub_url:
                link_text = f"`{sub_url}`"
            else:
                link_text = "Используйте /key чтобы получить ссылку"

            copy_hint = "Выберите ссылку и нажмите Cmd+C" if platform == 'macos' else (
                "Выберите ссылку и нажмите Ctrl+C" if platform == 'windows' else
                "Нажмите на ссылку и удерживайте для копирования"
            )

            message = (
                f"📋 **Импорт ссылки с подпиской ({platform_name})**\n\n"
                f"**Шаг 1:** Скопируйте ссылку на подписку\n"
                f"{link_text}\n"
                f"_{copy_hint}_\n\n"
                f"**Шаг 2:** Откройте приложение v2rayTun\n\n"
                f"**Шаг 3:** Нажмите кнопку **+** (плюс)\n\n"
                f"**Шаг 4:** Выберите **\"Импорт из буфера обмена\"**\n\n"
                f"**Шаг 5:** Подтвердите импорт\n\n"
                f"Готово! Подписка добавлена. Нажмите кнопку подключения. 🎉"
            )

            bot.edit_message_text(
                message,
                call.message.chat.id,
                call.message.id,
                reply_markup=clipboard_import_keyboard(platform),
                parse_mode='Markdown'
            )
            bot.answer_callback_query(call.id)

        except Exception as e:
            logger.error(f"Error in clipboard import callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('clavis_applink_'))
    def handle_clavis_applink(call: CallbackQuery):
        """Generate a one-time Clavis-app login link ('login by link') for the user.

        Reuses the same account/login-token flow as the /start clavis_login deep-link:
        ensure the user has a ClavisAccount (back-linking their subscriptions), mint a
        10-min single-use login token, and present it as copyable text + a tap button.
        """
        try:
            platform = call.data.replace('clavis_applink_', '')
            from services.account_service import ensure_implicit_account, create_login_token

            telegram_id = call.from_user.id
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == telegram_id).first()
                if user is None:
                    user = User(telegram_id=telegram_id, username=call.from_user.username)
                    db.add(user)
                    db.flush()
                account = ensure_implicit_account(db, user)
                if account is None:
                    bot.answer_callback_query(call.id, "Не удалось создать аккаунт Clavis")
                    return
                row = create_login_token(db, account.id)
                login_url = f"{SUBSCRIPTION_BASE_URL.rstrip('/')}/login/{row.token}"

            kb = InlineKeyboardMarkup()
            kb.row(InlineKeyboardButton("🔒 Открыть в Clavis", url=login_url))
            kb.row(InlineKeyboardButton("◀️ Назад", callback_data=f"{platform}_other_methods"))

            message = (
                "📲 **Вход в приложение Clavis по ссылке**\n\n"
                "**Шаг 1:** Откройте приложение Clavis → «Войти» → «Вход по ссылке».\n\n"
                "**Шаг 2:** Вставьте эту ссылку:\n"
                f"`{login_url}`\n\n"
                "Или нажмите **«🔒 Открыть в Clavis»** ниже, чтобы войти сразу.\n\n"
                "_Ссылка одноразовая и действует 10 минут._"
            )
            bot.edit_message_text(
                message,
                call.message.chat.id,
                call.message.id,
                reply_markup=kb,
                parse_mode='Markdown',
            )
            bot.answer_callback_query(call.id)

        except Exception as e:
            logger.error(f"Error in clavis applink callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('vless_keys_'))
    def handle_vless_keys(call: CallbackQuery):
        """Show individual VLESS keys from user's subscription."""
        try:
            platform = call.data.replace('vless_keys_', '')

            platform_names = {
                'android': 'Android',
                'ios': 'iOS',
                'windows': 'Windows',
                'macos': 'macOS'
            }

            platform_name = platform_names.get(platform)
            if not platform_name:
                bot.answer_callback_query(call.id, "Неизвестная платформа")
                return

            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == call.from_user.id).first()
                if not user:
                    bot.answer_callback_query(call.id, "Пользователь не найден")
                    return

                subscription = SubscriptionService.get_active_subscription(db, user)
                if not subscription:
                    bot.edit_message_text(
                        "❌ **Нет активной подписки**\n\n"
                        "Оформите подписку, чтобы получить VLESS-ключи.",
                        call.message.chat.id,
                        call.message.id,
                        reply_markup=vless_keys_keyboard(platform),
                        parse_mode='Markdown'
                    )
                    bot.answer_callback_query(call.id)
                    return

                keys = db.query(Key).filter(
                    Key.subscription_id == subscription.id,
                    Key.is_active == True
                ).all()

                if not keys:
                    bot.edit_message_text(
                        "❌ **Ключи не найдены**\n\n"
                        "Попробуйте обновить подписку через /key.",
                        call.message.chat.id,
                        call.message.id,
                        reply_markup=vless_keys_keyboard(platform),
                        parse_mode='Markdown'
                    )
                    bot.answer_callback_query(call.id)
                    return

                # Build list of (server_name, key_data) and sort alphabetically
                key_entries = []
                for i, key in enumerate(keys, 1):
                    if not key.key_data or not key.key_data.startswith("vless://"):
                        continue

                    server_name = f"Сервер {i}"
                    if key.server_id:
                        server = db.query(Server).filter(Server.id == key.server_id).first()
                        if server:
                            server_name = server.name

                    key_entries.append((server_name, key.key_data))

                key_entries.sort(key=lambda e: e[0].lower())

                # Build message
                lines = [f"🔑 **Отдельные VLESS-ключи ({platform_name})**\n"]
                lines.append(
                    "Скопируйте ключ, откройте v2rayTun (или похожий клиент), "
                    "нажмите **+** и выберите **\"Импорт из буфера обмена\"**.\n"
                )

                for server_name, key_data in key_entries:
                    lines.append(f"**{server_name}:**")
                    lines.append(f"`{key_data}`\n")

                message = "\n".join(lines)

                # Telegram message limit is 4096 chars — split into chunks
                # cutting by complete key blocks to avoid broken Markdown
                if len(message) > 4096:
                    chunks = []
                    current = "\n".join(lines[:2])  # header
                    for server_name, key_data in key_entries:
                        block = f"\n**{server_name}:**\n`{key_data}`\n"
                        if len(current) + len(block) > 4000:
                            chunks.append(current)
                            current = block
                        else:
                            current += block
                    if current:
                        chunks.append(current)
                else:
                    chunks = [message]

            bot.edit_message_text(
                chunks[0],
                call.message.chat.id,
                call.message.id,
                reply_markup=vless_keys_keyboard(platform),
                parse_mode='Markdown'
            )
            for chunk in chunks[1:]:
                bot.send_message(
                    call.message.chat.id,
                    chunk,
                    parse_mode='Markdown'
                )
            bot.answer_callback_query(call.id)

        except Exception as e:
            logger.error(f"Error in vless keys callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('outline_key_'))
    def handle_outline_key(call: CallbackQuery):
        """Show user's Outline (legacy) key if available."""
        try:
            platform = call.data.replace('outline_key_', '')

            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == call.from_user.id).first()
                if not user:
                    bot.answer_callback_query(call.id, "Пользователь не найден")
                    return

                # Find any subscription with an Outline key
                outline_keys = (
                    db.query(Key)
                    .join(Subscription)
                    .filter(
                        Subscription.user_id == user.id,
                        Key.protocol == "outline",
                        Key.is_active == True,
                    )
                    .all()
                )

                if not outline_keys:
                    bot.edit_message_text(
                        "❌ **Outline-ключ не найден**\n\n"
                        "У вас нет сохранённого Outline-ключа.\n"
                        "Используйте подписку VLESS для подключения.",
                        call.message.chat.id,
                        call.message.id,
                        reply_markup=outline_key_keyboard(platform),
                        parse_mode='Markdown',
                    )
                    bot.answer_callback_query(call.id)
                    return

                lines = ["🔑 **Outline-ключ (legacy)**\n"]
                for key in outline_keys:
                    lines.append(f"`{key.key_data}`\n")
                lines.append(
                    "⚠️ _Этот ключ работает в Outline/Shadowsocks клиенте. "
                    "Поддержка прекратится в будущем — рекомендуем перейти на VLESS-подписку._"
                )

                message = "\n".join(lines)
                if len(message) > 4096:
                    message = message[:4090] + "\n..."

            bot.edit_message_text(
                message,
                call.message.chat.id,
                call.message.id,
                reply_markup=outline_key_keyboard(platform),
                parse_mode='Markdown',
            )
            bot.answer_callback_query(call.id)

        except Exception as e:
            logger.error(f"Error in outline key callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    logger.info("Client instruction handlers registered")
