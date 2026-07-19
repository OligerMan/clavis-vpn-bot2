"""Test the account + login-token path behind the 'Add to Clavis app' menu button.

The bot callback (handle_clavis_applink) is thin UI wiring; this exercises the service
logic it runs (ensure_implicit_account + create_login_token) on an isolated temp DB.
"""

from datetime import datetime, timedelta

import pytest

import database.connection as dbconn
from database import init_db, get_db_session, User, Server, Subscription, Key


@pytest.fixture
def tmpdb(tmp_path):
    prev_engine, prev_factory = dbconn._engine, dbconn._SessionLocal
    dbconn._engine = None
    dbconn._SessionLocal = None
    init_db(db_path=str(tmp_path / "app.db"))
    with get_db_session() as s:
        u = User(telegram_id=900123, username="appu")
        s.add(u)
        s.flush()
        sub = Subscription(
            user_id=u.id, name="Main", token="tok-app-1",
            expires_at=datetime.utcnow() + timedelta(days=30), is_active=True,
        )
        s.add(sub)
    yield 900123
    dbconn._engine, dbconn._SessionLocal = prev_engine, prev_factory


def test_applink_creates_account_backlinks_sub_and_mints_token(tmpdb):
    from services.account_service import ensure_implicit_account, create_login_token
    from config.settings import SUBSCRIPTION_BASE_URL

    with get_db_session() as db:
        user = db.query(User).filter(User.telegram_id == 900123).first()
        assert user.account_id is None  # starts unlinked

        account = ensure_implicit_account(db, user)
        assert account is not None
        assert user.account_id == account.id  # user now linked

        # Existing subscription retroactively linked to the account.
        sub = db.query(Subscription).filter(Subscription.token == "tok-app-1").first()
        assert sub.account_id == account.id

        row = create_login_token(db, account.id)
        assert row.account_id == account.id
        assert row.claimed_at is None
        # 10-minute single-use TTL.
        assert timedelta(minutes=8) < (row.expires_at - datetime.utcnow()) < timedelta(minutes=12)
        assert len(row.token) >= 32

        login_url = f"{SUBSCRIPTION_BASE_URL.rstrip('/')}/login/{row.token}"
        assert login_url.endswith(f"/login/{row.token}")
        assert "/login/" in login_url


def test_applink_idempotent_account(tmpdb):
    from services.account_service import ensure_implicit_account, create_login_token

    with get_db_session() as db:
        user = db.query(User).filter(User.telegram_id == 900123).first()
        a1_id = ensure_implicit_account(db, user).id
    with get_db_session() as db:
        user = db.query(User).filter(User.telegram_id == 900123).first()
        a2_id = ensure_implicit_account(db, user).id
        assert a2_id == a1_id  # no duplicate account on repeat

        # Each call mints a fresh, distinct token.
        t1 = create_login_token(db, a2_id).token
        t2 = create_login_token(db, a2_id).token
        assert t1 != t2
