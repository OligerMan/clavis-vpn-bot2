"""Admin command handlers for Telegram bot."""

import csv
import io
import json
import logging
import random
import secrets
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from py3xui import Api, Inbound
from py3xui.inbound import Settings, Sniffing, StreamSettings
from telebot import TeleBot
from telebot.types import Message, CallbackQuery, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton

from sqlalchemy import func

from database import get_db_session
from database.models import Server, User, Subscription, Key, Transaction, ActivityLog
from database.activity_log import log_activity
from config.settings import ADMIN_IDS, XUI_USERNAME, XUI_PASSWORD, PLANS, format_msk
from services import KeyService

logger = logging.getLogger(__name__)

DEFAULT_XUI_PANEL_PORT = 2053
DEFAULT_XUI_BASE_PATH = "/dashboard/"

# Temporary storage for dialog state per chat_id
_add_server_state = {}
_manage_user_state = {}  # {chat_id: {"step": ..., "telegram_id": ..., ...}}


def is_admin(telegram_id: int) -> bool:
    """Check if user is an admin."""
    return telegram_id in ADMIN_IDS


def _discover_inbounds(domain: str, base_path: str = DEFAULT_XUI_BASE_PATH) -> dict:
    """Connect to x-ui panel and discover VLESS Reality inbounds.

    Returns dict with api, inbounds list, and api_url.
    """
    api_url = f"https://{domain}:{DEFAULT_XUI_PANEL_PORT}{base_path}"
    api = Api(api_url, username=XUI_USERNAME, password=XUI_PASSWORD, use_tls_verify=True)
    api.login()
    inbounds = api.inbound.get_list()
    return {"api": api, "api_url": api_url, "inbounds": inbounds}


def _extract_inbound_config(inbound) -> dict:
    """Extract connection settings from a py3xui inbound object."""
    ss = inbound.stream_settings
    reality = getattr(ss, 'reality_settings', None) or {}
    settings_inner = reality.get('settings', {})

    # Get flow from first client if available
    flow = "xtls-rprx-vision"
    if hasattr(inbound.settings, 'clients') and inbound.settings.clients:
        client_flow = getattr(inbound.settings.clients[0], 'flow', None)
        if client_flow:
            flow = client_flow

    server_names = reality.get('serverNames', [])
    sni = server_names[0] if server_names else ""
    short_ids = reality.get('shortIds', [])
    sid = short_ids[0] if short_ids else ""

    return {
        "inbound_id": inbound.id,
        "port": inbound.port,
        "protocol": inbound.protocol,
        "sni": sni,
        "pbk": settings_inner.get('publicKey', ''),
        "sid": sid,
        "flow": flow,
        "fingerprint": settings_inner.get('fingerprint', 'chrome'),
        "security": getattr(ss, 'security', 'reality'),
        "remark": getattr(inbound, 'remark', ''),
        "clients_count": len(inbound.settings.clients) if hasattr(inbound.settings, 'clients') else 0,
    }


def _format_inbound_info(cfg: dict) -> str:
    """Format inbound config for display."""
    return (
        f"  port: `{cfg['port']}` | protocol: `{cfg['protocol']}`\n"
        f"  security: `{cfg['security']}` | sni: `{cfg['sni']}`\n"
        f"  flow: `{cfg['flow']}` | fp: `{cfg['fingerprint']}`\n"
        f"  clients: {cfg['clients_count']}"
    )


