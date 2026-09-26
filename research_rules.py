"""User-requested rule changes to S1, each tested alone vs the baseline at Rs 5,000 with exact Mudrex order rounding.
Run: python research_rules.py
"""
import config
import data
import pick_coins
import portfolio as pf
from research_small import BASKET, EQUITY_INR

VARIANTS = [
    # label, signal kwargs, sim kwargs
    ("B0 S1 baseline (long-only, trend exit, 1x)", dict(), dict()),
    ("B1 + short selling", dict(allow_short=True), dict()),
    ("B2 + fixed stop 2xATR / target 4xATR", dict(), dict(sl_atr=2, tp_atr=4)),
    ("B3 + shorts + fixed stop/target", dict(allow_short=True), dict(sl_atr=2, tp_atr=4)),
    ("B4 + fixed stop 1.5xATR / target 3xATR", dict(), dict(sl_atr=1.5, tp_atr=3)),
    ("B5 leverage 2x", dict(), dict(lev=2)),
    ("B6 leverage 3x", dict(), dict(lev=3)),
    ("B7 leverage 5x", dict(), dict(lev=5)),
    ("B8 shorts + stop/target + 3x (all requests)", dict(allow_short=True), dict(sl_atr=2, tp_atr=4, lev=3)),
]


def main():
    rows = {r["symbol"].removesuffix("USDT"): r for r in pick_coins.listing()}
    specs = {c: dict(step=float(rows[c]["quantity_step"]), min_qty=float(rows[c]["min_contract"]),
                     min_notional=float(rows[c]["min_notional_value"])) for c in BASKET}
    uni = {c: data.load(2400, f"{c}/USDT", "1d", pf.DAY) for c in BASKET}
    closes = {c: {x[0]: x[4] for x in cs} for c, cs in uni.items()}
    tf = pf.basket_targets(BASKET, closes, EQUITY_INR / config.INR_PER_USDT, specs)
    for label, sig_kw, sim_kw in VARIANTS:
        out = []
        for period, a, b in (("DEV", None, pf.SPLIT), ("HOLD", pf.SPLIT, None)):
            r = pf.simulate_trades(uni, pf.zarattini, tf, a, b, target_vol=0.5, **sig_kw, **sim_kw)
            worst = min(r["daily"]) * EQUITY_INR
            out.append(f"{period} {r['total']:+8.1%} CAGR {r['cagr']:+6.1%} Sh {r['sharpe']:5.2f} t {r['tstat']:5.2f} "
                       f"DD {r['max_dd']:5.1%} trades {r['trades']:>3} win {r['win_rate']:4.0%} worst day Rs {worst:+5.0f}"
                       + (" WIPED" if r["min_equity"] <= 0.01 else ""))
        print(f"{label:<46} " + " | ".join(out), flush=True)


if __name__ == "__main__":
    main()
