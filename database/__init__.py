"""Database package for Clavis VPN Bot v2."""

from .models import (
    Base,
    User,
    Subscription,
    Key,
    Server,
    ServerGroup,
    ConnectionProfile,
    ServerInbound,
    UserConfig,
    RoutingList,
    TrafficLog,
    Transaction,
    ActivityLog,
    RefLink,
    RefLinkAccess,
    ReferralInvite,
    WebTrialActivation,
    ClavisAccount,
    Device,
    LoginToken,
    SyncPair,
    AppPayment,
)
from .connection import (
    init_db,
    get_db,
    get_db_session,
    init_test_db,
)
from .activity_log import log_activity

__all__ = [
    # Models
    "Base",
    "User",
    "Subscription",
    "Key",
    "Server",
    "ServerGroup",
    "ConnectionProfile",
    "ServerInbound",
    "UserConfig",
    "RoutingList",
    "TrafficLog",
    "Transaction",
    "ActivityLog",
    "RefLink",
    "RefLinkAccess",
    "ReferralInvite",
    "WebTrialActivation",
    "ClavisAccount",
    "Device",
    "LoginToken",
    "SyncPair",
    "AppPayment",
    # Connection
    "init_db",
    "get_db",
    "get_db_session",
    "init_test_db",
    # Helpers
    "log_activity",
]
