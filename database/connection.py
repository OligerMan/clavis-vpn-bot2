"""Database connection and session management."""

from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from .models import Base


def create_db_engine(database_url: str | None = None, echo: bool = False):
    """Create database engine.

    Args:
        database_url: Database URL. If None, uses DATABASE_URL from config.
        echo: If True, log all SQL statements.

    Returns:
        SQLAlchemy engine instance.
    """
    if database_url is None:
        from config.settings import DATABASE_URL
        database_url = DATABASE_URL

    # For SQLite databases, ensure directory exists
    if database_url.startswith('sqlite:///'):
        # Extract file path from URL (remove 'sqlite:///' prefix)
        db_path = database_url.replace('sqlite:///', '')
        if db_path and db_path != ':memory:':
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    connect_args = {}
    if database_url.startswith('sqlite'):
        # SQLite-specific: allow multi-threaded access
        connect_args["check_same_thread"] = False

    return create_engine(
        database_url,
        echo=echo,
        connect_args=connect_args
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


def init_db(database_url: str | None = None, echo: bool = False):
    """Initialize database: create engine and all tables.

    Args:
        database_url: Database URL. If None, uses DATABASE_URL from config.
        echo: If True, log all SQL statements.
    """
    global _engine, _SessionLocal

    _engine = create_db_engine(database_url, echo)
    _SessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=_engine
    )

    # Create all tables
    Base.metadata.create_all(bind=_engine)

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
