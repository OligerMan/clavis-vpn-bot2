"""Tests for the admin 'rotate subscription link' action (user_management_service.rotate_subscription).

Hermetic: isolated temp SQLite DB + a mocked XUIClient, so nothing touches a real panel
or the shared dev database.
"""

from datetime import datetime, timedelta

import pytest

import database.connection as dbconn
from database import (
    init_db,
    get_db_session,
    User,
    Server,
    Subscription,
    Key,
)
from config.settings import ROTATED_GRACE_SUB_NAME

VLESS = "vless://11111111-1111-1111-1111-111111111111@h:443?type=tcp#k"


class _FakeXUI:
    """No-network stand-in for XUIClient used by KeyService (create/expiry/delete)."""

    def __init__(self, server, server_inbound=None):
        self.server = server
        self.server_inbound = server_inbound

    def create_key(self, subscription, client_id, remarks=None, key_number=None,
                   traffic_limit_bytes=0):
        return Key(
            subscription_id=subscription.id,
            server_id=self.server.id,
            protocol="xui",
            remote_key_id=f"clavis_{client_id}_{subscription.id}_s{self.server.id}",
            key_data=VLESS,
            remarks=remarks or self.server.name,
            is_active=True,
        )

    def update_key_expiry(self, key, expiry_ms):
        return True

    def delete_key(self, key):
        return True


@pytest.fixture
def db_user(tmp_path, monkeypatch):
    prev_engine, prev_factory = dbconn._engine, dbconn._SessionLocal
    dbconn._engine = None
    dbconn._SessionLocal = None
    init_db(db_path=str(tmp_path / "rot.db"))

    monkeypatch.setattr("services.key_service.XUIClient", _FakeXUI)

    with get_db_session() as db:
        user = User(telegram_id=555001, username="rot")
        db.add(user)
        db.flush()
        s1 = Server(name="srvA", host="a.example", protocol="xui", is_active=True, server_set="default")
        s2 = Server(name="srvB", host="b.example", protocol="xui", is_active=True, server_set="default")
        db.add_all([s1, s2])
        db.flush()
        sub = Subscription(
            user_id=user.id, name="Main", token="old-token-uuid",
            expires_at=datetime.utcnow() + timedelta(days=100),
            device_limit=5, plan_type="basic", is_active=True,
        )
        db.add(sub)
        db.flush()
        db.add(Key(subscription_id=sub.id, server_id=s1.id, protocol="xui",
                   key_data=VLESS, is_active=True, remote_key_id="oldA"))
        db.add(Key(subscription_id=sub.id, server_id=s2.id, protocol="xui",
                   key_data=VLESS, is_active=True, remote_key_id="oldB"))
    yield 555001
    dbconn._engine, dbconn._SessionLocal = prev_engine, prev_factory


def test_rotate_creates_new_link_and_grace_demotes_old(db_user):
    from services.user_management_service import rotate_subscription

    with get_db_session() as db:
        old = db.query(Subscription).filter(Subscription.token == "old-token-uuid").first()
        old_id, old_expiry, old_server_ids = old.id, old.expires_at, {
            k.server_id for k in db.query(Key).filter(Key.subscription_id == old.id).all()
        }

    with get_db_session() as db:
        ok, msg = rotate_subscription(db, 555001)
    assert ok, msg

    with get_db_session() as db:
        # Old link is dead: token was rotated away.
        assert db.query(Subscription).filter(Subscription.token == "old-token-uuid").first() is None

        # Old sub: inactive, grace-named, ~24h expiry.
        old = db.query(Subscription).filter(Subscription.id == old_id).first()
        assert old.is_active is False
        assert old.name == ROTATED_GRACE_SUB_NAME
        assert timedelta(hours=23) < (old.expires_at - datetime.utcnow()) < timedelta(hours=25)

        # Exactly one active sub now — the new one.
        active = db.query(Subscription).filter(
            Subscription.user_id == db.query(User.id).filter(User.telegram_id == 555001).scalar(),
            Subscription.is_active == True,
            Subscription.expires_at > datetime.utcnow(),
        ).all()
        assert len(active) == 1
        new = active[0]
        assert new.id != old_id
        assert new.token != "old-token-uuid"
        assert new.plan_type == "basic" and new.device_limit == 5
        assert new.expires_at == old_expiry  # inherited remaining duration

        # New keys on the SAME servers as before.
        new_server_ids = {k.server_id for k in db.query(Key).filter(
            Key.subscription_id == new.id, Key.is_active == True).all()}
        assert new_server_ids == old_server_ids

        # Old keys still active (grace window), so they keep working until reaped.
        old_active_keys = db.query(Key).filter(
            Key.subscription_id == old_id, Key.is_active == True).count()
        assert old_active_keys == 2


def test_reaper_deletes_expired_grace_sub(db_user):
    from services.user_management_service import rotate_subscription
    from services.key_service import KeyService

    with get_db_session() as db:
        assert rotate_subscription(db, 555001)[0] is True

    # Force the grace sub expired, then run the reaper's core (mirrors main.reap job).
    with get_db_session() as db:
        g = db.query(Subscription).filter(Subscription.name == ROTATED_GRACE_SUB_NAME).first()
        g.expires_at = datetime.utcnow() - timedelta(minutes=1)

    with get_db_session() as db:
        expired = db.query(Subscription).filter(
            Subscription.name == ROTATED_GRACE_SUB_NAME,
            Subscription.expires_at < datetime.utcnow(),
        ).all()
        for s in expired:
            KeyService.delete_subscription_keys(db, s)
            db.delete(s)

    with get_db_session() as db:
        assert db.query(Subscription).filter(Subscription.name == ROTATED_GRACE_SUB_NAME).count() == 0
        # New sub and its keys survive.
        assert db.query(Subscription).filter(Subscription.is_active == True).count() == 1


def test_rotate_no_active_sub(db_user):
    from services.user_management_service import rotate_subscription
    with get_db_session() as db:
        db.query(Subscription).update({Subscription.is_active: False})
    with get_db_session() as db:
        ok, msg = rotate_subscription(db, 555001)
    assert ok is False
