"""Which coins can this account trade without risking more than RISK_PCT per trade?

The smallest legal order is max(min_contract * price, MIN_NOTIONAL). If that order's stop-loss would
lose more than RISK_PCT of equity, the coin is unaffordable: trading it means over-risking.

Run: python pick_coins.py 1000          (equity in INR)
With MUDREX_API_SECRET set, real contract specs come from GET /futures; otherwise ASSUMED values.
"""
import json
import os
import sys
import urllib.request

import config
import data
import strategy

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "LTC", "SUI", "TRX"]
# ponytail: typical exchange minimums, NOT confirmed for Mudrex; replaced by API values when a key is set
ASSUMED_MIN_CONTRACT = dict(BTC=0.001, ETH=0.01, SOL=0.1, XRP=1, DOGE=1, BNB=0.01, ADA=1,
                            AVAX=0.1, LINK=0.1, LTC=0.1, SUI=10, TRX=1)
SL_ATR = 2.5  # stop width used by the 1h variants


def listing():
    """Every futures contract on Mudrex (raw API rows), paginated. None without an API secret."""
    secret = os.environ.get("MUDREX_API_SECRET")
    if not secret:
        return None
    rows, limit = [], 100
    while True:
        url = f"https://trade.mudrex.com/fapi/v1/futures?offset={len(rows)}&limit={limit}&sort=volume&order=desc"
        req = urllib.request.Request(url, headers={"X-Authentication": secret})
        with urllib.request.urlopen(req, timeout=15) as r:
            page = json.load(r)["data"]
        rows += page
        if len(page) < limit:
            return rows


def api_specs():
    rows = listing()
    if rows is None:
        return None
    return {row["symbol"].removesuffix("USDT"): dict(min_contract=float(row["min_contract"]),
                                                    min_notional=float(row["min_notional_value"]))
            for row in rows}


def typical_stop(candles_1h):
    """Median stop distance as a fraction of price over the last 90 days of 1h candles."""
    cs = candles_1h[-2160:]
    atr = strategy.atr(cs, 14)
    stops = sorted(SL_ATR * a / x[4] for a, x in zip(atr[100:], cs[100:]))
    return stops[len(stops) // 2]


def main(equity_inr):
    equity = equity_inr / config.INR_PER_USDT
    specs = api_specs()
    src = "Mudrex API" if specs else "ASSUMED (set MUDREX_API_SECRET for real values)"
    budget = equity * config.RISK_PCT
    print(f"Equity Rs {equity_inr:.0f} = ${equity:.2f} at Rs {config.INR_PER_USDT}/USDT; "
          f"max risk/trade {config.RISK_PCT:.0%} = ${budget:.3f}. Specs: {src}\n")
    print(f"{'coin':<5} {'price':>10} {'stop%':>6} {'min order $':>11} {'risk at min':>11} {'margin@3x':>9}  verdict")
    rows = []
    for c in COINS:
        cs = data.load(365, f"{c}/USDT", "1h", 3600)
        price, stop_pct = cs[-1][4], typical_stop(cs)
        s = (specs or {}).get(c) or dict(min_contract=ASSUMED_MIN_CONTRACT[c], min_notional=config.MIN_NOTIONAL)
        min_order = max(s["min_contract"] * price, s["min_notional"])
        risk_at_min = min_order * stop_pct / equity
        rows.append((risk_at_min, c, price, stop_pct, min_order))
    for risk_at_min, c, price, stop_pct, min_order in sorted(rows):
        ok = risk_at_min <= config.RISK_PCT
        margin = min_order / config.LEVERAGE
        verdict = "OK" if ok and margin <= equity else f"NO: {risk_at_min / config.RISK_PCT:.1f}x the risk limit"
        print(f"{c:<5} {price:>10.4f} {stop_pct:>6.2%} {min_order:>11.2f} {risk_at_min:>11.2%} {margin:>9.2f}  {verdict}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 1000)
