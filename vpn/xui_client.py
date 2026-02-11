"""3x-ui API client wrapper using py3xui SDK."""

import json
import logging
import uuid as uuid_lib
from datetime import datetime
from typing import Optional

from py3xui import Api, Client

from database import Key, Server, Subscription

from .xui_models import (
    ClientInfo,
    ConnectionSettings,
    ServerHealth,
    TrafficStats,
    XUIAuthError,
    XUIClientNotFoundError,
    XUIConnectionError,
    XUIError,
    XUIInboundError,
)
from .xui_uri_builder import build_vless_uri

logger = logging.getLogger(__name__)


class XUIClient:
    """Wrapper for py3xui SDK with auto-reconnection and error handling.

    This client provides a high-level interface for managing VPN clients
    on a 3x-ui panel, integrating with our database models.

    Usage:
        server = db.query(Server).filter_by(protocol="xui").first()
        client = XUIClient(server)
        key = client.create_key(subscription, telegram_id=123456)
    """

    def __init__(self, server: Server):
        """Initialize the XUI client.

        Args:
            server: Server model with api_url and api_credentials

        Raises:
            XUIError: If credentials are invalid or missing
        """
        self.server = server
        self._api: Optional[Api] = None
        self._credentials = self._parse_credentials()
        self._connection_settings = ConnectionSettings.from_dict(
            self._credentials.get("connection_settings", {})
        )

    def _parse_credentials(self) -> dict:
        """Parse and validate server credentials."""
        if not self.server.api_credentials:
            raise XUIError("Server has no API credentials configured")

        try:
            creds = json.loads(self.server.api_credentials)
        except json.JSONDecodeError as e:
            raise XUIError(f"Invalid credentials JSON: {e}")

        required = ["username", "password", "inbound_id"]
        missing = [k for k in required if k not in creds]
        if missing:
            raise XUIError(f"Missing required credentials: {', '.join(missing)}")

        return creds

    @property
    def api(self) -> Api:
        """Get or create the py3xui API instance with authentication."""
        if self._api is None:
            self._connect()
        return self._api

    def _connect(self) -> None:
        """Establish connection and authenticate with the 3x-ui panel."""
        if not self.server.api_url:
            raise XUIConnectionError("Server has no API URL configured")

        try:
            use_tls_verify = self._credentials.get("use_tls_verify", True)
            self._api = Api(
                self.server.api_url,
                username=self._credentials["username"],
                password=self._credentials["password"],
                use_tls_verify=use_tls_verify,
            )
            self._api.login()
            logger.info(f"Connected to 3x-ui panel: {self.server.name}")
        except Exception as e:
            error_msg = str(e).lower()
            if "auth" in error_msg or "login" in error_msg or "401" in error_msg:
                raise XUIAuthError(f"Authentication failed: {e}", e)
            raise XUIConnectionError(f"Failed to connect: {e}", e)

    def _ensure_connected(self) -> None:
        """Ensure we have a valid connection, reconnecting if needed."""
        if self._api is None:
            self._connect()
            return

        # Try a simple API call to verify connection
        try:
            self._api.inbound.get_list()
        except Exception:
            logger.info("Connection lost, reconnecting...")
            self._api = None
            self._connect()

    def _generate_email(self, telegram_id: int, subscription_id: int) -> str:
        """Generate unique email identifier for a client.

        Format: clavis_{telegram_id}_{subscription_id}_s{server_id}
        """
        return f"clavis_{telegram_id}_{subscription_id}_s{self.server.id}"

    def _get_inbound_id(self) -> int:
        """Get the configured inbound ID."""
        return self._credentials["inbound_id"]

    def create_key(
        self,
        subscription: Subscription,
        user_telegram_id: int,
        remarks: Optional[str] = None,
    ) -> Key:
        """Create a new VPN key on the server.

        Args:
            subscription: Subscription model the key belongs to
            user_telegram_id: Telegram user ID for email generation
            remarks: Display name for the key (default: "Clavis VPN")

        Returns:
            Key model with populated key_data (VLESS URI)

        Raises:
            XUIError: On any API error
        """
        self._ensure_connected()

        # Generate unique identifiers
        client_uuid = str(uuid_lib.uuid4())
        email = self._generate_email(user_telegram_id, subscription.id)
        display_name = remarks or self.server.name
        inbound_id = self._get_inbound_id()

        # Calculate expiry timestamp (milliseconds since epoch)
        expiry_ms = int(subscription.expires_at.timestamp() * 1000)

        try:
            # Create client using py3xui
            client = Client(
                id=client_uuid,
                email=email,
                enable=True,
                expiry_time=expiry_ms,
                flow=self._connection_settings.flow,
                limit_ip=subscription.device_limit,
                total_gb=0,  # Unlimited traffic
            )

            self.api.client.add(inbound_id, [client])
            logger.info(f"Created client {email} on server {self.server.name}")

        except Exception as e:
            error_msg = str(e).lower()

            # Handle duplicate email by deleting old client first
            if "duplicate email" in error_msg:
                logger.warning(f"Client {email} already exists, deleting and retrying")
                temp_client_id = None
                temp_email = None
                try:
                    # Create dummy key object for deletion
                    dummy_key = Key(
                        subscription_id=subscription.id,
                        server_id=self.server.id,
                        protocol='xui',
                        remote_key_id=email,
                        key_data='',
                        remarks='',
                        is_active=False
                    )
                    # Try to delete existing client
                    try:
                        self.delete_key(dummy_key)
                        logger.info(f"Deleted duplicate client {email}")
                    except Exception as del_error:
                        # If "no client remained", add a temporary client first
                        if "no client remained" in str(del_error).lower():
                            logger.warning("Cannot delete last client, adding temporary then deleting duplicate")
                            temp_email = f"temp_{int(datetime.now().timestamp())}"
                            temp_client_id = str(uuid_lib.uuid4())
                            temp_client = Client(
                                id=temp_client_id,
                                email=temp_email,
                                enable=True,  # Keep it active
                                expiry_time=expiry_ms,
                                flow=self._connection_settings.flow,
                                limit_ip=1,
                                total_gb=0
                            )
                            self.api.client.add(inbound_id, [temp_client])
                            logger.info(f"Added temporary client {temp_email}")
                            # Now delete the duplicate
                            self.delete_key(dummy_key)
                            logger.info(f"Deleted duplicate client {email} (had to add temp first)")
                        else:
                            raise

                    # Retry creating the client
                    self.api.client.add(inbound_id, [client])
                    logger.info(f"Created client {email} after removing duplicate")

                    # Delete temporary client if it was created
                    if temp_client_id and temp_email:
                        try:
                            temp_key = Key(
                                subscription_id=subscription.id,
                                server_id=self.server.id,
                                protocol='xui',
                                remote_key_id=temp_email,
                                key_data='',
                                remarks='',
                                is_active=False
                            )
                            self.delete_key(temp_key)
                            logger.info(f"Deleted temporary client {temp_email}")
                        except Exception as temp_del_error:
                            logger.warning(f"Failed to delete temporary client {temp_email}: {temp_del_error}")
                            # Don't fail the whole operation if temp cleanup fails

                except Exception as retry_error:
                    raise XUIError(f"Failed to handle duplicate client: {retry_error}", retry_error)
            elif "inbound" in error_msg:
                raise XUIInboundError(f"Inbound error: {e}", e)
            else:
                raise XUIError(f"Failed to create client: {e}", e)

        # Build VLESS URI
        vless_uri = build_vless_uri(
            uuid=client_uuid,
            host=self.server.host,
            port=self._connection_settings.port,
            public_key=self._connection_settings.public_key,
            short_id=self._connection_settings.short_id,
            sni=self._connection_settings.sni,
            remark=display_name,
            flow=self._connection_settings.flow,
            fingerprint=self._connection_settings.fingerprint,
        )

        # Create Key model
        key = Key(
            subscription_id=subscription.id,
            server_id=self.server.id,
            protocol="xui",
            remote_key_id=email,  # Use email as remote ID for API lookups
            key_data=vless_uri,
            remarks=display_name,
            is_active=True,
        )

        return key

    def delete_key(self, key: Key) -> bool:
        """Delete a key from the server.

        Args:
            key: Key model to delete

        Returns:
            True if deleted successfully

        Raises:
            XUIClientNotFoundError: If client doesn't exist
            XUIError: On other API errors
        """
        self._ensure_connected()

        if not key.remote_key_id:
            raise XUIError("Key has no remote_key_id")

        inbound_id = self._get_inbound_id()
        email = key.remote_key_id

        try:
            # Find client UUID from inbound list (get_by_email returns numeric id,
            # but api.client.delete needs the UUID string)
            client_uuid = self._find_client_uuid_by_email(inbound_id, email)
            if client_uuid is None:
                raise XUIClientNotFoundError(f"Client not found: {email}")

            self.api.client.delete(inbound_id, client_uuid)
            logger.info(f"Deleted client {email} from server {self.server.name}")
            return True

        except XUIClientNotFoundError:
            raise
        except Exception as e:
            raise XUIError(f"Failed to delete client: {e}", e)

    def update_key_expiry(self, key: Key, new_expiry_ms: int) -> bool:
        """Update expiry time for an existing key.

        Args:
            key: Key model to update
            new_expiry_ms: New expiry time in milliseconds since epoch

        Returns:
            True if updated successfully

        Raises:
            XUIClientNotFoundError: If client doesn't exist
            XUIError: On other API errors
        """
        self._ensure_connected()

        if not key.remote_key_id:
            raise XUIError("Key has no remote_key_id")

        inbound_id = self._get_inbound_id()
        email = key.remote_key_id

        try:
            # Get inbound to access all clients
            inbound = self.api.inbound.get_by_id(inbound_id)

            # Find the client
            target_client = None
            for client in inbound.settings.clients:
                if client.email == email:
                    target_client = client
                    break

            if not target_client:
                raise XUIClientNotFoundError(f"Client not found: {email}")

            # Update client's expiry time
            target_client.expiry_time = new_expiry_ms

            # Set inbound_id (required for update API)
            target_client.inbound_id = inbound_id

            # Use client.update to update the client (positional args)
            self.api.client.update(target_client.id, target_client)

            logger.info(f"Updated expiry for client {email} on server {self.server.name} to {new_expiry_ms}")
            return True

        except XUIClientNotFoundError:
            raise
        except Exception as e:
            raise XUIError(f"Failed to update client expiry: {e}", e)

    def get_traffic(self, key: Key) -> TrafficStats:
        """Get traffic statistics for a key.

        Args:
            key: Key model to get traffic for

        Returns:
            TrafficStats with current usage

        Raises:
            XUIClientNotFoundError: If client doesn't exist
            XUIError: On other API errors
        """
        self._ensure_connected()

        if not key.remote_key_id:
            raise XUIError("Key has no remote_key_id")

        email = key.remote_key_id

        try:
            client = self._find_client_by_email(email)
            if client is None:
                raise XUIClientNotFoundError(f"Client not found: {email}")

            # Parse expiry time
            expiry_time = None
            if hasattr(client, "expiry_time") and client.expiry_time:
                expiry_ms = client.expiry_time
                if expiry_ms > 0:
                    expiry_time = datetime.fromtimestamp(expiry_ms / 1000)

            return TrafficStats(
                email=email,
                upload_bytes=getattr(client, "up", 0) or 0,
                download_bytes=getattr(client, "down", 0) or 0,
                total_bytes=(getattr(client, "up", 0) or 0) + (getattr(client, "down", 0) or 0),
                enabled=getattr(client, "enable", True),
                expiry_time=expiry_time,
            )

        except XUIClientNotFoundError:
            raise
        except Exception as e:
            raise XUIError(f"Failed to get traffic: {e}", e)

    def list_clients(self) -> list[ClientInfo]:
        """List all clients on the configured inbound.

        Returns:
            List of ClientInfo objects

        Raises:
            XUIError: On API errors
        """
        self._ensure_connected()

        inbound_id = self._get_inbound_id()

        try:
            inbounds = self.api.inbound.get_list()
            inbound = next((i for i in inbounds if i.id == inbound_id), None)

            if inbound is None:
                raise XUIInboundError(f"Inbound {inbound_id} not found")

            clients = []
            for client in inbound.settings.clients:
                expiry_time = None
                if hasattr(client, "expiry_time") and client.expiry_time:
                    if client.expiry_time > 0:
                        expiry_time = datetime.fromtimestamp(client.expiry_time / 1000)

                clients.append(
                    ClientInfo(
                        uuid=client.id,
                        email=client.email,
                        enabled=getattr(client, "enable", True),
                        inbound_id=inbound_id,
                        upload_bytes=getattr(client, "up", 0) or 0,
                        download_bytes=getattr(client, "down", 0) or 0,
                        total_bytes=(getattr(client, "up", 0) or 0) + (getattr(client, "down", 0) or 0),
                        expiry_time=expiry_time,
                        flow=getattr(client, "flow", None),
                        limit_ip=getattr(client, "limit_ip", 0) or 0,
                        total_gb=getattr(client, "total_gb", 0) or 0,
                    )
                )

            return clients

        except XUIInboundError:
            raise
        except Exception as e:
            raise XUIError(f"Failed to list clients: {e}", e)

    def health_check(self) -> ServerHealth:
        """Check server connectivity and health.

        Returns:
            ServerHealth with status information
        """
        try:
            self._connect()  # Force fresh connection

            # Try to get server status
            try:
                status = self.api.server.get_status()
                version = getattr(status, "xray_version", None)
                uptime = getattr(status, "uptime", None)
            except Exception:
                version = None
                uptime = None

            return ServerHealth(
                is_healthy=True,
                version=version,
                uptime=uptime,
            )

        except XUIAuthError as e:
            return ServerHealth(
                is_healthy=False,
                error_message=f"Authentication failed: {e.message}",
            )
        except XUIConnectionError as e:
            return ServerHealth(
                is_healthy=False,
                error_message=f"Connection failed: {e.message}",
            )
        except Exception as e:
            return ServerHealth(
                is_healthy=False,
                error_message=f"Unknown error: {e}",
            )

    def enable_key(self, key: Key) -> bool:
        """Enable a disabled key on the server.

        Args:
            key: Key model to enable

        Returns:
            True if enabled successfully
        """
        return self._set_key_enabled(key, True)

    def disable_key(self, key: Key) -> bool:
        """Disable a key on the server (without deleting).

        Args:
            key: Key model to disable

        Returns:
            True if disabled successfully
        """
        return self._set_key_enabled(key, False)

    def _set_key_enabled(self, key: Key, enabled: bool) -> bool:
        """Set the enabled status of a key."""
        self._ensure_connected()

        if not key.remote_key_id:
            raise XUIError("Key has no remote_key_id")

        inbound_id = self._get_inbound_id()
        email = key.remote_key_id

        try:
            # Get client from inbound list (more reliable)
            inbound = self.api.inbound.get_by_id(inbound_id)
            client = None
            for c in inbound.settings.clients:
                if c.email == email:
                    client = c
                    break

            if client is None:
                raise XUIClientNotFoundError(f"Client not found: {email}")

            # Ensure inbound_id is set (required for update)
            client.inbound_id = inbound_id

            client.enable = enabled
            self.api.client.update(client.id, client)
            status = "enabled" if enabled else "disabled"
            logger.info(f"Client {email} {status} on server {self.server.name}")
            return True

        except XUIClientNotFoundError:
            raise
        except Exception as e:
            raise XUIError(f"Failed to update client: {e}", e)

    def _find_client_uuid_by_email(self, inbound_id: int, email: str) -> Optional[str]:
        """Find a client's UUID by email from the inbound client list.

        get_by_email returns a numeric id, but delete/update need the UUID.
        This method gets the UUID from the inbound's client list directly.
        """
        try:
            inbounds = self.api.inbound.get_list()
            inbound = next((i for i in inbounds if i.id == inbound_id), None)
            if inbound is None:
                return None
            for c in inbound.settings.clients:
                if c.email == email:
                    return c.id  # UUID string
            return None
        except Exception:
            return None

    def _find_client_by_email(self, email: str) -> Optional[Client]:
        """Find a client by email address.

        Args:
            email: Client email to search for

        Returns:
            Client object if found, None otherwise
        """
        try:
            client = self.api.client.get_by_email(email)
            return client
        except Exception:
            return None
