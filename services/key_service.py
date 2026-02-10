"""Key management and traffic aggregation service."""

from typing import List, Dict, Optional
from sqlalchemy.orm import Session

from database.models import Server, Subscription, Key
from vpn.xui_client import XUIClient
from message_templates import Messages


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

        For now, creates a single key. In the future, may support multiple servers.

        Args:
            db: Database session
            subscription: Subscription object
            user_telegram_id: User's Telegram ID for remarks

        Returns:
            List of created Key objects

        Raises:
            ValueError: If server not available or key creation fails
        """
        # Get available server
        server = KeyService.get_available_server(db)

        # Initialize XUI client
        client = XUIClient(
            panel_url=server.api_url,
            username=server.username,
            password=server.password
        )

        # Login to panel
        if not client.login():
            raise ValueError(f"Failed to login to server {server.name}")

        # Generate remarks
        remarks = f"Clavis VPN - TG{user_telegram_id}"

        # Create key via XUI API
        try:
            key_data = client.create_key(
                subscription_id=subscription.id,
                telegram_id=user_telegram_id,
                remarks=remarks
            )
        except Exception as e:
            raise ValueError(f"Failed to create key: {str(e)}")

        # Create Key record in database
        key = Key(
            subscription_id=subscription.id,
            server_id=server.id,
            protocol='vless',
            key_data=key_data.get('uuid', ''),
            is_active=True
        )

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

        # Get all keys for this subscription
        keys = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.is_active == True
        ).all()

        for key in keys:
            # Get server for this key
            server = db.query(Server).filter(Server.id == key.server_id).first()

            if not server or not server.is_active:
                continue

            # Initialize XUI client
            client = XUIClient(
                panel_url=server.api_url,
                username=server.username,
                password=server.password
            )

            # Login and get traffic
            try:
                if client.login():
                    traffic = client.get_traffic(key.key_data)
                    if traffic:
                        total_upload += traffic.get('up', 0)
                        total_download += traffic.get('down', 0)
            except Exception:
                # Skip failed traffic queries
                continue

        # Convert bytes to GB
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
        Delete all keys for a subscription (e.g., when upgrading from test to paid).

        Args:
            db: Database session
            subscription: Subscription object
        """
        keys = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.is_active == True
        ).all()

        for key in keys:
            # Get server
            server = db.query(Server).filter(Server.id == key.server_id).first()

            if not server or not server.is_active:
                continue

            # Initialize XUI client
            client = XUIClient(
                panel_url=server.api_url,
                username=server.username,
                password=server.password
            )

            # Login and delete key
            try:
                if client.login():
                    client.delete_key(key.key_data)
            except Exception:
                # Continue even if deletion fails
                pass

            # Mark key as inactive
            key.is_active = False

        db.commit()
