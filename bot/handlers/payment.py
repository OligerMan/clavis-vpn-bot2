"""Payment flow handlers for Telegram bot using Telegram native payments."""

import json
import logging
import math
import threading
import time
import requests
from sqlalchemy.exc import IntegrityError
from telebot import TeleBot
from telebot.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery

from database import get_db_session
from database.models import User, Transaction
from database.activity_log import log_activity
from services import SubscriptionService, KeyService
from message_templates import Messages
from bot.keyboards.markups import tier_selection_keyboard, unlimited_plans_keyboard, standard_plans_keyboard, payment_method_keyboard, key_actions_keyboard, key_platform_keyboard, payment_help_keyboard, back_button_keyboard
from config.settings import (
    PLANS, ADMIN_IDS, SUBSCRIPTION_BASE_URL, DEVICE_LIMIT,
    TELEGRAM_PAYMENT_TOKEN, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    STARS_ENABLED, format_msk, PLAN_CONVERSION_RATES,
)

logger = logging.getLogger(__name__)

# Shared plan-tier math (single source; also used by the login-merge in account_service).
from services.plan_math import convert_remaining_days as _convert_remaining_days


_webhook_lock = threading.Lock()

# YooKassa API base URL
YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"

# Verification settings
VERIFY_DELAY_SECONDS = 5
VERIFY_RETRIES = 30
VERIFY_RETRY_INTERVAL = 10  # seconds between retries (~5 min total)


