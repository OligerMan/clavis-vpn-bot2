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
from typing import Optional

from py3xui import Api, Inbound
from py3xui.inbound import Settings, Sniffing, StreamSettings
from telebot import TeleBot
from telebot.types import Message, CallbackQuery, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton

from database import get_db_session
from database.models import Server, User, Subscription, Key, Transaction
from config.settings import ADMIN_IDS
from services import KeyService

logger = logging.getLogger(__name__)

# Default x-ui credentials
DEFAULT_XUI_USERNAME = "oligerman"
DEFAULT_XUI_PASSWORD = "c7j274yeoq2"
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
    api = Api(api_url, username=DEFAULT_XUI_USERNAME, password=DEFAULT_XUI_PASSWORD, use_tls_verify=True)
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
    settings = Settings()

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
            "`/servers` — list all servers with status and config\n"
            "`/add_server` — add server (dialog: name → domain → auto-setup)\n"
            "`/check_server <id>` — health check (version, uptime, clients)\n"
            "`/toggle_server <id>` — enable/disable server\n"
            "`/delete_server <id>` — delete server (force delete if keys exist)\n"
            "\n*User management:*\n"
            "`/manage_user <tg_id>` — user info, keys, subscription, actions\n"
            "\n*Legacy keys:*\n"
            "`/add_old_keys` — import legacy keys from CSV\n"
            "`/remove_old_keys` — soft-delete all legacy keys\n"
            "\n*Other:*\n"
            "`/check_reminders` — manually run subscription expiry check\n"
            "`/admin_help` — this message",
            parse_mode='Markdown'
        )

    # ── /servers ──────────────────────────────────────────────
    @bot.message_handler(commands=['servers'])
    def handle_servers(message: Message):
        """List all servers with status info."""
        if not is_admin(message.from_user.id):
            return

        try:
            with get_db_session() as db:
                servers = db.query(Server).all()

                if not servers:
                    bot.send_message(message.chat.id, "No servers configured.")
                    return

                lines = ["*Servers:*\n"]
                for s in servers:
                    status = "ON" if s.is_active else "OFF"
                    keys_count = len([k for k in s.keys if k.is_active])

                    creds_info = ""
                    if s.api_credentials:
                        try:
                            creds = json.loads(s.api_credentials)
                            inbound = creds.get("inbound_id", "?")
                            user = creds.get("username", "?")
                            conn = creds.get("connection_settings", {})
                            port = conn.get("port", "?")
                            sni = conn.get("sni", "?")
                            creds_info = (
                                f"  user: `{user}`, inbound: `{inbound}`\n"
                                f"  port: `{port}`, sni: `{sni}`"
                            )
                        except json.JSONDecodeError:
                            creds_info = "  credentials: invalid JSON"

                    lines.append(
                        f"*{s.id}.* `{s.name}` [{status}]\n"
                        f"  host: `{s.host}`\n"
                        f"  api: `{s.api_url}`\n"
                        f"{creds_info}\n"
                        f"  keys: {keys_count}/{s.capacity}\n"
                    )

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
            "*Add Server — Step 1/3*\n\nEnter a short name for this server (e.g. `cl24`):",
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
        """Step 2: Got name, ask for domain."""
        if not is_admin(message.from_user.id):
            return

        name = message.text.strip()
        if not name or len(name) > 50:
            bot.send_message(message.chat.id, "Name must be 1-50 characters. Try again.")
            return

        state = _add_server_state[message.chat.id]
        state["name"] = name
        state["step"] = "domain"

        msg = bot.send_message(
            message.chat.id,
            f"*Add Server — Step 2/3*\n\n"
            f"Name: `{name}`\n\n"
            f"Enter the domain where 3x-ui is running\n"
            f"(e.g. `cl24.clavisdashboard.ru`):",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True)
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
        """Step 3: Got domain, connect to panel, discover inbounds, ask which one."""
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

        # Find VLESS Reality inbounds
        vless_inbounds = []
        for ib in result["inbounds"]:
            if ib.protocol == "vless":
                ss = ib.stream_settings
                if getattr(ss, 'security', '') == 'reality':
                    vless_inbounds.append(ib)

        if not vless_inbounds:
            state["step"] = "no_inbound"

            if result["inbounds"]:
                lines = ["No VLESS Reality inbounds found.\n\nExisting inbounds:"]
                for ib in result["inbounds"]:
                    lines.append(f"  id={ib.id} protocol=`{ib.protocol}` port=`{ib.port}`")
                text = "\n".join(lines)
            else:
                text = "No inbounds found on this panel."

            keyboard = InlineKeyboardMarkup()
            keyboard.row(InlineKeyboardButton("Create inbound", callback_data="create_inbound"))
            keyboard.row(InlineKeyboardButton("Cancel", callback_data="cancel_add_server"))

            bot.send_message(
                message.chat.id,
                f"{text}\n\nCreate a new VLESS Reality inbound?",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            return

        if len(vless_inbounds) == 1:
            # Single inbound — auto-select
            _finish_add_server(bot, message.chat.id, state, vless_inbounds[0])
        else:
            # Multiple inbounds — ask which one
            state["step"] = "pick_inbound"
            state["inbounds"] = {ib.id: ib for ib in vless_inbounds}

            keyboard = InlineKeyboardMarkup()
            for ib in vless_inbounds:
                cfg = _extract_inbound_config(ib)
                label = f"id={ib.id} port={cfg['port']} sni={cfg['sni']} ({cfg['clients_count']} clients)"
                keyboard.row(InlineKeyboardButton(label, callback_data=f"pick_inbound_{ib.id}"))
            keyboard.row(InlineKeyboardButton("Cancel", callback_data="cancel_add_server"))

            bot.send_message(
                message.chat.id,
                "*Add Server — Step 3/3*\n\nMultiple VLESS Reality inbounds found. Pick one:",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('pick_inbound_'))
    def handle_pick_inbound(call: CallbackQuery):
        """Handle inbound selection callback."""
        if not is_admin(call.from_user.id):
            return

        state = _add_server_state.get(call.message.chat.id)
        if not state or state.get("step") != "pick_inbound":
            bot.answer_callback_query(call.id, "Session expired. Run /add_server again.")
            return

        inbound_id = int(call.data.replace('pick_inbound_', ''))
        inbound = state["inbounds"].get(inbound_id)
        if not inbound:
            bot.answer_callback_query(call.id, "Inbound not found")
            return

        bot.answer_callback_query(call.id)
        _finish_add_server(bot, call.message.chat.id, state, inbound)

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
            api = Api(api_url, username=DEFAULT_XUI_USERNAME, password=DEFAULT_XUI_PASSWORD, use_tls_verify=True)
            api.login()

            cfg = _create_vless_reality_inbound(api, remark=state["name"])

            # Save server to DB
            credentials = {
                "username": DEFAULT_XUI_USERNAME,
                "password": DEFAULT_XUI_PASSWORD,
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

            with get_db_session() as db:
                server = Server(
                    name=state["name"],
                    host=state["domain"],
                    protocol="xui",
                    api_url=state["api_url"],
                    api_credentials=json.dumps(credentials),
                    capacity=100,
                    is_active=True,
                )
                db.add(server)
                db.flush()
                server_id = server.id

            bot.send_message(
                call.message.chat.id,
                f"*Server added successfully!*\n\n"
                f"ID: `{server_id}`\n"
                f"Name: `{state['name']}`\n"
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

    def _finish_add_server(bot_instance: TeleBot, chat_id: int, state: dict, inbound):
        """Save server to DB with auto-discovered config."""
        cfg = _extract_inbound_config(inbound)

        credentials = {
            "username": DEFAULT_XUI_USERNAME,
            "password": DEFAULT_XUI_PASSWORD,
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

        try:
            with get_db_session() as db:
                server = Server(
                    name=state["name"],
                    host=state["domain"],
                    protocol="xui",
                    api_url=state["api_url"],
                    api_credentials=json.dumps(credentials),
                    capacity=100,
                    is_active=True,
                )
                db.add(server)
                db.flush()
                server_id = server.id

            bot_instance.send_message(
                chat_id,
                f"*Server added successfully!*\n\n"
                f"ID: `{server_id}`\n"
                f"Name: `{state['name']}`\n"
                f"Domain: `{state['domain']}`\n"
                f"Inbound: `{cfg['inbound_id']}`\n\n"
                f"*Connection settings:*\n"
                f"{_format_inbound_info(cfg)}",
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error saving server: {e}", exc_info=True)
            bot_instance.send_message(chat_id, f"Error saving server: {e}")

        _add_server_state.pop(chat_id, None)

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
            f"*Username:* @{user.username}" if user.username else "*Username:* —",
            f"*Registered:* {user.created_at.strftime('%d.%m.%Y %H:%M')}",
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
            lines.append(f"  Expires: {sub.expires_at.strftime('%d.%m.%Y %H:%M')}")
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
                lines.append(f"  Status: EXPIRED ({any_sub.expires_at.strftime('%d.%m.%Y %H:%M')})")
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
            lines.append(f"  Date: {tx.created_at.strftime('%d.%m.%Y %H:%M')}")

        return "\n".join(lines), user

    def _manage_user_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
        """Build inline keyboard for user management."""
        kb = InlineKeyboardMarkup()
        kb.row(InlineKeyboardButton("Refresh keys", callback_data=f"mu_refresh_{telegram_id}"))
        kb.row(InlineKeyboardButton("Adjust time", callback_data=f"mu_time_{telegram_id}"))
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
                    f"  Old expiry: {old_expiry.strftime('%d.%m.%Y %H:%M')}\n"
                    f"  New expiry: {sub.expires_at.strftime('%d.%m.%Y %H:%M')}",
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error adjusting time: {e}", exc_info=True)
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
            stats = {"users": 0, "outline_keys": 0, "vless_keys": 0, "skipped_dup": 0, "errors": 0}

            with get_db_session() as db:
                for row_num, row in enumerate(reader, 1):
                    row = [c.strip() for c in row]
                    if len(row) < 10:
                        stats["errors"] += 1
                        continue

                    try:
                        telegram_id = int(row[0])
                    except ValueError:
                        stats["errors"] += 1
                        continue

                    # Parse payment_until
                    try:
                        payment_until = int(row[4])
                    except ValueError:
                        payment_until = 0

                    if payment_until > 0:
                        expiry = datetime.utcfromtimestamp(payment_until)
                    else:
                        expiry = datetime.utcnow() + timedelta(days=90)

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
                f"Users with keys: {stats['users']}\n"
                f"Outline keys: {stats['outline_keys']}\n"
                f"VLESS keys: {stats['vless_keys']}\n"
                f"Skipped (duplicate): {stats['skipped_dup']}\n"
                f"Errors (bad rows): {stats['errors']}",
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
