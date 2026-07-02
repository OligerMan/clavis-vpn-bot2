"""Tests for the free-VPN bootstrap endpoint (GET /app/free-vpn/{install_id}).

Hermetic: each test runs against an isolated temp SQLite DB and a mocked XUIClient,
so nothing hits a real x-ui panel or the shared dev database. See FREE_VPN_BOOTSTRAP_PLAN.md.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import database.connection as dbconn
from database import (
    init_db,
    get_db_session,
    Server,
    ConnectionProfile,
    ServerInbound,
    Subscription,
    Key,
    BootstrapGrant,
)
from config.settings import BOOTSTRAP_SUB_NAME
from subscription.app import create_app

# A fake WS VLESS link returned by the mocked panel — note type=ws (not tcp+reality).
WS_VLESS = (
    "vless://550e8400-e29b-41d4-a716-446655440000@cl23.example.com:443"
    "?security=tls&encryption=none&type=ws&host=cl23.example.com&path=%2Fws"
    "#Clavis%20Free%20(login)"
)

VALID_ID = "abcdEFGH12345678_-xy"  # 20 chars, base64url charset


class _FakeXUI:
    """Stand-in for vpn.xui_client.XUIClient — no network, returns a WS key."""

    def __init__(self, server, server_inbound=None):
        self.server = server
        self.server_inbound = server_inbound

    def create_key(self, subscription, client_id, remarks=None, traffic_limit_bytes=0):
        return Key(
            subscription_id=subscription.id,
            server_id=self.server.id,
            server_inbound_id=self.server_inbound.id if self.server_inbound else None,
            protocol="xui",
            remote_key_id=f"clavis_{client_id}_{subscription.id}",
            key_data=WS_VLESS,
            remarks=remarks or "key",
            is_active=True,
        )

    def delete_key(self, key):  # used by the reaper path
        return True


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated temp DB seeded with one active Free-group WS server, plus mocked XUIClient."""
    # Swap the global engine/session factory to a throwaway DB, restore afterwards.
    prev_engine, prev_factory = dbconn._engine, dbconn._SessionLocal
    dbconn._engine = None
    dbconn._SessionLocal = None
    init_db(db_path=str(tmp_path / "boot.db"))

    with get_db_session() as db:
        profile = ConnectionProfile(
            name="ws-profile", protocol="vless", security="tls",
            network="ws", sni="cl23.example.com",
        )
        db.add(profile)
        db.flush()
        server = Server(
            name="Free-1", host="cl23.example.com", protocol="xui",
            is_active=True, server_set="Free",
        )
        db.add(server)
        db.flush()
        db.add(ServerInbound(
            server_id=server.id, profile_id=profile.id, inbound_id=3,
            port=443, public_key="pbk", short_id="sid",
        ))

    # Mock both call sites: the endpoint (create) and the reaper (delete).
    monkeypatch.setattr("vpn.xui_client.XUIClient", _FakeXUI)
    monkeypatch.setattr("services.key_service.XUIClient", _FakeXUI)

    yield TestClient(create_app())

    dbconn._engine, dbconn._SessionLocal = prev_engine, prev_factory


def test_first_call_issues_ws_link(client):
    r = client.get(f"/app/free-vpn/{VALID_ID}")
    assert r.status_code == 200
    link = r.json()["link"]
    assert link.startswith("vless://")
    assert "type=ws" in link  # WS, not tcp+reality

    with get_db_session() as db:
        subs = db.query(Subscription).filter(Subscription.name == BOOTSTRAP_SUB_NAME).all()
        assert len(subs) == 1
        assert subs[0].account_id is None
        assert subs[0].user_id is not None  # shared bootstrap guest owner (prod user_id is NOT NULL)
        assert subs[0].expiry_notified is True  # suppresses expiry-reminder noise
        # Short TTL, not the 3-day referral lifetime.
        assert subs[0].expires_at - datetime.utcnow() < timedelta(hours=2)


def test_second_call_reuses_live_key(client):
    r1 = client.get(f"/app/free-vpn/{VALID_ID}")
    r2 = client.get(f"/app/free-vpn/{VALID_ID}")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["link"] == r2.json()["link"]
    # Reuse must not mint a second sub.
    with get_db_session() as db:
        assert db.query(Subscription).filter(
            Subscription.name == BOOTSTRAP_SUB_NAME
        ).count() == 1


