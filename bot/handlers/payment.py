"""Payment flow handlers for Telegram bot using Telegram native payments."""

import json
import logging
from telebot import TeleBot
from telebot.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery

from database import get_db_session
from database.models import User, Transaction
from services import SubscriptionService, KeyService
from message_templates import Messages
from bot.keyboards.markups import payment_plans_keyboard, key_actions_keyboard, payment_help_keyboard
from config.settings import PLANS, ADMIN_IDS, SUBSCRIPTION_BASE_URL, DEVICE_LIMIT, TELEGRAM_PAYMENT_TOKEN

logger = logging.getLogger(__name__)


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

            # Send help message after the invoice
            bot.send_message(
                call.message.chat.id,
                Messages.PAYMENT_HELP,
                reply_markup=payment_help_keyboard(call.from_user.id),
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error in plan selection callback: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "Произошла ошибка")
            bot.send_message(call.message.chat.id, Messages.ERROR_GENERIC)

    @bot.pre_checkout_query_handler(func=lambda query: True)
    def handle_pre_checkout(query: PreCheckoutQuery):
        """
        Handle pre-checkout: activate subscription, then confirm.

        Same approach as v1 bot — process payment in pre_checkout because
        YooKassa does not send successful_payment callback via Telegram.
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

            # Activate subscription before answering (same as v1)
            success = handle_payment_webhook(bot, transaction_id, 'success')

            if not success:
                bot.answer_pre_checkout_query(query.id, ok=False, error_message="Ошибка активации подписки")
                return

            # Confirm payment after activation
            bot.answer_pre_checkout_query(query.id, ok=True)
            logger.info(f"Pre-checkout processed and subscription activated for transaction {transaction_id}")

        except Exception as e:
            logger.error(f"Error in pre_checkout handler: {e}", exc_info=True)
            bot.answer_pre_checkout_query(query.id, ok=False, error_message="Внутренняя ошибка")

    @bot.message_handler(content_types=['successful_payment'])
    def handle_successful_payment(message: Message):
        """Handle successful_payment if it arrives (belt-and-suspenders)."""
        try:
            payload = message.successful_payment.invoice_payload
            parts = payload.split('#')
            if len(parts) != 2:
                return

            _, transaction_id_str = parts
            transaction_id = int(transaction_id_str)

            # Check if already processed in pre_checkout
            with get_db_session() as db:
                transaction = db.query(Transaction).filter(
                    Transaction.id == transaction_id
                ).first()
                if transaction and transaction.status == 'completed':
                    logger.info(f"Transaction {transaction_id} already completed in pre_checkout, skipping")
                    return

            # Process if not yet completed (shouldn't normally happen)
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
                    "Использование: /confirm_payment <transaction_id>"
                )
                return

            try:
                transaction_id = int(parts[1])
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный ID транзакции")
                return

            # Process payment
            success = handle_payment_webhook(bot, transaction_id, 'success')

            if success:
                bot.send_message(
                    message.chat.id,
                    f"✅ Транзакция {transaction_id} подтверждена"
                )
            else:
                bot.send_message(
                    message.chat.id,
                    f"❌ Ошибка при обработке транзакции {transaction_id}"
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

                # Generate subscription URL and deep link
                subscription_url = SubscriptionService.get_subscription_url(
                    subscription,
                    SUBSCRIPTION_BASE_URL
                )
                v2raytun_deeplink = SubscriptionService.get_v2raytun_deeplink(
                    subscription,
                    SUBSCRIPTION_BASE_URL
                )

                # Send success message to user
                bot.send_message(
                    user.telegram_id,
                    Messages.PAYMENT_SUCCESS.format(
                        subscription_url=subscription_url,
                        v2raytun_deeplink=v2raytun_deeplink,
                        plan_description=plan['description'],
                        expiry_date=subscription.expires_at.strftime('%d.%m.%Y %H:%M'),
                        device_limit=DEVICE_LIMIT
                    ),
                    reply_markup=key_actions_keyboard(v2raytun_deeplink),
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
                    "❌ Платёж не прошёл. Попробуйте снова или обратитесь в поддержку.\n\n/payment"
                )

                logger.info(f"Payment failed for transaction {transaction_id}")
                return True

            return False

    except Exception as e:
        logger.error(f"Error processing payment webhook for transaction {transaction_id}: {e}", exc_info=True)
        return False
