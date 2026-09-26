"""Mean-reversion strategy family, same walk-forward harness and coins as the trend scans.
Pass bar (fixed before running): pooled PF >= 1.10 AND profitable on >= 60% of coins, out-of-sample, after costs.
Run: python scan_meanrev.py
"""
import csv
import os

import scan

HERE = os.path.dirname(os.path.abspath(__file__))
RSI = dict(rsi_lo=[25, 30], rsi_hi=[70, 75], sl_atr=[1.5, 2.5])
BB = dict(bb_n=[20, 50], bb_k=[2.0, 2.5], sl_atr=[1.5, 2.5])
VARIANTS = {
    "M1 RSI pullback, with trend":      ({"entry": "rsi", "rr": 0, "trend": 200}, RSI),
    "M2 RSI fade, chop only (ADX<max)": ({"entry": "rsi", "rr": 0, "trend": 0}, {**RSI, "adx_max": [20, 25]}),
    "M3 Bollinger fade, with trend":    ({"entry": "bb", "rr": 0, "trend": 200}, BB),
    "M4 Bollinger fade, chop only":     ({"entry": "bb", "rr": 0, "trend": 0}, {**BB, "adx_max": [20, 25]}),
}


def coins():
    with open(os.path.join(HERE, "universe.csv"), newline="") as f:
        eligible = [r["coin"] for r in csv.DictReader(f)]
    return list(dict.fromkeys(scan.COINS + eligible))


if __name__ == "__main__":
    scan.main(coins(), "meanrev_scan.csv", VARIANTS)
