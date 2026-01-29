# Clavis VPN Bot v2 - Project Context

## Old Project (v1)
- **Framework**: pyTelebot (synchronous)
- **Storage**: CSV files (user_info.csv, transactions_info.csv)
- **VPN**: Outline (Shadowsocks) + VLESS/Reality via X-UI
- **Structure**: Monolithic main.py (~1000 lines)
- **Issues**: Hardcoded credentials, race conditions, no logging

## New Project (v2)
- **Framework**: pyTelebot
- **Storage**: SQLite database
- **VPN**: 3x-ui (VLESS/Reality) primary, Outline for migration only
- **Model**: Subscription-based (not individual keys)
- **Limits**: Time-based subscriptions, 3-5 devices per subscription
- **Features**: Auto-renewal notifications, traffic statistics

## Git Practices
- Branch naming: `feature/`, `fix/`, `refactor/`
- Commit format: `type: short description` (e.g., `feat: add subscription model`)
- Always create PR for review before merging to main
- Never commit credentials or API keys

## Testing
- Unit tests in `tests/` folder
- Test database operations with in-memory SQLite
- Test bot handlers with mocked Telegram API
- Run tests before committing: `pytest tests/`

## Documentation
- `DOCS.md` in every source folder describing files
- Update `PROGRESS.md` after completing features
- Update `WIKI.md` when architecture changes
