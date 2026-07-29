"""Tests for login-merge: account_service.claim_login_token + helpers (spec §11).

Key re-provisioning is no-op'd (best-effort in prod, covered by payment tests); subs are
keyless so grace touches no panels. Focus: subscription/account/device state transitions.
"""

import uuid
from datetime import datetime, timedelta

import pytest

import database.connection as dbconn
from database import (
    init_db, get_db_session, ClavisAccount, Device, User, Subscription, LoginToken,
)


def _now():
    return datetime.utcnow()


@pytest.fixture
def env(tmp_path, monkeypatch):
    prev = (dbconn._engine, dbconn._SessionLocal)
    dbconn._engine = None
    dbconn._SessionLocal = None
    init_db(db_path=str(tmp_path / "merge.db"))
    # Isolate merge logic from key provisioning (best-effort in prod; covered elsewhere).
    monkeypatch.setattr("services.account_service._reprovision_keys", lambda *a, **k: None)
    yield
    dbconn._engine, dbconn._SessionLocal = prev


# ── builders (call inside an open session) ─────────────────────
def mk_account(s):
    a = ClavisAccount(last_active=_now()); s.add(a); s.flush(); return a.id

def mk_user(s, tg, acct):
    u = User(telegram_id=tg, account_id=acct); s.add(u); s.flush(); return u.id

def mk_device(s, acct, install=None):
    d = Device(account_id=acct, device_token="t" + uuid.uuid4().hex,
               device_type="android", install_id=install)
    s.add(d); s.flush(); return d.id

def mk_sub(s, acct, user_id=None, plan="basic", days=30, is_test=False, is_active=True):
    sub = Subscription(account_id=acct, user_id=user_id, plan_type=plan, is_test=is_test,
                       is_active=is_active, expires_at=_now() + timedelta(days=days))
    s.add(sub); s.flush(); return sub.id

def mk_token(s, acct):
    row = LoginToken(token=uuid.uuid4().hex, account_id=acct,
                     created_at=_now(), expires_at=_now() + timedelta(minutes=10))
    s.add(row); s.flush(); return row.token


def _claim(dev_id, tok):
    """Returns (account_id_or_None, error) — id captured in-session to avoid detach."""
    from services.account_service import claim_login_token
    with get_db_session() as s:
        dev = s.query(Device).filter(Device.id == dev_id).first()
        acc, err = claim_login_token(s, dev, tok)
        return (acc.id if acc else None), err


def _days_left(expires):
    return (expires - _now()).total_seconds() / 86400


# ── tests ──────────────────────────────────────────────────────
def test_invalid_token(env):
    with get_db_session() as s:
        dev_id = mk_device(s, None)
    acc, err = _claim(dev_id, "nope")
    assert acc is None and err == "invalid"


def test_ephemeral_device_attaches_no_merge(env):
    with get_db_session() as s:
        tgt = mk_account(s)
        dev_id = mk_device(s, acct=None)  # ephemeral
        tok = mk_token(s, tgt)
    acc_id, err = _claim(dev_id, tok)
    assert err is None and acc_id == tgt
    with get_db_session() as s:
        assert s.query(Device).filter(Device.id == dev_id).first().account_id == tgt


def test_source_with_user_rejected_409(env):
    with get_db_session() as s:
        src = mk_account(s); mk_user(s, 111, src)   # real Telegram account
        tgt = mk_account(s)
        dev_id = mk_device(s, acct=src)
        tok = mk_token(s, tgt)
    acc, err = _claim(dev_id, tok)
    assert acc is None and err == "source_has_account"
    with get_db_session() as s:
        assert s.query(Device).filter(Device.id == dev_id).first().account_id == src  # unchanged
        assert s.query(ClavisAccount).filter(ClavisAccount.id == src).first() is not None


def test_reanchor_when_target_has_no_paid(env):
    with get_db_session() as s:
        src = mk_account(s)                          # throwaway, no user
        src_sub = mk_sub(s, src, plan="basic", days=30)
        tgt = mk_account(s); mk_user(s, 222, tgt)    # TG account, no sub
        dev_id = mk_device(s, acct=src)
        tok = mk_token(s, tgt)
    acc_id, err = _claim(dev_id, tok)
    assert err is None and acc_id == tgt
    with get_db_session() as s:
        sub = s.query(Subscription).filter(Subscription.id == src_sub).first()
        uid = s.query(User).filter(User.telegram_id == 222).first().id
        assert sub.account_id == tgt and sub.user_id == uid and sub.is_active  # re-anchored to TG
        assert s.query(ClavisAccount).filter(ClavisAccount.id == src).first() is None  # abandoned deleted


