"""Business logic for Clavis app accounts: create, recover, login, sync, devices.

See spec: F:\\Projects\\clavis-app\\docs\\server-integration-spec.md §3.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from mnemonic import Mnemonic
from sqlalchemy import or_
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


# In-process locks, keyed by name, so concurrent registers (per install_id) and concurrent
# login-claims/merges (per target account) serialize. Each holder commits inside the lock so
# its effect is visible to the next waiter (requests use separate sessions).
_locks_guard = threading.Lock()
_install_locks: dict[str, threading.Lock] = {}
_account_locks: dict[str, threading.Lock] = {}
_user_locks: dict[str, threading.Lock] = {}


def _named_lock(registry: dict, key: str) -> threading.Lock:
    with _locks_guard:
        lk = registry.get(key)
        if lk is None:
            lk = threading.Lock()
            registry[key] = lk
        return lk


def account_lock(account_id: str):
    """Serialize account-level mutations (login-merge, payment activation) in-process."""
    return _named_lock(_account_locks, account_id)


def register_ephemeral_device(db: Session, install_id: Optional[str],
                              device_type: str, device_name: Optional[str]) -> Device:
    """Create an ephemeral (account_id NULL) device. Serialized per install_id and
    supersedes prior ephemeral rows for the same install (the client keeps only its latest
    token). Commits inside the lock so a concurrent register sees the result."""
    from services.auth import random_device_token
    install_id = (install_id or "").strip() or None

    def _do() -> Device:
        if install_id:
            db.query(Device).filter(
                Device.install_id == install_id,
                Device.account_id.is_(None),
            ).delete(synchronize_session=False)
        dev = Device(
            device_token=random_device_token(),
            device_type=device_type,
            device_name=device_name,
            install_id=install_id,
            created_at=_now(),
            last_seen=_now(),
        )
        db.add(dev)
        db.commit()
        db.refresh(dev)
        return dev

    if install_id:
        with _named_lock(_install_locks, install_id):
            return _do()
    return _do()


def _merge_install_id_duplicates(db: Session, caller: Device, target_account_id: str) -> int:
    """Spec §3.11 — before promoting ``caller``, delete other device rows with the same
    install_id that are either on the target account OR still ephemeral (superseded).

    No-op when ``caller.install_id`` is NULL (old clients). Keeps at most one row per
    install_id on the account and leaves no stray ephemeral for that install.
    """
    if not caller.install_id:
        return 0
    removed = (
        db.query(Device)
        .filter(
            or_(Device.account_id == target_account_id, Device.account_id.is_(None)),
            Device.install_id == caller.install_id,
            Device.id != caller.id,
        )
        .delete(synchronize_session=False)
    )
    if removed:
        logger.info(
            f"Install-id merge: removed {removed} stale device(s) for "
            f"install_id={caller.install_id[:8]}... (target {target_account_id})"
        )
    return removed


def _dedup_devices_by_install_id(db: Session, account_id: str) -> int:
    """After migrating devices onto an account, keep at most one row per install_id (the
    most-recently-seen); delete older duplicates. install_id NULL rows are left as-is."""
    from collections import defaultdict
    rows = (
        db.query(Device)
        .filter(Device.account_id == account_id, Device.install_id.isnot(None))
        .all()
    )
    by_install: dict = defaultdict(list)
    for d in rows:
        by_install[d.install_id].append(d)
    removed = 0
    for group in by_install.values():
        if len(group) <= 1:
            continue
        group.sort(key=lambda d: d.last_seen or d.created_at or _now(), reverse=True)
        for d in group[1:]:
            db.delete(d)
            removed += 1
    if removed:
        logger.info(f"Dedup devices on account {account_id}: removed {removed} install-id dup(s)")
    return removed


def _subscription_url_for_account(db: Session, account_id: str) -> Optional[str]:
    """Return the subscription URL for the account's live sub, if any.

    If any PAID (non-free, non-test) sub is active, it wins outright — free/test subs are
    ignored entirely, even if a free sub expires later. Only when there is no paid sub do
    we fall back to a free/test one.
    """
    from config.settings import SUBSCRIPTION_BASE_URL

    live = db.query(Subscription).filter(
        Subscription.account_id == account_id,
        Subscription.is_active == True,  # noqa: E712
        Subscription.expires_at > _now(),
    )
    sub = (
        live.filter(Subscription.is_test == False, Subscription.plan_type != "free")  # noqa: E712
        .order_by(Subscription.expires_at.desc())
        .first()
    )
    if sub is None:  # no paid sub → free/test fallback
        sub = live.order_by(Subscription.expires_at.desc()).first()
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
) -> tuple[Optional[ClavisAccount], Optional[str]]:
    """Claim a login token; switch the device onto the token's account and merge.

    The device may already be attached to a throwaway (userless) app account: it is
    switched, all its devices are migrated, and any active PAID subscription it leaves
    behind is folded into the target account. See server-integration-spec §11.

    Returns ``(account, error)``:
      - ``(account, None)``                    — success
      - ``(None, "invalid")``                  — unknown / expired / already-claimed token
      - ``(None, "source_has_account")``       — device is leaving a real (Telegram-linked)
        account; invalid state resolved by support (HTTP 409).
      - ``(None, "multiple_active_subscriptions")`` — 2+ active paid subs on the source or
        target account; invalid state resolved by support (HTTP 409).
    """
    now = _now()
    row = db.query(LoginToken).filter(LoginToken.token == token).first()
    if row is None or row.expires_at < now or row.claimed_at is not None:
        return None, "invalid"

    # Serialize per target account: closes the double-claim window (B2) and concurrent
    # merges into the same account (B3). Commit inside the lock so the next waiter sees it.
    with _named_lock(_account_locks, row.account_id):
        db.refresh(row)  # re-read under lock in case a concurrent claim just committed
        if row.claimed_at is not None or row.expires_at < now:
            return None, "invalid"

        leaving_account_id = device.account_id
        switching = bool(leaving_account_id) and leaving_account_id != row.account_id

        src_sub = None
        if switching:
            # Only a throwaway (userless) source may be merged; a real TG-linked source is
            # an invalid state for support.
            if db.query(User).filter(User.account_id == leaving_account_id).first() is not None:
                return None, "source_has_account"
            # Anomaly: 2+ active paid subs on either side is invalid — support handles it.
            src_paid = _count_active_paid_subs(db, leaving_account_id)
            if src_paid > 1:
                return None, "multiple_active_subscriptions"
            if src_paid == 1:
                if _count_active_paid_subs(db, row.account_id) > 1:
                    return None, "multiple_active_subscriptions"
                src_sub = _mergeable_subscription_for_account(db, leaving_account_id)

        _merge_install_id_duplicates(db, device, row.account_id)
        row.claimed_at = now
        row.claimed_by_device = device.id
        device.account_id = row.account_id
        device.last_seen = now

        account = db.query(ClavisAccount).filter(ClavisAccount.id == row.account_id).first()
        if account is not None:
            account.last_active = now

        merged = False
        if switching and account is not None:
            _migrate_all_devices(db, leaving_account_id, account.id)
            _dedup_devices_by_install_id(db, account.id)
            if src_sub is not None:
                _carry_subscription_into_account(db, src_sub, account)
                merged = True
            # Persist the re-anchor/grace account_id moves BEFORE selecting rows to delete,
            # so cleanup doesn't re-select the survivor sub (session has autoflush off).
            db.flush()
            _delete_abandoned_account(db, leaving_account_id, account.id)

        if merged:
            _log_merge_for_antifraud(db, device, leaving_account_id, account)

        db.commit()
        return account, None


# ── login-merge helpers (spec §11) ─────────────────────────────

def _mergeable_subscription_for_account(db: Session, account_id: str) -> Optional[Subscription]:
    """Latest active, non-expired, non-free, non-test subscription — the only kind that
    carries real paid value into a login-merge (free/test ignored on both sides)."""
    return (
        db.query(Subscription)
        .filter(
            Subscription.account_id == account_id,
            Subscription.is_active == True,   # noqa: E712
            Subscription.is_test == False,    # noqa: E712
            Subscription.plan_type != "free",
            Subscription.expires_at > _now(),
        )
        .order_by(Subscription.expires_at.desc())
        .first()
    )


def _count_active_paid_subs(db: Session, account_id: str) -> int:
    """Count active, non-expired, non-free, non-test subs. >1 is an anomaly (the invariant
    is at most one paid sub per account)."""
    return (
        db.query(Subscription)
        .filter(
            Subscription.account_id == account_id,
            Subscription.is_active == True,   # noqa: E712
            Subscription.is_test == False,    # noqa: E712
            Subscription.plan_type != "free",
            Subscription.expires_at > _now(),
        )
        .count()
    )


def _purge_expired_subs(db: Session, account_id: str) -> int:
    """Discard expired subs on the account (we never keep them) so a re-anchor leaves a
    single live sub. ORM delete cascades their keys."""
    n = 0
    for sub in (
        db.query(Subscription)
        .filter(Subscription.account_id == account_id, Subscription.expires_at <= _now())
        .all()
    ):
        db.delete(sub)
        n += 1
    return n


def _log_merge_for_antifraud(db: Session, device: Device, source_account_id: str,
                             target_account: ClavisAccount) -> None:
    """Record a subscription-carrying login-merge in activity_logs for fraud analysis
    (many throwaway devices funnelling paid subs into one Telegram account)."""
    from database.activity_log import log_activity
    tg = db.query(User).filter(User.account_id == target_account.id).first()
    tg_id = tg.telegram_id if tg else 0
    inst = (device.install_id or "?")[:12]
    log_activity(
        db, tg_id, "login_merge",
        f"install={inst} src_acct={source_account_id[:8]} dst_acct={target_account.id[:8]}",
    )


def _reprovision_keys(db: Session, sub: Subscription, user: Optional[User]) -> None:
    """Re-provision keys / expiry for the surviving sub. Best-effort (panel errors caught
    + logged), so a login-merge is never rolled back by a temporarily-down panel."""
    from services.payment_service import _ensure_keys_and_expiry
    _ensure_keys_and_expiry(db, sub, user)


def _migrate_all_devices(db: Session, from_account_id: str, to_account_id: str) -> int:
    """Move every device of the abandoned account onto the target account."""
    n = (
        db.query(Device)
        .filter(Device.account_id == from_account_id)
        .update({Device.account_id: to_account_id, Device.last_seen: _now()},
                synchronize_session=False)
    )
    if n:
        logger.info(f"login-merge: migrated {n} device(s) {from_account_id}->{to_account_id}")
    return n


def _carry_subscription_into_account(db: Session, src_sub: Subscription, dst_account: ClavisAccount) -> None:
    """Fold the leaving account's active paid sub (``src_sub``) into ``dst_account``.

    - target has no paid sub -> re-anchor src onto the target account (+ its user).
    - both have a paid sub    -> highest tier wins; the lower sub's remaining time converts
      into the winner (PLAN_CONVERSION_RATES, ceil in the user's favour); same tier -> sum.
      The survivor is the target-anchored sub; the app-side src sub gets a 24h key grace.
    """
    from datetime import timedelta
    from services.plan_math import PLAN_RANK, convert_remaining_days
    from services.subscription_grace import grace_demote_subscription

    now = _now()
    _purge_expired_subs(db, dst_account.id)  # discard target's expired subs (never kept)
    dst_user = db.query(User).filter(User.account_id == dst_account.id).first()
    dst_sub = _mergeable_subscription_for_account(db, dst_account.id)

    # (1) target has no paid sub -> re-anchor the source sub onto the target identity.
    if dst_sub is None:
        src_sub.account_id = dst_account.id
        src_sub.user_id = dst_user.id if dst_user else None
        _reprovision_keys(db, src_sub, dst_user)
        return
    if dst_sub.id == src_sub.id:
        return

    # (2) both active paid -> merge INTO dst_sub (survivor), grace the source.
    src_days = max(0.0, (src_sub.expires_at - now).total_seconds() / 86400)
    dst_days = max(0.0, (dst_sub.expires_at - now).total_seconds() / 86400)
    src_rank = PLAN_RANK.get(src_sub.plan_type, 0)
    dst_rank = PLAN_RANK.get(dst_sub.plan_type, 0)

    if src_rank == dst_rank:                        # same tier -> sum time
        total_days = dst_days + src_days
        winner = dst_sub.plan_type
    elif dst_rank > src_rank:                       # target higher -> convert src up
        total_days = dst_days + convert_remaining_days(src_days, src_sub.plan_type, dst_sub.plan_type)
        winner = dst_sub.plan_type
    else:                                           # src higher -> upgrade target sub
        total_days = src_days + convert_remaining_days(dst_days, dst_sub.plan_type, src_sub.plan_type)
        winner = src_sub.plan_type

    dst_sub.plan_type = winner
    dst_sub.expires_at = now + timedelta(days=total_days)
    dst_sub.is_active = True
    dst_sub.reset_reminder_flags()

    grace_demote_subscription(db, src_sub)          # app-side keys live 24h, then reaped
    _reprovision_keys(db, dst_sub, dst_user)


def _delete_abandoned_account(db: Session, account_id: str, target_account_id: str) -> None:
    """Delete the leaving account after its devices + paid sub have moved.

    Guard: never delete an account that still has a linked User (would orphan a real
    Telegram user — the caller already rejects that, double-checked here). In-app
    payments are reassigned to the survivor (audit follows the money); leftover free/test
    subs (+ their keys via ORM cascade) and any support chats are deleted; the account's
    login tokens go via ORM cascade. The graced paid sub already has account_id=None.
    """
    from database.models import AppPayment, SupportChat

    if db.query(User).filter(User.account_id == account_id).first() is not None:
        logger.warning(f"login-merge: account {account_id} still has a User — not deleting")
        return

    db.query(AppPayment).filter(AppPayment.account_id == account_id).update(
        {AppPayment.account_id: target_account_id}, synchronize_session=False)
    for sub in db.query(Subscription).filter(Subscription.account_id == account_id).all():
        db.delete(sub)                              # ORM cascade removes its keys
    for chat in db.query(SupportChat).filter(SupportChat.account_id == account_id).all():
        db.delete(chat)                             # ORM cascade removes its messages
    acc = db.query(ClavisAccount).filter(ClavisAccount.id == account_id).first()
    if acc is not None:
        db.delete(acc)                              # ORM cascade removes its login tokens
    logger.info(f"login-merge: deleted abandoned account {account_id}")


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

    # Serialize per Telegram user: two concurrent /start (threaded telebot) must not each
    # create an account. Commit inside the lock so the next waiter sees the link.
    with _named_lock(_user_locks, str(user.telegram_id)):
        db.refresh(user)
        if user.account_id is not None:
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
        db.commit()
        db.refresh(account)
        return account
