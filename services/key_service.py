"""Key management and traffic aggregation service."""

import logging
import random
from collections import defaultdict
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
    def select_servers_for_user(
        db: Session,
        subscription: Subscription,
        limit: int = USER_SERVER_LIMIT,
    ) -> List[Server]:
        """
        Select servers for a user, respecting server sets and limit.

        Round-robins across server_set groups: picks 1 from each set
        (randomized within set) before picking a 2nd from any set, etc.

        Args:
            db: Database session
            subscription: Subscription to check existing keys for
            limit: Maximum number of servers to return

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

        # Group available (not yet assigned) servers by set
        sets: Dict[str, List[Server]] = defaultdict(list)
        for server in all_servers:
            if server.id not in existing_server_ids and server.has_capacity:
                sets[server.server_set or "default"].append(server)

        # Shuffle within each set for randomness
        for servers_in_set in sets.values():
            random.shuffle(servers_in_set)

        # Round-robin across sets
        selected: List[Server] = []
        remaining = limit - len(existing_server_ids)

        while remaining > 0 and sets:
            picked_this_round = False
            empty_sets = []
            for set_name, servers_in_set in sets.items():
                if remaining <= 0:
                    break
                if servers_in_set:
                    selected.append(servers_in_set.pop(0))
                    remaining -= 1
                    picked_this_round = True
                if not servers_in_set:
                    empty_sets.append(set_name)

            # Remove exhausted sets
            for set_name in empty_sets:
                del sets[set_name]

            if not picked_this_round:
                break

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
        Ensure the subscription has keys up to USER_SERVER_LIMIT.

        Idempotent: if enough managed keys already exist, returns them as-is.
        Otherwise creates keys on additional servers to fill the gap.

        Args:
            db: Database session
            subscription: Subscription object
            user_telegram_id: User's Telegram ID

        Returns:
            List of all active keys for this subscription
        """
        # Count active managed keys (server_id IS NOT NULL)
        managed_keys_count = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.server_id.isnot(None),
            Key.is_active == True,
        ).count()

        if managed_keys_count < USER_SERVER_LIMIT:
            # Need more keys — select_servers_for_user already excludes existing
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

        keys = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.is_active == True
        ).all()

        # Group keys by server to reuse XUIClient connections
        keys_by_server: Dict[int, list] = {}
        for key in keys:
            if key.server_id not in keys_by_server:
                keys_by_server[key.server_id] = []
            keys_by_server[key.server_id].append(key)

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
        keys = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.is_active == True
        ).all()

        if not keys:
            raise ValueError("No active keys found for subscription")

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
