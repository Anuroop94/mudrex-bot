"""Daily PAPER trading of S1 (champion, s1.py) and its challengers, each with its own Rs 5,000 paper account.
Never places orders.

Run once a day after 05:30 IST:  python paper_s1.py
Each run realizes every fully-known day with portfolio.trade_day (the same function behind the backtest), then
shows the orders each variant would place at today's open. Rounding uses each paper account's current equity.
Learning: challengers are frozen in advance and trade the same days. leaderboard() marks a candidate only with
>= 90 overlapping days, >= 30 closed trades, a weekly paired t above a Bonferroni-corrected bar and no worse
drawdown; monthly_review() reports it only after two monthly reviews in a row. Promotion is a human decision.
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
PROMOTE_DAYS, PROMOTE_TRADES = 90, 30
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
    # frozen 2026-09-27 after s1_audit.py (dev-period only): no better or worse in the backtest -> forward test
    "S1-mood2": ("BTC mood: re-enter only after 2 closes above the 200-day average", "s1c_mood2",
                 dict(basket=s1.BASKET, sig=s1.SIGNAL_KW, sl=s1.SL_ATR, lev=s1.LEV, mood="two")),
}
PROMOTE_T = stats_.NormalDist().inv_cdf(1 - 0.05 / (len(VARIANTS) - 1))   # Bonferroni over the challengers


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(time.time() + config.IST_OFFSET))} IST  {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def day_str(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def paths(prefix):
    return os.path.join(HERE, f"{prefix}_state.json"), os.path.join(HERE, f"{prefix}_trades.csv")


def live_like_targets(ctx, closes, d, eq_inr, specs, btc, basket):
    """Weights as fractions of the paper equity, sized like live: rounded and sized on s1.sizing_equity."""
    size = s1.sizing_equity(eq_inr)
    x = s1.targets(ctx, closes, d, size, specs, btc, basket)
    return {c: w * size / eq_inr for c, w in x.items()}


def mood_gate(st, btc, d, mode):
    """Market mood for day d: True / False / None (unknown). mode False = no filter, True = S1 (one close),
    "two" = after a close below the 200-day average, re-enter only after 2 closes above it (state kept in st)."""
    if not mode:
        return True
    m = s1.btc_mood(btc, d)
    if mode != "two" or m is None:
        return m
    streak = st.get("mood_streak", 0) + 1 if m else 0
    on = bool(m) and (st.get("mood_on", True) or streak >= 2)
    st["mood_on"], st["mood_streak"] = on, streak
    return on


def gated(st, x_all, btc, d, mode):
    """Apply the market mood: bad -> no positions, unknown -> keep held, open nothing new."""
    g = mood_gate(st, btc, d, mode)
    return {} if g is False else s1.entries_only_for_held(x_all, st["S"]["pos"]) if g is None else x_all


def run_variant(name, shared):
    desc, prefix, cfg = VARIANTS[name]
    state_path, trades_path = paths(prefix)
    today, now, last_closed = shared["today"], shared["now"], shared["today"] - DAY
    basket = cfg["basket"]
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
        x_all = gated(st, live_like_targets(ctx, closes, d, eq_inr, specs, None, basket), shared["btc"], d, cfg["mood"])
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
    want = gated(dict(st), live_like_targets(ctx, closes, last_closed, eq_inr, specs, None, basket), shared["btc"],
                 last_closed, cfg["mood"])
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


def weekly_t(diff):
    """t-stat of the paired return difference summed per 7-day block (daily t overstates: returns cluster)."""
    weeks = [sum(diff[i:i + 7]) for i in range(0, len(diff) - len(diff) % 7, 7)]
    sd = stats_.pstdev(weeks) if len(weeks) > 1 else 0
    return stats_.mean(weeks) / (sd / math.sqrt(len(weeks))) if sd else 0.0


def closed_trades(prefix):
    path = paths(prefix)[1]
    if not os.path.exists(path):
        return 0
    with open(path, newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def leaderboard():
    """Forward paper stats per variant vs the champion. A challenger is a promotion CANDIDATE only with
    >= PROMOTE_DAYS overlapping days, >= PROMOTE_TRADES closed trades, weekly paired t >= PROMOTE_T (one-sided 5%
    Bonferroni-corrected for the number of challengers) and a max drawdown no worse than S1's + 2 points."""
    ledgers = {}
    for name, (desc, prefix, _) in VARIANTS.items():
        sp = paths(prefix)[0]
        led = {}
        if os.path.exists(sp):
            with open(sp) as f:
                led = {r["date"]: r["ret"] for r in json.load(f).get("ledger", [])}
        ledgers[name] = (desc, prefix, led)
    champ = ledgers[CHAMPION][2]
    out = []
    for name, (desc, prefix, led) in ledgers.items():
        rets = [led[d] for d in sorted(led)]
        eq, peak, mdd = 1.0, 1.0, 0.0
        for r in rets:
            eq *= 1 + r
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
        row = dict(name=name, desc=desc, days=len(rets), total=eq - 1, max_dd=mdd, champion=name == CHAMPION,
                   trades=closed_trades(prefix))
        out.append(row)
    champ_dd = next(r["max_dd"] for r in out if r["champion"])
    for row in out:
        if row["champion"]:
            continue
        led = ledgers[row["name"]][2]
        common = sorted(set(led) & set(champ))
        t = weekly_t([led[d] - champ[d] for d in common])
        row.update(vs_champion_t=t, overlap_days=len(common),
                   promote=(len(common) >= PROMOTE_DAYS and row["trades"] >= PROMOTE_TRADES and t >= PROMOTE_T
                            and row["max_dd"] <= champ_dd + 0.02))
    return out


def monthly_review(today, rows, path=None):
    """Promotion is looked at only on the 1st of each month, and a candidate must pass two reviews in a row
    (a month of quarantine) before it is reported. Returns the confirmed names (a human still decides)."""
    path = path or os.path.join(HERE, "s1_promotion.json")
    if day_str(today)[8:] != "01":
        return []
    prev = json.load(open(path)) if os.path.exists(path) else {}
    now = [r["name"] for r in rows if r.get("promote")]
    confirmed = [n for n in now if n in prev.get("candidates", [])]
    with open(path, "w") as f:
        json.dump(dict(reviewed=day_str(today), candidates=now), f)
    return confirmed


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
    rows = leaderboard()
    for name in monthly_review(today, rows):
        r = next(x for x in rows if x["name"] == name)
        log(f"PROMOTION CANDIDATE (passed 2 monthly reviews): {name} beats S1 (weekly t={r['vs_champion_t']:.2f}, "
            f"{r['trades']} trades, {r['overlap_days']} days). A human decides; change s1.py only after review.")


if __name__ == "__main__":
    run()
