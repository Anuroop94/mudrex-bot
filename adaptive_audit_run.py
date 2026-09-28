"""Run adaptive_backtest.py (the exact new two-sided live rules) on real cached daily data. Research only.

Costs match the bot's cost model (config + portfolio.SLIPPAGE 0.05%/side), not the backtester's lighter default.
DEV = before portfolio.SPLIT (evidence); POST = after (descriptive only, looked at many times).
Run: python adaptive_audit_run.py
"""
import statistics as st
import sys

import adaptive_backtest as ab
import config
import data
import pick_coins
import portfolio as pf
import s1
import s1_audit

DAY = 86400
ab.SLIPPAGE = pf.SLIPPAGE
ab.TAKER_FEE, ab.GST, ab.FUNDING_PER_DAY, ab.INR_PER_USDT = (config.TAKER_FEE, config.GST,
                                                             config.FUNDING_PER_DAY, config.INR_PER_USDT)


def bars(symbol):
    return [ab.Bar(int(c[0]), c[1], c[2], c[3], c[4]) for c in data.load(2400, f"{symbol}/USDT", "1d", DAY)]


def contiguous(bs):
    """Longest tail of strictly consecutive daily bars (the backtester refuses gaps)."""
    i = len(bs) - 1
    while i > 0 and bs[i].ts - bs[i - 1].ts == DAY:
        i -= 1
    return bs[i:]


def stats(result, a=None, b=None):
    d = [x for x in result["daily"] if (a is None or x["ts"] >= a) and (b is None or x["ts"] < b)]
    rets = [y["equity_inr"] / x["equity_inr"] - 1 for x, y in zip(d, d[1:]) if x["equity_inr"]]
    tr = [t for t in result["trades"] if (a is None or t["exit_ts"] >= a) and (b is None or t["exit_ts"] < b)]
    if len(rets) < 30:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    years = len(rets) / 365
    wins = [t["net_inr"] for t in tr if t["net_inr"] > 0]
    return dict(total=eq - 1, cagr=eq ** (1 / years) - 1 if eq > 0 else -1, dd=mdd, trades=len(tr),
                win=len(wins) / len(tr) if tr else 0, t_nw=s1_audit.newey_west_t(rets),
                longs=sum(t["side"] == "LONG" for t in tr), shorts=sum(t["side"] == "SHORT" for t in tr),
                net_long=sum(t["net_inr"] for t in tr if t["side"] == "LONG"),
                net_short=sum(t["net_inr"] for t in tr if t["side"] == "SHORT"),
                reasons={k: sum(t["reason"] == k for t in tr) for k in sorted({t["reason"] for t in tr})},
                days_traded=len({(t["entry_ts"] + config.IST_OFFSET) // DAY for t in tr}), days=len(rets))


def line(name, s):
    if not s:
        return f"{name}: n/a"
    return (f"{name}: {s['total']:+.1%} (CAGR {s['cagr']:+.1%}) DD {s['dd']:.1%} tNW {s['t_nw']:.2f} | "
            f"{s['trades']} trades ({s['longs']} long Rs{s['net_long']:+,.0f}, {s['shorts']} short "
            f"Rs{s['net_short']:+,.0f}) win {s['win']:.0%} | trades/day {s['trades'] / s['days']:.2f} | "
            f"exits {s['reasons']}")


def wide_basket(rows, n=40):
    """Top-n Mudrex coins by 24h volume whose contiguous daily history starts no later than the 6-coin basket's
    (young listings and stock/commodity tokens fall out on history). Uses today's volume: survivorship bias."""
    base = max(contiguous(bars(c))[0].ts for c in s1.BASKET + ["BTC"])
    out = []
    for sym in sorted(rows, key=lambda c: -float(rows[c].get("volume") or 0)):
        if len(out) >= n:
            break
        try:
            bs = contiguous(bars(sym))
        except Exception:                           # noqa: BLE001 - no candles for this symbol
            continue
        if bs and bs[0].ts <= base:
            out.append(sym)
    return out


def main():
    rows = {r["symbol"].removesuffix("USDT"): r for r in pick_coins.listing()}
    wide = len(sys.argv) > 1 and sys.argv[1] == "wide"
    basket = [c for c in wide_basket(rows) if c != "BTC"] if wide else s1.BASKET
    ab.BASKET = tuple(basket)                       # confidence = |weight| x basket size, as live
    series = {c: contiguous(bars(c)) for c in basket + ["BTC"]}
    start = max(bs[0].ts for bs in series.values())
    series = {c: [b for b in bs if b.ts >= start] for c, bs in series.items()}
    specs = {c: ab.Spec(step=float(rows[c]["quantity_step"]), min_qty=float(rows[c]["min_contract"]),
                        min_notional_usdt=float(rows[c]["min_notional_value"]),
                        max_leverage=float(rows[c]["max_leverage"])) for c in basket}
    first = start + 400 * DAY                      # warm-up: 360-day judges + BTC 200-day average
    r = ab.run_backtest(series, specs, start=first)
    print(f"Exact new live rules (adaptive_backtest.run_backtest), {len(basket)} coins, daily bars, costs = bot model")
    if wide:
        print("coins:", " ".join(basket))
    print("test window starts", __import__("time").strftime("%Y-%m-%d", __import__("time").gmtime(first)))
    print(line("DEV  (evidence)", stats(r, None, pf.SPLIT)))
    print(line("POST (descriptive)", stats(r, pf.SPLIT, None)))
    print("Legacy S1 reference (s1_audit parity, long-only no target): DEV +231.6% DD 25.1% tNW 1.79, 111 trades")


if __name__ == "__main__":
    main()
