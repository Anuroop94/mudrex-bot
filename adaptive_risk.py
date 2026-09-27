"""Pure, deterministic sizing contract for bounded adaptive trade risk.

Leverage is returned as a margin setting only. Quantity is derived exclusively
from the rupee risk budget and stop distance; changing leverage cannot change it.
"""
from dataclasses import dataclass
import math
from typing import Iterable, Optional

import trade_policy


DAILY_RISK_LIMIT_INR = trade_policy.DAILY_LOSS_LIMIT_INR
MAX_CANDIDATE_RISK_INR = 250.0
HARD_MAX_LEVERAGE = 5.0
MIN_REWARD_RISK = 1.5
MIN_CONFIDENCE = 0.55
BASE_COST_RATE = 0.00158  # two taker fees including GST plus entry/exit slippage
DEFAULT_FUNDING_PER_HOUR = 0.0003 / 24.0
EXPECTED_HOLD_HOURS = 24.0


@dataclass(frozen=True)
class VolatilityTier:
    name: str
    stop_atr: float
    leverage_cap: float


@dataclass(frozen=True)
class RiskPlan:
    side: str
    stop_loss: float
    take_profit: float
    volatility: float
    volatility_tier: str
    leverage: float
    quantity: float
    risk_per_unit_inr: float
    planned_stop_risk_inr: float
    available_risk_inr: float


