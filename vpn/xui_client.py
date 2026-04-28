"""3x-ui API client wrapper using py3xui SDK."""

import json
import logging
import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Optional

from py3xui import Api, Client
from py3xui.inbound.sniffing import Sniffing

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

    def __init__(self, server: Server, server_inbound=None):
        """Initialize the XUI client.

        Args:
            server: Server model with api_url and api_credentials
            server_inbound: Optional ServerInbound for multi-inbound mode.
                            If provided, uses its inbound_id/port/pbk/sid + profile settings.
                            If None, falls back to legacy api_credentials.

        Raises:
            XUIError: If credentials are invalid or missing
        """
        self.server = server
        self._server_inbound = server_inbound
        self._api: Optional[Api] = None
        self._credentials = self._parse_credentials()

        if server_inbound:
            profile = server_inbound.profile
            self._connection_settings = ConnectionSettings(
                port=server_inbound.port,
                sni=profile.sni,
                public_key=server_inbound.public_key,
                short_id=server_inbound.short_id,
                flow=profile.flow,
                fingerprint=profile.fingerprint,
                security=profile.security,
                network=profile.network,
            )
        else:
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

        required = ["username", "password"]
        if self._server_inbound is None:
            required.append("inbound_id")
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

    def _generate_email(self, client_id, subscription_id: int) -> str:
        """Generate unique email identifier for a client.

        Format with ServerInbound: clavis_{client_id}_{subscription_id}_si{server_inbound_id}
        Legacy format: clavis_{client_id}_{subscription_id}_s{server_id}
        """
        if self._server_inbound:
            return f"clavis_{client_id}_{subscription_id}_si{self._server_inbound.id}"
        return f"clavis_{client_id}_{subscription_id}_s{self.server.id}"

    def _get_inbound_id(self) -> int:
        """Get the configured inbound ID."""
        if self._server_inbound:
            return self._server_inbound.inbound_id
        return self._credentials["inbound_id"]

    def create_key(
        self,
        subscription: Subscription,
        client_id,
        remarks: Optional[str] = None,
        key_number: Optional[int] = None,
    ) -> Key:
        """Create a new VPN key on the server.

        Args:
            subscription: Subscription model the key belongs to
            client_id: Identifier for email generation (telegram_id or app_{account_id})
            remarks: Display name for the key (default: server name)
            key_number: If set, appends #{number} to display name (for multi-inbound)

        Returns:
            Key model with populated key_data (VLESS URI)

        Raises:
            XUIError: On any API error
        """
        self._ensure_connected()

        # Generate unique identifiers
        client_uuid = str(uuid_lib.uuid4())
        email = self._generate_email(client_id, subscription.id)
        display_name = remarks or self.server.name
        if key_number is not None:
            display_name = f"{display_name} #{key_number}"
        inbound_id = self._get_inbound_id()

        # Calculate expiry timestamp (milliseconds since epoch)
        expiry_ms = int(subscription.expires_at.replace(tzinfo=timezone.utc).timestamp() * 1000)

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
            server_inbound_id=self._server_inbound.id if self._server_inbound else None,
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
                    expiry_time = datetime.utcfromtimestamp(expiry_ms / 1000)

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

    def list_clients_multi(self, inbound_ids: list[int]) -> list[ClientInfo]:
        """List clients from multiple inbounds in a single API call.

        Makes ONE get_list() request and aggregates clients from all
        specified inbound IDs. Use this instead of calling list_clients()
        per-inbound to avoid redundant logins and API round-trips.
        """
        self._ensure_connected()

        try:
            all_inbounds = self.api.inbound.get_list()
        except Exception as e:
            raise XUIError(f"Failed to get inbound list: {e}", e)

        id_set = set(inbound_ids)
        clients = []
        for inbound in all_inbounds:
            if inbound.id not in id_set:
                continue
            stats_by_email = {}
            if inbound.client_stats:
                for cs in inbound.client_stats:
                    stats_by_email[cs.email] = cs
            for client in (inbound.settings.clients or []):
                expiry_time = None
                cs = stats_by_email.get(client.email)
                up = (cs.up if cs else 0) or 0
                down = (cs.down if cs else 0) or 0
                if cs and hasattr(cs, "expiry_time") and cs.expiry_time:
                    if cs.expiry_time > 0:
                        expiry_time = datetime.utcfromtimestamp(cs.expiry_time / 1000)
                elif hasattr(client, "expiry_time") and client.expiry_time:
                    if client.expiry_time > 0:
                        expiry_time = datetime.utcfromtimestamp(client.expiry_time / 1000)
                clients.append(ClientInfo(
                    uuid=client.id,
                    email=client.email,
                    enabled=getattr(client, "enable", True),
                    inbound_id=inbound.id,
                    upload_bytes=up,
                    download_bytes=down,
                    total_bytes=up + down,
                    expiry_time=expiry_time,
                    flow=getattr(client, "flow", None),
                    limit_ip=getattr(client, "limit_ip", 0) or 0,
                    total_gb=getattr(client, "total_gb", 0) or 0,
                ))
        return clients

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

            # Build traffic lookup from client_stats (has actual up/down)
            stats_by_email = {}
            if inbound.client_stats:
                for cs in inbound.client_stats:
                    stats_by_email[cs.email] = cs

            clients = []
            for client in inbound.settings.clients:
                expiry_time = None
                # Traffic stats from client_stats (not settings)
                cs = stats_by_email.get(client.email)
                up = (cs.up if cs else 0) or 0
                down = (cs.down if cs else 0) or 0

                if cs and hasattr(cs, "expiry_time") and cs.expiry_time:
                    if cs.expiry_time > 0:
                        expiry_time = datetime.utcfromtimestamp(cs.expiry_time / 1000)
                elif hasattr(client, "expiry_time") and client.expiry_time:
                    if client.expiry_time > 0:
                        expiry_time = datetime.utcfromtimestamp(client.expiry_time / 1000)

                clients.append(
                    ClientInfo(
                        uuid=client.id,
                        email=client.email,
                        enabled=getattr(client, "enable", True),
                        inbound_id=inbound_id,
                        upload_bytes=up,
                        download_bytes=down,
                        total_bytes=up + down,
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
            version = uptime = cpu_pct = mem_used_pct = disk_used_pct = xray_state = None
            try:
                status = self.api.server.get_status()
                version = getattr(status, "xray_version", None)
                uptime = getattr(status, "uptime", None)
                cpu_pct = getattr(status, "cpu", None)
                if hasattr(status, "mem") and getattr(status.mem, "total", 0) > 0:
                    mem_used_pct = status.mem.current / status.mem.total * 100
                if hasattr(status, "disk") and getattr(status.disk, "total", 0) > 0:
                    disk_used_pct = status.disk.current / status.disk.total * 100
                if hasattr(status, "xray"):
                    xray_state = getattr(status.xray, "state", None)
            except Exception:
                pass

            return ServerHealth(
                is_healthy=True,
                version=version,
                uptime=uptime,
                cpu_pct=cpu_pct,
                mem_used_pct=mem_used_pct,
                disk_used_pct=disk_used_pct,
                xray_state=xray_state,
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

    def get_inbound_traffic(self) -> int | None:
        """Total bytes (up + down) for the configured inbound.

        Returns None on error so the caller can skip the check.
        """
        try:
            self._ensure_connected()
            inbound = self.api.inbound.get_by_id(self._get_inbound_id())
            return (inbound.up or 0) + (inbound.down or 0)
        except Exception as e:
            logger.warning(f"Failed to get inbound traffic for {self.server.name}: {e}")
            return None

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

    def setup_domain_blocking(self, blocked_domains: list[str] | None = None) -> dict:
        """Add routing rules to block domains via blackhole outbound.

        Only enables sniffing on the bot-managed inbound, not all inbounds.

        Args:
            blocked_domains: List of domains to block. Defaults to
                             ["oneme.ru", "ok.ru", "max.ru"].

        Returns:
            dict with keys: routing_updated (bool), sniffing_updated (bool), errors (list)
        """
        import httpx

        if blocked_domains is None:
            blocked_domains = ["oneme.ru", "ok.ru", "max.ru"]

        self._ensure_connected()
        result = {"routing_updated": False, "sniffing_updated": False, "errors": []}

        # ── 1. Update xray routing config ──
        try:
            base = self.server.api_url.rstrip("/")
            use_tls = self._credentials.get("use_tls_verify", True)
            cookies = {self.api.cookie_name: self.api.session}

            # Read current xray config
            resp = httpx.post(
                f"{base}/panel/xray",
                cookies=cookies, verify=use_tls, timeout=15,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
            config = json.loads(data["obj"]["xraySetting"]) if isinstance(data["obj"], dict) else json.loads(data["obj"])

            # Ensure blackhole outbound exists
            outbounds = config.setdefault("outbounds", [])
            if not any(o.get("tag") == "blocked" for o in outbounds):
                outbounds.append({"protocol": "blackhole", "tag": "blocked", "settings": {}})

            # Add/merge domain rules
            routing = config.setdefault("routing", {})
            rules = routing.setdefault("rules", [])
            blocked_rule = next((r for r in rules if r.get("outboundTag") == "blocked"), None)

            domain_entries = [f"domain:{d}" for d in blocked_domains]
            if blocked_rule:
                existing = blocked_rule.setdefault("domain", [])
                for entry in domain_entries:
                    if entry not in existing:
                        existing.append(entry)
            else:
                rules.append({
                    "type": "field",
                    "domain": domain_entries,
                    "outboundTag": "blocked",
                })

            # Save config
            save_resp = httpx.post(
                f"{base}/panel/xray/update",
                cookies=cookies, verify=use_tls, timeout=15,
                data={"xraySetting": json.dumps(config)},
                follow_redirects=True,
            )
            save_resp.raise_for_status()
            save_data = save_resp.json()
            if save_data.get("success"):
                result["routing_updated"] = True
                logger.info(f"[{self.server.name}] Domain blocking routing rules updated")
            else:
                result["errors"].append(f"Routing save failed: {save_data}")
        except Exception as e:
            result["errors"].append(f"Routing update error: {e}")
            logger.error(f"[{self.server.name}] Routing update error: {e}")

        # ── 2. Enable sniffing on bot-managed inbound only ──
        try:
            inbound_id = self._get_inbound_id()
            inbounds = self.api.inbound.get_list()
            ib = next((i for i in inbounds if i.id == inbound_id), None)

            if ib is None:
                result["errors"].append(f"Inbound {inbound_id} not found")
            else:
                sniff = getattr(ib, "sniffing", None)
                needs_update = False
                if sniff is None or not getattr(sniff, "enabled", False):
                    needs_update = True
                else:
                    dest = getattr(sniff, "dest_override", []) or []
                    required = {"http", "tls", "quic", "fakedns"}
                    if not required.issubset(set(dest)):
                        needs_update = True

                if needs_update:
                    ib.sniffing = Sniffing(
                        enabled=True,
                        destOverride=["http", "tls", "quic", "fakedns"],
                    )
                    self.api.inbound.update(ib.id, ib)
                    result["sniffing_updated"] = True
                    logger.info(f"[{self.server.name}] Sniffing enabled on inbound {inbound_id}")
                else:
                    logger.info(f"[{self.server.name}] Sniffing already enabled on inbound {inbound_id}")
        except Exception as e:
            result["errors"].append(f"Sniffing update error: {e}")
            logger.error(f"[{self.server.name}] Sniffing update error: {e}")

        return result

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
