"""Clavis app API — account / device / login / sync / payment endpoints.

Mounted under ``/api/v1``. See spec §3 in
``F:\\Projects\\clavis-app\\docs\\server-integration-spec.md``
and payment contract in ``docs/api/interfaces/payment.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config.settings import SUBSCRIPTION_BASE_URL
from database.models import Device
from services import account_service
from services.auth import (
    get_db_dep,
    random_device_token,
    require_device,
    require_ephemeral_device,
    require_full_device,
)

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api/v1", tags=["clavis-app"])


# ── request / response schemas ────────────────────────────────

class DeviceRegisterIn(BaseModel):
    device_type: str = Field(..., max_length=16)
    device_name: Optional[str] = Field(None, max_length=128)
    # Stable per-install UUID; used for dedup on promote — see spec §3.11.
    # Absent/empty is fine (old clients) — no dedup happens then.
    install_id: Optional[str] = Field(None, max_length=64)


class DeviceRegisterOut(BaseModel):
    device_token: str


class AccountCreateOut(BaseModel):
    account_id: str
    recovery_phrase: List[str]


class AccountRecoverIn(BaseModel):
    phrase: List[str]


class AccountRecoverOut(BaseModel):
    account_id: str
    subscription_url: Optional[str] = None


class LoginRequestOut(BaseModel):
    one_time_token: str
    expires_at: int
    login_url: str


class LoginClaimIn(BaseModel):
    one_time_token: str


class LoginClaimOut(BaseModel):
    account_id: str
    subscription_url: Optional[str] = None


class SyncShowOut(BaseModel):
    pair_code: str
    expires_at: int
    qr_url: str
    https_url: str


class SyncClaimIn(BaseModel):
    pair_code: str


class SyncStatusOut(BaseModel):
    status: str
    error_reason: Optional[str] = None


class RevokeDeviceIn(BaseModel):
    device_id: str


class FcmTokenIn(BaseModel):
    fcm_token: str = Field(..., max_length=255)


class PaymentCreateIn(BaseModel):
    plan_id: str = Field(..., max_length=16)
    email: str = Field(..., max_length=255)


class PaymentCreateOut(BaseModel):
    payment_id: str
    confirmation_url: str


class PaymentStatusOut(BaseModel):
    payment_id: str
    status: str
    paid: bool


class SupportMessageOut(BaseModel):
    id: str
    direction: str
    text: Optional[str] = None
    image_url: Optional[str] = None
    created_at: int


class SupportMessagesOut(BaseModel):
    messages: List[SupportMessageOut]
    has_more: bool


# ── helpers ────────────────────────────────────────────────────

_VALID_TYPES = {"telegram", "ios", "android", "macos", "windows", "linux"}


def _base() -> str:
    return SUBSCRIPTION_BASE_URL.rstrip("/")


def _ts(dt: datetime) -> int:
    return int(dt.replace(tzinfo=None).timestamp())


# ── endpoints ──────────────────────────────────────────────────

@api_router.post("/device/register", response_model=DeviceRegisterOut)
async def device_register(body: DeviceRegisterIn, db: Session = Depends(get_db_dep)):
    """Create an ephemeral device (serialized + deduped per install_id)."""
    if body.device_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="invalid device_type")
    dev = account_service.register_ephemeral_device(
        db, body.install_id, body.device_type, body.device_name
    )
    return {"device_token": dev.device_token}


@api_router.post("/account/create", response_model=AccountCreateOut)
async def account_create(
    device: Device = Depends(require_ephemeral_device),
    db: Session = Depends(get_db_dep),
):
    account, phrase = account_service.create_account(db, device)
    return {"account_id": account.id, "recovery_phrase": phrase}


@api_router.post("/account/recover", response_model=AccountRecoverOut)
async def account_recover(
    body: AccountRecoverIn,
    device: Device = Depends(require_ephemeral_device),
    db: Session = Depends(get_db_dep),
):
    if not body.phrase or len(body.phrase) != 12:
        raise HTTPException(status_code=401, detail="not_found")
    account = account_service.recover_account(db, device, body.phrase)
    if account is None:
        raise HTTPException(status_code=401, detail="not_found")
    sub_url = account_service._subscription_url_for_account(db, account.id)
    return {"account_id": account.id, "subscription_url": sub_url}


@api_router.post("/login/request", response_model=LoginRequestOut)
async def login_request(
    device: Device = Depends(require_full_device),
    db: Session = Depends(get_db_dep),
):
    row = account_service.create_login_token(db, device.account_id)
    return {
        "one_time_token": row.token,
        "expires_at": _ts(row.expires_at),
        "login_url": f"{_base()}/login/{row.token}",
    }


@api_router.post("/login/claim", response_model=LoginClaimOut)
async def login_claim(
    body: LoginClaimIn,
    device: Device = Depends(require_device),  # switch/merge from an already-attached device
    db: Session = Depends(get_db_dep),
):
    account, error = account_service.claim_login_token(db, device, body.one_time_token)
    if error in ("source_has_account", "multiple_active_subscriptions"):
        # Invalid state (real TG source, or 2+ active paid subs) — resolved by support.
        raise HTTPException(status_code=409, detail=error)
    if account is None:
        raise HTTPException(status_code=401, detail="invalid_token")
    sub_url = account_service._subscription_url_for_account(db, account.id)
    return {"account_id": account.id, "subscription_url": sub_url}


@api_router.post("/sync/show", response_model=SyncShowOut)
async def sync_show(
    device: Device = Depends(require_device),
    db: Session = Depends(get_db_dep),
):
    pair = account_service.create_sync_pair(db, device)
    return {
        "pair_code": pair.pair_code,
        "expires_at": _ts(pair.expires_at),
        "qr_url": f"clavis://sync/{pair.pair_code}",
        "https_url": f"{_base()}/sync/{pair.pair_code}",
    }


@api_router.post("/sync/claim")
async def sync_claim(
    body: SyncClaimIn,
    device: Device = Depends(require_device),
    db: Session = Depends(get_db_dep),
):
    outcome, account = account_service.claim_sync_pair(db, device, body.pair_code)
    if outcome == "expired":
        raise HTTPException(status_code=410, detail="expired")
    if outcome in ("both_empty", "both_full", "cross_account"):
        raise HTTPException(status_code=409, detail=outcome)
    if outcome == "rate_limited":
        raise HTTPException(status_code=429, detail="too_many_attempts")
    if outcome == "already_linked":
        return {"already_linked": True}
    if outcome == "claimed_from_shower":
        sub_url = account_service._subscription_url_for_account(db, account.id) if account else None
        return {"account_id": account.id if account else None, "subscription_url": sub_url}
    if outcome == "claimed_shower":
        return {"synced": True}
    raise HTTPException(status_code=500, detail="unknown_outcome")


@api_router.get("/sync/status/{pair_code}", response_model=SyncStatusOut)
async def sync_status(
    pair_code: str,
    device: Device = Depends(require_device),
    db: Session = Depends(get_db_dep),
):
    status = account_service.get_sync_status(db, pair_code, device)
    if status is None:
        raise HTTPException(status_code=404, detail="pair_not_found")
    return status


@api_router.get("/account/me")
async def account_me(
    device: Device = Depends(require_full_device),
    db: Session = Depends(get_db_dep),
):
    return account_service.get_account_me(db, device)


@api_router.post("/account/revoke-device")
async def account_revoke_device(
    body: RevokeDeviceIn,
    device: Device = Depends(require_full_device),
    db: Session = Depends(get_db_dep),
):
    ok, err = account_service.revoke_device(db, device, body.device_id)
    if not ok:
        if err == "cannot_revoke_telegram":
            raise HTTPException(status_code=403, detail="cannot_revoke_telegram")
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


# ── device FCM token ─────────────────────────────────────────


@api_router.post("/device/fcm-token")
async def device_fcm_token(
    body: FcmTokenIn,
    device: Device = Depends(require_device),
    db: Session = Depends(get_db_dep),
):
    device.fcm_token = body.fcm_token
    device.fcm_token_updated_at = datetime.utcnow()
    return {"ok": True}


# ── payment endpoints ────────────────────────────────────────


@api_router.post("/payment/create", response_model=PaymentCreateOut)
async def payment_create(
    body: PaymentCreateIn,
    device: Device = Depends(require_full_device),
    db: Session = Depends(get_db_dep),
):
    from services import payment_service

    try:
        payment_id, confirmation_url = await payment_service.create_payment(
            db, device, body.plan_id, body.email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"payment_id": payment_id, "confirmation_url": confirmation_url}


@api_router.get("/payment/{payment_id}/status", response_model=PaymentStatusOut)
async def payment_status(
    payment_id: str,
    device: Device = Depends(require_full_device),
    db: Session = Depends(get_db_dep),
):
    from services import payment_service

    result = await payment_service.get_payment_status(db, device.account_id, payment_id)
    if result is None:
        raise HTTPException(status_code=404, detail="not_found")
    return result


@api_router.post("/payment/webhook")
async def payment_webhook(request: Request, db: Session = Depends(get_db_dep)):
    """YooKassa sends notifications here. No auth — verified via re-fetch."""
    from services import payment_service

    try:
        body = await request.json()
        await payment_service.process_webhook(db, body)
    except Exception as e:
        logger.error(f"Payment webhook error: {e}", exc_info=True)
    return {}


# ── support chat ──────────────────────────────────────────────

_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_SIZE = 5 * 1024 * 1024


@api_router.post("/support/send", response_model=SupportMessageOut)
async def support_send(
    request: Request,
    device: Device = Depends(require_full_device),
    db: Session = Depends(get_db_dep),
):
    from services import support_service

    text = None
    image_bytes = None
    image_filename = None
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        text = form.get("text")
        image = form.get("image")
        if image and hasattr(image, "filename") and image.filename:
            if image.content_type not in _ALLOWED_IMAGE_TYPES:
                raise HTTPException(status_code=400, detail="unsupported_image_type")
            image_bytes = await image.read()
            if len(image_bytes) > _MAX_IMAGE_SIZE:
                raise HTTPException(status_code=400, detail="image_too_large")
            image_filename = image.filename
    else:
        body = await request.json()
        text = body.get("text")

    if text and len(text) > 4000:
        text = text[:4000]

    try:
        msg = support_service.send_user_message(
            db, device.account_id, text, device.device_name,
            image_bytes=image_bytes, image_filename=image_filename,
        )
    except ValueError as e:
        detail = str(e)
        if "rate_limited" in detail:
            raise HTTPException(status_code=429, detail="rate_limited")
        raise HTTPException(status_code=400, detail=detail)

    return _support_msg_out(msg)


@api_router.get("/support/messages", response_model=SupportMessagesOut)
async def support_messages(
    before: Optional[int] = None,
    limit: int = 50,
    device: Device = Depends(require_full_device),
    db: Session = Depends(get_db_dep),
):
    from services import support_service

    clamped = min(max(1, limit), 100)
    msgs = support_service.get_messages(db, device.account_id, before_ts=before, limit=clamped)

    return {
        "messages": [_support_msg_out(m) for m in msgs],
        "has_more": len(msgs) == clamped,
    }


@api_router.get("/support/image/{message_id}")
async def support_image(
    message_id: str,
    device: Device = Depends(require_full_device),
    db: Session = Depends(get_db_dep),
):
    from services import support_service

    result = support_service.get_image(db, device.account_id, message_id)
    if result is None:
        raise HTTPException(status_code=404, detail="not_found")
    data, mime = result
    return Response(content=data, media_type=mime)


def _support_msg_out(m) -> dict:
    return {
        "id": m.id,
        "direction": m.direction,
        "text": m.text,
        "image_url": f"/api/v1/support/image/{m.id}" if m.image_file_id else None,
        "created_at": _ts(m.created_at),
    }
