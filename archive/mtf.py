"""Multi-year trend-following test on 4h and daily candles. Variants and pass bar fixed BEFORE running:
    pass = pooled PF >= 1.10 AND >= 60% of coins profitable, out-of-sample walk-forward, after fees+GST+funding.
Also prints per-year P&L and an equal-weight buy & hold benchmark over the same out-of-sample span.
Run: python mtf.py
"""
import time
from collections import defaultdict

import config
import data
import scan

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "LTC", "TRX", "ETC", "HBAR"]
TF_4H = dict(interval="4h", sec=14400, days=2400, warmup=600, train_days=180, test_days=30, min_trades=8)
TF_1D = dict(interval="1d", sec=86400, days=2400, warmup=300, train_days=365, test_days=90, min_trades=5)

BO = dict(don_n=[20, 55], sl_atr=[2.0, 3.0], trail_atr=[2.0, 3.0])
EMA = dict(fast=[10, 20], slow=[50, 100], sl_atr=[2.0, 3.0], trail_atr=[2.0, 3.0])


def variants(trend):
    return {
        f"T1 Breakout + trailing, trend EMA{trend}": ({"entry": "breakout", "rr": 0, "trend": trend}, BO),
        "T2 Breakout + trailing, no trend filter": ({"entry": "breakout", "rr": 0, "trend": 0}, BO),
        f"T3 EMA cross + trailing, trend EMA{trend}": ({"entry": "ema", "rr": 0, "trend": trend}, EMA),
    }


def report(results, tf):
    for name, per in results.items():
        by_year = defaultdict(float)
        for _, L, _ in per:
            for t in L["trade_list"]:
                by_year[time.gmtime(t["exit_time"]).tm_year] += t["pnl"] / config.START_EQUITY
        n = len(per)
        years = "  ".join(f"{y}:{v / n:+.1%}" for y, v in sorted(by_year.items()))
        print(f"  {name:<40} per-year avg per coin: {years}")
    # buy & hold over each coin's out-of-sample span
    first_oos = tf["warmup"] + tf["train_days"] * 86400 // tf["sec"]
    bh = []
    for c in COINS:
        cs = data.load(tf["days"], f"{c}/USDT", tf["interval"], tf["sec"])
        if len(cs) > first_oos + tf["test_days"] * 86400 // tf["sec"]:
            bh.append(cs[-1][4] / cs[first_oos][1] - 1)
    print(f"  benchmark: equal-weight buy & hold over same spans = {sum(bh) / len(bh):+.1%} ({len(bh)} coins)")


if __name__ == "__main__":
    for label, tf, trend in (("4H", TF_4H, 200), ("DAILY", TF_1D, 100)):
        print(f"\n===== {label}: train {tf['train_days']}d / test {tf['test_days']}d, up to {tf['days']}d history =====",
              flush=True)
        res = scan.main(COINS, f"mtf_{label.lower()}_scan.csv", variants(trend), tf)
        report(res, tf)
