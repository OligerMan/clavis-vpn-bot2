"""Inline keyboard markup generators for Telegram bot."""

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Generate main menu keyboard with 2x2 grid layout.

    Buttons:
    - Row 1: Test Key | My Key
    - Row 2: Payment | Status
    - Row 3: Help
    """
    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.row(
        InlineKeyboardButton("🎁 Тестовый ключ", callback_data="get_test_key"),
        InlineKeyboardButton("🔑 Мой ключ", callback_data="get_key")
    )
    keyboard.row(
        InlineKeyboardButton("💳 Оплата", callback_data="payment"),
        InlineKeyboardButton("📊 Статус", callback_data="status")
    )
    keyboard.row(
        InlineKeyboardButton("❓ Помощь", callback_data="help")
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
