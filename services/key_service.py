"""Key management and traffic aggregation service."""

import json
import logging
import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set

from sqlalchemy.orm import Session

from config.settings import USER_SERVER_LIMIT
from database.models import Key, Server, ServerInbound, Subscription
from vpn.xui_client import XUIClient

logger = logging.getLogger(__name__)

_SCORES_FILE = Path(__file__).parent.parent / "data" / "server_scores.json"


def _client_id_for_sub(sub: Subscription):
    """Return client identifier for x-ui key creation.

    Telegram users → telegram_id (int), app-only users → 'app_{account_id}'.
    """
    if sub.user_id is not None and sub.user is not None:
        return sub.user.telegram_id
    return f"app_{sub.account_id}"


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
    def get_preferred_server_ids() -> Set[int]:
        """Load preferred server IDs from scores file.

        Returns empty set if file is missing or older than 48 hours.
        """
        try:
            if _SCORES_FILE.exists():
                data = json.loads(_SCORES_FILE.read_text())
                updated = datetime.fromisoformat(data["updated_at"])
                if (datetime.utcnow() - updated).total_seconds() < 48 * 3600:
                    return set(data.get("chosen_ids", []))
        except Exception:
            pass
        return set()

    @staticmethod
    def recalculate_server_scores(db: Session) -> Set[int]:
        """Compute throughput index for every active server and pick the
        least-loaded 25 % per group (min 1).

        Index = (smart_monthly + stupid_monthly) / 2
        - smart_monthly  = sum(key_traffic / max(age_days, 1) * 30)
        - stupid_monthly = avg_traffic_per_key_global * keys_on_server * 2

        Saves chosen server IDs to data/server_scores.json.
        """
        now = datetime.utcnow()
        servers = db.query(Server).filter(
            Server.protocol == "xui",
            Server.is_active == True,
        ).all()

        if not servers:
            return set()

        # ── Collect per-server data from x-ui panels ──
        # server_id -> {db_keys, smart_monthly, traffic_total}
        server_data: Dict[int, dict] = {}
        global_traffic = 0
        global_keys = 0

        for server in servers:
            try:
                clients = KeyService.list_all_clients_for_server(db, server)
            except Exception as e:
                logger.warning(f"server_scores: can't reach {server.name}: {e}")
                continue

            traffic_by_email = {
                c.email: c.upload_bytes + c.download_bytes
                for c in clients
            }

            # Only keys with non-expired subscriptions for load index
            db_keys_active = db.query(Key).join(
                Subscription, Key.subscription_id == Subscription.id
            ).filter(
                Key.server_id == server.id,
                Key.is_active == True,
                Subscription.expires_at > now,
            ).all()

            srv_traffic = sum(traffic_by_email.values())
            smart = 0
            for k in db_keys_active:
                t = traffic_by_email.get(k.remote_key_id, 0)
                if t <= 0:
                    continue
                age_days = max(
                    (now - k.created_at).total_seconds() / 86400, 1.0
                ) if k.created_at else 1.0
                smart += t / age_days * 30

            server_data[server.id] = {
                "key_count": len(db_keys_active),
                "smart": smart,
                "traffic": srv_traffic,
            }
            global_traffic += srv_traffic
            global_keys += len(db_keys_active)

        if not server_data:
            return set()

        avg_traffic_per_key = global_traffic / global_keys if global_keys > 0 else 0

        # ── Compute index ──
        indices: Dict[int, float] = {}
        for sid, d in server_data.items():
            stupid = avg_traffic_per_key * d["key_count"] * 2
            indices[sid] = (d["smart"] + stupid) / 2

        # ── Pick top 20 % per group (min 1) ──
        groups: Dict[str, list] = defaultdict(list)  # group -> [(server_id, index)]
        server_by_id = {s.id: s for s in servers}
        for sid, idx in indices.items():
            srv = server_by_id.get(sid)
            if srv and srv.has_capacity:
                groups[srv.server_set or "default"].append((sid, idx))

        chosen: Set[int] = set()
        for group_name, items in groups.items():
            items.sort(key=lambda x: x[1])  # ascending by index
            count = max(1, math.ceil(len(items) * 0.20))
            for sid, _ in items[:count]:
                chosen.add(sid)

        # ── Persist ──
        try:
            _SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SCORES_FILE.write_text(json.dumps({
                "updated_at": now.isoformat(),
                "chosen_ids": list(chosen),
            }))
        except Exception as e:
            logger.error(f"server_scores: can't save file: {e}")

        logger.info(f"server_scores: chosen {chosen} from {len(indices)} servers")
        return chosen

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

        # Filter servers by plan type
        from config.settings import PREMIUM_GROUPS, FREE_GROUP_NAME
        plan_type = getattr(subscription, 'plan_type', 'basic') or 'basic'

        if plan_type == 'free':
            # Free (referral) plan: only servers in the Free group, 1 server max
            all_servers = [
                s for s in all_servers
                if (s.server_set or "default") == FREE_GROUP_NAME
            ]
            if limit is None:
                limit = 1
        elif plan_type != 'premium':
            all_servers = [
                s for s in all_servers
                if (s.server_set or "default") not in PREMIUM_GROUPS
                and (s.server_set or "default") != FREE_GROUP_NAME
            ]

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

        # Prefer servers chosen by throughput scoring
        preferred = KeyService.get_preferred_server_ids()

        # Pick 1 from each group (up to limit), preferring low-index servers
        selected: List[Server] = []
        for group_name, servers_in_set in sets.items():
            if len(selected) >= limit:
                break
            if not servers_in_set:
                continue
            # Split into preferred and rest
            pref = [s for s in servers_in_set if s.id in preferred]
            rest = [s for s in servers_in_set if s.id not in preferred]
            pool = pref if pref else rest
            selected.append(random.choice(pool))

        return selected

    @staticmethod
    def create_subscription_keys(
        db: Session,
        subscription: Subscription,
        client_id,
        servers: List[Server] = None,
    ) -> List[Key]:
        """
        Create VPN keys on selected servers for a subscription.

        For each server, creates one key per active ServerInbound.
        Falls back to legacy single-inbound mode if no ServerInbound records exist.

        Args:
            db: Database session
            subscription: Subscription object
            client_id: Identifier for key email (telegram_id or app_{account_id})
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

        # Use a fixed display name for free (referral) subscriptions
        plan_type = getattr(subscription, 'plan_type', 'basic') or 'basic'
        free_remarks = "Clavis Free" if plan_type == 'free' else None

        for server in servers:
            inbounds = db.query(ServerInbound).filter(
                ServerInbound.server_id == server.id,
                ServerInbound.is_active == True,
            ).all()

            if not inbounds:
                # Legacy mode: single inbound from api_credentials
                try:
                    client = XUIClient(server)
                    key = client.create_key(subscription, client_id, remarks=free_remarks)
                    db.add(key)
                    created_keys.append(key)
                    logger.info(f"Created key on server {server.name} for subscription {subscription.id}")
                except Exception as e:
                    logger.warning(f"Failed to create key on server {server.name}: {e}")
                    errors.append(f"{server.name}: {e}")
            else:
                # Multi-inbound mode: one key per active ServerInbound
                for idx, si in enumerate(inbounds):
                    key_number = idx + 1 if len(inbounds) > 1 else None
                    try:
                        client = XUIClient(server, server_inbound=si)
                        key = client.create_key(
                            subscription, client_id,
                            remarks=free_remarks,
                            key_number=key_number,
                        )
                        db.add(key)
                        created_keys.append(key)
                        logger.info(
                            f"Created key on server {server.name} inbound {si.inbound_id} "
                            f"for subscription {subscription.id}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to create key on server {server.name} "
                            f"inbound {si.inbound_id}: {e}"
                        )
                        errors.append(f"{server.name}/inbound-{si.inbound_id}: {e}")

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
        client_id,
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
            client_id: Identifier for key email (telegram_id or app_{account_id})

        Returns:
            List of all active keys for this subscription
        """
        # select_servers_for_user already computes which groups are missing
        servers = KeyService.select_servers_for_user(db, subscription)
        if servers:
            try:
                KeyService.create_subscription_keys(
                    db, subscription, client_id, servers=servers,
                )
            except ValueError as e:
                logger.warning(f"Could not create all keys for sub {subscription.id}: {e}")

        # Return all active keys (managed + legacy)
        return db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.is_active == True,
        ).all()

    @staticmethod
    def _make_xui_client(db: Session, server: Server, key: Key) -> XUIClient:
        """Create XUIClient for a key, resolving ServerInbound if available."""
        si = None
        if key.server_inbound_id:
            si = db.query(ServerInbound).filter(
                ServerInbound.id == key.server_inbound_id
            ).first()
        return XUIClient(server, server_inbound=si)

    @staticmethod
    def list_all_clients_for_server(db: Session, server: Server) -> list:
        """List clients across all active inbounds for a server.

        For profile-based servers (with ServerInbound records), makes a single
        login and single get_list() call, then aggregates clients from all
        active inbound IDs. Falls back to legacy single-inbound mode.
        """
        inbounds = db.query(ServerInbound).filter(
            ServerInbound.server_id == server.id,
            ServerInbound.is_active == True,
        ).all()

        if inbounds:
            # One connection, one get_list() for all inbound IDs
            client = XUIClient(server, server_inbound=inbounds[0])
            return client.list_clients_multi([si.inbound_id for si in inbounds])
        else:
            return XUIClient(server).list_clients()

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

        # Group keys by server to reuse connections where possible
        keys_by_server: Dict[int, list] = {}
        for key in keys:
            keys_by_server.setdefault(key.server_id, []).append(key)

        for server_id, server_keys in keys_by_server.items():
            server = db.query(Server).filter(Server.id == server_id).first()
            if not server or not server.is_active:
                continue

            try:
                for key in server_keys:
                    try:
                        client = KeyService._make_xui_client(db, server, key)
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
                for key in server_keys:
                    key.is_active = False
                continue

            for key in server_keys:
                try:
                    client = KeyService._make_xui_client(db, server, key)
                    client.delete_key(key)
                except Exception:
                    pass
                key.is_active = False

        db.commit()

    @staticmethod
    def disable_subscription_keys(db: Session, subscription: Subscription) -> int:
        """
        Mark all active keys for an expired subscription as inactive in DB.
        x-ui panels handle actual blocking via expiry_time on the key.

        Returns:
            Number of keys marked inactive.
        """
        count = db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.is_active == True
        ).update({Key.is_active: False})
        return count

        return disabled

    @staticmethod
    def has_server_keys(db: Session, subscription: Subscription) -> bool:
        """Check if subscription has any active keys linked to a server."""
        return db.query(Key).filter(
            Key.subscription_id == subscription.id,
            Key.server_id.isnot(None),
            Key.is_active == True,
        ).first() is not None

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
    def create_keys_for_new_inbound(
        db: Session,
        server_inbound: ServerInbound,
    ) -> Dict[str, int]:
        """
        Create keys on a new inbound for all existing active subscriptions
        that already have keys on this server.

        Called when a profile is assigned to a server with existing users.

        Args:
            db: Database session
            server_inbound: The newly created ServerInbound

        Returns:
            Dict with created, skipped, failed counts
        """
        server = db.query(Server).filter(Server.id == server_inbound.server_id).first()
        if not server:
            return {"created": 0, "skipped": 0, "failed": 0}

        stats = {"created": 0, "skipped": 0, "failed": 0}

        # Find all active subscriptions that have keys on this server
        sub_ids = set(
            sid for (sid,) in db.query(Key.subscription_id).filter(
                Key.server_id == server.id,
                Key.is_active == True,
            ).distinct().all()
        )

        for sub_id in sub_ids:
            sub = db.query(Subscription).filter(
                Subscription.id == sub_id,
                Subscription.is_active == True,
                Subscription.expires_at > datetime.utcnow(),
                Subscription.plan_type != 'free',
            ).first()
            if not sub:
                stats["skipped"] += 1
                continue

            # Skip if this subscription already has a key on this inbound
            already = db.query(Key).filter(
                Key.subscription_id == sub.id,
                Key.server_inbound_id == server_inbound.id,
                Key.is_active == True,
            ).first()
            if already:
                stats["skipped"] += 1
                continue

            # Determine key number
            inbound_count = db.query(ServerInbound).filter(
                ServerInbound.server_id == server.id,
                ServerInbound.is_active == True,
            ).count()
            key_number = inbound_count if inbound_count > 1 else None

            try:
                client = XUIClient(server, server_inbound=server_inbound)
                key = client.create_key(
                    sub, _client_id_for_sub(sub),
                    key_number=key_number,
                )
                db.add(key)
                db.commit()
                stats["created"] += 1
            except Exception as e:
                logger.warning(f"create_keys_for_new_inbound: failed for sub {sub.id}: {e}")
                stats["failed"] += 1

        return stats

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
            Subscription.plan_type != 'free',
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

            # Get inbounds for this server
            inbounds = db.query(ServerInbound).filter(
                ServerInbound.server_id == server.id,
                ServerInbound.is_active == True,
            ).all()

            cid = _client_id_for_sub(sub)
            if not inbounds:
                # Legacy mode
                try:
                    client = XUIClient(server)
                    key = client.create_key(sub, cid)
                    db.add(key)
                    db.commit()
                    db.refresh(key)
                    stats["created"] += 1
                    logger.info(
                        f"activate_group: created key on {server.name} "
                        f"for sub {sub.id} (client {cid})"
                    )
                except Exception as e:
                    logger.warning(
                        f"activate_group: failed to create key on {server.name} "
                        f"for sub {sub.id}: {e}"
                    )
                    stats["failed"] += 1
            else:
                # Multi-inbound
                created_any = False
                for idx, si in enumerate(inbounds):
                    key_number = idx + 1 if len(inbounds) > 1 else None
                    try:
                        client = XUIClient(server, server_inbound=si)
                        key = client.create_key(
                            sub, cid,
                            key_number=key_number,
                        )
                        db.add(key)
                        created_any = True
                    except Exception as e:
                        logger.warning(
                            f"activate_group: failed on {server.name}/inbound-{si.inbound_id} "
                            f"for sub {sub.id}: {e}"
                        )
                if created_any:
                    db.commit()
                    stats["created"] += 1
                else:
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

        new_expiry_ms = int(subscription.expires_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
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

            for key in server_keys:
                try:
                    client = KeyService._make_xui_client(db, server, key)
                    client.update_key_expiry(key, new_expiry_ms)
                    updated_count += 1
                    logger.info(f"Updated expiry for key {key.remote_key_id} on server {server.name}")
                except Exception as e:
                    logger.warning(f"Failed to update expiry for key {key.remote_key_id}: {e}")
                    errors.append(f"{server.name}/{key.remote_key_id}: {e}")

        if updated_count == 0:
            raise ValueError(f"Failed to update any keys: {'; '.join(errors)}")

        logger.info(f"Updated expiry for {updated_count}/{len(keys)} keys in subscription {subscription.id}")
        return updated_count
