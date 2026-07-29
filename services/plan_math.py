"""Shared plan-tier math: ranking + remaining-time conversion on tier change.

Single source for both the paid-plan-change flow (bot/handlers/payment.py) and the
login-merge flow (services/account_service.py). Rounding is ``math.ceil`` — i.e. always
in the user's favour (they keep the fractional day).
"""
import math

from config.settings import PLAN_CONVERSION_RATES

# Only basic/unlimited carry value in a login-merge: premium is a test-only group with
# 0 live subs, free is ignored by policy. Unknown tiers rank 0 (never win).
PLAN_RANK = {"basic": 1, "unlimited": 2}


def convert_remaining_days(remaining_days: float, from_type: str, to_type: str) -> int:
    """Convert remaining days when moving from one plan tier to another.

    Old plan valued at its cheapest daily rate, new plan priced at its most expensive
    daily rate (anti-abuse). No rate for the pair (same tier, or an unlisted pair) ->
    keep the days as-is. Always rounds UP (ceil), in the user's favour.
    """
    rates = PLAN_CONVERSION_RATES.get((from_type, to_type))
    if not rates:
        return math.ceil(remaining_days)
    value_per_day, cost_per_day = rates
    return math.ceil(remaining_days * value_per_day / cost_per_day)
