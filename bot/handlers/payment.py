"""Payment flow handlers for Telegram bot using Telegram native payments."""

import json
import logging
import threading
import time
import requests
from sqlalchemy.exc import IntegrityError
from telebot import TeleBot
from telebot.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery

from database import get_db_session
from database.models import User, Transaction
from services import SubscriptionService, KeyService
from message_templates import Messages
from bot.keyboards.markups import payment_plans_keyboard, key_actions_keyboard, key_platform_keyboard, payment_help_keyboard
from config.settings import (
    PLANS, ADMIN_IDS, SUBSCRIPTION_BASE_URL, DEVICE_LIMIT,
    TELEGRAM_PAYMENT_TOKEN, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    format_msk,
)

logger = logging.getLogger(__name__)

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

                                # Claim it
                                txn = db.query(Transaction).filter(
                                    Transaction.id == transaction_id
                                ).first()
                                if not txn or txn.status != 'pending':
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
        """Handle /payment command - show payment plans."""
        try:
            bot.send_message(
                message.chat.id,
                Messages.PAYMENT_OPTIONS,
                reply_markup=payment_plans_keyboard(),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error in /payment handler: {e}", exc_info=True)
            bot.send_message(message.chat.id, Messages.ERROR_GENERIC)

    @bot.callback_query_handler(func=lambda call: call.data == 'payment')
    def callback_payment(call: CallbackQuery):
        """Handle payment callback - same as /payment command."""
        handle_payment(call.message)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data in ['plan_90', 'plan_365'])
    def handle_plan_selection(call: CallbackQuery):
        """Handle plan selection — send Telegram native invoice."""
        try:
            plan_key = '90_days' if call.data == 'plan_90' else '365_days'
            plan = PLANS[plan_key]

            with get_db_session() as db:
                user = db.query(User).filter(
                    User.telegram_id == call.from_user.id
                ).first()

                if not user:
                    bot.answer_callback_query(call.id, "Ошибка: пользователь не найден")
                    return

                # Create pending transaction
                transaction = Transaction(
                    user_id=user.id,
                    amount=plan['amount'],
                    status='pending',
                    plan=plan_key
                )
                db.add(transaction)
                db.commit()
                db.refresh(transaction)
                transaction_id = transaction.id

                logger.info(f"Created transaction {transaction_id} for user {user.telegram_id}, plan {plan_key}")

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

            # Send Telegram native invoice
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
                provider_data=provider_data
            )


        except Exception as e:
            logger.error(f"Error in plan selection callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")
            bot.send_message(call.message.chat.id, Messages.ERROR_GENERIC)

    @bot.pre_checkout_query_handler(func=lambda query: True)
    def handle_pre_checkout(query: PreCheckoutQuery):
        """
        Handle pre-checkout: validate and confirm, then verify payment via YooKassa API.

        Does NOT activate subscription here — waits for YooKassa confirmation.
        """
        try:
            payload = query.invoice_payload
            parts = payload.split('#')
            if len(parts) != 2:
                bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неверные данные платежа")
                return

            plan_key, transaction_id_str = parts

            if plan_key not in PLANS:
                bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неверный тарифный план")
                return

            try:
                transaction_id = int(transaction_id_str)
            except ValueError:
                bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неверный ID транзакции")
                return

            # Confirm pre-checkout (allow payment to proceed)
            bot.answer_pre_checkout_query(query.id, ok=True)

            # Record current time — only look for YooKassa payments created after this moment
            from datetime import datetime, timezone
            created_after = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

            logger.info(f"Pre-checkout confirmed for transaction {transaction_id}, starting YooKassa verification (after {created_after})")

            # Start background verification via YooKassa API
            plan = PLANS[plan_key]
            thread = threading.Thread(
                target=verify_payment_via_yookassa,
                args=(bot, transaction_id, query.from_user.id, plan['amount'], created_after),
                daemon=True,
            )
            thread.start()

        except Exception as e:
            logger.error(f"Error in pre_checkout handler: {e}", exc_info=True)
            bot.answer_pre_checkout_query(query.id, ok=False, error_message="Внутренняя ошибка")

    @bot.message_handler(content_types=['successful_payment'])
    def handle_successful_payment(message: Message):
        """Handle successful_payment if it arrives (belt-and-suspenders)."""
        logger.info(">>> successful_payment RECEIVED!")
        try:
            payload = message.successful_payment.invoice_payload
            parts = payload.split('#')
            if len(parts) != 2:
                return

            _, transaction_id_str = parts
            transaction_id = int(transaction_id_str)

            # Check if already processed by YooKassa verification
            with get_db_session() as db:
                transaction = db.query(Transaction).filter(
                    Transaction.id == transaction_id
                ).first()
                if transaction and transaction.status == 'completed':
                    logger.info(f"Transaction {transaction_id} already completed, skipping")
                    return

            # Process if not yet completed
            handle_payment_webhook(bot, transaction_id, 'success')
            logger.info(f"Successful payment processed for transaction {transaction_id} via successful_payment handler")

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

                # Create or extend subscription
                subscription = SubscriptionService.create_or_extend_paid_subscription(
                    db, user, days, transaction_id
                )

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
