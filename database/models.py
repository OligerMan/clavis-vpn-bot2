"""SQLAlchemy models for Clavis VPN Bot v2."""

import json
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


class User(Base):
    """Telegram user."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    ref_source = Column(String(100), nullable=True, index=True)
    # Link to Clavis app account — nullable (only set for users who went
    # through the app-integration flow; old and gated users stay NULL).
    account_id = Column(String(36), ForeignKey("clavis_accounts.id", ondelete="SET NULL"), nullable=True, index=True)

    # Relationships
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    config = relationship("UserConfig", back_populates="user", uselist=False, cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")
    referral_invites = relationship("ReferralInvite", back_populates="inviter", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, telegram_id={self.telegram_id}, username={self.username})>"


class Subscription(Base):
    """User subscription with unique token for subscription URL."""

    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    name = Column(String(100), default="Main")
    token = Column(String(36), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    device_limit = Column(Integer, default=5)
    plan_type = Column(String(20), default="basic")  # "basic" or "premium"
    is_test = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Renewal reminder tracking
    reminder_7d_sent = Column(Boolean, default=False)
    reminder_3d_sent = Column(Boolean, default=False)
    reminder_1d_sent = Column(Boolean, default=False)
    expiry_notified = Column(Boolean, default=False)
    fcm_notified_days = Column(Integer, default=-1)

    # Clavis app account link — nullable; old subs and non-app users stay NULL.
    account_id = Column(String(36), ForeignKey("clavis_accounts.id", ondelete="SET NULL"), nullable=True, index=True)

    # Relationships
    user = relationship("User", back_populates="subscriptions")
    keys = relationship("Key", back_populates="subscription", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="subscription")

    def __repr__(self):
        return f"<Subscription(id={self.id}, user_id={self.user_id}, name={self.name}, is_test={self.is_test})>"

    @property
    def is_expired(self) -> bool:
        """Check if subscription has expired."""
        return datetime.utcnow() > self.expires_at

    @property
    def days_until_expiry(self) -> int:
        """Days until subscription expires (negative if expired)."""
        delta = self.expires_at - datetime.utcnow()
        return delta.days

    def get_subscription_url(self, base_url: str) -> str:
        """Generate full subscription URL."""
        return f"{base_url.rstrip('/')}/sub/{self.token}"

    def reset_reminder_flags(self) -> None:
        """Reset all renewal reminder flags (call when subscription is extended)."""
        self.reminder_7d_sent = False
        self.reminder_3d_sent = False
        self.reminder_1d_sent = False
        self.expiry_notified = False
        self.fcm_notified_days = 1_000_000_000


@event.listens_for(Subscription, "before_insert")
def generate_subscription_token(mapper, connection, target):
    """Auto-generate UUID token for new subscriptions."""
    if not target.token:
        target.token = str(uuid.uuid4())


class Key(Base):
    """VPN key belonging to a subscription."""

    __tablename__ = "keys"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="SET NULL"), nullable=True)
    server_inbound_id = Column(Integer, ForeignKey("server_inbounds.id", ondelete="SET NULL"), nullable=True)
    protocol = Column(String(20), nullable=False)  # 'xui' or 'outline'
    remote_key_id = Column(String(255), nullable=True)  # ID on VPN server for API calls
    key_data = Column(Text, nullable=False)  # Full URI (vless://..., ss://...)
    remarks = Column(String(255), nullable=True)  # Display name
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Traffic tracking (for calculating daily diffs)
    last_traffic_total = Column(Integer, default=0)  # Last known total from 3x-ui (bytes)
    last_traffic_update = Column(DateTime, nullable=True)  # When last_traffic_total was updated

    # Relationships
    subscription = relationship("Subscription", back_populates="keys")
    server = relationship("Server", back_populates="keys")
    server_inbound = relationship("ServerInbound", back_populates="keys")
    traffic_logs = relationship("TrafficLog", back_populates="key", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Key(id={self.id}, protocol={self.protocol}, remarks={self.remarks})>"

    def update_traffic(self, new_total: int) -> int:
        """Update traffic total and return the diff.

        Args:
            new_total: Current total traffic from 3x-ui API (bytes)

        Returns:
            Diff since last update (bytes)
        """
        diff = max(0, new_total - self.last_traffic_total)
        self.last_traffic_total = new_total
        self.last_traffic_update = datetime.utcnow()
        return diff


class Server(Base):
    """VPN server configuration."""

    __tablename__ = "servers"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    host = Column(String(255), nullable=False)
    protocol = Column(String(20), nullable=False)  # 'xui' or 'outline'
    api_url = Column(String(500), nullable=True)
    api_credentials = Column(Text, nullable=True)  # Encrypted JSON
    capacity = Column(Integer, default=200)
    is_active = Column(Boolean, default=True)
    monitor_enabled = Column(Boolean, default=True)
    server_set = Column(String(50), default="default")

    # Relationships
    keys = relationship("Key", back_populates="server")
    inbounds = relationship("ServerInbound", back_populates="server", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Server(id={self.id}, name={self.name}, protocol={self.protocol})>"

    @property
    def current_load(self) -> int:
        """Count active keys on this server."""
        return len([k for k in self.keys if k.is_active])

    @property
    def has_capacity(self) -> bool:
        """Check if server can accept new keys (no hard limit, manual balancing)."""
        return self.is_active


class ServerGroup(Base):
    """Named server group for organizing servers."""

    __tablename__ = "server_groups"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ServerGroup(id={self.id}, name={self.name})>"


class ConnectionProfile(Base):
    """Reusable connection profile template (protocol + settings)."""

    __tablename__ = "connection_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    protocol = Column(String(20), default="vless")
    security = Column(String(20), default="reality")
    network = Column(String(20), default="tcp")
    flow = Column(String(50), default="xtls-rprx-vision")
    fingerprint = Column(String(20), default="chrome")
    sni = Column(String(255), nullable=False)
    dest = Column(String(255), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    server_inbounds = relationship("ServerInbound", back_populates="profile", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ConnectionProfile(id={self.id}, name={self.name}, sni={self.sni})>"


class ServerInbound(Base):
    """An inbound instance on a specific server, created from a profile."""

    __tablename__ = "server_inbounds"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False)
    profile_id = Column(Integer, ForeignKey("connection_profiles.id", ondelete="CASCADE"), nullable=False)
    inbound_id = Column(Integer, nullable=False)
    port = Column(Integer, nullable=False)
    public_key = Column(String(255), nullable=False)
    short_id = Column(String(255), nullable=False)
    private_key = Column(String(255), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    server = relationship("Server", back_populates="inbounds")
    profile = relationship("ConnectionProfile", back_populates="server_inbounds")
    keys = relationship("Key", back_populates="server_inbound")

    __table_args__ = (
        UniqueConstraint("server_id", "profile_id", name="uq_server_profile"),
    )

    def __repr__(self):
        return f"<ServerInbound(id={self.id}, server={self.server_id}, profile={self.profile_id}, inbound={self.inbound_id})>"


class UserConfig(Base):
    """Per-user routing configuration."""

    __tablename__ = "user_configs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    bypass_domains = Column(Text, default="[]")  # JSON array
    blocked_domains = Column(Text, default="[]")  # JSON array
    proxied_domains = Column(Text, default="[]")  # JSON array
    enabled_lists = Column(Text, default="[]")  # JSON array of RoutingList IDs
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="config")

    def __repr__(self):
        return f"<UserConfig(id={self.id}, user_id={self.user_id})>"

    # JSON helpers
    def get_bypass_domains(self) -> list[str]:
        return json.loads(self.bypass_domains or "[]")

    def set_bypass_domains(self, domains: list[str]):
        self.bypass_domains = json.dumps(domains)

    def get_blocked_domains(self) -> list[str]:
        return json.loads(self.blocked_domains or "[]")

    def set_blocked_domains(self, domains: list[str]):
        self.blocked_domains = json.dumps(domains)

    def get_proxied_domains(self) -> list[str]:
        return json.loads(self.proxied_domains or "[]")

    def set_proxied_domains(self, domains: list[str]):
        self.proxied_domains = json.dumps(domains)

    def get_enabled_lists(self) -> list[int]:
        return json.loads(self.enabled_lists or "[]")

    def set_enabled_lists(self, list_ids: list[int]):
        self.enabled_lists = json.dumps(list_ids)


class RoutingList(Base):
    """Admin-managed domain routing list."""

    __tablename__ = "routing_lists"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)  # 'ru_bypass', 'ads_block'
    display_name = Column(String(100), nullable=False)  # 'Russian sites', 'Ad blocker'
    type = Column(String(20), nullable=False)  # 'bypass', 'block', 'proxy'
    domains = Column(Text, default="[]")  # JSON array
    is_default = Column(Boolean, default=False)  # Auto-enable for new users
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<RoutingList(id={self.id}, name={self.name}, type={self.type})>"

    def get_domains(self) -> list[str]:
        return json.loads(self.domains or "[]")

    def set_domains(self, domains: list[str]):
        self.domains = json.dumps(domains)

    def add_domain(self, domain: str):
        domains = self.get_domains()
        if domain not in domains:
            domains.append(domain)
            self.set_domains(domains)

    def remove_domain(self, domain: str):
        domains = self.get_domains()
        if domain in domains:
            domains.remove(domain)
            self.set_domains(domains)


class TrafficLog(Base):
    """Daily traffic diff for abuse detection.

    Storage strategy:
    - Scheduler fetches all-time traffic from 3x-ui API daily
    - Calculates diff from previous day and stores here
    - Only last 30 days retained (old records auto-deleted)
    - For all-time stats: query 3x-ui API directly

    Example:
        Day 1: 3x-ui reports 100MB total → store diff=100MB
        Day 2: 3x-ui reports 250MB total → store diff=150MB
        Day 3: 3x-ui reports 400MB total → store diff=150MB
    """

    __tablename__ = "traffic_logs"

    id = Column(Integer, primary_key=True)
    key_id = Column(Integer, ForeignKey("keys.id", ondelete="CASCADE"), nullable=False)
    date = Column(DateTime, nullable=False)  # Date of the record (daily granularity)
    upload_diff = Column(Integer, default=0)  # Upload diff from previous day (bytes)
    download_diff = Column(Integer, default=0)  # Download diff from previous day (bytes)

    # Relationships
    key = relationship("Key", back_populates="traffic_logs")

    # Unique constraint: one record per key per day
    # Index for time-range queries and cleanup
    __table_args__ = (
        Index("ix_traffic_logs_key_date", "key_id", "date", unique=True),
        Index("ix_traffic_logs_date", "date"),  # For cleanup queries
    )

    def __repr__(self):
        return f"<TrafficLog(key_id={self.key_id}, date={self.date.date()}, down={self.download_diff}, up={self.upload_diff})>"

    @property
    def total_diff(self) -> int:
        """Total traffic diff for this day."""
        return self.upload_diff + self.download_diff

    @classmethod
    def cleanup_old_records(cls, session, days: int = 30):
        """Delete records older than specified days."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        deleted = session.query(cls).filter(cls.date < cutoff).delete()
        return deleted


