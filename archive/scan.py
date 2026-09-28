"""Multi-coin strategy scan: every variant runs the full walk-forward (out-of-sample) on every coin.
Run: python scan.py        Output: per-variant pooled results + scan_results.csv
"""
import csv
import os
import time

import config
import data
import walkforward as wf

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "LTC", "SUI", "TRX"]

# Same small base grid for every variant so differences come from the rule, not grid size.
EMA_BASE = dict(fast=[9, 20], slow=[21, 55], sl_atr=[1.5, 2.5])
BO_BASE = dict(don_n=[20, 55], sl_atr=[1.5, 2.5])
VARIANTS = {
    "V0 EMA cross + TP (current)":       ({"entry": "ema"}, {**EMA_BASE, "rr": [2, 3]}),
    "V1 EMA + ADX filter":               ({"entry": "ema"}, {**EMA_BASE, "rr": [2, 3], "adx_min": [20, 25]}),
    "V2 EMA + trailing stop":            ({"entry": "ema", "rr": 0}, {**EMA_BASE, "trail_atr": [2, 3]}),
    "V3 EMA + ADX + trailing":           ({"entry": "ema", "rr": 0}, {**EMA_BASE, "adx_min": [20, 25], "trail_atr": [2, 3]}),
    "V4 Breakout + trailing":            ({"entry": "breakout", "rr": 0}, {**BO_BASE, "trail_atr": [2, 3]}),
    "V5 Breakout + ADX + trailing":      ({"entry": "breakout", "rr": 0}, {**BO_BASE, "adx_min": [20, 25], "trail_atr": [2, 3]}),
    "V6 V5 + higher-TF trend (EMA400)":  ({"entry": "breakout", "rr": 0, "trend": 400}, {**BO_BASE, "adx_min": [20, 25], "trail_atr": [2, 3]}),
    "V7 V5 + volume > 1.5x avg":         ({"entry": "breakout", "rr": 0, "vol_mult": 1.5}, {**BO_BASE, "adx_min": [20, 25], "trail_atr": [2, 3]}),
}


# Timeframe presets. warmup = 3x the longest trend EMA used on that timeframe.
TF_1H = dict(interval="1h", sec=3600, days=365, warmup=1200, train_days=90, test_days=14, min_trades=10)


def setup(tf=TF_1H):
    config.INTERVAL, config.INTERVAL_SEC = tf["interval"], tf["sec"]
    config.WARMUP, config.MIN_TRADES = tf["warmup"], tf["min_trades"]
    config.TRAIN_DAYS, config.TEST_DAYS = tf["train_days"], tf["test_days"]
    # ponytail: sizing step neutral in scans; real per-coin step comes from GET /futures/{id} with API key
    config.QTY_STEP = config.MIN_QTY = 1e-8


def setup_1h():
    setup(TF_1H)


def pooled(trades):
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return gp / gl if gl else float("inf")


def main(coins=COINS, out_name="scan_results.csv", variants=VARIANTS, tf=TF_1H):
    """Returns {variant: [(coin, learned_result, sat_out_windows)]} for further analysis."""
    setup(tf)
    candles = {c: data.load(tf["days"], f"{c}/USDT", tf["interval"], tf["sec"]) for c in coins}
    n_train, n_test = tf["train_days"] * 86400 // tf["sec"], tf["test_days"] * 86400 // tf["sec"]
    need = config.WARMUP + n_train + n_test
    results = {}
    base_default = dict(config.DEFAULT_PARAMS)
    out = []
    for name, (fixed, grid) in variants.items():
        t0 = time.time()
        config.DEFAULT_PARAMS = {**base_default, **fixed, **{k: v[0] for k, v in grid.items()}}
        config.GRID = grid
        per = []
        for c in coins:
            if len(candles[c]) < need:
                continue
            learned, _, rows = wf.walk(candles[c])
            per.append((c, learned, sum(r["decision"].startswith("sit") for r in rows)))
            out.append(dict(variant=name, coin=c, net=f"{learned['net_return']:.4f}",
                            max_dd=f"{learned['max_dd']:.4f}", pf=f"{learned['profit_factor']:.3f}",
                            trades=learned["trades"], win_rate=f"{learned['win_rate']:.3f}", sat_out=per[-1][2]))
        trades = [t for _, L, _ in per for t in L["trade_list"]]
        avg = sum(L["net_return"] for _, L, _ in per) / len(per)
        wins = sum(L["net_return"] > 0 for _, L, _ in per)
        best = max(per, key=lambda x: x[1]["net_return"])
        worst = min(per, key=lambda x: x[1]["net_return"])
        print(f"{name:<36} avg={avg:+6.2%}  profitable coins={wins:>2}/{len(per)}  pooled PF={pooled(trades):.2f}  "
              f"trades={len(trades):>4}  best={best[0]} {best[1]['net_return']:+.1%}  "
              f"worst={worst[0]} {worst[1]['net_return']:+.1%}  ({time.time() - t0:.0f}s)", flush=True)
        results[name] = per
    config.DEFAULT_PARAMS = base_default
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), out_name)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"\nper-coin detail: {path}")
    return results


if __name__ == "__main__":
    main()
