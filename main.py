"""Main entry point for Clavis VPN Bot v2."""

import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler

from database import init_db, get_db_session
from bot import register_handlers, start_polling, get_bot
from services import NotificationService


def setup_logging():
    """Configure logging for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('clavis_vpn_bot.log', encoding='utf-8')
        ]
    )


def check_subscriptions_job():
    """Periodic job to check subscriptions and send renewal reminders."""
    logger = logging.getLogger(__name__)
    try:
        with get_db_session() as db:
            bot = get_bot()
            sent_counts = NotificationService.check_and_send_reminders(db, bot)
            if sum(sent_counts.values()) > 0:
                logger.info(f"Renewal check completed: {sent_counts}")
    except Exception as e:
        logger.error(f"Error in subscription check job: {e}", exc_info=True)


def main():
    """Main function to start the bot."""
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("=" * 50)
    logger.info("Clavis VPN Bot v2 starting...")
    logger.info("=" * 50)

    try:
        # Initialize database
        logger.info("Initializing database...")
        init_db()

        # Register all handlers
        register_handlers()

        # Start scheduler for renewal reminders
        logger.info("Starting renewal reminder scheduler...")
        scheduler = BackgroundScheduler()
        # Run every hour
        scheduler.add_job(check_subscriptions_job, 'interval', hours=1, id='subscription_check')
        scheduler.start()
        logger.info("Scheduler started (checking subscriptions every hour)")

        # Start polling (blocks main thread)
        start_polling()

    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C)")
        sys.exit(0)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
