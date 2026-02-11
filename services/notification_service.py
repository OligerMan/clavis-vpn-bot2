"""Notification service for subscription renewal reminders."""

import logging
from datetime import datetime, timedelta
from typing import List

from sqlalchemy.orm import Session
from telebot import TeleBot

from database.models import Subscription, User
from message_templates import Messages
from config.settings import SUBSCRIPTION_BASE_URL

logger = logging.getLogger(__name__)


class NotificationService:
    """Service for sending subscription renewal notifications."""

    @staticmethod
    def check_and_send_reminders(db: Session, bot: TeleBot) -> dict:
        """
        Check all active subscriptions and send renewal reminders.

        Args:
            db: Database session
            bot: TeleBot instance for sending messages

        Returns:
            Dictionary with counts of sent notifications
        """
        now = datetime.utcnow()
        sent_counts = {
            '7d': 0,
            '3d': 0,
            '1d': 0,
            'expired': 0
        }

        # Get all active subscriptions
        active_subs = db.query(Subscription).filter(
            Subscription.is_active == True
        ).all()

        for sub in active_subs:
            user = db.query(User).filter(User.id == sub.user_id).first()
            if not user:
                continue

            # Calculate days until expiry
            days_left = (sub.expires_at - now).total_seconds() / 86400

            try:
                # Check if expired
                if days_left <= 0 and not sub.expiry_notified:
                    NotificationService._send_expiry_notification(bot, user, sub)
                    sub.expiry_notified = True
                    sent_counts['expired'] += 1

                # Check 1 day reminder
                elif 0 < days_left <= 1 and not sub.reminder_1d_sent:
                    NotificationService._send_reminder(bot, user, sub, 1)
                    sub.reminder_1d_sent = True
                    sent_counts['1d'] += 1

                # Check 3 day reminder
                elif 1 < days_left <= 3 and not sub.reminder_3d_sent:
                    NotificationService._send_reminder(bot, user, sub, 3)
                    sub.reminder_3d_sent = True
                    sent_counts['3d'] += 1

                # Check 7 day reminder
                elif 3 < days_left <= 7 and not sub.reminder_7d_sent:
                    NotificationService._send_reminder(bot, user, sub, 7)
                    sub.reminder_7d_sent = True
                    sent_counts['7d'] += 1

            except Exception as e:
                logger.error(f"Failed to send notification for subscription {sub.id}: {e}", exc_info=True)
                continue

        # Commit all flag updates
        db.commit()

        logger.info(f"Sent reminders: 7d={sent_counts['7d']}, 3d={sent_counts['3d']}, "
                   f"1d={sent_counts['1d']}, expired={sent_counts['expired']}")

        return sent_counts

    @staticmethod
    def _send_reminder(bot: TeleBot, user: User, subscription: Subscription, days: int) -> None:
        """Send renewal reminder to user."""
        expiry_str = subscription.expires_at.strftime('%d.%m.%Y %H:%M')

        if days == 7:
            message = Messages.RENEWAL_REMINDER_7.format(expiry_date=expiry_str)
        elif days == 3:
            message = Messages.RENEWAL_REMINDER_3.format(expiry_date=expiry_str)
        elif days == 1:
            message = Messages.RENEWAL_REMINDER_1.format(expiry_date=expiry_str)
        else:
            return

        bot.send_message(
            user.telegram_id,
            message,
            parse_mode='Markdown'
        )

        logger.info(f"Sent {days}d reminder to user {user.telegram_id} for subscription {subscription.id}")

    @staticmethod
    def _send_expiry_notification(bot: TeleBot, user: User, subscription: Subscription) -> None:
        """Send subscription expiry notification to user."""
        expiry_str = subscription.expires_at.strftime('%d.%m.%Y %H:%M')

        message = Messages.SUBSCRIPTION_EXPIRED.format(expiry_date=expiry_str)

        bot.send_message(
            user.telegram_id,
            message,
            parse_mode='Markdown'
        )

        logger.info(f"Sent expiry notification to user {user.telegram_id} for subscription {subscription.id}")
