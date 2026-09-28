"""Small-capital research (Rs 5,000). Fixed basket of coins whose Mudrex minimum order is ~10% of Rs 5,000.
Strategies from papers, fixed before running. Every weight is rounded exactly as Mudrex would accept it.
Run: python research_small.py
"""
import config
import data
import pick_coins
import portfolio as pf

EQUITY_INR = 5000
BASKET = ["XRP", "ADA", "DOGE", "LINK", "AVAX", "TRX"]   # min order Rs 510, fine quantity steps
FAST = (5, 10, 20, 30, 60)
CANDIDATES = {
    "S1 Zarattini ensemble, long-only, vol 50%": (pf.zarattini, dict(target_vol=0.5)),
    "S2 Zarattini fast lookbacks, long-only, vol 50%": (pf.zarattini, dict(target_vol=0.5, lookbacks=FAST)),
    "S3 TSMOM EMA 3 horizons, long-only, vol 50%": (pf.ema_tsmom, dict(target_vol=0.5)),
    "S4 TSMOM EMA 3 horizons, long/short, vol 50%": (pf.ema_tsmom, dict(target_vol=0.5, allow_short=True)),
}


def main():
    rows = {r["symbol"].removesuffix("USDT"): r for r in pick_coins.listing()}
    specs = {c: dict(step=float(rows[c]["quantity_step"]), min_qty=float(rows[c]["min_contract"]),
                     min_notional=float(rows[c]["min_notional_value"])) for c in BASKET}
    uni = {c: data.load(2400, f"{c}/USDT", "1d", pf.DAY) for c in BASKET}
    closes = {c: {x[0]: x[4] for x in cs} for c, cs in uni.items()}
    eq_usd = EQUITY_INR / config.INR_PER_USDT
    for name, (fn, kw) in CANDIDATES.items():
        for label, e, s in (("ideal sizes ", None, None), ("Rs5000 exact", eq_usd, specs)):
            tf = pf.basket_targets(BASKET, closes, e, s)
            for period, a, b in (("DEV", None, pf.SPLIT), ("HOLDOUT", pf.SPLIT, None)):
                r = pf.simulate_targets(uni, fn, tf, a, b, **kw)
                exposure = sum(1 for x in r["daily"] if x) / max(len(r["daily"]), 1)
                print(f"{name:<48} {label} {period:<7} {pf.fmt(r)}  in-market {exposure:4.0%}", flush=True)
    hold = pf.basket_targets(BASKET, closes)
    for period, a, b in (("DEV", None, pf.SPLIT), ("HOLDOUT", pf.SPLIT, None)):
        r = pf.simulate_targets(uni, lambda cs: {x[0]: 1.0 for x in cs}, hold, a, b)
        print(f"{'benchmark: hold the 6 coins equally':<48} {'':12} {period:<7} {pf.fmt(r)}")


if __name__ == "__main__":
    main()
