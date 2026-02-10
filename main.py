"""Main entry point for Clavis VPN Bot v2."""

import logging
import sys
import threading
import time

from database import init_db
from bot import register_handlers, start_polling
from subscription import start_subscription_server


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

        # Start subscription server in background thread
        logger.info("Starting subscription server in background thread...")
        subscription_thread = threading.Thread(
            target=start_subscription_server,
            daemon=True,
            name="SubscriptionServer"
        )
        subscription_thread.start()

        # Wait for server to start (give it 2 seconds)
        logger.info("Waiting for subscription server to start...")
        time.sleep(2)

        # Register all handlers
        register_handlers()

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
