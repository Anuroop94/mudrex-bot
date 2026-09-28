"""Strategy research on the whole Mudrex universe with a locked hold-out.

  python research.py dev        -> every candidate on development data only (< portfolio.SPLIT)
  python research.py holdout Z1 Z3   -> named finalists on the hold-out, ONCE. Pass bar (fixed in advance):
        t-stat >= 1.5, PF(daily) >= 1.10, >= 55% of traded coins net-profitable, after all costs.
"""
import sys
import time

import portfolio as pf

FAST = (5, 10, 20, 30, 60)
CANDIDATES = {
    "Z1 Zarattini ensemble, long-only, top20, vol 25%": dict(fn=pf.zarattini, top_n=20),
    "Z2 same, long/short":                              dict(fn=pf.zarattini, top_n=20, allow_short=True),
    "Z3 long-only, top10":                              dict(fn=pf.zarattini, top_n=10),
    "Z4 long-only, top40":                              dict(fn=pf.zarattini, top_n=40),
    "Z5 long-only, top20, vol 40%":                     dict(fn=pf.zarattini, top_n=20, target_vol=0.40),
    "Z6 long-only, fast lookbacks only (5-60d)":        dict(fn=pf.zarattini, top_n=20, lookbacks=(5, 10, 20, 30, 60)),
    "Z7 long-only, slow lookbacks only (60-360d)":      dict(fn=pf.zarattini, top_n=20, lookbacks=(60, 90, 150, 250, 360)),
    # round 2 (development only): combine breadth (Z4) with fast ensemble (Z6); cut churn with a rebalance band
    "Z8 top40, fast lookbacks":                         dict(fn=pf.zarattini, top_n=40, lookbacks=FAST),
    "Z9 top40, fast lookbacks, band 25%":               dict(fn=pf.zarattini, top_n=40, lookbacks=FAST, band=0.25),
    "Z10 top40, all lookbacks, band 25%":               dict(fn=pf.zarattini, top_n=40, band=0.25),
    "Z11 top20, fast lookbacks, band 25%":              dict(fn=pf.zarattini, top_n=20, lookbacks=FAST, band=0.25),
    "Z12 top40, fast lookbacks, band 50%":              dict(fn=pf.zarattini, top_n=40, lookbacks=FAST, band=0.50),
}


def run(names, start, end, uni):
    for name in names:
        cfg = dict(CANDIDATES[name])
        fn, top_n = cfg.pop("fn"), cfg.pop("top_n")
        t0 = time.time()
        r = pf.simulate(uni, fn, top_n, start, end, **cfg)
        traded = [v for v in r["contrib"].values() if v]
        share = sum(v > 0 for v in traded) / len(traded) if traded else 0
        passed = r["tstat"] >= 1.5 and r["pf"] >= 1.10 and share >= 0.55
        print(f"{name:<50} {pf.fmt(r)}  coins+ {share:4.0%} of {len(traded):>3}"
              f"{'  PASS' if passed else ''}  ({time.time() - t0:.0f}s)", flush=True)
    bh = pf.buy_hold(uni, start, end)
    print(f"{'benchmark: equal-weight top20, always long':<50} {pf.fmt(bh)}")
    if "BTC" in uni:
        b = pf.simulate({"BTC": uni["BTC"]}, lambda cs: {x[0]: 1.0 for x in cs}, 1, start, end)
        print(f"{'benchmark: BTC buy & hold':<50} {pf.fmt(b)}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "dev"
    print("loading universe...", flush=True)
    uni = pf.load_universe()
    span = time.strftime("%Y-%m-%d", time.gmtime(min(cs[0][0] for cs in uni.values())))
    print(f"{len(uni)} coins with daily data (earliest {span})\n", flush=True)
    if mode == "dev":
        print(f"== DEVELOPMENT: data before {time.strftime('%Y-%m-%d', time.gmtime(pf.SPLIT))} ==")
        only = [n for n in CANDIDATES if n.split()[0] in sys.argv[2:]] or list(CANDIDATES)
        run(only, None, pf.SPLIT, uni)
    else:
        picks = [n for n in CANDIDATES if n.split()[0] in sys.argv[2:]]
        print(f"== HOLD-OUT (one look): {time.strftime('%Y-%m-%d', time.gmtime(pf.SPLIT))} onward ==")
        run(picks, pf.SPLIT, None, uni)
