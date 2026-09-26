"""Whole-market coin selection from the live Mudrex listing (needs MUDREX_API_SECRET, read-only).

Filters, in order:
  1. liquidity: 24h USD volume >= MIN_USD_VOL_24H (thin books = slippage far above the modelled 0.02%)
  2. history: enough 1h candles for warmup + train + test windows
  3. affordability: smallest legal order's stop-loss loses <= RISK_PCT of equity
Then every surviving coin goes through the walk-forward scan.

Run: python universe.py 1000        (equity in INR)
"""
import csv
import os
import sys

import config
import data
import pick_coins
import scan

MIN_USD_VOL_24H = 12_000_000   # ~$500k/hour


def eligible(equity_inr):
    rows = pick_coins.listing()
    if rows is None:
        sys.exit("MUDREX_API_SECRET missing: add it to .env")
    equity = equity_inr / config.INR_PER_USDT
    scan.setup_1h()
    need_bars = config.WARMUP + 3000
    out, dropped = [], dict(liquidity=0, history=0, affordability=0)
    liquid = [r for r in rows if float(r["volume"]) * float(r["price"]) >= MIN_USD_VOL_24H]
    dropped["liquidity"] = len(rows) - len(liquid)
    print(f"{len(rows)} contracts; {len(liquid)} pass liquidity; loading 1h history...", flush=True)
    for n, r in enumerate(liquid, 1):
        coin, price = r["symbol"].removesuffix("USDT"), float(r["price"])
        print(f"  [{n}/{len(liquid)}] {coin}", flush=True)
        try:
            cs = data.load(365, f"{coin}/USDT", "1h", 3600)
        except RuntimeError:
            cs = []
        if len(cs) < need_bars:
            dropped["history"] += 1
            continue
        stop = pick_coins.typical_stop(cs)
        min_order = max(float(r["min_contract"]) * price, float(r["min_notional_value"]))
        risk_at_min = min_order * stop / equity
        if risk_at_min > config.RISK_PCT:
            dropped["affordability"] += 1
            continue
        out.append(dict(coin=coin, price=price, usd_vol_24h=round(float(r["volume"]) * price),
                        min_order=round(min_order, 2), stop_pct=round(stop, 4),
                        risk_at_min=round(risk_at_min, 4), max_leverage=r["max_leverage"],
                        funding_perc=r["funding_fee_perc"]))
    print(f"dropped: {dropped}; eligible: {len(out)}")
    return out


def main(equity_inr):
    coins = eligible(equity_inr)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "universe.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(coins[0]))
        w.writeheader()
        w.writerows(coins)
    print(", ".join(c["coin"] for c in coins), "\n")
    scan.main([c["coin"] for c in coins], "universe_scan.csv")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 1000)
