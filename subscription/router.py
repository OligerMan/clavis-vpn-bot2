"""Subscription server API routes."""

import base64
import logging
import re
from datetime import timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse
from typing import List, Dict, Any

from database import get_db_session, Subscription, Key, WebTrialActivation
from database.activity_log import log_activity
from vpn.xui_uri_builder import parse_vless_uri
from subscription.cache import (
    get_cached_subscription,
    cache_subscription_response,
)
from subscription.formatter import format_subscription_response

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


def _validate_token(token: str) -> None:
    """Raise 404 if token is not a valid UUID format."""
    if not _UUID_RE.match(token):
        raise HTTPException(status_code=404, detail="Not found")


def _make_profile_title(subscription: Subscription) -> str:
    """Encode profile title in base64: format for v2ray clients.

    Format: "base64:<b64 encoded title>"
    Title includes service name and subscription type indicator.
    """
    plan_type = getattr(subscription, 'plan_type', 'basic') or 'basic'
    if plan_type == 'free':
        title = "Clavis v2 (приглашение от друга)"
    elif subscription.is_test:
        title = "Clavis v2 (Тест)"
    else:
        title = "Clavis v2"
    encoded = base64.b64encode(title.encode("utf-8")).decode("utf-8")
    return f"base64:{encoded}"


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sub/{token}")
async def get_subscription(token: str, request: Request) -> PlainTextResponse:
    """Serve subscription as base64-encoded VLESS URIs.

    Args:
        token: Subscription token (UUID)
        request: FastAPI request object

    Returns:
        PlainTextResponse with base64-encoded VLESS URIs

    Raises:
        HTTPException: 404 if subscription not found or has no keys
    """
    _validate_token(token)

    # Log access for analytics
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    logger.info(
        f"Subscription access: token={token[:8]}..., ip={client_ip}, ua={user_agent[:50]}"
    )

    # Check cache first
    cached = get_cached_subscription(token)
    if cached:
        logger.debug(f"Cache hit for token={token[:8]}...")
        body, token_short, expires_ts = cached

        # Still need to get subscription for correct title and fresh expires_ts
        try:
            with get_db_session() as db:
                subscription = db.query(Subscription).filter(
                    Subscription.token == token
                ).first()

                if subscription:
                    profile_title = _make_profile_title(subscription)
                    # Use fresh expires_ts from database, not from cache
                    expires_ts = int(subscription.expires_at.replace(tzinfo=timezone.utc).timestamp())
                else:
                    # Fallback if subscription not found (shouldn't happen)
                    profile_title = "base64:Q2xhdmlzIHYy"  # "Clavis v2"
                    # Keep cached expires_ts as fallback
        except Exception:
            # Fallback on DB error
            profile_title = "base64:Q2xhdmlzIHYy"  # "Clavis v2"
            # Keep cached expires_ts as fallback

        headers = {
            "profile-title": profile_title,
            "profile-update-interval": "12",
            "subscription-userinfo": f"upload=0; download=0; total=0; expire={expires_ts}",
            "content-disposition": "inline",
        }
        return PlainTextResponse(content=body, headers=headers)

    logger.debug(f"Cache miss for token={token[:8]}...")

    # Query database
    try:
        with get_db_session() as db:
            # Get subscription by token
            subscription = db.query(Subscription).filter(
                Subscription.token == token
            ).first()

            if not subscription:
                logger.warning(f"Subscription not found: token={token[:8]}...")
                raise HTTPException(
                    status_code=404,
                    detail="Subscription not found"
                )

            # Get all active keys for this subscription (supports multi-server)
            keys = db.query(Key).filter(
                Key.subscription_id == subscription.id,
                Key.is_active == True
            ).all()

            if not keys:
                logger.warning(
                    f"No active keys for subscription: token={token[:8]}..."
                )
                raise HTTPException(
                    status_code=404,
                    detail="No active keys found"
                )

            # Check if subscription is expired or inactive
            is_expired = not subscription.is_active or subscription.is_expired

            if is_expired:
                logger.info(
                    f"Subscription expired/inactive: token={token[:8]}..., "
                    f"is_active={subscription.is_active}, "
                    f"is_expired={subscription.is_expired}"
                )

            # Build ad lines for free (referral) subscriptions
            ad_lines = None
            if getattr(subscription, 'plan_type', None) == 'free':
                from config.settings import FREE_AD_LINES
                ad_lines = FREE_AD_LINES

            # Format response (will modify remarks if expired)
            response = format_subscription_response(keys, is_expired=is_expired, ad_lines=ad_lines)

            # Cache response (cache expired subscriptions too, they rarely change)
            token_short = subscription.token[:8] if subscription.token else "unknown"
            expires_ts = int(subscription.expires_at.replace(tzinfo=timezone.utc).timestamp())
            cache_subscription_response(token, (response, token_short, expires_ts))

            logger.info(
                f"Subscription served: token={token[:8]}..., "
                f"keys={len(keys)}, "
                f"servers={len(set(k.server_id for k in keys if k.server_id))}, "
                f"expired={is_expired}"
            )

            # Add v2raytun headers
            headers = {
                "profile-title": _make_profile_title(subscription),
                "profile-update-interval": "12",
                "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(subscription.expires_at.replace(tzinfo=timezone.utc).timestamp())}",
                "content-disposition": "inline",
            }

            return PlainTextResponse(content=response, headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error serving subscription {token[:8]}...: {e}",
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )


