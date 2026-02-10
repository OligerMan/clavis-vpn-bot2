"""Data models and exceptions for 3x-ui API client."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# === Exceptions ===

class XUIError(Exception):
    """Base exception for all 3x-ui API errors."""

    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.message = message
        self.original_error = original_error


class XUIAuthError(XUIError):
    """Authentication failed - invalid credentials or session expired."""
    pass


class XUIConnectionError(XUIError):
    """Failed to connect to 3x-ui server."""
    pass


class XUIClientNotFoundError(XUIError):
    """Client with specified email/UUID not found on server."""
    pass


class XUIInboundError(XUIError):
    """Inbound configuration error (not found, invalid, etc.)."""
    pass


# === Data Classes ===

@dataclass
class TrafficStats:
    """Traffic statistics for a VPN client."""

    email: str
    upload_bytes: int
    download_bytes: int
    total_bytes: int
    enabled: bool
    expiry_time: Optional[datetime] = None

    @property
    def upload_mb(self) -> float:
        """Upload in megabytes."""
        return self.upload_bytes / (1024 * 1024)

    @property
    def download_mb(self) -> float:
        """Download in megabytes."""
        return self.download_bytes / (1024 * 1024)

    @property
    def total_mb(self) -> float:
        """Total traffic in megabytes."""
        return self.total_bytes / (1024 * 1024)

    @property
    def total_gb(self) -> float:
        """Total traffic in gigabytes."""
        return self.total_bytes / (1024 * 1024 * 1024)


@dataclass
class ClientInfo:
    """Information about a VPN client on the server."""

    uuid: str
    email: str
    enabled: bool
    inbound_id: int
    upload_bytes: int
    download_bytes: int
    total_bytes: int
    expiry_time: Optional[datetime] = None
    flow: Optional[str] = None
    limit_ip: int = 0
    total_gb: int = 0  # Traffic limit in GB (0 = unlimited)

    @property
    def is_expired(self) -> bool:
        """Check if client has expired."""
        if self.expiry_time is None:
            return False
        return datetime.utcnow() > self.expiry_time


@dataclass
class ServerHealth:
    """Health status of a 3x-ui server."""

    is_healthy: bool
    version: Optional[str] = None
    uptime: Optional[int] = None  # Seconds
    error_message: Optional[str] = None

    @property
    def uptime_hours(self) -> Optional[float]:
        """Uptime in hours."""
        if self.uptime is None:
            return None
        return self.uptime / 3600


@dataclass
class ConnectionSettings:
    """VLESS/Reality connection settings from server credentials."""

    port: int
    sni: str
    public_key: str  # pbk
    short_id: str  # sid
    flow: str = "xtls-rprx-vision"
    fingerprint: str = "chrome"
    security: str = "reality"
    network: str = "tcp"

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionSettings":
        """Create from dictionary (api_credentials JSON)."""
        return cls(
            port=data.get("port", 443),
            sni=data.get("sni", ""),
            public_key=data.get("pbk", ""),
            short_id=data.get("sid", ""),
            flow=data.get("flow", "xtls-rprx-vision"),
            fingerprint=data.get("fingerprint", "chrome"),
            security=data.get("security", "reality"),
            network=data.get("network", "tcp"),
        )
