"""Subscription business logic service."""

from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from database.models import User, Subscription
from config.settings import TEST_SUBSCRIPTION_HOURS, DEVICE_LIMIT
from message_templates import Messages


class SubscriptionService:
    """Service for managing subscription business logic."""

    @staticmethod
    def create_test_subscription(db: Session, user: User) -> Subscription:
        """
        Create a 48-hour test subscription for a user.

        Args:
            db: Database session
            user: User object

        Returns:
            Created Subscription object

        Raises:
            ValueError: If user already has a test subscription
        """
        # Check if user already had a test
        if SubscriptionService.has_test_subscription(db, user):
            raise ValueError("User already has a test subscription")

        # Create test subscription
        expires_at = datetime.utcnow() + timedelta(hours=TEST_SUBSCRIPTION_HOURS)

        subscription = Subscription(
            user_id=user.id,
            is_test=True,
            is_active=True,
            expires_at=expires_at,
            device_limit=DEVICE_LIMIT
        )

        db.add(subscription)
        db.commit()
        db.refresh(subscription)

        return subscription

    @staticmethod
    def has_test_subscription(db: Session, user: User) -> bool:
        """
        Check if user has ever had a test subscription.

        Args:
            db: Database session
            user: User object

        Returns:
            True if user has/had a test subscription
        """
        test_sub = db.query(Subscription).filter(
            Subscription.user_id == user.id,
            Subscription.is_test == True
        ).first()

        return test_sub is not None

    @staticmethod
    def get_active_subscription(db: Session, user: User) -> Optional[Subscription]:
        """
        Get user's active (non-expired) subscription.

        Args:
            db: Database session
            user: User object

        Returns:
            Active Subscription object or None
        """
        subscription = db.query(Subscription).filter(
            Subscription.user_id == user.id,
            Subscription.is_active == True,
            Subscription.expires_at > datetime.utcnow()
        ).first()

        return subscription

    @staticmethod
    def create_or_extend_paid_subscription(
        db: Session,
        user: User,
        days: int,
        transaction_id: int
    ) -> Subscription:
        """
        Create a new paid subscription or extend existing one.

        Logic:
        - If user has active test subscription: upgrade it to paid
        - If user has active paid subscription: extend it
        - If user has expired paid subscription: create new one from now
        - If user has no subscription: create new one

        Args:
            db: Database session
            user: User object
            days: Number of days to add
            transaction_id: Associated transaction ID

        Returns:
            Created or updated Subscription object
        """
        # Check for existing active subscription
        active_sub = SubscriptionService.get_active_subscription(db, user)

        if active_sub:
            # Extend existing subscription
            if active_sub.is_test:
                # Upgrade test to paid
                active_sub.is_test = False

            # Add days to current expiry
            active_sub.expires_at = active_sub.expires_at + timedelta(days=days)
            db.commit()
            db.refresh(active_sub)
            return active_sub

        # Check for expired paid subscription
        expired_sub = db.query(Subscription).filter(
            Subscription.user_id == user.id,
            Subscription.is_test == False,
            Subscription.expires_at <= datetime.utcnow()
        ).first()

        if expired_sub:
            # Extend from now
            expired_sub.is_active = True
            expired_sub.expires_at = datetime.utcnow() + timedelta(days=days)
            db.commit()
            db.refresh(expired_sub)
            return expired_sub

        # Create new paid subscription
        expires_at = datetime.utcnow() + timedelta(days=days)

        subscription = Subscription(
            user_id=user.id,
            is_test=False,
            is_active=True,
            expires_at=expires_at,
            device_limit=DEVICE_LIMIT
        )

        db.add(subscription)
        db.commit()
        db.refresh(subscription)

        return subscription

    @staticmethod
    def get_subscription_url(subscription: Subscription, base_url: str) -> str:
        """
        Generate subscription URL for a subscription.

        Args:
            subscription: Subscription object
            base_url: Base URL (e.g., https://vpn.example.com)

        Returns:
            Full subscription URL
        """
        return subscription.get_subscription_url(base_url)

    @staticmethod
    def get_v2raytun_deeplink(subscription: Subscription, base_url: str) -> str:
        """
        Generate v2raytun one-tap import link.

        This link opens a redirect page that sends the user to v2raytun app
        with the subscription URL pre-filled for automatic import.

        Args:
            subscription: Subscription object
            base_url: Base URL (e.g., https://vpn.example.com)

        Returns:
            Full deep link URL (https://... /v2raytun/{token})
        """
        return f"{base_url.rstrip('/')}/v2raytun/{subscription.token}"

    @staticmethod
    def get_renewal_reminder(subscription: Subscription) -> str:
        """
        Get renewal reminder message based on days until expiry.

        Args:
            subscription: Subscription object

        Returns:
            Reminder message or empty string
        """
        days_left = (subscription.expires_at - datetime.utcnow()).days

        if days_left <= 0:
            return Messages.SUBSCRIPTION_EXPIRED.format(
                expiry_date=subscription.expires_at.strftime('%d.%m.%Y %H:%M')
            )
        elif days_left == 1:
            return Messages.RENEWAL_REMINDER_1.format(
                expiry_date=subscription.expires_at.strftime('%d.%m.%Y %H:%M')
            )
        elif days_left <= 3:
            return Messages.RENEWAL_REMINDER_3.format(
                expiry_date=subscription.expires_at.strftime('%d.%m.%Y %H:%M')
            )
        elif days_left <= 7:
            return Messages.RENEWAL_REMINDER_7.format(
                expiry_date=subscription.expires_at.strftime('%d.%m.%Y %H:%M')
            )

        return ""