@router.get("/info/{token}")
async def get_subscription_info(token: str) -> JSONResponse:
    """Get subscription metadata for debugging.

    Args:
        token: Subscription token (UUID)

    Returns:
        JSONResponse with subscription metadata

    Raises:
        HTTPException: 404 if subscription not found
    """
    _validate_token(token)

    try:
        with get_db_session() as db:
            # Get subscription
            subscription = db.query(Subscription).filter(
                Subscription.token == token
            ).first()

            if not subscription:
                raise HTTPException(
                    status_code=404,
                    detail="Subscription not found"
                )

            # Get keys
            keys = db.query(Key).filter(
                Key.subscription_id == subscription.id,
                Key.is_active == True
            ).all()

            # Get unique servers
            server_ids = set(k.server_id for k in keys if k.server_id)

            # Build response
            info = {
                "token": token[:8] + "..." + token[-4:],  # Partially masked
                "is_active": subscription.is_active,
                "is_expired": subscription.is_expired,
                "expires_at": subscription.expires_at.isoformat(),
                "days_remaining": subscription.days_until_expiry,
                "is_test": subscription.is_test,
                "device_limit": subscription.device_limit,
                "key_count": len(keys),
                "server_count": len(server_ids),
                "server_ids": list(server_ids),
                "protocols": list(set(k.protocol for k in keys)),
            }

            return JSONResponse(content=info)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error getting subscription info {token[:8]}...: {e}",
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )


@router.get("/raw/{token}")
async def get_subscription_raw(token: str, request: Request) -> PlainTextResponse:
    """Serve subscription as raw VLESS URIs (not base64).

    For clients like v2raytun that don't support base64 format.

    Args:
        token: Subscription token (UUID)
        request: FastAPI request object

    Returns:
        PlainTextResponse with newline-separated VLESS URIs

    Raises:
        HTTPException: 404 if subscription not found or has no keys
    """
    _validate_token(token)

    # Log access
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    logger.info(
        f"Raw subscription access: token={token[:8]}..., ip={client_ip}, ua={user_agent[:50]}"
    )

    try:
        with get_db_session() as db:
            # Get subscription
            subscription = db.query(Subscription).filter(
                Subscription.token == token
            ).first()

            if not subscription:
                logger.warning(f"Subscription not found: token={token[:8]}...")
                raise HTTPException(
                    status_code=404,
                    detail="Subscription not found"
                )

            # Get all active keys
            keys = db.query(Key).filter(
                Key.subscription_id == subscription.id,
                Key.is_active == True
            ).all()

            if not keys:
                logger.warning(
                    f"No active keys for subscription: token={token[:8]}..."
                )
                raise HTTPException(
                    status_code=404,
                    detail="No active keys found"
                )

            # Check if expired
            is_expired = not subscription.is_active or subscription.is_expired

            # Get raw URIs (not base64 encoded)
            from subscription.formatter import modify_vless_remark

            from subscription.formatter import _extract_server_name

            uris = []
            for key in keys:
                uri = key.key_data
                if not uri or not uri.startswith("vless://"):
                    continue

                # Modify remark if expired
                if is_expired:
                    server_name = _extract_server_name(uri)
                    expired_remark = f"⏰ Clavis {server_name} - Expired, please renew subscription"
                    uri = modify_vless_remark(uri, expired_remark)
                elif key.server_id is None:
                    uri = modify_vless_remark(uri, "Clavis v1 (старый ключ)")
                else:
                    from subscription.formatter import _extract_remark, _country_flag
                    remark = _extract_remark(uri)
                    flag = _country_flag(remark)
                    if flag:
                        uri = modify_vless_remark(uri, f"{remark} {flag}")

                uris.append(uri)

            if not uris:
                raise HTTPException(
                    status_code=404,
                    detail="No valid URIs found"
                )

            # Return as plain text with newlines
            response = "\n".join(uris)

            logger.info(
                f"Raw subscription served: token={token[:8]}..., "
                f"keys={len(uris)}, "
                f"servers={len(set(k.server_id for k in keys if k.server_id))}, "
                f"expired={is_expired}"
            )

            # Add v2raytun headers
            headers = {
                "profile-title": _make_profile_title(subscription),
                "profile-update-interval": "12",
                "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(subscription.expires_at.replace(tzinfo=timezone.utc).timestamp())}",
                "content-disposition": "inline",
            }

            return PlainTextResponse(content=response, headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error serving raw subscription {token[:8]}...: {e}",
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )


