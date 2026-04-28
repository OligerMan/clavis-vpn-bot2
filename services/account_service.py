"""Business logic for Clavis app accounts: create, recover, login, sync, devices.

See spec: F:\\Projects\\clavis-app\\docs\\server-integration-spec.md §3.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from mnemonic import Mnemonic
from sqlalchemy.orm import Session

from config.settings import (
    ARGON2_MEMORY_COST,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    TG_TOKEN_SECRET,
)
from database.models import (
    ClavisAccount,
    Device,
    LoginToken,
    Subscription,
    SyncPair,
    User,
)

logger = logging.getLogger(__name__)

_LOGIN_TTL = timedelta(minutes=10)
_SYNC_TTL = timedelta(minutes=2)
_MAX_CLAIM_ATTEMPTS = 5
_PAIR_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_hasher = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST,
    parallelism=ARGON2_PARALLELISM,
)

_mnemonic = Mnemonic("english")


# ── helpers ────────────────────────────────────────────────────

def _normalize_phrase(phrase: list[str]) -> str:
    return " ".join(w.strip().lower() for w in phrase)


def _phrase_lookup_key(phrase_text: str) -> str:
    """Keyed HMAC of the phrase — indexed shortcut so /recover is O(log n)."""
    if not TG_TOKEN_SECRET:
        # Fall back to a secret-less hash; still a namespace, just not keyed.
        return hashlib.sha256(phrase_text.encode()).hexdigest()[:32]
    return hmac.new(TG_TOKEN_SECRET.encode(), phrase_text.encode(), hashlib.sha256).hexdigest()[:32]


def _login_token_value() -> str:
    """64-char random base64url, distinct namespace from subscription UUIDs."""
    return secrets.token_urlsafe(48)


def _pair_code() -> str:
    return "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(8))


def _now() -> datetime:
    return datetime.utcnow()


def _merge_install_id_duplicates(db: Session, caller: Device, target_account_id: str) -> int:
    """Spec §3.11 — delete older device rows with the same install_id on the
    target account before promoting ``caller``.

    No-op when ``caller.install_id`` is NULL (old clients). Returns the number
    of rows removed. Safe to call in every promote path (create / recover /
    login-claim / sync-claim) — keeps at most one row per (install_id, account).
    """
    if not caller.install_id:
        return 0
    removed = (
        db.query(Device)
        .filter(
            Device.account_id == target_account_id,
            Device.install_id == caller.install_id,
            Device.id != caller.id,
        )
        .delete(synchronize_session=False)
    )
    if removed:
        logger.info(
            f"Install-id merge: removed {removed} stale device(s) on account "
            f"{target_account_id} with install_id={caller.install_id[:8]}..."
        )
    return removed


def _subscription_url_for_account(db: Session, account_id: str) -> Optional[str]:
    """Return the subscription URL for the account's latest active sub, if any."""
    from config.settings import SUBSCRIPTION_BASE_URL

    sub = (
        db.query(Subscription)
        .filter(
            Subscription.account_id == account_id,
            Subscription.is_active == True,  # noqa: E712
            Subscription.expires_at > _now(),
        )
        .order_by(Subscription.expires_at.desc())
        .first()
    )
    if sub is None:
        return None
    return f"{SUBSCRIPTION_BASE_URL.rstrip('/')}/sub/{sub.token}"


# ── account lifecycle ─────────────────────────────────────────

def create_account(db: Session, device: Device) -> tuple[ClavisAccount, list[str]]:
    """Create an account + attach to the given (ephemeral) device.

    Returns the new account and the BIP-39 recovery phrase words (shown once).
    Caller must ensure the device is ephemeral — ``require_ephemeral_device``
    handles that at the FastAPI layer.
    """
    # Generate BIP-39 12-word phrase server-side (128-bit entropy).
    phrase = _mnemonic.generate(strength=128).split()
    phrase_text = _normalize_phrase(phrase)

    account = ClavisAccount(
        recovery_phrase_hash=_hasher.hash(phrase_text),
        phrase_lookup_key=_phrase_lookup_key(phrase_text),
        last_active=_now(),
    )
    db.add(account)
    db.flush()

    # Spec §3.11 — drop any stale rows sharing this install_id on the new account.
    # Harmless on a brand-new account (there shouldn't be any) but kept for
    # symmetry with the other promote paths.
    _merge_install_id_duplicates(db, device, account.id)
    device.account_id = account.id
    device.last_seen = _now()
    return account, phrase


