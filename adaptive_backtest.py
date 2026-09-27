"""Hermetic strategy audit using the adaptive-risk contract; never used by live execution.

Input is caller-supplied OHLCV data. This module performs no I/O, imports no
configuration, and cannot contact an exchange. Daily bars are used with a
conservative stop-first rule when stop and target are both touched.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Mapping, Sequence

from adaptive_risk import plan_trade

DAY = 86_400
LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
BASKET = ("XRP", "ADA", "DOGE", "LINK", "AVAX", "TRX")
TARGET_VOL = 0.5
VOL_LEN = 90
BTC_SMA = 200
INR_PER_USDT = 102.0
TAKER_FEE = 0.0005
GST = 0.18
SLIPPAGE = 0.0002
FUNDING_PER_DAY = 0.0003
IST_OFFSET_SECONDS = 19_800
CAPITAL_INR = 5_000.0
DAILY_RISK_INR = 500.0
CANDIDATE_BUFFER_INR = 10.0


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Spec:
    step: float = 0.001
    min_qty: float = 0.001
    min_notional_usdt: float = 5.0
    max_leverage: float = 5.0


def _validate_bars(bars: Sequence[Bar], name: str) -> None:
    last = None
    for b in bars:
        vals = (b.open, b.high, b.low, b.close)
        if not all(math.isfinite(x) and x > 0 for x in vals):
            raise ValueError(f"{name}: prices must be finite and positive")
        if b.high < max(b.open, b.close, b.low) or b.low > min(b.open, b.close, b.high):
            raise ValueError(f"{name}: invalid OHLC range")
        if last is not None and b.ts - last != DAY:
            raise ValueError(f"{name}: bars must be contiguous daily UTC candles")
        last = b.ts


def _atr(bars: Sequence[Bar], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        tr = b.high - b.low if i == 0 else max(b.high - b.low, abs(b.high - bars[i-1].close),
                                                  abs(b.low - bars[i-1].close))
        trs.append(tr)
        if i >= n - 1:
            out[i] = sum(trs[i - n + 1:i + 1]) / n
    return out


def signals(bars: Sequence[Bar]) -> dict[int, float]:
    """Match S1's nine close-based Donchian judges and 50% annual vol scaling."""
    closes = [b.close for b in bars]
    rets = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    states = {length: [0, None] for length in LOOKBACKS}
    out = {}
    for i in range(1, len(bars)):
        votes = 0
        for length in LOOKBACKS:
            if i < length:
                continue
            prior = closes[i - length:i]
            hi, lo = max(prior), min(prior)
            mid = (hi + lo) / 2
            state, stop = states[length]
            if state == 1:
                stop = max(stop, mid)
                if closes[i] < stop:
                    state = 0
            elif state == -1:
                stop = min(stop, mid)
                if closes[i] > stop:
                    state = 0
            if state == 0:
                if closes[i] > hi:
                    state, stop = 1, mid
                elif closes[i] < lo:
                    state, stop = -1, mid
            states[length] = [state, stop]
            votes += state
        if i < VOL_LEN:
            continue
        vol = statistics.pstdev(rets[i - VOL_LEN + 1:i + 1]) * math.sqrt(365)
        out[bars[i].ts] = votes / len(LOOKBACKS) * (min(TARGET_VOL / vol, 1.0) if vol > 0 else 0.0)
    return out


def _fee(notional_inr: float) -> float:
    """Exchange fee including GST; adverse fill-price slippage is separate."""
    return notional_inr * TAKER_FEE * (1 + GST)


