"""Main entry point for Clavis VPN Bot v2."""

import io
import json
import logging
import os
import sys
import threading
import types

from apscheduler.schedulers.background import BackgroundScheduler

from datetime import datetime, timedelta
from pathlib import Path

from database import init_db, get_db_session
from database.models import Transaction, User, Subscription, Server, Key
from database.connection import DEFAULT_DB_PATH
from bot import register_handlers, start_polling, get_bot
from services import NotificationService, KeyService

BACKUP_ADMIN_ID = 331186567
MONITOR_ADMIN_ID = 331186567

# Monitoring thresholds
MONITOR_STALE_TRAFFIC_MIN_KEYS = 15
MONITOR_STALE_TRAFFIC_COOLDOWN_HOURS = 1
MONITOR_PEAK_HOURS_MSK = (10, 22)
MONITOR_CPU_ALERT_PCT = 90.0
MONITOR_DISK_ALERT_PCT = 95.0

# Monitor state: server_id -> {is_up, last_seen_up, last_seen_down, last_traffic_bytes, stale_traffic_alerted_at, consecutive_failures}
_MONITOR_STATE_FILE = Path(__file__).parent / "data" / "monitor_state.json"
_monitor_state: dict = {}


def setup_logging():
    """Configure logging for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('clavis_vpn_bot.log', encoding='utf-8')
        ]
    )


def check_subscriptions_job():
    """Periodic job to check subscriptions and send renewal reminders."""
    logger = logging.getLogger(__name__)
    try:
        with get_db_session() as db:
            bot = get_bot()
            sent_counts = NotificationService.check_and_send_reminders(db, bot)
            if sum(sent_counts.values()) > 0:
                logger.info(f"Renewal check completed: {sent_counts}")
    except Exception as e:
        logger.error(f"Error in subscription check job: {e}", exc_info=True)


def expire_stale_transactions_job():
    """Mark pending transactions older than 10 minutes as failed."""
    logger = logging.getLogger(__name__)
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        with get_db_session() as db:
            count = db.query(Transaction).filter(
                Transaction.status == 'pending',
                Transaction.created_at < cutoff,
            ).update({Transaction.status: 'failed'})
            if count > 0:
                logger.info(f"Expired {count} stale pending transaction(s)")
    except Exception as e:
        logger.error(f"Error in expire_stale_transactions job: {e}", exc_info=True)


def deactivate_expired_subscriptions_job():
    """Disable keys and deactivate subscriptions that have expired."""
    from config.settings import BOOTSTRAP_SUB_NAME
    from sqlalchemy import or_
    logger = logging.getLogger(__name__)
    try:
        now = datetime.utcnow()
        with get_db_session() as db:
            # Bootstrap (free-VPN) subs are handled by reap_bootstrap_subscriptions_job,
            # which needs their keys still active so it can delete the x-ui clients.
            # NULL-safe: `name != x` excludes NULL-name rows in SQL, so OR in IS NULL
            # to keep deactivating legacy subs that have no name set.
            expired_subs = db.query(Subscription).filter(
                Subscription.is_active == True,
                Subscription.expires_at < now,
                or_(
                    Subscription.name.is_(None),
                    Subscription.name != BOOTSTRAP_SUB_NAME,
                ),
            ).all()

            if not expired_subs:
                return

            total_disabled = 0
            for sub in expired_subs:
                disabled = KeyService.disable_subscription_keys(db, sub)
                sub.is_active = False
                total_disabled += disabled
                logger.info(f"Deactivated sub {sub.id}, disabled {disabled} keys")

            db.commit()
            logger.info(f"Deactivated {len(expired_subs)} expired subscription(s), "
                       f"disabled {total_disabled} key(s) total")
    except Exception as e:
        logger.error(f"Error in deactivate_expired_subscriptions job: {e}", exc_info=True)


def reap_bootstrap_subscriptions_job():
    """Delete expired free-VPN bootstrap subs and their x-ui clients.

    The generic deactivate job only marks keys inactive (x-ui enforces expiry via
    expiry_time); for throwaway bootstrap keys we also delete the x-ui client so
    dead entries don't accumulate on the Free server.
    """
    from config.settings import BOOTSTRAP_SUB_NAME
    logger = logging.getLogger(__name__)
    try:
        now = datetime.utcnow()
        with get_db_session() as db:
            expired = db.query(Subscription).filter(
                Subscription.name == BOOTSTRAP_SUB_NAME,
                Subscription.expires_at < now,
            ).all()
            if not expired:
                return
            reaped = 0
            for sub in expired:
                try:
                    # Deletes the x-ui client(s) and marks keys inactive.
                    KeyService.delete_subscription_keys(db, sub)
                except Exception as e:
                    logger.warning(f"reap bootstrap: delete keys failed for sub {sub.id}: {e}")
                db.delete(sub)  # cascade removes the (now inactive) key rows
                reaped += 1
            db.commit()
            logger.info(f"Reaped {reaped} expired bootstrap subscription(s)")
    except Exception as e:
        logger.error(f"Error in reap_bootstrap_subscriptions job: {e}", exc_info=True)


def recalculate_server_scores_job():
    """Recalculate server throughput scores and update preferred servers."""
    logger = logging.getLogger(__name__)
    try:
        with get_db_session() as db:
            chosen = KeyService.recalculate_server_scores(db)
            logger.info(f"Server scores recalculated, chosen: {chosen}")
    except Exception as e:
        logger.error(f"Error in server_scores job: {e}", exc_info=True)


def send_db_backup(bot, chat_id: int):
    """Copy the database file and send it as a Telegram document."""
    db_path = os.environ.get("CLAVIS_DB_PATH", DEFAULT_DB_PATH)
    db_path = Path(db_path)

    if not db_path.exists():
        bot.send_message(chat_id, "Database file not found.")
        return

    # Read DB into memory
    data = db_path.read_bytes()
    size_kb = len(data) / 1024

    # Build caption with basic stats
    with get_db_session() as db:
        total_users = db.query(User).count()
        active_subs = db.query(Subscription).filter(
            Subscription.is_active == True,
            Subscription.expires_at > datetime.utcnow(),
        ).count()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO(data)
    buf.name = f"clavis_backup_{ts}.db"

    caption = f"DB backup | {size_kb:.0f} KB | {total_users} users | {active_subs} active subs"

    bot.send_document(chat_id, buf, caption=caption)


def backup_database_job():
    """Scheduled job: send database backup to admin."""
    logger = logging.getLogger(__name__)
    try:
        bot = get_bot()
        send_db_backup(bot, BACKUP_ADMIN_ID)
        logger.info("Database backup sent to admin")
    except Exception as e:
        logger.error(f"Error in database backup job: {e}", exc_info=True)


def fcm_expiry_notifications_job():
    """Send FCM push notifications for expiring/expired subscriptions."""
    logger = logging.getLogger(__name__)
    try:
        from services.fcm_service import send_expiry_notifications
        send_expiry_notifications()
    except Exception as e:
        logger.error(f"Error in FCM expiry notifications job: {e}", exc_info=True)


def cleanup_ephemeral_devices_job():
    """Delete Clavis-app devices that never got promoted to an account (>24h)."""
    logger = logging.getLogger(__name__)
    try:
        from database.models import Device
        cutoff = datetime.utcnow() - timedelta(hours=24)
        with get_db_session() as db:
            removed = (
                db.query(Device)
                .filter(Device.account_id.is_(None), Device.created_at < cutoff)
                .delete(synchronize_session=False)
            )
            if removed:
                logger.info(f"Cleaned up {removed} ephemeral devices (>24h)")
    except Exception as e:
        logger.error(f"Error cleaning ephemeral devices: {e}", exc_info=True)


def _load_monitor_state() -> dict:
    """Load monitoring state from JSON file."""
    try:
        if _MONITOR_STATE_FILE.exists():
            raw = json.loads(_MONITOR_STATE_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in raw.items()}
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not load monitor state: {e}")
    return {}


def _save_monitor_state(state: dict) -> None:
    """Save monitoring state to JSON file."""
    try:
        _MONITOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MONITOR_STATE_FILE.write_text(
            json.dumps({str(k): v for k, v in state.items()}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not save monitor state: {e}")


def _send_monitor_alert(bot, text: str, server_id: int = None) -> None:
    """Send an alert message to the monitoring admin.

    If server_id is given, attaches a 'Mute 6h' inline button.
    """
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    from html import escape as _html_escape
    try:
        markup = None
        if server_id is not None:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton(
                "Заглушить на 6 часов",
                callback_data=f"mute_srv_{server_id}",
            ))
        bot.send_message(MONITOR_ADMIN_ID, text, parse_mode='HTML', reply_markup=markup)
    except Exception as e:
        logging.getLogger(__name__).warning(f"HTML alert failed ({e}), retrying as plain text")
        try:
            # Strip HTML tags and send without parse_mode
            import re
            plain = re.sub(r'<[^>]+>', '', text)
            bot.send_message(MONITOR_ADMIN_ID, plain, parse_mode='', reply_markup=markup)
        except Exception as e2:
            logging.getLogger(__name__).error(f"Failed to send monitor alert: {e2}")


def mute_server_alerts(server_id: int, hours: int = 6) -> None:
    """Mute monitoring alerts for a server for the given number of hours."""
    global _monitor_state
    muted_until = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
    _monitor_state.setdefault(server_id, {})["muted_until"] = muted_until
    _save_monitor_state(_monitor_state)


def _fmt_uptime(seconds) -> str:
    """Format uptime seconds into a human-readable string."""
    if seconds is None:
        return "?"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h >= 24:
        d = h // 24
        h = h % 24
        return f"{d}d {h}h"
    return f"{h}h {m}m"


def monitor_servers_job():
    """Periodic server health monitoring with Telegram alerts."""
    global _monitor_state
    logger = logging.getLogger(__name__)

    from vpn.xui_client import XUIClient

    now_utc = datetime.utcnow()
    now_msk_hour = (now_utc + timedelta(hours=3)).hour

    try:
        # Collect server data inside session to avoid DetachedInstanceError
        server_data = []  # list of (Server, key_count) — Server objects stay bound
        with get_db_session() as db:
            servers = db.query(Server).filter(
                Server.is_active == True,
                Server.protocol == 'xui',
                Server.monitor_enabled == True,
            ).all()
            for srv in servers:
                kc = db.query(Key).filter(
                    Key.server_id == srv.id,
                    Key.is_active == True,
                ).count()
                # Preload first ServerInbound (for profile-based servers)
                from database.models import ServerInbound as _SI
                first_si = db.query(_SI).filter(
                    _SI.server_id == srv.id,
                    _SI.is_active == True,
                ).first()
                si_data = None
                if first_si and first_si.profile:
                    p = first_si.profile
                    si_data = {
                        "id": first_si.id,
                        "inbound_id": first_si.inbound_id,
                        "port": first_si.port,
                        "public_key": first_si.public_key,
                        "short_id": first_si.short_id,
                        "private_key": first_si.private_key,
                        "profile_sni": p.sni,
                        "profile_flow": p.flow,
                        "profile_fingerprint": p.fingerprint,
                        "profile_security": p.security,
                        "profile_network": p.network,
                    }
                # Force-load all attributes XUIClient will need
                server_data.append({
                    "id": srv.id,
                    "name": srv.name,
                    "host": srv.host,
                    "api_url": srv.api_url,
                    "api_credentials": srv.api_credentials,
                    "protocol": srv.protocol,
                    "is_active": srv.is_active,
                    "capacity": srv.capacity,
                    "server_set": srv.server_set,
                    "key_count": kc,
                    "first_inbound": si_data,
                })

        bot = get_bot()

        for sd in server_data:
            sid = sd["id"]
            sname = sd["name"]
            prev = _monitor_state.setdefault(sid, {
                "is_up": True,
                "last_seen_up": None,
                "last_seen_down": None,
                "last_traffic_bytes": None,
                "stale_traffic_alerted_at": None,
                "muted_until": None,
                "consecutive_failures": 0,
            })

            # Skip all alerts if server is muted
            muted_until_raw = prev.get("muted_until")
            if muted_until_raw:
                try:
                    if datetime.utcnow() < datetime.fromisoformat(muted_until_raw):
                        logger.debug(f"Monitor: {sname} is muted, skipping alerts")
                        continue
                    else:
                        prev["muted_until"] = None  # Mute expired
                except Exception:
                    prev["muted_until"] = None

            # Create a lightweight server-like object for XUIClient
            server_obj = types.SimpleNamespace(
                id=sd["id"], name=sd["name"], host=sd["host"],
                api_url=sd["api_url"], api_credentials=sd["api_credentials"],
                protocol=sd["protocol"], is_active=sd["is_active"],
                capacity=sd["capacity"], server_set=sd["server_set"],
            )
            si_obj = None
            if sd.get("first_inbound"):
                fsi = sd["first_inbound"]
                profile_obj = types.SimpleNamespace(
                    sni=fsi["profile_sni"], flow=fsi["profile_flow"],
                    fingerprint=fsi["profile_fingerprint"],
                    security=fsi["profile_security"], network=fsi["profile_network"],
                )
                si_obj = types.SimpleNamespace(
                    id=fsi["id"], inbound_id=fsi["inbound_id"],
                    port=fsi["port"], public_key=fsi["public_key"],
                    short_id=fsi["short_id"], private_key=fsi["private_key"],
                    profile=profile_obj,
                )

            try:
                client = XUIClient(server_obj, server_inbound=si_obj)
                health = client.health_check()
            except Exception as e:
                logger.error(f"Monitor: XUIClient error for {sname}: {e}")
                from vpn.xui_models import ServerHealth
                health = ServerHealth(is_healthy=False, error_message=str(e))

            was_up = prev.get("is_up", True)

            if health.is_healthy:
                if not was_up:
                    _send_monitor_alert(
                        bot,
                        f"<b>Server {sname} — ONLINE</b>\n"
                        f"Server has recovered.\n"
                        f"Version: {health.version or '?'} | "
                        f"Uptime: {_fmt_uptime(health.uptime)}",
                        server_id=sid,
                    )
                    logger.info(f"Monitor: {sname} is back UP")

                prev["is_up"] = True
                prev["last_seen_up"] = now_utc.isoformat()
                prev["consecutive_failures"] = 0

                # Resource alerts
                if health.cpu_pct is not None and health.cpu_pct > MONITOR_CPU_ALERT_PCT:
                    _send_monitor_alert(
                        bot,
                        f"<b>Server {sname} — High CPU</b>\n"
                        f"CPU: {health.cpu_pct:.1f}%",
                        server_id=sid,
                    )

                if health.disk_used_pct is not None and health.disk_used_pct > MONITOR_DISK_ALERT_PCT:
                    _send_monitor_alert(
                        bot,
                        f"<b>Server {sname} — Low disk space</b>\n"
                        f"Disk used: {health.disk_used_pct:.1f}%",
                        server_id=sid,
                    )

                if health.xray_state is not None and health.xray_state != "running":
                    _send_monitor_alert(
                        bot,
                        f"<b>Server {sname} — Xray not running</b>\n"
                        f"Xray state: <code>{health.xray_state}</code>",
                        server_id=sid,
                    )

                # Stale traffic check (peak hours MSK only)
                start_h, end_h = MONITOR_PEAK_HOURS_MSK
                if start_h <= now_msk_hour < end_h:
                    active_keys = sd["key_count"]
                    if active_keys >= MONITOR_STALE_TRAFFIC_MIN_KEYS:
                        traffic = client.get_inbound_traffic()
                        if traffic is not None:
                            last_traffic = prev.get("last_traffic_bytes")
                            if last_traffic is not None and traffic <= last_traffic:
                                last_alert_raw = prev.get("stale_traffic_alerted_at")
                                last_alert = (
                                    datetime.fromisoformat(last_alert_raw)
                                    if last_alert_raw else None
                                )
                                cooldown_ok = (
                                    last_alert is None
                                    or (now_utc - last_alert) >= timedelta(
                                        hours=MONITOR_STALE_TRAFFIC_COOLDOWN_HOURS
                                    )
                                )
                                if cooldown_ok:
                                    traffic_gb = traffic / (1024 ** 3)
                                    _send_monitor_alert(
                                        bot,
                                        f"<b>Server {sname} — No traffic growth</b>\n"
                                        f"Traffic unchanged at {traffic_gb:.1f} GB "
                                        f"during peak hours ({now_msk_hour}:xx MSK) "
                                        f"with {active_keys} active keys.\n"
                                        f"Check if server is blocked.",
                                        server_id=sid,
                                    )
                                    prev["stale_traffic_alerted_at"] = now_utc.isoformat()
                            prev["last_traffic_bytes"] = traffic

            else:
                prev["consecutive_failures"] = prev.get("consecutive_failures", 0) + 1

                if was_up:
                    prev["is_up"] = False
                    prev["last_seen_down"] = now_utc.isoformat()
                    from html import escape as _esc
                    _send_monitor_alert(
                        bot,
                        f"<b>Server {sname} — DOWN</b>\n"
                        f"Error: {_esc(str(health.error_message))}",
                        server_id=sid,
                    )
                    logger.warning(f"Monitor: {sname} went DOWN: {health.error_message}")
                else:
                    logger.warning(
                        f"Monitor: {sname} still DOWN "
                        f"(failure #{prev['consecutive_failures']})"
                    )

        _save_monitor_state(_monitor_state)

    except Exception as e:
        logger.error(f"Monitor job error: {e}", exc_info=True)


def resume_pending_verifications():
    """Resume YooKassa verification for pending transactions after bot restart."""
    logger = logging.getLogger(__name__)
    try:
        from bot.handlers.payment import verify_payment_via_yookassa

        cutoff = datetime.utcnow() - timedelta(minutes=10)
        bot = get_bot()

        with get_db_session() as db:
            pending = db.query(Transaction).join(User).filter(
                Transaction.status == 'pending',
                Transaction.created_at >= cutoff,
                Transaction.yookassa_payment_id.is_(None),
            ).all()

            for txn in pending:
                created_after = txn.created_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                logger.info(
                    f"Resuming verification for transaction {txn.id} "
                    f"(user {txn.user.telegram_id}, {txn.amount / 100:.0f} RUB, after={created_after})"
                )
                thread = threading.Thread(
                    target=verify_payment_via_yookassa,
                    args=(bot, txn.id, txn.user.telegram_id, txn.amount, created_after),
                    daemon=True,
                )
                thread.start()

            if pending:
                logger.info(f"Resumed {len(pending)} pending verification(s)")
    except Exception as e:
        logger.error(f"Error resuming pending verifications: {e}", exc_info=True)


def main():
    """Main function to start the bot."""
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("=" * 50)
    logger.info("Clavis VPN Bot v2 starting...")
    logger.info("=" * 50)

    try:
        # Initialize database
        logger.info("Initializing database...")
        init_db()

        # Register all handlers
        register_handlers()

        # Resume any pending payment verifications killed by prior restart
        resume_pending_verifications()

        # Load monitoring state from disk
        global _monitor_state
        _monitor_state = _load_monitor_state()

        # Start scheduler for renewal reminders
        logger.info("Starting renewal reminder scheduler...")
        scheduler = BackgroundScheduler()
        # Run every hour
        scheduler.add_job(check_subscriptions_job, 'interval', hours=1, id='subscription_check')
        scheduler.add_job(deactivate_expired_subscriptions_job, 'interval', hours=1, id='deactivate_expired')
        scheduler.add_job(
            reap_bootstrap_subscriptions_job, 'interval', minutes=15,
            id='reap_bootstrap', misfire_grace_time=300,
        )
        scheduler.add_job(expire_stale_transactions_job, 'interval', minutes=5, id='expire_stale_transactions')
        scheduler.add_job(
            recalculate_server_scores_job, 'interval', hours=12,
            id='server_scores', misfire_grace_time=300,
        )
        scheduler.add_job(
            backup_database_job, 'cron',
            hour=0, minute=0,  # 00:00 UTC = 03:00 MSK
            id='database_backup', misfire_grace_time=3600,
        )
        scheduler.add_job(
            monitor_servers_job, 'interval', minutes=30,
            id='monitor_servers', misfire_grace_time=60,
        )
        scheduler.add_job(
            cleanup_ephemeral_devices_job, 'interval', hours=6,
            id='ephemeral_devices_cleanup', misfire_grace_time=3600,
        )
        scheduler.add_job(
            fcm_expiry_notifications_job, 'cron',
            hour=7, minute=0,  # 07:00 UTC = 10:00 MSK
            id='fcm_expiry_notifications', misfire_grace_time=3600,
        )
        # Run once on startup
        recalculate_server_scores_job()
        scheduler.start()
        logger.info("Scheduler started (checking subscriptions every hour)")

        # Start polling (blocks main thread)
        start_polling()

    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C)")
        sys.exit(0)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
