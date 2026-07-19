"""Tests for auto-linking a subscription to its user's Clavis account.

Covers the before_insert event (all new subs) and the extend/reactivate branches of
SubscriptionService.create_or_extend_paid_subscription (updates the event can't catch).
"""

from datetime import datetime, timedelta

import pytest

import database.connection as dbconn
from database import (
    init_db, get_db_session, User, Subscription, ClavisAccount,
)


@pytest.fixture
def tmpdb(tmp_path):
    prev_engine, prev_factory = dbconn._engine, dbconn._SessionLocal
    dbconn._engine = None
    dbconn._SessionLocal = None
    init_db(db_path=str(tmp_path / "acctlink.db"))
    yield
    dbconn._engine, dbconn._SessionLocal = prev_engine, prev_factory


def test_new_sub_autolinks_to_user_account(tmpdb):
    with get_db_session() as db:
        acc = ClavisAccount(last_active=datetime.utcnow())
        db.add(acc)
        db.flush()
        acc_id = acc.id
        user = User(telegram_id=101, username="withacct", account_id=acc_id)
        db.add(user)
        db.flush()

        sub = Subscription(user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=30), is_active=True)
        db.add(sub)
        db.flush()
        assert sub.account_id == acc_id  # auto-linked by the before_insert event


def test_guest_user_without_account_is_noop(tmpdb):
    with get_db_session() as db:
        user = User(telegram_id=202, username="noacct")  # account_id NULL
        db.add(user)
        db.flush()
        sub = Subscription(user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=30), is_active=True)
        db.add(sub)
        db.flush()
        assert sub.account_id is None  # nothing to link → harmless no-op


def test_explicit_account_id_not_overwritten(tmpdb):
    with get_db_session() as db:
        acc = ClavisAccount(last_active=datetime.utcnow())
        db.add(acc)
        db.flush()
        acc_id = acc.id
        # user has NO account, but the sub explicitly sets account_id (e.g. in-app payment)
        user = User(telegram_id=303, username="appuser")
        db.add(user)
        db.flush()
        sub = Subscription(user_id=user.id, account_id=acc_id,
                           expires_at=datetime.utcnow() + timedelta(days=30), is_active=True)
        db.add(sub)
        db.flush()
        assert sub.account_id == acc_id  # explicit value preserved


def test_extend_links_old_unlinked_active_sub(tmpdb):
    from services.subscription_service import SubscriptionService
    with get_db_session() as db:
        acc = ClavisAccount(last_active=datetime.utcnow())
        db.add(acc)
        db.flush()
        acc_id = acc.id
        user = User(telegram_id=404, account_id=acc_id)
        db.add(user)
        db.flush()
        # Simulate a legacy active sub created before linking (bypass event via account_id set,
        # then null it directly to mimic the historical state).
        sub = Subscription(user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=10), is_active=True)
        db.add(sub)
        db.flush()
        sub.account_id = None  # force the pre-fix state
        db.flush()
        assert sub.account_id is None

        uid = user.id

    with get_db_session() as db:
        user = db.query(User).filter(User.id == uid).first()
        SubscriptionService.create_or_extend_paid_subscription(db, user, days=30, transaction_id=0)

    with get_db_session() as db:
        subs = db.query(Subscription).filter(Subscription.user_id == uid).all()
        assert len(subs) == 1  # extended, not new
        assert subs[0].account_id == acc_id  # now linked by the extend branch