def run_backtest(data: Mapping[str, Sequence[Bar]], specs: Mapping[str, Spec] | None = None,
                 *, start: int | None = None, end: int | None = None) -> dict:
    """Simulate next-open entries, adaptive brackets, regime, costs and INR risk.

    Stop/target are checked on each daily range; if both touch, stop wins. Open
    gaps fill at the open (plus adverse slippage). Candidate risk plus reserved
    stops and gross realized losses is capped at Rs500 per IST cycle. Daily
    candles approximate the cycle boundary. Indicators are calculated only from
    a prior closed candle; entries/exits use the next candle open. Stop/target
    checks use daily OHLC with stop-first ordering and slipped-fill brackets.
    """
    if "BTC" not in data:
        raise ValueError("BTC daily candles are required for the 200-day regime")
    coins = tuple(c for c in data if c != "BTC")
    if not coins:
        raise ValueError("at least one traded symbol is required")
    for coin, bars in data.items():
        _validate_bars(bars, coin)
    specmap = dict(specs or {})
    for c in coins:
        specmap.setdefault(c, Spec())
    maps = {c: {b.ts: i for i, b in enumerate(bs)} for c, bs in data.items()}
    sig = {c: signals(data[c]) for c in coins}
    atrs = {c: _atr(data[c]) for c in coins}
    btcs = data["BTC"]
    btc_map = maps["BTC"]
    timestamps = sorted(set.intersection(*(set(m) for m in maps.values())))
    cash = CAPITAL_INR
    positions = {}
    trades = []
    daily = []
    cycle_pnls: dict[int, list[float]] = {}
    cycle_set_counts: dict[int, int] = {}
    active_cycle = None
    cycle_start_equity = CAPITAL_INR
    previous_equity = CAPITAL_INR

    def marked_equity(ts):
        value = cash
        for coin, position in positions.items():
            bar = data[coin][maps[coin][ts]]
            direction = 1 if position["side"] == "LONG" else -1
            value += (bar.close - position["entry"]) * position["qty"] * INR_PER_USDT * direction
        return value

    for ti, ts in enumerate(timestamps):
        if (start is not None and ts < start) or (end is not None and ts >= end):
            continue
        indices = {c: maps[c][ts] for c in data}
        bi = indices["BTC"]
        decision_ts = timestamps[ti - 1] if ti else None
        decision_indices = ({c: maps[c][decision_ts] for c in data} if decision_ts is not None else None)
        regime = None
        if decision_ts is not None:
            prev_bi = decision_indices["BTC"]
            if prev_bi >= BTC_SMA - 1:
                prev_btc = btcs[prev_bi-BTC_SMA+1:prev_bi+1]
                regime = "LONG" if btcs[prev_bi].close >= sum(x.close for x in prev_btc) / BTC_SMA else "SHORT"
        cycle = (ts + IST_OFFSET_SECONDS) // DAY
        if cycle != active_cycle:
            if active_cycle is not None:
                cycle_start_equity = previous_equity
            active_cycle = cycle
        cycle_pnls.setdefault(cycle, [])
        # Existing positions are tested against this day's range before new entries.
        for c in list(positions):
            p = positions[c]
            b = data[c][indices[c]]
            exit_px, reason = None, None
            if p["side"] == "LONG":
                if b.open <= p["stop"]: exit_px, reason = b.open, "stop-gap"
                elif b.open >= p["target"]: exit_px, reason = b.open, "target-gap"
                elif b.low <= p["stop"]: exit_px, reason = p["stop"], "stop"
                elif b.high >= p["target"]: exit_px, reason = p["target"], "target"
                elif regime != "LONG" or sig[c].get(decision_ts, 0) <= 0: exit_px, reason = b.open, "trend/regime"
            else:
                if b.open >= p["stop"]: exit_px, reason = b.open, "stop-gap"
                elif b.open <= p["target"]: exit_px, reason = b.open, "target-gap"
                elif b.high >= p["stop"]: exit_px, reason = p["stop"], "stop"
                elif b.low <= p["target"]: exit_px, reason = p["target"], "target"
                elif regime != "SHORT" or sig[c].get(decision_ts, 0) >= 0: exit_px, reason = b.open, "trend/regime"
            # One full daily funding charge per day held; reserved costs are modeled in plan_trade.
            funding = p["qty"] * b.open * INR_PER_USDT * FUNDING_PER_DAY
            cash -= funding
            if exit_px is not None:
                px = exit_px * (1 - SLIPPAGE if p["side"] == "LONG" else 1 + SLIPPAGE)
                direction = 1 if p["side"] == "LONG" else -1
                gross = (px - p["entry"]) * p["qty"] * INR_PER_USDT * direction
                fee_out = _fee(p["qty"] * px * INR_PER_USDT)
                net = gross - fee_out - p["entry_cost"] - funding
                cash += gross - fee_out
                cycle_pnls[cycle].append(net)
                trades.append({"coin": c, "side": p["side"], "entry_ts": p["ts"], "exit_ts": ts,
                               "reason": reason, "net_inr": net, "leverage": p["leverage"],
                               "quantity": p["qty"], "stop": p["stop"], "target": p["target"]})
                del positions[c]
        if regime and decision_ts is not None:
            candidates = []
            cycle_pnl = marked_equity(ts) - cycle_start_equity
            pnl_blocked = cycle_pnl <= -DAILY_RISK_INR or cycle_pnl >= DAILY_RISK_INR
            for c in coins:
                i = indices[c]
                previous_i = decision_indices[c]
                b = data[c][i]
                w = sig[c].get(decision_ts, 0.0)
                if (c in positions or atrs[c][previous_i] is None or
                    (regime == "LONG" and w <= 0) or (regime == "SHORT" and w >= 0)):
                    continue
                confidence = min(1.0, abs(w) * len(BASKET))
                if confidence >= 0.55:
                    candidates.append((confidence, c, b, atrs[c][previous_i]))
            candidates.sort(reverse=True, key=lambda x: x[0])
            opened = 0
            completed = cycle_set_counts.get(cycle, 0)
            for confidence, c, b, atr in candidates:
                if pnl_blocked or completed + opened >= 3 or (completed + opened >= 2 and confidence < 0.85):
                    continue
                realized = cycle_pnls[cycle]
                active = [p["risk"] for p in positions.values()]
                try:
                    plan = plan_trade(side=regime, entry=b.open, atr=atr, confidence=confidence,
                        exchange_max_leverage=specmap[c].max_leverage, realized_pnls=realized,
                        active_stop_risks=active, candidate_cost_buffer_inr=CANDIDATE_BUFFER_INR,
                        inr_per_price_unit=INR_PER_USDT, qty_step=specmap[c].step,
                        min_qty=specmap[c].min_qty,
                        min_notional_inr=specmap[c].min_notional_usdt * INR_PER_USDT)
                except ValueError:
                    continue
                if plan is None: continue
                qty = plan.quantity
                entry = b.open * (1 + SLIPPAGE if regime == "LONG" else 1 - SLIPPAGE)
                entry_cost = _fee(qty * entry * INR_PER_USDT)
                # No more than the Rs5,000 allocation of margin; leverage never changes risk-sized qty.
                margin = qty * entry * INR_PER_USDT / plan.leverage
                used_margin = sum(p["qty"] * p["entry"] * INR_PER_USDT / p["leverage"]
                                  for p in positions.values())
                if used_margin + margin > CAPITAL_INR: continue
                cash -= entry_cost
                fill_shift = entry - b.open
                positions[c] = {"side": regime, "entry": entry, "ts": ts, "qty": qty,
                    "stop": plan.stop_loss + fill_shift, "target": plan.take_profit + fill_shift,
                    "risk": plan.planned_stop_risk_inr,
                    "leverage": plan.leverage, "entry_cost": entry_cost}
                opened += 1
                p = positions[c]
                stop_hit = (b.low <= p["stop"] if regime == "LONG" else b.high >= p["stop"])
                target_hit = (b.high >= p["target"] if regime == "LONG" else b.low <= p["target"])
                if stop_hit or target_hit:
                    why = "stop" if stop_hit else "target"
                    raw_exit = p["stop"] if stop_hit else p["target"]
                    exit_px = raw_exit * (1 - SLIPPAGE if regime == "LONG" else 1 + SLIPPAGE)
                    direction = 1 if regime == "LONG" else -1
                    gross = (exit_px - entry) * qty * INR_PER_USDT * direction
                    fee_out = _fee(qty * exit_px * INR_PER_USDT)
                    funding = qty * b.open * INR_PER_USDT * FUNDING_PER_DAY
                    cash -= funding
                    net = gross - fee_out - entry_cost - funding
                    cash += gross - fee_out
                    cycle_pnls[cycle].append(net)
                    trades.append({"coin": c, "side": regime, "entry_ts": ts, "exit_ts": ts,
                                   "reason": why, "net_inr": net, "leverage": plan.leverage,
                                   "quantity": qty, "stop": p["stop"], "target": p["target"]})
                    del positions[c]
            cycle_set_counts[cycle] = completed + opened
        # Mark-to-market daily P&L, including estimated floating costs/funding.
        unreal = sum((data[c][maps[c][ts]].close - p["entry"]) * p["qty"] * INR_PER_USDT
                     * (1 if p["side"] == "LONG" else -1) for c, p in positions.items())
        equity_now = cash + unreal
        previous_equity = equity_now
        daily.append({"ts": ts, "equity_inr": equity_now, "realized_inr": sum(cycle_pnls[cycle]),
                      "open_positions": len(positions), "regime": regime})
    return {"daily": daily, "trades": trades, "open_positions": len(positions),
            "assumptions": {"fee": TAKER_FEE, "gst": GST, "slippage_per_side": SLIPPAGE,
                            "funding_per_day": FUNDING_PER_DAY, "fx_inr_per_usdt": INR_PER_USDT}}


def walk_forward(data: Mapping[str, Sequence[Bar]], *, test_days: int = 365,
                 first_test_ts: int | None = None, specs: Mapping[str, Spec] | None = None) -> list[dict]:
    """Return non-overlapping chronological test-fold summaries; no parameter fitting."""
    ts = sorted(set.intersection(*(set(b.ts for b in bars) for bars in data.values())))
    if not ts:
        return []
    first = first_test_ts or ts[0] + 360 * DAY
    end_all = ts[-1] + DAY
    out = []
    fold_start = first
    while fold_start < end_all:
        fold_end = min(end_all, fold_start + test_days * DAY)
        result = run_backtest(data, specs, start=fold_start, end=fold_end)
        marks = [x["equity_inr"] for x in result["daily"]]
        returns = [b / a - 1 for a, b in zip(marks, marks[1:]) if a]
        peak, mdd = 1.0, 0.0
        eq = 1.0
        for r in returns:
            eq *= 1 + r
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
        out.append({"start": fold_start, "end": fold_end, "days": len(returns),
                    "total_return": eq - 1, "max_drawdown": mdd,
                    "trades": sum(fold_start <= t["exit_ts"] < fold_end for t in result["trades"])})
        fold_start = fold_end
    return out