@router.get("/json/{token}")
async def get_subscription_json(token: str, request: Request) -> JSONResponse:
    """Serve subscription as JSON for v2raytun."""
    _validate_token(token)

    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    logger.info(f"JSON subscription access: token={token[:8]}..., ip={client_ip}, ua={user_agent[:50]}")

    try:
        with get_db_session() as db:
            subscription = db.query(Subscription).filter(Subscription.token == token).first()
            if not subscription:
                raise HTTPException(status_code=404, detail="Subscription not found")

            keys = db.query(Key).filter(
                Key.subscription_id == subscription.id,
                Key.is_active == True
            ).all()

            if not keys:
                raise HTTPException(status_code=404, detail="No active keys found")

            is_expired = not subscription.is_active or subscription.is_expired

            servers = []
            for key in keys:
                uri = key.key_data
                if not uri or not uri.startswith("vless://"):
                    continue

                try:
                    parsed = parse_vless_uri(uri)
                    server_config = {
                        "type": "vless",
                        "name": "Clavis VPN - cl23" if not is_expired else "⏰ Expired - Renew",
                        "server": parsed["host"],
                        "port": parsed["port"],
                        "uuid": parsed["uuid"],
                        "network": parsed["params"].get("type", "tcp"),
                        "tls": "reality" if parsed["params"].get("security") == "reality" else "none",
                        "reality-opts": {
                            "public-key": parsed["params"].get("pbk", ""),
                            "short-id": parsed["params"].get("sid", ""),
                        } if parsed["params"].get("security") == "reality" else None,
                        "sni": parsed["params"].get("sni", ""),
                        "flow": parsed["params"].get("flow", ""),
                        "fingerprint": parsed["params"].get("fp", "chrome"),
                    }
                    servers.append(server_config)
                except Exception as e:
                    logger.warning(f"Failed to parse VLESS URI: {e}")

            if not servers:
                raise HTTPException(status_code=404, detail="No valid servers")

            response_data = servers  # Return array directly

            headers = {
                "profile-title": _make_profile_title(subscription),
                "profile-update-interval": "12",
                "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(subscription.expires_at.replace(tzinfo=timezone.utc).timestamp())}",
                "content-disposition": "inline",
            }

            logger.info(f"JSON subscription served: token={token[:8]}..., servers={len(servers)}")
            return JSONResponse(content=response_data, headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving JSON subscription {token[:8]}...: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/v2raytun/{token}")
async def v2raytun_redirect(token: str, request: Request):
    """Serve HTML page that redirects to v2raytun deep link.

    Used in Telegram bot messages. When user taps the link:
    1. Browser opens this URL
    2. HTML page triggers redirect to v2raytun://import-sub?url=...
    3. Android opens v2raytun app and imports the subscription
    4. If app not installed, page shows manual instructions

    Args:
        token: Subscription token (UUID)
        request: FastAPI request object
    """
    _validate_token(token)

    from config.settings import SUBSCRIPTION_BASE_URL

    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"v2raytun redirect: token={token[:8]}..., ip={client_ip}")

    sub_url = f"{SUBSCRIPTION_BASE_URL.rstrip('/')}/sub/{token}"
    deep_link = f"v2raytun://import/{sub_url}"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Clavis VPN</title>
    <meta http-equiv="refresh" content="0;url={deep_link}">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0;
            background: #0f172a; color: #e2e8f0;
            text-align: center; padding: 20px;
        }}
        .container {{ max-width: 400px; }}
        h2 {{ color: #38bdf8; margin-bottom: 8px; }}
        p {{ color: #94a3b8; line-height: 1.5; }}
        .sub-url {{
            background: #1e293b; border: 1px solid #334155;
            border-radius: 8px; padding: 12px; margin: 16px 0;
            word-break: break-all; font-family: monospace; font-size: 13px;
            color: #7dd3fc; user-select: all;
        }}
        a.btn {{
            display: inline-block; margin-top: 12px; padding: 12px 24px;
            background: #0ea5e9; color: #fff; text-decoration: none;
            border-radius: 8px; font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Clavis VPN</h2>
        <p>Открываем v2rayTun...</p>
        <p>Если приложение не открылось автоматически:</p>
        <a class="btn" href="{deep_link}">Открыть v2rayTun</a>
        <p style="margin-top: 24px; font-size: 13px;">Или скопируйте ссылку вручную:</p>
        <div class="sub-url">{sub_url}</div>
    </div>
    <script>window.location.href = "{deep_link}";</script>
</body>
</html>"""

    return HTMLResponse(content=html)


@router.get("/happ/{token}")
async def happ_redirect(token: str, request: Request):
    """Serve HTML page that redirects to Happ deep link (iOS/macOS)."""
    _validate_token(token)

    from config.settings import SUBSCRIPTION_BASE_URL

    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"happ redirect: token={token[:8]}..., ip={client_ip}")

    sub_url = f"{SUBSCRIPTION_BASE_URL.rstrip('/')}/sub/{token}"
    deep_link = f"happ://add/{sub_url}"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Clavis VPN</title>
    <meta http-equiv="refresh" content="0;url={deep_link}">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0;
            background: #0f172a; color: #e2e8f0;
            text-align: center; padding: 20px;
        }}
        .container {{ max-width: 400px; }}
        h2 {{ color: #38bdf8; margin-bottom: 8px; }}
        p {{ color: #94a3b8; line-height: 1.5; }}
        .sub-url {{
            background: #1e293b; border: 1px solid #334155;
            border-radius: 8px; padding: 12px; margin: 16px 0;
            word-break: break-all; font-family: monospace; font-size: 13px;
            color: #7dd3fc; user-select: all;
        }}
        a.btn {{
            display: inline-block; margin-top: 12px; padding: 12px 24px;
            background: #0ea5e9; color: #fff; text-decoration: none;
            border-radius: 8px; font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Clavis VPN</h2>
        <p>Открываем Happ...</p>
        <p>Если приложение не открылось автоматически:</p>
        <a class="btn" href="{deep_link}">Открыть Happ</a>
        <p style="margin-top: 24px; font-size: 13px;">Или скопируйте ссылку вручную:</p>
        <div class="sub-url">{sub_url}</div>
    </div>
    <script>window.location.href = "{deep_link}";</script>
</body>
</html>"""

    return HTMLResponse(content=html)


@router.get("/login/{one_time_token}")
async def clavis_login_redirect(one_time_token: str, request: Request):
    """Meta-refresh → ``clavis://login-token/<token>``.

    We do NOT validate the token here — an invalid/expired token must be
    detected by the app's ``/api/v1/login/claim`` call, so this wrapper can't
    leak information about token existence.
    """
    from config.settings import SUBSCRIPTION_BASE_URL

    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"clavis login redirect: token={one_time_token[:8]}..., ip={client_ip}")

    deep_link = f"clavis://login-token/{one_time_token}"
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Clavis — Login</title>
    <meta http-equiv="refresh" content="0;url={deep_link}">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0;
            background: #0f172a; color: #e2e8f0;
            text-align: center; padding: 20px;
        }}
        .container {{ max-width: 400px; }}
        h2 {{ color: #38bdf8; margin-bottom: 8px; }}
        p {{ color: #94a3b8; line-height: 1.5; }}
        a.btn {{
            display: inline-block; margin-top: 12px; padding: 12px 24px;
            background: #0ea5e9; color: #fff; text-decoration: none;
            border-radius: 8px; font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Clavis</h2>
        <p>Открываем приложение Clavis...</p>
        <p>Если приложение не открылось автоматически, нажмите кнопку ниже:</p>
        <a class="btn" href="{deep_link}">Открыть Clavis</a>
    </div>
    <script>window.location.href = "{deep_link}";</script>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/sync/{pair_code}")
async def clavis_sync_redirect(pair_code: str, request: Request):
    """Meta-refresh → ``clavis://sync/<pair_code>``.

    Like ``/login/{token}``: no validation here. Invalid codes surface on
    the app's ``/api/v1/sync/claim`` call.
    """
    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"clavis sync redirect: code={pair_code}, ip={client_ip}")

    deep_link = f"clavis://sync/{pair_code}"
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Clavis — Sync</title>
    <meta http-equiv="refresh" content="0;url={deep_link}">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0;
            background: #0f172a; color: #e2e8f0;
            text-align: center; padding: 20px;
        }}
        .container {{ max-width: 400px; }}
        h2 {{ color: #38bdf8; margin-bottom: 8px; }}
        p {{ color: #94a3b8; line-height: 1.5; }}
        a.btn {{
            display: inline-block; margin-top: 12px; padding: 12px 24px;
            background: #0ea5e9; color: #fff; text-decoration: none;
            border-radius: 8px; font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Clavis</h2>
        <p>Открываем приложение Clavis...</p>
        <p>Если приложение не открылось автоматически, нажмите кнопку ниже:</p>
        <a class="btn" href="{deep_link}">Открыть Clavis</a>
    </div>
    <script>window.location.href = "{deep_link}";</script>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/invite/{code}")
async def referral_invite(code: str, request: Request):
    """Activate a referral invite and show a VPN setup landing page.

    On first access: creates an anonymous guest user + 7-day free subscription,
    issues a key on the Free server group, stores the subscription token.
    On subsequent accesses: serves the existing subscription page.
    The page includes a v2raytun:// deeplink so the friend can connect without Telegram.
    """
    import secrets
    import re
    from datetime import datetime, timedelta
    from database import get_db_session, ReferralInvite, User, Subscription
    from config.settings import SUBSCRIPTION_BASE_URL, REFERRAL_SUBSCRIPTION_DAYS

    # Basic code format guard
    if not re.match(r'^[a-zA-Z0-9]{6,20}$', code):
        raise HTTPException(status_code=404, detail="Not found")

    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"Referral invite access: code={code}, ip={client_ip}")

    try:
        with get_db_session() as db:
            invite = db.query(ReferralInvite).filter(ReferralInvite.code == code).first()
            if not invite:
                raise HTTPException(status_code=404, detail="Invite link not found or expired")

            # If already activated — just serve the existing subscription page
            if invite.subscription_token:
                sub_token = invite.subscription_token
            else:
                # First use: create guest user + subscription + key
                from services.key_service import KeyService

                # Create anonymous guest user with a negative telegram_id
                guest = User(telegram_id=0, username="[referral]")
                db.add(guest)
                db.flush()  # get guest.id
                guest.telegram_id = -guest.id  # guaranteed unique negative ID

                expires_at = datetime.utcnow() + timedelta(days=REFERRAL_SUBSCRIPTION_DAYS)
                subscription = Subscription(
                    user_id=guest.id,
                    is_test=False,
                    is_active=True,
                    expires_at=expires_at,
                    device_limit=1,
                    plan_type='free',
                )
                db.add(subscription)
                db.flush()  # get subscription.id and auto-generated token

                # Issue key on Free server group
                try:
                    KeyService.ensure_keys_exist(db, subscription, guest.telegram_id)
                except Exception as e:
                    logger.error(f"Failed to create key for invite {code}: {e}", exc_info=True)
                    raise HTTPException(status_code=503, detail="No free servers available right now. Try again later.")

                invite.subscription_token = subscription.token
                invite.activated_at = datetime.utcnow()
                db.commit()

                sub_token = subscription.token
                log_activity(db, guest.telegram_id, "invite_used", f"code={code}, inviter_db_id={invite.inviter_id}")
                db.commit()
                logger.info(f"Referral invite {code} activated: guest_user={guest.id}, sub={subscription.id}")

        base = SUBSCRIPTION_BASE_URL.rstrip('/')
        sub_url = f"{base}/sub/{sub_token}"
        deep_link = f"v2raytun://import/{sub_url}"
        invite_url = f"{base}/invite/{code}"

        # Generate QR code as inline base64 PNG
        try:
            import segno, io as _io, base64 as _b64
            qr = segno.make(invite_url, error='h')
            qr_buf = _io.BytesIO()
            qr.save(qr_buf, kind='png', scale=8, border=2)
            qr_b64 = _b64.b64encode(qr_buf.getvalue()).decode()
            qr_block = (
                '<p class="label">Или отсканируй QR-код с другого устройства:</p>'
                f'<img src="data:image/png;base64,{qr_b64}" '
                'style="width:200px;height:200px;margin:12px auto;display:block;border-radius:8px;">'
            )
        except Exception:
            qr_block = ''

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Clavis VPN — Подключение</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0;
            background: #0f172a; color: #e2e8f0;
            text-align: center; padding: 20px; box-sizing: border-box;
        }}
        .container {{ max-width: 420px; width: 100%; }}
        h2 {{ color: #38bdf8; margin-bottom: 4px; }}
        .subtitle {{ color: #64748b; font-size: 14px; margin-bottom: 24px; }}
        .steps {{ text-align: left; background: #1e293b; border-radius: 12px;
                  padding: 16px 20px; margin-bottom: 20px; }}
        .steps li {{ color: #94a3b8; margin-bottom: 8px; line-height: 1.5; }}
        .steps li b {{ color: #e2e8f0; }}
        a.btn {{
            display: block; margin: 12px 0; padding: 14px 24px;
            background: #0ea5e9; color: white; text-decoration: none;
            border-radius: 10px; font-size: 16px; font-weight: 600;
        }}
        a.btn:hover {{ background: #0284c7; }}
        a.btn.secondary {{ background: #1e293b; border: 1px solid #334155;
                           font-size: 14px; padding: 11px 20px; color: #94a3b8; }}
        .sub-url {{
            background: #1e293b; border: 1px solid #334155; border-radius: 8px;
            padding: 12px; margin: 16px 0; word-break: break-all;
            font-family: monospace; font-size: 12px; color: #7dd3fc;
            user-select: all; text-align: left;
        }}
        .label {{ color: #64748b; font-size: 13px; margin-top: 20px; }}
        .ad {{ color: #475569; font-size: 12px; margin-top: 24px; }}
        {_DOWNLOAD_LINKS_CSS}
    </style>
</head>
<body>
    <div class="container">
        <h2>🔐 Clavis VPN</h2>
        <p class="subtitle">Бесплатный VPN на 3 дня</p>

        <ol class="steps">
            <li><b>Установите приложение</b> (ссылки ниже)</li>
            <li><b>Нажмите кнопку «Подключить»</b> — подписка добавится автоматически</li>
            <li><b>Включите VPN</b> в приложении</li>
        </ol>
        {_DOWNLOAD_LINKS_HTML}

        <a class="btn" href="{deep_link}">🚀 Подключить VPN</a>

        {qr_block}

        <p class="label">Или добавьте ссылку вручную в любой VLESS-клиент:</p>
        <div class="sub-url">{sub_url}</div>

        <p class="ad">Для постоянного быстрого VPN: @clavis_vpn_bot</p>
    </div>
</body>
</html>"""

        return HTMLResponse(content=html)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /invite/{code}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


_DOWNLOAD_LINKS_HTML = """
<p class="label" style="margin-top:24px;">Скачать приложение:</p>
<div class="dl-grid">
    <a class="dl-btn" href="https://play.google.com/store/apps/details?id=com.v2raytun.android">Android</a>
    <a class="dl-btn" href="https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973">iPhone / iPad</a>
    <a class="dl-btn" href="https://github.com/mdf45/v2raytun/releases/download/v3.7.10/v2RayTun_Setup.exe">Windows</a>
    <a class="dl-btn" href="https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973">macOS</a>
</div>
"""

_DOWNLOAD_LINKS_CSS = """
        .dl-grid {
            display: grid; grid-template-columns: 1fr 1fr;
            gap: 8px; margin: 8px 0 20px;
        }
        a.dl-btn {
            display: block; padding: 10px 8px;
            background: #1e293b; border: 1px solid #334155;
            color: #94a3b8; text-decoration: none;
            border-radius: 8px; font-size: 14px;
        }
        a.dl-btn:hover { background: #273548; color: #e2e8f0; }
"""


def _trial_page(sub_url: str, base: str, returning: bool = False) -> str:
    """Build the /trial HTML success page."""
    deep_link = f"v2raytun://import/{sub_url}"
    trial_url = f"{base}/trial"

    try:
        import segno, io as _io, base64 as _b64
        qr = segno.make(trial_url, error='h')
        qr_buf = _io.BytesIO()
        qr.save(qr_buf, kind='png', scale=8, border=2)
        qr_b64 = _b64.b64encode(qr_buf.getvalue()).decode()
        qr_block = (
            '<p class="label">Или отсканируй QR с другого устройства:</p>'
            f'<img src="data:image/png;base64,{qr_b64}" '
            'style="width:200px;height:200px;margin:12px auto;display:block;border-radius:8px;">'
        )
    except Exception:
        qr_block = ''

    welcome = (
        '<p class="returning">👋 Добро пожаловать снова! Ваш VPN ещё активен.</p>'
        if returning else ''
    )

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Clavis VPN — Бесплатный триал</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0;
            background: #0f172a; color: #e2e8f0;
            text-align: center; padding: 20px; box-sizing: border-box;
        }}
        .container {{ max-width: 420px; width: 100%; }}
        h2 {{ color: #38bdf8; margin-bottom: 4px; }}
        .subtitle {{ color: #64748b; font-size: 14px; margin-bottom: 24px; }}
        .returning {{ background: #1e3a2f; border: 1px solid #166534; border-radius: 8px;
                      padding: 10px 16px; color: #4ade80; font-size: 14px; margin-bottom: 16px; }}
        .steps {{ text-align: left; background: #1e293b; border-radius: 12px;
                  padding: 16px 20px; margin-bottom: 20px; }}
        .steps li {{ color: #94a3b8; margin-bottom: 8px; line-height: 1.5; }}
        .steps li b {{ color: #e2e8f0; }}
        a.btn {{
            display: block; margin: 12px 0; padding: 14px 24px;
            background: #0ea5e9; color: white; text-decoration: none;
            border-radius: 10px; font-size: 16px; font-weight: 600;
        }}
        a.btn:hover {{ background: #0284c7; }}
        .sub-url {{
            background: #1e293b; border: 1px solid #334155; border-radius: 8px;
            padding: 12px; margin: 16px 0; word-break: break-all;
            font-family: monospace; font-size: 12px; color: #7dd3fc;
            user-select: all; text-align: left;
        }}
        .label {{ color: #64748b; font-size: 13px; margin-top: 20px; }}
        .ad {{ color: #475569; font-size: 12px; margin-top: 24px; }}
        {_DOWNLOAD_LINKS_CSS}
    </style>
</head>
<body>
    <div class="container">
        <h2>🔐 Clavis VPN</h2>
        <p class="subtitle">Бесплатный VPN на 48 часов</p>

        {welcome}

        <ol class="steps">
            <li><b>Установите приложение</b> (ссылки ниже)</li>
            <li><b>Нажмите кнопку «Подключить»</b> — подписка добавится автоматически</li>
            <li><b>Включите VPN</b> в приложении</li>
        </ol>
        {_DOWNLOAD_LINKS_HTML}

        <a class="btn" href="{deep_link}">🚀 Подключить VPN</a>

        {qr_block}

        <p class="label">Или добавьте ссылку вручную в любой VLESS-клиент:</p>
        <div class="sub-url">{sub_url}</div>

        <p class="ad">Для постоянного быстрого VPN: @clavis_vpn_bot</p>
    </div>
</body>
</html>"""


@router.get("/trial")
async def web_trial_disabled(request: Request):
    """Temporarily disabled."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse("<h2>Страница временно недоступна.</h2>", status_code=503)


@router.get("/trial_disabled")
async def web_trial(request: Request):
    """Free 48h VPN trial, rate-limited per IP.

    First visit: creates anonymous user + subscription + key on Free server group.
    Repeat visits while active: returns the same subscription page.
    After expiry: shows "trial expired" page with upgrade link.
    """
    from datetime import datetime, timedelta
    from database import get_db_session, WebTrialActivation, User, Subscription
    from config.settings import SUBSCRIPTION_BASE_URL

    ip = (
        request.headers.get('x-real-ip') or
        (request.headers.get('x-forwarded-for') or '').split(',')[0].strip() or
        (request.client.host if request.client else 'unknown')
    )

    logger.info(f"Web trial access: ip={ip}")

    base = SUBSCRIPTION_BASE_URL.rstrip('/')

    try:
        with get_db_session() as db:
            activation = db.query(WebTrialActivation).filter(
                WebTrialActivation.ip_address == ip
            ).first()

            if activation:
                # Always increment visit counter
                activation.visit_count += 1
                db.commit()

                sub = db.query(Subscription).filter(
                    Subscription.token == activation.subscription_token
                ).first()

                now = datetime.utcnow()
                if sub and sub.expires_at > now:
                    # Still active — return the same page
                    sub_url = f"{base}/sub/{sub.token}"
                    return HTMLResponse(content=_trial_page(sub_url, base, returning=True))
                else:
                    # Expired
                    bot_url = "https://t.me/clavis_vpn_bot"
                    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Clavis VPN — Триал истёк</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center;
               align-items: center; min-height: 100vh; margin: 0;
               background: #0f172a; color: #e2e8f0; text-align: center; padding: 20px; }}
        .container {{ max-width: 400px; }}
        h2 {{ color: #f87171; }}
        p {{ color: #94a3b8; line-height: 1.6; }}
        a.btn {{ display: inline-block; margin-top: 20px; padding: 14px 28px;
                 background: #0ea5e9; color: white; text-decoration: none;
                 border-radius: 10px; font-size: 16px; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>⏰ Пробный период истёк</h2>
        <p>Ваши бесплатные 48 часов закончились.</p>
        <p>Перейдите в бот, чтобы оформить постоянную подписку — от 275₽ за 3 месяца.</p>
        <a class="btn" href="{bot_url}">Перейти в @clavis_vpn_bot</a>
    </div>
</body>
</html>"""
                    return HTMLResponse(content=html)

            # New IP — create subscription
            from services.key_service import KeyService

            guest = User(telegram_id=0, username="[web-trial]")
            db.add(guest)
            db.flush()
            guest.telegram_id = -guest.id

            expires_at = datetime.utcnow() + timedelta(hours=48)
            subscription = Subscription(
                user_id=guest.id,
                is_test=False,
                is_active=True,
                expires_at=expires_at,
                device_limit=1,
                plan_type='free',
            )
            db.add(subscription)
            db.flush()

            try:
                KeyService.ensure_keys_exist(db, subscription, guest.telegram_id)
            except Exception as e:
                logger.error(f"Failed to create key for web trial ip={ip}: {e}", exc_info=True)
                raise HTTPException(status_code=503, detail="No free servers available. Try again later.")

            db.add(WebTrialActivation(
                ip_address=ip,
                subscription_token=subscription.token,
            ))
            db.commit()

            logger.info(f"Web trial activated: ip={ip}, sub={subscription.id}")
            sub_url = f"{base}/sub/{subscription.token}"
            return HTMLResponse(content=_trial_page(sub_url, base, returning=False))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /trial: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ──────────────────────────────────────────────────────────────
# Free-VPN bootstrap (app censorship escape hatch)
# Contract — see FREE_VPN_BOOTSTRAP_PLAN.md:
#   GET /app/free-vpn/{install_id}
#     200 {"link": "vless://..."}      — a working Free-group WS key
#     429 {"error": "rate_limited"}    — over the per-install rate limit
#     400                              — malformed install_id
#     503                              — no Free server / key creation failed
# Issues a short-TTL (config) Free-group WS key (NOT tcp+REALITY). The sub has no
# user/account (throwaway) and is reaped by reap_bootstrap_subscriptions_job.
# ──────────────────────────────────────────────────────────────

_INSTALL_ID_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')


def _select_free_ws_inbound(db):
    """Pick a random active Free-group WS inbound. Returns (server, server_inbound) or (None, None)."""
    import random
    from config.settings import FREE_GROUP_NAME
    from database import Server, ServerInbound, ConnectionProfile

    servers = db.query(Server).filter(
        Server.protocol == 'xui',
        Server.is_active == True,
        Server.server_set == FREE_GROUP_NAME,
    ).all()

    candidates = []
    for server in servers:
        if not server.has_capacity:
            continue
        inbounds = db.query(ServerInbound).join(
            ConnectionProfile, ServerInbound.profile_id == ConnectionProfile.id
        ).filter(
            ServerInbound.server_id == server.id,
            ServerInbound.is_active == True,
            ConnectionProfile.network == 'ws',
        ).all()
        for si in inbounds:
            candidates.append((server, si))

    if not candidates:
        return None, None
    return random.choice(candidates)


def _live_bootstrap_link(db, token):
    """Return the vless link of a live (active, non-expired) bootstrap sub, else None."""
    if not token:
        return None
    from datetime import datetime
    sub = db.query(Subscription).filter(Subscription.token == token).first()
    if not sub or not sub.is_active or sub.expires_at <= datetime.utcnow():
        return None
    key = db.query(Key).filter(
        Key.subscription_id == sub.id,
        Key.is_active == True,
    ).first()
    if key and key.key_data and key.key_data.startswith("vless://"):
        return key.key_data
    return None


def _get_or_create_bootstrap_user(db):
    """Get (or create) the shared guest User that owns all throwaway bootstrap subs.

    Prod's subscriptions.user_id is NOT NULL (legacy schema), so bootstrap subs need
    a real owner; we funnel them all to one sentinel user instead of littering the
    users table.
    """
    from database import User
    from config.settings import BOOTSTRAP_GUEST_TELEGRAM_ID
    user = db.query(User).filter(
        User.telegram_id == BOOTSTRAP_GUEST_TELEGRAM_ID
    ).first()
    if not user:
        user = User(telegram_id=BOOTSTRAP_GUEST_TELEGRAM_ID, username="[bootstrap]")
        db.add(user)
        db.flush()
    return user


@router.get("/app/free-vpn/{install_id}")
async def free_vpn_bootstrap(install_id: str, request: Request) -> JSONResponse:
    """Hand out a short-lived Free-group WS key for the app's Telegram-login escape hatch."""
    import json
    from datetime import datetime, timedelta
    from config.settings import (
        BOOTSTRAP_SUB_NAME,
        FREE_VPN_BOOTSTRAP_TTL_MINUTES,
        FREE_VPN_BOOTSTRAP_WINDOW_MINUTES,
        FREE_VPN_BOOTSTRAP_MAX_PER_WINDOW,
        FREE_VPN_BOOTSTRAP_MAX_PER_DAY,
        FREE_VPN_BOOTSTRAP_TRAFFIC_MB,
        FREE_VPN_BOOTSTRAP_RATE_LIMIT_ENABLED,
    )
    from database import BootstrapGrant
    from vpn.xui_client import XUIClient

    # 1. Validate install_id (base64url, 16–64 chars)
    if not _INSTALL_ID_RE.match(install_id or ""):
        raise HTTPException(status_code=400, detail="invalid install_id")

    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"free-vpn bootstrap request: install_id={install_id[:8]}..., ip={client_ip}")

    now = datetime.utcnow()

    try:
        with get_db_session() as db:
            grant = db.query(BootstrapGrant).filter(
                BootstrapGrant.install_id == install_id
            ).first()

            # 2. Reuse-over-reissue: a live key is returned without touching the rate limit.
            if grant:
                existing = _live_bootstrap_link(db, grant.last_subscription_token)
                if existing:
                    logger.info(f"free-vpn bootstrap reuse: install_id={install_id[:8]}...")
                    return JSONResponse(content={"link": existing})

            # 3. Rate-limit (per install_id) from the pruned issuance history.
            issues = []
            if grant and grant.recent_issues:
                try:
                    issues = [datetime.fromisoformat(t) for t in json.loads(grant.recent_issues)]
                except Exception:
                    issues = []
            issues = [t for t in issues if now - t < timedelta(hours=24)]
            # The issuance ledger above is always maintained; only the 429 decision is
            # gated by the switch, so the limit can be toggled for testing without losing
            # history (see FREE_VPN_BOOTSTRAP_RATE_LIMIT_ENABLED in config/settings.py).
            if FREE_VPN_BOOTSTRAP_RATE_LIMIT_ENABLED:
                in_window = [
                    t for t in issues
                    if now - t < timedelta(minutes=FREE_VPN_BOOTSTRAP_WINDOW_MINUTES)
                ]
                if (len(in_window) >= FREE_VPN_BOOTSTRAP_MAX_PER_WINDOW
                        or len(issues) >= FREE_VPN_BOOTSTRAP_MAX_PER_DAY):
                    logger.info(f"free-vpn bootstrap rate-limited: install_id={install_id[:8]}...")
                    return JSONResponse(status_code=429, content={"error": "rate_limited"})

            # 4. Pick a Free-group WS inbound.
            server, si = _select_free_ws_inbound(db)
            if not server or not si:
                logger.error("free-vpn bootstrap: no active Free-group WS inbound available")
                raise HTTPException(status_code=503, detail="no_free_server")

            # 5. Create short-lived sub + WS key (shared guest owner: throwaway).
            guest = _get_or_create_bootstrap_user(db)
            expires_at = now + timedelta(minutes=FREE_VPN_BOOTSTRAP_TTL_MINUTES)
            sub = Subscription(
                user_id=guest.id,
                account_id=None,
                name=BOOTSTRAP_SUB_NAME,
                is_test=False,
                is_active=True,
                expires_at=expires_at,
                device_limit=1,
                plan_type='free',
                expiry_notified=True,  # suppress expiry-reminder noise for throwaway subs
            )
            db.add(sub)
            db.flush()  # assigns sub.id and auto-generated token

            client_id = f"app_boot_{install_id}"
            tlb = (FREE_VPN_BOOTSTRAP_TRAFFIC_MB * 1024 * 1024
                   if FREE_VPN_BOOTSTRAP_TRAFFIC_MB else 0)
            try:
                xui = XUIClient(server, server_inbound=si)
                key = xui.create_key(
                    sub, client_id,
                    remarks="Clavis Free (login)",
                    traffic_limit_bytes=tlb,
                )
                db.add(key)
                db.flush()
            except Exception as e:
                logger.error(
                    f"free-vpn bootstrap: key creation failed on {server.name}: {e}",
                    exc_info=True,
                )
                raise HTTPException(status_code=503, detail="key_creation_failed")

            link = key.key_data
            if not link or not link.startswith("vless://"):
                logger.error("free-vpn bootstrap: created key has no valid vless link")
                raise HTTPException(status_code=503, detail="bad_key")

            # 6. Record the grant (rate-limit ledger + reuse pointer).
            issues.append(now)
            if not grant:
                grant = BootstrapGrant(install_id=install_id, first_seen=now)
                db.add(grant)
            grant.last_issued_at = now
            grant.issue_count = (grant.issue_count or 0) + 1
            grant.recent_issues = json.dumps([t.isoformat() for t in issues])
            grant.last_subscription_token = sub.token

            db.commit()
            logger.info(
                f"free-vpn bootstrap issued: install_id={install_id[:8]}..., "
                f"sub={sub.id}, server={server.name}"
            )
            return JSONResponse(content={"link": link})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"free-vpn bootstrap error: install_id={install_id[:8]}...: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="server_error")
