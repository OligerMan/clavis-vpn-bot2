"""Referral link statistics with per-user access control."""

import logging
from datetime import datetime

from sqlalchemy import func
from telebot import TeleBot
from telebot.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config.settings import ADMIN_IDS, format_msk
from database import get_db_session
from database.models import RefLink, RefLinkAccess, User, Transaction

logger = logging.getLogger(__name__)

REAL_PAYMENTS_CUTOFF = datetime(2026, 2, 20)


def _is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def _get_accessible_links(db, telegram_id: int) -> list[tuple[str, str | None]]:
    """Return list of (tag, note) tuples accessible to this user."""
    if _is_admin(telegram_id):
        return db.query(RefLink.tag, RefLink.note).order_by(RefLink.tag).all()
    return (
        db.query(RefLink.tag, RefLink.note)
        .join(RefLinkAccess, RefLink.id == RefLinkAccess.ref_link_id)
        .filter(RefLinkAccess.telegram_id == telegram_id)
        .order_by(RefLink.tag)
        .all()
    )


def _build_stats_text(db, tag: str, note: str | None) -> str:
    """Build statistics text for a referral link."""
    # Registered users (excluding admins)
    registered = db.query(func.count(User.id)).filter(
        User.ref_source == tag,
        ~User.telegram_id.in_(ADMIN_IDS),
    ).scalar() or 0

    # Paid users (unique, since real payments cutoff)
    paid_users = db.query(func.count(func.distinct(Transaction.user_id))).join(
        User, User.id == Transaction.user_id
    ).filter(
        User.ref_source == tag,
        Transaction.status == 'completed',
        Transaction.created_at >= REAL_PAYMENTS_CUTOFF,
    ).scalar() or 0

    # 90-day plan purchases
    plan_90 = db.query(func.count(Transaction.id)).join(
        User, User.id == Transaction.user_id
    ).filter(
        User.ref_source == tag,
        Transaction.status == 'completed',
        Transaction.created_at >= REAL_PAYMENTS_CUTOFF,
        Transaction.plan.like('%90%'),
    ).scalar() or 0

    # 365-day plan purchases
    plan_365 = db.query(func.count(Transaction.id)).join(
        User, User.id == Transaction.user_id
    ).filter(
        User.ref_source == tag,
        Transaction.status == 'completed',
        Transaction.created_at >= REAL_PAYMENTS_CUTOFF,
        Transaction.plan.like('%365%'),
    ).scalar() or 0

    # Total revenue (kopeks → rubles)
    revenue_kopeks = db.query(func.coalesce(func.sum(Transaction.amount), 0)).join(
        User, User.id == Transaction.user_id
    ).filter(
        User.ref_source == tag,
        Transaction.status == 'completed',
        Transaction.created_at >= REAL_PAYMENTS_CUTOFF,
    ).scalar() or 0
    revenue_rub = revenue_kopeks / 100

    # Access list count
    ref_link = db.query(RefLink).filter(RefLink.tag == tag).first()
    access_count = 0
    if ref_link:
        access_count = db.query(func.count(RefLinkAccess.id)).filter(
            RefLinkAccess.ref_link_id == ref_link.id
        ).scalar() or 0

    note_text = note if note else "—"

    fmt_rev = f"{revenue_rub:,.0f}".replace(",", " ")

    lines = [
        f"*Статистика:* `{tag}`\n",
        f"Зарегистрировано: *{registered}*",
        f"Оплатили: *{paid_users}*",
        f"  90 дней: {plan_90}",
        f"  365 дней: {plan_365}",
        f"Выручка: *{fmt_rev}₽*\n",
        f"Заметка: {note_text}",
    ]

    if access_count > 0:
        lines.append(f"Доступ выдан: {access_count} польз.")

    lines.append(f"\n_{format_msk(datetime.utcnow())}_")

    return "\n".join(lines)


