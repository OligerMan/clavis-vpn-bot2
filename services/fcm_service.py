"""FCM push notification service for subscription expiry reminders."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from config.settings import FCM_CREDENTIALS_PATH

logger = logging.getLogger(__name__)

_fcm_ready = False

_THRESHOLDS = [7, 3, 1]
_INITIAL_VALUE = 1_000_000_000
_DORMANT_VALUE = -1

_STALE_TOKEN_DAYS = 60
_EXPIRED_NOTIFY_WINDOW_DAYS = 7


def _pluralize_days(n: int) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 19:
        return "дней"
    mod10 = n % 10
    if mod10 == 1:
        return "день"
    if 2 <= mod10 <= 4:
        return "дня"
    return "дней"


def _make_notification(days_left: int) -> tuple[str, str]:
    if days_left == 1:
        return "Подписка истекает завтра", "Продлите подписку сегодня."
    return (
        f"Подписка истекает через {days_left} {_pluralize_days(days_left)}",
        "Продлите подписку, чтобы не потерять доступ.",
    )


def init_fcm() -> bool:
    """Initialize Firebase Admin SDK. Returns True on success."""
    global _fcm_ready
    if _fcm_ready:
        return True
    if not FCM_CREDENTIALS_PATH:
        logger.warning("FCM_CREDENTIALS_PATH not set, push notifications disabled")
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials
        cred = credentials.Certificate(FCM_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
        _fcm_ready = True
        logger.info("Firebase Admin SDK initialized")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")
        return False


def _send_push(token: str, title: str, body: str) -> Optional[str]:
    """Send a single FCM notification. Returns message ID or None on failure.

    Returns 'invalid_token' when the token is stale/unregistered so the
    caller can clear it from the database.
    """
    try:
        from firebase_admin import messaging
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="default",
                    priority="high",
                ),
            ),
            token=token,
        )
        return messaging.send(msg)
    except Exception as e:
        err_str = str(e)
        if "Requested entity was not found" in err_str or "not a valid FCM" in err_str or "UNREGISTERED" in err_str:
            return "invalid_token"
        logger.warning(f"FCM send failed: {e}")
        return None


def send_expiry_notifications() -> None:
    """Check all account-linked subscriptions and send FCM push where due.

    Uses a threshold-based algorithm: _THRESHOLDS = [7, 3, 1].
    For each active sub, iterates thresholds top-down:
      - days_left > threshold → break (too early)
      - fcm_notified_days > threshold → send, record days_left, break
      - else → already notified for this threshold, continue

    Expired subs get a single push when fcm_notified_days != -1.
    """
    if not _fcm_ready and not init_fcm():
        return

    from database.connection import get_db_session
    from database.models import Device, Subscription

    now = datetime.utcnow()
    stale_cutoff = now - timedelta(days=_STALE_TOKEN_DAYS)

    with get_db_session() as db:
        _gc_stale_tokens(db, stale_cutoff)

        active_subs = (
            db.query(Subscription)
            .filter(
                Subscription.account_id.isnot(None),
                Subscription.is_active == True,
            )
            .all()
        )

        expired_subs = (
            db.query(Subscription)
            .filter(
                Subscription.account_id.isnot(None),
                Subscription.is_active == False,
                Subscription.expires_at >= now - timedelta(days=_EXPIRED_NOTIFY_WINDOW_DAYS),
            )
            .all()
        )

        # (sub, account_id, title, body, new_fcm_value)
        targets: list[tuple[Subscription, str, str, str, int]] = []

        for sub in active_subs:
            days_left = (sub.expires_at - now).days
            notified = sub.fcm_notified_days if sub.fcm_notified_days is not None else _DORMANT_VALUE

            for threshold in _THRESHOLDS:
                if days_left > threshold:
                    break
                if notified > threshold:
                    title, body = _make_notification(days_left)
                    targets.append((sub, sub.account_id, title, body, days_left))
                    break

        for sub in expired_subs:
            notified = sub.fcm_notified_days if sub.fcm_notified_days is not None else _DORMANT_VALUE
            if notified != _DORMANT_VALUE:
                targets.append((
                    sub, sub.account_id,
                    "Подписка истекла",
                    "Продлите подписку для продолжения работы.",
                    _DORMANT_VALUE,
                ))

        if not targets:
            logger.debug("FCM expiry notifications: nothing to send")
            return

        account_ids = list({t[1] for t in targets})
        devices = (
            db.query(Device)
            .filter(
                Device.account_id.in_(account_ids),
                Device.fcm_token.isnot(None),
            )
            .all()
        )
        tokens_by_account: dict[str, list[Device]] = {}
        for dev in devices:
            tokens_by_account.setdefault(dev.account_id, []).append(dev)

        sent = 0
        cleared = 0
        for sub, account_id, title, body, new_val in targets:
            devs = tokens_by_account.get(account_id, [])
            any_sent = False
            for dev in devs:
                result = _send_push(dev.fcm_token, title, body)
                if result == "invalid_token":
                    dev.fcm_token = None
                    dev.fcm_token_updated_at = None
                    cleared += 1
                elif result:
                    sent += 1
                    any_sent = True
            if any_sent:
                sub.fcm_notified_days = new_val

        if sent or cleared:
            logger.info(f"FCM expiry notifications: sent={sent}, cleared_stale={cleared}")


def _gc_stale_tokens(db: Session, cutoff: datetime) -> None:
    """Clear FCM tokens not refreshed since cutoff."""
    from database.models import Device
    count = (
        db.query(Device)
        .filter(
            Device.fcm_token.isnot(None),
            Device.fcm_token_updated_at < cutoff,
        )
        .update(
            {Device.fcm_token: None, Device.fcm_token_updated_at: None},
            synchronize_session=False,
        )
    )
    if count:
        logger.info(f"GC'd {count} stale FCM token(s) (>{_STALE_TOKEN_DAYS}d)")
