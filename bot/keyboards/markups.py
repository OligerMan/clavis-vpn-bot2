"""Inline keyboard markup generators for Telegram bot."""

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def start_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Generate start menu keyboard (for /start command).

    Buttons:
    - Row 1: Test Key | Payment
    - Row 2: Support
    """
    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.row(
        InlineKeyboardButton("🎁 Тестовый ключ", callback_data="get_test_key"),
        InlineKeyboardButton("💳 Оплата", callback_data="payment")
    )
    keyboard.row(
        InlineKeyboardButton("❓ Поддержка", callback_data="support")
    )

    return keyboard


def full_menu_keyboard(hide_test_key: bool = False, show_old_keys: bool = False) -> InlineKeyboardMarkup:
    """
    Generate full menu keyboard (for back to menu).

    Buttons:
    - Row 1: Test Key (if shown) | My Key
    - Row 2: Payment | Status
    - Row 3: Old Keys (if shown)
    - Row 4: Support

    Args:
        hide_test_key: Whether to hide test key button (if user used test OR has paid subscription)
        show_old_keys: Whether to show old legacy keys button
    """
    keyboard = InlineKeyboardMarkup(row_width=2)

    # First row - Test Key (if not hidden) and My Key
    if not hide_test_key:
        keyboard.row(
            InlineKeyboardButton("🎁 Тестовый ключ", callback_data="get_test_key"),
            InlineKeyboardButton("🔑 Мой ключ", callback_data="get_key")
        )
    else:
        keyboard.row(
            InlineKeyboardButton("🔑 Мой ключ", callback_data="get_key")
        )

    # Second row - Payment and Status
    keyboard.row(
        InlineKeyboardButton("💳 Оплата", callback_data="payment"),
        InlineKeyboardButton("📊 Статус", callback_data="status")
    )

    # Old keys row (only if user has legacy keys)
    if show_old_keys:
        keyboard.row(
            InlineKeyboardButton("📦 Старые ключи", callback_data="old_keys")
        )

    # Support row
    keyboard.row(
        InlineKeyboardButton("❓ Поддержка", callback_data="support")
    )

    return keyboard


def old_keys_keyboard() -> InlineKeyboardMarkup:
    """Generate keyboard for old keys page with back button."""
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )
    return keyboard


def test_key_confirmation_keyboard() -> InlineKeyboardMarkup:
    """
    Generate confirmation keyboard for test key offer.

    Buttons:
    - Получить (confirm)
    - Отмена (cancel)
    """
    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.row(
        InlineKeyboardButton("✅ Получить", callback_data="confirm_test_key"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel")
    )

    return keyboard


def payment_plans_keyboard() -> InlineKeyboardMarkup:
    """
    Generate payment plans keyboard.

    Buttons:
    - 90 дней — 275₽
    - 365 дней — 925₽
    """
    keyboard = InlineKeyboardMarkup(row_width=1)

    keyboard.row(
        InlineKeyboardButton("📅 90 дней — 275₽", callback_data="plan_90")
    )
    keyboard.row(
        InlineKeyboardButton("📅 365 дней — 925₽ (Выгоднее!)", callback_data="plan_365")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")
    )

    return keyboard


def key_actions_keyboard(v2raytun_deeplink: str) -> InlineKeyboardMarkup:
    """
    Keyboard for /key command.

    Buttons:
    - v2rayTun connect (URL button)
    - Install client (platform selection)
    - Back to menu
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("🚀 Подключить v2rayTun", url=v2raytun_deeplink)
    )
    keyboard.row(
        InlineKeyboardButton("📲 Установить клиент", callback_data="show_platforms")
    )
    keyboard.row(
        InlineKeyboardButton("📚 Другие варианты подключения", callback_data="show_platforms_detailed")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def key_platform_keyboard() -> InlineKeyboardMarkup:
    """
    Generate OS selection keyboard for /key flow.

    Buttons:
    - Android | iOS
    - Windows | macOS
    - Back to menu
    """
    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.row(
        InlineKeyboardButton("📱 Android", callback_data="platform_android:key"),
        InlineKeyboardButton("📱 iOS", callback_data="platform_ios:key")
    )
    keyboard.row(
        InlineKeyboardButton("💻 Windows", callback_data="platform_windows:key"),
        InlineKeyboardButton("💻 macOS", callback_data="platform_macos:key")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def platform_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Generate platform selection keyboard.

    Buttons:
    - Android | iOS
    - Windows | macOS
    - Back
    """
    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.row(
        InlineKeyboardButton("📱 Android", callback_data="platform_android"),
        InlineKeyboardButton("📱 iOS", callback_data="platform_ios")
    )
    keyboard.row(
        InlineKeyboardButton("💻 Windows", callback_data="platform_windows"),
        InlineKeyboardButton("💻 macOS", callback_data="platform_macos")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def platform_detailed_menu_keyboard() -> InlineKeyboardMarkup:
    """Platform selection that leads to 'other connection methods' for each platform."""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        InlineKeyboardButton("📱 Android", callback_data="android_detailed"),
        InlineKeyboardButton("📱 iOS", callback_data="ios_detailed")
    )
    keyboard.row(
        InlineKeyboardButton("💻 Windows", callback_data="windows_detailed"),
        InlineKeyboardButton("💻 macOS", callback_data="macos_detailed")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )
    return keyboard


def back_button_keyboard() -> InlineKeyboardMarkup:
    """
    Generate simple back button keyboard.

    Button:
    - Back to menu
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def faq_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Generate FAQ keyboard with support contact and back button."""
    import urllib.parse

    keyboard = InlineKeyboardMarkup()

    support_message = f"Здравствуйте! Мой ID(для поддержки): {telegram_id}. У меня есть вопрос: "
    encoded_message = urllib.parse.quote(support_message)
    support_url = f"https://t.me/Clavis_support2?text={encoded_message}"

    keyboard.row(
        InlineKeyboardButton("💬 Написать в поддержку", url=support_url)
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def status_actions_keyboard() -> InlineKeyboardMarkup:
    """
    Generate status actions keyboard (for users without subscription).

    Buttons:
    - Get Key
    - Renew/Buy
    """
    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.row(
        InlineKeyboardButton("🎁 Попробовать тест", callback_data="get_test_key"),
        InlineKeyboardButton("💳 Купить подписку", callback_data="payment")
    )

    return keyboard


def status_with_sub_keyboard() -> InlineKeyboardMarkup:
    """Status keyboard for users WITH an active subscription."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.row(InlineKeyboardButton("💳 Купить подписку", callback_data="payment"))
    keyboard.row(InlineKeyboardButton("◀️ Вернуться в меню", callback_data="back_to_menu"))
    return keyboard


def payment_help_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """
    Generate payment help keyboard shown after invoice is sent.

    Buttons:
    - Попробовать снова (retry payment)
    - Связаться с поддержкой (URL to support)

    Args:
        telegram_id: User's Telegram ID to include in support message
    """
    import urllib.parse

    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("🔄 Попробовать снова", callback_data="payment")
    )

    support_message = f"Здравствуйте! Мой ID: {telegram_id}. Оплата прошла, но подписка не активирована."
    encoded_message = urllib.parse.quote(support_message)
    support_url = f"https://t.me/Clavis_support2?text={encoded_message}"

    keyboard.row(
        InlineKeyboardButton("💬 Связаться с поддержкой", url=support_url)
    )

    return keyboard


def android_instructions_keyboard(v2raytun_deeplink: str = None, source: str = "key") -> InlineKeyboardMarkup:
    """
    Generate simplified Android instructions keyboard.

    Buttons:
    - Download v2rayTun
    - Connect button (if deeplink provided)
    - Other connection methods
    - Back (to key or support depending on source)
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📥 Скачать v2rayTun", url="https://play.google.com/store/apps/details?id=com.v2raytun.android")
    )

    if v2raytun_deeplink:
        keyboard.row(
            InlineKeyboardButton("🚀 Подключить", url=v2raytun_deeplink)
        )

    keyboard.row(
        InlineKeyboardButton("📚 Другие варианты подключения", callback_data="android_detailed")
    )

    back_callback = "back_to_key" if source == "key" else "show_platforms_support"
    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data=back_callback)
    )

    return keyboard


def ios_instructions_keyboard(v2raytun_deeplink: str = None, source: str = "key") -> InlineKeyboardMarkup:
    """Generate simplified iOS instructions keyboard."""
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📥 Скачать v2rayTun", url="https://apps.apple.com/ru/app/v2raytun/id6476628951")
    )

    if v2raytun_deeplink:
        keyboard.row(
            InlineKeyboardButton("🚀 Подключить", url=v2raytun_deeplink)
        )

    keyboard.row(
        InlineKeyboardButton("📚 Другие варианты подключения", callback_data="ios_detailed")
    )

    back_callback = "back_to_key" if source == "key" else "show_platforms_support"
    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data=back_callback)
    )

    return keyboard


def windows_instructions_keyboard(v2raytun_deeplink: str = None, source: str = "key") -> InlineKeyboardMarkup:
    """Generate simplified Windows instructions keyboard."""
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📥 Скачать v2rayTun", url="https://github.com/mdf45/v2raytun/releases/download/v3.7.10/v2RayTun_Setup.exe")
    )

    if v2raytun_deeplink:
        keyboard.row(
            InlineKeyboardButton("🚀 Подключить", url=v2raytun_deeplink)
        )

    keyboard.row(
        InlineKeyboardButton("📚 Другие варианты подключения", callback_data="windows_detailed")
    )

    back_callback = "back_to_key" if source == "key" else "show_platforms_support"
    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data=back_callback)
    )

    return keyboard


def macos_instructions_keyboard(v2raytun_deeplink: str = None, source: str = "key") -> InlineKeyboardMarkup:
    """Generate simplified macOS instructions keyboard."""
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📥 Скачать v2rayTun", url="https://apps.apple.com/ru/app/v2raytun/id6476628951")
    )

    if v2raytun_deeplink:
        keyboard.row(
            InlineKeyboardButton("🚀 Подключить", url=v2raytun_deeplink)
        )

    keyboard.row(
        InlineKeyboardButton("📚 Другие варианты подключения", callback_data="macos_detailed")
    )

    back_callback = "back_to_key" if source == "key" else "show_platforms_support"
    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data=back_callback)
    )

    return keyboard


def detailed_instructions_keyboard(platform: str) -> InlineKeyboardMarkup:
    """
    Generate keyboard for detailed instructions with back button.

    Args:
        platform: Platform name (android, ios, windows, macos)
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("◀️ Назад к простой инструкции", callback_data=f"platform_{platform}")
    )

    return keyboard


