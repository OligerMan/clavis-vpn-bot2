# Clavis VPN Bot v2 - Wiki

## Architecture

### Project Structure
```
clavis-vpn-bot2/
├── main.py                 # Entry point
├── config/
│   └── settings.json       # Configuration (no secrets!)
├── database/
│   ├── models.py           # SQLite models
│   ├── connection.py       # DB connection
│   └── migrations/         # Schema migrations
├── bot/
│   ├── handlers/
│   │   ├── user.py         # User commands
│   │   ├── admin.py        # Admin commands
│   │   └── payment.py      # Payment handling
│   ├── keyboards/
│   │   └── markups.py      # Keyboard layouts
│   └── middlewares/        # Request processing
├── vpn/
│   ├── xui_client.py       # 3x-ui API client (primary)
│   ├── outline_client.py   # Outline client (compatibility)
│   └── subscription.py     # Subscription URL endpoint
├── services/
│   ├── scheduler.py        # Background tasks (traffic logging, reminders)
│   ├── traffic.py          # Traffic stats collection
│   └── migration.py        # v1 data migration
├── routing/
│   └── lists/              # Domain lists (ru_bypass.txt, etc.)
└── message_templates/      # Bot messages
```

---

## Database Schema

### Users
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| telegram_id | INTEGER | Telegram user ID (unique) |
| username | TEXT | Telegram username (nullable) |
| created_at | DATETIME | Registration date |

### Subscriptions
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| user_id | INTEGER | FK → Users |
| name | TEXT | Display name ("Main", "Test") |
| token | TEXT | UUID for subscription URL (unique) |
| expires_at | DATETIME | Expiration timestamp |
| device_limit | INTEGER | Max devices (default 5) |
| is_test | BOOLEAN | Test subscription flag |
| is_active | BOOLEAN | Active status |
| created_at | DATETIME | Creation date |

### Keys
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| subscription_id | INTEGER | FK → Subscriptions |
| server_id | INTEGER | FK → Servers |
| protocol | TEXT | 'xui' or 'outline' |
| remote_key_id | TEXT | Key ID on VPN server (for API calls) |
| key_data | TEXT | Full key URI (vless://..., ss://...) |
| remarks | TEXT | Display name for this key |
| is_active | BOOLEAN | Active status |
| created_at | DATETIME | Creation date |

### Servers
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| name | TEXT | Display name ("Frankfurt", "Amsterdam") |
| host | TEXT | Server hostname/IP |
| protocol | TEXT | 'xui' or 'outline' |
| api_url | TEXT | API endpoint URL |
| api_credentials | TEXT | Encrypted JSON (login, password, etc.) |
| capacity | INTEGER | Max users (for load balancing) |
| is_active | BOOLEAN | Server available for new keys |

### UserConfigs
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| user_id | INTEGER | FK → Users (unique) |
| bypass_domains | TEXT | JSON array - direct connection |
| blocked_domains | TEXT | JSON array - block completely |
| proxied_domains | TEXT | JSON array - force proxy |
| enabled_lists | TEXT | JSON array of RoutingList IDs |
| updated_at | DATETIME | Last modification |

### RoutingLists
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| name | TEXT | Identifier ("ru_bypass", "ads_block") |
| display_name | TEXT | UI name ("Russian sites", "Ad blocker") |
| type | TEXT | 'bypass' / 'block' / 'proxy' |
| domains | TEXT | JSON array of domains |
| is_default | BOOLEAN | Auto-enable for new users |
| updated_at | DATETIME | Last modification |

**Default routing lists:**
| Name | Type | Description |
|------|------|-------------|
| ru_bypass | bypass | Russian sites direct (banks, government, local services) |
| ads_block | block | Advertising and tracking domains |
| ru_blocked_proxy | proxy | Sites blocked in Russia (force through VPN) |

### TrafficLogs
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| key_id | INTEGER | FK → Keys |
| upload_bytes | INTEGER | Upload since last snapshot |
| download_bytes | INTEGER | Download since last snapshot |
| recorded_at | DATETIME | Snapshot timestamp |

*Indexed by (key_id, recorded_at) for time-range queries. Used for abuse detection, not enforcement.*

### Transactions
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| user_id | INTEGER | FK → Users |
| subscription_id | INTEGER | FK → Subscriptions (nullable) |
| amount | INTEGER | Amount in kopeks |
| plan | TEXT | Plan name ("90_days", "365_days") |
| status | TEXT | 'pending' / 'completed' / 'failed' |
| created_at | DATETIME | Transaction initiated |
| completed_at | DATETIME | Transaction completed (nullable) |

---

## Subscription URL System

### Endpoint
```
GET https://vpn.example.com/sub/{token}
```

### Response Body
```
vless://...@server1.example.com:443?...#Frankfurt
vless://...@server2.example.com:443?...#Amsterdam
ss://...#Outline-Legacy
```

### Response Headers
| Header | Value | Description |
|--------|-------|-------------|
| profile-title | `base64:Q2xhdmlzIFZQTg==` | "Clavis VPN" |
| subscription-userinfo | `upload=0; download=0; expire=1749954800` | No traffic limit, expiry only |
| profile-update-interval | `12` | Update every 12 hours |
| routing | `base64:{routing_json}` | Domain routing rules |
| announce | Reminder text (if applicable) | Renewal notifications |
| announce-url | `https://t.me/clavis_vpn_bot` | Link to bot |

### Renewal Reminders via Announce Header
| Days until expiry | Announce message |
|-------------------|------------------|
| 7 days | "Subscription expires in 7 days. Renew now!" |
| 3 days | "Only 3 days left! Tap to renew." |
| 1 day | "Last day! Your VPN expires tomorrow." |
| Expired | "Subscription expired. Renew to restore access." |

---

## VPN Protocols

### 3x-ui (Primary)
- VLESS + Reality protocol
- Better obfuscation against DPI
- Used for all new subscriptions
- Multiple keys per user supported

### Outline (Compatibility)
- Shadowsocks-based
- Used only for migrated v1 users
- Keys replaced with 3x-ui on renewal

---

## Test → Paid Transition

```
1. User requests test:
   → Create Subscription(is_test=true, expires=+24h)
   → Create Key on available server
   → Return subscription URL

2. User pays:
   → Update existing subscription:
      - is_test = false
      - expires_at = now + plan_days
   → Replace key on server (new remote_key_id)
   → Same subscription URL continues working
```

---

## Bot Commands

### User Commands
| Command | Description |
|---------|-------------|
| /start | Welcome + registration |
| /status | Subscription status + traffic stats |
| /subscribe | Get subscription URL (for v2raytun) |
| /payment | Payment options |
| /settings | Routing preferences |
| /instruction | Setup guide |
| /help | Help message |

### Admin Commands
| Command | Description |
|---------|-------------|
| /admin | Admin panel |
| /check <id> | Check user info + traffic |
| /servers | Server status + load |
| /broadcast | Send message to all |
| /stats | Usage statistics |
| /abuse | Show suspicious usage patterns |

---

## Payment Plans
- 90 days: 175 RUB
- 365 days: 600 RUB

---

## Traffic Abuse Detection

Scheduler runs daily analysis:
```python
# Flag conditions (configurable):
- Download > 500 GB / 30 days
- Upload/Download ratio > 0.5 (unusual for VPN)
- Traffic spikes (10x normal in 24h)
```

Admin notified via Telegram when flagged.

---

## Migration from v1
1. Export CSV data (user_info.csv, transactions_info.csv)
2. Run migration script
3. Create subscriptions for existing users
4. Map Outline keys to compatibility mode
5. New subscriptions use 3x-ui only