def _finite(name: str, value: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def gross_realized_losses(realized_pnls: Iterable[float]) -> float:
    """Sum losing realized outcomes only; gains never replenish risk capacity."""
    total = 0.0
    for pnl in realized_pnls:
        value = _finite("realized P&L", pnl)
        total += max(0.0, -value)
    return total


def remaining_daily_risk(realized_pnls: Iterable[float], active_stop_risks: Iterable[float],
                         candidate_cost_buffer_inr: float = 0.0,
                         daily_limit_inr: float = DAILY_RISK_LIMIT_INR) -> float:
    """Available modeled loss budget after gross losses, open stops, and candidate costs.

    Invalid/negative inputs fail closed. `active_stop_risks` must contain verified
    stop risks inclusive of any reserved gap/slippage/fee buffer for those trades.
    """
    limit = _finite("daily limit", daily_limit_inr)
    buffer = _finite("candidate cost buffer", candidate_cost_buffer_inr)
    if limit <= 0 or buffer < 0:
        raise ValueError("daily limit must be positive and cost buffer nonnegative")
    active = 0.0
    for risk in active_stop_risks:
        risk = _finite("active stop risk", risk)
        if risk < 0:
            raise ValueError("active stop risk cannot be negative")
        active += risk
    losses = gross_realized_losses(realized_pnls)
    return max(0.0, limit - losses - active - buffer)


def volatility_tier(entry: float, atr: float) -> tuple[float, VolatilityTier]:
    """Classify ATR/price; high but allowed volatility widens the stop, extreme vetoes."""
    entry, atr = _finite("entry", entry), _finite("ATR", atr)
    if entry <= 0 or atr <= 0:
        raise ValueError("entry and ATR must be positive")
    vol = atr / entry
    if vol <= 0.02:
        tier = VolatilityTier("low", 1.5, 5.0)
    elif vol <= 0.05:
        tier = VolatilityTier("moderate", 2.0, 4.0)
    elif vol <= 0.08:
        tier = VolatilityTier("high", 2.5, 2.0)
    else:
        raise ValueError("extreme volatility: candidate vetoed; callers must treat this as SKIP")
    return vol, tier


def _floor_step(value: float, step: float) -> float:
    return math.floor(value / step + 1e-12) * step


def plan_trade(*, side: str, entry: float, atr: float, confidence: float,
               exchange_max_leverage: float, realized_pnls: Iterable[float] = (),
               active_stop_risks: Iterable[float] = (), candidate_cost_buffer_inr: float,
               funding_fee_perc_hour: float = DEFAULT_FUNDING_PER_HOUR,
               expected_hold_hours: float = EXPECTED_HOLD_HOURS,
               base_cost_rate: float = BASE_COST_RATE,
               inr_per_price_unit: float = 1.0, qty_step: float = 1.0,
               min_qty: float = 0.0, min_notional_inr: float = 0.0,
               daily_limit_inr: float = DAILY_RISK_LIMIT_INR,
               max_candidate_risk_inr: float = MAX_CANDIDATE_RISK_INR) -> Optional[RiskPlan]:
    """Return a bracket and quantity, or None when no safe valid trade can be sized.

    Confidence is normalized to [0,1] and can only reduce candidate risk and
    leverage. The explicit fixed candidate buffer, round-trip base costs, and
    hourly funding reserve over the expected hold are charged before sizing.
    Funding input is a fraction per hour (0.0001 means 0.01% each hour).
    `inr_per_price_unit` converts one price-unit of P&L per contract into INR.
    """
    side = str(side).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    entry = _finite("entry", entry)
    atr = _finite("ATR", atr)
    confidence = _finite("confidence", confidence)
    exchange_max_leverage = _finite("exchange max leverage", exchange_max_leverage)
    fx = _finite("INR per price unit", inr_per_price_unit)
    step, min_qty = _finite("quantity step", qty_step), _finite("minimum quantity", min_qty)
    min_notional = _finite("minimum notional", min_notional_inr)
    candidate_cap = _finite("candidate risk cap", max_candidate_risk_inr)
    funding_rate = _finite("hourly funding rate", funding_fee_perc_hour)
    hold_hours = _finite("expected hold hours", expected_hold_hours)
    base_rate = _finite("base cost rate", base_cost_rate)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if confidence < MIN_CONFIDENCE:
        return None
    if min(entry, atr, exchange_max_leverage, fx, step) <= 0 or min_qty < 0 or min_notional < 0 or candidate_cap <= 0:
        raise ValueError("prices, leverage, FX, quantity step, and candidate cap must be positive")
    if funding_rate < 0 or hold_hours <= 0 or base_rate < 0:
        raise ValueError("funding and base costs must be nonnegative; expected hold must be positive")

    vol, tier = volatility_tier(entry, atr)
    stop_distance = tier.stop_atr * atr
    stop = entry - stop_distance if side == "LONG" else entry + stop_distance
    if stop <= 0:
        return None
    target_distance = MIN_REWARD_RISK * stop_distance
    target = entry + target_distance if side == "LONG" else entry - target_distance
    if target <= 0 or not (stop < entry < target if side == "LONG" else target < entry < stop):
        return None

    realized_pnls = tuple(realized_pnls)
    active_stop_risks = tuple(active_stop_risks)
    cost_buffer = _finite("candidate cost buffer", candidate_cost_buffer_inr)
    if cost_buffer < 0:
        raise ValueError("candidate cost buffer cannot be negative")
    remaining = remaining_daily_risk(realized_pnls, active_stop_risks, 0.0, daily_limit_inr)
    confidence_factor = 0.5 + 0.5 * confidence
    candidate_budget = min(candidate_cap - cost_buffer, remaining - cost_buffer) * confidence_factor
    if candidate_budget <= 0:
        return None

    # Margin setting decays with both volatility tier and confidence. It never
    # appears in the quantity equation below.
    leverage_cap = min(HARD_MAX_LEVERAGE, exchange_max_leverage, tier.leverage_cap)
    leverage = max(1.0, math.floor(leverage_cap * confidence_factor + 1e-12))
    leverage = min(leverage, leverage_cap)

    cost_rate = base_rate + funding_rate * hold_hours
    risk_per_unit = stop_distance * fx + entry * fx * cost_rate
    quantity = _floor_step(candidate_budget / risk_per_unit, step)
    if quantity < min_qty or quantity * entry * fx < min_notional:
        return None
    modeled_risk = quantity * risk_per_unit + cost_buffer
    # Rounding and costs must fit both the candidate allowance and full cycle cap.
    losses = gross_realized_losses(realized_pnls)
    active = sum(_finite("active stop risk", r) for r in active_stop_risks)
    if modeled_risk > min(candidate_cap, remaining) + 1e-9 or losses + active + modeled_risk > _finite("daily limit", daily_limit_inr) + 1e-9:
        return None
    return RiskPlan(side, stop, target, vol, tier.name, leverage, quantity,
                    risk_per_unit, modeled_risk, remaining)
