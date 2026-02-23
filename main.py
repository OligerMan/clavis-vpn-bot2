"""Main entry point for Clavis VPN Bot v2."""

import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler

from datetime import datetime, timedelta

from database import init_db, get_db_session
from database.models import Transaction
from bot import register_handlers, start_polling, get_bot
from services import NotificationService, KeyService


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


def expire_stale_transactions_job():
    """Mark pending transactions older than 10 minutes as failed."""
    logger = logging.getLogger(__name__)
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        with get_db_session() as db:
            count = db.query(Transaction).filter(
                Transaction.status == 'pending',
                Transaction.created_at < cutoff,
            ).update({Transaction.status: 'failed'})
            if count > 0:
                logger.info(f"Expired {count} stale pending transaction(s)")
    except Exception as e:
        logger.error(f"Error in expire_stale_transactions job: {e}", exc_info=True)


def recalculate_server_scores_job():
    """Recalculate server throughput scores and update preferred servers."""
    logger = logging.getLogger(__name__)
    try:
        with get_db_session() as db:
            chosen = KeyService.recalculate_server_scores(db)
            logger.info(f"Server scores recalculated, chosen: {chosen}")
    except Exception as e:
        logger.error(f"Error in server_scores job: {e}", exc_info=True)


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
        scheduler.add_job(expire_stale_transactions_job, 'interval', minutes=5, id='expire_stale_transactions')
        scheduler.add_job(
            recalculate_server_scores_job, 'interval', hours=12,
            id='server_scores', misfire_grace_time=300,
        )
        # Run once on startup
        recalculate_server_scores_job()
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
