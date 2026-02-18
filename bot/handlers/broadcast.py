"""Admin broadcast handler — interactive mass-messaging from the bot."""

import io
import logging
import threading
import time
from datetime import datetime

import telebot
from telebot import TeleBot
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config.settings import ADMIN_IDS

logger = logging.getLogger(__name__)

BROADCAST_DELAY = 1.0  # seconds between messages
PROGRESS_INTERVAL = 10  # seconds between progress edits

# State dict keyed by chat_id
_broadcast_state: dict[int, dict] = {}


def _is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def _menu_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Broadcast status", callback_data="bc_status"),
        InlineKeyboardButton("Cancel", callback_data="bc_cancel"),
    )
    return kb


def _preview_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Send test to me", callback_data="bc_test"),
        InlineKeyboardButton("Start broadcast", callback_data="bc_start"),
    )
    kb.add(
        InlineKeyboardButton("Change message", callback_data="bc_change"),
        InlineKeyboardButton("Cancel", callback_data="bc_cancel"),
    )
    return kb


def _menu_button_markup() -> InlineKeyboardMarkup:
    """Button attached to every broadcast message sent to users."""
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Открыть меню", callback_data="back_to_menu"))
    return kb


def _running_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Cancel broadcast", callback_data="bc_cancel_run"))
    return kb


def _send_broadcast_message(bot: TeleBot, chat_id: int, text: str) -> None:
    """Send broadcast message trying Markdown first, fallback to plain text.

    Raises telebot exceptions for non-parse errors (403, 429, etc.)
    so the caller can handle them.
    """
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=_menu_button_markup())
    except Exception as e:
        if "can't parse entities" in str(e).lower():
            # Bot default is parse_mode='Markdown', must explicitly set None
            bot.send_message(chat_id, text, parse_mode="", reply_markup=_menu_button_markup())
        else:
            raise


def _parse_ids_file(content: str) -> set[int]:
    """Parse telegram IDs from text (one per line, comments allowed)."""
    ids = set()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ids.add(int(line.split()[0]))
        except (ValueError, IndexError):
            pass
    return ids