def _generate_x25519_keys() -> tuple[str, str]:
    """Generate x25519 key pair using xray binary.

    Returns:
        (private_key, public_key)
    """
    try:
        result = subprocess.run(
            ["/usr/local/x-ui/bin/xray-linux-amd64", "x25519"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        private_key = lines[0].split(": ", 1)[1].strip()
        public_key = lines[1].split(": ", 1)[1].strip()  # "Password" in newer xray = public key
        return private_key, public_key
    except Exception as e:
        raise RuntimeError(f"Failed to generate x25519 keys: {e}")


def _generate_short_ids() -> list[str]:
    """Generate a set of random short IDs for Reality."""
    return [
        secrets.token_hex(5),   # 10 hex chars
        secrets.token_hex(2),   # 4 hex chars
        secrets.token_hex(8),   # 16 hex chars
        secrets.token_hex(3),   # 6 hex chars
    ]


def _create_vless_reality_inbound(api: Api, remark: str = "clavis") -> dict:
    """Create a VLESS Reality inbound on the panel.

    Returns:
        dict with inbound config (same format as _extract_inbound_config)
    """
    private_key, public_key = _generate_x25519_keys()
    short_ids = _generate_short_ids()
    port = random.randint(20000, 60000)

    reality_settings = {
        "show": False,
        "xver": 0,
        "target": "yahoo.com:443",
        "serverNames": ["yahoo.com", "www.yahoo.com"],
        "privateKey": private_key,
        "minClientVer": "",
        "maxClientVer": "",
        "maxTimediff": 0,
        "shortIds": short_ids,
        "settings": {
            "publicKey": public_key,
            "fingerprint": "chrome",
            "serverName": "",
            "spiderX": "/",
        }
    }

    tcp_settings = {
        "acceptProxyProtocol": False,
        "header": {"type": "none"},
    }

    stream_settings = StreamSettings(
        security="reality",
        network="tcp",
        tcp_settings=tcp_settings,
        reality_settings=reality_settings,
    )

    sniffing = Sniffing(enabled=True)
    settings = Settings(decryption="none")

    inbound = Inbound(
        enable=True,
        port=port,
        protocol="vless",
        settings=settings,
        stream_settings=stream_settings,
        sniffing=sniffing,
        remark=remark,
    )

    api.inbound.add(inbound)

    # Re-fetch to get the assigned ID
    inbounds = api.inbound.get_list()
    created = None
    for ib in inbounds:
        if ib.port == port and ib.protocol == "vless":
            created = ib
            break

    if not created:
        raise RuntimeError("Inbound was created but could not be found")

    return {
        "inbound_id": created.id,
        "port": port,
        "protocol": "vless",
        "sni": "yahoo.com",
        "pbk": public_key,
        "sid": short_ids[0],
        "flow": "xtls-rprx-vision",
        "fingerprint": "chrome",
        "security": "reality",
        "remark": remark,
        "clients_count": 0,
    }


def register_admin_handlers(bot: TeleBot) -> None:
    """Register all admin command handlers."""

    # ── /admin_help ───────────────────────────────────────────
    @bot.message_handler(commands=['admin_help'])
    def handle_admin_help(message: Message):
        """Show all admin commands."""
        if not is_admin(message.from_user.id):
            return

        bot.send_message(
            message.chat.id,
            "*Admin Commands*\n\n"
            "*Server management:*\n"
            "`/servers` — list all servers grouped by server set\n"
            "`/groups` — quick overview of server groups\n"
            "`/add_server` — add server (dialog: name → group → domain → auto-setup)\n"
            "`/activate_group` — bulk-create keys for a group for all active subs\n"
            "`/check_server <id>` — health check (version, uptime, clients)\n"
            "`/toggle_server <id>` — enable/disable server\n"
            "`/delete_server <id>` — delete server (force delete if keys exist)\n"
            "\n*User management:*\n"
            "`/manage_user <tg_id>` — user info, keys, subscription, actions\n"
            "\n*Legacy keys:*\n"
            "`/add_old_keys` — import legacy keys from CSV\n"
            "`/remove_old_keys` — soft-delete all legacy keys\n"
            "\n*Other:*\n"
            "`/report` — service dashboard (users, subs, payments, servers)\n"
            "`/analytics` — conversion, ARPU, revenue by plan\n"
            "`/logs` — last N user actions (default 50)\n"
            "`/last_logs` — only new actions since last call\n"
            "`/broadcast` — interactive broadcast to a list of users\n"
            "`/check_reminders` — manually run subscription expiry check\n"
            "`/admin_help` — this message",
            parse_mode='Markdown'
        )

    # ── /report ───────────────────────────────────────────────
    @bot.message_handler(commands=['report'])
    def handle_report(message: Message):
        """Show service dashboard: users, subscriptions, payments, servers."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                now = datetime.utcnow()
                week_ago = now - timedelta(days=7)
                month_ago = now - timedelta(days=30)

                # Users
                total_users = db.query(func.count(User.id)).scalar()
                new_7d = db.query(func.count(User.id)).filter(User.created_at >= week_ago).scalar()
                new_30d = db.query(func.count(User.id)).filter(User.created_at >= month_ago).scalar()

                # Subscriptions
                active_paid = db.query(func.count(Subscription.id)).filter(
                    Subscription.is_active == True,
                    Subscription.expires_at > now,
                    Subscription.is_test == False,
                ).scalar()
                active_test = db.query(func.count(Subscription.id)).filter(
                    Subscription.is_active == True,
                    Subscription.expires_at > now,
                    Subscription.is_test == True,
                ).scalar()
                expired = db.query(func.count(Subscription.id)).filter(
                    Subscription.is_active == True,
                    Subscription.expires_at <= now,
                ).scalar()

                # Payments — exclude admin transactions
                admin_user_ids = db.query(User.id).filter(
                    User.telegram_id.in_(ADMIN_IDS)
                ).subquery()
                # Exclude admin transactions and test payments (before 20.02.2026)
                real_payments_cutoff = datetime(2026, 2, 20)
                non_admin_tx = db.query(Transaction).filter(
                    ~Transaction.user_id.in_(admin_user_ids),
                    Transaction.created_at >= real_payments_cutoff,
                ).subquery()

                completed = db.query(
                    func.count(non_admin_tx.c.id),
                    func.coalesce(func.sum(non_admin_tx.c.amount), 0),
                ).filter(non_admin_tx.c.status == 'completed').one()
                completed_count, completed_sum_kopeks = completed

                pending_count = db.query(func.count(non_admin_tx.c.id)).filter(
                    non_admin_tx.c.status == 'pending'
                ).scalar()
                failed_count = db.query(func.count(non_admin_tx.c.id)).filter(
                    non_admin_tx.c.status == 'failed'
                ).scalar()

                # Recent revenue (non-admin)
                rev_7d = db.query(
                    func.count(non_admin_tx.c.id),
                    func.coalesce(func.sum(non_admin_tx.c.amount), 0),
                ).filter(
                    non_admin_tx.c.status == 'completed',
                    non_admin_tx.c.completed_at >= week_ago,
                ).one()
                rev_7d_count, rev_7d_sum = rev_7d

                rev_30d = db.query(
                    func.count(non_admin_tx.c.id),
                    func.coalesce(func.sum(non_admin_tx.c.amount), 0),
                ).filter(
                    non_admin_tx.c.status == 'completed',
                    non_admin_tx.c.completed_at >= month_ago,
                ).one()
                rev_30d_count, rev_30d_sum = rev_30d

                # Servers
                total_servers = db.query(func.count(Server.id)).scalar()
                active_servers = db.query(func.count(Server.id)).filter(
                    Server.is_active == True
                ).scalar()
                total_keys = db.query(func.count(Key.id)).filter(
                    Key.is_active == True,
                    Key.server_id.isnot(None),
                ).scalar()
                total_capacity = db.query(
                    func.coalesce(func.sum(Server.capacity), 0)
                ).filter(Server.is_active == True).scalar()

            def fmt_rub(kopeks: int) -> str:
                """Format kopeks as rubles with thousands separator."""
                rub = kopeks // 100
                return f"{rub:,}".replace(",", " ")

            text = (
                "*Отчёт по сервису*\n\n"
                "*Пользователи*\n"
                f"  Всего: {total_users}\n"
                f"  Новых за 7 дней: {new_7d}\n"
                f"  Новых за 30 дней: {new_30d}\n\n"
                "*Подписки*\n"
                f"  Активных платных: {active_paid}\n"
                f"  Активных тестовых: {active_test}\n"
                f"  Истекших (всего): {expired}\n\n"
                "*Платежи*\n"
                f"  Успешных: {completed_count} на {fmt_rub(completed_sum_kopeks)}₽\n"
                f"  Ожидающих: {pending_count}\n"
                f"  Неудачных: {failed_count}\n"
                f"  За 7 дней: {rev_7d_count} на {fmt_rub(rev_7d_sum)}₽\n"
                f"  За 30 дней: {rev_30d_count} на {fmt_rub(rev_30d_sum)}₽\n\n"
                "*Серверы*\n"
                f"  Активных: {active_servers} из {total_servers}\n"
                f"  Ключей: {total_keys} / {total_capacity}\n\n"
                f"_{format_msk(now)} МСК_"
            )

            bot.send_message(message.chat.id, text, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error in /report: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    def _truncate_lines(lines: list[str], max_len: int) -> str:
        """Join lines, truncating at line boundaries to stay under max_len."""
        result = []
        total = 0
        for line in lines:
            if total + len(line) + 1 > max_len:  # +1 for \n
                result.append("...")
                break
            result.append(line)
            total += len(line) + 1
        return "\n".join(result)

    # ── /logs ────────────────────────────────────────────────
    ACTION_DISPLAY = {
        "test_key": "Тест-ключ",
        "payment": "Оплата",
        "new_user": "Новый пользователь",
        "sub_extended": "Продление",
        "sub_reactivated": "Реактивация",
        "admin_grant_sub": "Выдана подписка",
    }

    @bot.message_handler(commands=['logs'])
    def handle_logs(message: Message):
        """Show last N user actions. Usage: /logs [N]"""
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split()
        limit = 50
        if len(parts) >= 2:
            try:
                limit = max(1, min(200, int(parts[1])))
            except ValueError:
                pass

        try:
            with get_db_session() as db:
                logs = (
                    db.query(ActivityLog)
                    .order_by(ActivityLog.created_at.desc())
                    .limit(limit)
                    .all()
                )

                if not logs:
                    bot.send_message(message.chat.id, "Нет записей.")
                    return

                lines = [f"*Последние действия ({len(logs)})*\n"]
                for entry in logs:
                    ts = format_msk(entry.created_at, fmt="%d.%m %H:%M").replace(" МСК", "")
                    action_name = ACTION_DISPLAY.get(entry.action, entry.action)
                    detail = f": `{entry.details}`" if entry.details else ""
                    lines.append(f"`{ts}` | `{entry.telegram_id}` | {action_name}{detail}")

                # Truncate by lines to avoid cutting mid-entity
                text = _truncate_lines(lines, 4000)
                bot.send_message(message.chat.id, text, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error in /logs: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /last_logs ────────────────────────────────────────────
    # Persist watermark to file so it survives bot restarts
    _LAST_LOGS_FILE = Path(__file__).parent.parent.parent / "data" / "last_logs_seen.json"

    def _load_watermarks() -> dict:
        try:
            if _LAST_LOGS_FILE.exists():
                import json as _json
                raw = _json.loads(_LAST_LOGS_FILE.read_text())
                return {int(k): datetime.fromisoformat(v) for k, v in raw.items()}
        except Exception:
            pass
        return {}

    def _save_watermarks(wm: dict):
        try:
            import json as _json
            _LAST_LOGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            raw = {str(k): v.isoformat() for k, v in wm.items()}
            _LAST_LOGS_FILE.write_text(_json.dumps(raw))
        except Exception:
            pass

    _last_logs_seen = _load_watermarks()

    @bot.message_handler(commands=['last_logs'])
    def handle_last_logs(message: Message):
        """Show new logs since last /last_logs call. First call = all logs."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                since = _last_logs_seen.get(message.chat.id)

                query = db.query(ActivityLog)
                if since:
                    query = query.filter(ActivityLog.created_at > since)
                logs = query.order_by(ActivityLog.created_at.desc()).limit(200).all()

                if not logs:
                    bot.send_message(message.chat.id, "Нет новых записей.")
                    return

                # Update and persist watermark
                _last_logs_seen[message.chat.id] = logs[0].created_at
                _save_watermarks(_last_logs_seen)

                lines = [f"*Новые действия ({len(logs)})*\n"]
                for entry in logs:
                    ts = format_msk(entry.created_at, fmt="%d.%m %H:%M").replace(" МСК", "")
                    action_name = ACTION_DISPLAY.get(entry.action, entry.action)
                    detail = f": `{entry.details}`" if entry.details else ""
                    lines.append(f"`{ts}` | `{entry.telegram_id}` | {action_name}{detail}")

                # Truncate by lines to avoid cutting mid-entity
                text = _truncate_lines(lines, 4000)
                bot.send_message(message.chat.id, text, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error in /last_logs: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /analytics ────────────────────────────────────────────
    @bot.message_handler(commands=['analytics'])
    def handle_analytics(message: Message):
        """Show conversion and business health metrics."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                now = datetime.utcnow()
                real_payments_cutoff = datetime(2026, 2, 20)

                # Exclude admins
                admin_user_ids = db.query(User.id).filter(
                    User.telegram_id.in_(ADMIN_IDS)
                ).subquery()

                # ── Funnel ──
                total_users = db.query(func.count(User.id)).filter(
                    ~User.id.in_(admin_user_ids)
                ).scalar()

                admin_tg_ids = [aid for aid in ADMIN_IDS]

                # All telegram_ids who ever had a test
                # Source 1: activity log (post-deployment)
                test_tg_from_log = set(
                    r[0] for r in db.query(ActivityLog.telegram_id).filter(
                        ActivityLog.action == 'test_key',
                        ~ActivityLog.telegram_id.in_(admin_tg_ids),
                    ).all()
                )
                # Source 2: current test subscriptions (covers pre-logging history)
                test_tg_from_sub = set(
                    r[0] for r in db.query(User.telegram_id).join(Subscription).filter(
                        Subscription.is_test == True,
                        ~User.id.in_(admin_user_ids),
                    ).all()
                )
                all_test_tg = test_tg_from_log | test_tg_from_sub
                test_users = len(all_test_tg)

                # All telegram_ids who paid
                paid_tg = set(
                    r[0] for r in db.query(User.telegram_id).join(
                        Transaction, User.id == Transaction.user_id
                    ).filter(
                        Transaction.status == 'completed',
                        ~User.id.in_(admin_user_ids),
                        Transaction.created_at >= real_payments_cutoff,
                    ).all()
                )
                paid_users = len(paid_tg)

                # Converted from test = had test AND paid
                converted_from_test = len(all_test_tg & paid_tg)

                # Decided = had test, but no longer in undecided active test
                active_test_tg = set(
                    r[0] for r in db.query(User.telegram_id).join(Subscription).filter(
                        Subscription.is_test == True,
                        Subscription.is_active == True,
                        Subscription.expires_at > now,
                        ~User.id.in_(admin_user_ids),
                    ).all()
                )
                undecided_tg = active_test_tg - paid_tg  # still in test and haven't paid
                test_decided = len(all_test_tg - undecided_tg)

                conv_test = (converted_from_test / test_decided * 100) if test_decided > 0 else 0
                conv_total = (paid_users / total_users * 100) if total_users > 0 else 0
                paid_without_test = len(paid_tg - all_test_tg)

                # ── Renewals (from activity log) ──
                renewal_users = db.query(func.count(func.distinct(ActivityLog.telegram_id))).filter(
                    ActivityLog.action == 'sub_extended',
                    ~ActivityLog.telegram_id.in_(
                        db.query(User.telegram_id).filter(User.telegram_id.in_(ADMIN_IDS))
                    ),
                ).scalar()
                renewal_pct = (renewal_users / paid_users * 100) if paid_users > 0 else 0

                # ── Revenue by plan ──
                plan_stats = db.query(
                    Transaction.plan,
                    func.count(Transaction.id),
                    func.coalesce(func.sum(Transaction.amount), 0),
                ).filter(
                    Transaction.status == 'completed',
                    ~Transaction.user_id.in_(admin_user_ids),
                    Transaction.created_at >= real_payments_cutoff,
                ).group_by(Transaction.plan).all()

                total_revenue = 0
                plan_lines = []
                for plan_key, count, amount in plan_stats:
                    plan_info = PLANS.get(plan_key, {})
                    desc = plan_info.get('description', plan_key)
                    price = plan_info.get('price_display', '?')
                    rub = amount // 100
                    total_revenue += amount
                    plan_lines.append(f"  {desc} ({price}): {count} шт — {rub:,}₽".replace(",", " "))

                total_rub = total_revenue // 100
                arpu = (total_rub // paid_users) if paid_users > 0 else 0

                # ── Expiring soon ──
                expiring_7d = db.query(func.count(Subscription.id)).filter(
                    Subscription.is_active == True,
                    Subscription.is_test == False,
                    Subscription.expires_at > now,
                    Subscription.expires_at <= now + timedelta(days=7),
                    ~Subscription.user_id.in_(admin_user_ids),
                ).scalar()

                expiring_30d = db.query(func.count(Subscription.id)).filter(
                    Subscription.is_active == True,
                    Subscription.is_test == False,
                    Subscription.expires_at > now,
                    Subscription.expires_at <= now + timedelta(days=30),
                    ~Subscription.user_id.in_(admin_user_ids),
                ).scalar()

                # ── Build message ──
                text = (
                    "*Аналитика*\n\n"
                    "*Воронка*\n"
                    f"  Всего пользователей: {total_users}\n"
                    f"  Получили тест-ключ: {test_users}\n"
                    f"  Оплатили (всего): {paid_users}\n"
                    f"  Оплатили после теста: {converted_from_test}\n"
                    f"  Оплатили без теста: {paid_without_test}\n"
                    f"  Конверсия тест → оплата: {conv_test:.1f}%"
                    f"  ({converted_from_test} из {test_decided} решивших)\n"
                    f"  Конверсия регистрация → оплата: {conv_total:.1f}%\n\n"
                    "*Продления*\n"
                    f"  Продлили подписку: {renewal_users}\n"
                    f"  Доля продлений: {renewal_pct:.1f}%\n\n"
                    "*Выручка по тарифам*\n"
                )
                if plan_lines:
                    text += "\n".join(plan_lines) + "\n"
                text += (
                    f"  Итого: {total_rub:,}₽\n\n".replace(",", " ") +
                    f"*ARPU:* {arpu:,}₽\n\n".replace(",", " ") +
                    "*Истекают*\n"
                    f"  В ближайшие 7 дней: {expiring_7d}\n"
                    f"  В ближайшие 30 дней: {expiring_30d}\n\n"
                    f"_{format_msk(now)}_"
                )

                bot.send_message(message.chat.id, text, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error in /analytics: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /servers ──────────────────────────────────────────────
    @bot.message_handler(commands=['servers'])
    def handle_servers(message: Message):
        """List all servers grouped by server_set."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                servers = db.query(Server).all()

                if not servers:
                    bot.send_message(message.chat.id, "No servers configured.")
                    return

                # Group by server_set
                from collections import defaultdict as _defaultdict
                groups: dict = _defaultdict(list)
                for s in servers:
                    groups[s.server_set or "default"].append(s)

                lines = ["*Servers:*\n"]
                for group_name in sorted(groups.keys()):
                    lines.append(f"*Group: {group_name}*")
                    for s in groups[group_name]:
                        status = "ON" if s.is_active else "OFF"
                        keys_count = len([k for k in s.keys if k.is_active])

                        creds_info = ""
                        if s.api_credentials:
                            try:
                                creds = json.loads(s.api_credentials)
                                inbound = creds.get("inbound_id", "?")
                                conn = creds.get("connection_settings", {})
                                port = conn.get("port", "?")
                                sni = conn.get("sni", "?")
                                creds_info = (
                                    f"  inbound: `{inbound}` | "
                                    f"port: `{port}` | sni: `{sni}`"
                                )
                            except json.JSONDecodeError:
                                creds_info = "  credentials: invalid JSON"

                        lines.append(
                            f"  *{s.id}.* `{s.name}` [{status}]\n"
                            f"  host: `{s.host}`\n"
                            f"{creds_info}\n"
                            f"  keys: {keys_count}/{s.capacity}"
                        )
                    lines.append("")  # blank line between groups

                bot.send_message(
                    message.chat.id,
                    "\n".join(lines),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in /servers: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /add_server (dialog) ─────────────────────────────────
    @bot.message_handler(commands=['add_server'])
    def handle_add_server(message: Message):
        """Step 1: Ask for server name."""
        if not is_admin(message.from_user.id):
            return

        _add_server_state[message.chat.id] = {"step": "name"}
        msg = bot.send_message(
            message.chat.id,
            "*Add Server — Step 1/4*\n\nEnter a short name for this server (e.g. `cl24`):",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True)
        )
        _add_server_state[message.chat.id]["prompt_id"] = msg.id

    @bot.message_handler(
        func=lambda m: (
            m.chat.id in _add_server_state
            and _add_server_state[m.chat.id].get("step") == "name"
            and m.reply_to_message is not None
        )
    )
    def handle_add_server_name(message: Message):
        """Step 2: Got name, ask for group."""
        if not is_admin(message.from_user.id):
            return

        name = message.text.strip()
        if not name or len(name) > 50:
            bot.send_message(message.chat.id, "Name must be 1-50 characters. Try again.")
            return

        state = _add_server_state[message.chat.id]
        state["name"] = name
        state["step"] = "group"

        # Get existing groups
        try:
            with get_db_session() as db:
                groups = db.query(Server.server_set).filter(
                    Server.is_active == True
                ).distinct().all()
                existing_groups = sorted(set(g[0] or "default" for g in groups))
        except Exception:
            existing_groups = []

        keyboard = InlineKeyboardMarkup()
        for group in existing_groups:
            keyboard.row(InlineKeyboardButton(group, callback_data=f"addsvr_group_{group}"))
        keyboard.row(InlineKeyboardButton("+ New group", callback_data="addsvr_group_new"))

        bot.send_message(
            message.chat.id,
            f"*Add Server — Step 2/4*\n\n"
            f"Name: `{name}`\n\n"
            f"Select a server group:",
            parse_mode='Markdown',
            reply_markup=keyboard,
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('addsvr_group_'))
    def handle_add_server_group_select(call: CallbackQuery):
        """Handle group selection for add_server."""
        if not is_admin(call.from_user.id):
            return

        state = _add_server_state.get(call.message.chat.id)
        if not state or state.get("step") != "group":
            bot.answer_callback_query(call.id, "Session expired. Run /add_server again.")
            return

        bot.answer_callback_query(call.id)
        group_value = call.data.replace('addsvr_group_', '', 1)

        if group_value == "new":
            state["step"] = "group_name"
            msg = bot.send_message(
                call.message.chat.id,
                "Enter a name for the new group (e.g. `Germany`, `Switzerland`):",
                parse_mode='Markdown',
                reply_markup=ForceReply(selective=True),
            )
            state["prompt_id"] = msg.id
        else:
            state["group"] = group_value
            state["step"] = "domain"
            msg = bot.send_message(
                call.message.chat.id,
                f"*Add Server — Step 3/4*\n\n"
                f"Name: `{state['name']}` | Group: `{group_value}`\n\n"
                f"Enter the domain where 3x-ui is running\n"
                f"(e.g. `cl24.clavisdashboard.ru`):",
                parse_mode='Markdown',
                reply_markup=ForceReply(selective=True),
            )
            state["prompt_id"] = msg.id

    @bot.message_handler(
        func=lambda m: (
            m.chat.id in _add_server_state
            and _add_server_state[m.chat.id].get("step") == "group_name"
            and m.reply_to_message is not None
        )
    )
    def handle_add_server_group_name(message: Message):
        """Got new group name, ask for domain."""
        if not is_admin(message.from_user.id):
            return

        group_name = message.text.strip()
        if not group_name or len(group_name) > 50:
            bot.send_message(message.chat.id, "Group name must be 1-50 characters. Try again.")
            return

        state = _add_server_state[message.chat.id]
        state["group"] = group_name
        state["step"] = "domain"

        msg = bot.send_message(
            message.chat.id,
            f"*Add Server — Step 3/4*\n\n"
            f"Name: `{state['name']}` | Group: `{group_name}`\n\n"
            f"Enter the domain where 3x-ui is running\n"
            f"(e.g. `cl24.clavisdashboard.ru`):",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True),
        )
        state["prompt_id"] = msg.id

    @bot.message_handler(
        func=lambda m: (
            m.chat.id in _add_server_state
            and _add_server_state[m.chat.id].get("step") == "domain"
            and m.reply_to_message is not None
        )
    )
    def handle_add_server_domain(message: Message):
        """Step 4: Got domain, connect to panel, discover inbounds, ask which one."""
        if not is_admin(message.from_user.id):
            return

        domain = message.text.strip().lower()
        state = _add_server_state[message.chat.id]
        state["domain"] = domain

        bot.send_message(message.chat.id, f"Connecting to `{domain}:{DEFAULT_XUI_PANEL_PORT}`...", parse_mode='Markdown')

        try:
            result = _discover_inbounds(domain)
        except Exception as e:
            logger.error(f"Failed to connect to {domain}: {e}", exc_info=True)
            bot.send_message(
                message.chat.id,
                f"Failed to connect to panel:\n`{e}`\n\nMake sure 3x-ui is running and credentials are correct.",
                parse_mode='Markdown'
            )
            _add_server_state.pop(message.chat.id, None)
            return

        state["api_url"] = result["api_url"]
        state["step"] = "no_inbound"  # Always create new inbound

        # Show existing inbounds as info (never reuse — protects old keys)
        vless_inbounds = []
        for ib in result["inbounds"]:
            if ib.protocol == "vless":
                ss = ib.stream_settings
                if getattr(ss, 'security', '') == 'reality':
                    vless_inbounds.append(ib)

        lines = ["*Add Server — Step 3/3*\n"]

        if vless_inbounds:
            lines.append(f"Found {len(vless_inbounds)} existing VLESS Reality inbound(s):")
            for ib in vless_inbounds:
                cfg = _extract_inbound_config(ib)
                lines.append(
                    f"  id={ib.id} port=`{cfg['port']}` sni=`{cfg['sni']}` "
                    f"({cfg['clients_count']} clients)"
                )
            lines.append("\n⚠️ Existing inbounds will NOT be reused (to protect old keys).")
        elif result["inbounds"]:
            lines.append("No VLESS Reality inbounds found.\nExisting inbounds:")
            for ib in result["inbounds"]:
                lines.append(f"  id={ib.id} protocol=`{ib.protocol}` port=`{ib.port}`")
        else:
            lines.append("No inbounds found on this panel.")

        lines.append("\nA *new* VLESS Reality inbound will be created.")

        keyboard = InlineKeyboardMarkup()
        keyboard.row(InlineKeyboardButton("Create new inbound", callback_data="create_inbound"))
        keyboard.row(InlineKeyboardButton("Cancel", callback_data="cancel_add_server"))

        bot.send_message(
            message.chat.id,
            "\n".join(lines),
            reply_markup=keyboard,
            parse_mode='Markdown'
        )

    @bot.callback_query_handler(func=lambda call: call.data == 'cancel_add_server')
    def handle_cancel_add_server(call: CallbackQuery):
        """Cancel add server flow."""
        _add_server_state.pop(call.message.chat.id, None)
        bot.answer_callback_query(call.id, "Cancelled")
        bot.edit_message_text("Server addition cancelled.", call.message.chat.id, call.message.id)

    @bot.callback_query_handler(func=lambda call: call.data == 'create_inbound')
    def handle_create_inbound(call: CallbackQuery):
        """Create a VLESS Reality inbound on the panel and save server."""
        if not is_admin(call.from_user.id):
            return

        state = _add_server_state.get(call.message.chat.id)
        if not state or state.get("step") != "no_inbound":
            bot.answer_callback_query(call.id, "Session expired. Run /add_server again.")
            return

        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "Creating VLESS Reality inbound...",
            call.message.chat.id,
            call.message.id
        )

        try:
            api_url = state["api_url"]
            api = Api(api_url, username=XUI_USERNAME, password=XUI_PASSWORD, use_tls_verify=True)
            api.login()

            cfg = _create_vless_reality_inbound(api, remark=state["name"])

            # Save server to DB
            credentials = {
                "username": XUI_USERNAME,
                "password": XUI_PASSWORD,
                "inbound_id": cfg["inbound_id"],
                "use_tls_verify": True,
                "connection_settings": {
                    "port": cfg["port"],
                    "sni": cfg["sni"],
                    "pbk": cfg["pbk"],
                    "sid": cfg["sid"],
                    "flow": cfg["flow"],
                    "fingerprint": cfg["fingerprint"],
                }
            }

            group = state.get("group", "default")
            with get_db_session() as db:
                server = Server(
                    name=state["name"],
                    host=state["domain"],
                    protocol="xui",
                    api_url=state["api_url"],
                    api_credentials=json.dumps(credentials),
                    capacity=100,
                    is_active=True,
                    server_set=group,
                )
                db.add(server)
                db.flush()
                server_id = server.id

            bot.send_message(
                call.message.chat.id,
                f"*Server added successfully!*\n\n"
                f"ID: `{server_id}`\n"
                f"Name: `{state['name']}`\n"
                f"Group: `{group}`\n"
                f"Domain: `{state['domain']}`\n"
                f"Inbound: `{cfg['inbound_id']}` (newly created)\n\n"
                f"*Connection settings:*\n"
                f"{_format_inbound_info(cfg)}",
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error creating inbound: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error creating inbound: `{e}`", parse_mode='Markdown')

        _add_server_state.pop(call.message.chat.id, None)

    # ── /groups ───────────────────────────────────────────────
    @bot.message_handler(commands=['groups'])
    def handle_groups(message: Message):
        """Quick overview of server groups."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                servers = db.query(Server).all()
                if not servers:
                    bot.send_message(message.chat.id, "No servers configured.")
                    return

                from collections import defaultdict as _defaultdict
                groups: dict = _defaultdict(lambda: {"servers": 0, "active": 0, "keys": 0})
                for s in servers:
                    g = groups[s.server_set or "default"]
                    g["servers"] += 1
                    if s.is_active:
                        g["active"] += 1
                    g["keys"] += len([k for k in s.keys if k.is_active])

                lines = ["*Server Groups:*\n"]
                for name in sorted(groups.keys()):
                    g = groups[name]
                    lines.append(
                        f"*{name}*: {g['active']}/{g['servers']} servers active, "
                        f"{g['keys']} keys"
                    )

                bot.send_message(message.chat.id, "\n".join(lines), parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error in /groups: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /activate_group ──────────────────────────────────────
    @bot.message_handler(commands=['activate_group'])
    def handle_activate_group(message: Message):
        """Bulk-create keys for a group for all active subscriptions."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                # Get groups that have active servers
                groups = db.query(Server.server_set).filter(
                    Server.is_active == True,
                    Server.protocol == 'xui',
                ).distinct().all()
                group_names = sorted(set(g[0] or "default" for g in groups))

            if not group_names:
                bot.send_message(message.chat.id, "No active server groups found.")
                return

            keyboard = InlineKeyboardMarkup()
            for name in group_names:
                keyboard.row(InlineKeyboardButton(name, callback_data=f"actgrp_select_{name}"))
            keyboard.row(InlineKeyboardButton("Cancel", callback_data="actgrp_cancel"))

            bot.send_message(
                message.chat.id,
                "*Activate Group*\n\nSelect a group to activate for all active subscriptions:",
                parse_mode='Markdown',
                reply_markup=keyboard,
            )

        except Exception as e:
            logger.error(f"Error in /activate_group: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('actgrp_select_'))
    def handle_activate_group_select(call: CallbackQuery):
        """Show confirmation before activating group."""
        if not is_admin(call.from_user.id):
            return

        group_name = call.data.replace('actgrp_select_', '', 1)
        bot.answer_callback_query(call.id)

        try:
            with get_db_session() as db:
                server_count = db.query(Server).filter(
                    Server.is_active == True,
                    Server.protocol == 'xui',
                    Server.server_set == group_name,
                ).count()

                # Count active subs with managed keys that DON'T have a key in this group
                from datetime import datetime as _dt
                active_subs = db.query(Subscription).filter(
                    Subscription.is_active == True,
                    Subscription.expires_at > _dt.utcnow(),
                ).all()

                need_keys = 0
                not_interacted = 0
                for sub in active_subs:
                    # Only count subs that have at least one managed key
                    has_managed = db.query(Key).filter(
                        Key.subscription_id == sub.id,
                        Key.server_id.isnot(None),
                        Key.is_active == True,
                    ).first()
                    if not has_managed:
                        not_interacted += 1
                        continue

                    has_key = db.query(Key).join(Server).filter(
                        Key.subscription_id == sub.id,
                        Key.is_active == True,
                        Key.server_id.isnot(None),
                        Server.server_set == group_name,
                    ).first()
                    if not has_key:
                        need_keys += 1

            keyboard = InlineKeyboardMarkup()
            keyboard.row(
                InlineKeyboardButton("Confirm", callback_data=f"actgrp_confirm_{group_name}"),
                InlineKeyboardButton("Cancel", callback_data="actgrp_cancel"),
            )

            bot.edit_message_text(
                f"*Activate Group: {group_name}*\n\n"
                f"Servers in group: {server_count}\n"
                f"Interacted users needing keys: {need_keys}\n"
                f"Not yet interacted (will get keys lazily): {not_interacted}\n\n"
                f"This will create 1 key per interacted user on a random server from this group.",
                call.message.chat.id,
                call.message.id,
                parse_mode='Markdown',
                reply_markup=keyboard,
            )

        except Exception as e:
            logger.error(f"Error in activate_group select: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('actgrp_confirm_'))
    def handle_activate_group_confirm(call: CallbackQuery):
        """Execute group activation."""
        if not is_admin(call.from_user.id):
            return

        group_name = call.data.replace('actgrp_confirm_', '', 1)
        bot.answer_callback_query(call.id)

        bot.edit_message_text(
            f"Activating group `{group_name}`... Please wait.",
            call.message.chat.id,
            call.message.id,
            parse_mode='Markdown',
        )

        try:
            with get_db_session() as db:
                stats = KeyService.activate_group_for_all(db, group_name)

            bot.send_message(
                call.message.chat.id,
                f"*Group `{group_name}` activated!*\n\n"
                f"Created: {stats['created']} keys\n"
                f"Skipped (already had key): {stats['skipped']}\n"
                f"Skipped (not interacted yet): {stats['skipped_no_keys']}\n"
                f"Failed: {stats['failed']}",
                parse_mode='Markdown',
            )

        except Exception as e:
            logger.error(f"Error activating group: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data == 'actgrp_cancel')
    def handle_activate_group_cancel(call: CallbackQuery):
        """Cancel group activation."""
        bot.answer_callback_query(call.id, "Cancelled")
        bot.edit_message_text("Group activation cancelled.", call.message.chat.id, call.message.id)

    # ── /toggle_server ───────────────────────────────────────
    @bot.message_handler(commands=['toggle_server'])
    def handle_toggle_server(message: Message):
        """Toggle server active/inactive. Usage: /toggle_server <id>"""
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "Usage: `/toggle_server <id>`", parse_mode='Markdown')
            return

        try:
            server_id = int(parts[1])
        except ValueError:
            bot.send_message(message.chat.id, "Invalid server ID")
            return

        try:
            with get_db_session() as db:
                server = db.query(Server).filter(Server.id == server_id).first()
                if not server:
                    bot.send_message(message.chat.id, f"Server {server_id} not found")
                    return

                server.is_active = not server.is_active
                status = "ON" if server.is_active else "OFF"

            bot.send_message(
                message.chat.id,
                f"Server `{server.name}` (id={server_id}) is now *{status}*",
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error toggling server: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /check_server ────────────────────────────────────────
    @bot.message_handler(commands=['check_server'])
    def handle_check_server(message: Message):
        """Health check a server. Usage: /check_server <id>"""
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "Usage: `/check_server <id>`", parse_mode='Markdown')
            return

        try:
            server_id = int(parts[1])
        except ValueError:
            bot.send_message(message.chat.id, "Invalid server ID")
            return

        try:
            with get_db_session() as db:
                server = db.query(Server).filter(Server.id == server_id).first()
                if not server:
                    bot.send_message(message.chat.id, f"Server {server_id} not found")
                    return

                from vpn.xui_client import XUIClient
                client = XUIClient(server)
                health = client.health_check()

                if health.is_healthy:
                    lines = [
                        f"*Server `{server.name}` — OK*\n",
                        f"Version: `{health.version or 'unknown'}`",
                    ]
                    if health.uptime_hours is not None:
                        lines.append(f"Uptime: `{health.uptime_hours:.1f}h`")

                    try:
                        clients = client.list_clients()
                        active = sum(1 for c in clients if c.enabled)
                        lines.append(f"Clients: {active}/{len(clients)}")
                    except Exception:
                        pass

                    bot.send_message(message.chat.id, "\n".join(lines), parse_mode='Markdown')
                else:
                    bot.send_message(
                        message.chat.id,
                        f"*Server `{server.name}` — FAIL*\n{health.error_message}",
                        parse_mode='Markdown'
                    )

        except Exception as e:
            logger.error(f"Error checking server: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /delete_server ───────────────────────────────────────
    @bot.message_handler(commands=['delete_server'])
    def handle_delete_server(message: Message):
        """Delete a server. Usage: /delete_server <id>"""
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "Usage: `/delete_server <id>`", parse_mode='Markdown')
            return

        try:
            server_id = int(parts[1])
        except ValueError:
            bot.send_message(message.chat.id, "Invalid server ID")
            return

        try:
            with get_db_session() as db:
                server = db.query(Server).filter(Server.id == server_id).first()
                if not server:
                    bot.send_message(message.chat.id, f"Server {server_id} not found")
                    return

                active_keys = len([k for k in server.keys if k.is_active])
                if active_keys > 0:
                    keyboard = InlineKeyboardMarkup()
                    keyboard.row(
                        InlineKeyboardButton("Force delete", callback_data=f"force_delete_server_{server_id}"),
                        InlineKeyboardButton("Cancel", callback_data="cancel_delete_server")
                    )
                    bot.send_message(
                        message.chat.id,
                        f"Server `{server.name}` (id={server_id}) has *{active_keys} active keys*.\n\n"
                        f"Force delete will deactivate all keys and remove the server.",
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                    return

                name = server.name
                db.delete(server)

            bot.send_message(
                message.chat.id,
                f"Server `{name}` (id={server_id}) deleted",
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error deleting server: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('force_delete_server_'))
    def handle_force_delete_server(call: CallbackQuery):
        """Force delete a server, deactivating all its keys."""
        if not is_admin(call.from_user.id):
            return

        server_id = int(call.data.replace('force_delete_server_', ''))

        try:
            with get_db_session() as db:
                server = db.query(Server).filter(Server.id == server_id).first()
                if not server:
                    bot.answer_callback_query(call.id, "Server not found")
                    return

                name = server.name
                # Deactivate all keys on this server
                from database.models import Key
                keys = db.query(Key).filter(Key.server_id == server_id, Key.is_active == True).all()
                for key in keys:
                    key.is_active = False

                db.delete(server)

            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                f"Server `{name}` (id={server_id}) deleted. {len(keys)} keys deactivated.",
                call.message.chat.id,
                call.message.id,
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error force deleting server: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Error")
            bot.send_message(call.message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data == 'cancel_delete_server')
    def handle_cancel_delete_server(call: CallbackQuery):
        """Cancel server deletion."""
        bot.answer_callback_query(call.id, "Cancelled")
        bot.edit_message_text("Deletion cancelled.", call.message.chat.id, call.message.id)

    # ── /manage_user ──────────────────────────────────────────
    def _format_user_info(db, telegram_id: int) -> tuple[str, Optional[User]]:
        """Build user info text. Returns (text, user_or_None)."""
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            return f"User with Telegram ID `{telegram_id}` not found.", None

        lines = [
            f"*User Management*\n",
            f"*Telegram ID:* `{user.telegram_id}`",
            f"*Username:* `@{user.username}`" if user.username else "*Username:* —",
            f"*Registered:* {format_msk(user.created_at)}",
        ]

        # Active subscription
        sub = db.query(Subscription).filter(
            Subscription.user_id == user.id,
            Subscription.is_active == True,
            Subscription.expires_at > datetime.utcnow()
        ).first()

        if sub:
            days_left = (sub.expires_at - datetime.utcnow()).days
            sub_type = "Test" if sub.is_test else "Paid"
            active_keys = db.query(Key).filter(
                Key.subscription_id == sub.id,
                Key.is_active == True
            ).count()
            key_servers = db.query(Key.server_id).filter(
                Key.subscription_id == sub.id,
                Key.is_active == True
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
            # Check for any subscription (expired)
            any_sub = db.query(Subscription).filter(
                Subscription.user_id == user.id
            ).order_by(Subscription.created_at.desc()).first()
            if any_sub:
                lines.append(f"\n*Subscription (id={any_sub.id}):*")
                lines.append(f"  Type: {'Test' if any_sub.is_test else 'Paid'}")
                lines.append(f"  Status: EXPIRED ({format_msk(any_sub.expires_at)})")
            else:
                lines.append("\n*Subscription:* None")

        # Has used test
        has_test = db.query(Subscription).filter(
            Subscription.user_id == user.id,
            Subscription.is_test == True
        ).first() is not None
        lines.append(f"*Test used:* {'Yes' if has_test else 'No'}")

        # Last transaction
        tx = db.query(Transaction).filter(
            Transaction.user_id == user.id
        ).order_by(Transaction.created_at.desc()).first()
        if tx:
            lines.append(f"\n*Last transaction (id={tx.id}):*")
            lines.append(f"  Plan: `{tx.plan}` | Amount: {tx.amount_rub}₽")
            lines.append(f"  Status: `{tx.status}`")
            lines.append(f"  Date: {format_msk(tx.created_at)}")

        return "\n".join(lines), user

    def _manage_user_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
        """Build inline keyboard for user management."""
        kb = InlineKeyboardMarkup()
        kb.row(InlineKeyboardButton("Refresh keys", callback_data=f"mu_refresh_{telegram_id}"))
        kb.row(InlineKeyboardButton("Adjust time", callback_data=f"mu_time_{telegram_id}"))
        kb.row(InlineKeyboardButton("Grant subscription", callback_data=f"mu_grantsub_{telegram_id}"))
        kb.row(InlineKeyboardButton("Reset test period", callback_data=f"mu_resettest_{telegram_id}"))
        return kb

    @bot.message_handler(commands=['manage_user'])
    def handle_manage_user(message: Message):
        """Show user info and management buttons. Usage: /manage_user <telegram_id>"""
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "Usage: `/manage_user <telegram_id>`", parse_mode='Markdown')
            return

        try:
            tg_id = int(parts[1])
        except ValueError:
            bot.send_message(message.chat.id, "Invalid Telegram ID")
            return

        try:
            with get_db_session() as db:
                text, user = _format_user_info(db, tg_id)
                if not user:
                    bot.send_message(message.chat.id, text, parse_mode='Markdown')
                    return

                bot.send_message(
                    message.chat.id,
                    text,
                    reply_markup=_manage_user_keyboard(tg_id),
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"Error in /manage_user: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── Refresh keys callback ─────────────────────────────────
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_refresh_'))
    def handle_mu_refresh(call: CallbackQuery):
        """Delete old keys, create new ones on a random server."""
        if not is_admin(call.from_user.id):
            return

        tg_id = int(call.data.replace('mu_refresh_', ''))
        bot.answer_callback_query(call.id, "Refreshing keys...")

        try:
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == tg_id).first()
                if not user:
                    bot.send_message(call.message.chat.id, "User not found")
                    return

                sub = db.query(Subscription).filter(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,
                    Subscription.expires_at > datetime.utcnow()
                ).first()

                if not sub:
                    bot.send_message(call.message.chat.id, "No active subscription to refresh keys for.")
                    return

                # Delete ALL keys (active AND inactive) from x-ui and DB
                # This handles stale keys that were marked inactive in DB
                # but still exist on the panel
                all_keys = db.query(Key).filter(
                    Key.subscription_id == sub.id
                ).all()

                from vpn.xui_client import XUIClient
                for key in all_keys:
                    if key.server_id:
                        server = db.query(Server).filter(Server.id == key.server_id).first()
                        if server:
                            try:
                                client = XUIClient(server)
                                client.delete_key(key)
                            except Exception as e:
                                logger.warning(f"Failed to delete key {key.remote_key_id} from server: {e}")
                    key.is_active = False
                db.commit()

                # Create new keys (lazy init — up to USER_SERVER_LIMIT)
                keys = KeyService.ensure_keys_exist(db, sub, user.telegram_id)

                server_names_list = []
                for key in keys:
                    if key.server_id:
                        srv = db.query(Server).filter(Server.id == key.server_id).first()
                        if srv and srv.name not in server_names_list:
                            server_names_list.append(srv.name)
                server_name = ", ".join(server_names_list) if server_names_list else "unknown"

                # Refresh the info message
                text, _ = _format_user_info(db, tg_id)
                bot.edit_message_text(
                    text + f"\n\n_Keys refreshed. New server: {server_name}_",
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=_manage_user_keyboard(tg_id),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error refreshing keys: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error refreshing keys: {e}")

    # ── Adjust time callback (starts dialog) ──────────────────
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_time_'))
    def handle_mu_time(call: CallbackQuery):
        """Start dialog to adjust subscription time."""
        if not is_admin(call.from_user.id):
            return

        tg_id = int(call.data.replace('mu_time_', ''))
        bot.answer_callback_query(call.id)

        msg = bot.send_message(
            call.message.chat.id,
            f"Enter hours to add/subtract for user `{tg_id}`.\n"
            f"Positive = add time, negative = reduce time.\n"
            f"Example: `48` or `-24`",
            parse_mode='Markdown',
        )
        bot.register_next_step_handler(msg, _process_adjust_time, tg_id)

    def _process_adjust_time(message: Message, tg_id: int):
        """Process the hours input for time adjustment."""
        if not is_admin(message.from_user.id):
            return

        try:
            hours = int(message.text.strip())
        except (ValueError, AttributeError):
            bot.send_message(message.chat.id, "Invalid number. Cancelled.")
            return

        try:
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == tg_id).first()
                if not user:
                    bot.send_message(message.chat.id, "User not found")
                    return

                sub = db.query(Subscription).filter(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,
                ).order_by(Subscription.created_at.desc()).first()

                if not sub:
                    bot.send_message(message.chat.id, "No active subscription found.")
                    return

                old_expiry = sub.expires_at
                sub.expires_at = sub.expires_at + timedelta(hours=hours)
                db.commit()

                sign = "+" if hours >= 0 else ""
                bot.send_message(
                    message.chat.id,
                    f"Subscription adjusted for user `{tg_id}`:\n"
                    f"  {sign}{hours}h\n"
                    f"  Old expiry: {format_msk(old_expiry)}\n"
                    f"  New expiry: {format_msk(sub.expires_at)}",
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error adjusting time: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── Grant subscription callback (starts dialog) ───────────
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_grantsub_'))
    def handle_mu_grantsub(call: CallbackQuery):
        """Start dialog to grant a paid subscription."""
        if not is_admin(call.from_user.id):
            return

        tg_id = int(call.data.replace('mu_grantsub_', ''))
        bot.answer_callback_query(call.id)

        msg = bot.send_message(
            call.message.chat.id,
            f"Enter expiry date for user `{tg_id}` in format `DD.MM.YYYY`\n"
            f"Example: `01.01.2027`",
            parse_mode='Markdown',
        )
        bot.register_next_step_handler(msg, _process_grant_subscription, tg_id)

    def _process_grant_subscription(message: Message, tg_id: int):
        """Process the date input and create subscription."""
        if not is_admin(message.from_user.id):
            return

        try:
            expires_at = datetime.strptime(message.text.strip(), "%d.%m.%Y").replace(
                hour=23, minute=59, second=59
            )
        except (ValueError, AttributeError):
            bot.send_message(message.chat.id, "Invalid date format. Use `DD.MM.YYYY`. Cancelled.", parse_mode='Markdown')
            return

        try:
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == tg_id).first()
                if not user:
                    bot.send_message(message.chat.id, "User not found")
                    return

                # Check for existing active non-expired subscription
                existing = db.query(Subscription).filter(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,
                    Subscription.expires_at > datetime.utcnow(),
                ).first()

                if existing:
                    bot.send_message(
                        message.chat.id,
                        f"User already has active subscription (id={existing.id}, "
                        f"expires {format_msk(existing.expires_at)}). "
                        f"Use *Adjust time* instead.",
                        parse_mode='Markdown',
                    )
                    return

                sub = Subscription(
                    user_id=user.id,
                    name="Main",
                    token=str(uuid.uuid4()),
                    expires_at=expires_at,
                    is_test=False,
                    is_active=True,
                )
                db.add(sub)
                log_activity(db, tg_id, "admin_grant_sub", f"до {format_msk(expires_at)}")
                db.flush()

                text, _ = _format_user_info(db, tg_id)
                bot.send_message(
                    message.chat.id,
                    text + f"\n\n_Subscription granted until {format_msk(expires_at)}_",
                    reply_markup=_manage_user_keyboard(tg_id),
                    parse_mode='Markdown',
                )

        except Exception as e:
            logger.error(f"Error granting subscription: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── Reset test period callback ────────────────────────────
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_resettest_'))
    def handle_mu_resettest(call: CallbackQuery):
        """Reset test period — delete all test subscriptions so user can get a new test."""
        if not is_admin(call.from_user.id):
            return

        tg_id = int(call.data.replace('mu_resettest_', ''))
        bot.answer_callback_query(call.id)

        try:
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == tg_id).first()
                if not user:
                    bot.send_message(call.message.chat.id, "User not found")
                    return

                test_subs = db.query(Subscription).filter(
                    Subscription.user_id == user.id,
                    Subscription.is_test == True
                ).all()

                if not test_subs:
                    bot.edit_message_text(
                        "User never had a test subscription.",
                        call.message.chat.id,
                        call.message.id
                    )
                    return

                count = 0
                for sub in test_subs:
                    # Delete keys from VPN servers
                    KeyService.delete_subscription_keys(db, sub)
                    db.delete(sub)
                    count += 1
                db.commit()

                text, _ = _format_user_info(db, tg_id)
                bot.edit_message_text(
                    text + f"\n\n_Test period reset. {count} test subscription(s) deleted._",
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=_manage_user_keyboard(tg_id),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error resetting test: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}")

    @bot.message_handler(commands=['delete_admin'])
    def handle_delete_admin(message: Message):
        """Delete admin user and all related data for testing."""
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ Access denied")
            return

        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == message.from_user.id
                ).first()

                if not user:
                    bot.send_message(message.chat.id, "✅ User not found (already deleted)")
                    return

                # Get all keys for deletion from x-ui
                keys = db.query(Key).filter(Key.subscription_id.in_(
                    db.query(Subscription.id).filter(Subscription.user_id == user.id)
                )).all()

                deleted_from_xui = 0
                failed_xui = 0

                # Delete keys from x-ui panels
                for key in keys:
                    try:
                        from vpn.xui_client import XUIClient
                        client = XUIClient(key.server)
                        client.delete_key(key)
                        deleted_from_xui += 1
                        logger.info(f"Deleted key {key.remote_key_id} from server {key.server.name}")
                    except Exception as e:
                        failed_xui += 1
                        logger.warning(f"Failed to delete key {key.remote_key_id}: {e}")

                # Delete from database
                deleted_keys = db.query(Key).filter(Key.subscription_id.in_(
                    db.query(Subscription.id).filter(Subscription.user_id == user.id)
                )).delete(synchronize_session=False)

                deleted_transactions = db.query(Transaction).filter(
                    Transaction.user_id == user.id
                ).delete()

                deleted_subs = db.query(Subscription).filter(
                    Subscription.user_id == user.id
                ).delete()

                # Delete user
                db.delete(user)
                db.commit()

                message_text = f"""✅ **Admin user deleted successfully**

**Deleted:**
• User: {message.from_user.id}
• Keys from x-ui: {deleted_from_xui} (failed: {failed_xui})
• Keys from DB: {deleted_keys}
• Transactions: {deleted_transactions}
• Subscriptions: {deleted_subs}

You can now start testing from scratch with /start"""

                bot.send_message(message.chat.id, message_text, parse_mode='Markdown')
                logger.info(f"Admin {message.from_user.id} deleted themselves via /delete_admin")

        except Exception as e:
            logger.error(f"Error in /delete_admin: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"❌ Error: {e}")

    # ── /check_reminders ──────────────────────────────────────
    @bot.message_handler(commands=['check_reminders'])
    def handle_check_reminders(message: Message):
        """Manually trigger subscription reminder check."""
        if not is_admin(message.from_user.id):
            return

        try:
            bot.send_message(message.chat.id, "🔄 Running subscription check...")

            from services import NotificationService
            with get_db_session() as db:
                sent_counts = NotificationService.check_and_send_reminders(db, bot)

            summary = (
                f"✅ **Reminder check completed**\n\n"
                f"Sent notifications:\n"
                f"• 7 days: {sent_counts['7d']}\n"
                f"• 3 days: {sent_counts['3d']}\n"
                f"• 1 day: {sent_counts['1d']}\n"
                f"• Expired: {sent_counts['expired']}\n"
                f"\nTotal: {sum(sent_counts.values())}"
            )

            bot.send_message(message.chat.id, summary, parse_mode='Markdown')
            logger.info(f"Manual reminder check triggered by admin {message.from_user.id}: {sent_counts}")

        except Exception as e:
            logger.error(f"Error in /check_reminders: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"❌ Error: {e}")

    # ── /add_old_keys ──────────────────────────────────────
    _add_old_keys_state = {}  # {chat_id: True} — waiting for CSV upload

    @bot.message_handler(commands=['add_old_keys'])
    def handle_add_old_keys(message: Message):
        """Start old keys import flow — ask admin to upload CSV."""
        if not is_admin(message.from_user.id):
            return

        _add_old_keys_state[message.chat.id] = True
        bot.send_message(
            message.chat.id,
            "Upload `user_info.csv` file.\n\n"
            "Expected format (no headers):\n"
            "`telegram_id, server_ip, outline_key1, outline_key1_id, payment_until, bool, outline_key2, outline_key2_id, something, vless_uri`\n\n"
            "Use `nokey`/`noid` for missing values.",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True)
        )

    @bot.message_handler(
        content_types=['document'],
        func=lambda m: m.chat.id in _add_old_keys_state and is_admin(m.from_user.id)
    )
    def handle_old_keys_csv_upload(message: Message):
        """Process uploaded CSV with old keys."""
        _add_old_keys_state.pop(message.chat.id, None)

        try:
            file_info = bot.get_file(message.document.file_id)
            file_bytes = bot.download_file(file_info.file_path)
            content = file_bytes.decode('utf-8-sig')

            reader = csv.reader(io.StringIO(content))
            stats = {
                "users": 0, "outline_keys": 0, "vless_keys": 0,
                "skipped_dup": 0, "errors": 0,
                "skipped_no_payment": 0, "skipped_expired": 0,
                "total_rows": 0, "skipped_no_keys": 0,
            }

            with get_db_session() as db:
                for row_num, row in enumerate(reader, 1):
                    stats["total_rows"] += 1
                    row = [c.strip() for c in row]
                    if len(row) < 10:
                        stats["errors"] += 1
                        logger.info(f"Row {row_num}: bad format ({len(row)} cols)")
                        continue

                    try:
                        telegram_id = int(row[0])
                    except ValueError:
                        stats["errors"] += 1
                        logger.info(f"Row {row_num}: bad telegram_id '{row[0]}'")
                        continue

                    # Parse payment_until — skip users with no active payment
                    try:
                        payment_until = float(row[4])
                    except ValueError:
                        payment_until = 0

                    if payment_until <= 0:
                        stats["skipped_no_payment"] += 1
                        continue

                    expiry = datetime.utcfromtimestamp(int(payment_until))
                    if expiry < datetime.utcnow():
                        stats["skipped_expired"] += 1
                        continue

                    # Check if row has any actual keys
                    has_outline1 = row[2].lower() not in ('nokey', '')
                    has_outline2 = row[6].lower() not in ('nokey', '')
                    has_vless = row[9].lower() not in ('nokey', '')
                    if not has_outline1 and not has_outline2 and not has_vless:
                        stats["skipped_no_keys"] += 1
                        continue

                    # Find or create user
                    user = db.query(User).filter(User.telegram_id == telegram_id).first()
                    if not user:
                        user = User(telegram_id=telegram_id)
                        db.add(user)
                        db.flush()

                    # Find active subscription or create legacy one
                    sub = db.query(Subscription).filter(
                        Subscription.user_id == user.id,
                        Subscription.is_active == True,
                    ).first()

                    if not sub:
                        sub = Subscription(
                            user_id=user.id,
                            name="Legacy",
                            token=str(uuid.uuid4()),
                            expires_at=expiry,
                            is_test=False,
                            is_active=True,
                        )
                        db.add(sub)
                        db.flush()

                    user_created = False

                    # Outline key 1
                    outline1 = row[2] if row[2].lower() not in ('nokey', '') else None
                    if outline1:
                        exists = db.query(Key).filter(Key.key_data == outline1).first()
                        if exists:
                            stats["skipped_dup"] += 1
                        else:
                            db.add(Key(
                                subscription_id=sub.id,
                                server_id=None,
                                protocol="outline",
                                key_data=outline1,
                                remarks="Outline (legacy)",
                                is_active=True,
                            ))
                            stats["outline_keys"] += 1
                            user_created = True

                    # Outline key 2
                    outline2 = row[6] if row[6].lower() not in ('nokey', '') else None
                    if outline2:
                        exists = db.query(Key).filter(Key.key_data == outline2).first()
                        if exists:
                            stats["skipped_dup"] += 1
                        else:
                            db.add(Key(
                                subscription_id=sub.id,
                                server_id=None,
                                protocol="outline",
                                key_data=outline2,
                                remarks="Outline (legacy)",
                                is_active=True,
                            ))
                            stats["outline_keys"] += 1
                            user_created = True

                    # VLESS key
                    vless = row[9] if row[9].lower() not in ('nokey', '') else None
                    if vless:
                        exists = db.query(Key).filter(Key.key_data == vless).first()
                        if exists:
                            stats["skipped_dup"] += 1
                        else:
                            # Extract host from vless URI for remarks
                            host = "unknown"
                            try:
                                at_idx = vless.index('@')
                                colon_idx = vless.index(':', at_idx)
                                host = vless[at_idx + 1:colon_idx]
                            except (ValueError, IndexError):
                                pass
                            db.add(Key(
                                subscription_id=sub.id,
                                server_id=None,
                                protocol="xui",
                                key_data=vless,
                                remarks=f"{host} (old key)",
                                is_active=True,
                            ))
                            stats["vless_keys"] += 1
                            user_created = True

                    if user_created:
                        stats["users"] += 1

                db.commit()

            bot.send_message(
                message.chat.id,
                f"*Import complete*\n\n"
                f"Total rows: {stats['total_rows']}\n"
                f"Users with keys: {stats['users']}\n"
                f"Outline keys: {stats['outline_keys']}\n"
                f"VLESS keys: {stats['vless_keys']}\n"
                f"\n*Skipped:*\n"
                f"Never paid: {stats['skipped_no_payment']}\n"
                f"Payment expired: {stats['skipped_expired']}\n"
                f"No keys in row: {stats['skipped_no_keys']}\n"
                f"Duplicate keys: {stats['skipped_dup']}\n"
                f"Bad rows: {stats['errors']}",
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error importing old keys: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error importing CSV: `{e}`", parse_mode='Markdown')

    # ── /remove_old_keys ─────────────────────────────────
    @bot.message_handler(commands=['remove_old_keys'])
    def handle_remove_old_keys(message: Message):
        """Show count of legacy keys and ask for confirmation."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                count = db.query(Key).filter(
                    Key.server_id.is_(None),
                    Key.is_active == True,
                ).count()

                if count == 0:
                    bot.send_message(message.chat.id, "No active legacy keys found.")
                    return

                keyboard = InlineKeyboardMarkup()
                keyboard.row(
                    InlineKeyboardButton(f"Delete {count} legacy keys", callback_data="confirm_remove_old_keys"),
                    InlineKeyboardButton("Cancel", callback_data="cancel_remove_old_keys")
                )

                bot.send_message(
                    message.chat.id,
                    f"Found *{count}* active legacy keys (`server_id=NULL`).\n\n"
                    f"This will soft-delete them (mark `is_active=False`). "
                    f"Keys will NOT be removed from VPN servers.",
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error in /remove_old_keys: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data == 'confirm_remove_old_keys')
    def handle_confirm_remove_old_keys(call: CallbackQuery):
        """Soft-delete all legacy keys."""
        if not is_admin(call.from_user.id):
            return

        try:
            with get_db_session() as db:
                count = db.query(Key).filter(
                    Key.server_id.is_(None),
                    Key.is_active == True,
                ).update({Key.is_active: False})
                db.commit()

            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                f"Done. {count} legacy keys marked inactive.",
                call.message.chat.id,
                call.message.id
            )

        except Exception as e:
            logger.error(f"Error removing old keys: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Error")
            bot.send_message(call.message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data == 'cancel_remove_old_keys')
    def handle_cancel_remove_old_keys(call: CallbackQuery):
        """Cancel old keys removal."""
        bot.answer_callback_query(call.id, "Cancelled")
        bot.edit_message_text("Removal cancelled.", call.message.chat.id, call.message.id)

    logger.info("Admin handlers registered")
