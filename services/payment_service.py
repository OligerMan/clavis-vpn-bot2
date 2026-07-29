"""In-app payment service: YooKassa direct API integration.

Creates payments via YooKassa REST API (not Telegram Payments), handles
webhooks, and activates/extends subscriptions on success.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from config.settings import (
    APP_PLANS,
    DEVICE_LIMIT,
    YOOKASSA_SECRET_KEY,
    YOOKASSA_SHOP_ID,
)
from database.models import AppPayment, Subscription, User

logger = logging.getLogger(__name__)

_YOOKASSA_API = "https://api.yookassa.ru/v3/payments"
_PENDING_FALLBACK_SECONDS = 30


async def create_payment(
    db: Session,
    device,
    plan_id: str,
    email: str,
) -> tuple[str, str]:
    """Create a YooKassa payment. Returns (yookassa_payment_id, confirmation_url)."""
    plan = APP_PLANS.get(plan_id)
    if plan is None:
        raise ValueError("invalid_plan")

    idempotence_key = str(uuid.uuid4())
    body = {
        "amount": {"value": plan["amount"], "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "return_url": "clavis://payment-complete",
        },
        "capture": True,
        "description": plan["description"],
        "metadata": {
            "account_id": device.account_id,
            "plan_id": plan_id,
            "device_id": device.id,
        },
        "receipt": {
            "customer": {"email": email},
            "items": [
                {
                    "description": plan["description"],
                    "quantity": "1",
                    "amount": {"value": plan["amount"], "currency": "RUB"},
                    "vat_code": 1,
                    "payment_subject": "service",
                    "payment_mode": "full_payment",
                }
            ],
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _YOOKASSA_API,
            json=body,
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers={"Idempotence-Key": idempotence_key},
        )
        if resp.status_code >= 400:
            logger.error(f"YooKassa create failed: {resp.status_code} {resp.text}")
            raise RuntimeError(f"YooKassa API error: {resp.status_code}")
        data = resp.json()

    row = AppPayment(
        yookassa_id=data["id"],
        account_id=device.account_id,
        device_id=device.id,
        plan_id=plan_id,
        amount_value=plan["amount"],
        email=email,
        status=data.get("status", "pending"),
        paid=data.get("paid", False),
    )
    db.add(row)
    db.flush()

    confirmation_url = data["confirmation"]["confirmation_url"]
    logger.info(
        f"Created payment {data['id']} for account {device.account_id}, "
        f"plan={plan_id}, amount={plan['amount']} RUB"
    )
    return data["id"], confirmation_url


async def get_payment_status(
    db: Session,
    account_id: str,
    yookassa_id: str,
) -> Optional[dict]:
    """Return payment status. Falls back to YooKassa API if stale pending."""
    row = (
        db.query(AppPayment)
        .filter(
            AppPayment.yookassa_id == yookassa_id,
            AppPayment.account_id == account_id,
        )
        .first()
    )
    if row is None:
        return None

    if row.status == "pending":
        age = (datetime.utcnow() - row.created_at).total_seconds()
        if age > _PENDING_FALLBACK_SECONDS:
            await _sync_from_yookassa(db, row)

    return {
        "payment_id": row.yookassa_id,
        "status": row.status,
        "paid": row.paid,
    }


async def process_webhook(db: Session, body: dict) -> None:
    """Handle a YooKassa webhook notification. Always returns (caller sends 200)."""
    event_type = body.get("event", "")
    obj = body.get("object", {})
    yookassa_id = obj.get("id")
    if not yookassa_id:
        return

    row = db.query(AppPayment).filter(AppPayment.yookassa_id == yookassa_id).first()
    if row is None:
        logger.warning(f"Webhook for unknown app payment {yookassa_id}, ignoring")
        return

    if event_type == "payment.succeeded":
        if row.status == "succeeded":
            return

        verified = await _verify_with_yookassa(yookassa_id)
        if not verified:
            return

        row.status = "succeeded"
        row.paid = True
        row.completed_at = datetime.utcnow()
        db.flush()

        try:
            _activate_subscription(db, row.account_id, row.plan_id)
        except Exception as e:
            logger.error(
                f"Subscription activation failed for payment {yookassa_id}: {e}",
                exc_info=True,
            )

    elif event_type == "payment.canceled":
        if row.status != "succeeded":
            row.status = "canceled"
            row.completed_at = datetime.utcnow()

    elif event_type == "refund.succeeded":
        logger.info(f"Refund succeeded for payment {yookassa_id}")


# ── internal helpers ──────────────────────────────────────────


async def _verify_with_yookassa(yookassa_id: str) -> bool:
    """Fetch payment from YooKassa to verify it's really succeeded."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_YOOKASSA_API}/{yookassa_id}",
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            )
            if resp.status_code != 200:
                logger.error(f"Cannot verify payment {yookassa_id}: HTTP {resp.status_code}")
                return False
            data = resp.json()
        if data.get("status") != "succeeded" or not data.get("paid"):
            logger.warning(f"Payment {yookassa_id} verification mismatch: status={data.get('status')}")
            return False
        return True
    except Exception as e:
        logger.error(f"Payment verification failed for {yookassa_id}: {e}")
        return False