def _generate_file(lines: list[str], prefix: str) -> io.BytesIO:
    """Create an in-memory text file for send_document."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO()
    buf.write("\n".join(lines).encode("utf-8"))
    buf.seek(0)
    buf.name = f"{prefix}_{ts}.txt"
    return buf


def _progress_text(stats: dict) -> str:
    sent = stats.get("sent", 0)
    errors = stats.get("errors", 0)
    blocked = stats.get("blocked", 0)
    total = stats.get("total", 0)
    current = stats.get("current", 0)
    pct = int(current / total * 100) if total else 0
    return (
        f"*Broadcast in progress*\n\n"
        f"Progress: {current}/{total} ({pct}%)\n"
        f"Sent: {sent}\n"
        f"Blocked/not found: {blocked}\n"
        f"Other errors: {errors}"
    )


def _summary_text(stats: dict, cancelled: bool = False) -> str:
    sent = stats.get("sent", 0)
    errors = stats.get("errors", 0)
    blocked = stats.get("blocked", 0)
    total = stats.get("total", 0)
    current = stats.get("current", 0)
    status = "CANCELLED" if cancelled else "COMPLETE"
    return (
        f"*Broadcast {status}*\n\n"
        f"Processed: {current}/{total}\n"
        f"Sent: {sent}\n"
        f"Blocked/not found: {blocked}\n"
        f"Other errors: {errors}\n"
        f"Remaining: {total - current}"
    )


def _run_broadcast(bot: TeleBot, chat_id: int) -> None:
    """Background thread: send messages with throttling and progress updates."""
    state = _broadcast_state.get(chat_id)
    if not state:
        return

    targets = sorted(state["targets"])
    message_text = state["message_text"]
    stats = state["stats"]
    stats["total"] = len(targets)

    last_progress_update = time.monotonic()
    status_msg_id = state.get("status_msg_id")

    for i, tg_id in enumerate(targets):
        if state.get("cancelled"):
            break

        stats["current"] = i + 1

        try:
            _send_broadcast_message(bot, tg_id, message_text)
            stats["sent"] += 1
            state["sent_ids"].append(tg_id)

        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                retry_after = 30
                try:
                    if hasattr(e, "result_json") and e.result_json:
                        retry_after = e.result_json.get("parameters", {}).get(
                            "retry_after", 30
                        )
                except Exception:
                    pass
                logger.warning(f"Broadcast rate-limited, waiting {retry_after + 5}s")
                time.sleep(retry_after + 5)
                # Retry once
                try:
                    _send_broadcast_message(bot, tg_id, message_text)
                    stats["sent"] += 1
                    state["sent_ids"].append(tg_id)
                except Exception as e2:
                    stats["errors"] += 1
                    state["error_ids"].append((tg_id, str(e2)))
            elif e.error_code in (403, 400):
                stats["blocked"] += 1
                state["error_ids"].append((tg_id, e.description))
            else:
                stats["errors"] += 1
                state["error_ids"].append((tg_id, str(e)))

        except Exception as e:
            stats["errors"] += 1
            state["error_ids"].append((tg_id, str(e)))

        # Update progress message every PROGRESS_INTERVAL seconds
        now = time.monotonic()
        if status_msg_id and now - last_progress_update >= PROGRESS_INTERVAL:
            try:
                bot.edit_message_text(
                    _progress_text(stats),
                    chat_id,
                    status_msg_id,
                    parse_mode="Markdown",
                    reply_markup=_running_markup(),
                )
            except Exception:
                pass
            last_progress_update = now

        # Throttle
        if i < len(targets) - 1 and not state.get("cancelled"):
            time.sleep(BROADCAST_DELAY)

    # Done
    cancelled = state.get("cancelled", False)
    state["step"] = "done"

    # Final progress edit
    if status_msg_id:
        try:
            bot.edit_message_text(
                _summary_text(stats, cancelled),
                chat_id,
                status_msg_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # Send result files
    try:
        if state["sent_ids"]:
            f = _generate_file([str(x) for x in state["sent_ids"]], "broadcast_sent")
            bot.send_document(chat_id, f, caption=f"Sent: {len(state['sent_ids'])} users")

        if state["error_ids"]:
            lines = [f"{tid} {reason}" for tid, reason in state["error_ids"]]
            f = _generate_file(lines, "broadcast_errors")
            bot.send_document(chat_id, f, caption=f"Errors: {len(state['error_ids'])} users")

        # Remaining (unsent) targets
        processed = {tid for tid in state["sent_ids"]}
        processed.update(tid for tid, _ in state["error_ids"])
        remaining = [tid for tid in targets if tid not in processed]
        if remaining:
            f = _generate_file([str(x) for x in remaining], "broadcast_remaining")
            bot.send_document(chat_id, f, caption=f"Remaining: {len(remaining)} users (not yet sent)")

    except Exception as e:
        logger.error(f"Error sending broadcast report files: {e}")
        try:
            bot.send_message(chat_id, f"Error generating report files: {e}")
        except Exception:
            pass


def register_broadcast_handlers(bot: TeleBot) -> None:
    """Register broadcast-related handlers."""

    # ── /broadcast command ─────────────────────────────────────
    @bot.message_handler(commands=["broadcast"])
    def handle_broadcast(message: Message):
        if not _is_admin(message.from_user.id):
            return

        chat_id = message.chat.id

        # If a broadcast is currently running, show status instead
        st = _broadcast_state.get(chat_id)
        if st and st.get("step") == "running":
            bot.send_message(
                chat_id,
                _progress_text(st["stats"]),
                parse_mode="Markdown",
                reply_markup=_running_markup(),
            )
            return

        _broadcast_state[chat_id] = {"step": "awaiting_file"}
        bot.send_message(
            chat_id,
            "*Broadcast*\n\nSend a `.txt` file with target telegram IDs (one per line).",
            parse_mode="Markdown",
            reply_markup=_menu_markup(),
        )

    # ── Document handler (target list upload) ──────────────────
    @bot.message_handler(
        content_types=["document"],
        func=lambda m: (
            _broadcast_state.get(m.chat.id, {}).get("step") == "awaiting_file"
            and _is_admin(m.from_user.id)
        ),
    )
    def handle_bc_file_upload(message: Message):
        chat_id = message.chat.id

        try:
            file_info = bot.get_file(message.document.file_id)
            file_bytes = bot.download_file(file_info.file_path)
            content = file_bytes.decode("utf-8-sig")
        except Exception as e:
            bot.send_message(chat_id, f"Failed to read file: {e}")
            return

        ids = _parse_ids_file(content)
        if not ids:
            bot.send_message(chat_id, "No valid IDs found in file. Try again.")
            return

        state = _broadcast_state[chat_id]
        state["targets"] = ids
        state["step"] = "awaiting_message"

        bot.send_message(
            chat_id,
            f"Loaded *{len(ids)}* target IDs.\n\nNow send the message text (Markdown supported).",
            parse_mode="Markdown",
        )

    # ── Message text handler ───────────────────────────────────
    @bot.message_handler(
        func=lambda m: (
            _broadcast_state.get(m.chat.id, {}).get("step") == "awaiting_message"
            and _is_admin(m.from_user.id)
        ),
        content_types=["text"],
    )
    def handle_bc_message_text(message: Message):
        chat_id = message.chat.id
        state = _broadcast_state[chat_id]
        state["message_text"] = message.text
        state["step"] = "ready"
        logger.info(f"Broadcast: message text received from {chat_id}, length={len(message.text)}")

        # Show preview exactly as users will see it (Markdown with plain-text fallback)
        try:
            _send_broadcast_message(bot, chat_id, message.text)
        except Exception as e:
            logger.error(f"Broadcast: failed to send preview: {e}")

        bot.send_message(
            chat_id,
            f"Targets: {len(state['targets'])}\n\nChoose action:",
            reply_markup=_preview_markup(),
        )

    # ── Send test to admin ─────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "bc_test")
    def handle_bc_test(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            return
        chat_id = call.message.chat.id
        state = _broadcast_state.get(chat_id)
        if not state or not state.get("message_text"):
            bot.answer_callback_query(call.id, "No message set.")
            return

        bot.answer_callback_query(call.id, "Sending test...")
        try:
            _send_broadcast_message(bot, chat_id, state["message_text"])
        except Exception as e:
            bot.send_message(chat_id, f"Test send failed: {e}")

    # ── Start broadcast ────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "bc_start")
    def handle_bc_start(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            return
        chat_id = call.message.chat.id
        state = _broadcast_state.get(chat_id)
        if not state or state.get("step") != "ready":
            bot.answer_callback_query(call.id, "Not ready to send.")
            return

        bot.answer_callback_query(call.id)

        # Initialize stats
        state["step"] = "running"
        state["cancelled"] = False
        state["sent_ids"] = []
        state["error_ids"] = []
        state["stats"] = {"sent": 0, "errors": 0, "blocked": 0, "total": 0, "current": 0}

        # Send initial status message
        msg = bot.send_message(
            chat_id,
            _progress_text(state["stats"]),
            parse_mode="Markdown",
            reply_markup=_running_markup(),
        )
        state["status_msg_id"] = msg.message_id

        # Launch background thread
        t = threading.Thread(target=_run_broadcast, args=(bot, chat_id), daemon=True)
        state["thread"] = t
        t.start()

    # ── Change message ─────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "bc_change")
    def handle_bc_change(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            return
        chat_id = call.message.chat.id
        state = _broadcast_state.get(chat_id)
        if not state:
            bot.answer_callback_query(call.id)
            return

        state["step"] = "awaiting_message"
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "Send the new message text.",
            chat_id,
            call.message.message_id,
        )

    # ── Cancel (cleanup or stop running broadcast) ─────────────
    @bot.callback_query_handler(func=lambda c: c.data in ("bc_cancel", "bc_cancel_run"))
    def handle_bc_cancel(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            return
        chat_id = call.message.chat.id
        state = _broadcast_state.get(chat_id)

        bot.answer_callback_query(call.id)

        if state and state.get("step") == "running":
            state["cancelled"] = True
            bot.edit_message_text(
                "Cancelling broadcast... waiting for current message to finish.",
                chat_id,
                call.message.message_id,
            )
        else:
            _broadcast_state.pop(chat_id, None)
            bot.edit_message_text(
                "Broadcast cancelled.",
                chat_id,
                call.message.message_id,
            )

    # ── Broadcast status ───────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "bc_status")
    def handle_bc_status(call: CallbackQuery):
        if not _is_admin(call.from_user.id):
            return
        chat_id = call.message.chat.id
        state = _broadcast_state.get(chat_id)

        bot.answer_callback_query(call.id)

        if not state:
            bot.send_message(chat_id, "No broadcast in progress or completed.")
            return

        if state.get("step") == "running":
            bot.send_message(
                chat_id,
                _progress_text(state["stats"]),
                parse_mode="Markdown",
                reply_markup=_running_markup(),
            )
        elif state.get("step") == "done":
            cancelled = state.get("cancelled", False)
            bot.send_message(
                chat_id,
                _summary_text(state["stats"], cancelled),
                parse_mode="Markdown",
            )
        else:
            bot.send_message(
                chat_id,
                f"Broadcast setup in progress (step: {state.get('step')}).",
            )
