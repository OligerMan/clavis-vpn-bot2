"""Clavis app authentication: device tokens, HMAC helpers, FastAPI deps.

Three token namespaces — see plan "Изоляция токенов":

* Opaque device token (random base64url, 43 chars) — stored in `devices.device_token`.
* Telegram device token (deterministic `tg_<tg_id>_<hmac16>`) — recoverable from
  `TG_TOKEN_SECRET` without DB lookup; upserts a `Device(type='telegram')` row.
* Subscription token (UUID) — NOT accepted here; belongs to `subscriptions.token`
  and is only used by `/sub/{token}` and its deep-link wrappers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from config.settings import TG_TOKEN_SECRET
from database.connection import get_session_factory
from database.models import Device

logger = logging.getLogger(__name__)


# ── Opaque device tokens ────────────────────────────────────────

def random_device_token() -> str:
    """Generate a 43-char base64url token (32 random bytes)."""
    return secrets.token_urlsafe(32)


# ── Telegram HMAC device tokens ────────────────────────────────

def _secret_bytes() -> bytes:
    if not TG_TOKEN_SECRET:
        raise RuntimeError(
            "TG_TOKEN_SECRET is not configured — cannot build/verify Telegram device tokens"
        )
    return TG_TOKEN_SECRET.encode()


def telegram_device_token(telegram_id: int) -> str:
    """Deterministic device token for a Telegram user.

    Format: ``tg_<telegram_id>_<hmac16>`` where ``hmac16`` is the first 16 hex
    characters of HMAC-SHA256(TG_TOKEN_SECRET, str(telegram_id)).
    """
    mac = hmac.new(_secret_bytes(), str(telegram_id).encode(), hashlib.sha256)
    return f"tg_{telegram_id}_{mac.hexdigest()[:16]}"


def verify_telegram_token(token: str) -> Optional[int]:
    """Return the Telegram ID if the token is well-formed and HMAC-valid.

    Returns ``None`` on malformed, wrong-prefix, or HMAC-mismatch tokens.
    """
    if not token.startswith("tg_"):
        return None
    try:
        _, tg_id_str, suffix = token.split("_", 2)
        tg_id = int(tg_id_str)
    except (ValueError, IndexError):
        return None
    try:
        expected = hmac.new(_secret_bytes(), str(tg_id).encode(), hashlib.sha256).hexdigest()[:16]
    except RuntimeError:
        # TG_TOKEN_SECRET not configured — reject all tg_* tokens for safety.
        return None
    return tg_id if hmac.compare_digest(suffix, expected) else None


# ── FastAPI session + auth dependencies ────────────────────────

def get_db_dep():
    """FastAPI dependency that yields a DB session and closes it on exit.

    Rolls back on unhandled exception, commits otherwise. Distinct from the
    generator-based context manager ``get_db_session()`` used elsewhere.
    """
    SessionLocal = get_session_factory()
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _get_or_create_telegram_device(db: Session, telegram_id: int, token: str) -> Device:
    """Upsert a Telegram-type Device row for a Telegram user.

    The token is deterministic, so looking up by token is stable across bot
    restarts. We also attach the device to the user's ``ClavisAccount`` when
    one exists — the bot middleware is responsible for creating that row.
    """
    dev = db.query(Device).filter(Device.device_token == token).first()
    now = datetime.utcnow()
    if dev is None:
        # If a ClavisAccount has already been linked to this Telegram user
        # (implicit /start flow), attach the device to it.
        from database.models import User
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        account_id = user.account_id if (user and user.account_id) else None
        dev = Device(
            account_id=account_id,
            device_token=token,
            device_type="telegram",
            device_name=f"Telegram {telegram_id}",
            created_at=now,
            last_seen=now,
        )
        db.add(dev)
        db.flush()
    else:
        dev.last_seen = now
    return dev


async def require_device(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db_dep),
) -> Device:
    """Resolve Bearer → Device. Accepts opaque and Telegram HMAC tokens.

    Ephemeral (account_id IS NULL) devices are allowed — use ``require_full_device``
    when an account is required.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="empty bearer")

    tg_id = verify_telegram_token(token)
    if tg_id is not None:
        return _get_or_create_telegram_device(db, tg_id, token)

    dev = db.query(Device).filter(Device.device_token == token).first()
    if dev is None:
        raise HTTPException(status_code=401, detail="unknown token")
    dev.last_seen = datetime.utcnow()
    return dev


async def require_full_device(device: Device = Depends(require_device)) -> Device:
    """Refuses ephemeral (account_id IS NULL) devices."""
    if device.account_id is None:
        raise HTTPException(status_code=401, detail="ephemeral device not allowed here")
    return device


async def require_ephemeral_device(device: Device = Depends(require_device)) -> Device:
    """Refuses full devices — used by ``/account/create``, ``/login/claim``,
    ``/account/recover`` where an un-attached device is mandatory."""
    if device.account_id is not None:
        raise HTTPException(status_code=409, detail="already_attached")
    return device
