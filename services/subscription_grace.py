"""Shared 'grace-demote' for a subscription.

Kills the subscription's link immediately but keeps its x-ui keys working for a short
window, then lets the reaper (main.reap_bootstrap_subscriptions_job) delete the sub +
its panel clients. Used by admin link-rotation and by the login-merge (the folded
app-side subscription). See server-integration-spec §11 and the rotate flow.
"""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database.models import Subscription

logger = logging.getLogger(__name__)


def grace_demote_subscription(db: Session, sub: Subscription, grace_hours: int = None) -> None:
    """Put ``sub`` into the shared ``rotated-grace`` state:

    - its active x-ui keys get expiry = now + grace_hours (best-effort — panel errors
      are logged, never raised, so a merge/rotation is not rolled back),
    - ``expires_at`` = now + grace_hours, ``is_active`` = False,
    - ``name`` = ROTATED_GRACE_SUB_NAME, token rotated (old ``/sub/<token>`` dies now),
    - ``account_id`` detached (the account is the survivor's / is being abandoned).

    The reaper deletes the row + its x-ui clients once the window passes.
    """
    from config.settings import ROTATED_GRACE_SUB_NAME, ROTATE_GRACE_HOURS
    from services.key_service import KeyService
    from subscription.cache import invalidate_subscription_cache

    if grace_hours is None:
        grace_hours = ROTATE_GRACE_HOURS

    old_token = sub.token
    sub.expires_at = datetime.utcnow() + timedelta(hours=grace_hours)
    db.flush()
    try:
        KeyService.update_subscription_keys_expiry(db, sub)  # push grace expiry to panels
    except Exception as e:
        logger.warning(f"grace_demote: key expiry update failed for sub {sub.id}: {e}")

    sub.is_active = False
    sub.name = ROTATED_GRACE_SUB_NAME
    sub.token = str(uuid.uuid4())  # invalidate the old /sub/<token> link immediately
    sub.account_id = None          # detach from the survivor's / abandoned account
    if old_token:
        invalidate_subscription_cache(old_token)
