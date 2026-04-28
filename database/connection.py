"""Database connection and session management."""

import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from .models import Base

# Default database path (can be overridden via environment variable)
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "clavis.db"


def get_database_url(db_path: str | Path | None = None) -> str:
    """Get SQLite database URL."""
    if db_path is None:
        db_path = os.environ.get("CLAVIS_DB_PATH", DEFAULT_DB_PATH)
    return f"sqlite:///{db_path}"


def create_db_engine(db_path: str | Path | None = None, echo: bool = False):
    """Create database engine.

    Args:
        db_path: Path to SQLite database file. Uses default if not provided.
        echo: If True, log all SQL statements.

    Returns:
        SQLAlchemy engine instance.
    """
    url = get_database_url(db_path)

    # Ensure directory exists
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    return create_engine(
        url,
        echo=echo,
        connect_args={"check_same_thread": False}  # Allow multi-threaded access
    )


# Global engine and session factory (initialized lazily)
_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the global database engine."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory():
    """Get or create the global session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine()
        )
    return _SessionLocal


def _run_migrations(engine):
    """Run schema migrations for existing databases."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)

    # Migration: add yookassa_payment_id to transactions (prevents double payment activation)
    if 'transactions' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('transactions')]
        if 'yookassa_payment_id' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE transactions ADD COLUMN yookassa_payment_id VARCHAR(255)"
                ))
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_transactions_yookassa_payment_id "
                    "ON transactions (yookassa_payment_id)"
                ))
                conn.commit()

    # Migration: add monitor_enabled to servers
    if 'servers' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('servers')]
        if 'monitor_enabled' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE servers ADD COLUMN monitor_enabled BOOLEAN DEFAULT 1"
                ))
                conn.commit()

    # Migration: add server_inbound_id to keys
    if 'keys' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('keys')]
        if 'server_inbound_id' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE keys ADD COLUMN server_inbound_id INTEGER "
                    "REFERENCES server_inbounds(id) ON DELETE SET NULL"
                ))
                conn.commit()

    # Migration: add ref_source to users (referral tracking)
    if 'users' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('users')]
        if 'ref_source' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN ref_source VARCHAR(100)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_users_ref_source ON users (ref_source)"
                ))
                conn.commit()

    # Migration: seed server_groups from existing server_set values
    if 'server_groups' in inspector.get_table_names() and 'servers' in inspector.get_table_names():
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM server_groups")).scalar()
            if count == 0:
                conn.execute(text(
                    "INSERT OR IGNORE INTO server_groups (name) "
                    "SELECT DISTINCT server_set FROM servers "
                    "WHERE server_set IS NOT NULL AND server_set != ''"
                ))
                conn.commit()

    # Migration: add plan_type to subscriptions
    if 'subscriptions' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('subscriptions')]
        if 'plan_type' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE subscriptions ADD COLUMN plan_type VARCHAR(20) DEFAULT 'basic'"
                ))
                conn.commit()

    # Migration: seed ref_links from existing User.ref_source values
    if 'ref_links' in inspector.get_table_names() and 'users' in inspector.get_table_names():
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM ref_links")).scalar()
            if count == 0:
                conn.execute(text(
                    "INSERT OR IGNORE INTO ref_links (tag) "
                    "SELECT DISTINCT ref_source FROM users "
                    "WHERE ref_source IS NOT NULL AND ref_source != ''"
                ))
                conn.commit()

    # Migration: add account_id to users (Clavis app integration)
    if 'users' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('users')]
        if 'account_id' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN account_id VARCHAR(36) "
                    "REFERENCES clavis_accounts(id) ON DELETE SET NULL"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_users_account_id ON users (account_id)"
                ))
                conn.commit()

    # Migration: add account_id to subscriptions (Clavis app integration)
    if 'subscriptions' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('subscriptions')]
        if 'account_id' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE subscriptions ADD COLUMN account_id VARCHAR(36) "
                    "REFERENCES clavis_accounts(id) ON DELETE SET NULL"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_subscriptions_account_id "
                    "ON subscriptions (account_id)"
                ))
                conn.commit()

    # Migration: add fcm_token to devices (push notifications)
    if 'devices' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('devices')]
        if 'fcm_token' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN fcm_token VARCHAR(255)"
                ))
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN fcm_token_updated_at DATETIME"
                ))
                conn.commit()

    # Migration: add fcm_notified_days to subscriptions (FCM push dedup)
    if 'subscriptions' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('subscriptions')]
        if 'fcm_notified_days' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE subscriptions ADD COLUMN fcm_notified_days INTEGER DEFAULT -1"
                ))
                conn.commit()

    # Migration: set fcm_notified_days for existing subscriptions
    if 'subscriptions' in inspector.get_table_names():
        with engine.connect() as conn:
            updated = conn.execute(text(
                "UPDATE subscriptions SET fcm_notified_days = -1 "
                "WHERE fcm_notified_days IS NULL"
            )).rowcount
            if updated:
                conn.execute(text(
                    "UPDATE subscriptions SET fcm_notified_days = 1000000000 "
                    "WHERE account_id IS NOT NULL AND is_active = 1 AND fcm_notified_days = -1"
                ))
                conn.commit()

    # Migration: add install_id to devices (Clavis app dedup — spec §3.11)
    if 'devices' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('devices')]
        if 'install_id' not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN install_id VARCHAR(64)"
                ))
                # Partial index — only rows with install_id set — accelerates
                # the dedup lookup on every promote-to-account operation.
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_devices_account_install "
                    "ON devices (account_id, install_id) WHERE install_id IS NOT NULL"
                ))
                conn.commit()


def init_db(db_path: str | Path | None = None, echo: bool = False):
    """Initialize database: create engine and all tables.

    Args:
        db_path: Path to SQLite database file.
        echo: If True, log all SQL statements.
    """
    global _engine, _SessionLocal

    _engine = create_db_engine(db_path, echo)
    _SessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=_engine
    )

    # Create all tables
    Base.metadata.create_all(bind=_engine)

    # Run migrations for existing tables
    _run_migrations(_engine)

    return _engine


def get_db() -> Session:
    """Get a database session. Remember to close it after use."""
    SessionLocal = get_session_factory()
    return SessionLocal()


@contextmanager
def get_db_session():
    """Context manager for database sessions.

    Usage:
        with get_db_session() as db:
            user = db.query(User).first()
    """
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# For testing: in-memory database
def init_test_db():
    """Initialize an in-memory database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)

    TestSession = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine
    )

    return engine, TestSession