def test_reanchor_app_only_target_user_id_null(env):
    with get_db_session() as s:
        src = mk_account(s)
        src_sub = mk_sub(s, src, plan="basic", days=30)
        tgt = mk_account(s)                          # app-only target, NO user
        dev_id = mk_device(s, acct=src)
        tok = mk_token(s, tgt)
    acc, err = _claim(dev_id, tok)
    assert err is None
    with get_db_session() as s:
        sub = s.query(Subscription).filter(Subscription.id == src_sub).first()
        assert sub.account_id == tgt and sub.user_id is None  # nullable user_id (Phase 0)


def test_merge_same_tier_sums(env):
    with get_db_session() as s:
        src = mk_account(s); mk_sub(s, src, plan="basic", days=30)
        tgt = mk_account(s); uid = mk_user(s, 333, tgt)
        tgt_sub = mk_sub(s, tgt, user_id=uid, plan="basic", days=40)
        dev_id = mk_device(s, acct=src); tok = mk_token(s, tgt)
    assert _claim(dev_id, tok)[1] is None
    with get_db_session() as s:
        dst = s.query(Subscription).filter(Subscription.id == tgt_sub).first()
        assert 69 < _days_left(dst.expires_at) < 71 and dst.plan_type == "basic"  # 30+40
        graced = s.query(Subscription).filter(Subscription.name == "rotated-grace").all()
        assert len(graced) == 1 and graced[0].is_active is False   # source graced


def test_merge_cross_tier_unlimited_wins(env):
    with get_db_session() as s:
        src = mk_account(s); mk_sub(s, src, plan="basic", days=30)
        tgt = mk_account(s); uid = mk_user(s, 444, tgt)
        tgt_sub = mk_sub(s, tgt, user_id=uid, plan="unlimited", days=20)
        dev_id = mk_device(s, acct=src); tok = mk_token(s, tgt)
    assert _claim(dev_id, tok)[1] is None
    with get_db_session() as s:
        dst = s.query(Subscription).filter(Subscription.id == tgt_sub).first()
        d = _days_left(dst.expires_at)
        assert dst.plan_type == "unlimited"          # highest tier wins
        assert 20 < d < 50                           # 20 + converted(30 basic->unlimited)


def test_all_devices_migrate(env):
    with get_db_session() as s:
        src = mk_account(s); mk_sub(s, src, days=30)
        d1 = mk_device(s, src); d2 = mk_device(s, src)
        tgt = mk_account(s); mk_user(s, 555, tgt)
        tok = mk_token(s, tgt)
    assert _claim(d1, tok)[1] is None
    with get_db_session() as s:
        assert s.query(Device).filter(Device.id == d1).first().account_id == tgt
        assert s.query(Device).filter(Device.id == d2).first().account_id == tgt  # both moved


def test_abandoned_account_deleted_and_free_cleaned(env):
    with get_db_session() as s:
        src = mk_account(s)
        mk_sub(s, src, plan="basic", days=30)        # paid → re-anchored
        free_sub = mk_sub(s, src, plan="free", days=10)  # free → deleted with account
        tgt = mk_account(s); mk_user(s, 666, tgt)
        dev_id = mk_device(s, src); tok = mk_token(s, tgt)
    assert _claim(dev_id, tok)[1] is None
    with get_db_session() as s:
        assert s.query(ClavisAccount).filter(ClavisAccount.id == src).first() is None
        assert s.query(Subscription).filter(Subscription.id == free_sub).first() is None


def test_resolver_prefers_paid_over_later_free(env):
    from services.account_service import _subscription_url_for_account
    with get_db_session() as s:
        acc = mk_account(s)
        paid = mk_sub(s, acc, plan="basic", days=10)    # paid, expires sooner
        mk_sub(s, acc, plan="free", days=100)           # free, expires later
    with get_db_session() as s:
        paid_tok = s.query(Subscription).filter(Subscription.id == paid).first().token
        url = _subscription_url_for_account(s, acc)
        assert url is not None and url.endswith(paid_tok)   # paid wins outright


def test_resolver_falls_back_to_free_when_no_paid(env):
    from services.account_service import _subscription_url_for_account
    with get_db_session() as s:
        acc = mk_account(s)
        free = mk_sub(s, acc, plan="free", days=30)
    with get_db_session() as s:
        free_tok = s.query(Subscription).filter(Subscription.id == free).first().token
        url = _subscription_url_for_account(s, acc)
        assert url is not None and url.endswith(free_tok)


