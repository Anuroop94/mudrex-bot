"""Position sizing and circuit breakers. Shared by backtest, paper and live so all three size identically."""
import math

import config


def size(equity, entry, stop, risk_mult=1.0, step=None, min_qty=None, min_notional=None):
    """Quantity risking RISK_PCT*risk_mult of equity if stop is hit, capped by leverage.
    Always rounds DOWN to the quantity step; 0 if below minimums (never round up = never over-risk).
    step/min_qty/min_notional: per-contract values from the Mudrex listing; default to config."""
    step = config.QTY_STEP if step is None else step
    min_qty = config.MIN_QTY if min_qty is None else min_qty
    min_notional = config.MIN_NOTIONAL if min_notional is None else min_notional
    dist = abs(entry - stop)
    if equity <= 0 or dist <= 0 or entry <= 0:
        return 0.0
    qty = min(equity * config.RISK_PCT * risk_mult / dist, equity * config.LEVERAGE / entry)
    qty = math.floor(qty / step + 1e-9) * step
    qty = round(qty, 10)  # kill float dust like 0.0030000000000000001
    return qty if qty >= min_qty and qty * entry >= min_notional else 0.0


def fee(notional):
    return notional * config.TAKER_FEE * (1 + config.GST)


def risk_mult(equity, peak):
    """1.0 normal, 0.5 in drawdown, 0.0 halted."""
    # multiply, don't divide: 1 - 800/1000 == 0.19999... would miss an exact 20% drawdown
    if peak <= 0 or equity <= peak * (1 - config.DD_HALT):
        return 0.0
    return 0.5 if equity <= peak * (1 - config.DD_HALVE) else 1.0


def day_blocked(equity, day_start_equity):
    return equity <= day_start_equity * (1 - config.DAILY_LOSS_CAP)
