"""FastAPI application for subscription server."""

import logging
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from subscription.router import router as subscription_router
from config.settings import SUBSCRIPTION_PORT

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure FastAPI application.

    Returns:
        Configured FastAPI app
    """
    app = FastAPI(
        title="Clavis VPN Subscription Server",
        description="Serves VLESS URIs to v2ray clients (v2rayNG, v2raytun, v2rayN)",
        version="2.0.0",
    )

    # Add CORS middleware (allow all origins for subscription access)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # Register routers
    app.include_router(subscription_router)

    # Health check endpoint
    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "ok", "service": "clavis-subscription-server"}

    # Root endpoint
    @app.get("/")
    async def root():
        """Root endpoint with API info."""
        return {
            "name": "Clavis VPN Subscription Server",
            "version": "2.0.0",
            "endpoints": {
                "subscription": "/sub/{token}",
                "info": "/info/{token}",
                "cache_stats": "/cache/stats",
                "health": "/health",
                "docs": "/docs",
            }
        }

    logger.info("FastAPI application created")
    return app


def start_subscription_server() -> None:
    """Start subscription server (blocking).

    This function is meant to be run in a background thread.
    """
    logger.info(f"Starting subscription server on port {SUBSCRIPTION_PORT}...")

    app = create_app()

    # Run server (blocks until shutdown)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=SUBSCRIPTION_PORT,
        log_level="info",
        access_log=True,
    )
