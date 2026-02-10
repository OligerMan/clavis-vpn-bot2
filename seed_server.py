"""Seed the database with the cl23 server record."""

import json
import sys

from database.connection import init_db, get_db_session
from database.models import Server


def seed_server():
    """Insert or update the cl23 server record."""
    init_db()

    credentials = {
        "username": "oligerman",
        "password": "c7j274yeoq2",
        "inbound_id": 2,
        "use_tls_verify": True,
        "connection_settings": {
            "port": 57794,
            "sni": "yahoo.com",
            "pbk": "e6cvHDMwJAZdLYztFeD9tVkijpQA1i4MejVeqTKi0hY",
            "sid": "8801f458a0",
            "flow": "xtls-rprx-vision",
            "fingerprint": "chrome"
        }
    }

    with get_db_session() as db:
        existing = db.query(Server).filter(Server.name == "cl23").first()

        if existing:
            existing.host = "cl23.clavisdashboard.ru"
            existing.protocol = "xui"
            existing.api_url = "https://cl23.clavisdashboard.ru:2053/dashboard/"
            existing.api_credentials = json.dumps(credentials)
            existing.is_active = True
            print(f"Updated server cl23 (id={existing.id})")
        else:
            server = Server(
                name="cl23",
                host="cl23.clavisdashboard.ru",
                protocol="xui",
                api_url="https://cl23.clavisdashboard.ru:2053/dashboard/",
                api_credentials=json.dumps(credentials),
                is_active=True,
            )
            db.add(server)
            db.flush()
            print(f"Created server cl23 (id={server.id})")


if __name__ == "__main__":
    seed_server()
