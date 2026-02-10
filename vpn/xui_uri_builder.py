"""VLESS URI builder for 3x-ui clients."""

from urllib.parse import quote, urlencode


def build_vless_uri(
    uuid: str,
    host: str,
    port: int,
    public_key: str,
    short_id: str,
    sni: str,
    remark: str,
    flow: str = "xtls-rprx-vision",
    fingerprint: str = "chrome",
    security: str = "reality",
    network: str = "tcp",
) -> str:
    """Build a VLESS Reality URI for VPN clients.

    Args:
        uuid: Client UUID
        host: Server hostname or IP
        port: Server port
        public_key: Reality public key (pbk)
        short_id: Reality short ID (sid)
        sni: Server Name Indication (e.g., yahoo.com)
        remark: Display name for the connection
        flow: XTLS flow type (default: xtls-rprx-vision)
        fingerprint: TLS fingerprint (default: chrome)
        security: Security type (default: reality)
        network: Network type (default: tcp)

    Returns:
        VLESS URI string like: vless://uuid@host:port?params#remark

    Example:
        >>> build_vless_uri(
        ...     uuid="550e8400-e29b-41d4-a716-446655440000",
        ...     host="vpn.example.com",
        ...     port=443,
        ...     public_key="abc123",
        ...     short_id="def456",
        ...     sni="yahoo.com",
        ...     remark="Clavis VPN",
        ... )
        'vless://550e8400-e29b-41d4-a716-446655440000@vpn.example.com:443?...'
    """
    # Build query parameters
    params = {
        "security": security,
        "encryption": "none",
        "pbk": public_key,
        "sid": short_id,
        "sni": sni,
        "fp": fingerprint,
        "type": network,
        "flow": flow,
        "headerType": "none",
    }

    # Build query string (don't encode the values yet, urlencode will do it)
    query_string = urlencode(params)

    # URL-encode the remark for fragment
    encoded_remark = quote(remark, safe="")

    # Construct the URI
    return f"vless://{uuid}@{host}:{port}?{query_string}#{encoded_remark}"


def parse_vless_uri(uri: str) -> dict:
    """Parse a VLESS URI into its components.

    Args:
        uri: VLESS URI string

    Returns:
        Dictionary with parsed components:
        - uuid: Client UUID
        - host: Server hostname
        - port: Server port
        - params: Query parameters dict
        - remark: Fragment (display name)

    Raises:
        ValueError: If URI format is invalid
    """
    from urllib.parse import parse_qs, unquote, urlparse

    if not uri.startswith("vless://"):
        raise ValueError("URI must start with 'vless://'")

    # Parse the URI
    # urlparse doesn't handle vless:// well, so we replace it temporarily
    parsed = urlparse(uri.replace("vless://", "https://"))

    # Extract UUID from username portion
    uuid = parsed.username
    if not uuid:
        raise ValueError("UUID not found in URI")

    # Extract host and port
    host = parsed.hostname
    port = parsed.port or 443

    if not host:
        raise ValueError("Host not found in URI")

    # Parse query parameters
    params = {}
    if parsed.query:
        for key, values in parse_qs(parsed.query).items():
            params[key] = values[0] if values else ""

    # Extract remark from fragment
    remark = unquote(parsed.fragment) if parsed.fragment else ""

    return {
        "uuid": uuid,
        "host": host,
        "port": port,
        "params": params,
        "remark": remark,
    }