async def _sync_from_yookassa(db: Session, row: AppPayment) -> None:
    """Fetch fresh status from YooKassa and update local row (fallback for lost webhooks)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_YOOKASSA_API}/{row.yookassa_id}",
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            )
            if resp.status_code != 200:
                return
            data = resp.json()

        new_status = data.get("status", row.status)
        new_paid = data.get("paid", row.paid)

        if new_status == row.status and new_paid == row.paid:
            return

        row.status = new_status
        row.paid = new_paid
        if new_status in ("succeeded", "canceled"):
            row.completed_at = datetime.utcnow()
        if new_status == "succeeded" and new_paid:
            try:
                _activate_subscription(db, row.account_id, row.plan_id)
            except Exception as e:
                logger.error(f"Subscription activation failed during sync for {row.yookassa_id}: {e}", exc_info=True)
        db.flush()
    except Exception as e:
        logger.warning(f"YooKassa sync failed for {row.yookassa_id}: {e}")


def _activate_subscription(db: Session, account_id: str, plan_id: str) -> None:
    """Serialized entry point — activation is locked per account so it can't race a
    concurrent login-merge on the same account. Commits inside the lock."""
    from services.account_service import account_lock
    with account_lock(account_id):
        _activate_subscription_impl(db, account_id, plan_id)
        db.commit()


def _activate_subscription_impl(db: Session, account_id: str, plan_id: str) -> None:
    """Create or extend subscription for the given account after successful payment."""
    from services.key_service import KeyService
    from subscription.cache import invalidate_subscription_cache

    plan = APP_PLANS[plan_id]
    days = plan["days"]
    plan_type = plan.get("plan_type", "basic")
    now = datetime.utcnow()

    active_sub = (
        db.query(Subscription)
        .filter(
            Subscription.account_id == account_id,
            Subscription.is_active == True,
            Subscription.expires_at > now,
        )
        .first()
    )

    user = db.query(User).filter(User.account_id == account_id).first()

    if active_sub:
        active_sub.expires_at += timedelta(days=days)
        if active_sub.plan_type != plan_type:
            active_sub.plan_type = plan_type
        active_sub.reset_reminder_flags()
        db.flush()
        _ensure_keys_and_expiry(db, active_sub, user)
        logger.info(f"Extended sub {active_sub.id} by {days}d for account {account_id}")
        return

    expired_sub = (
        db.query(Subscription)
        .filter(
            Subscription.account_id == account_id,
            Subscription.is_test == False,
        )
        .order_by(Subscription.expires_at.desc())
        .first()
    )

    if expired_sub:
        expired_sub.is_active = True
        expired_sub.expires_at = now + timedelta(days=days)
        expired_sub.plan_type = plan_type
        expired_sub.reset_reminder_flags()
        db.flush()
        _ensure_keys_and_expiry(db, expired_sub, user)
        logger.info(f"Reactivated sub {expired_sub.id} for {days}d for account {account_id}")
        return

    sub = Subscription(
        user_id=user.id if user else None,
        account_id=account_id,
        is_test=False,
        is_active=True,
        expires_at=now + timedelta(days=days),
        device_limit=DEVICE_LIMIT,
        plan_type=plan_type,
    )
    db.add(sub)
    db.flush()
    _ensure_keys_and_expiry(db, sub, user)
    logger.info(f"Created sub {sub.id} for {days}d for account {account_id}")


def _ensure_keys_and_expiry(db: Session, sub: Subscription, user: Optional[User]) -> None:
    """Ensure VPN keys exist and update their expiry on x-ui panels."""
    from services.key_service import KeyService
    from subscription.cache import invalidate_subscription_cache

    client_identifier = user.telegram_id if user else f"app_{sub.account_id}"
    try:
        KeyService.ensure_keys_exist(db, sub, client_identifier)
    except Exception as e:
        logger.error(f"Key creation failed for sub {sub.id}: {e}")

    try:
        KeyService.update_subscription_keys_expiry(db, sub)
    except Exception as e:
        logger.warning(f"Key expiry update failed for sub {sub.id}: {e}")

    if sub.token:
        invalidate_subscription_cache(sub.token)
