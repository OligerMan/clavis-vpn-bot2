"""Key management and traffic aggregation service."""

import logging
from typing import Dict, List

from sqlalchemy.orm import Session

from database.models import Key, Server, Subscription
from vpn.xui_client import XUIClient

logger = logging.getLogger(__name__)


class KeyService:
    """Service for managing VPN keys and traffic statistics."""

    @staticmethod
    def get_available_server(db: Session) -> Server:
        """
        Get an available server for key creation.

        Args:
            db: Database session

        Returns:
            Available Server object

        Raises:
            ValueError: If no servers available
        """
        server = db.query(Server).filter(
            Server.protocol == 'xui',
            Server.is_active == True
        ).first()

        if not server:
            raise ValueError("No available servers")

        return server

    @staticmethod
    def create_subscription_keys(
        db: Session,
        subscription: Subscription,
        user_telegram_id: int
    ) -> List[Key]:
        """
        Create VPN keys for a subscription via XUIClient.

        Args:
            db: Database session
            subscription: Subscription object
            user_telegram_id: User's Telegram ID

        Returns:
            List of created Key objects

        Raises:
            ValueError: If server not available or key creation fails
        """
        server = KeyService.get_available_server(db)

        client = XUIClient(server)

        try:
            key = client.create_key(subscription, user_telegram_id)
        except Exception as e:
            raise ValueError(f"Failed to create key: {e}")

        db.add(key)
        db.commit()
        db.refresh(key)

        return [key]

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