def verify_payment_via_yookassa(
    bot: TeleBot,
    transaction_id: int,
    telegram_id: int,
    amount_kopeks: int,
    created_after: str,
) -> None:
    """
    Background task: poll YooKassa API to verify payment succeeded.

    Searches for a recent payment matching telegram_id (in description),
    amount, and created after a given timestamp. When found with
    status 'succeeded', activates subscription.

    Args:
        bot: TeleBot instance
        transaction_id: Our internal transaction ID
        telegram_id: User's Telegram ID (YooKassa stores it in description)
        amount_kopeks: Expected payment amount in kopeks
        created_after: ISO timestamp — only consider payments created after this time
    """
    amount_rub = f"{amount_kopeks / 100:.2f}"
    logger.info(
        f"Starting YooKassa verification for transaction {transaction_id}, "
        f"telegram_id={telegram_id}, amount={amount_rub} RUB, after={created_after}"
    )

    time.sleep(VERIFY_DELAY_SECONDS)

    for attempt in range(1, VERIFY_RETRIES + 1):
        try:
            # Check if transaction was already completed (e.g. by successful_payment handler)
            with get_db_session() as db:
                transaction = db.query(Transaction).filter(
                    Transaction.id == transaction_id
                ).first()
                if transaction and transaction.status == 'completed':
                    logger.info(f"Transaction {transaction_id} already completed, stopping verification")
                    return

            # Query YooKassa for recent payments created after our transaction
            response = requests.get(
                YOOKASSA_API_URL,
                params={
                    "limit": 10,
                    "created_at.gte": created_after,
                },
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            # Find matching payment by telegram_id and amount
            for payment in data.get("items", []):
                if (
                    payment.get("description") == str(telegram_id)
                    and payment.get("amount", {}).get("value") == amount_rub
                ):
                    status = payment.get("status")

                    if status == "succeeded" and payment.get("paid") is True:
                        yookassa_id = payment['id']
                        logger.info(
                            f"YooKassa payment found: {yookassa_id}, "
                            f"transaction {transaction_id}, attempt {attempt}"
                        )

                        # Atomically claim this payment to prevent double activation.
                        # Unique constraint on yookassa_payment_id is the safety net.
                        try:
                            with get_db_session() as db:
                                # Check if already claimed
                                existing = db.query(Transaction).filter(
                                    Transaction.yookassa_payment_id == yookassa_id
                                ).first()
                                if existing:
                                    logger.warning(
                                        f"YooKassa payment {yookassa_id} already claimed by "
                                        f"transaction {existing.id}, skipping transaction {transaction_id}"
                                    )
                                    txn = db.query(Transaction).filter(
                                        Transaction.id == transaction_id
                                    ).first()
                                    if txn and txn.status == 'pending':
                                        txn.fail()
                                    return

                                # Claim it (allow failed→completed transition for
                                # retried payments where a prior attempt was canceled)
                                txn = db.query(Transaction).filter(
                                    Transaction.id == transaction_id
                                ).first()
                                if not txn or txn.status == 'completed':
                                    return
                                txn.yookassa_payment_id = yookassa_id
                        except IntegrityError:
                            # Race condition: another thread claimed it between check and commit
                            logger.warning(
                                f"YooKassa payment {yookassa_id} claimed concurrently, "
                                f"skipping transaction {transaction_id}"
                            )
                            with get_db_session() as db:
                                txn = db.query(Transaction).filter(
                                    Transaction.id == transaction_id
                                ).first()
                                if txn and txn.status == 'pending':
                                    txn.fail()
                            return

                        handle_payment_webhook(bot, transaction_id, 'success')
                        return

                    if status == "canceled":
                        logger.info(
                            f"YooKassa payment canceled: {payment['id']}, "
                            f"transaction {transaction_id}, attempt {attempt}"
                        )
                        handle_payment_webhook(bot, transaction_id, 'failed')
                        return

            logger.info(
                f"YooKassa verification attempt {attempt}/{VERIFY_RETRIES} "
                f"for transaction {transaction_id}: no matching final payment found"
            )

        except Exception as e:
            logger.error(
                f"YooKassa verification error for transaction {transaction_id}, "
                f"attempt {attempt}: {e}", exc_info=True
            )

        if attempt < VERIFY_RETRIES:
            time.sleep(VERIFY_RETRY_INTERVAL)

    # All retries exhausted — payment status unknown
    logger.warning(f"YooKassa verification failed for transaction {transaction_id} after {VERIFY_RETRIES} attempts")

    # Mark as failed so delayed help message won't fire either
    with get_db_session() as db:
        txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if txn and txn.status == 'pending':
            txn.fail()
            db.commit()

    try:
        bot.send_message(
            telegram_id,
            "Не удалось подтвердить оплату. Если деньги были списаны, "
            "свяжитесь с поддержкой — мы всё решим.\n\n/payment",
            reply_markup=payment_help_keyboard(telegram_id),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error notifying user {telegram_id} about failed verification: {e}")


def register_payment_handlers(bot: TeleBot) -> None:
    """Register all payment-related handlers."""

    @bot.message_handler(commands=['payment'])
    def handle_payment(message: Message):
        """Handle /payment command - show tier selection."""
        try:
            bot.send_message(
                message.chat.id,
                Messages.TIER_SELECTION,
                reply_markup=tier_selection_keyboard(),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error in /payment handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.callback_query_handler(func=lambda call: call.data == 'payment')
    def callback_payment(call: CallbackQuery):
        """Handle payment callback - show tier selection (back from plan list)."""
        try:
            bot.edit_message_text(
                Messages.TIER_SELECTION,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=tier_selection_keyboard(),
                parse_mode='Markdown',
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == 'tier_unlimited')
    def handle_tier_unlimited(call: CallbackQuery):
        """Handle unlimited tier selection - show unlimited plans."""
        try:
            bot.edit_message_text(
                Messages.UNLIMITED_PLANS,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=unlimited_plans_keyboard(),
                parse_mode='Markdown',
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in tier_unlimited callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'tier_standard')
    def handle_tier_standard(call: CallbackQuery):
        """Handle standard tier selection - show standard plans."""
        try:
            bot.edit_message_text(
                Messages.STANDARD_PLANS,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=standard_plans_keyboard(),
                parse_mode='Markdown',
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in tier_standard callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data in [
        'choose_plan_90', 'choose_plan_365',
        'choose_plan_unlimited_30', 'choose_plan_unlimited_90',
    ])
    def handle_plan_choice(call: CallbackQuery):
        """Handle plan choice — show payment method selection."""
        try:
            plan_map = {
                'choose_plan_90': '90_days',
                'choose_plan_365': '365_days',
                'choose_plan_unlimited_30': 'unlimited_30_days',
                'choose_plan_unlimited_90': 'unlimited_90_days',
            }
            plan_key = plan_map[call.data]
            plan = PLANS[plan_key]
            text = (
                f"*{plan['description']}* — {plan['days']} дней\n\n"
                f"Выберите способ оплаты:"
            )
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=payment_method_keyboard(plan_key),
                parse_mode='Markdown',
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in plan choice callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data in [
        'card_90', 'card_365', 'card_unlimited_30', 'card_unlimited_90',
    ])
    def handle_card_plan(call: CallbackQuery):
        """Handle card payment — send RUB invoice via YooKassa."""
        try:
            card_plan_map = {
                'card_90': '90_days',
                'card_365': '365_days',
                'card_unlimited_30': 'unlimited_30_days',
                'card_unlimited_90': 'unlimited_90_days',
            }
            plan_key = card_plan_map[call.data]
            plan = PLANS[plan_key]

            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                if not user:
                    bot.answer_callback_query(call.id, "Ошибка: пользователь не найден")
                    return

                transaction = Transaction(
                    user_id=user.id,
                    amount=plan['amount'],
                    status='pending',
                    plan=plan_key,
                    payment_method='card',
                )
                db.add(transaction)
                db.commit()
                db.refresh(transaction)
                transaction_id = transaction.id

                logger.info(f"Created card transaction {transaction_id} for user {user.telegram_id}, plan {plan_key}")

            bot.answer_callback_query(call.id)

            price_rub = f"{plan['amount'] / 100:.2f}"

            provider_data = json.dumps({
                "receipt": {
                    "items": [{
                        "description": f"Оплата услуг Clavis на {plan['days']} дней",
                        "quantity": "1.00",
                        "amount": {
                            "value": price_rub,
                            "currency": "RUB"
                        },
                        "vat_code": 1
                    }]
                }
            })

            bot.send_invoice(
                call.message.chat.id,
                "Оплата VPN",
                f"Оплата VPN на {plan['days']} дней",
                f"{plan_key}#{transaction_id}",
                TELEGRAM_PAYMENT_TOKEN,
                "RUB",
                [LabeledPrice(f"{price_rub} рублей", plan['amount'])],
                need_email=True,
                send_email_to_provider=True,
                provider_data=provider_data,
            )

        except Exception as e:
            logger.error(f"Error in card plan callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")
            bot.send_message(call.message.chat.id, Messages.ERROR_GENERIC)

    @bot.callback_query_handler(func=lambda call: call.data in [
        'stars_90', 'stars_365', 'stars_unlimited_30', 'stars_unlimited_90',
    ])
    def handle_stars_plan(call: CallbackQuery):
        """Handle Stars payment — send XTR invoice."""
        try:
            stars_plan_map = {
                'stars_90': '90_days',
                'stars_365': '365_days',
                'stars_unlimited_30': 'unlimited_30_days',
                'stars_unlimited_90': 'unlimited_90_days',
            }
            plan_key = stars_plan_map[call.data]
            plan = PLANS[plan_key]

            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                if not user:
                    bot.answer_callback_query(call.id, "Ошибка: пользователь не найден")
                    return

                transaction = Transaction(
                    user_id=user.id,
                    amount=plan['stars_amount'],
                    status='pending',
                    plan=plan_key,
                    payment_method='stars',
                )
                db.add(transaction)
                db.commit()
                db.refresh(transaction)
                transaction_id = transaction.id

                logger.info(f"Created stars transaction {transaction_id} for user {user.telegram_id}, plan {plan_key}")

            bot.answer_callback_query(call.id)

            bot.send_invoice(
                call.message.chat.id,
                "Оплата VPN",
                f"VPN на {plan['days']} дней",
                f"{plan_key}#{transaction_id}",
                "",  # empty provider_token for Stars
                "XTR",
                [LabeledPrice(f"VPN {plan['description']}", plan['stars_amount'])],
            )

        except Exception as e:
            logger.error(f"Error in stars plan callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")
            bot.send_message(call.message.chat.id, Messages.ERROR_GENERIC)

    @bot.pre_checkout_query_handler(func=lambda query: True)
    def handle_pre_checkout(query: PreCheckoutQuery):
        """
        Handle pre-checkout: validate and confirm.

        For Stars (XTR): just confirm, successful_payment will handle activation.
        For Card (RUB): confirm and start YooKassa background verification.
        """
        try:
            payload = query.invoice_payload
            parts = payload.split('#')
            if len(parts) != 2:
                bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неверные данные платежа")
                return

            plan_key, transaction_id_str = parts

            if plan_key not in PLANS and plan_key != 'donation':
                bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неверный тарифный план")
                return

            try:
                transaction_id = int(transaction_id_str)
            except ValueError:
                bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неверный ID транзакции")
                return

            # Confirm pre-checkout (allow payment to proceed)
            bot.answer_pre_checkout_query(query.id, ok=True)

            # Stars: no background verification needed — successful_payment is definitive
            if query.currency == "XTR":
                logger.info(f"Stars pre-checkout confirmed for transaction {transaction_id}")
                return

            # Card/RUB: start YooKassa background verification
            from datetime import datetime, timezone
            created_after = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

            plan = PLANS.get(plan_key)

            # If the original transaction is no longer pending (e.g. marked failed by a
            # previous canceled payment attempt), create a fresh transaction for this retry.
            active_transaction_id = transaction_id
            with get_db_session() as db:
                txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
                amount_kopeks = plan['amount'] if plan else (txn.amount if txn else 0)
                if not txn or txn.status != 'pending':
                    user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
                    if user:
                        new_txn = Transaction(
                            user_id=user.id,
                            amount=amount_kopeks,
                            status='pending',
                            plan=plan_key,
                            payment_method='card',
                        )
                        db.add(new_txn)
                        db.commit()
                        db.refresh(new_txn)
                        active_transaction_id = new_txn.id
                        logger.info(
                            f"Retry payment: created transaction {active_transaction_id} "
                            f"(original #{transaction_id} status={txn.status if txn else 'missing'})"
                        )

            logger.info(f"Pre-checkout confirmed for transaction {active_transaction_id}, starting YooKassa verification (after {created_after})")

            # Start background verification via YooKassa API
            thread = threading.Thread(
                target=verify_payment_via_yookassa,
                args=(bot, active_transaction_id, query.from_user.id, amount_kopeks, created_after),
                daemon=True,
            )
            thread.start()

        except Exception as e:
            logger.error(f"Error in pre_checkout handler: {e}", exc_info=True)
            bot.answer_pre_checkout_query(query.id, ok=False, error_message="Внутренняя ошибка")

    @bot.message_handler(content_types=['successful_payment'])
    def handle_successful_payment(message: Message):
        """Handle successful_payment — definitive for Stars, belt-and-suspenders for cards."""
        logger.info(">>> successful_payment RECEIVED!")
        try:
            sp = message.successful_payment
            payload = sp.invoice_payload
            parts = payload.split('#')
            if len(parts) != 2:
                return

            _, transaction_id_str = parts
            transaction_id = int(transaction_id_str)

            # Check if already processed by YooKassa verification or failed (retry created a new txn)
            with get_db_session() as db:
                transaction = db.query(Transaction).filter(
                    Transaction.id == transaction_id
                ).first()
                if transaction and transaction.status != 'pending':
                    logger.info(f"Transaction {transaction_id} status is '{transaction.status}', skipping successful_payment")
                    return

                # Save telegram_charge_id for Stars refunds
                if sp.currency == "XTR" and transaction:
                    transaction.telegram_charge_id = sp.telegram_payment_charge_id

            # Process if not yet completed
            handle_payment_webhook(bot, transaction_id, 'success')
            logger.info(
                f"Successful payment processed for transaction {transaction_id} "
                f"via successful_payment handler (currency={sp.currency})"
            )

        except Exception as e:
            logger.error(f"Error in successful_payment handler: {e}", exc_info=True)

    @bot.message_handler(commands=['confirm_payment'])
    def handle_confirm_payment(message: Message):
        """
        Admin command to manually confirm payment.

        Usage: /confirm_payment <transaction_id>
        """
        try:
            # Check if user is admin
            if message.from_user.id not in ADMIN_IDS:
                bot.send_message(message.chat.id, "❌ Нет доступа")
                return

            # Parse transaction ID
            parts = message.text.split()
            if len(parts) != 2:
                bot.send_message(
                    message.chat.id,
                    "Usage: /confirm_payment <transaction_id>"
                )
                return

            try:
                transaction_id = int(parts[1])
            except ValueError:
                bot.send_message(message.chat.id, "Invalid transaction ID")
                return

            # Process payment
            success = handle_payment_webhook(bot, transaction_id, 'success')

            if success:
                bot.send_message(
                    message.chat.id,
                    f"Transaction {transaction_id} confirmed"
                )
            else:
                bot.send_message(
                    message.chat.id,
                    f"Error processing transaction {transaction_id}"
                )

        except Exception as e:
            logger.error(f"Error in /confirm_payment handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    # ── Donation flow ──────────────────────────────────────────

    @bot.callback_query_handler(func=lambda call: call.data == 'donation')
    def handle_donation(call: CallbackQuery):
        try:
            bot.edit_message_text(
                Messages.DONATION_PROMPT,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_button_keyboard(),
                parse_mode='Markdown',
            )
            bot.register_next_step_handler(call.message, process_donation_amount)
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Error in donation callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")

    def process_donation_amount(message: Message):
        text = (message.text or '').strip()
        if not text or text.startswith('/'):
            return

        try:
            amount = int(text)
        except ValueError:
            msg = bot.send_message(
                message.chat.id,
                "Введите целое число.",
                reply_markup=back_button_keyboard(),
            )
            bot.register_next_step_handler(msg, process_donation_amount)
            return

        if amount < 100 or amount > 10000:
            msg = bot.send_message(
                message.chat.id,
                "Сумма должна быть от 100 до 10 000₽.",
                reply_markup=back_button_keyboard(),
            )
            bot.register_next_step_handler(msg, process_donation_amount)
            return

        amount_kopeks = amount * 100
        try:
            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == message.from_user.id
                ).first()
                if not user:
                    bot.send_message(message.chat.id, Messages.ERROR_GENERIC)
                    return

                transaction = Transaction(
                    user_id=user.id,
                    amount=amount_kopeks,
                    status='pending',
                    plan='donation',
                    payment_method='card',
                )
                db.add(transaction)
                db.commit()
                db.refresh(transaction)
                transaction_id = transaction.id

            price_rub = f"{amount:.2f}"
            provider_data = json.dumps({
                "receipt": {
                    "items": [{
                        "description": "Пожертвование Clavis VPN",
                        "quantity": "1.00",
                        "amount": {"value": price_rub, "currency": "RUB"},
                        "vat_code": 1,
                    }]
                }
            })
            bot.send_invoice(
                message.chat.id,
                "Пожертвование",
                f"Пожертвование на {amount}₽",
                f"donation#{transaction_id}",
                TELEGRAM_PAYMENT_TOKEN,
                "RUB",
                [LabeledPrice(f"{amount} рублей", amount_kopeks)],
                need_email=True,
                send_email_to_provider=True,
                provider_data=provider_data,
            )
            logger.info(f"Donation invoice sent: {amount}₽, transaction {transaction_id}, user {message.from_user.id}")
        except Exception as e:
            logger.error(f"Error creating donation invoice: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    logger.info("Payment handlers registered")


def handle_payment_webhook(bot: TeleBot, transaction_id: int, status: str) -> bool:
    """
    Handle payment webhook (or manual confirmation).

    Args:
        bot: TeleBot instance
        transaction_id: Transaction ID
        status: Payment status ('success' or 'failed')

    Returns:
        True if processing succeeded, False otherwise
    """
    _webhook_lock.acquire()
    try:
        with get_db_session() as db:
            # Load transaction
            transaction = db.query(Transaction).filter(
                Transaction.id == transaction_id
            ).first()

            if not transaction:
                logger.error(f"Transaction {transaction_id} not found")
                return False

            # Skip if already completed (idempotency)
            if transaction.status == 'completed':
                logger.info(f"Transaction {transaction_id} already completed, skipping")
                return True

            # Get user
            user = db.query(User).filter(User.id == transaction.user_id).first()

            if not user:
                logger.error(f"User {transaction.user_id} not found for transaction {transaction_id}")
                return False

            if status == 'success':
                # Handle donation separately
                if transaction.plan == 'donation':
                    transaction.complete()
                    user.total_donated = (user.total_donated or 0) + transaction.amount
                    rub = transaction.amount // 100
                    log_activity(db, user.telegram_id, "donation", f"{rub}₽")
                    db.commit()

                    bot.send_message(
                        user.telegram_id,
                        Messages.DONATION_SUCCESS,
                        parse_mode='Markdown',
                    )
                    logger.info(f"Donation {rub}₽ completed for user {user.telegram_id}, transaction {transaction_id}")
                    return True

                # Get plan details
                plan = PLANS.get(transaction.plan)
                if not plan:
                    logger.error(f"Plan {transaction.plan} not found")
                    return False

                days = plan['days']

                # Check if user has active subscription before extending
                existing_sub = SubscriptionService.get_active_subscription(db, user)
                is_upgrade_from_test = existing_sub and existing_sub.is_test
                is_new_subscription = not existing_sub

                # Convert remaining days if plan type is changing
                new_plan_type = plan.get('plan_type', 'basic')
                old_plan_type = (existing_sub.plan_type or 'basic') if existing_sub else None

                if (existing_sub
                        and not existing_sub.is_test
                        and old_plan_type not in (None, 'free')
                        and old_plan_type != new_plan_type):
                    from datetime import datetime, timedelta
                    remaining_seconds = (existing_sub.expires_at - datetime.utcnow()).total_seconds()
                    remaining_days = max(0, remaining_seconds / 86400)
                    if remaining_days > 0:
                        converted = _convert_remaining_days(remaining_days, old_plan_type, new_plan_type)
                        existing_sub.expires_at = datetime.utcnow() + timedelta(days=converted)
                        db.commit()
                        logger.info(
                            f"Converted {remaining_days:.1f}d {old_plan_type} -> {converted}d {new_plan_type} "
                            f"for user {user.telegram_id}"
                        )

                # Create or extend subscription (adds purchased days on top)
                subscription = SubscriptionService.create_or_extend_paid_subscription(
                    db, user, days, transaction_id
                )

                # Set plan type
                if subscription.plan_type != new_plan_type:
                    subscription.plan_type = new_plan_type
                    db.commit()

                # Update whitelist traffic limit BEFORE creating keys
                from services.traffic_limit_service import set_user_limit
                from config.settings import WHITELIST_GROUP_NAME, WHITELIST_TRAFFIC_LIMIT_GB
                if new_plan_type == 'unlimited':
                    set_user_limit(db, user.id, WHITELIST_GROUP_NAME, 0)
                else:
                    set_user_limit(db, user.id, WHITELIST_GROUP_NAME, WHITELIST_TRAFFIC_LIMIT_GB)
                db.commit()

                # Ensure managed keys exist (creates if needed)
                from database.models import Key
                try:
                    KeyService.ensure_keys_exist(db, subscription, user.telegram_id)
                    logger.info(f"Ensured keys for subscription {subscription.id}")
                except ValueError as e:
                    logger.error(f"Error creating keys for transaction {transaction_id}: {e}")
                    bot.send_message(
                        user.telegram_id,
                        Messages.ERROR_KEY_CREATION
                    )
                    return False

                # Update expiry on all managed keys
                managed_keys_count = db.query(Key).filter(
                    Key.subscription_id == subscription.id,
                    Key.server_id.isnot(None),
                    Key.is_active == True,
                ).count()

                if managed_keys_count > 0:
                    try:
                        updated_count = KeyService.update_subscription_keys_expiry(db, subscription)
                        logger.info(f"Updated expiry for {updated_count} keys in subscription {subscription.id}")
                    except ValueError as e:
                        logger.warning(f"Could not update key expiry for transaction {transaction_id}: {e}")

                # Log activity
                plan_desc = plan['description']
                if transaction.payment_method == 'stars':
                    price_str = f"{transaction.amount}⭐"
                else:
                    price_str = f"{transaction.amount // 100}₽"
                if is_new_subscription or is_upgrade_from_test:
                    log_activity(db, user.telegram_id, "payment", f"{plan_desc}, {price_str}")
                else:
                    log_activity(db, user.telegram_id, "sub_extended", f"+{days}д, {price_str}")

                # Mark transaction as completed
                transaction.complete()
                transaction.subscription_id = subscription.id
                db.commit()

                # Invalidate subscription cache after key updates
                from subscription.cache import invalidate_subscription_cache
                if subscription.token:
                    invalidate_subscription_cache(subscription.token)
                    logger.info(f"Invalidated cache for subscription {subscription.id}")

                # Send success message to user with platform selection
                bot.send_message(
                    user.telegram_id,
                    Messages.PAYMENT_SUCCESS.format(
                        plan_description=plan['description'],
                        expiry_date=format_msk(subscription.expires_at),
                    ),
                    reply_markup=key_platform_keyboard(),
                    parse_mode='Markdown'
                )

                logger.info(f"Payment processed successfully for transaction {transaction_id}")
                return True

            elif status == 'failed':
                # Mark transaction as failed
                transaction.fail()
                db.commit()

                # Notify user
                bot.send_message(
                    user.telegram_id,
                    "❌ Платёж не прошёл. Попробуйте снова или обратитесь в поддержку.",
                    reply_markup=payment_help_keyboard(user.telegram_id),
                    parse_mode='Markdown'
                )

                logger.info(f"Payment failed for transaction {transaction_id}")
                return True

            return False

    except Exception as e:
        logger.error(f"Error processing payment webhook for transaction {transaction_id}: {e}", exc_info=True)
        return False
    finally:
        _webhook_lock.release()
