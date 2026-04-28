"""Configuration settings loader for Clavis VPN Bot v2."""

import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Bot Token
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

# Telegram Payment Provider Token (YooKassa via Telegram Payments)
TELEGRAM_PAYMENT_TOKEN = os.getenv('TELEGRAM_PAYMENT_TOKEN', '')

# YooKassa API credentials (for payment verification)
YOOKASSA_SHOP_ID = os.getenv('YOOKASSA_SHOP_ID', '')
YOOKASSA_SECRET_KEY = os.getenv('YOOKASSA_SECRET_KEY', '')

# Subscription Base URL
SUBSCRIPTION_BASE_URL = os.getenv('SUBSCRIPTION_BASE_URL', 'https://vpn.example.com')

# Database URL
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///clavis_vpn.db')

# Admin Telegram IDs
ADMIN_IDS: List[int] = [
    int(id_str.strip())
    for id_str in os.getenv('ADMIN_IDS', '').split(',')
    if id_str.strip()
]

# Stars payment toggle (set to True to enable Stars payments)
STARS_ENABLED = False

# Payment Plans (amount in kopeks for card, whole stars for Stars)
PLANS: Dict[str, Dict[str, any]] = {
    '90_days': {
        'days': 90,
        'amount': 27500,  # 275 rubles in kopeks
        'stars_amount': 150,
        'price_display': '275₽',
        'stars_display': '150⭐',
        'description': '3 месяца',
        'plan_type': 'basic',
    },
    '365_days': {
        'days': 365,
        'amount': 92500,  # 925 rubles in kopeks
        'stars_amount': 500,
        'price_display': '925₽',
        'stars_display': '500⭐',
        'description': '1 год',
        'plan_type': 'basic',
    },
    'premium_90_days': {
        'days': 90,
        'amount': 55000,  # 550 rubles in kopeks
        'stars_amount': 300,
        'price_display': '550₽',
        'stars_display': '300⭐',
        'description': '3 месяца Premium',
        'plan_type': 'premium',
    },
    'premium_365_days': {
        'days': 365,
        'amount': 185000,  # 1850 rubles in kopeks
        'stars_amount': 1000,
        'price_display': '1850₽',
        'stars_display': '1000⭐',
        'description': '1 год Premium',
        'plan_type': 'premium',
    },
}

# Premium feature: which user IDs see premium plan buttons
PREMIUM_TESTERS = [331186567]

# Server groups accessible by plan type
# "basic" gets all groups NOT listed in premium_groups
# "premium" gets ALL groups (basic + premium)
PREMIUM_GROUPS = {"Test Premium"}

# Free VPN plan configuration
FREE_GROUP_NAME = "Free"  # Server group name for free-tier servers
FREE_SUBSCRIPTION_DAYS = 3650  # ~10 years (effectively permanent)
FREE_AD_TEXT = (
    "Для быстрого VPN и обхода белых списков:\n"
    "👉 @clavis_vpn_bot"
)

# Free (referral) VPN plan — server group name and duration for referred users
FREE_GROUP_NAME = "Free"       # ServerGroup name for referral/free-tier servers
REFERRAL_SUBSCRIPTION_DAYS = 3  # Duration for a referred friend's free subscription

# Ad lines appended to free subscription content (shown as server names in VPN clients)
# Each line becomes a separate non-connectable entry visible in the server list.
FREE_AD_LINES = [
    "Хочешь быстрый VPN?",
    "С обходом белых списков",
    "t.me/clavis_vpn_bot",
]

# Test subscription duration (hours)
TEST_SUBSCRIPTION_HOURS = 48

# Device limit for subscriptions
DEVICE_LIMIT = 5

# Max number of servers a user gets keys on (lazy init)
USER_SERVER_LIMIT = int(os.getenv('USER_SERVER_LIMIT', '2'))

# X-UI panel credentials
XUI_USERNAME = os.getenv('XUI_USERNAME', '')
XUI_PASSWORD = os.getenv('XUI_PASSWORD', '')

# Moscow timezone (UTC+3)
MSK = timezone(timedelta(hours=3))


def format_msk(dt: datetime, fmt: str = '%d.%m.%Y %H:%M') -> str:
    """Format a naive UTC datetime as Moscow time (UTC+3)."""
    return dt.replace(tzinfo=timezone.utc).astimezone(MSK).strftime(fmt) + ' МСК'


# Subscription server settings
SUBSCRIPTION_PORT = int(os.getenv('SUBSCRIPTION_PORT', 8080))
SUBSCRIPTION_CACHE_TTL = int(os.getenv('SUBSCRIPTION_CACHE_TTL', 300))
SUBSCRIPTION_CACHE_SIZE = int(os.getenv('SUBSCRIPTION_CACHE_SIZE', 1000))

# ── Clavis app integration ────────────────────────────────────
# During rollout, all new account/sync/login features are gated
# to this single developer. When the integration is validated,
# drop the gate in bot/middlewares/user_registration.py and /start.
MAIN_DEVELOPER_ID = 331186567

# HMAC secret for deterministic Telegram-device tokens of the form
# `tg_<telegram_id>_<hmac16>`. MUST be set in .env in production — random
# 32+ bytes. Rotating invalidates every Telegram session; users re-auth.
TG_TOKEN_SECRET = os.getenv('TG_TOKEN_SECRET', '')

# Argon2id tuning for recovery-phrase hashing (see services/auth.py)
FCM_CREDENTIALS_PATH = os.getenv('FCM_CREDENTIALS_PATH', '')

ARGON2_TIME_COST = int(os.getenv('ARGON2_TIME_COST', '3'))
ARGON2_MEMORY_COST = int(os.getenv('ARGON2_MEMORY_COST', '65536'))  # 64 MiB
ARGON2_PARALLELISM = int(os.getenv('ARGON2_PARALLELISM', '2'))

# In-app payment plans (direct YooKassa API, not Telegram Payments).
# plan_id → {days, amount (RUB string), description, plan_type}.
APP_PLANS = {
    "90d": {
        "days": 90,
        "amount": "275.00",
        "description": "Clavis VPN — 90 days",
        "plan_type": "basic",
    },
    "365d": {
        "days": 365,
        "amount": "925.00",
        "description": "Clavis VPN — 365 days",
        "plan_type": "basic",
    },
}
