"""Authoritative policy contract for the autonomous 24-hour trading cycle.

This module contains owner rules, not strategy suggestions.  Runtime code and
tests must import these values instead of copying them into prompts or prose.

The new two-sided strategy is deliberately not activated yet. "Two-sided"
means it may select a LONG in an uptrend or a SHORT in a downtrend; it does not
mean simultaneous opposite positions on one symbol.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

IST = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")

# Owner rules (2026-09-27).
TARGET_SETS_PER_CYCLE = 2
MAX_SETS_PER_CYCLE = 3
AUTONOMOUS_SETS_PER_CYCLE = 3
DAILY_LOSS_LIMIT_INR = 500.0
DAILY_PROFIT_LIMIT_INR = 500.0
REQUIRE_STOP_LOSS = True
REQUIRE_TAKE_PROFIT = True
TELEGRAM_UPDATES_REQUIRED = True

# Safety interpretation: "at least two" is a target, never permission to force
# a low-quality trade. The first three qualified sets are autonomous. Every set
# after the third requires its own current, Telegram-bound owner approval.
FORCE_MINIMUM_TRADES = False
HUMAN_OVERRIDE_ABOVE_MAX = True

# Phase gate.  This becomes True only after the hedge definition, side-aware
# bracket execution, cycle accounting and fake-exchange tests are complete.
AUTONOMOUS_HEDGE_READY = False


@dataclass(frozen=True)
class Bracket:
    side: str
    entry: float
    stop_loss: float
    take_profit: float


@dataclass(frozen=True)
class SetApproval:
    """Owner approval already authenticated and journaled by the Telegram layer."""
    cycle: str
    set_number: int
    expires_at: datetime
    proposal_id: str = ""
    channel: str = "telegram"


def cycle_id(at=None):
    """Calendar-day cycle in Asia/Kolkata, the bot's existing accounting zone."""
    if at is None:
        dt = datetime.now(IST)
    elif isinstance(at, (int, float)):
        dt = datetime.fromtimestamp(at, IST)
    elif at.tzinfo is None:
        dt = at.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        dt = at.astimezone(IST)
    return dt.date().isoformat()


def pnl_stop_reason(pnl_inr):
    """Return the mandatory reason to stop opening sets, or None."""
    try:
        pnl_inr = float(pnl_inr)
    except (TypeError, ValueError):
        return "daily P&L is unavailable"
    if not math.isfinite(pnl_inr):
        return "daily P&L is unavailable"
    if pnl_inr <= -DAILY_LOSS_LIMIT_INR:
        return f"daily loss limit reached (Rs {pnl_inr:,.0f})"
    if pnl_inr >= DAILY_PROFIT_LIMIT_INR:
        return f"daily profit limit reached (Rs {pnl_inr:,.0f})"
    return None


def _valid_extra_set_approval(approval, completed_sets, proposal_id, at=None):
    if not isinstance(approval, SetApproval) or approval.channel != "telegram":
        return False
    now = datetime.now(IST) if at is None else at
    if now.tzinfo is None or approval.expires_at.tzinfo is None:
        return False
    return (approval.cycle == cycle_id(now)
            and approval.set_number == completed_sets + 1
            and bool(proposal_id)
            and approval.proposal_id == proposal_id
            and approval.expires_at > now)


def new_set_block_reason(completed_sets, pnl_inr, approval=None, proposal_id="", at=None):
    """Hard authorization check for opening another set in the current cycle."""
    if completed_sets < 0:
        raise ValueError("completed_sets cannot be negative")
    reason = pnl_stop_reason(pnl_inr)
    if reason:
        return reason
    if completed_sets >= MAX_SETS_PER_CYCLE and not _valid_extra_set_approval(
            approval, completed_sets, proposal_id, at):
        return f"owner approval required above {MAX_SETS_PER_CYCLE} sets"
    return None


def validate_bracket(side, entry, stop_loss, take_profit):
    """Return a normalized bracket or raise before any order can be submitted."""
    side = str(side).upper()
    try:
        entry, stop_loss, take_profit = map(float, (entry, stop_loss, take_profit))
    except (TypeError, ValueError) as e:
        raise ValueError("entry, stop-loss and take-profit must be finite numbers") from e
    if not all(math.isfinite(x) for x in (entry, stop_loss, take_profit)):
        raise ValueError("entry, stop-loss and take-profit must be finite numbers")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if min(entry, stop_loss, take_profit) <= 0:
        raise ValueError("entry, stop-loss and take-profit must be positive")
    valid = stop_loss < entry < take_profit if side == "LONG" else take_profit < entry < stop_loss
    if not valid:
        raise ValueError(f"invalid {side} bracket: stop and target are on the wrong sides of entry")
    return Bracket(side, entry, stop_loss, take_profit)


def migration_block_reason():
    """Block production entries while the new contract is only partially implemented."""
    if AUTONOMOUS_HEDGE_READY:
        return None
    return "autonomous hedge policy migration is incomplete; new entries are disabled"
