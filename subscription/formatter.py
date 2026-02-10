"""Format subscription responses for v2ray clients."""

import base64
import re
from typing import List, Any
from urllib.parse import unquote, quote


def modify_vless_remark(vless_uri: str, new_remark: str) -> str:
    """Modify the remark (fragment) of a VLESS URI.

    Args:
        vless_uri: Original VLESS URI
        new_remark: New remark to set

    Returns:
        VLESS URI with modified remark
    """
    # Split URI and fragment
    if "#" in vless_uri:
        base_uri = vless_uri.split("#")[0]
    else:
        base_uri = vless_uri

    # URL-encode the new remark
    encoded_remark = quote(new_remark, safe="")

    return f"{base_uri}#{encoded_remark}"


def format_subscription_response(
    keys: List[Any],
    is_expired: bool = False
) -> str:
    """Format subscription response as base64-encoded VLESS URIs.

    Args:
        keys: List of Key objects with key_data containing VLESS URIs
        is_expired: Whether subscription is expired (modifies remarks)

    Returns:
        Base64-encoded string of newline-separated VLESS URIs

    Raises:
        ValueError: If keys list is empty or contains invalid URIs
    """
    if not keys:
        raise ValueError("No keys provided")

    vless_uris = []

    for key in keys:
        # Get VLESS URI from key_data
        uri = key.key_data
        if not uri:
            continue

        # Validate URI format
        if not uri.startswith("vless://"):
            continue

        # Modify remark if subscription is expired/inactive
        if is_expired:
            # Extract server name from URI for better UX
            server_name = _extract_server_name(uri)
            expired_remark = f"⏰ Clavis {server_name} - Expired, please renew subscription"
            uri = modify_vless_remark(uri, expired_remark)

        vless_uris.append(uri)

    if not vless_uris:
        raise ValueError("No valid VLESS URIs found in keys")

    # Join URIs with newlines
    uris_text = "\n".join(vless_uris)

    # Base64 encode
    encoded = base64.b64encode(uris_text.encode("utf-8")).decode("utf-8")

    return encoded


def _extract_server_name(vless_uri: str) -> str:
    """Extract server name from VLESS URI remark.

    Args:
        vless_uri: VLESS URI

    Returns:
        Server name or "VPN" as fallback
    """
    # Try to extract remark (fragment)
    if "#" in vless_uri:
        remark = vless_uri.split("#")[1]
        remark = unquote(remark)

        # Try to extract server name from patterns like:
        # "Clavis VPN - TG123456"
        # "Clavis Server 1"
        # "cl23.example.com"
        match = re.search(r"(?:Clavis|Server|cl\d+)", remark, re.IGNORECASE)
        if match:
            return match.group(0)

    return "VPN"


def parse_subscription_response(encoded: str) -> List[str]:
    """Parse base64-encoded subscription response.

    Helper for testing and debugging.

    Args:
        encoded: Base64-encoded subscription response

    Returns:
        List of VLESS URIs

    Raises:
        ValueError: If response is not valid base64
    """
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
        uris = [uri.strip() for uri in decoded.split("\n") if uri.strip()]
        return uris
    except Exception as e:
        raise ValueError(f"Invalid base64 response: {e}")
