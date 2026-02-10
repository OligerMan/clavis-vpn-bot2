# VPN Module

This module provides API clients for managing VPN keys on 3x-ui panels.

## Files

| File | Description |
|------|-------------|
| `__init__.py` | Module exports |
| `xui_client.py` | Main 3x-ui API client wrapper |
| `xui_models.py` | Data classes and exceptions |
| `xui_uri_builder.py` | VLESS URI construction utilities |

## Quick Start

```python
from database import get_db_session, Server, Subscription
from vpn import XUIClient

with get_db_session() as db:
    server = db.query(Server).filter_by(protocol="xui", is_active=True).first()
    subscription = db.query(Subscription).get(1)

    client = XUIClient(server)
    key = client.create_key(subscription, user_telegram_id=123456789)
    db.add(key)

    print(key.key_data)  # vless://uuid@host:443?...#Clavis VPN
```

## Server Credentials Format

Store in `Server.api_credentials` as JSON:

```json
{
    "username": "admin",
    "password": "secret",
    "inbound_id": 1,
    "use_tls_verify": true,
    "connection_settings": {
        "port": 443,
        "sni": "yahoo.com",
        "pbk": "public_key",
        "sid": "short_id",
        "flow": "xtls-rprx-vision",
        "fingerprint": "chrome"
    }
}
```

## XUIClient Methods

| Method | Description |
|--------|-------------|
| `create_key(subscription, telegram_id)` | Create new VPN key |
| `delete_key(key)` | Delete key from server |
| `get_traffic(key)` | Get traffic statistics |
| `list_clients()` | List all clients on server |
| `health_check()` | Check server connectivity |
| `update_key_expiry(key, new_expiry)` | Update expiration time |
| `enable_key(key)` | Enable a disabled key |
| `disable_key(key)` | Disable key without deleting |

## Exceptions

| Exception | Description |
|-----------|-------------|
| `XUIError` | Base exception for all errors |
| `XUIAuthError` | Authentication failed |
| `XUIConnectionError` | Connection failed |
| `XUIClientNotFoundError` | Client not found on server |
| `XUIInboundError` | Inbound configuration error |

## Data Classes

### TrafficStats
Traffic statistics for a VPN client.
- `email`: Client email
- `upload_bytes`, `download_bytes`, `total_bytes`: Traffic in bytes
- `enabled`: Whether client is enabled
- `expiry_time`: Expiration datetime

### ClientInfo
Information about a VPN client.
- `uuid`: Client UUID
- `email`: Client email
- `enabled`: Whether enabled
- `inbound_id`: Associated inbound ID
- Traffic fields and expiry

### ServerHealth
Health status of a server.
- `is_healthy`: Boolean status
- `version`: Xray version
- `uptime`: Uptime in seconds
- `error_message`: Error if unhealthy

## URI Builder

```python
from vpn import build_vless_uri, parse_vless_uri

# Build URI
uri = build_vless_uri(
    uuid="550e8400-e29b-41d4-a716-446655440000",
    host="vpn.example.com",
    port=443,
    public_key="abc123",
    short_id="def456",
    sni="yahoo.com",
    remark="Clavis VPN",
)

# Parse URI
parsed = parse_vless_uri(uri)
print(parsed["uuid"], parsed["host"], parsed["port"])
```

## Testing

```bash
pytest tests/test_xui_client.py -v
```
