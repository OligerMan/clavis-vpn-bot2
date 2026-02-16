"""Configuration settings loader for Clavis VPN Bot v2."""

import os
from typing import List, Dict
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Bot Token
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

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

# Payment Plans (amount in kopeks)
PLANS: Dict[str, Dict[str, any]] = {
    '90_days': {
        'days': 90,
        'amount': 17500,  # 175 rubles in kopeks
        'price_display': '175₽',
        'description': '3 месяца'
    },
    '365_days': {
        'days': 365,
        'amount': 60000,  # 600 rubles in kopeks
        'price_display': '600₽',
        'description': '1 год'
    }
}

# Test subscription duration (hours)
TEST_SUBSCRIPTION_HOURS = 48

# Device limit for subscriptions
DEVICE_LIMIT = 5

# Max number of servers a user gets keys on (lazy init)
USER_SERVER_LIMIT = int(os.getenv('USER_SERVER_LIMIT', '2'))

# Subscription server settings
SUBSCRIPTION_PORT = int(os.getenv('SUBSCRIPTION_PORT', 8080))
SUBSCRIPTION_CACHE_TTL = int(os.getenv('SUBSCRIPTION_CACHE_TTL', 300))
SUBSCRIPTION_CACHE_SIZE = int(os.getenv('SUBSCRIPTION_CACHE_SIZE', 1000))
