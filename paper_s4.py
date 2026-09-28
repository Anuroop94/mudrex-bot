"""PAPER trading of S4 (s4.py), the intraday momentum set trader. Never places orders.

Same rules as live S4 via s4_research.run (the backtest engine): strongest 24h mover, LONG above / SHORT below the
BTC 200-day average, TP 2x / SL 1x daily ATR, max 72h, one set at a time, Rs150 risk per set, <= 3 sets and the
Rs500 line per IST day, fees + GST + slippage + funding. Fresh Rs 5,000 paper account from START.
Coins: the top 40 by turnover, frozen on the first run (live S4 uses live_universe; close but not identical).
Run hourly (scheduled task MudrexPaperS4): replays every closed hour since START (deterministic, no drift).
"""
import json
import os
import time

import config
import data
import s4
import s4_research as r4

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "s4_paper_state.json")
LOG_PATH = os.path.join(HERE, "s4_paper.log")
START = 1790553600          # 2026-09-28 00:00 UTC: paper account starts here
N_COINS = 40
HOUR, DAY = r4.HOUR, r4.DAY


def ist(t):
    return time.strftime("%d %b %H:%M", time.gmtime(t + config.IST_OFFSET))


def load(coins, rows):
    """Like s4_research.load but with short histories (hourly 60 days, daily 500), so an hourly run stays cheap."""
    D = dict(coins=[], f={}, trend={}, btc=data.load(500, "BTC/USDT", "1d", DAY), specs={}, datr={})
    kw = dict(r4.s1.SIGNAL_KW, allow_short=True)
    for c in coins:
        try:
            cs = data.load(60, f"{c}/USDT", "1h", HOUR)
            daily = data.load(500, f"{c}/USDT", "1d", DAY)
            r = rows[c]
            spec = dict(min_notional=float(r["min_notional_value"]), min_qty=float(r["min_contract"]),
                        step=float(r["quantity_step"]))
        except Exception:                                           # noqa: BLE001 - delisted / no candles
            continue
        D["f"][c] = s4.coin_features(cs)
        D["trend"][c] = r4.pf.zarattini(daily, **kw)
        D["datr"][c] = dict(zip([x[0] for x in daily], r4.strategy.atr(daily, 14)))
        D["specs"][c] = spec
        D["coins"].append(c)
    return D


def curve(r, now, value):
    """Balance after each closed trade (start + cumulative net results), then today's value incl. the open trade."""
    pts, bal = [dict(t=START, v=r4.ALLOC)], r4.ALLOC
    for t in r["trades"]:
        bal += t["net"]
        pts.append(dict(t=t["exit_t"], v=round(bal, 2)))
    return pts + [dict(t=now, v=round(value, 2))]


def run():
    old = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            old = json.load(f)
    rows = {x["symbol"].removesuffix("USDT"): x for x in r4.pick_coins.listing()}
    coins = old.get("coins") or r4.universe(N_COINS)[0]              # frozen: a changing list would rewrite history
    D = load(coins, rows)
    r = r4.run(D, s4.KIND, s4.TP_DATR, s4.SL_DATR, s4.HOLD_H, scale="d", start=START)
    now = time.time()
    today = (now + config.IST_OFFSET) // DAY
    value = r["equity"] + sum(p["u"] for p in r["open"])
    wins = [t for t in r["trades"] if t["net"] > 0]
    st = dict(at=now, coins=coins, start_inr=r4.ALLOC, equity_inr=round(r["equity"], 2), value_inr=round(value, 2),
              sets_total=r["sets"], sets_today=r["per_day"].get(today, 0), blocked_today=r["blocked"],
              wins=len(wins), losses=len(r["trades"]) - len(wins),
              curve=curve(r, now, value),
              open_set=[dict(coin=p["c"], side=p["side"], entry=p["entry"], target=p["tp"], stop=p["sl"],
                             since=ist(p["t"]), pnl_inr=round(p["u"], 2)) for p in r["open"]],
              trades=[dict(coin=t["coin"], side=t["side"], opened=ist(t["entry_t"]), closed=ist(t["exit_t"]),
                           why=t["why"], entry=t["entry"], exit=t["exit"], pnl_inr=round(t["net"], 2))
                      for t in r["trades"]][-50:])
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_PATH)
    line = (f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(now + config.IST_OFFSET))} IST  S4 paper: "
            f"Rs {value:,.0f} (incl. open), {r['sets']} sets ({st['sets_today']} today), "
            f"open: {', '.join(p['c'] + ' ' + p['side'] for p in r['open']) or 'none'}")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    run()