class ActivityLog(Base):
    """User activity log for admin monitoring."""

    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, nullable=False, index=True)
    action = Column(String(50), nullable=False)
    details = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<ActivityLog(id={self.id}, telegram_id={self.telegram_id}, action={self.action})>"


class Transaction(Base):
    """Payment transaction."""

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Integer, nullable=False)  # kopeks for card, whole stars for Stars
    plan = Column(String(50), nullable=False)  # '90_days', '365_days'
    payment_method = Column(String(20), default='card')  # 'card' or 'stars'
    status = Column(String(20), default="pending")  # 'pending', 'completed', 'failed'
    yookassa_payment_id = Column(String(255), unique=True, nullable=True)  # Prevents double activation
    telegram_charge_id = Column(String(255), nullable=True)  # For Stars refunds
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="transactions")
    subscription = relationship("Subscription", back_populates="transactions")

    def __repr__(self):
        return f"<Transaction(id={self.id}, user_id={self.user_id}, amount={self.amount}, status={self.status})>"

    @property
    def amount_rub(self) -> float:
        """Amount in rubles."""
        return self.amount / 100

    def complete(self):
        """Mark transaction as completed."""
        self.status = "completed"
        self.completed_at = datetime.utcnow()

    def fail(self):
        """Mark transaction as failed."""
        self.status = "failed"


