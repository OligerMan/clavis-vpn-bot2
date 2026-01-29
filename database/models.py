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

    # Relationships
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    config = relationship("UserConfig", back_populates="user", uselist=False, cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, telegram_id={self.telegram_id}, username={self.username})>"


class Subscription(Base):
    """User subscription with unique token for subscription URL."""

    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), default="Main")
    token = Column(String(36), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    device_limit = Column(Integer, default=5)
    is_test = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

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
    capacity = Column(Integer, default=100)
    is_active = Column(Boolean, default=True)

    # Relationships
    keys = relationship("Key", back_populates="server")

    def __repr__(self):
        return f"<Server(id={self.id}, name={self.name}, protocol={self.protocol})>"

    @property
    def current_load(self) -> int:
        """Count active keys on this server."""
        return len([k for k in self.keys if k.is_active])

    @property
    def has_capacity(self) -> bool:
        """Check if server can accept new keys."""
        return self.is_active and self.current_load < self.capacity


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


class Transaction(Base):
    """Payment transaction."""

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Integer, nullable=False)  # Amount in kopeks
    plan = Column(String(50), nullable=False)  # '90_days', '365_days'
    status = Column(String(20), default="pending")  # 'pending', 'completed', 'failed'
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
