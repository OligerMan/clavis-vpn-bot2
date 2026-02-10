"""Subscription server API routes."""

import base64
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse
from typing import List, Dict, Any

from database import get_db_session, Subscription, Key
from vpn.xui_uri_builder import parse_vless_uri
from subscription.cache import (
    get_cached_subscription,
    cache_subscription_response,
    get_cache_stats,
)
from subscription.formatter import format_subscription_response


def _make_profile_title(subscription: Subscription) -> str:
    """Encode profile title in base64: format for v2ray clients.

    Format: "base64:<b64 encoded title>"
    Title includes service name and short token for identification.
    """
    token_short = subscription.token[:8] if subscription.token else "unknown"
    title = f"Clavis VPN\n{token_short}"
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
        title = f"Clavis VPN\n{token_short}"
        encoded_title = base64.b64encode(title.encode("utf-8")).decode("utf-8")
        headers = {
            "profile-title": f"base64:{encoded_title}",
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

            # Format response (will modify remarks if expired)
            response = format_subscription_response(keys, is_expired=is_expired)

            # Cache response (cache expired subscriptions too, they rarely change)
            token_short = subscription.token[:8] if subscription.token else "unknown"
            expires_ts = int(subscription.expires_at.timestamp())
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
                "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(subscription.expires_at.timestamp())}",
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


@router.get("/cache/stats")
async def get_cache_statistics() -> JSONResponse:
    """Get cache statistics.

    Returns:
        JSONResponse with cache statistics
    """
    stats = get_cache_stats()
    return JSONResponse(content=stats)


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

            uris = []
            for key in keys:
                uri = key.key_data
                if not uri or not uri.startswith("vless://"):
                    continue

                # Modify remark if expired
                if is_expired:
                    from subscription.formatter import _extract_server_name
                    server_name = _extract_server_name(uri)
                    expired_remark = f"⏰ Clavis {server_name} - Expired, please renew subscription"
                    uri = modify_vless_remark(uri, expired_remark)

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
                "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(subscription.expires_at.timestamp())}",
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
                "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(subscription.expires_at.timestamp())}",
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
