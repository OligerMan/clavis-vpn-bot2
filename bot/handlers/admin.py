"""Admin command handlers for Telegram bot."""

import csv
import io
import json
import logging
import random
import secrets
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from py3xui import Api, Inbound
from py3xui.inbound import Settings, Sniffing, StreamSettings
from telebot import TeleBot
from telebot.types import Message, CallbackQuery, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton

from sqlalchemy import func, Integer

from database import get_db_session
from database.models import Server, ServerGroup, User, Subscription, Key, Transaction, ActivityLog, ConnectionProfile, ServerInbound, ReferralInvite
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


def _create_inbound_with_profile(api, profile, remark: str) -> dict:
    """Create a VLESS Reality inbound using ConnectionProfile settings.

    Returns dict: inbound_id, port, public_key, private_key, short_id, sni
    """
    private_key, public_key = _generate_x25519_keys()
    short_ids = _generate_short_ids()
    port = random.randint(20000, 60000)
    sni = profile.sni
    dest = profile.dest or f"{sni}:443"
    server_names = [sni]
    if not sni.startswith("www."):
        server_names.append(f"www.{sni}")
    else:
        server_names.append(sni[4:])

    reality_settings = {
        "show": False, "xver": 0, "target": dest, "serverNames": server_names,
        "privateKey": private_key, "minClientVer": "", "maxClientVer": "",
        "maxTimediff": 0, "shortIds": short_ids,
        "settings": {"publicKey": public_key, "fingerprint": profile.fingerprint,
                     "serverName": "", "spiderX": "/"},
    }
    stream_settings = StreamSettings(
        security=profile.security, network=profile.network,
        tcp_settings={"acceptProxyProtocol": False, "header": {"type": "none"}},
        reality_settings=reality_settings,
    )
    api.inbound.add(Inbound(
        enable=True, port=port, protocol=profile.protocol,
        settings=Settings(decryption="none"),
        stream_settings=stream_settings, sniffing=Sniffing(enabled=True),
        remark=remark,
    ))

    all_inbounds = api.inbound.get_list()
    created = next((ib for ib in all_inbounds if ib.port == port and ib.protocol == profile.protocol), None)
    if not created:
        raise RuntimeError("Inbound was created but could not be found on panel")

    return {
        "inbound_id": created.id,
        "port": port,
        "public_key": public_key,
        "private_key": private_key,
        "short_id": short_ids[0],
        "sni": sni,
    }


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
            "<b>Admin Commands</b>\n\n"
            "<b>Server management:</b>\n"
            "/servers — list all servers grouped by server set\n"
            "/groups — quick overview of server groups\n"
            "/add_server — add server (dialog)\n"
            "/add_group — create a new server group\n"
            "/activate_group — bulk-create keys for a group\n"
            "/check_server — health check (version, uptime, clients)\n"
            "/toggle_server — enable/disable server\n"
            "/delete_server — delete server\n"
            "\n<b>Connection profiles:</b>\n"
            "/profiles — list all connection profiles\n"
            "/import_profile — import profile from existing inbound\n"
            "/add_profile — create profile manually\n"
            "/assign_profile — assign profile to server (creates inbound)\n"
            "\n<b>User management:</b>\n"
            "/manage_user — user info, keys, subscription, actions\n"
            "\n<b>Legacy keys:</b>\n"
            "/add_old_keys — import legacy keys from CSV\n"
            "/remove_old_keys — soft-delete all legacy keys\n"
            "\n<b>Referral links:</b>\n"
            "/generate_ref_link — generate deep link with ref tag\n"
            "/ref_links — list, stats, delete, grant access to ref links\n"
            "\n<b>Service analytics:</b>\n"
            "/report — service dashboard (users, subs, payments, servers)\n"
            "/analytics — conversion, ARPU, revenue by plan\n"
            "/traffic — traffic stats per server (live from x-ui)\n"
            "/sub_graph — subscription growth chart since launch\n"
            "/invite_stat [N] — top N users by invite activity (default 25)\n"
            "/logs — last N user actions (default 50)\n"
            "/last_logs — only new actions since last call\n"
            "\n<b>Other:</b>\n"
            "/broadcast — interactive broadcast to a list of users\n"
            "/check_reminders — manually run subscription expiry check\n"
            "/backup — send database backup file\n"
            "/monitor_status — server monitoring state\n"
            "\n<b>App releases:</b>\n"
            "/release — publish Windows app update (upload .exe → set version)\n"
            "\n"
            "/admin_help — this message",
            parse_mode='HTML',
        )

    # ── /report ───────────────────────────────────────────────
    def _build_report_text(ref_source: str = None) -> str:
        """Build report text (reused by command and refresh callback)."""
        with get_db_session() as db:
            now = datetime.utcnow()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)

            admin_user_ids = db.query(User.id).filter(
                User.telegram_id.in_(ADMIN_IDS)
            ).subquery()

            # Build filtered user IDs subquery
            base_user_q = db.query(User.id).filter(~User.id.in_(admin_user_ids))
            if ref_source:
                base_user_q = base_user_q.filter(User.ref_source == ref_source)
            filtered_user_ids = base_user_q.subquery()

            total_users = db.query(func.count(User.id)).filter(
                User.id.in_(filtered_user_ids)
            ).scalar()
            new_7d = db.query(func.count(User.id)).filter(
                User.created_at >= week_ago,
                User.id.in_(filtered_user_ids),
            ).scalar()
            new_30d = db.query(func.count(User.id)).filter(
                User.created_at >= month_ago,
                User.id.in_(filtered_user_ids),
            ).scalar()

            active_paid = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.expires_at > now,
                Subscription.is_test == False,
                Subscription.plan_type != 'free',
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()
            active_standard = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.expires_at > now,
                Subscription.is_test == False,
                Subscription.plan_type == 'basic',
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()
            active_unlimited = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.expires_at > now,
                Subscription.is_test == False,
                Subscription.plan_type == 'unlimited',
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()
            active_invite = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.expires_at > now,
                Subscription.is_test == False,
                Subscription.plan_type == 'free',
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()
            active_test = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.expires_at > now,
                Subscription.is_test == True,
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()
            expired = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.expires_at <= now,
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()
            real_payments_cutoff = datetime(2026, 2, 20)
            non_admin_tx = db.query(Transaction).filter(
                Transaction.user_id.in_(filtered_user_ids),
                Transaction.created_at >= real_payments_cutoff,
                Transaction.plan != 'donation',
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

            new_paid_7d = db.query(func.count(func.distinct(non_admin_tx.c.user_id))).filter(
                non_admin_tx.c.status == 'completed',
                non_admin_tx.c.completed_at >= week_ago,
            ).scalar()

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

            # Donation stats for report
            rpt_donation = db.query(
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.amount), 0),
            ).filter(
                Transaction.status == 'completed',
                Transaction.plan == 'donation',
                Transaction.user_id.in_(filtered_user_ids),
                Transaction.created_at >= real_payments_cutoff,
            ).one()
            rpt_donation_count, rpt_donation_sum = rpt_donation

            total_servers = db.query(func.count(Server.id)).scalar()
            active_servers = db.query(func.count(Server.id)).filter(
                Server.is_active == True
            ).scalar()
            total_keys = db.query(func.count(Key.id)).filter(
                Key.is_active == True,
                Key.server_id.isnot(None),
            ).scalar()
            total_capacity = 0  # no longer used

        def fmt_rub(kopeks: int) -> str:
            rub = kopeks // 100
            return f"{rub:,}".replace(",", " ")

        header = "*Отчёт по сервису*"
        if ref_source:
            header += f" (источник: `{ref_source}`)"
        header += "\n\n"

        return (
            header +
            "*Пользователи*\n"
            f"  Всего: {total_users}\n"
            f"  Новых за 7 дней: {new_7d}\n"
            f"  Новых за 30 дней: {new_30d}\n\n"
            "*Подписки*\n"
            f"  Активных платных: {active_paid}"
            f" (Стандарт: {active_standard}, Безлимит: {active_unlimited})\n"
            f"  Активных тестовых: {active_test}\n"
            f"  Активных инвайт: {active_invite}\n"
            f"  Истекших (всего): {expired}\n\n"
            "*Платежи*\n"
            f"  Успешных: {completed_count} на {fmt_rub(completed_sum_kopeks)}₽\n"
            f"  Ожидающих: {pending_count}\n"
            f"  Неудачных: {failed_count}\n"
            f"  Новых платных за 7 дней: {new_paid_7d}\n"
            f"  За 7 дней: {rev_7d_count} на {fmt_rub(rev_7d_sum)}₽\n"
            f"  За 30 дней: {rev_30d_count} на {fmt_rub(rev_30d_sum)}₽\n"
            f"  Пожертвования: {rpt_donation_count} на {fmt_rub(rpt_donation_sum)}₽\n\n"
            "*Серверы*\n"
            f"  Активных: {active_servers} из {total_servers}\n"
            f"  Ключей: {total_keys}\n\n"
            f"_{format_msk(now)}_"
        )

    def _report_keyboard(ref_source: str = None) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup()
        if ref_source:
            kb.row(
                InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_report:{ref_source}"),
                InlineKeyboardButton("Сбросить фильтр", callback_data="refresh_report"),
            )
        else:
            kb.row(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_report"))
        kb.row(InlineKeyboardButton("Фильтр по источнику", callback_data="filter_rpt_src"))
        return kb

    @bot.message_handler(commands=['report'])
    def handle_report(message: Message):
        if not is_admin(message.from_user.id):
            return
        try:
            text = _build_report_text()
            bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=_report_keyboard())
        except Exception as e:
            logger.error(f"Error in /report: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda c: c.data == 'refresh_report' or c.data.startswith('refresh_report:'))
    def handle_refresh_report(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        try:
            ref_source = None
            if ':' in call.data:
                ref_source = call.data.split(':', 1)[1]
            text = _build_report_text(ref_source)
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                parse_mode='Markdown', reply_markup=_report_keyboard(ref_source),
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error refreshing /report: {e}", exc_info=True)
            bot.answer_callback_query(call.id, text=f"Error: {e}")

    @bot.callback_query_handler(func=lambda c: c.data == 'filter_rpt_src')
    def handle_filter_report_sources(call: CallbackQuery):
        """Show list of known ref_source values for report filtering."""
        if not is_admin(call.from_user.id):
            return
        try:
            with get_db_session() as db:
                sources = db.query(
                    User.ref_source, func.count(User.id)
                ).filter(
                    User.ref_source.isnot(None),
                    ~User.telegram_id.in_(ADMIN_IDS),
                ).group_by(User.ref_source).order_by(User.ref_source).all()

            if not sources:
                bot.answer_callback_query(call.id, "Нет пользователей с реферальным источником")
                return

            kb = InlineKeyboardMarkup()
            for src, cnt in sources:
                kb.row(InlineKeyboardButton(f"{src} ({cnt})", callback_data=f"rpt_src:{src}"))
            kb.row(InlineKeyboardButton("← Назад", callback_data="refresh_report"))

            bot.edit_message_text(
                "*Выберите источник для фильтрации:*",
                call.message.chat.id, call.message.message_id,
                parse_mode='Markdown', reply_markup=kb,
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in filter_report_sources: {e}", exc_info=True)
            bot.answer_callback_query(call.id, text=f"Error: {e}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith('rpt_src:'))
    def handle_report_by_source(call: CallbackQuery):
        """Show report filtered by a specific ref_source."""
        if not is_admin(call.from_user.id):
            return
        try:
            ref_source = call.data.split(':', 1)[1]
            text = _build_report_text(ref_source)
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                parse_mode='Markdown', reply_markup=_report_keyboard(ref_source),
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in report_by_source: {e}", exc_info=True)
            bot.answer_callback_query(call.id, text=f"Error: {e}")

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
        "invite_created": "Инвайт создан",
        "invite_used": "Инвайт использован",
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
    def _build_analytics_text(ref_source: str = None) -> str:
        """Build analytics text (reused by command and refresh callback)."""
        with get_db_session() as db:
            now = datetime.utcnow()
            real_payments_cutoff = datetime(2026, 2, 20)

            admin_user_ids = db.query(User.id).filter(
                User.telegram_id.in_(ADMIN_IDS)
            ).subquery()

            # Build filtered user IDs subquery
            base_user_q = db.query(User.id).filter(~User.id.in_(admin_user_ids))
            if ref_source:
                base_user_q = base_user_q.filter(User.ref_source == ref_source)
            filtered_user_ids = base_user_q.subquery()

            # Build filtered telegram_id list (for ActivityLog queries)
            filtered_tg_q = db.query(User.telegram_id).filter(~User.id.in_(admin_user_ids))
            if ref_source:
                filtered_tg_q = filtered_tg_q.filter(User.ref_source == ref_source)
            filtered_tg_ids = [r[0] for r in filtered_tg_q.all()]

            # ── Funnel ──
            total_users = db.query(func.count(User.id)).filter(
                User.id.in_(filtered_user_ids)
            ).scalar()

            active_paid = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.expires_at > now,
                Subscription.is_test == False,
                Subscription.plan_type != 'free',
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()
            active_test = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.expires_at > now,
                Subscription.is_test == True,
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()

            new_users = db.query(func.count(func.distinct(ActivityLog.telegram_id))).filter(
                ActivityLog.action == 'new_user',
                ActivityLog.telegram_id.in_(filtered_tg_ids),
            ).scalar()

            test_tg_from_log = set(
                r[0] for r in db.query(ActivityLog.telegram_id).filter(
                    ActivityLog.action == 'test_key',
                    ActivityLog.telegram_id.in_(filtered_tg_ids),
                ).all()
            )
            test_tg_from_sub = set(
                r[0] for r in db.query(User.telegram_id).join(Subscription).filter(
                    Subscription.is_test == True,
                    User.id.in_(filtered_user_ids),
                ).all()
            )
            all_test_tg = test_tg_from_log | test_tg_from_sub
            test_users = len(all_test_tg)

            paid_tg = set(
                r[0] for r in db.query(User.telegram_id).join(
                    Transaction, User.id == Transaction.user_id
                ).filter(
                    Transaction.status == 'completed',
                    User.id.in_(filtered_user_ids),
                    Transaction.created_at >= real_payments_cutoff,
                ).all()
            )
            paid_users = len(paid_tg)

            converted_from_test = len(all_test_tg & paid_tg)

            active_test_tg = set(
                r[0] for r in db.query(User.telegram_id).join(Subscription).filter(
                    Subscription.is_test == True,
                    Subscription.is_active == True,
                    Subscription.expires_at > now,
                    User.id.in_(filtered_user_ids),
                ).all()
            )
            undecided_tg = active_test_tg - paid_tg
            test_decided = len(all_test_tg - undecided_tg)

            conv_test = (converted_from_test / test_decided * 100) if test_decided > 0 else 0
            paid_without_test = len(paid_tg - all_test_tg)

            new_tg = set(
                r[0] for r in db.query(ActivityLog.telegram_id).filter(
                    ActivityLog.action == 'new_user',
                    ActivityLog.telegram_id.in_(filtered_tg_ids),
                ).all()
            )
            new_paid = len(new_tg & paid_tg)
            conv_total = (new_paid / new_users * 100) if new_users > 0 else 0

            # ── Renewals ──
            renewal_users = db.query(func.count(func.distinct(ActivityLog.telegram_id))).filter(
                ActivityLog.action == 'sub_extended',
                ActivityLog.telegram_id.in_(filtered_tg_ids),
            ).scalar()
            renewal_pct = (renewal_users / paid_users * 100) if paid_users > 0 else 0

            # ── Revenue by plan ──
            plan_stats = db.query(
                Transaction.plan,
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.amount), 0),
            ).filter(
                Transaction.status == 'completed',
                Transaction.plan != 'donation',
                Transaction.user_id.in_(filtered_user_ids),
                Transaction.created_at >= real_payments_cutoff,
            ).group_by(Transaction.plan).all()

            total_revenue = 0
            tier_groups = {'basic': [], 'unlimited': [], 'other': []}
            tier_totals = {'basic': 0, 'unlimited': 0, 'other': 0}
            for plan_key, count, amount in plan_stats:
                plan_info = PLANS.get(plan_key, {})
                desc = plan_info.get('description', plan_key)
                price = plan_info.get('price_display', '?')
                rub = amount // 100
                total_revenue += amount
                line = f"  {desc} ({price}): {count} шт — {rub:,}₽".replace(",", " ")
                pt = plan_info.get('plan_type', 'other')
                bucket = pt if pt in tier_groups else 'other'
                tier_groups[bucket].append(line)
                tier_totals[bucket] += amount

            # ── Donations ──
            donation_stats = db.query(
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.amount), 0),
            ).filter(
                Transaction.status == 'completed',
                Transaction.plan == 'donation',
                Transaction.user_id.in_(filtered_user_ids),
                Transaction.created_at >= real_payments_cutoff,
            ).one()
            donation_count, donation_sum = donation_stats
            donation_rub = donation_sum // 100

            total_rub = total_revenue // 100
            arpu = (total_rub // paid_users) if paid_users > 0 else 0

            # ── Expiring soon ──
            expiring_7d = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.is_test == False,
                Subscription.plan_type != 'free',
                Subscription.expires_at > now,
                Subscription.expires_at <= now + timedelta(days=7),
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()

            expiring_30d = db.query(func.count(Subscription.id)).filter(
                Subscription.is_active == True,
                Subscription.is_test == False,
                Subscription.plan_type != 'free',
                Subscription.expires_at > now,
                Subscription.expires_at <= now + timedelta(days=30),
                Subscription.user_id.in_(filtered_user_ids),
            ).scalar()

            # ── Referral invites (global stats) ──
            invite_total = db.query(func.count(ReferralInvite.id)).scalar() or 0
            invite_creators = db.query(func.count(func.distinct(ReferralInvite.inviter_id))).scalar() or 0
            invite_used = db.query(func.count(ReferralInvite.id)).filter(
                ReferralInvite.activated_at.isnot(None)
            ).scalar() or 0

        # ── Build message ──
        header = "*Аналитика*"
        if ref_source:
            header += f" (источник: `{ref_source}`)"
        header += "\n\n"

        text = (
            header +
            "*Воронка*\n"
            f"  Всего пользователей: {total_users}\n"
            f"  Активных платных: {active_paid}\n"
            f"  Активных тестовых: {active_test}\n"
            f"  Новых (v2): {new_users}\n"
            f"  Получили тест-ключ: {test_users}\n"
            f"  Оплатили (всего): {paid_users}\n"
            f"  Оплатили после теста: {converted_from_test}\n"
            f"  Оплатили без теста: {paid_without_test}\n"
            f"  Конверсия тест → оплата: {conv_test:.1f}%"
            f"  ({converted_from_test} из {test_decided} решивших)\n"
            f"  Конверсия рег → оплата: {conv_total:.1f}%"
            f"  ({new_paid} из {new_users} новых)\n\n"
            "*Продления*\n"
            f"  Продлили подписку: {renewal_users}\n"
            f"  Доля продлений: {renewal_pct:.1f}%\n\n"
            "*Выручка по тарифам*\n"
        )
        if tier_groups['basic']:
            text += "  _Стандарт:_\n" + "\n".join(tier_groups['basic']) + "\n"
            text += f"  Итого Стандарт: {tier_totals['basic'] // 100:,}₽\n".replace(",", " ")
        if tier_groups['unlimited']:
            text += "  _Безлимит:_\n" + "\n".join(tier_groups['unlimited']) + "\n"
            text += f"  Итого Безлимит: {tier_totals['unlimited'] // 100:,}₽\n".replace(",", " ")
        if tier_groups['other']:
            text += "  _Прочее:_\n" + "\n".join(tier_groups['other']) + "\n"
        text += (
            f"  *Итого: {total_rub:,}₽*\n\n".replace(",", " ") +
            f"*ARPU:* {arpu:,}₽\n\n".replace(",", " ") +
            "*Пожертвования*\n"
            f"  Количество: {donation_count}\n"
            f"  Сумма: {donation_rub:,}₽\n\n".replace(",", " ") +
            "*Истекают*\n"
            f"  В ближайшие 7 дней: {expiring_7d}\n"
            f"  В ближайшие 30 дней: {expiring_30d}\n\n"
            f"*Инвайты*\n"
            f"  Создано инвайтов: {invite_total}\n"
            f"  Пользователей создали: {invite_creators}\n"
            f"  Использовано: {invite_used}\n\n"
            f"_{format_msk(now)}_"
        )
        return text

    def _analytics_keyboard(ref_source: str = None) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup()
        if ref_source:
            kb.row(
                InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_analytics:{ref_source}"),
                InlineKeyboardButton("Сбросить фильтр", callback_data="refresh_analytics"),
            )
        else:
            kb.row(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_analytics"))
        kb.row(InlineKeyboardButton("Фильтр по источнику", callback_data="filter_anl_src"))
        return kb

    @bot.message_handler(commands=['analytics'])
    def handle_analytics(message: Message):
        if not is_admin(message.from_user.id):
            return
        try:
            text = _build_analytics_text()
            bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=_analytics_keyboard())
        except Exception as e:
            logger.error(f"Error in /analytics: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda c: c.data == 'refresh_analytics' or c.data.startswith('refresh_analytics:'))
    def handle_refresh_analytics(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        try:
            ref_source = None
            if ':' in call.data:
                ref_source = call.data.split(':', 1)[1]
            text = _build_analytics_text(ref_source)
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                parse_mode='Markdown', reply_markup=_analytics_keyboard(ref_source),
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error refreshing /analytics: {e}", exc_info=True)
            bot.answer_callback_query(call.id, text=f"Error: {e}")

    @bot.callback_query_handler(func=lambda c: c.data == 'filter_anl_src')
    def handle_filter_analytics_sources(call: CallbackQuery):
        """Show list of known ref_source values for analytics filtering."""
        if not is_admin(call.from_user.id):
            return
        try:
            with get_db_session() as db:
                sources = db.query(
                    User.ref_source, func.count(User.id)
                ).filter(
                    User.ref_source.isnot(None),
                    ~User.telegram_id.in_(ADMIN_IDS),
                ).group_by(User.ref_source).order_by(User.ref_source).all()

            if not sources:
                bot.answer_callback_query(call.id, "Нет пользователей с реферальным источником")
                return

            kb = InlineKeyboardMarkup()
            for src, cnt in sources:
                kb.row(InlineKeyboardButton(f"{src} ({cnt})", callback_data=f"anl_src:{src}"))
            kb.row(InlineKeyboardButton("← Назад", callback_data="refresh_analytics"))

            bot.edit_message_text(
                "*Выберите источник для фильтрации:*",
                call.message.chat.id, call.message.message_id,
                parse_mode='Markdown', reply_markup=kb,
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in filter_analytics_sources: {e}", exc_info=True)
            bot.answer_callback_query(call.id, text=f"Error: {e}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith('anl_src:'))
    def handle_analytics_by_source(call: CallbackQuery):
        """Show analytics filtered by a specific ref_source."""
        if not is_admin(call.from_user.id):
            return
        try:
            ref_source = call.data.split(':', 1)[1]
            text = _build_analytics_text(ref_source)
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                parse_mode='Markdown', reply_markup=_analytics_keyboard(ref_source),
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in analytics_by_source: {e}", exc_info=True)
            bot.answer_callback_query(call.id, text=f"Error: {e}")

    # ── /generate_ref_link ─────────────────────────────────────
    @bot.message_handler(commands=['generate_ref_link'])
    def handle_generate_ref_link(message: Message):
        """Generate a deep link with a referral tag."""
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            msg = bot.send_message(
                message.chat.id,
                "Введите тег источника (латиница, цифры, `_`, `-`).\n"
                "Например: `instagram`, `youtube_ad`, `friend`",
                parse_mode='Markdown',
                reply_markup=ForceReply(selective=True),
            )
            bot.register_next_step_handler(msg, _process_ref_tag)
            return

        _send_ref_link(message.chat.id, parts[1].strip())

    def _process_ref_tag(message: Message):
        if not is_admin(message.from_user.id):
            return
        tag = (message.text or "").strip()
        if not tag:
            bot.send_message(message.chat.id, "Тег не может быть пустым.")
            return
        _send_ref_link(message.chat.id, tag)

    def _send_ref_link(chat_id: int, tag: str):
        import re
        from database.models import RefLink as _RefLink
        tag = re.sub(r'[^a-zA-Z0-9_\-]', '', tag)[:50]
        if not tag:
            bot.send_message(chat_id, "Тег должен содержать латинские буквы, цифры, `_` или `-`.", parse_mode='Markdown')
            return

        # Ensure RefLink record exists
        try:
            with get_db_session() as db:
                if not db.query(_RefLink).filter(_RefLink.tag == tag).first():
                    db.add(_RefLink(tag=tag))
        except Exception:
            pass  # non-critical

        username = bot.get_me().username
        link = f"https://t.me/{username}?start=ref_{tag}"
        bot.send_message(
            chat_id,
            f"Реферальная ссылка для `{tag}`:\n\n`{link}`",
            parse_mode='Markdown',
        )

    # ── /traffic ──────────────────────────────────────────────
    def _fmt_bytes(b: int) -> str:
        if b >= 1024**4:
            return f"{b / 1024**4:.1f} TB"
        if b >= 1024**3:
            return f"{b / 1024**3:.1f} GB"
        if b >= 1024**2:
            return f"{b / (1024**2):.0f} MB"
        return f"{b / 1024:.0f} KB"

    @bot.message_handler(commands=['traffic'])
    def handle_traffic(message: Message):
        """Show live traffic stats per server from x-ui panels."""
        if not is_admin(message.from_user.id):
            return

        bot.send_message(message.chat.id, "Собираю данные с серверов...")

        try:
            from vpn.xui_client import XUIClient

            with get_db_session() as db:
                now = datetime.utcnow()
                week_ago = now - timedelta(days=7)
                servers = db.query(Server).filter(Server.is_active == True).all()

                if not servers:
                    bot.send_message(message.chat.id, "Нет активных серверов.")
                    return

                from collections import defaultdict as _defaultdict

                total_keys = 0
                total_traffic = 0
                total_monthly = 0
                # group -> list of (name, db_count, new_7d, traffic, monthly_est)
                groups: dict = _defaultdict(list)
                errors = []

                # Fetch clients from all servers in parallel to avoid hanging
                # on slow/unreachable servers (each timeout adds ~2-3 minutes).
                # SQLAlchemy sessions aren't thread-safe — each worker uses its own.
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _fetch(server_id):
                    try:
                        with get_db_session() as tdb:
                            srv = tdb.query(Server).get(server_id)
                            clients = KeyService.list_all_clients_for_server(tdb, srv)
                        return server_id, clients, None
                    except Exception as exc:
                        return server_id, None, exc

                clients_by_server: dict = {}
                server_name_by_id = {s.id: s.name for s in servers}
                with ThreadPoolExecutor(max_workers=min(16, len(servers))) as pool:
                    futures = [pool.submit(_fetch, s.id) for s in servers]
                    for fut in as_completed(futures):
                        sid, clients, exc = fut.result()
                        if exc is not None:
                            errors.append(f"{server_name_by_id.get(sid, sid)}: {exc}")
                        else:
                            clients_by_server[sid] = clients

                for server in servers:
                    if server.id not in clients_by_server:
                        continue
                    clients = clients_by_server[server.id]

                    # Traffic by email from panel
                    traffic_by_email = {
                        c.email: c.upload_bytes + c.download_bytes
                        for c in clients
                    }

                    # DB keys for this server (all active, for display)
                    db_keys = db.query(Key).filter(
                        Key.server_id == server.id,
                        Key.is_active == True,
                    ).all()

                    # Keys with non-expired subscriptions (for monthly estimate)
                    db_keys_active = db.query(Key).join(
                        Subscription, Key.subscription_id == Subscription.id
                    ).filter(
                        Key.server_id == server.id,
                        Key.is_active == True,
                        Subscription.expires_at > now,
                    ).all()

                    db_count = len(db_keys)
                    new_7d = sum(
                        1 for k in db_keys
                        if k.created_at and k.created_at >= week_ago
                    )

                    srv_traffic = sum(traffic_by_email.values())

                    # Monthly estimate: only keys with active subscriptions
                    srv_monthly = 0
                    for k in db_keys_active:
                        t = traffic_by_email.get(k.remote_key_id, 0)
                        if t <= 0:
                            continue
                        age_days = max(
                            (now - k.created_at).total_seconds() / 86400,
                            1.0,
                        ) if k.created_at else 1.0
                        srv_monthly += t / age_days * 30

                    total_keys += db_count
                    total_traffic += srv_traffic
                    total_monthly += srv_monthly

                    group = server.server_set or "default"
                    groups[group].append((
                        server.id, server.name, db_count, new_7d,
                        srv_traffic, int(srv_monthly),
                    ))

                # Sort servers alphabetically within each group
                for g in groups:
                    groups[g].sort(key=lambda r: r[1])  # sort by name

                preferred = KeyService.get_preferred_server_ids()

                # Build table
                hdr = (
                    f"{'Сервер':<17} {'Кл':>3} {'+7д':>3}"
                    f" {'Трафик':>8} {'~мес':>8}"
                )
                sep = "─" * len(hdr)
                lines = [hdr, sep]

                for group_name in sorted(groups.keys()):
                    rows = groups[group_name]
                    if len(groups) > 1:
                        lines.append(f"[ {group_name} ]")

                    g_keys = 0
                    g_traffic = 0
                    g_monthly = 0
                    for sid, name, cnt, new, traffic, monthly in rows:
                        g_keys += cnt
                        g_traffic += traffic
                        g_monthly += monthly
                        mark = "*" if sid in preferred else " "
                        lines.append(
                            f"{name[:16]:<16}{mark} {cnt:>3} {new:>3}"
                            f" {_fmt_bytes(traffic):>8}"
                            f" {_fmt_bytes(monthly):>8}"
                        )

                    if len(rows) > 1 and len(groups) > 1:
                        lines.append(
                            f"{'':>17} {g_keys:>3} {'':>3}"
                            f" {_fmt_bytes(g_traffic):>8}"
                            f" {_fmt_bytes(g_monthly):>8}"
                        )

                lines.append(sep)
                lines.append(
                    f"{'Итого':<17} {total_keys:>3} {'':>3}"
                    f" {_fmt_bytes(total_traffic):>8}"
                    f" {_fmt_bytes(int(total_monthly)):>8}"
                )

                if preferred:
                    lines.append("* в ротации для новых")

                for err in errors:
                    lines.append(f"⚠ {err}")

                lines.append(f"\n{format_msk(now)}")

                bot.send_message(
                    message.chat.id,
                    "```\n" + "\n".join(lines) + "\n```",
                    parse_mode='Markdown',
                )

        except Exception as e:
            logger.error(f"Error in /traffic: {e}", exc_info=True)
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

                        # Show ServerInbound info if available
                        si_list = db.query(ServerInbound).filter(
                            ServerInbound.server_id == s.id,
                            ServerInbound.is_active == True,
                        ).all()

                        if si_list:
                            inbound_parts = []
                            for si in si_list:
                                profile = db.query(ConnectionProfile).filter(
                                    ConnectionProfile.id == si.profile_id
                                ).first()
                                pname = profile.name if profile else "?"
                                inbound_parts.append(
                                    f"  inbound `{si.inbound_id}`: port `{si.port}` | {pname}"
                                )
                            creds_info = "\n".join(inbound_parts)
                        elif s.api_credentials:
                            try:
                                creds = json.loads(s.api_credentials)
                                inbound = creds.get("inbound_id", "?")
                                conn = creds.get("connection_settings", {})
                                port = conn.get("port", "?")
                                sni = conn.get("sni", "?")
                                creds_info = (
                                    f"  inbound: `{inbound}` | "
                                    f"port: `{port}` | sni: `{sni}` (legacy)"
                                )
                            except json.JSONDecodeError:
                                creds_info = "  credentials: invalid JSON"
                        else:
                            creds_info = ""

                        lines.append(
                            f"  *{s.id}.* `{s.name}` [{status}]\n"
                            f"  host: `{s.host}`\n"
                            f"{creds_info}\n"
                            f"  keys: {keys_count}"
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

    # ── /profiles ──────────────────────────────────────────────
    @bot.message_handler(commands=['profiles'])
    def handle_profiles(message: Message):
        """List all connection profiles."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                profiles = db.query(ConnectionProfile).order_by(ConnectionProfile.id).all()

                if not profiles:
                    bot.send_message(message.chat.id, "No profiles configured. Use /import_profile or /add_profile.")
                    return

                lines = ["<b>Connection Profiles:</b>\n"]
                for p in profiles:
                    status = "ON" if p.is_active else "OFF"
                    server_count = db.query(ServerInbound).filter(
                        ServerInbound.profile_id == p.id,
                        ServerInbound.is_active == True,
                    ).count()
                    lines.append(
                        f"  <b>{p.id}.</b> <code>{p.name}</code> [{status}]\n"
                        f"  sni: <code>{p.sni}</code> | dest: <code>{p.dest or '-'}</code>\n"
                        f"  {p.protocol}/{p.security}/{p.network} | flow: <code>{p.flow}</code>\n"
                        f"  Servers: {server_count}\n"
                    )

                bot.send_message(message.chat.id, "\n".join(lines), parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error in /profiles: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /add_profile ─────────────────────────────────────────
    _add_profile_state = {}

    @bot.message_handler(commands=['add_profile'])
    def handle_add_profile(message: Message):
        """Create a connection profile manually."""
        if not is_admin(message.from_user.id):
            return
        _add_profile_state[message.chat.id] = {"step": "name"}
        bot.send_message(
            message.chat.id,
            "Enter profile name (e.g. `VLESS Microsoft`):",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True),
        )

    @bot.message_handler(
        func=lambda m: m.chat.id in _add_profile_state
        and _add_profile_state[m.chat.id]["step"] == "name"
    )
    def handle_add_profile_name(message: Message):
        if not is_admin(message.from_user.id):
            return
        state = _add_profile_state[message.chat.id]
        state["name"] = message.text.strip()
        state["step"] = "sni"
        bot.send_message(
            message.chat.id,
            "Enter SNI (e.g. `www.microsoft.com`):",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True),
        )

    @bot.message_handler(
        func=lambda m: m.chat.id in _add_profile_state
        and _add_profile_state[m.chat.id]["step"] == "sni"
    )
    def handle_add_profile_sni(message: Message):
        if not is_admin(message.from_user.id):
            return
        state = _add_profile_state.pop(message.chat.id)
        sni = message.text.strip()
        dest = f"{sni}:443"

        try:
            with get_db_session() as db:
                profile = ConnectionProfile(
                    name=state["name"],
                    sni=sni,
                    dest=dest,
                )
                db.add(profile)
                db.commit()
                db.refresh(profile)

                bot.send_message(
                    message.chat.id,
                    f"Profile created: #{profile.id} `{profile.name}`\n"
                    f"SNI: `{profile.sni}` | dest: `{profile.dest}`\n\n"
                    f"Use `/assign_profile <server_id> {profile.id}` to assign to a server.",
                    parse_mode='Markdown',
                )
        except Exception as e:
            logger.error(f"Error in /add_profile: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── /import_profile (button-based) ──────────────────────
    @bot.message_handler(commands=['import_profile'])
    def handle_import_profile(message: Message):
        """Step 1: show server selection buttons."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                servers = db.query(Server).filter(
                    Server.is_active == True,
                    Server.protocol == 'xui',
                ).order_by(Server.name).all()
                server_list = [(s.id, s.name) for s in servers]

            if not server_list:
                bot.send_message(message.chat.id, "No active XUI servers.")
                return

            keyboard = InlineKeyboardMarkup()
            for sid, sname in server_list:
                keyboard.row(InlineKeyboardButton(sname, callback_data=f"imp_srv_{sid}"))
            keyboard.row(InlineKeyboardButton("Cancel", callback_data="imp_cancel"))

            bot.send_message(
                message.chat.id,
                "<b>Import Profile</b>\n\nSelect server:",
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Error in /import_profile: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('imp_srv_'))
    def handle_import_profile_server(call: CallbackQuery):
        """Step 2: connect to panel, show inbound selection buttons."""
        if not is_admin(call.from_user.id):
            return

        server_id = int(call.data.replace('imp_srv_', ''))
        bot.answer_callback_query(call.id)

        try:
            with get_db_session() as db:
                server = db.query(Server).filter(Server.id == server_id).first()
                if not server:
                    bot.edit_message_text(
                        f"Server {server_id} not found.",
                        call.message.chat.id, call.message.id,
                    )
                    return

                sname = server.name
                api_url = server.api_url
                creds = json.loads(server.api_credentials)

                # IDs of inbounds already imported for this server
                imported_ids = {
                    si.inbound_id for si in
                    db.query(ServerInbound).filter(
                        ServerInbound.server_id == server_id,
                    ).all()
                }

            bot.edit_message_text(
                f"Connecting to <b>{sname}</b>...",
                call.message.chat.id, call.message.id,
                parse_mode='HTML',
            )

            api = Api(
                api_url,
                username=creds["username"],
                password=creds["password"],
                use_tls_verify=creds.get("use_tls_verify", True),
            )
            api.login()
            inbounds = api.inbound.get_list()

            # Filter out already-imported inbounds
            available = [ib for ib in inbounds if ib.id not in imported_ids]

            if not available:
                bot.edit_message_text(
                    f"<b>{sname}</b>: all inbounds already imported.",
                    call.message.chat.id, call.message.id,
                    parse_mode='HTML',
                )
                return

            keyboard = InlineKeyboardMarkup()
            for ib in available:
                n_clients = len(ib.settings.clients) if ib.settings and ib.settings.clients else 0
                remark = ib.remark or ""
                label = f"#{ib.id} :{ib.port} ({remark}) — {n_clients} clients"
                keyboard.row(InlineKeyboardButton(
                    label, callback_data=f"imp_ib_{server_id}_{ib.id}",
                ))
            keyboard.row(InlineKeyboardButton("Cancel", callback_data="imp_cancel"))

            bot.edit_message_text(
                f"<b>Import Profile — {sname}</b>\n\nSelect inbound:",
                call.message.chat.id, call.message.id,
                parse_mode='HTML',
                reply_markup=keyboard,
            )

        except Exception as e:
            logger.error(f"Error in import_profile server select: {e}", exc_info=True)
            bot.edit_message_text(
                f"Error: {e}",
                call.message.chat.id, call.message.id,
            )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('imp_ib_'))
    def handle_import_profile_inbound(call: CallbackQuery):
        """Step 3: import selected inbound as profile."""
        if not is_admin(call.from_user.id):
            return

        parts = call.data.replace('imp_ib_', '').split('_')
        server_id = int(parts[0])
        target_inbound_id = int(parts[1])
        bot.answer_callback_query(call.id)

        try:
            with get_db_session() as db:
                server = db.query(Server).filter(Server.id == server_id).first()
                if not server:
                    bot.edit_message_text(
                        f"Server {server_id} not found.",
                        call.message.chat.id, call.message.id,
                    )
                    return

                # Double-check not already imported
                existing_si = db.query(ServerInbound).filter(
                    ServerInbound.server_id == server_id,
                    ServerInbound.inbound_id == target_inbound_id,
                ).first()
                if existing_si:
                    bot.edit_message_text(
                        f"Inbound {target_inbound_id} on {server.name} "
                        f"already imported as profile #{existing_si.profile_id}.",
                        call.message.chat.id, call.message.id,
                    )
                    return

                bot.edit_message_text(
                    f"Importing inbound #{target_inbound_id} from <b>{server.name}</b>...",
                    call.message.chat.id, call.message.id,
                    parse_mode='HTML',
                )

                # Connect to panel and find inbound
                creds = json.loads(server.api_credentials)
                api = Api(
                    server.api_url,
                    username=creds["username"],
                    password=creds["password"],
                    use_tls_verify=creds.get("use_tls_verify", True),
                )
                api.login()
                inbounds = api.inbound.get_list()

                target = None
                for ib in inbounds:
                    if ib.id == target_inbound_id:
                        target = ib
                        break

                if not target:
                    bot.edit_message_text(
                        f"Inbound {target_inbound_id} not found on panel.",
                        call.message.chat.id, call.message.id,
                    )
                    return

                # Extract config
                cfg = _extract_inbound_config(target)

                ss = target.stream_settings
                reality = getattr(ss, 'reality_settings', None) or {}
                private_key = reality.get('privateKey', '')
                dest = reality.get('target', '')
                server_names = reality.get('serverNames', [])

                # Create or reuse profile
                profile_name = f"VLESS {cfg['sni']}" if cfg['sni'] else f"VLESS inbound-{target_inbound_id}"

                existing_profile = db.query(ConnectionProfile).filter(
                    ConnectionProfile.name == profile_name
                ).first()
                if existing_profile:
                    profile = existing_profile
                else:
                    profile = ConnectionProfile(
                        name=profile_name,
                        protocol=cfg.get('protocol', 'vless'),
                        security=cfg.get('security', 'reality'),
                        network='tcp',
                        flow=cfg.get('flow', 'xtls-rprx-vision'),
                        fingerprint=cfg.get('fingerprint', 'chrome'),
                        sni=cfg.get('sni', ''),
                        dest=dest,
                    )
                    db.add(profile)
                    db.flush()

                si = ServerInbound(
                    server_id=server.id,
                    profile_id=profile.id,
                    inbound_id=target_inbound_id,
                    port=cfg['port'],
                    public_key=cfg.get('pbk', ''),
                    short_id=cfg.get('sid', ''),
                    private_key=private_key,
                )
                db.add(si)
                db.commit()
                db.refresh(si)
                db.refresh(profile)

                info = (
                    f"<b>Profile imported:</b>\n"
                    f"  Profile: #{profile.id} <code>{profile.name}</code>\n"
                    f"  SNI: <code>{profile.sni}</code>\n"
                    f"  Dest: <code>{dest}</code>\n"
                    f"  ServerNames: <code>{', '.join(server_names)}</code>\n\n"
                    f"<b>ServerInbound created:</b>\n"
                    f"  Server: {server.name} (#{server.id})\n"
                    f"  Inbound ID: {target_inbound_id}\n"
                    f"  Port: {cfg['port']}\n"
                    f"  PBK: <code>{cfg.get('pbk', '')[:20]}...</code>\n"
                    f"  SID: <code>{cfg.get('sid', '')}</code>\n\n"
                    f"Use <code>/assign_profile &lt;server_id&gt; {profile.id}</code> "
                    f"to assign this profile to other servers."
                )
                bot.send_message(call.message.chat.id, info, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error in import_profile inbound select: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda call: call.data == 'imp_cancel')
    def handle_import_profile_cancel(call: CallbackQuery):
        """Cancel import profile flow."""
        bot.answer_callback_query(call.id, "Cancelled")
        bot.edit_message_text(
            "Import cancelled.",
            call.message.chat.id, call.message.id,
        )

    # ── /assign_profile <server_id> <profile_id> ─────────────
    @bot.message_handler(commands=['assign_profile'])
    def handle_assign_profile(message: Message):
        """Assign a profile to a server — creates a new inbound on the x-ui panel."""
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(
                message.chat.id,
                "Usage: `/assign_profile <server_id> <profile_id>`",
                parse_mode='Markdown',
            )
            return

        try:
            server_id = int(parts[1])
            profile_id = int(parts[2])
        except ValueError:
            bot.send_message(message.chat.id, "server_id and profile_id must be integers.")
            return

        try:
            with get_db_session() as db:
                server = db.query(Server).filter(Server.id == server_id).first()
                if not server:
                    bot.send_message(message.chat.id, f"Server {server_id} not found.")
                    return

                profile = db.query(ConnectionProfile).filter(
                    ConnectionProfile.id == profile_id
                ).first()
                if not profile:
                    bot.send_message(message.chat.id, f"Profile {profile_id} not found.")
                    return

                # Check if already assigned
                existing = db.query(ServerInbound).filter(
                    ServerInbound.server_id == server_id,
                    ServerInbound.profile_id == profile_id,
                ).first()
                if existing:
                    bot.send_message(
                        message.chat.id,
                        f"Profile `{profile.name}` already assigned to `{server.name}`.",
                        parse_mode='Markdown',
                    )
                    return

                bot.send_message(
                    message.chat.id,
                    f"Creating inbound on {server.name} with profile {profile.name}...",
                )

                # Connect to panel
                creds = json.loads(server.api_credentials)
                api = Api(
                    server.api_url,
                    username=creds["username"],
                    password=creds["password"],
                    use_tls_verify=creds.get("use_tls_verify", True),
                )
                api.login()

                # Generate keys and create inbound
                private_key, public_key = _generate_x25519_keys()
                short_ids = _generate_short_ids()
                port = random.randint(20000, 60000)
                dest = profile.dest or f"{profile.sni}:443"
                sni = profile.sni
                server_names = [sni]
                if not sni.startswith("www."):
                    server_names.append(f"www.{sni}")
                else:
                    bare = sni[4:]
                    server_names.append(bare)

                reality_settings = {
                    "show": False,
                    "xver": 0,
                    "target": dest,
                    "serverNames": server_names,
                    "privateKey": private_key,
                    "minClientVer": "",
                    "maxClientVer": "",
                    "maxTimediff": 0,
                    "shortIds": short_ids,
                    "settings": {
                        "publicKey": public_key,
                        "fingerprint": profile.fingerprint,
                        "serverName": "",
                        "spiderX": "/",
                    }
                }

                tcp_settings = {
                    "acceptProxyProtocol": False,
                    "header": {"type": "none"},
                }

                stream_settings = StreamSettings(
                    security=profile.security,
                    network=profile.network,
                    tcp_settings=tcp_settings,
                    reality_settings=reality_settings,
                )

                sniffing = Sniffing(enabled=True)
                settings = Settings(decryption="none")

                inbound = Inbound(
                    enable=True,
                    port=port,
                    protocol=profile.protocol,
                    settings=settings,
                    stream_settings=stream_settings,
                    sniffing=sniffing,
                    remark=f"clavis_{profile.name.lower().replace(' ', '_')}",
                )

                api.inbound.add(inbound)

                # Re-fetch to get assigned ID
                all_inbounds = api.inbound.get_list()
                created = None
                for ib in all_inbounds:
                    if ib.port == port and ib.protocol == profile.protocol:
                        created = ib
                        break

                if not created:
                    bot.send_message(message.chat.id, "Inbound created but could not be found on panel.")
                    return

                # Save ServerInbound
                si = ServerInbound(
                    server_id=server.id,
                    profile_id=profile.id,
                    inbound_id=created.id,
                    port=port,
                    public_key=public_key,
                    short_id=short_ids[0],
                    private_key=private_key,
                )
                db.add(si)
                db.commit()
                db.refresh(si)

                # Check if server has existing subscriptions
                existing_keys_count = db.query(Key).filter(
                    Key.server_id == server.id,
                    Key.is_active == True,
                ).count()

                result_text = (
                    f"<b>Inbound created on {server.name}</b>\n"
                    f"  Profile: {profile.name}\n"
                    f"  Inbound ID: {created.id}\n"
                    f"  Port: {port}\n"
                    f"  SNI: <code>{sni}</code>\n"
                    f"  PBK: <code>{public_key[:20]}...</code>\n"
                )

                if existing_keys_count > 0:
                    result_text += (
                        f"\nServer has {existing_keys_count} existing keys. "
                        f"Creating keys for existing subscriptions..."
                    )
                    bot.send_message(message.chat.id, result_text, parse_mode='HTML')

                    stats = KeyService.create_keys_for_new_inbound(db, si)
                    bot.send_message(
                        message.chat.id,
                        f"Done: {stats['created']} created, "
                        f"{stats['skipped']} skipped, {stats['failed']} failed.",
                    )
                else:
                    bot.send_message(message.chat.id, result_text, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error in /assign_profile: {e}", exc_info=True)
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

        # Get existing groups from ServerGroup table
        try:
            with get_db_session() as db:
                existing_groups = sorted(
                    g.name for g in db.query(ServerGroup).all()
                )
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

        # Also persist new group to ServerGroup table
        try:
            with get_db_session() as db:
                if not db.query(ServerGroup).filter(ServerGroup.name == group_name).first():
                    db.add(ServerGroup(name=group_name))
        except Exception:
            pass  # non-critical, group will still work via server_set

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
        """Show profile selection buttons for the new inbound."""
        if not is_admin(call.from_user.id):
            return

        state = _add_server_state.get(call.message.chat.id)
        if not state or state.get("step") != "no_inbound":
            bot.answer_callback_query(call.id, "Session expired. Run /add_server again.")
            return

        bot.answer_callback_query(call.id)

        try:
            with get_db_session() as db:
                profiles = db.query(ConnectionProfile).filter(
                    ConnectionProfile.is_active == True
                ).order_by(ConnectionProfile.name).all()
                profile_list = [(p.id, p.name) for p in profiles]

            if not profile_list:
                bot.edit_message_text(
                    "No active connection profiles found. Create one first with /add_profile.",
                    call.message.chat.id, call.message.id
                )
                _add_server_state.pop(call.message.chat.id, None)
                return

            state["step"] = "select_profile"
            kb = InlineKeyboardMarkup()
            for pid, pname in profile_list:
                kb.add(InlineKeyboardButton(pname, callback_data=f"add_srv_profile_{pid}"))
            kb.add(InlineKeyboardButton("Cancel", callback_data="add_srv_cancel"))

            bot.edit_message_text(
                "Select connection profile for the new inbound:",
                call.message.chat.id, call.message.id,
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Error showing profile list: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: `{e}`", parse_mode='Markdown')
            _add_server_state.pop(call.message.chat.id, None)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('add_srv_profile_'))
    def handle_add_srv_profile(call: CallbackQuery):
        """Create inbound with selected profile and save Server + ServerInbound."""
        if not is_admin(call.from_user.id):
            return

        state = _add_server_state.get(call.message.chat.id)
        if not state or state.get("step") != "select_profile":
            bot.answer_callback_query(call.id, "Session expired. Run /add_server again.")
            return

        try:
            profile_id = int(call.data.split('_')[-1])
        except ValueError:
            bot.answer_callback_query(call.id, "Invalid profile.")
            return

        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "Creating inbound with selected profile...",
            call.message.chat.id, call.message.id
        )

        try:
            with get_db_session() as db:
                profile = db.query(ConnectionProfile).filter(
                    ConnectionProfile.id == profile_id,
                    ConnectionProfile.is_active == True,
                ).first()
                if not profile:
                    bot.send_message(call.message.chat.id, "Profile not found.")
                    _add_server_state.pop(call.message.chat.id, None)
                    return

                profile_name = profile.name
                profile_sni = profile.sni

                # Connect to panel and create inbound
                api = Api(state["api_url"], username=XUI_USERNAME, password=XUI_PASSWORD, use_tls_verify=True)
                api.login()

                cfg = _create_inbound_with_profile(api, profile, remark=state["name"])

                # Save Server
                credentials = {
                    "username": XUI_USERNAME,
                    "password": XUI_PASSWORD,
                    "use_tls_verify": True,
                }
                group = state.get("group", "default")
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

                # Save ServerInbound
                si = ServerInbound(
                    server_id=server.id,
                    profile_id=profile.id,
                    inbound_id=cfg["inbound_id"],
                    port=cfg["port"],
                    public_key=cfg["public_key"],
                    short_id=cfg["short_id"],
                    private_key=cfg["private_key"],
                )
                db.add(si)
                db.commit()
                db.refresh(server)
                db.refresh(si)
                server_id = server.id
                si_id = si.id

            # Setup domain blocking
            domain_block_msg = ""
            try:
                from vpn.xui_client import XUIClient
                with get_db_session() as db:
                    srv = db.query(Server).get(server_id)
                    srv_si = db.query(ServerInbound).get(si_id)
                    xui = XUIClient(srv, server_inbound=srv_si)
                    block_result = xui.setup_domain_blocking()
                    parts = []
                    if block_result["routing_updated"]:
                        parts.append("routing OK")
                    if block_result["sniffing_updated"]:
                        parts.append("sniffing OK")
                    if block_result["errors"]:
                        parts.append(f"errors: {block_result['errors']}")
                    domain_block_msg = f"\nDomain blocking: {', '.join(parts) if parts else 'no changes'}"
            except Exception as e:
                domain_block_msg = f"\nDomain blocking setup failed: {e}"
                logger.error(f"Domain blocking setup failed for {state['name']}: {e}")

            # Recalculate server scores
            scores_msg = ""
            try:
                with get_db_session() as db:
                    preferred = KeyService.recalculate_server_scores(db)
                scores_msg = f"\nServer scores recalculated, preferred: {len(preferred)}"
            except Exception as e:
                scores_msg = f"\nScore recalc failed: {e}"
                logger.error(f"Score recalc after add_server failed: {e}")

            success_text = (
                f"*Server added successfully!*\n\n"
                f"ID: `{server_id}`\n"
                f"Name: `{state['name']}`\n"
                f"Group: `{group}`\n"
                f"Domain: `{state['domain']}`\n"
                f"Profile: `{profile_name}` (SNI: `{profile_sni}`)\n"
                f"Inbound ID: `{cfg['inbound_id']}`\n"
                f"Port: `{cfg['port']}`\n"
                f"PBK: `{cfg['public_key'][:24]}...`"
                f"{domain_block_msg}"
                f"{scores_msg}"
            )
            try:
                bot.send_message(call.message.chat.id, success_text, parse_mode='Markdown')
            except Exception as send_err:
                logger.error(f"Failed to send success message with Markdown: {send_err}")
                # Fallback: send without markdown so user always gets the result
                try:
                    bot.send_message(
                        call.message.chat.id,
                        success_text.replace('*', '').replace('`', ''),
                        parse_mode="",
                    )
                except Exception as send_err2:
                    logger.error(f"Failed to send plain success message: {send_err2}")

        except Exception as e:
            logger.error(f"Error creating inbound: {e}", exc_info=True)
            try:
                bot.send_message(
                    call.message.chat.id,
                    f"Error creating inbound: {e}",
                    parse_mode="",
                )
            except Exception as send_err:
                logger.error(f"Failed to send error message: {send_err}")

        _add_server_state.pop(call.message.chat.id, None)

    @bot.callback_query_handler(func=lambda call: call.data == 'add_srv_cancel')
    def handle_add_srv_cancel(call: CallbackQuery):
        """Cancel /add_server at profile selection step."""
        if not is_admin(call.from_user.id):
            return
        _add_server_state.pop(call.message.chat.id, None)
        bot.edit_message_text("Cancelled.", call.message.chat.id, call.message.id)
        bot.answer_callback_query(call.id)

    # ── /add_group ────────────────────────────────────────────
    _add_group_state: dict = {}  # chat_id -> {"step": "name", "prompt_id": int}

    @bot.message_handler(commands=['add_group'])
    def handle_add_group(message: Message):
        """Start add group dialog."""
        if not is_admin(message.from_user.id):
            return

        msg = bot.send_message(
            message.chat.id,
            "Введите название новой группы серверов\n"
            "(например `Germany`, `Netherlands`):",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True),
        )
        _add_group_state[message.chat.id] = {"step": "name", "prompt_id": msg.id}

    @bot.message_handler(
        func=lambda m: (
            m.chat.id in _add_group_state
            and _add_group_state[m.chat.id].get("step") == "name"
            and m.reply_to_message is not None
        )
    )
    def handle_add_group_name(message: Message):
        """Got group name, save to DB."""
        if not is_admin(message.from_user.id):
            return

        _add_group_state.pop(message.chat.id, None)
        group_name = message.text.strip()

        if not group_name or len(group_name) > 50:
            bot.send_message(message.chat.id, "Название должно быть от 1 до 50 символов.")
            return

        try:
            with get_db_session() as db:
                existing = db.query(ServerGroup).filter(ServerGroup.name == group_name).first()
                if existing:
                    bot.send_message(
                        message.chat.id,
                        f"Группа `{group_name}` уже существует (id={existing.id}).",
                        parse_mode='Markdown',
                    )
                    return

                sg = ServerGroup(name=group_name)
                db.add(sg)
                db.flush()
                sg_id = sg.id

            bot.send_message(
                message.chat.id,
                f"Группа `{group_name}` создана (id={sg_id}).\n"
                f"Теперь её можно выбрать при /add\\_server.",
                parse_mode='Markdown',
            )
        except Exception as e:
            logger.error(f"Error creating group: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Ошибка: {e}")

    # ── /groups ───────────────────────────────────────────────
    @bot.message_handler(commands=['groups'])
    def handle_groups(message: Message):
        """Quick overview of server groups."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                from collections import defaultdict as _defaultdict
                groups: dict = _defaultdict(lambda: {"servers": 0, "active": 0, "keys": 0})

                # Include all registered groups (even empty ones)
                for sg in db.query(ServerGroup).all():
                    _ = groups[sg.name]  # ensure entry exists

                servers = db.query(Server).all()
                for s in servers:
                    g = groups[s.server_set or "default"]
                    g["servers"] += 1
                    if s.is_active:
                        g["active"] += 1
                    g["keys"] += len([k for k in s.keys if k.is_active])

                if not groups:
                    bot.send_message(message.chat.id, "No groups configured.")
                    return

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

            # Recalculate server scores so new group is included in rotation
            try:
                with get_db_session() as db:
                    KeyService.recalculate_server_scores(db)
            except Exception as e:
                logger.warning(f"Failed to recalculate scores after group activation: {e}")

            # Invalidate subscription caches so new keys appear immediately
            if stats['created'] > 0:
                try:
                    from subscription.cache import invalidate_subscription_cache
                    with get_db_session() as db:
                        tokens = db.query(Subscription.token).filter(
                            Subscription.is_active == True,
                            Subscription.token.isnot(None),
                        ).all()
                        for (token,) in tokens:
                            invalidate_subscription_cache(token)
                    logger.info(f"Invalidated {len(tokens)} subscription caches after group activation")
                except Exception as e:
                    logger.warning(f"Failed to invalidate caches after group activation: {e}")

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
                si = db.query(ServerInbound).filter(
                    ServerInbound.server_id == server.id,
                    ServerInbound.is_active == True,
                ).first()
                client = XUIClient(server, server_inbound=si)
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
        from services.user_management_service import format_user_info
        return format_user_info(db, telegram_id)

    def _manage_user_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
        """Build inline keyboard for user management."""
        kb = InlineKeyboardMarkup()
        kb.row(InlineKeyboardButton("Refresh keys", callback_data=f"mu_refresh_{telegram_id}"))
        kb.row(InlineKeyboardButton("🔄 Rotate link (24h grace)", callback_data=f"mu_rotate_{telegram_id}"))
        kb.row(InlineKeyboardButton("Adjust time", callback_data=f"mu_time_{telegram_id}"))
        kb.row(InlineKeyboardButton("Grant subscription", callback_data=f"mu_grantsub_{telegram_id}"))
        kb.row(InlineKeyboardButton("Reset test period", callback_data=f"mu_resettest_{telegram_id}"))
        kb.row(InlineKeyboardButton("🔗 Sub link", callback_data=f"mu_sublink_{telegram_id}"))
        kb.row(InlineKeyboardButton("🔄 Reset WL traffic", callback_data=f"mu_resetwl_{telegram_id}"))
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

        from services.user_management_service import refresh_keys

        tg_id = int(call.data.replace('mu_refresh_', ''))
        bot.answer_callback_query(call.id, "Refreshing keys...")

        try:
            with get_db_session() as db:
                ok, result = refresh_keys(db, tg_id)
                if not ok:
                    bot.send_message(call.message.chat.id, result)
                    return

                text, _ = _format_user_info(db, tg_id)
                bot.edit_message_text(
                    text + f"\n\n_{result}_",
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=_manage_user_keyboard(tg_id),
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error refreshing keys: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error refreshing keys: {e}")

    # ── Rotate subscription link callback (destructive → confirm) ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_rotate_'))
    def handle_mu_rotate(call: CallbackQuery):
        """Ask for confirmation before rotating a user's subscription link."""
        if not is_admin(call.from_user.id):
            return
        tg_id = int(call.data.replace('mu_rotate_', ''))
        bot.answer_callback_query(call.id)
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("✅ Rotate", callback_data=f"mu_rotcfm_{tg_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"mu_rotcxl_{tg_id}"),
        )
        bot.send_message(
            call.message.chat.id,
            f"⚠️ Rotate subscription link for `{tg_id}`?\n\n"
            f"• Old link dies immediately\n"
            f"• Old keys keep working 24h, then stop\n"
            f"• New link + new keys on the *same* servers, same expiry",
            reply_markup=kb,
            parse_mode='Markdown',
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_rotcfm_'))
    def handle_mu_rotate_confirm(call: CallbackQuery):
        """Execute the subscription-link rotation."""
        if not is_admin(call.from_user.id):
            return
        from services.user_management_service import rotate_subscription

        tg_id = int(call.data.replace('mu_rotcfm_', ''))
        bot.answer_callback_query(call.id, "Rotating...")
        # Drop the confirm buttons immediately so a repeat tap can't re-fire the rotation
        # (belt-and-suspenders alongside the per-user lock + dedup guard in the service).
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.id, reply_markup=None)
        except Exception:
            pass
        try:
            with get_db_session() as db:
                ok, result = rotate_subscription(db, tg_id)
                if not ok:
                    bot.edit_message_text(result, call.message.chat.id, call.message.id)
                    return
                text, _ = _format_user_info(db, tg_id)
                bot.edit_message_text(
                    text + f"\n\n{result}",
                    call.message.chat.id,
                    call.message.id,
                    reply_markup=_manage_user_keyboard(tg_id),
                    parse_mode='Markdown',
                )
        except Exception as e:
            logger.error(f"Error rotating subscription: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}", parse_mode="")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_rotcxl_'))
    def handle_mu_rotate_cancel(call: CallbackQuery):
        """Cancel the rotation."""
        if not is_admin(call.from_user.id):
            return
        bot.answer_callback_query(call.id)
        bot.edit_message_text("Отменено.", call.message.chat.id, call.message.id)

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

        from services.user_management_service import adjust_time

        try:
            hours = int(message.text.strip())
        except (ValueError, AttributeError):
            bot.send_message(message.chat.id, "Invalid number. Cancelled.")
            return

        try:
            with get_db_session() as db:
                ok, result = adjust_time(db, tg_id, hours)
                bot.send_message(
                    message.chat.id,
                    f"User `{tg_id}`: {result}",
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error adjusting time: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── Grant subscription callback (starts dialog) ───────────
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_grantsub_') and not call.data.startswith('mu_grantsub_cancel_'))
    def handle_mu_grantsub(call: CallbackQuery):
        """Start dialog to grant a paid subscription — first ask plan type."""
        if not is_admin(call.from_user.id):
            return

        tg_id = int(call.data.replace('mu_grantsub_', ''))
        bot.answer_callback_query(call.id)

        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("📦 Стандарт", callback_data=f"mu_grant_type_basic_{tg_id}"),
            InlineKeyboardButton("🔥 Безлимит", callback_data=f"mu_grant_type_unlimited_{tg_id}"),
        )
        kb.row(InlineKeyboardButton("❌ Отмена", callback_data=f"mu_grantsub_cancel_{tg_id}"))
        bot.send_message(
            call.message.chat.id,
            "Выберите тариф для подписки:",
            reply_markup=kb,
        )

    def _do_grant_subscription(db, tg_id: int, user, expires_at: datetime, existing_sub, plan_type: str = "basic"):
        """Create new or replace existing subscription.  Delegates to shared service."""
        from services.user_management_service import _do_grant
        _do_grant(db, tg_id, user, expires_at, existing_sub, plan_type=plan_type)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_grant_type_'))
    def handle_mu_grant_type(call: CallbackQuery):
        """Handle plan type selection — then ask for expiry date."""
        if not is_admin(call.from_user.id):
            return
        bot.answer_callback_query(call.id)

        # Parse: mu_grant_type_basic_123 or mu_grant_type_unlimited_123
        parts = call.data.split('_')  # ['mu', 'grant', 'type', 'basic'|'unlimited', tg_id]
        plan_type = parts[3]
        tg_id = int(parts[4])

        plan_label = "Безлимит" if plan_type == "unlimited" else "Стандарт"
        bot.edit_message_text(
            f"Тариф: *{plan_label}*\n\n"
            f"Введите дату окончания для `{tg_id}` в формате `ДД.ММ.ГГГГ`\n"
            f"Пример: `01.01.2027`",
            call.message.chat.id,
            call.message.id,
            parse_mode='Markdown',
        )
        bot.register_next_step_handler(call.message, _process_grant_date, tg_id, plan_type)

    def _process_grant_date(message: Message, tg_id: int, plan_type: str):
        """Process date input and create/replace subscription."""
        if not is_admin(message.from_user.id):
            return

        try:
            expires_at = datetime.strptime(message.text.strip(), "%d.%m.%Y").replace(
                hour=23, minute=59, second=59
            )
        except (ValueError, AttributeError):
            bot.send_message(message.chat.id, "Неверный формат даты. Используйте `ДД.ММ.ГГГГ`. Отменено.", parse_mode='Markdown')
            return

        try:
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == tg_id).first()
                if not user:
                    bot.send_message(message.chat.id, "User not found")
                    return

                existing = db.query(Subscription).filter(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,
                    Subscription.expires_at > datetime.utcnow(),
                ).first()

                _do_grant_subscription(db, tg_id, user, expires_at=expires_at, existing_sub=existing, plan_type=plan_type)
                plan_label = "Безлимит" if plan_type == "unlimited" else "Стандарт"
                text, _ = _format_user_info(db, tg_id)
                bot.send_message(
                    message.chat.id,
                    text + f"\n\n_Subscription granted: {plan_label}, до {format_msk(expires_at)}_",
                    reply_markup=_manage_user_keyboard(tg_id),
                    parse_mode='Markdown',
                )
        except Exception as e:
            logger.error(f"Error granting subscription: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}", parse_mode="")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_grantsub_cancel_'))
    def handle_mu_grantsub_cancel(call: CallbackQuery):
        """Cancel grant subscription replacement."""
        if not is_admin(call.from_user.id):
            return
        bot.answer_callback_query(call.id)
        tg_id = int(call.data.replace('mu_grantsub_cancel_', ''))
        _manage_user_state.pop(call.message.chat.id, None)
        bot.edit_message_text("Отменено.", call.message.chat.id, call.message.id)

    # ── Sub link callback ────────────────────────────────────
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_sublink_'))
    def handle_mu_sublink(call: CallbackQuery):
        """Show the user's subscription URL."""
        if not is_admin(call.from_user.id):
            return
        bot.answer_callback_query(call.id)
        tg_id = int(call.data.replace('mu_sublink_', ''))
        try:
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == tg_id).first()
                if not user:
                    bot.send_message(call.message.chat.id, "User not found")
                    return
                sub = db.query(Subscription).filter(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,
                ).order_by(Subscription.expires_at.desc()).first()
                if not sub:
                    bot.send_message(call.message.chat.id, "No active subscription")
                    return
                from config.settings import SUBSCRIPTION_BASE_URL
                base = SUBSCRIPTION_BASE_URL.rstrip('/')
                sub_url = f"{base}/sub/{sub.token}"
                bot.send_message(
                    call.message.chat.id,
                    f"`{sub_url}`",
                    parse_mode='Markdown',
                )
        except Exception as e:
            logger.error(f"Error in mu_sublink: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}", parse_mode="")

    # ── Reset whitelist traffic callback ──────────────────────
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mu_resetwl_'))
    def handle_mu_resetwl(call: CallbackQuery):
        """Reset whitelist traffic consumption to 0 for a user."""
        if not is_admin(call.from_user.id):
            return
        bot.answer_callback_query(call.id)
        tg_id = int(call.data.replace('mu_resetwl_', ''))
        try:
            with get_db_session() as db:
                user = db.query(User).filter(User.telegram_id == tg_id).first()
                if not user:
                    bot.send_message(call.message.chat.id, "User not found")
                    return
                from services.traffic_limit_service import reset_user_traffic
                from config.settings import WHITELIST_GROUP_NAME
                count = reset_user_traffic(db, user.id, WHITELIST_GROUP_NAME)
                bot.send_message(
                    call.message.chat.id,
                    f"Whitelist traffic reset. Keys updated: {count}",
                )
        except Exception as e:
            logger.error(f"Error in mu_resetwl: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}", parse_mode="")

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
                        client = KeyService._make_xui_client(db, key.server, key)
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

    # ── /backup ───────────────────────────────────────────────
    @bot.message_handler(commands=['backup'])
    def handle_backup(message: Message):
        """Send database backup file."""
        if not is_admin(message.from_user.id):
            return

        try:
            from main import send_db_backup
            send_db_backup(bot, message.chat.id)
        except Exception as e:
            logger.error(f"Error sending backup: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Backup error: {e}")

    # ── /monitor_status ──────────────────────────────────────
    @bot.message_handler(commands=['monitor_status'])
    def handle_monitor_status(message: Message):
        """Show current server monitoring state."""
        if not is_admin(message.from_user.id):
            return

        try:
            state_file = Path(__file__).parent.parent.parent / "data" / "monitor_state.json"
            state = {}
            if state_file.exists():
                raw = json.loads(state_file.read_text(encoding="utf-8"))
                state = {int(k): v for k, v in raw.items()}

            with get_db_session() as db:
                servers = db.query(Server).filter(
                    Server.is_active == True,
                    Server.protocol == 'xui',
                ).order_by(Server.name).all()
                # Extract data inside session to avoid DetachedInstanceError
                server_info = [(srv.id, srv.name) for srv in servers]

            if not server_info:
                bot.send_message(message.chat.id, "No active servers.")
                return

            now = datetime.utcnow()
            lines = ["<b>Monitor Status</b>\n"]

            for sid, sname in server_info:
                s = state.get(sid, {})
                is_up = s.get("is_up", True)
                status_icon = "OK" if is_up else "DOWN"
                failures = s.get("consecutive_failures", 0)

                last_up_raw = s.get("last_seen_up")
                last_up_str = ""
                if last_up_raw:
                    try:
                        delta = now - datetime.fromisoformat(last_up_raw)
                        mins = int(delta.total_seconds() / 60)
                        if mins < 60:
                            last_up_str = f" ({mins}m ago)"
                        else:
                            last_up_str = f" ({mins // 60}h {mins % 60}m ago)"
                    except Exception:
                        pass

                traffic_str = ""
                traffic_bytes = s.get("last_traffic_bytes")
                if traffic_bytes is not None:
                    traffic_gb = traffic_bytes / (1024 ** 3)
                    traffic_str = f" | {traffic_gb:.1f} GB"

                stale_str = ""
                stale_raw = s.get("stale_traffic_alerted_at")
                if stale_raw:
                    try:
                        delta = now - datetime.fromisoformat(stale_raw)
                        mins = int(delta.total_seconds() / 60)
                        stale_str = f" | stale {mins}m ago"
                    except Exception:
                        pass

                mute_str = ""
                muted_until_raw = s.get("muted_until")
                if muted_until_raw:
                    try:
                        muted_until = datetime.fromisoformat(muted_until_raw)
                        if muted_until > now:
                            mins_left = int((muted_until - now).total_seconds() / 60)
                            mute_str = f" | 🔇 {mins_left}m"
                    except Exception:
                        pass

                fail_str = f" | fails: {failures}" if failures > 0 else ""
                lines.append(
                    f"<b>{sname}</b>: <code>{status_icon}</code>"
                    f"{last_up_str}{fail_str}{traffic_str}{stale_str}{mute_str}"
                )

            bot.send_message(message.chat.id, "\n".join(lines), parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error in /monitor_status: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    # ── Mute server alerts ────────────────────────────────────
    @bot.callback_query_handler(func=lambda call: call.data.startswith('mute_srv_'))
    def handle_mute_server(call: CallbackQuery):
        """Mute monitoring alerts for a server for 6 hours."""
        if not is_admin(call.from_user.id):
            return

        server_id = int(call.data.replace('mute_srv_', ''))
        bot.answer_callback_query(call.id, "Заглушено на 6 часов")

        try:
            from main import mute_server_alerts
            mute_server_alerts(server_id, hours=6)

            with get_db_session() as db:
                server = db.query(Server).filter(Server.id == server_id).first()
                sname = server.name if server else f"#{server_id}"

            # Edit the alert message to show it's muted
            original_text = call.message.html_text or call.message.text or ""
            bot.edit_message_text(
                original_text + f"\n\n<i>Оповещения заглушены на 6 часов.</i>",
                call.message.chat.id,
                call.message.id,
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error(f"Error muting server {server_id}: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}")

    # ── /sub_graph ─────────────────────────────────────────────

    def _build_sub_graph(period: str) -> "io.BytesIO":
        """Build subscription graph image for a given period.

        period: 'all', 'all_weekly', '90d', '30d'
        """
        import io
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.ticker import MaxNLocator

        launch_date = datetime(2026, 2, 20).date()
        MSK_OFFSET = timedelta(hours=3)
        today = (datetime.utcnow() + MSK_OFFSET).date()

        if period == '30d':
            start_date = today - timedelta(days=30)
        elif period == '90d':
            start_date = today - timedelta(days=90)
        else:
            start_date = launch_date

        with get_db_session() as db:
            subs = db.query(
                Subscription.created_at,
                Subscription.expires_at,
                Subscription.is_test,
                Subscription.user_id,
                Subscription.plan_type,
            ).all()
            users = db.query(User.created_at).all()

        first_paid_date: dict[int, datetime] = {}
        for created_at, expires_at, is_test, user_id, plan_type in subs:
            if not created_at or is_test or (plan_type or 'basic') == 'free':
                continue
            prev = first_paid_date.get(user_id)
            if prev is None or created_at < prev:
                first_paid_date[user_id] = created_at

        dates = []
        active_series = []
        paid_series = []
        test_series = []
        invite_series = []
        test_invite_series = []
        new_users_series = []
        new_paid_series = []

        d = start_date
        while d <= today:
            dt_start = datetime(d.year, d.month, d.day, 0, 0, 0) - MSK_OFFSET
            dt_end   = datetime(d.year, d.month, d.day, 23, 59, 59) - MSK_OFFSET

            active = paid = test = invite = 0
            for created_at, expires_at, is_test, user_id, plan_type in subs:
                if not created_at or not expires_at:
                    continue
                if created_at <= dt_end and expires_at > dt_start:
                    active += 1
                    if is_test:
                        test += 1
                    elif (plan_type or 'basic') == 'free' and created_at >= datetime(2026, 3, 21) - MSK_OFFSET:
                        invite += 1
                    else:
                        paid += 1

            new_users = sum(1 for (ca,) in users if ca and dt_start <= ca <= dt_end)
            new_paid = sum(1 for fp in first_paid_date.values() if dt_start <= fp <= dt_end)

            dates.append(d)
            active_series.append(active)
            paid_series.append(paid)
            test_series.append(test)
            invite_series.append(invite)
            test_invite_series.append(test + invite)
            new_users_series.append(new_users)
            new_paid_series.append(new_paid)
            d += timedelta(days=1)

        # Aggregate weekly if requested
        if period == 'all_weekly' and len(dates) > 7:
            from itertools import zip_longest
            w_dates, w_active, w_paid, w_test, w_invite, w_ti = [], [], [], [], [], []
            w_new_users, w_new_paid = [], []
            i = 0
            while i < len(dates):
                end = min(i + 7, len(dates))
                w_dates.append(dates[i])
                w_active.append(active_series[end - 1])
                w_paid.append(paid_series[end - 1])
                w_test.append(test_series[end - 1])
                w_invite.append(invite_series[end - 1])
                w_ti.append(test_invite_series[end - 1])
                w_new_users.append(sum(new_users_series[i:end]))
                w_new_paid.append(sum(new_paid_series[i:end]))
                i = end
            dates, active_series, paid_series = w_dates, w_active, w_paid
            test_series, invite_series, test_invite_series = w_test, w_invite, w_ti
            new_users_series, new_paid_series = w_new_users, w_new_paid

        # Plot
        bar_width = 5.0 if period == 'all_weekly' else 0.8
        bar_width_narrow = 2.5 if period == 'all_weekly' else 0.4
        fig, ax1 = plt.subplots(figsize=(12, 7))

        ax1.plot(dates, active_series, color='#2196F3', linewidth=2, label='Активные подписки')
        ax1.plot(dates, paid_series, color='#4CAF50', linewidth=2, label='Платные подписки')
        ax1.set_ylabel('Активные / Платные', color='#2196F3')
        ax1.tick_params(axis='y', labelcolor='#2196F3')

        ax2 = ax1.twinx()
        ax2.plot(dates, test_series, color='#FF9800', linewidth=1.5, linestyle='--', label='Тестовые подписки')
        ax2.plot(dates, invite_series, color='#00BCD4', linewidth=1.5, linestyle='--', label='Инвайт-подписки')
        ax2.plot(dates, test_invite_series, color='#7C4DFF', linewidth=1.5, linestyle=':', label='Тест + Инвайт')
        new_label = 'Новые юзеры/нед' if period == 'all_weekly' else 'Новые юзеры/день'
        paid_label = 'Новые платные/нед' if period == 'all_weekly' else 'Новые платные/день'
        ax2.bar(dates, new_users_series, color='#9C27B0', alpha=0.3, width=bar_width, label=new_label)
        ax2.bar(dates, new_paid_series, color='#E91E63', alpha=0.5, width=bar_width_narrow, label=paid_label)
        ax2.set_ylabel('Тестовые / Инвайты / Новые', color='#FF9800')
        ax2.tick_params(axis='y', labelcolor='#FF9800')

        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
        ax1.xaxis.set_major_locator(MaxNLocator(nbins=10, integer=True))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')

        titles = {
            'all': 'Подписки с 20.02',
            'all_weekly': 'Подписки с 20.02 (по неделям)',
            '90d': 'Подписки за 90 дней',
            '30d': 'Подписки за 30 дней',
        }
        ax1.set_title(titles.get(period, 'Подписки'))
        ax1.grid(True, alpha=0.3)

        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=9)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=120)
        buf.seek(0)
        buf.name = 'sub_graph.png'
        plt.close(fig)
        return buf

    @bot.message_handler(commands=['sub_graph'])
    def handle_sub_graph(message: Message):
        """Show period selection buttons for subscription graph."""
        if not is_admin(message.from_user.id):
            return
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("За всё время", callback_data="subgraph_all"),
            InlineKeyboardButton("За всё время (по неделям)", callback_data="subgraph_all_weekly"),
        )
        markup.row(
            InlineKeyboardButton("За 90 дней", callback_data="subgraph_90d"),
            InlineKeyboardButton("За 30 дней", callback_data="subgraph_30d"),
        )
        bot.send_message(message.chat.id, "Выберите период:", reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('subgraph_'))
    def handle_sub_graph_callback(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        period = call.data.replace('subgraph_', '')  # all, all_weekly, 90d, 30d
        bot.answer_callback_query(call.id, "Генерирую график...")
        try:
            buf = _build_sub_graph(period)
            bot.send_photo(call.message.chat.id, buf)
        except Exception as e:
            logger.error(f"Error in /sub_graph ({period}): {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"Error: {e}")

    @bot.message_handler(commands=['invite_stat'])
    def handle_invite_stat(message: Message):
        """Show top users by invite activity (created + used)."""
        if not is_admin(message.from_user.id):
            return

        # Parse optional N argument
        limit = 25
        parts = message.text.strip().split()
        if len(parts) > 1:
            try:
                limit = max(1, int(parts[1]))
            except ValueError:
                bot.send_message(message.chat.id, "Использование: /invite_stat [N]")
                return

        try:
            with get_db_session() as db:
                rows = db.query(
                    User.id,
                    User.telegram_id,
                    User.username,
                    func.count(ReferralInvite.id).label("created"),
                    func.sum(
                        func.cast(ReferralInvite.activated_at.isnot(None), Integer)
                    ).label("used"),
                ).outerjoin(
                    ReferralInvite, ReferralInvite.inviter_id == User.id
                ).group_by(User.id).having(
                    func.count(ReferralInvite.id) > 0
                ).order_by(
                    func.count(ReferralInvite.id).desc(),
                    func.sum(
                        func.cast(ReferralInvite.activated_at.isnot(None), Integer)
                    ).desc(),
                ).limit(limit).all()

            if not rows:
                bot.send_message(message.chat.id, "Инвайты ещё не создавались.", parse_mode="")
                return

            lines = [f"Топ {min(limit, len(rows))} по инвайтам:\n"]
            for i, row in enumerate(rows, 1):
                name = f"@{row.username}" if row.username else f"id{row.telegram_id}"
                used = row.used or 0
                lines.append(f"{i}. {name} — создано: {row.created}, использовано: {used}")

            bot.send_message(message.chat.id, "\n".join(lines), parse_mode="")

        except Exception as e:
            logger.error(f"Error in /invite_stat: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}", parse_mode="")

    # ── /release — app release management ───────────────────

    _UPDATES_DIR = Path("/var/www/clavis-updates")
    _BUILDS_DIR = _UPDATES_DIR / "builds"
    _MANIFESTS_DIR = _UPDATES_DIR / "manifests"
    _ACTIVE_MANIFEST = _UPDATES_DIR / "windows.json"
    _UPDATES_DOMAIN = "cl23.clavisdashboard.ru"

    def _load_builds() -> list:
        """Load all build manifests from manifests/ directory."""
        builds = []
        if not _MANIFESTS_DIR.exists():
            return builds
        for f in sorted(_MANIFESTS_DIR.glob("*.json")):
            try:
                builds.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        return builds

    def _save_build_manifest(version: str, filename: str, notes: str) -> None:
        data = json.dumps({"version": version, "file": filename, "notes": notes})
        (_MANIFESTS_DIR / f"{version}.json").write_text(data)

    def _delete_build_manifest(version: str) -> None:
        p = _MANIFESTS_DIR / f"{version}.json"
        if p.exists():
            p.unlink()

    def _load_current() -> dict | None:
        try:
            return json.loads(_ACTIVE_MANIFEST.read_text()) if _ACTIVE_MANIFEST.exists() else None
        except (json.JSONDecodeError, OSError):
            return None

    def _publish_version(version: str, builds: list) -> None:
        build = next((b for b in builds if b["version"] == version), None)
        if not build:
            raise ValueError(f"Build {version} not found")
        manifest = json.dumps({
            "version": version,
            "url": f"https://{_UPDATES_DOMAIN}/update/builds/{build['file']}",
            "notes": build.get("notes", ""),
        })
        _ACTIVE_MANIFEST.write_text(manifest)

    def _build_file_size(filename: str) -> float | None:
        p = _BUILDS_DIR / filename
        return p.stat().st_size / (1024 * 1024) if p.exists() else None

    def _send_release_menu(chat_id: int, text_prefix: str = "") -> None:
        """Send the main release management menu."""
        current = _load_current()
        builds = _load_builds()

        cur_ver = current.get("version", "—") if current else "—"
        lines = [text_prefix] if text_prefix else []
        lines.append("*Release Management*\n")
        lines.append(f"Опубликованная версия: `{cur_ver}`")

        if builds:
            lines.append(f"\nБилды ({len(builds)}):")
            for b in builds:
                marker = " ✅" if current and b["version"] == cur_ver else ""
                size = _build_file_size(b["file"])
                size_str = f" ({size:.1f} MB)" if size else " (файл не найден!)"
                notes_str = f' — {b["notes"]}' if b.get("notes") else ""
                lines.append(f"  `{b['version']}`{size_str}{notes_str}{marker}")
        else:
            lines.append("\nНет подготовленных билдов.")

        markup = InlineKeyboardMarkup()
        for b in builds:
            is_current = current and b["version"] == cur_ver
            row = []
            if not is_current:
                row.append(InlineKeyboardButton(
                    f"📢 Опубликовать {b['version']}",
                    callback_data=f"rel_pub_{b['version']}",
                ))
            row.append(InlineKeyboardButton(
                f"🗑 {b['version']}",
                callback_data=f"rel_del_{b['version']}",
            ))
            markup.row(*row)
        markup.row(InlineKeyboardButton("➕ Подготовить билд", callback_data="rel_prepare"))

        bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown", reply_markup=markup)

    @bot.message_handler(commands=['release'])
    def handle_release(message: Message):
        """Open release management menu."""
        logger.info(f"/release from {message.from_user.id}, is_admin={is_admin(message.from_user.id)}")
        if not is_admin(message.from_user.id):
            return
        try:
            _send_release_menu(message.chat.id)
        except Exception as e:
            logger.error(f"Error in /release: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("rel_pub_"))
    def handle_release_publish(call: CallbackQuery):
        """Publish a build as the current version."""
        if not is_admin(call.from_user.id):
            return
        version = call.data[len("rel_pub_"):]
        try:
            builds = _load_builds()
            _publish_version(version, builds)
            bot.answer_callback_query(call.id, f"Версия {version} опубликована!")
            bot.delete_message(call.message.chat.id, call.message.message_id)
            _send_release_menu(call.message.chat.id, f"✅ Версия `{version}` опубликована!\n")
            logger.info(f"Release {version} published by {call.from_user.id}")
        except Exception as e:
            bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("rel_del_"))
    def handle_release_delete(call: CallbackQuery):
        """Delete a build (manifest entry + file)."""
        if not is_admin(call.from_user.id):
            return
        version = call.data[len("rel_del_"):]
        try:
            current = _load_current()
            if current and current.get("version") == version:
                bot.answer_callback_query(
                    call.id,
                    "Нельзя удалить опубликованную версию. Сначала опубликуйте другую.",
                    show_alert=True,
                )
                return

            builds = _load_builds()
            build = next((b for b in builds if b["version"] == version), None)
            if build:
                exe = _BUILDS_DIR / build["file"]
                if exe.exists():
                    exe.unlink()
                _delete_build_manifest(version)

            bot.answer_callback_query(call.id, f"Билд {version} удалён")
            bot.delete_message(call.message.chat.id, call.message.message_id)
            _send_release_menu(call.message.chat.id, f"🗑 Билд `{version}` удалён.\n")
            logger.info(f"Build {version} deleted by {call.from_user.id}")
        except Exception as e:
            bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)

    _release_prepare_state: dict[int, dict] = {}

    @bot.callback_query_handler(func=lambda c: c.data == "rel_prepare")
    def handle_release_prepare(call: CallbackQuery):
        """Start the build preparation flow."""
        if not is_admin(call.from_user.id):
            return
        bot.answer_callback_query(call.id)

        # List .exe files on server that aren't in builds.json yet
        try:
            builds = _load_builds()
            known_files = {b["file"] for b in builds}

            all_files = [f.name for f in sorted(_BUILDS_DIR.glob("*.exe"))] if _BUILDS_DIR.exists() else []
            new_files = [f for f in all_files if f not in known_files]

            if new_files:
                markup = InlineKeyboardMarkup()
                for f in new_files:
                    size = _build_file_size(f)
                    label = f"{f} ({size:.1f} MB)" if size else f
                    markup.row(InlineKeyboardButton(label, callback_data=f"rel_pickfile_{f}"))
                markup.row(InlineKeyboardButton("✏️ Ввести версию вручную", callback_data="rel_manual"))
                markup.row(InlineKeyboardButton("❌ Отмена", callback_data="rel_cancel"))
                bot.send_message(
                    call.message.chat.id,
                    "Выберите загруженный файл или введите версию вручную:",
                    reply_markup=markup,
                )
            else:
                msg = bot.send_message(
                    call.message.chat.id,
                    "Нет новых `.exe` файлов на сервере.\n\n"
                    "Загрузите установщик в `builds/` через SCP.\n\n"
                    "Или введите версию (файл `clavis-setup-VERSION.exe` должен быть в `builds/`):",
                    parse_mode="Markdown",
                )
                _release_prepare_state[call.message.chat.id] = {"step": "awaiting_version"}
                bot.register_next_step_handler(msg, _process_prepare_version)
        except Exception as e:
            bot.send_message(call.message.chat.id, f"Error: {e}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("rel_pickfile_"))
    def handle_release_pick_file(call: CallbackQuery):
        """User picked an .exe file from the list."""
        if not is_admin(call.from_user.id):
            return
        filename = call.data[len("rel_pickfile_"):]
        bot.answer_callback_query(call.id)

        # Try to extract version from filename: clavis-setup-X.Y.Z.exe
        version = ""
        if filename.startswith("clavis-setup-") and filename.endswith(".exe"):
            version = filename[len("clavis-setup-"):-len(".exe")]

        _release_prepare_state[call.message.chat.id] = {
            "step": "awaiting_notes",
            "file": filename,
            "version": version,
        }
        msg = bot.send_message(
            call.message.chat.id,
            f"Файл: `{filename}`\nВерсия: `{version}`\n\n"
            "Введите заметки к релизу (или `-` чтобы пропустить):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, _process_prepare_notes)

    @bot.callback_query_handler(func=lambda c: c.data == "rel_manual")
    def handle_release_manual(call: CallbackQuery):
        """User wants to enter version manually."""
        if not is_admin(call.from_user.id):
            return
        bot.answer_callback_query(call.id)
        _release_prepare_state[call.message.chat.id] = {"step": "awaiting_version"}
        msg = bot.send_message(call.message.chat.id, "Введите версию (X.Y.Z):")
        bot.register_next_step_handler(msg, _process_prepare_version)

    @bot.callback_query_handler(func=lambda c: c.data == "rel_cancel")
    def handle_release_cancel(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        _release_prepare_state.pop(call.message.chat.id, None)
        bot.answer_callback_query(call.id, "Отменено")
        bot.delete_message(call.message.chat.id, call.message.message_id)

    def _process_prepare_version(message: Message):
        """Process manually entered version."""
        chat_id = message.chat.id
        version = message.text.strip() if message.text else ""

        if not version or not all(p.isdigit() for p in version.split(".")):
            bot.send_message(chat_id, "Неверный формат. Введите как `X.Y.Z`:", parse_mode="Markdown")
            bot.register_next_step_handler(message, _process_prepare_version)
            return

        filename = f"clavis-setup-{version}.exe"
        size = _build_file_size(filename)
        if size is None:
            bot.send_message(
                chat_id,
                f"`{filename}` не найден на сервере.\n\n"
                "Загрузите через SCP в `builds/`.",
                parse_mode="Markdown",
            )
            _release_prepare_state.pop(chat_id, None)
            return

        # Check if already in builds
        builds = _load_builds()
        if any(b["version"] == version for b in builds):
            bot.send_message(chat_id, f"Билд `{version}` уже существует.", parse_mode="Markdown")
            _release_prepare_state.pop(chat_id, None)
            return

        _release_prepare_state[chat_id] = {
            "step": "awaiting_notes",
            "file": filename,
            "version": version,
        }
        msg = bot.send_message(
            chat_id,
            f"Файл: `{filename}` ({size:.1f} MB)\nВерсия: `{version}`\n\n"
            "Введите заметки к релизу (или `-` чтобы пропустить):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, _process_prepare_notes)

    def _process_prepare_notes(message: Message):
        """Process release notes and finalize build preparation."""
        chat_id = message.chat.id
        state = _release_prepare_state.pop(chat_id, None)
        if not state:
            return

        notes_text = message.text.strip() if message.text else ""
        if notes_text == "-":
            notes_text = ""

        version = state["version"]
        filename = state["file"]

        try:
            _save_build_manifest(version, filename, notes_text)

            bot.send_message(
                chat_id,
                f"✅ Билд `{version}` подготовлен.\n"
                "Используйте /release чтобы опубликовать.",
                parse_mode="Markdown",
            )
        except Exception as e:
            bot.send_message(chat_id, f"Ошибка: {e}")

    logger.info("Admin handlers registered")
