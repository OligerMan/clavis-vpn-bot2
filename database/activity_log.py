"""Activity logging helper."""

from sqlalchemy.orm import Session

from .models import ActivityLog


def log_activity(db: Session, telegram_id: int, action: str, details: str = None):
    """Record a user activity event.

    No separate commit — caller's session will commit.
    """
    db.add(ActivityLog(telegram_id=telegram_id, action=action, details=details))