def recover_account(
    db: Session, device: Device, phrase: list[str]
) -> Optional[ClavisAccount]:
    """Look up account by phrase; on match, link the device and return account.

    Returns None on no-match. Caller is expected to map None → 401.
    Device must be ephemeral (``require_ephemeral_device``).
    """
    phrase_text = _normalize_phrase(phrase)
    if not _mnemonic.check(phrase_text):
        return None
    lookup = _phrase_lookup_key(phrase_text)

    candidates = (
        db.query(ClavisAccount)
        .filter(ClavisAccount.phrase_lookup_key == lookup)
        .filter(ClavisAccount.recovery_phrase_hash.isnot(None))
        .all()
    )
    matched: Optional[ClavisAccount] = None
    for account in candidates:
        try:
            _hasher.verify(account.recovery_phrase_hash, phrase_text)
            matched = account
            break
        except VerifyMismatchError:
            continue

    if matched is None:
        return None
    _merge_install_id_duplicates(db, device, matched.id)
    device.account_id = matched.id
    device.last_seen = _now()
    matched.last_active = _now()
    return matched


# ── login tokens (cross-device login initiated from Telegram) ─

def create_login_token(db: Session, account_id: str) -> LoginToken:
    """Allocate a one-time login token for the given account. 10-min TTL."""
    now = _now()
    row = LoginToken(
        token=_login_token_value(),
        account_id=account_id,
        created_at=now,
        expires_at=now + _LOGIN_TTL,
    )
    db.add(row)
    db.flush()
    return row


def claim_login_token(
    db: Session, device: Device, token: str
) -> Optional[ClavisAccount]:
    """Claim a login token. Device must be ephemeral. Returns the linked account.

    Returns None if the token is unknown / expired / already claimed.
    """
    now = _now()
    row = (
        db.query(LoginToken)
        .filter(LoginToken.token == token)
        .first()
    )
    if row is None:
        return None
    if row.expires_at < now:
        return None
    if row.claimed_at is not None:
        return None

    _merge_install_id_duplicates(db, device, row.account_id)
    row.claimed_at = now
    row.claimed_by_device = device.id
    device.account_id = row.account_id
    device.last_seen = now

    account = db.query(ClavisAccount).filter(ClavisAccount.id == row.account_id).first()
    if account is not None:
        account.last_active = now
    return account


# ── sync pairs (show-code ↔ scan-code) ─────────────────────────

def create_sync_pair(db: Session, shower: Device) -> SyncPair:
    """Allocate a new pair code owned by the shower device. 2-min TTL."""
    now = _now()
    for _ in range(5):  # retry on improbable collision
        code = _pair_code()
        exists = db.query(SyncPair).filter(SyncPair.pair_code == code).first()
        if exists is None:
            break
    else:
        raise RuntimeError("could not allocate pair code")

    pair = SyncPair(
        pair_code=code,
        shower_device=shower.id,
        status="pending",
        created_at=now,
        expires_at=now + _SYNC_TTL,
    )
    db.add(pair)
    db.flush()
    return pair


def claim_sync_pair(
    db: Session, claimer: Device, pair_code: str
) -> tuple[str, Optional[ClavisAccount]]:
    """Run the sync state machine.

    Returns (outcome, account). ``outcome`` is one of:
      - ``"claimed_from_shower"`` — claimer got the shower's account (account is the shower's).
      - ``"claimed_shower"`` — shower will get the claimer's account (account is the claimer's).
      - ``"already_linked"`` — both on same account (idempotent no-op).
      - ``"both_empty"``, ``"both_full"``, ``"cross_account"`` — error outcomes.
      - ``"expired"`` — pair is gone.
      - ``"rate_limited"`` — more than 5 attempts.
    """
    now = _now()
    pair = db.query(SyncPair).filter(SyncPair.pair_code == pair_code).first()
    if pair is None or pair.expires_at < now:
        return "expired", None

    pair.claim_attempts = (pair.claim_attempts or 0) + 1
    if pair.claim_attempts > _MAX_CLAIM_ATTEMPTS:
        pair.status = "error"
        pair.error_reason = "rate_limited"
        return "rate_limited", None

    if pair.status in ("claimed", "error", "expired"):
        return "expired", None

    shower = db.query(Device).filter(Device.id == pair.shower_device).first()
    if shower is None:
        pair.status = "error"
        pair.error_reason = "both_empty"
        return "expired", None

    # Prevent self-claim (scanning your own QR).
    if shower.id == claimer.id:
        pair.status = "error"
        pair.error_reason = "cross_account"
        return "cross_account", None

    s_full = shower.account_id is not None
    c_full = claimer.account_id is not None

    if not s_full and not c_full:
        pair.status = "error"
        pair.error_reason = "both_empty"
        return "both_empty", None

    if s_full and c_full:
        if shower.account_id == claimer.account_id:
            pair.status = "claimed"
            pair.claimer_device = claimer.id
            return "already_linked", None
        pair.status = "error"
        pair.error_reason = "cross_account"
        return "cross_account", None

    # One ephemeral, one full — promote the ephemeral.
    if s_full and not c_full:
        _merge_install_id_duplicates(db, claimer, shower.account_id)
        claimer.account_id = shower.account_id
        claimer.last_seen = now
        account = db.query(ClavisAccount).filter(ClavisAccount.id == shower.account_id).first()
        if account is not None:
            account.last_active = now
        pair.status = "claimed"
        pair.claimer_device = claimer.id
        return "claimed_from_shower", account

    # c_full and not s_full
    _merge_install_id_duplicates(db, shower, claimer.account_id)
    shower.account_id = claimer.account_id
    shower.last_seen = now
    account = db.query(ClavisAccount).filter(ClavisAccount.id == claimer.account_id).first()
    if account is not None:
        account.last_active = now
    pair.status = "claimed"
    pair.claimer_device = claimer.id
    return "claimed_shower", account