def other_connection_methods_keyboard(platform: str, show_outline: bool = False, back_callback: str = None) -> InlineKeyboardMarkup:
    """
    Generate keyboard for other connection methods menu.

    Args:
        platform: Platform name (android, ios, windows, macos)
        show_outline: Whether to show Outline key button (only if user has legacy keys)
        back_callback: Custom callback for back button (default: platform_{platform})
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📋 Вставить ссылку с подпиской", callback_data=f"clipboard_import_{platform}")
    )
    keyboard.row(
        InlineKeyboardButton("🔑 Отдельные VLESS-ключи", callback_data=f"vless_keys_{platform}")
    )
    if show_outline:
        keyboard.row(
            InlineKeyboardButton("🔑 Outline-ключ (legacy)", callback_data=f"outline_key_{platform}")
        )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data=back_callback or f"platform_{platform}")
    )

    return keyboard


def vless_keys_keyboard(platform: str) -> InlineKeyboardMarkup:
    """
    Generate keyboard for VLESS keys page.

    Args:
        platform: Platform name (android, ios, windows, macos)
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data=f"{platform}_other_methods")
    )

    return keyboard


def outline_key_keyboard(platform: str) -> InlineKeyboardMarkup:
    """
    Generate keyboard for Outline key page.

    Args:
        platform: Platform name (android, ios, windows, macos)
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data=f"{platform}_other_methods")
    )

    return keyboard


def clipboard_import_keyboard(platform: str) -> InlineKeyboardMarkup:
    """
    Generate keyboard for clipboard import instructions.

    Args:
        platform: Platform name (android, ios, windows, macos)
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data=f"{platform}_other_methods")
    )

    return keyboard


