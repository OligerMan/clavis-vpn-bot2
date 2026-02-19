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