def _list_keyboard(tags: list[tuple[str, str | None]]) -> InlineKeyboardMarkup:
    """Keyboard with one button per ref link."""
    kb = InlineKeyboardMarkup(row_width=1)
    for tag, note in tags:
        label = f"{tag}" + (f" — {note[:20]}" if note else "")
        kb.row(InlineKeyboardButton(label, callback_data=f"rl_pick:{tag}"))
    kb.row(InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu"))
    return kb


def _stats_keyboard_admin(tag: str) -> InlineKeyboardMarkup:
    """Admin keyboard for a ref link."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🗑 Удалить", callback_data=f"rl_del:{tag}"),
        InlineKeyboardButton("👤 Дать доступ", callback_data=f"rl_grant:{tag}"),
    )
    kb.row(
        InlineKeyboardButton("📝 Заметка", callback_data=f"rl_note:{tag}"),
        InlineKeyboardButton("🔄 Обновить", callback_data=f"rl_refresh:{tag}"),
    )
    kb.row(InlineKeyboardButton("◀️ Назад", callback_data="rl_back"))
    return kb


def _stats_keyboard_user(tag: str, has_multiple: bool) -> InlineKeyboardMarkup:
    """User keyboard for a ref link."""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.row(InlineKeyboardButton("🔄 Обновить", callback_data=f"rl_refresh:{tag}"))
    if has_multiple:
        kb.row(InlineKeyboardButton("◀️ Назад", callback_data="rl_back"))
    else:
        kb.row(InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu"))
    return kb


def register_ref_links_handlers(bot: TeleBot) -> None:
    """Register /ref_links command and callbacks."""

    _rl_state: dict = {}  # chat_id -> {"action": "grant"|"note", "tag": str}

    @bot.message_handler(commands=['ref_links'])
    def handle_ref_links(message: Message):
        """Show accessible referral links."""
        tid = message.from_user.id
        try:
            with get_db_session() as db:
                links = _get_accessible_links(db, tid)

            if not links:
                bot.send_message(
                    message.chat.id,
                    "У вас нет доступа к реферальным ссылкам.",
                )
                return

            if len(links) == 1:
                tag, note = links[0]
                with get_db_session() as db:
                    text = _build_stats_text(db, tag, note)
                    link_count = len(_get_accessible_links(db, tid))
                if _is_admin(tid):
                    kb = _stats_keyboard_admin(tag)
                else:
                    kb = _stats_keyboard_user(tag, has_multiple=False)
                bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode='Markdown')
            else:
                bot.send_message(
                    message.chat.id,
                    "Выберите реферальную ссылку:",
                    reply_markup=_list_keyboard(links),
                )
        except Exception as e:
            logger.error(f"Error in /ref_links: {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Ошибка: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('rl_pick:'))
    def handle_rl_pick(call: CallbackQuery):
        """Show stats for selected ref link."""
        tid = call.from_user.id
        tag = call.data.split(':', 1)[1]
        bot.answer_callback_query(call.id)

        try:
            with get_db_session() as db:
                # Verify access
                links = _get_accessible_links(db, tid)
                accessible_tags = {t for t, _ in links}
                if tag not in accessible_tags:
                    bot.edit_message_text("Нет доступа.", call.message.chat.id, call.message.id)
                    return

                ref = db.query(RefLink).filter(RefLink.tag == tag).first()
                note = ref.note if ref else None
                text = _build_stats_text(db, tag, note)

            if _is_admin(tid):
                kb = _stats_keyboard_admin(tag)
            else:
                kb = _stats_keyboard_user(tag, has_multiple=len(links) > 1)

            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                 reply_markup=kb, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error in rl_pick: {e}", exc_info=True)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('rl_refresh:'))
    def handle_rl_refresh(call: CallbackQuery):
        """Refresh stats display."""
        tid = call.from_user.id
        tag = call.data.split(':', 1)[1]
        bot.answer_callback_query(call.id, "Обновлено")

        try:
            with get_db_session() as db:
                links = _get_accessible_links(db, tid)
                accessible_tags = {t for t, _ in links}
                if tag not in accessible_tags:
                    return

                ref = db.query(RefLink).filter(RefLink.tag == tag).first()
                note = ref.note if ref else None
                text = _build_stats_text(db, tag, note)

            if _is_admin(tid):
                kb = _stats_keyboard_admin(tag)
            else:
                kb = _stats_keyboard_user(tag, has_multiple=len(links) > 1)

            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                 reply_markup=kb, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error in rl_refresh: {e}", exc_info=True)

    @bot.callback_query_handler(func=lambda call: call.data == 'rl_back')
    def handle_rl_back(call: CallbackQuery):
        """Back to link list."""
        tid = call.from_user.id
        bot.answer_callback_query(call.id)

        try:
            with get_db_session() as db:
                links = _get_accessible_links(db, tid)

            if not links:
                bot.edit_message_text("Нет доступных ссылок.", call.message.chat.id, call.message.id)
                return

            bot.edit_message_text(
                "Выберите реферальную ссылку:",
                call.message.chat.id, call.message.id,
                reply_markup=_list_keyboard(links),
            )
        except Exception as e:
            logger.error(f"Error in rl_back: {e}", exc_info=True)

    # ── Admin actions ──

    @bot.callback_query_handler(func=lambda call: call.data.startswith('rl_del:'))
    def handle_rl_delete(call: CallbackQuery):
        """Show delete confirmation."""
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "Нет доступа")
            return

        tag = call.data.split(':', 1)[1]
        bot.answer_callback_query(call.id)

        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"rl_cdel:{tag}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"rl_pick:{tag}"),
        )
        bot.edit_message_text(
            f"Удалить ссылку `{tag}` и все права доступа к ней?",
            call.message.chat.id, call.message.id,
            reply_markup=kb, parse_mode='Markdown',
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('rl_cdel:'))
    def handle_rl_confirm_delete(call: CallbackQuery):
        """Execute delete."""
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "Нет доступа")
            return

        tag = call.data.split(':', 1)[1]
        bot.answer_callback_query(call.id)

        try:
            with get_db_session() as db:
                ref = db.query(RefLink).filter(RefLink.tag == tag).first()
                if ref:
                    db.delete(ref)

            # Show updated list
            with get_db_session() as db:
                links = _get_accessible_links(db, call.from_user.id)

            if links:
                bot.edit_message_text(
                    f"Ссылка `{tag}` удалена.\n\nВыберите реферальную ссылку:",
                    call.message.chat.id, call.message.id,
                    reply_markup=_list_keyboard(links), parse_mode='Markdown',
                )
            else:
                bot.edit_message_text(
                    f"Ссылка `{tag}` удалена. Больше ссылок нет.",
                    call.message.chat.id, call.message.id, parse_mode='Markdown',
                )
        except Exception as e:
            logger.error(f"Error deleting ref link: {e}", exc_info=True)
            bot.edit_message_text(f"Ошибка: {e}", call.message.chat.id, call.message.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('rl_grant:'))
    def handle_rl_grant(call: CallbackQuery):
        """Start grant access flow."""
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "Нет доступа")
            return

        tag = call.data.split(':', 1)[1]
        bot.answer_callback_query(call.id)

        _rl_state[call.message.chat.id] = {"action": "grant", "tag": tag}
        bot.send_message(
            call.message.chat.id,
            f"Введите Telegram ID пользователя для доступа к `{tag}`:",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True),
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('rl_note:'))
    def handle_rl_note(call: CallbackQuery):
        """Start add note flow."""
        if not _is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "Нет доступа")
            return

        tag = call.data.split(':', 1)[1]
        bot.answer_callback_query(call.id)

        _rl_state[call.message.chat.id] = {"action": "note", "tag": tag}
        bot.send_message(
            call.message.chat.id,
            f"Введите заметку для ссылки `{tag}`:",
            parse_mode='Markdown',
            reply_markup=ForceReply(selective=True),
        )

    @bot.message_handler(
        func=lambda m: (
            m.chat.id in _rl_state
            and m.reply_to_message is not None
        )
    )
    def handle_rl_reply(message: Message):
        """Handle ForceReply responses for grant/note actions."""
        state = _rl_state.pop(message.chat.id, None)
        if not state:
            return
        if not _is_admin(message.from_user.id):
            return

        tag = state["tag"]
        action = state["action"]

        try:
            if action == "grant":
                text = message.text.strip()
                if not text.isdigit():
                    bot.send_message(message.chat.id, "Telegram ID должен быть числом.")
                    return

                target_tid = int(text)
                with get_db_session() as db:
                    ref = db.query(RefLink).filter(RefLink.tag == tag).first()
                    if not ref:
                        bot.send_message(message.chat.id, f"Ссылка `{tag}` не найдена.", parse_mode='Markdown')
                        return

                    existing = db.query(RefLinkAccess).filter(
                        RefLinkAccess.ref_link_id == ref.id,
                        RefLinkAccess.telegram_id == target_tid,
                    ).first()

                    if existing:
                        bot.send_message(
                            message.chat.id,
                            f"Пользователь `{target_tid}` уже имеет доступ к `{tag}`.",
                            parse_mode='Markdown',
                        )
                        return

                    db.add(RefLinkAccess(ref_link_id=ref.id, telegram_id=target_tid))

                bot.send_message(
                    message.chat.id,
                    f"Доступ к `{tag}` выдан пользователю `{target_tid}`.",
                    parse_mode='Markdown',
                )

            elif action == "note":
                note_text = message.text.strip()[:500]
                with get_db_session() as db:
                    ref = db.query(RefLink).filter(RefLink.tag == tag).first()
                    if ref:
                        ref.note = note_text

                bot.send_message(
                    message.chat.id,
                    f"Заметка для `{tag}` обновлена.",
                    parse_mode='Markdown',
                )

        except Exception as e:
            logger.error(f"Error in rl_reply ({action}): {e}", exc_info=True)
            bot.send_message(message.chat.id, f"Ошибка: {e}")
