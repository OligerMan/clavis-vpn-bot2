"""Regression: in-app payment must EXTEND an existing active sub, not create a duplicate.

The duplicate seen on prod (acct 4c259b20) was caused by the active sub having
account_id=NULL, so _activate_subscription (which keys off account_id) couldn't find it.
Today's before_insert account-link event guarantees account_id is set, so this now
correctly extends. This test locks that behaviour.
"""

from datetime import datetime, timedelta

import pytest

import database.connection as dbconn
from database import init_db, get_db_session, User, Subscription, ClavisAccount, Server, Key


class _FakeXUI:
    def __init__(self, server, server_inbound=None):
        self.server = server
        self.server_inbound = server_inbound

    def create_key(self, subscription, client_id, remarks=None, key_number=None, traffic_limit_bytes=0):
        return Key(subscription_id=subscription.id, server_id=self.server.id, protocol="xui",
                   remote_key_id=f"e_{subscription.id}", key_data="vless://x@h:443#k", is_active=True)

    def update_key_expiry(self, key, expiry_ms):
        return True

    def delete_key(self, key):
        return True


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    prev = (dbconn._engine, dbconn._SessionLocal)
    dbconn._engine = None
    dbconn._SessionLocal = None
    init_db(db_path=str(tmp_path / "pay.db"))
    monkeypatch.setattr("services.key_service.XUIClient", _FakeXUI)
    yield
    dbconn._engine, dbconn._SessionLocal = prev


def test_inapp_payment_extends_not_duplicates(tmpdb):
    from services.payment_service import _activate_subscription

    with get_db_session() as db:
        acc = ClavisAccount(last_active=datetime.utcnow())
        db.add(acc)
        db.flush()
        acc_id = acc.id
        user = User(telegram_id=700, account_id=acc_id)
        db.add(user)
        db.flush()
        srv = Server(name="s", host="h", protocol="xui", is_active=True, server_set="default")
        db.add(srv)
        db.flush()
        sub = Subscription(user_id=user.id, account_id=acc_id, name="Main",
                           expires_at=datetime.utcnow() + timedelta(days=30),
                           is_active=True, plan_type="basic")
        db.add(sub)
        db.flush()
        db.add(Key(subscription_id=sub.id, server_id=srv.id, protocol="xui",
                   key_data="vless://x@h:443#k", is_active=True))
        old_exp, sub_id = sub.expires_at, sub.id

    with get_db_session() as db:
        _activate_subscription(db, acc_id, "90d")  # in-app purchase while a sub is active

    with get_db_session() as db:
        subs = db.query(Subscription).filter(Subscription.account_id == acc_id).all()
        assert len(subs) == 1                       # extended, NOT duplicated
        assert subs[0].id == sub_id
        assert subs[0].expires_at > old_exp + timedelta(days=89)  # +90 days
