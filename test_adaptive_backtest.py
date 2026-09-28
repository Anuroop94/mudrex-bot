"""Synthetic regressions for adaptive_backtest.py (Codex handoff item 2). No I/O, no network.
Run: python test_adaptive_backtest.py
"""
import math
import random

import adaptive_backtest as ab

DAY = ab.DAY
T0 = 1_600_000_000 // DAY * DAY


def series(prices, spread=0.01):
    return [ab.Bar(T0 + i * DAY, p, p * (1 + spread), p * (1 - spread), p) for i, p in enumerate(prices)]


def trend(n, start, daily):
    return [start * (1 + daily) ** i for i in range(n)]


def run(data, **kw):
    return ab.run_backtest(data, {c: ab.Spec(step=0.001, min_qty=0.001, min_notional_usdt=1.0) for c in data if c != "BTC"},
                           **kw)


def test_no_lookahead():
    """Trades that closed before day k must not change when prices after day k change."""
    random.seed(3)
    n, k = 700, 600
    walk = [100.0]
    for _ in range(n - 1):
        walk.append(walk[-1] * (1 + random.gauss(0.001, 0.03)))
    btc = series(trend(n, 100, 0.002))
    base = run({"BTC": btc, "XRP": series(walk)})
    changed = walk[:k] + [p * 3 for p in walk[k:]]
    alt = run({"BTC": btc, "XRP": series(changed)})
    early = lambda r: [(t["entry_ts"], t["exit_ts"], round(t["net_inr"], 6)) for t in r["trades"]  # noqa: E731
                       if t["exit_ts"] < T0 + (k - 1) * DAY]
    assert early(base) and early(base) == early(alt)


def test_short_profits_when_price_falls():
    n = 700
    btc = series(trend(n, 100, -0.003))                      # BTC below its 200-day average: SHORT regime
    r = run({"BTC": btc, "XRP": series(trend(n, 100, -0.004), spread=0.002)})
    shorts = [t for t in r["trades"] if t["side"] == "SHORT"]
    assert shorts and not any(t["side"] == "LONG" for t in r["trades"])
    assert sum(t["net_inr"] for t in shorts) > 0 and r["daily"][-1]["equity_inr"] > ab.CAPITAL_INR


def test_brackets_are_side_aware():
    n = 700
    r = run({"BTC": series(trend(n, 100, 0.003)), "XRP": series(trend(n, 100, 0.004), spread=0.002)})
    for t in r["trades"]:
        assert t["stop"] < t["target"] if t["side"] == "LONG" else t["target"] < t["stop"]


def test_bar_exit_gap_and_ambiguous_touch_are_conservative():
    b = ab.Bar(T0, 100, 112, 88, 100)
    assert ab._bar_exit("LONG", b, 90, 110) == (90, "stop")
    assert ab._bar_exit("SHORT", b, 110, 90) == (110, "stop")
    assert ab._bar_exit("LONG", ab.Bar(T0, 85, 90, 80, 88), 90, 110) == (85, "stop-gap")
    assert ab._bar_exit("SHORT", ab.Bar(T0, 115, 120, 112, 118), 110, 90) == (115, "stop-gap")


def test_slippage_reanchors_both_sides_without_changing_distance():
    assert ab._reanchor_bracket("LONG", 100, 101, 95, 110) == (96, 111)
    assert ab._reanchor_bracket("SHORT", 100, 99, 105, 90) == (104, 89)


def test_daily_loss_never_starts_new_trades_past_cap():
    """Every trade opened in a cycle starts while cycle P&L is inside +/-Rs500 and at most 3 per cycle."""
    random.seed(5)
    walk = [100.0]
    for _ in range(699):
        walk.append(walk[-1] * (1 + random.gauss(0.002, 0.02)))
    r = run({"BTC": series(trend(700, 100, 0.002)), "XRP": series(walk), "ADA": series(walk[::-1]),
             "DOGE": series([w * 0.5 for w in walk])})
    per_cycle = {}
    for t in r["trades"]:
        c = (t["entry_ts"] + ab.IST_OFFSET_SECONDS) // DAY
        per_cycle[c] = per_cycle.get(c, 0) + 1
    assert per_cycle and max(per_cycle.values()) <= 3
    assert all(t["net_inr"] > -ab.DAILY_RISK_INR - 50 for t in r["trades"])   # one trade never ~Rs500+ (gaps aside)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} passed")
