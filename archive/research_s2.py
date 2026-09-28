"""Test S2 (2-5 trades/day, self-learning ranking, 5% day caps) at Rs 5,000. Run: python research_s2.py"""
import time

import data
import pick_coins
import portfolio as pf
import s1
import s2

COINS = s1.BASKET


def main():
    t0 = time.time()
    all_setups, hourly = [], {}
    for c in COINS:
        cs = data.load(2400, f"{c}/USDT", "1h", 3600)
        hourly[c] = {x[0]: (x[1], x[2], x[3], x[4]) for x in cs}
        su = s2.setups(c, cs)
        all_setups += su
        print(f"{c}: {len(cs)} hourly bars, {len(su)} setups, raw win rate {sum(r['y'] for r in su) / len(su):.0%}",
              flush=True)
    s2.score_walk_forward(all_setups)
    rows = {r["symbol"].removesuffix("USDT"): r for r in pick_coins.listing()}
    specs = {c: dict(min_notional=float(rows[c]["min_notional_value"]), min_qty=float(rows[c]["min_contract"]))
             for c in COINS}
    scored = [r for r in all_setups if r.get("p") is not None]
    top = [r for r in scored if r["p"] >= r["cut"]]
    print(f"\nscored setups {len(scored)}; win rate all {sum(r['y'] for r in scored) / len(scored):.1%} vs "
          f"model-approved {sum(r['y'] for r in top) / max(len(top), 1):.1%} "
          f"(avg R {sum(r['R'] for r in scored) / len(scored):+.3f} vs {sum(r['R'] for r in top) / max(len(top), 1):+.3f})\n")
    first_scored = min(r["t"] for r in scored)
    for label, use in (("WITH learning", True), ("WITHOUT learning", False)):
        for period, a, b in (("DEV", first_scored, pf.SPLIT), ("HOLDOUT", pf.SPLIT, None)):
            s = s2.simulate(all_setups, hourly, use_model=use, start=a, end=b, specs=specs)
            print(f"{label:<17} {period:<7} total {s['total']:+8.1%}  CAGR {s['cagr']:+7.1%}  Sharpe {s['sharpe']:5.2f}  "
                  f"maxDD {s['max_dd']:5.1%}  trades/day {s['trades_per_day']:.1f}  win {s['win_rate']:.0%}  "
                  f"worst day {s['worst_day']:+.1%}  best day {s['best_day']:+.1%}  "
                  f"cap days +{s['cap_profit_days']}/-{s['cap_loss_days']} of {s['days']}", flush=True)
    print(f"\n({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
