"""User management service: business logic for admin actions on users.

Extracted from bot/handlers/admin.py so both /manage_user and support-topic
inline buttons share the same logic.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from config.settings import format_msk
from database.activity_log import log_activity
from database.models import (
    ClavisAccount,
    Device,
    Key,
    Server,
    Subscription,
    SupportChat,
    Transaction,
    User,
)
from services import KeyService

logger = logging.getLogger(__name__)


# ── Resolution helpers ────────────────────────────────────────


def resolve_topic_to_account_id(db: Session, topic_id: int) -> Optional[str]:
    """SupportChat.topic_id → account_id."""
    chat = db.query(SupportChat).filter(SupportChat.topic_id == topic_id).first()
    if chat is None:
        return None
    return chat.account_id


def resolve_account_to_telegram_id(db: Session, account_id: str) -> Optional[int]:
    """ClavisAccount → User.account_id → telegram_id."""
    user = db.query(User).filter(User.account_id == account_id).first()
    if user is None:
        return None
    return user.telegram_id


# ── Info formatting ───────────────────────────────────────────


def format_user_info(db: Session, telegram_id: int) -> tuple[str, Optional[User]]:
    """Build user info text.  Returns (text, user_or_None).

    Identical logic to the original admin.py _format_user_info.
    """
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return f"User with Telegram ID `{telegram_id}` not found.", None

    lines = [
        "*User Management*\n",
        f"*Telegram ID:* `{user.telegram_id}`",
        f"*Username:* `@{user.username}`" if user.username else "*Username:* —",
        f"*Registered:* {format_msk(user.created_at)}",
    ]

    sub = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.is_active == True,
        Subscription.expires_at > datetime.utcnow(),
    ).first()

    if sub:
        days_left = (sub.expires_at - datetime.utcnow()).days
        if sub.is_test:
            sub_type = "Test"
        elif sub.plan_type == 'unlimited':
            sub_type = "Безлимит"
        elif sub.plan_type == 'free':
            sub_type = "Free"
        else:
            sub_type = "Стандарт"
        active_keys = db.query(Key).filter(
            Key.subscription_id == sub.id,
            Key.is_active == True,
        ).count()
        key_servers = db.query(Key.server_id).filter(
            Key.subscription_id == sub.id,
            Key.is_active == True,
        ).distinct().all()
        server_names = []
        for (sid,) in key_servers:
            srv = db.query(Server).filter(Server.id == sid).first()
            if srv:
                server_names.append(srv.name)

        lines.append(f"\n*Subscription (id={sub.id}):*")
        lines.append(f"  Type: {sub_type}")
        lines.append(f"  Token: `{sub.token[:8]}...`")
        lines.append(f"  Expires: {format_msk(sub.expires_at)}")
        lines.append(f"  Days left: {days_left}")
        lines.append(f"  Keys: {active_keys} on {', '.join(server_names) if server_names else '—'}")
    else:
        any_sub = db.query(Subscription).filter(
            Subscription.user_id == user.id,
        ).order_by(Subscription.created_at.desc()).first()
        if any_sub:
            lines.append(f"\n*Subscription (id={any_sub.id}):*")
            lines.append(f"  Type: {'Test' if any_sub.is_test else 'Paid'}")
            lines.append(f"  Status: EXPIRED ({format_msk(any_sub.expires_at)})")
        else:
            lines.append("\n*Subscription:* None")

    has_test = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.is_test == True,
    ).first() is not None
    lines.append(f"*Test used:* {'Yes' if has_test else 'No'}")

    try:
        from services.traffic_limit_service import get_user_whitelist_traffic
        wl = get_user_whitelist_traffic(db, user.id)
        if wl:
            wl_status = "EXCEEDED" if wl["is_exceeded"] else f"{wl['remaining_gb']} GB left"
            lines.append(f"*Whitelist:* {wl['used_gb']} / {wl['limit_gb']} GB ({wl_status})")
    except Exception:
        pass

    tx = db.query(Transaction).filter(
        Transaction.user_id == user.id,
    ).order_by(Transaction.created_at.desc()).first()
    if tx:
        lines.append(f"\n*Last transaction (id={tx.id}):*")
        lines.append(f"  Plan: `{tx.plan}` | Amount: {tx.amount_rub}₽")
        lines.append(f"  Status: `{tx.status}`")
        lines.append(f"  Date: {format_msk(tx.created_at)}")

    return "\n".join(lines), user


def format_account_info(db: Session, account_id: str) -> tuple[str, Optional[int]]:
    """Build info text for an account that may or may not have a linked User.

    Returns (text, telegram_id_or_None).
    """
    user = db.query(User).filter(User.account_id == account_id).first()
    if user:
        text, _ = format_user_info(db, user.telegram_id)
        return text, user.telegram_id

    account = db.query(ClavisAccount).filter(ClavisAccount.id == account_id).first()
    if not account:
        return f"Account `{account_id[:8]}` not found.", None

    lines = [
        "*Support: User Management*\n",
        f"*Account:* `{account_id[:8]}`",
        "*Telegram:* — (app-only account)",
    ]

    sub = db.query(Subscription).filter(
        Subscription.account_id == account_id,
        Subscription.is_active == True,
        Subscription.expires_at > datetime.utcnow(),
    ).first()

    if sub:
        days_left = (sub.expires_at - datetime.utcnow()).days
        if sub.is_test:
            sub_type = "Test"
        elif sub.plan_type == 'unlimited':
            sub_type = "Безлимит"
        elif sub.plan_type == 'free':
            sub_type = "Free"
        else:
            sub_type = "Стандарт"
        active_keys = db.query(Key).filter(
            Key.subscription_id == sub.id,
            Key.is_active == True,
        ).count()
        key_servers = db.query(Key.server_id).filter(
            Key.subscription_id == sub.id,
            Key.is_active == True,
        ).distinct().all()
        server_names = []
        for (sid,) in key_servers:
            srv = db.query(Server).filter(Server.id == sid).first()
            if srv:
                server_names.append(srv.name)
        lines.append(f"\n*Subscription:* {sub_type}, expires {format_msk(sub.expires_at)} ({days_left}d)")
        lines.append(f"  Keys: {active_keys} on {', '.join(server_names) if server_names else '—'}")
    else:
        any_sub = db.query(Subscription).filter(
            Subscription.account_id == account_id,
        ).order_by(Subscription.created_at.desc()).first()
        if any_sub:
            lines.append(f"\n*Subscription:* EXPIRED ({format_msk(any_sub.expires_at)})")
        else:
            lines.append("\n*Subscription:* None")

    devices = db.query(Device).filter(Device.account_id == account_id).all()
    if devices:
        dev_types = [d.device_type for d in devices]
        lines.append(f"*Devices:* {len(devices)} ({', '.join(dev_types)})")
    else:
        lines.append("*Devices:* 0")

    return "\n".join(lines), None


# ── Actions ───────────────────────────────────────────────────


def refresh_keys(db: Session, telegram_id: int) -> tuple[bool, str]:
    """Delete old keys, create new ones.  Returns (success, result_text)."""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return False, "User not found."

    sub = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.is_active == True,
        Subscription.expires_at > datetime.utcnow(),
    ).first()

    if not sub:
        return False, "No active subscription to refresh keys for."

    all_keys = db.query(Key).filter(Key.subscription_id == sub.id).all()
    for key in all_keys:
        if key.server_id:
            server = db.query(Server).filter(Server.id == key.server_id).first()
            if server:
                try:
                    client = KeyService._make_xui_client(db, server, key)
                    client.delete_key(key)
                except Exception as e:
                    logger.warning(f"Failed to delete key {key.remote_key_id} from server: {e}")
        key.is_active = False
    db.commit()

    keys = KeyService.ensure_keys_exist(db, sub, telegram_id)
    server_names_list = []
    for key in keys:
        if key.server_id:
            srv = db.query(Server).filter(Server.id == key.server_id).first()
            if srv and srv.name not in server_names_list:
                server_names_list.append(srv.name)
    server_name = ", ".join(server_names_list) if server_names_list else "unknown"

    return True, f"Keys refreshed. New server: {server_name}"


def rotate_subscription(db: Session, telegram_id: int, grace_hours: int = None) -> tuple[bool, str]:
    """Rotate a user's subscription link: kill the old link now, keep old keys alive for
    a short grace window, and issue a NEW link + NEW keys on the SAME servers.

    Steps:
      1. New subscription with a fresh token, same remaining expiry / plan / device limit,
         and new keys on the same servers+inbounds the user currently has.
      2. Old subscription: set its x-ui keys to expire in `grace_hours`, mark it inactive,
         rename it to the grace marker, and rotate its token so the old /sub/<token> link
         404s immediately. A background reaper deletes the old keys after the grace window.

    Returns (success, result_text).
    """
    from subscription.cache import invalidate_subscription_cache
    from config.settings import ROTATED_GRACE_SUB_NAME, ROTATE_GRACE_HOURS, SUBSCRIPTION_BASE_URL

    if grace_hours is None:
        grace_hours = ROTATE_GRACE_HOURS

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return False, "User not found."

    s_old = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.is_active == True,
        Subscription.expires_at > datetime.utcnow(),
    ).order_by(Subscription.expires_at.desc()).first()
    if not s_old:
        return False, "No active subscription to rotate."

    old_keys = db.query(Key).filter(
        Key.subscription_id == s_old.id,
        Key.server_id.isnot(None),
        Key.is_active == True,
    ).all()
    if not old_keys:
        return False, "Subscription has no managed keys to rotate."

    server_ids = list({k.server_id for k in old_keys})
    servers = db.query(Server).filter(Server.id.in_(server_ids)).all()
    if not servers:
        return False, "None of the current servers are available."

    # 1. New subscription (fresh token) with the same remaining expiry and plan.
    s_new = Subscription(
        user_id=user.id,
        name=s_old.name if s_old.name and s_old.name != ROTATED_GRACE_SUB_NAME else "Main",
        token=str(uuid.uuid4()),
        expires_at=s_old.expires_at,
        device_limit=s_old.device_limit,
        plan_type=s_old.plan_type,
        is_test=s_old.is_test,
        is_active=True,
    )
    db.add(s_new)
    db.flush()
    new_token = s_new.token

    # 2. New keys on the SAME servers (create_subscription_keys covers all active
    #    inbounds of each server + whitelist traffic limits; commits on success).
    try:
        KeyService.create_subscription_keys(db, s_new, telegram_id, servers=servers)
    except ValueError as e:
        db.rollback()
        return False, f"Failed to create new keys, rotation aborted: {e}"

    # 3. Grace-demote the old subscription: keys live `grace_hours`, link dies now.
    grace_expiry = datetime.utcnow() + timedelta(hours=grace_hours)
    grace_ms = int(grace_expiry.replace(tzinfo=timezone.utc).timestamp() * 1000)
    for key in old_keys:
        server = db.query(Server).filter(Server.id == key.server_id).first()
        if not server or not server.is_active:
            continue
        try:
            client = KeyService._make_xui_client(db, server, key)
            client.update_key_expiry(key, grace_ms)
        except Exception as e:
            logger.warning(f"rotate: failed to set grace expiry on key {key.remote_key_id}: {e}")

    old_token = s_old.token
    s_old.expires_at = grace_expiry
    s_old.is_active = False
    s_old.name = ROTATED_GRACE_SUB_NAME
    s_old.token = str(uuid.uuid4())  # invalidate the old /sub/<token> link immediately
    db.commit()

    invalidate_subscription_cache(old_token)
    invalidate_subscription_cache(new_token)
    log_activity(db, telegram_id, "admin_rotate_sub",
                 f"old_sub={s_old.id}, new_sub={s_new.id}, servers={len(servers)}")
    db.commit()

    base = SUBSCRIPTION_BASE_URL.rstrip('/')
    new_url = f"{base}/sub/{new_token}"
    server_names = ", ".join(sorted({s.name for s in servers}))
    return True, (
        f"🔄 Link rotated.\n"
        f"New link: `{new_url}`\n"
        f"New keys on: {server_names}\n"
        f"Old link disabled; old keys expire in {grace_hours}h."
    )


def adjust_time(db: Session, telegram_id: int, hours: int) -> tuple[bool, str]:
    """Adjust subscription expiry.  Returns (success, result_text)."""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return False, "User not found."

    sub = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.is_active == True,
    ).order_by(Subscription.created_at.desc()).first()

    if not sub:
        return False, "No active subscription found."

    old_expiry = sub.expires_at
    sub.expires_at = sub.expires_at + timedelta(hours=hours)
    db.commit()

    sign = "+" if hours >= 0 else ""
    return True, (
        f"Subscription adjusted:\n"
        f"  {sign}{hours}h\n"
        f"  Old: {format_msk(old_expiry)}\n"
        f"  New: {format_msk(sub.expires_at)}"
    )


def grant_subscription(
    db: Session,
    telegram_id: int,
    days: int,
    force_replace: bool = False,
    plan_type: str = "basic",
) -> tuple[str, bool]:
    """Create or replace a paid subscription.

    Returns (result_text, needs_confirmation).
    If needs_confirmation is True, caller should show confirm/cancel buttons
    and call again with force_replace=True.
    """
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return "User not found.", False

    expires_at = datetime.utcnow() + timedelta(days=days)
    expires_at = expires_at.replace(hour=23, minute=59, second=59)

    existing = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.is_active == True,
        Subscription.expires_at > datetime.utcnow(),
    ).first()

    if existing and not force_replace:
        if existing.is_test:
            sub_type = "тестовая"
        elif existing.plan_type == 'unlimited':
            sub_type = "Безлимит"
        else:
            sub_type = "Стандарт"
        return (
            f"⚠️ Уже есть активная {sub_type} подписка "
            f"(id={existing.id}, до {format_msk(existing.expires_at)}).\n\n"
            f"Заменить на {days}д (до {expires_at.strftime('%d.%m.%Y')})?\n"
            f"Старые ключи проработают ещё 24ч."
        ), True

    _do_grant(db, telegram_id, user, expires_at, existing if force_replace else None, plan_type=plan_type)
    return f"Subscription granted until {format_msk(expires_at)}", False


def _do_grant(
    db: Session,
    telegram_id: int,
    user: User,
    expires_at: datetime,
    existing_sub: Optional[Subscription],
    plan_type: str = "basic",
) -> None:
    """Create new or replace existing subscription (internal)."""
    from subscription.cache import invalidate_subscription_cache

    from services.traffic_limit_service import set_user_limit
    from config.settings import WHITELIST_GROUP_NAME, WHITELIST_TRAFFIC_LIMIT_GB

    set_user_limit(
        db, user.id, WHITELIST_GROUP_NAME,
        0 if plan_type == "unlimited" else WHITELIST_TRAFFIC_LIMIT_GB,
    )

    if existing_sub is not None:
        existing_sub.expires_at = expires_at
        existing_sub.plan_type = plan_type
        existing_sub.is_test = False
        existing_sub.is_active = True
        existing_sub.reminder_7d_sent = False
        existing_sub.reminder_3d_sent = False
        existing_sub.reminder_1d_sent = False
        existing_sub.expiry_notified = False
        db.flush()

        new_expiry_ms = int(expires_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        keys = db.query(Key).filter(
            Key.subscription_id == existing_sub.id,
            Key.server_id.isnot(None),
            Key.is_active == True,
        ).all()

        for key in keys:
            try:
                server = db.query(Server).filter(Server.id == key.server_id).first()
                if not server or not server.is_active:
                    continue
                client = KeyService._make_xui_client(db, server, key)
                if (server.server_set or "") == WHITELIST_GROUP_NAME:
                    from services.traffic_limit_service import get_user_limit_gb
                    limit_gb = get_user_limit_gb(db, user.id, WHITELIST_GROUP_NAME)
                    client.update_key_expiry_and_limit(key, new_expiry_ms, limit_gb * 1024 ** 3)
                else:
                    client.update_key_expiry(key, new_expiry_ms)
            except Exception as e:
                logger.warning(f"Failed to update key {key.remote_key_id}: {e}")

        invalidate_subscription_cache(existing_sub.token)
    else:
        sub = Subscription(
            user_id=user.id,
            name="Main",
            token=str(uuid.uuid4()),
            expires_at=expires_at,
            plan_type=plan_type,
            is_test=False,
            is_active=True,
        )
        db.add(sub)
        db.flush()

    plan_label = "Безлимит" if plan_type == "unlimited" else "Стандарт"
    log_activity(db, telegram_id, "admin_grant_sub", f"{plan_label}, до {format_msk(expires_at)}")
