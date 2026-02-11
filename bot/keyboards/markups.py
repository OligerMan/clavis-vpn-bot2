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


def full_menu_keyboard(hide_test_key: bool = False) -> InlineKeyboardMarkup:
    """
    Generate full menu keyboard (for back to menu).

    Buttons:
    - Row 1: Test Key (if shown) | My Key
    - Row 2: Payment | Status
    - Row 3: Support

    Args:
        hide_test_key: Whether to hide test key button (if user used test OR has paid subscription)
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

    # Third row - Support
    keyboard.row(
        InlineKeyboardButton("❓ Поддержка", callback_data="support")
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
    - 90 дней - 175₽
    - 365 дней - 600₽
    """
    keyboard = InlineKeyboardMarkup(row_width=1)

    keyboard.row(
        InlineKeyboardButton("📅 90 дней - 175₽", callback_data="plan_90")
    )
    keyboard.row(
        InlineKeyboardButton("📅 365 дней - 600₽ (Выгоднее!)", callback_data="plan_365")
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


def payment_confirmation_keyboard(transaction_id: int) -> InlineKeyboardMarkup:
    """
    Generate payment confirmation keyboard with mock payment button.

    Buttons:
    - Оплатить (simulates successful payment)

    Args:
        transaction_id: Transaction ID to confirm
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("💳 Оплатить", callback_data=f"mock_pay_{transaction_id}")
    )

    return keyboard


def android_instructions_keyboard() -> InlineKeyboardMarkup:
    """
    Generate Android instructions keyboard with download buttons.

    Buttons:
    - Google Play
    - GitHub
    - Клиент скачан, добавить подписку
    - Назад в меню
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📥 Google Play", url="https://play.google.com/store/apps/details?id=com.v2raytun.android"),
        InlineKeyboardButton("📥 GitHub", url="https://github.com/DigneZzZ/v2raytun/releases")
    )
    keyboard.row(
        InlineKeyboardButton("✅ Клиент скачан, добавить подписку", callback_data="add_subscription_to_client")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def ios_instructions_keyboard() -> InlineKeyboardMarkup:
    """
    Generate iOS instructions keyboard with download buttons.

    Buttons:
    - App Store (v2rayTun)
    - Клиент скачан, добавить подписку
    - Назад в меню
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📥 Скачать v2rayTun (App Store)", url="https://apps.apple.com/ru/app/v2raytun/id6476628951")
    )
    keyboard.row(
        InlineKeyboardButton("✅ Клиент скачан, добавить подписку", callback_data="add_subscription_to_client")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def windows_instructions_keyboard() -> InlineKeyboardMarkup:
    """
    Generate Windows instructions keyboard with download button.

    Buttons:
    - GitHub (v2rayTun)
    - Клиент скачан, добавить подписку
    - Назад в меню
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📥 Скачать v2rayTun (GitHub)", url="https://github.com/mdf45/v2raytun/releases")
    )
    keyboard.row(
        InlineKeyboardButton("✅ Клиент скачан, добавить подписку", callback_data="add_subscription_to_client")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def macos_instructions_keyboard() -> InlineKeyboardMarkup:
    """
    Generate macOS instructions keyboard with download button.

    Buttons:
    - App Store (v2rayTun)
    - Клиент скачан, добавить подписку
    - Назад в меню
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("📥 Скачать v2rayTun (App Store)", url="https://apps.apple.com/ru/app/v2raytun/id6476628951")
    )
    keyboard.row(
        InlineKeyboardButton("✅ Клиент скачан, добавить подписку", callback_data="add_subscription_to_client")
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard


def support_actions_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """
    Generate support actions keyboard.

    Buttons:
    - FAQ (Частые вопросы)
    - Инструкции по подключению
    - Связаться с поддержкой (URL to @clavis_support with pre-filled message)
    - Назад в меню

    Args:
        telegram_id: User's Telegram ID to include in support message
    """
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton("❔ Частые вопросы", callback_data="faq")
    )
    keyboard.row(
        InlineKeyboardButton("📲 Инструкции по подключению", callback_data="show_platforms")
    )

    # URL to open chat with support with pre-filled message
    import urllib.parse
    support_message = f"Здравствуйте! Мой ID(для поддержки): {telegram_id}. У меня есть вопрос: "
    encoded_message = urllib.parse.quote(support_message)
    support_url = f"https://t.me/clavis_support?text={encoded_message}"

    keyboard.row(
        InlineKeyboardButton("💬 Связаться с поддержкой", url=support_url)
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")
    )

    return keyboard
