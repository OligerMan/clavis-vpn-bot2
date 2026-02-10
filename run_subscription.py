"""Standalone entry point for the subscription server.

Run this to start only the subscription server (without the Telegram bot).
Useful for testing and separate deployment.

Usage:
    python run_subscription.py
"""

import logging
import os
import sys

import uvicorn

from database import init_db
from subscription.app import create_app
from config.settings import SUBSCRIPTION_PORT


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
        ]
    )


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Initializing database...")
    init_db()

    logger.info(f"Starting subscription server on port {SUBSCRIPTION_PORT}...")
    app = create_app()

    ssl_cert = os.path.join(os.path.dirname(__file__), "cert.pem")
    ssl_key = os.path.join(os.path.dirname(__file__), "key.pem")

    ssl_kwargs = {}
    if os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        ssl_kwargs = {"ssl_certfile": ssl_cert, "ssl_keyfile": ssl_key}
        logger.info("HTTPS enabled with self-signed certificate")
    else:
        logger.warning("No cert.pem/key.pem found, running plain HTTP")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=SUBSCRIPTION_PORT,
        log_level="info",
        access_log=True,
        **ssl_kwargs,
    )


if __name__ == '__main__':
    main()