def support_actions_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """
    Generate support actions keyboard.

    Buttons:
    - Инструкции по подключению (opens platform selection)
    - FAQ (Частые вопросы)
    - Связаться с поддержкой (URL to @Clavis_support2 with pre-filled message)
    - Назад в меню

    Args:
        telegram_id: User's Telegram ID to include in support message
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📲 Инструкции по подключению", callback_data="show_platforms_support")
    )
    keyboard.row(
        InlineKeyboardButton("❔ Частые вопросы", callback_data="faq")
    )

    # URL to open chat with support with pre-filled message
    import urllib.parse
    support_message = f"Здравствуйте! Мой ID(для поддержки): {telegram_id}. У меня есть вопрос: "
    encoded_message = urllib.parse.quote(support_message)
    support_url = f"https://t.me/Clavis_support2?text={encoded_message}"

    keyboard.row(
        InlineKeyboardButton("💬 Связаться с поддержкой", url=support_url)
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def support_platform_keyboard() -> InlineKeyboardMarkup:
    """
    Generate OS selection keyboard for support flow.

    Buttons:
    - Android | iOS
    - Windows | macOS
    - Back to support
    """
    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.row(
        InlineKeyboardButton("📱 Android", callback_data="platform_android:support"),
        InlineKeyboardButton("📱 iOS", callback_data="platform_ios:support")
    )
    keyboard.row(
        InlineKeyboardButton("💻 Windows", callback_data="platform_windows:support"),
        InlineKeyboardButton("💻 macOS", callback_data="platform_macos:support")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data="back_to_support")
    )

    return keyboard
