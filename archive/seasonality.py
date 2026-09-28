"""Intraday seasonality (Quantpedia: BTC strongest 21:00-23:00 UTC). Development data only.
For each coin: mean return per UTC hour, and the net result of "long from HOUR_START open to HOUR_END open daily",
paying a full round trip (2x fee+GST+slippage) every day. Run: python seasonality.py
"""
import statistics as st

import config
import data
import portfolio as pf

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "LINK", "LTC", "AVAX"]
WINDOWS = [(21, 23), (22, 24), (20, 24)]


def main():
    rt_cost = 2 * pf.cost_per_turnover()
    print(f"round-trip cost per day traded: {rt_cost:.3%}\n")
    for c in COINS:
        cs = [x for x in data.load(2400, f"{c}/USDT", "1h", 3600) if x[0] < pf.SPLIT]
        by_hour = {h: [] for h in range(24)}
        opens = {x[0]: x[1] for x in cs}
        for x in cs:
            by_hour[(x[0] // 3600) % 24].append(x[4] / x[1] - 1)
        best = sorted(((st.mean(v), h) for h, v in by_hour.items() if v), reverse=True)[:3]
        line = f"{c:<5} {len(cs) // 24:>5}d  best hours: " + ", ".join(f"{h:02d}h {m:+.3%}" for m, h in best)
        for a, b in WINDOWS:
            rets = []
            for day in sorted({x[0] // 86400 * 86400 for x in cs}):
                o1, o2 = opens.get(day + a * 3600), opens.get(day + b * 3600)
                if o1 and o2:
                    rets.append(o2 / o1 - 1 - rt_cost)
            if rets:
                t = st.mean(rets) / (st.pstdev(rets) / len(rets) ** 0.5)
                line += f" | {a}-{b}h net {st.mean(rets):+.3%}/day t={t:+.2f}"
        print(line, flush=True)


if __name__ == "__main__":
    main()
