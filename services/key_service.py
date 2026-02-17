"""Key management and traffic aggregation service."""

import logging
import random
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

from sqlalchemy.orm import Session

from config.settings import USER_SERVER_LIMIT
from database.models import Key, Server, Subscription
from vpn.xui_client import XUIClient

logger = logging.getLogger(__name__)


class KeyService:
    """Service for managing VPN keys and traffic statistics."""

    @staticmethod
    def get_all_active_servers(db: Session) -> List[Server]:
        """
        Get all active XUI servers for key creation.

        Args:
            db: Database session

        Returns:
            List of active Server objects

        Raises:
            ValueError: If no servers available
        """
        servers = db.query(Server).filter(
            Server.protocol == 'xui',
            Server.is_active == True
        ).all()

        if not servers:
            raise ValueError("No available servers")

        return servers

    @staticmethod
    def get_activated_groups(db: Session) -> List[str]:
        """
        Get distinct server_set names that have at least one active server.

        Returns:
            List of group names
        """
        rows = db.query(Server.server_set).filter(
            Server.protocol == 'xui',
            Server.is_active == True,
        ).distinct().all()
        return [r[0] or "default" for r in rows]

    @staticmethod
    def select_servers_for_user(
        db: Session,
        subscription: Subscription,
        limit: int = None,
    ) -> List[Server]:
        """
        Select servers for a user — 1 per activated group.

        If limit is None, computes it as number of distinct activated groups.
        Picks 1 random server from each group the user doesn't already have
        a key in.

        Args:
            db: Database session
            subscription: Subscription to check existing keys for
            limit: Maximum number of servers to return (default: 1 per group)

        Returns:
            List of Server objects to create keys on (may be empty)
        """
        all_servers = db.query(Server).filter(
            Server.protocol == 'xui',
            Server.is_active == True,
        ).all()

        if not all_servers:
            return []

        # Servers user already has active managed keys on
        existing_server_ids = set(
            sid for (sid,) in db.query(Key.server_id).filter(
                Key.subscription_id == subscription.id,
                Key.server_id.isnot(None),
                Key.is_active == True,
            ).distinct().all()
        )

        # Groups user already has keys in
        existing_groups = set()
        for server in all_servers:
            if server.id in existing_server_ids:
                existing_groups.add(server.server_set or "default")

        # Group available (not yet assigned) servers by set
        sets: Dict[str, List[Server]] = defaultdict(list)
        for server in all_servers:
            group = server.server_set or "default"
            if group not in existing_groups and server.has_capacity:
                sets[group].append(server)

        if limit is None:
            # 1 per group: only fill groups user is missing
            limit = len(sets)

        # Shuffle within each set for randomness
        for servers_in_set in sets.values():
            random.shuffle(servers_in_set)

        # Pick 1 from each group (up to limit)
        selected: List[Server] = []
        for group_name, servers_in_set in sets.items():
            if len(selected) >= limit:
                break
            if servers_in_set:
                selected.append(servers_in_set[0])

        return selected

    @staticmethod
    def create_subscription_keys(
        db: Session,
        subscription: Subscription,
        user_telegram_id: int,
        servers: List[Server] = None,
    ) -> List[Key]:
        """
        Create VPN keys on selected servers for a subscription.

        If servers is None, uses select_servers_for_user() to pick them.

        Args:
            db: Database session
            subscription: Subscription object
            user_telegram_id: User's Telegram ID
            servers: Optional explicit list of servers to create keys on

        Returns:
            List of created Key objects

        Raises:
            ValueError: If no servers available or all servers failed
        """
        if servers is None:
            servers = KeyService.select_servers_for_user(db, subscription)

        if not servers:
            raise ValueError("No available servers")

        created_keys = []
        errors = []

        for server in servers:
            try:
                client = XUIClient(server)
                key = client.create_key(subscription, user_telegram_id)
                db.add(key)
                created_keys.append(key)
                logger.info(f"Created key on server {server.name} for subscription {subscription.id}")
            except Exception as e:
                logger.warning(f"Failed to create key on server {server.name}: {e}")
                errors.append(f"{server.name}: {e}")

        if not created_keys:
            raise ValueError(f"Failed to create keys on all servers: {'; '.join(errors)}")

        db.commit()
        for key in created_keys:
            db.refresh(key)

        return created_keys

    @staticmethod
    def ensure_keys_exist(
        db: Session,
        subscription: Subscription,
        user_telegram_id: int,
    ) -> List[Key]:
        """
        Ensure the subscription has keys in all activated groups.

        Idempotent: if user already has a key in every activated group,
        returns existing keys. Otherwise creates keys to fill missing groups.

        Note: keys are NOT auto-created for newly added groups.
        Use /activate_group for that.

        Args:
            db: Database session
            subscription: Subscription object
            user_telegram_id: User's Telegram ID

        Returns:
            List of all active keys for this subscription
        """
        # select_servers_for_user already computes which groups are missing
        servers = KeyService.select_servers_for_user(db, subscription)
        if servers:
            try:
                KeyService.create_subscription_keys(
                    db, subscription, user_telegram_id, servers=servers,
                )
            except ValueError as e:
                logger.warning(f"Could not create all keys for sub {subscription.id}: {e}")

        # Return all active keys (managed + legacy)
        return db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.is_active == True,
        ).all()

    @staticmethod
    def get_subscription_traffic(db: Session, subscription: Subscription) -> Dict[str, float]:
        """
        Aggregate traffic statistics from all keys in a subscription.

        Args:
            db: Database session
            subscription: Subscription object

        Returns:
            Dictionary with upload_gb, download_gb, total_gb
        """
        total_upload = 0
        total_download = 0

        # Only query managed keys (legacy keys have no server to fetch traffic from)
        keys = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.server_id.isnot(None),
            Key.is_active == True
        ).all()

        # Group keys by server to reuse XUIClient connections
        keys_by_server: Dict[int, list] = {}
        for key in keys:
            keys_by_server.setdefault(key.server_id, []).append(key)

        for server_id, server_keys in keys_by_server.items():
            server = db.query(Server).filter(Server.id == server_id).first()
            if not server or not server.is_active:
                continue

            try:
                client = XUIClient(server)
                for key in server_keys:
                    try:
                        traffic = client.get_traffic(key)
                        total_upload += traffic.upload_bytes
                        total_download += traffic.download_bytes
                    except Exception:
                        continue
            except Exception:
                continue

        upload_gb = total_upload / (1024 ** 3)
        download_gb = total_download / (1024 ** 3)
        total_gb = upload_gb + download_gb

        return {
            'upload_gb': round(upload_gb, 2),
            'download_gb': round(download_gb, 2),
            'total_gb': round(total_gb, 2)
        }

    @staticmethod
    def delete_subscription_keys(db: Session, subscription: Subscription) -> None:
        """
        Delete all keys for a subscription.

        Args:
            db: Database session
            subscription: Subscription object
        """
        keys = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.is_active == True
        ).all()

        # Group keys by server
        keys_by_server: Dict[int, list] = {}
        for key in keys:
            if key.server_id not in keys_by_server:
                keys_by_server[key.server_id] = []
            keys_by_server[key.server_id].append(key)

        for server_id, server_keys in keys_by_server.items():
            server = db.query(Server).filter(Server.id == server_id).first()
            if not server or not server.is_active:
                # Still mark keys inactive even if server unreachable
                for key in server_keys:
                    key.is_active = False
                continue

            try:
                client = XUIClient(server)
                for key in server_keys:
                    try:
                        client.delete_key(key)
                    except Exception:
                        pass
                    key.is_active = False
            except Exception:
                for key in server_keys:
                    key.is_active = False

        db.commit()

    @staticmethod
    def has_legacy_keys(db: Session, user) -> bool:
        """Check if user has any active legacy keys (server_id IS NULL)."""
        return db.query(Key).join(Subscription).filter(
            Subscription.user_id == user.id,
            Key.server_id.is_(None),
            Key.is_active == True,
        ).first() is not None

    @staticmethod
    def get_legacy_keys(db: Session, user) -> List[Key]:
        """Get all active legacy keys for a user (server_id IS NULL)."""
        return db.query(Key).join(Subscription).filter(
            Subscription.user_id == user.id,
            Key.server_id.is_(None),
            Key.is_active == True,
        ).all()

    @staticmethod
    def activate_group_for_all(
        db: Session,
        group_name: str,
    ) -> Dict[str, int]:
        """
        Create 1 key from the specified group for every active subscription
        that doesn't already have a key in that group.

        Args:
            db: Database session
            group_name: Server set / group name

        Returns:
            Dict with created, skipped, failed counts
        """
        # Get servers in this group
        group_servers = db.query(Server).filter(
            Server.protocol == 'xui',
            Server.is_active == True,
            Server.server_set == group_name,
        ).all()

        if not group_servers:
            raise ValueError(f"No active servers in group '{group_name}'")

        # Get active, non-expired subscriptions that already have managed keys
        # (user interacted with new bot). Users without managed keys get keys
        # lazily via ensure_keys_exist when they interact.
        active_subs = db.query(Subscription).filter(
            Subscription.is_active == True,
            Subscription.expires_at > datetime.utcnow(),
        ).all()

        # Filter to subs that have at least one managed key (server_id not null)
        subs_with_keys = []
        for sub in active_subs:
            has_managed = db.query(Key).filter(
                Key.subscription_id == sub.id,
                Key.server_id.isnot(None),
                Key.is_active == True,
            ).first()
            if has_managed:
                subs_with_keys.append(sub)

        stats = {"created": 0, "skipped": 0, "skipped_no_keys": len(active_subs) - len(subs_with_keys), "failed": 0}

        for sub in subs_with_keys:
            # Check if user already has a key in this group
            existing = db.query(Key).join(Server).filter(
                Key.subscription_id == sub.id,
                Key.is_active == True,
                Key.server_id.isnot(None),
                Server.server_set == group_name,
            ).first()

            if existing:
                stats["skipped"] += 1
                continue

            # Pick random server from group with capacity
            candidates = [s for s in group_servers if s.has_capacity]
            if not candidates:
                stats["failed"] += 1
                continue

            server = random.choice(candidates)
            try:
                client = XUIClient(server)
                key = client.create_key(sub, sub.user.telegram_id)
                db.add(key)
                db.commit()
                db.refresh(key)
                stats["created"] += 1
                logger.info(
                    f"activate_group: created key on {server.name} "
                    f"for sub {sub.id} (user {sub.user.telegram_id})"
                )
            except Exception as e:
                logger.warning(
                    f"activate_group: failed to create key on {server.name} "
                    f"for sub {sub.id}: {e}"
                )
                stats["failed"] += 1

        return stats

    @staticmethod
    def update_subscription_keys_expiry(db: Session, subscription: Subscription) -> int:
        """
        Update expiry time for all active keys in a subscription.

        Args:
            db: Database session
            subscription: Subscription object with updated expires_at

        Returns:
            Number of keys successfully updated

        Raises:
            ValueError: If subscription has no active keys
        """
        # Only update managed keys (server_id IS NOT NULL); legacy keys are unmanaged
        keys = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.server_id.isnot(None),
            Key.is_active == True
        ).all()

        if not keys:
            raise ValueError("No active managed keys found for subscription")

        new_expiry_ms = int(subscription.expires_at.timestamp() * 1000)
        updated_count = 0
        errors = []

        # Group keys by server
        keys_by_server: Dict[int, list] = {}
        for key in keys:
            if key.server_id not in keys_by_server:
                keys_by_server[key.server_id] = []
            keys_by_server[key.server_id].append(key)

        for server_id, server_keys in keys_by_server.items():
            server = db.query(Server).filter(Server.id == server_id).first()
            if not server or not server.is_active:
                logger.warning(f"Server {server_id} not found or inactive, skipping key updates")
                continue

            try:
                client = XUIClient(server)
                for key in server_keys:
                    try:
                        client.update_key_expiry(key, new_expiry_ms)
                        updated_count += 1
                        logger.info(f"Updated expiry for key {key.remote_key_id} on server {server.name}")
                    except Exception as e:
                        logger.warning(f"Failed to update expiry for key {key.remote_key_id}: {e}")
                        errors.append(f"{server.name}/{key.remote_key_id}: {e}")
            except Exception as e:
                logger.error(f"Failed to connect to server {server.name}: {e}")
                errors.append(f"{server.name}: {e}")

        if updated_count == 0:
            raise ValueError(f"Failed to update any keys: {'; '.join(errors)}")

        logger.info(f"Updated expiry for {updated_count}/{len(keys)} keys in subscription {subscription.id}")
        return updated_count
