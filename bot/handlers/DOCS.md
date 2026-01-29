# /bot/handlers - Command Handlers

## Purpose
Telegram command and callback query handlers.

## Files
| File | Description |
|------|-------------|
| user.py | User commands (/start, /status, /key, /help) |
| admin.py | Admin commands (/admin, /check, /broadcast) |
| payment.py | Payment flow and confirmation handlers |

## Dependencies
- Internal: database.models, vpn.subscription, bot.keyboards
- External: pyTelebot
