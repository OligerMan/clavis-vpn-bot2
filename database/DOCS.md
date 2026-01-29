# /database - Data Layer

## Purpose
SQLite database models, connection management, and schema migrations.

## Files
| File | Description |
|------|-------------|
| `__init__.py` | Package exports (models + connection functions) |
| `models.py` | SQLAlchemy ORM models (8 tables) |
| `connection.py` | Database engine and session management |
| `migrations/` | Schema migration scripts (future) |

## Models

| Model | Table | Description |
|-------|-------|-------------|
| `User` | users | Telegram users |
| `Subscription` | subscriptions | Time-based access with token for URL |
| `Key` | keys | VPN keys (multiple per subscription) |
| `Server` | servers | VPN server configurations |
| `UserConfig` | user_configs | Per-user routing preferences |
| `RoutingList` | routing_lists | Admin-managed domain lists |
| `TrafficLog` | traffic_logs | Periodic traffic snapshots |
| `Transaction` | transactions | Payment history |

## Usage

```python
from database import init_db, get_db_session, User, Subscription

# Initialize database (creates tables)
init_db()

# Use context manager for sessions
with get_db_session() as db:
    user = User(telegram_id=123456789, username="john")
    db.add(user)
    # Auto-commits on exit

# Query example
with get_db_session() as db:
    user = db.query(User).filter_by(telegram_id=123456789).first()
    print(user.subscriptions)
```

## Testing

```python
from database import init_test_db, User

# In-memory database for tests
engine, TestSession = init_test_db()

with TestSession() as db:
    user = User(telegram_id=1, username="test")
    db.add(user)
    db.commit()
```

## Dependencies
- Internal: None
- External: `sqlalchemy>=2.0`