class RefLink(Base):
    """Referral link with access control."""

    __tablename__ = "ref_links"

    id = Column(Integer, primary_key=True)
    tag = Column(String(100), unique=True, nullable=False, index=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    access_list = relationship("RefLinkAccess", back_populates="ref_link", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<RefLink(id={self.id}, tag={self.tag})>"


class RefLinkAccess(Base):
    """Per-user access to a referral link's statistics."""

    __tablename__ = "ref_link_access"

    id = Column(Integer, primary_key=True)
    ref_link_id = Column(Integer, ForeignKey("ref_links.id", ondelete="CASCADE"), nullable=False)
    telegram_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    ref_link = relationship("RefLink", back_populates="access_list")

    __table_args__ = (
        UniqueConstraint("ref_link_id", "telegram_id", name="uq_reflink_user"),
    )


class ReferralInvite(Base):
    """Invite link generated by a paid user to give a friend a free 7-day subscription."""

    __tablename__ = "referral_invites"

    id = Column(Integer, primary_key=True)
    # 12-char random alphanumeric code, URL-safe
    code = Column(String(16), unique=True, nullable=False, index=True)
    # The paid user who generated this invite
    inviter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Token of the friend's subscription (filled on first /invite/{code} access)
    subscription_token = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    activated_at = Column(DateTime, nullable=True)

    inviter = relationship("User", back_populates="referral_invites")

    def __repr__(self):
        return f"<ReferralInvite(id={self.id}, code={self.code}, activated={self.activated_at is not None})>"


class WebTrialActivation(Base):
    """Tracks IP addresses that received a free 48h trial via the /trial web endpoint."""

    __tablename__ = "web_trial_activations"

    id = Column(Integer, primary_key=True)
    ip_address = Column(String(45), nullable=False, index=True)  # IPv4 or IPv6
    subscription_token = Column(String(36), nullable=False)       # token of the created sub
    created_at = Column(DateTime, default=datetime.utcnow)
    visit_count = Column(Integer, default=1)  # total /trial hits from this IP

    def __repr__(self):
        return f"<WebTrialActivation(id={self.id}, ip={self.ip_address}, visits={self.visit_count})>"


class BootstrapGrant(Base):
    """Rate-limit + reuse ledger for the free-VPN bootstrap endpoint, keyed by install_id.

    One row per app install that ever hit GET /app/free-vpn/{install_id}. Holds the
    rolling issuance history (for rate limiting) and a pointer to the currently-live
    bootstrap subscription (for reuse-over-reissue). Rows are kept even after the sub
    is reaped, so abusive auto-generated install_ids stay observable.
    """

    __tablename__ = "bootstrap_grants"

    install_id = Column(String(64), primary_key=True)
    first_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_issued_at = Column(DateTime, nullable=True)
    issue_count = Column(Integer, default=0, nullable=False)  # total lifetime issuances
    # JSON array of recent issuance ISO timestamps (pruned to last 24h) — source of
    # truth for both the short-window and rolling-24h rate limits.
    recent_issues = Column(Text, default="[]", nullable=False)
    # Token of the currently-live bootstrap subscription (for reuse-over-reissue).
    last_subscription_token = Column(String(36), nullable=True)

    def __repr__(self):
        return f"<BootstrapGrant(install_id={self.install_id[:8]}..., count={self.issue_count})>"


# ──────────────────────────────────────────────────────────────
# Clavis app integration: account / device / login / sync models
# See F:\Projects\clavis-app\docs\server-integration-spec.md §2.
# All tokens in distinct namespaces — see plan "Изоляция токенов".
# ──────────────────────────────────────────────────────────────

class ClavisAccount(Base):
    """A Clavis app account. Server-issued UUID. Links users ↔ devices ↔ subscriptions."""

    __tablename__ = "clavis_accounts"

    id = Column(String(36), primary_key=True)  # UUID v4
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_active = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Argon2id-encoded recovery phrase hash; NULL for Telegram-only accounts.
    recovery_phrase_hash = Column(String(200), nullable=True)
    # HMAC(SERVER_SECRET, normalized_phrase)[:16] — indexed shortcut for recovery
    # to avoid O(n) Argon2 scans. NULL for TG-only accounts.
    phrase_lookup_key = Column(String(32), nullable=True, index=True)

    # Relationships
    devices = relationship("Device", back_populates="account", cascade="all, delete-orphan")
    login_tokens = relationship("LoginToken", back_populates="account", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ClavisAccount(id={self.id})>"


@event.listens_for(ClavisAccount, "before_insert")
def _generate_account_id(mapper, connection, target):
    if not target.id:
        target.id = str(uuid.uuid4())


class Device(Base):
    """A registered device. Ephemeral (account_id NULL) until login/create/recover."""

    __tablename__ = "devices"

    id = Column(String(36), primary_key=True)  # UUID v4
    account_id = Column(
        String(36),
        ForeignKey("clavis_accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    device_token = Column(String(128), unique=True, nullable=False, index=True)
    # One of: 'telegram','ios','android','macos','windows','linux'
    device_type = Column(String(16), nullable=False)
    device_name = Column(String(128), nullable=True)
    # Client-generated per-install UUID (stable across logout/login on the
    # same OS install). Used as a dedup key during promote-to-account — see
    # spec §3.11. NULL for old clients and for Telegram devices.
    install_id = Column(String(64), nullable=True)
    fcm_token = Column(String(255), nullable=True)
    fcm_token_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)

    account = relationship("ClavisAccount", back_populates="devices")

    def __repr__(self):
        return f"<Device(id={self.id}, type={self.device_type}, account={self.account_id})>"


@event.listens_for(Device, "before_insert")
def _generate_device_id(mapper, connection, target):
    if not target.id:
        target.id = str(uuid.uuid4())


class LoginToken(Base):
    """One-time cross-device login token (Telegram → app). 10-min TTL, single-use."""

    __tablename__ = "login_tokens"

    # Random 64-char base64url; never collides with subscription.token (UUID 36 chars).
    token = Column(String(96), primary_key=True)
    account_id = Column(
        String(36),
        ForeignKey("clavis_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # +10 min
    claimed_at = Column(DateTime, nullable=True)
    claimed_by_device = Column(
        String(36),
        ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    account = relationship("ClavisAccount", back_populates="login_tokens")

    def __repr__(self):
        return f"<LoginToken(token={self.token[:8]}..., account={self.account_id}, claimed={self.claimed_at is not None})>"


class SyncPair(Base):
    """Short-code pairing flow (show QR ↔ scan). 2-min TTL, single-success."""

    __tablename__ = "sync_pairs"

    # 8 uppercase alphanumeric; distinct namespace from UUIDs.
    pair_code = Column(String(8), primary_key=True)
    shower_device = Column(
        String(36),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    claimer_device = Column(
        String(36),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=True,
    )
    status = Column(String(16), nullable=False, default="pending")  # pending|claimed|expired|error
    error_reason = Column(String(32), nullable=True)  # both_empty|both_full|cross_account
    claim_attempts = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # +2 min

    def __repr__(self):
        return f"<SyncPair(code={self.pair_code}, status={self.status})>"


class AppPayment(Base):
    """In-app payment via YooKassa direct API (not Telegram Payments)."""

    __tablename__ = "app_payments"

    id = Column(String(36), primary_key=True)
    yookassa_id = Column(String(64), unique=True, nullable=False, index=True)
    account_id = Column(
        String(36),
        ForeignKey("clavis_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_id = Column(String(36), nullable=True)
    plan_id = Column(String(16), nullable=False)
    amount_value = Column(String(16), nullable=False)
    email = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    paid = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<AppPayment(yookassa={self.yookassa_id}, status={self.status})>"


@event.listens_for(AppPayment, "before_insert")
def _generate_app_payment_id(mapper, connection, target):
    if not target.id:
        target.id = str(uuid.uuid4())
