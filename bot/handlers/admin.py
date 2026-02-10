"""Admin command handlers for Telegram bot."""

import json
import logging
import random
import secrets
import subprocess

from py3xui import Api, Inbound
from py3xui.inbound import Settings, Sniffing, StreamSettings
from telebot import TeleBot
from telebot.types import Message, CallbackQuery, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton

from database import get_db_session
from database.models import Server
from config.settings import ADMIN_IDS

logger = logging.getLogger(__name__)

# Default x-ui credentials
DEFAULT_XUI_USERNAME = "oligerman"
DEFAULT_XUI_PASSWORD = "c7j274yeoq2"
DEFAULT_XUI_PANEL_PORT = 2053
DEFAULT_XUI_BASE_PATH = "/dashboard/"

# Temporary storage for add_server dialog state per chat_id
_add_server_state = {}


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
            "`/servers` — list all servers with status and config\n"
            "`/add_server` — add server (dialog: name → domain → auto-setup)\n"
            "`/check_server <id>` — health check (version, uptime, clients)\n"
            "`/toggle_server <id>` — enable/disable server\n"
            "`/delete_server <id>` — delete server (force delete if keys exist)\n"
            "\n`/admin_help` — this message",
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

    logger.info("Admin handlers registered")
