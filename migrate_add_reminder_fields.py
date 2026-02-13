"""Migration script to add reminder tracking fields to subscriptions table."""

import sqlite3
import sys
from pathlib import Path

# Database path (hardcoded in database/connection.py)
DB_PATH = Path("data/clavis.db")


def migrate():
    """Add reminder tracking fields to subscriptions table."""
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Check if columns already exist
        cursor.execute("PRAGMA table_info(subscriptions)")
        columns = [row[1] for row in cursor.fetchall()]

        fields_to_add = []
        if 'reminder_7d_sent' not in columns:
            fields_to_add.append('reminder_7d_sent')
        if 'reminder_3d_sent' not in columns:
            fields_to_add.append('reminder_3d_sent')
        if 'reminder_1d_sent' not in columns:
            fields_to_add.append('reminder_1d_sent')
        if 'expiry_notified' not in columns:
            fields_to_add.append('expiry_notified')

        if not fields_to_add:
            print("All reminder fields already exist. No migration needed.")
            conn.close()
            return

        print(f"Adding fields: {', '.join(fields_to_add)}")

        # Add missing columns
        for field in fields_to_add:
            cursor.execute(f"ALTER TABLE subscriptions ADD COLUMN {field} BOOLEAN DEFAULT 0")
            print(f"  + Added {field}")

        conn.commit()
        print("Migration completed successfully!")

    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        sys.exit(1)

    finally:
        conn.close()


if __name__ == '__main__':
    migrate()
