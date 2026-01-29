# /vpn - VPN Management

## Purpose
VPN server API clients and subscription logic.

## Files
| File | Description |
|------|-------------|
| xui_client.py | 3x-ui panel API client (primary protocol) |
| outline_client.py | Outline server API client (compatibility) |
| subscription.py | Subscription creation, renewal, expiration |

## Dependencies
- Internal: database.models
- External: requests, httpx