def test_register_dedups_ephemeral_by_install(env):
    from services.account_service import register_ephemeral_device
    with get_db_session() as s:
        register_ephemeral_device(s, "instX", "android", "n1")
    with get_db_session() as s:
        register_ephemeral_device(s, "instX", "android", "n2")   # supersedes the first
    with get_db_session() as s:
        eph = s.query(Device).filter(Device.install_id == "instX", Device.account_id.is_(None)).all()
        assert len(eph) == 1


def test_source_two_paid_is_409(env):
    with get_db_session() as s:
        src = mk_account(s)
        mk_sub(s, src, plan="basic", days=30); mk_sub(s, src, plan="unlimited", days=40)  # anomaly
        tgt = mk_account(s); mk_user(s, 888, tgt)
        dev_id = mk_device(s, src); tok = mk_token(s, tgt)
    acc_id, err = _claim(dev_id, tok)
    assert acc_id is None and err == "multiple_active_subscriptions"
    with get_db_session() as s:
        assert s.query(ClavisAccount).filter(ClavisAccount.id == src).first() is not None  # untouched


def test_target_two_paid_is_409(env):
    with get_db_session() as s:
        src = mk_account(s); mk_sub(s, src, plan="basic", days=30)
        tgt = mk_account(s); uid = mk_user(s, 889, tgt)
        mk_sub(s, tgt, user_id=uid, plan="basic", days=40)
        mk_sub(s, tgt, user_id=uid, plan="unlimited", days=50)  # anomaly on target
        dev_id = mk_device(s, src); tok = mk_token(s, tgt)
    assert _claim(dev_id, tok)[1] == "multiple_active_subscriptions"


def test_reanchor_purges_target_expired(env):
    with get_db_session() as s:
        src = mk_account(s); src_sub = mk_sub(s, src, plan="basic", days=30)
        tgt = mk_account(s); uid = mk_user(s, 890, tgt)
        exp = mk_sub(s, tgt, user_id=uid, plan="basic", days=-5)   # expired
        dev_id = mk_device(s, src); tok = mk_token(s, tgt)
    assert _claim(dev_id, tok)[1] is None
    with get_db_session() as s:
        assert s.query(Subscription).filter(Subscription.id == exp).first() is None  # purged
        assert s.query(Subscription).filter(Subscription.id == src_sub).first().account_id == tgt


def test_migration_dedups_install_collision(env):
    with get_db_session() as s:
        src = mk_account(s); mk_sub(s, src, days=30)
        mk_device(s, src, install="collide")               # non-claiming source device
        d_claim = mk_device(s, src, install="claimer")     # the claiming device
        tgt = mk_account(s); mk_user(s, 892, tgt)
        mk_device(s, tgt, install="collide")               # target already has this install
        tok = mk_token(s, tgt)
    assert _claim(d_claim, tok)[1] is None
    with get_db_session() as s:
        assert s.query(Device).filter(Device.account_id == tgt, Device.install_id == "collide").count() == 1


def test_merge_writes_antifraud_log(env):
    with get_db_session() as s:
        src = mk_account(s); mk_sub(s, src, plan="basic", days=30)
        tgt = mk_account(s); uid = mk_user(s, 891, tgt); mk_sub(s, tgt, user_id=uid, plan="basic", days=40)
        dev_id = mk_device(s, src, install="instABC"); tok = mk_token(s, tgt)
    assert _claim(dev_id, tok)[1] is None
    with get_db_session() as s:
        from database.models import ActivityLog
        logs = s.query(ActivityLog).filter(ActivityLog.action == "login_merge").all()
        assert len(logs) == 1 and "instABC" in logs[0].details


def test_free_only_source_not_merged(env):
    with get_db_session() as s:
        src = mk_account(s); mk_sub(s, src, plan="free", days=30)   # only free → ignored
        tgt = mk_account(s); uid = mk_user(s, 777, tgt)
        tgt_sub = mk_sub(s, tgt, user_id=uid, plan="basic", days=50)
        dev_id = mk_device(s, src); tok = mk_token(s, tgt)
    assert _claim(dev_id, tok)[1] is None
    with get_db_session() as s:
        dst = s.query(Subscription).filter(Subscription.id == tgt_sub).first()
        assert 49 < _days_left(dst.expires_at) < 51   # unchanged — free source not merged
        assert s.query(ClavisAccount).filter(ClavisAccount.id == src).first() is None