def test_bootstrap_subs_share_one_guest_user(client):
    from database import User
    from config.settings import BOOTSTRAP_GUEST_TELEGRAM_ID

    assert client.get("/app/free-vpn/aaaaaaaaaaaaaaaa11").status_code == 200
    assert client.get("/app/free-vpn/bbbbbbbbbbbbbbbb22").status_code == 200
    with get_db_session() as db:
        subs = db.query(Subscription).filter(Subscription.name == BOOTSTRAP_SUB_NAME).all()
        assert len(subs) == 2
        assert len({s.user_id for s in subs}) == 1  # all funnel to one guest
        guests = db.query(User).filter(User.telegram_id == BOOTSTRAP_GUEST_TELEGRAM_ID).all()
        assert len(guests) == 1  # not one guest per install
        assert subs[0].user_id == guests[0].id


def test_rate_limited_after_window(client):
    r1 = client.get(f"/app/free-vpn/{VALID_ID}")
    assert r1.status_code == 200
    # Expire the issued sub so reuse won't short-circuit; the issuance timestamp
    # still counts against the per-window limit (default 1 / 10 min).
    with get_db_session() as db:
        sub = db.query(Subscription).filter(Subscription.name == BOOTSTRAP_SUB_NAME).first()
        sub.expires_at = datetime.utcnow() - timedelta(minutes=1)

    r2 = client.get(f"/app/free-vpn/{VALID_ID}")
    assert r2.status_code == 429
    assert r2.json() == {"error": "rate_limited"}


def test_rate_limit_can_be_disabled(client, monkeypatch):
    # Flag is read at call time via `from config.settings import ...`, so this takes effect.
    monkeypatch.setattr("config.settings.FREE_VPN_BOOTSTRAP_RATE_LIMIT_ENABLED", False)
    iid = "DISABLEDtest12345678"
    assert client.get(f"/app/free-vpn/{iid}").status_code == 200
    # Expire so reuse won't short-circuit; with limits OFF the 2nd issuance must still 200
    # (would be 429 with limits ON, per test_rate_limited_after_window).
    with get_db_session() as db:
        for s in db.query(Subscription).filter(Subscription.name == BOOTSTRAP_SUB_NAME).all():
            s.expires_at = datetime.utcnow() - timedelta(minutes=1)
    assert client.get(f"/app/free-vpn/{iid}").status_code == 200


@pytest.mark.parametrize("bad", ["!!!", "short123", "tooooo" * 20, "has/slash1234567"])
def test_bad_install_id_rejected(client, bad):
    r = client.get(f"/app/free-vpn/{bad}")
    # "has/slash..." becomes a different path entirely (404); the rest fail validation (400).
    assert r.status_code in (400, 404)


def test_no_free_server_returns_503(client):
    # Deactivate the only Free server → no WS inbound available.
    with get_db_session() as db:
        db.query(Server).update({Server.is_active: False})
    r = client.get(f"/app/free-vpn/{VALID_ID}")
    assert r.status_code == 503


def test_reaper_deletes_expired_bootstrap(client):
    # Issue a key, then force it expired.
    assert client.get(f"/app/free-vpn/{VALID_ID}").status_code == 200
    with get_db_session() as db:
        sub = db.query(Subscription).filter(Subscription.name == BOOTSTRAP_SUB_NAME).first()
        sub.expires_at = datetime.utcnow() - timedelta(minutes=1)

    # Mirror reap_bootstrap_subscriptions_job (main.py) — can't import main (needs BOT_TOKEN).
    from services.key_service import KeyService
    with get_db_session() as db:
        expired = db.query(Subscription).filter(
            Subscription.name == BOOTSTRAP_SUB_NAME,
            Subscription.expires_at < datetime.utcnow(),
        ).all()
        for s in expired:
            KeyService.delete_subscription_keys(db, s)
            db.delete(s)

    with get_db_session() as db:
        assert db.query(Subscription).filter(Subscription.name == BOOTSTRAP_SUB_NAME).count() == 0
        assert db.query(Key).count() == 0
        # The grant ledger is intentionally retained (observability for abusive ids).
        assert db.query(BootstrapGrant).count() == 1
