"""Daily PAPER trading of S1 (champion, s1.py) and its challengers, each with its own Rs 5,000 paper account.
Never places orders.

Run once a day after 05:30 IST:  python paper_s1.py
Each run realizes every fully-known day with portfolio.trade_day (the same function behind the backtest), then
shows the orders each variant would place at today's open. Rounding uses each paper account's current equity.
Learning: challengers trade the same days; leaderboard() flags one that beats S1 by paired daily t >= 2 over
>= 90 overlapping days. Promotion is a human decision (review, then change s1.py).
"""
import csv
import json
import math
import os
import statistics as stats_
import time

import config
import data
import pick_coins
import portfolio as pf
import s1

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "s1_paper.log")
DAY = pf.DAY
FAST = (5, 10, 20, 30, 60)
EXTRA = ["LTC", "BNB", "SUI", "SOL"]
CHAMPION = "S1"
PROMOTE_T, PROMOTE_DAYS = 2.0, 90
VARIANTS = {   # name: (description, file prefix, settings)
    "S1": ("champion: 6 coins, BTC mood filter, stop 3xATR, 2x", "s1_paper",
           dict(basket=s1.BASKET, sig=s1.SIGNAL_KW, sl=s1.SL_ATR, lev=s1.LEV, mood=True)),
    "S1-nomood": ("same without BTC mood filter", "s1c_nomood",
                  dict(basket=s1.BASKET, sig=s1.SIGNAL_KW, sl=s1.SL_ATR, lev=s1.LEV, mood=False)),
    "S1-fast": ("fast trend judges only (5-60 days)", "s1c_fast",
                dict(basket=s1.BASKET, sig=dict(s1.SIGNAL_KW, lookbacks=FAST), sl=s1.SL_ATR, lev=s1.LEV, mood=True)),
    "S1-stop4": ("wider safety stop 4xATR", "s1c_stop4",
                 dict(basket=s1.BASKET, sig=s1.SIGNAL_KW, sl=4, lev=s1.LEV, mood=True)),
    "S1-10coins": ("10 coins (+LTC, BNB, SUI, SOL)", "s1c_10coins",
                   dict(basket=s1.BASKET + EXTRA, sig=s1.SIGNAL_KW, sl=s1.SL_ATR, lev=s1.LEV, mood=True)),
}


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(time.time() + config.IST_OFFSET))} IST  {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def day_str(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def paths(prefix):
    return os.path.join(HERE, f"{prefix}_state.json"), os.path.join(HERE, f"{prefix}_trades.csv")


def run_variant(name, shared):
    desc, prefix, cfg = VARIANTS[name]
    state_path, trades_path = paths(prefix)
    today, now, last_closed = shared["today"], shared["now"], shared["today"] - DAY
    basket, btc = cfg["basket"], (shared["btc"] if cfg["mood"] else None)
    key = json.dumps(cfg["sig"], sort_keys=True, default=list)
    if key not in shared["ctx"]:
        shared["ctx"][key] = pf.prepare(shared["closed_uni"], pf.zarattini, **cfg["sig"])
    ctx, closes, specs, bars, atrs = shared["ctx"][key], shared["closes"], shared["specs"], shared["bars"], shared["atrs"]

    if os.path.exists(state_path):
        with open(state_path) as f:
            st = json.load(f)
    else:
        st = dict(S=pf.new_trade_state(basket), start_inr=s1.CAPITAL_CAP_INR, last_decision=last_closed - DAY,
                  started=now, ledger=[])
        log(f"[{name}] paper trading started: {desc}; Rs {s1.CAPITAL_CAP_INR:,}")

    S, n_closed = st["S"], len(st["S"]["closed"])
    d = st["last_decision"] + DAY
    while d + DAY <= last_closed:   # d1 = d+1 must be a CLOSED bar; d2 = d+2 <= today, so its open is known
        eq_inr = S["equity"] * st["start_inr"]
        x_all = s1.targets(ctx, closes, d, eq_inr, specs, btc, basket)
        r = pf.trade_day(S, bars, atrs, basket, x_all, d, d + DAY, d + 2 * DAY, cfg["sl"], 0, cfg["lev"])
        st["ledger"].append(dict(date=day_str(d + DAY), ret=r, equity_inr=S["equity"] * st["start_inr"],
                                 positions=len(S["pos"])))
        log(f"[{name}] {day_str(d + DAY)} realized {r:+.3%} -> Rs {S['equity'] * st['start_inr']:,.0f}; "
            f"{len(S['pos'])} open")
        st["last_decision"] = d
        d += DAY

    new = S["closed"][n_closed:]
    if new:
        exists = os.path.exists(trades_path)
        with open(trades_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["coin", "entry_day", "exit_day", "entry", "exit", "reason", "pnl_inr"])
            if not exists:
                w.writeheader()
            for t in new:
                pnl_inr = t["pnl"] * st["start_inr"]
                w.writerow(dict(coin=t["coin"], entry_day=day_str(t["entry_day"]), exit_day=day_str(t["exit_day"]),
                                entry=f"{t['entry']:.6g}", exit=f"{t['exit']:.6g}", reason=t["reason"],
                                pnl_inr=f"{pnl_inr:+.2f}"))
                log(f"[{name}] CLOSED {t['coin']} {t['reason']}: {t['entry']:.6g} -> {t['exit']:.6g}, "
                    f"Rs {pnl_inr:+.1f}")
    S["closed"] = []   # persisted in CSV

    eq_inr = S["equity"] * st["start_inr"]
    want = s1.targets(ctx, closes, last_closed, eq_inr, specs, btc, basket)
    plan = []
    for c in basket:
        w, held = want.get(c, 0.0), c in S["pos"]
        if held and w <= 0:
            plan.append(f"SELL {c} (trend exit)")
        elif not held and w > 0 and S["armed"].get(c, True):
            plan.append(f"BUY {c} ~Rs {w * cfg['lev'] * eq_inr:,.0f} (stop {cfg['sl']}xATR)")
        elif held:
            plan.append(f"HOLD {c}")
    st["snapshot"] = dict(at=now, equity_inr=eq_inr, plan=plan, decision=day_str(last_closed), name=name, desc=desc,
                          mood_ok=s1.btc_mood_ok(shared["btc"], last_closed),
                          positions={c: dict(entry=p["entry"], sl=p["sl"], value_inr=p["w"] * eq_inr,
                                             since=day_str(p["day"])) for c, p in S["pos"].items()})
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, state_path)
    log(f"[{name}] equity Rs {eq_inr:,.0f}; plan for {day_str(today)} open: "
        f"{'; '.join(plan) or 'no positions, no orders'}")


def leaderboard():
    """Forward paper stats per variant + paired daily t-stat vs the champion on overlapping days."""
    ledgers = {}
    for name, (desc, prefix, _) in VARIANTS.items():
        sp = paths(prefix)[0]
        led = {}
        if os.path.exists(sp):
            with open(sp) as f:
                led = {r["date"]: r["ret"] for r in json.load(f).get("ledger", [])}
        ledgers[name] = (desc, led)
    champ = ledgers[CHAMPION][1]
    out = []
    for name, (desc, led) in ledgers.items():
        rets = list(led.values())
        eq, peak, mdd = 1.0, 1.0, 0.0
        for r in rets:
            eq *= 1 + r
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
        row = dict(name=name, desc=desc, days=len(rets), total=eq - 1, max_dd=mdd, champion=name == CHAMPION)
        if name != CHAMPION:
            common = sorted(set(led) & set(champ))
            diff = [led[d] - champ[d] for d in common]
            sd = stats_.pstdev(diff) if len(diff) > 1 else 0
            t = stats_.mean(diff) / (sd / math.sqrt(len(diff))) if sd else 0.0
            row.update(vs_champion_t=t, overlap_days=len(common),
                       promote=len(common) >= PROMOTE_DAYS and t >= PROMOTE_T)
        out.append(row)
    return out


def run():
    now = int(time.time())
    today = now // DAY * DAY
    coins = sorted({c for _, _, cfg in VARIANTS.values() for c in cfg["basket"]})
    uni = {c: data.load(2400, f"{c}/USDT", "1d", DAY) for c in coins}
    closed_uni = {c: [x for x in cs if x[0] < today] for c, cs in uni.items()}
    for c in coins:   # today's open = fill price for decisions on the last closed day
        f = [x for x in data.fetch(today, now, f"{c}/USDT", "1d", DAY) if x[0] == today]
        if f:
            uni[c] = closed_uni[c] + [[today, f[0][1], f[0][1], f[0][1], f[0][1], 0.0]]
    bars, atrs = pf.trade_lookups(uni)
    shared = dict(now=now, today=today, closed_uni=closed_uni, bars=bars, atrs=atrs, ctx={},
                  closes={c: {x[0]: x[4] for x in cs} for c, cs in closed_uni.items()},
                  specs=s1.specs_from_listing(pick_coins.listing(), coins),
                  btc=[x for x in data.load(2400, "BTC/USDT", "1d", DAY) if x[0] < today])
    for name in VARIANTS:
        try:
            run_variant(name, shared)
        except Exception as e:           # a broken challenger must never stop the champion
            log(f"[{name}] ERROR {type(e).__name__}: {e}")
    for r in leaderboard():
        if r.get("promote"):
            log(f"PROMOTION CANDIDATE: {r['name']} beats S1 (t={r['vs_champion_t']:.2f} over {r['overlap_days']} days)")


if __name__ == "__main__":
    run()
