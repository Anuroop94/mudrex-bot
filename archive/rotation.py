"""Cross-sectional momentum rotation on daily bars.

Every REBAL days at the daily close: rank coins by LOOKBACK-day return, hold the top TOP_N (equal weight,
1x, long only) if their momentum > 0 and close > SMA(trend_days); otherwise cash. Fills at next day's open.
Costs: fee + GST + slippage on every buy and sell. Few params, fixed in advance: no optimiser = no overfit.

ponytail: 1x notional with no stop-loss, so per-position risk is far above the 1% cap. This file only
answers "is there an edge?". A live version needs a stop design that fits RISK_PCT first.
Run: python rotation.py
"""
import config
import data
import scan_meanrev

REBAL = 7
TREND_DAYS = 50
LOOKBACKS = (7, 14, 30)
TOPS = (1, 3)


def daily(candles_1h):
    """UTC-day bars from 1h candles; only complete days (24 bars)."""
    days = {}
    for c in candles_1h:
        days.setdefault(c[0] // 86400, []).append(c)
    return {d: (g[0][1], g[-1][4]) for d, g in days.items() if len(g) == 24}  # day -> (open, close)


def run(bars, lookback, top_n):
    cost = config.TAKER_FEE * (1 + config.GST) + config.SLIPPAGE
    all_days = sorted({d for b in bars.values() for d in b})
    start = max(lookback, TREND_DAYS) + 1
    equity, peak, mdd, held, switches = 1.0, 1.0, 0.0, {}, 0
    curve = []
    for k in range(start, len(all_days) - 1):
        d, nxt = all_days[k], all_days[k + 1]
        # mark held positions from their last mark to next day's open (the fill price of any switch)
        if held:
            ret = 0.0
            for coin, entry_px in held.items():
                b = bars[coin]
                if nxt in b:
                    ret += (b[nxt][0] / entry_px - 1) / len(held)
                    held[coin] = b[nxt][0]
            equity *= 1 + ret
        if (k - start) % REBAL == 0:
            ranked = []
            for coin, b in bars.items():
                past = all_days[k - lookback]
                closes = [b[x][1] for x in all_days[k - TREND_DAYS + 1:k + 1] if x in b]
                if d in b and past in b and nxt in b and len(closes) == TREND_DAYS:
                    mom = b[d][1] / b[past][1] - 1
                    if mom > 0 and b[d][1] > sum(closes) / TREND_DAYS:
                        ranked.append((mom, coin))
            want = {c for _, c in sorted(ranked, reverse=True)[:top_n]}
            changed = (set(held) ^ want)
            if changed:
                # cost on the traded fraction of the book (each position = 1/len of book)
                sells = len(set(held) - want) / max(len(held), 1)
                buys = len(want - set(held)) / max(len(want), 1)
                equity *= 1 - cost * (sells + buys)
                switches += 1
            held = {c: bars[c][nxt][0] for c in want} if changed else held
        peak = max(peak, equity)
        mdd = max(mdd, 1 - equity / peak)
        curve.append(equity)
    return dict(net=equity - 1, max_dd=mdd, switches=switches, days=len(curve))


def buy_hold(bars, first_day):
    """Equal-weight buy & hold over the same span the strategy trades."""
    rets = [b[max(b)][1] / b[min(d for d in b if d >= first_day)][0] - 1 for b in bars.values()]
    return sum(rets) / len(rets)


if __name__ == "__main__":
    coins = scan_meanrev.coins()
    bars = {c: daily(data.load(365, f"{c}/USDT", "1h", 3600)) for c in coins}
    bars = {c: b for c, b in bars.items() if len(b) > 200}
    print(f"{len(bars)} coins, daily bars, rebalance every {REBAL}d, trend filter SMA{TREND_DAYS}\n")
    for top in TOPS:
        for lb in LOOKBACKS:
            r = run(bars, lb, top)
            print(f"top {top}  lookback {lb:>2}d  net={r['net']:+7.2%}  maxDD={r['max_dd']:6.2%}  "
                  f"switches={r['switches']:>3}  days={r['days']}")
    days = sorted({d for b in bars.values() for d in b})
    first = days[max(LOOKBACKS[0], TREND_DAYS) + 1]
    print(f"\nbenchmark: equal-weight buy & hold, same span = {buy_hold(bars, first):+.2%}")
    if "BTC" in bars:
        print(f"benchmark: buy & hold BTC, same span = {buy_hold({'BTC': bars['BTC']}, first):+.2%}")