def get_sync_status(db: Session, pair_code: str, shower: Device) -> Optional[dict]:
    """Shower polls this. Returns None if pair unknown or caller isn't the shower."""
    pair = db.query(SyncPair).filter(SyncPair.pair_code == pair_code).first()
    if pair is None or pair.shower_device != shower.id:
        return None

    now = _now()
    if pair.status == "pending" and pair.expires_at < now:
        pair.status = "expired"

    out: dict = {"status": pair.status}
    if pair.status == "error" and pair.error_reason:
        out["error_reason"] = pair.error_reason
    return out


# ── account views & device management ──────────────────────────

def get_account_me(db: Session, device: Device) -> dict:
    """Build the payload for GET /account/me."""
    from config.settings import SUBSCRIPTION_BASE_URL

    account = db.query(ClavisAccount).filter(ClavisAccount.id == device.account_id).first()
    devices = (
        db.query(Device)
        .filter(Device.account_id == device.account_id)
        .order_by(Device.created_at.asc())
        .all()
    )
    device_list = [
        {
            "device_id": d.id,
            "device_type": d.device_type,
            "device_name": d.device_name,
            "last_seen": int(d.last_seen.replace(tzinfo=None).timestamp()) if d.last_seen else 0,
            "is_current": d.id == device.id,
        }
        for d in devices
    ]

    sub_url = _subscription_url_for_account(db, device.account_id)

    return {
        "account_id": device.account_id,
        "device_count": len(device_list),
        "devices": device_list,
        "subscription_url": sub_url or "",
        "created_at": int(account.created_at.replace(tzinfo=None).timestamp()) if account else 0,
    }


def revoke_device(db: Session, actor: Device, target_device_id: str) -> tuple[bool, Optional[str]]:
    """Delete ``target_device_id`` if it belongs to the actor's account.

    Returns (ok, error). Error values: ``"not_found"`` (404), ``"cannot_revoke_telegram"`` (403).
    """
    target = db.query(Device).filter(Device.id == target_device_id).first()
    if target is None or target.account_id != actor.account_id:
        return False, "not_found"
    if target.device_type == "telegram":
        return False, "cannot_revoke_telegram"
    db.delete(target)
    return True, None


# ── Telegram implicit account (bot-side) ───────────────────────

def upsert_telegram_device_for_user(db: Session, user: User) -> Device:
    """Ensure the given Telegram user has a Device row linked to their account.

    Requires ``user.account_id`` to be set. Idempotent.
    """
    from services.auth import telegram_device_token

    assert user.account_id, "upsert_telegram_device_for_user needs user.account_id set"
    token = telegram_device_token(user.telegram_id)
    now = _now()

    dev = db.query(Device).filter(Device.device_token == token).first()
    if dev is None:
        dev = Device(
            account_id=user.account_id,
            device_token=token,
            device_type="telegram",
            device_name=f"Telegram @{user.username or user.telegram_id}",
            created_at=now,
            last_seen=now,
        )
        db.add(dev)
        db.flush()
    else:
        dev.account_id = user.account_id
        dev.last_seen = now
    return dev


def ensure_implicit_account(db: Session, user: User) -> Optional[ClavisAccount]:
    """Create a ClavisAccount + Telegram device for a Telegram user if missing.

    Also retroactively links any of the user's active subscriptions that don't
    yet have ``account_id``. Called from the bot middleware on /start; gated
    to ``MAIN_DEVELOPER_ID`` during rollout.
    """
    if user.account_id is not None:
        # Already attached — still make sure the device row exists.
        upsert_telegram_device_for_user(db, user)
        return db.query(ClavisAccount).filter(ClavisAccount.id == user.account_id).first()

    account = ClavisAccount(last_active=_now())  # recovery_phrase_hash stays NULL
    db.add(account)
    db.flush()
    user.account_id = account.id

    # Back-link existing subscriptions that have no account yet.
    subs = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.account_id.is_(None))
        .all()
    )
    for s in subs:
        s.account_id = account.id

    upsert_telegram_device_for_user(db, user)
    return account
