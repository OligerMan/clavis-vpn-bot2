"""VPN module for Clavis VPN Bot v2.

This module provides clients for managing VPN keys on 3x-ui panels.

Usage:
    from vpn import XUIClient, build_vless_uri

    client = XUIClient(server)
    key = client.create_key(subscription, telegram_id=123456)
    print(key.key_data)  # vless://uuid@host:443?...#Clavis VPN
"""

from .xui_client import XUIClient
from .xui_models import (
    ClientInfo,
    ConnectionSettings,
    ServerHealth,
    TrafficStats,
    XUIAuthError,
    XUIClientNotFoundError,
    XUIConnectionError,
    XUIError,
    XUIInboundError,
)
from .xui_uri_builder import build_vless_uri, parse_vless_uri

__all__ = [
    # Main client
    "XUIClient",
    # URI utilities
    "build_vless_uri",
    "parse_vless_uri",
    # Data classes
    "TrafficStats",
    "ClientInfo",
    "ServerHealth",
    "ConnectionSettings",
    # Exceptions
    "XUIError",
    "XUIAuthError",
    "XUIConnectionError",
    "XUIClientNotFoundError",
    "XUIInboundError",
]
