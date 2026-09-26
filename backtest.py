"""Bar-by-bar backtest. Decisions at bar close, fills at next bar open. Conservative: if SL and TP
both touch in one bar, SL wins. Fees + GST + slippage on every fill; flat worst-case funding per day held.

ponytail: liquidation not modelled; at 3x the SL always sits inside the liquidation price.
ponytail: funding is a flat always-paid rate; real rates flip sign and spike. Use /futures/fee/history live.
ponytail: drawdown uses realized equity, not mark-to-market; open-trade dips are invisible to it.
"""
import config
import risk
import strategy


def _stats(trades, start_equity, equity, curve):
    wins = [t for t in trades if t["pnl"] > 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    peak, mdd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    return dict(
        trades=len(trades),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        profit_factor=gp / gl if gl else (float("inf") if gp else 0.0),
        net_return=equity / start_equity - 1,
        max_dd=mdd,
        fees=sum(t["fees"] for t in trades),
    )


def run(candles, ind, p, start, end, equity=config.START_EQUITY, peak=None):
    """Simulate bars [start, end). Returns stats + trades + final equity/peak/halted."""
    start = max(start, config.WARMUP, 1)
    end = min(end, len(candles))
    start_equity = equity
    peak = equity if peak is None else peak
    slip = config.SLIPPAGE
    trades, curve = [], [equity]
    pos, want_entry, want_exit = None, 0, False
    day, day_equity, halted = None, equity, False

    def close(px_raw, t, reason):
        nonlocal equity, peak, pos
        side, qty = pos["side"], pos["qty"]
        px = px_raw * (1 - side * slip)
        held_days = (t - pos["time"]) / 86400
        f = risk.fee(px * qty) + pos["entry"] * qty * config.FUNDING_PER_DAY * held_days
        pnl = side * (px - pos["entry"]) * qty - f - pos["entry_fee"]
        equity += side * (px - pos["entry"]) * qty - f
        peak = max(peak, equity)
        trades.append(dict(side=side, qty=qty, entry=pos["entry"], exit=px, entry_time=pos["time"],
                           exit_time=t, pnl=pnl, fees=f + pos["entry_fee"], reason=reason))
        curve.append(equity)
        pos = None

    for i in range(start, end):
        t, o, h, l, _, _ = candles[i]
        d = (t + config.IST_OFFSET) // 86400
        if d != day:
            day, day_equity = d, equity

        # 1. act at this bar's open on last bar's decision
        if pos and want_exit:
            close(o, t, "signal")
        mult = risk.risk_mult(equity, peak)
        if mult == 0:
            halted = True
        if want_entry and pos is None and not halted and not risk.day_blocked(equity, day_equity):
            side = want_entry
            entry = o * (1 + side * slip)
            dist = p["sl_atr"] * ind["atr"][i - 1]
            sl = entry - side * dist
            tp = entry + side * dist * p["rr"] if p["rr"] else side * float("inf")
            qty = risk.size(equity, entry, sl, mult)
            if qty > 0:
                f = risk.fee(entry * qty)
                equity -= f
                pos = dict(side=side, qty=qty, entry=entry, sl=sl, tp=tp, entry_fee=f, time=t)
        want_entry, want_exit = 0, False

        # 2. SL / TP inside this bar (SL first: conservative)
        if pos:
            if pos["side"] == 1:
                if l <= pos["sl"]:
                    close(min(o, pos["sl"]), t, "sl")
                elif h >= pos["tp"]:
                    close(pos["tp"], t, "tp")
            else:
                if h >= pos["sl"]:
                    close(max(o, pos["sl"]), t, "sl")
                elif l <= pos["tp"]:
                    close(pos["tp"], t, "tp")

        # trailing stop: ratchet from this bar's extreme, effective from next bar (no intrabar lookahead)
        if pos and p["trail_atr"]:
            trail = p["trail_atr"] * ind["atr"][i]
            if pos["side"] == 1:
                pos["sl"] = max(pos["sl"], h - trail)
            else:
                pos["sl"] = min(pos["sl"], l + trail)

        # 3. decide at close; acted on next bar
        if i < end - 1:
            sig = strategy.signal(ind, i)
            if pos and strategy.exit_signal(ind, i, pos["side"]):
                want_exit = True
            if sig and (pos is None or want_exit):
                want_entry = sig

    if pos:
        close(candles[end - 1][4], candles[end - 1][0], "end")

    r = _stats(trades, start_equity, equity, curve)
    r.update(equity=equity, peak=peak, halted=halted, trade_list=trades)
    return r


def fmt(r):
    return (f"trades={r['trades']} win={r['win_rate']:.1%} PF={r['profit_factor']:.2f} "
            f"net={r['net_return']:+.2%} maxDD={r['max_dd']:.2%} fees={r['fees']:.2f}"
            + (" HALTED" if r["halted"] else ""))


if __name__ == "__main__":
    import data
    c = data.load()
    p = config.DEFAULT_PARAMS
    r = run(c, strategy.indicators(c, p), p, 0, len(c))
    print(f"Default params {p}\n{fmt(r)}  equity {config.START_EQUITY:.0f} -> {r['equity']:.2f}")
