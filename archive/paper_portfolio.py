"""Daily PAPER trading, champion vs challengers. Never places orders: this file contains no order endpoint.

Champion Z8: Zarattini-style ensemble trend (Donchian 5/10/20/30/60d, midpoint trailing stop), vol-targeted 25%,
long-only, rotating top-40 Mudrex coins by 30-day median $ volume. Challengers are research variants traded in
parallel on the same days and coins. Every day is new, unseen data, so the comparison cannot be overfit.
Promotion rule (see leaderboard()): challenger beats champion with paired daily t-stat >= 2 over >= 90 days.

Run once a day after 00:00 UTC (05:30 IST):  python paper_portfolio.py
Uses portfolio.targets/day_pnl, the exact functions behind the research numbers
(test_core.test_portfolio_paper_matches_simulate proves parity). Realized days are appended to each ledger and
never recomputed, so a later universe change cannot rewrite history. Missed days are caught up in order.
"""
import csv
import json
import math
import os
import statistics as st_
import time

import config
import data
import portfolio as pf

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "portfolio.log")
DAY = pf.DAY
PAPER_EQUITY_INR = 100_000   # holds up to 40 coins at ~0.5-2.5% each; smaller accounts fall below $5 minimums
LEDGER_FIELDS = ["date", "return", "equity_inr", "positions", "gross_exposure", "best", "worst"]
FAST, ALL = (5, 10, 20, 30, 60), (5, 10, 20, 30, 60, 90, 150, 250, 360)
CHAMPION = "Z8"
PROMOTE_T, PROMOTE_DAYS = 2.0, 90

# name: (description, file prefix, params). Z8 keeps its original "portfolio" files.
STRATEGIES = {
    "Z8":  ("top40, fast lookbacks, long-only (champion)", "portfolio", dict(top_n=40, lookbacks=FAST)),
    "Z12": ("top40, fast lookbacks, 50% rebalance band", "pf_Z12", dict(top_n=40, lookbacks=FAST, band=0.5)),
    "Z10": ("top40, all 9 lookbacks, 25% band", "pf_Z10", dict(top_n=40, lookbacks=ALL, band=0.25)),
    "Z11": ("top20, fast lookbacks, 25% band", "pf_Z11", dict(top_n=20, lookbacks=FAST, band=0.25)),
    "Z1":  ("paper original: top20, all lookbacks", "pf_Z1", dict(top_n=20, lookbacks=ALL)),
    "ZLS": ("top40, fast lookbacks, long/short", "pf_ZLS", dict(top_n=40, lookbacks=FAST, allow_short=True)),
}
# Parity test / legacy callers
LOOKBACKS, TOP_N = FAST, 40


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(time.time() + config.IST_OFFSET))} IST  {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def day_str(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def paths(prefix):
    return (os.path.join(HERE, f"{prefix}_state.json"), os.path.join(HERE, f"{prefix}_ledger.csv"))


# ---------- core (pure; shared with the parity test)

def new_state():
    return dict(equity=1.0, w={}, w_prev={}, w_day=None, last_decision=None)


def advance(st, ctx, decision_days, on_day=None, top_n=TOP_N, band=0.0):
    """Process each decision day d in order: realize the weights held since st['w_day'] up to d+1 open,
    then set new targets from d's close, switched at d+1 open. on_day(d_open, r, per) receives realized days."""
    for d in decision_days:
        d1 = d + DAY
        if st["w_day"] is not None:
            r, per = pf.day_pnl(ctx, st["w"], st["w_prev"], st["w_day"], d1)
            st["equity"] *= 1 + r
            if on_day:
                on_day(st["w_day"], r, per)
        target = pf.targets(ctx, d, st["w"], top_n, band=band)
        st["w_prev"], st["w"], st["w_day"], st["last_decision"] = st["w"], target, d1, d


# ---------- live paper run

def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    st = new_state()
    st.update(started=time.time(), start_inr=PAPER_EQUITY_INR)
    return st


def save_state(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, path)


def run_one(name, uni, today, now, forming_cache):
    desc, prefix, params = STRATEGIES[name]
    params = dict(params)
    top_n, band = params.pop("top_n"), params.pop("band", 0.0)
    state_path, ledger_path = paths(prefix)
    st = load_state(state_path)
    ctx = pf.prepare(uni, pf.zarattini, **params)
    closed_days = sorted({x[0] for cs in uni.values() for x in cs})
    last_closed = closed_days[-1]
    if st["last_decision"] is None:
        todo = [last_closed]
        log(f"[{name}] started: {desc}; Rs {PAPER_EQUITY_INR:,}; universe {len(uni)} coins")
    else:
        todo = [d for d in closed_days if d > st["last_decision"]]

    # today's open (switch price for the newest decision) from the forming candle; fetched once per coin per run
    for c in set(st["w"]) | set(pf.targets(ctx, last_closed, st["w"], top_n, band=band)):
        if c not in forming_cache:
            f = [x for x in data.fetch(today, now, f"{c}/USDT", "1d", DAY) if x[0] == today]
            forming_cache[c] = f[0][1] if f else None
        if forming_cache[c] is not None:
            ctx["opens"].setdefault(c, {})[today] = forming_cache[c]

    rows = []

    def record(d_open, r, per):
        ranked = sorted(per.items(), key=lambda kv: kv[1])
        rows.append(dict(date=day_str(d_open), **{"return": f"{r:+.5f}"},
                         equity_inr=f"{st['equity'] * st['start_inr']:.2f}", positions=len(st["w"]),
                         gross_exposure=f"{sum(abs(x) for x in st['w'].values()):.3f}",
                         best=f"{ranked[-1][0]} {ranked[-1][1]:+.5f}" if ranked else "",
                         worst=f"{ranked[0][0]} {ranked[0][1]:+.5f}" if ranked else ""))
        log(f"[{name}] {day_str(d_open)} realized {r:+.3%} -> equity Rs {st['equity'] * st['start_inr']:,.0f}")

    advance(st, ctx, todo, record, top_n, band)
    if rows:
        new = not os.path.exists(ledger_path)
        with open(ledger_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)

    eq_usdt = st["equity"] * st["start_inr"] / config.INR_PER_USDT
    book = []
    for c, x in sorted(st["w"].items(), key=lambda kv: -abs(kv[1])):
        px = ctx["opens"].get(c, {}).get(st["w_day"])
        notional = x * eq_usdt
        book.append(dict(coin=c, weight=x, notional_usdt=notional, price=px,
                         qty=notional / px if px else None, below_min=abs(notional) < config.MIN_NOTIONAL))
    added = sorted(set(st["w"]) - set(st["w_prev"]))
    dropped = sorted(set(st["w_prev"]) - set(st["w"]))
    st["snapshot"] = dict(at=now, book=book, added=added, dropped=dropped, universe=len(uni),
                          effective=day_str(st["w_day"]), name=name, desc=desc)
    save_state(state_path, st)
    gross = sum(abs(x) for x in st["w"].values())
    log(f"[{name}] book from {day_str(st['w_day'])} open: {len(st['w'])} positions, gross {gross:.1%}"
        f"{'; added ' + ', '.join(added) if added else ''}{'; dropped ' + ', '.join(dropped) if dropped else ''}; "
        f"processed {len(todo)} day(s)")
    return set(st["w"]) | set(st["w_prev"])


def read_ledger(prefix):
    path = paths(prefix)[1]
    if not os.path.exists(path):
        return {}
    with open(path, newline="") as f:
        return {r["date"]: float(r["return"]) for r in csv.DictReader(f)}


def leaderboard():
    """Forward (paper) stats per strategy + promotion test vs champion on overlapping days."""
    champ = read_ledger(STRATEGIES[CHAMPION][1])
    out = []
    for name, (desc, prefix, _) in STRATEGIES.items():
        led = read_ledger(prefix)
        rets = list(led.values())
        eq = math.prod(1 + r for r in rets) if rets else 1.0
        peak, mdd, e = 1.0, 0.0, 1.0
        for r in rets:
            e *= 1 + r
            peak = max(peak, e)
            mdd = max(mdd, 1 - e / peak)
        sd = st_.pstdev(rets) if len(rets) > 1 else 0
        row = dict(name=name, desc=desc, days=len(rets), total=eq - 1, max_dd=mdd,
                   sharpe=st_.mean(rets) / sd * math.sqrt(365) if sd else 0.0, champion=name == CHAMPION)
        if name != CHAMPION:
            common = sorted(set(led) & set(champ))
            diff = [led[d] - champ[d] for d in common]
            dsd = st_.pstdev(diff) if len(diff) > 1 else 0
            t = st_.mean(diff) / (dsd / math.sqrt(len(diff))) if dsd else 0.0
            row.update(vs_champion_t=t, overlap_days=len(common),
                       promote=len(common) >= PROMOTE_DAYS and t >= PROMOTE_T)
        out.append(row)
    return out


def main():
    now = int(time.time())
    today = now // DAY * DAY
    uni = pf.load_universe()
    held = set()
    for name, (_, prefix, _) in STRATEGIES.items():
        sp = paths(prefix)[0]
        if os.path.exists(sp):
            with open(sp) as f:
                s = json.load(f)
            held |= set(s["w"]) | set(s["w_prev"])
    for c in held - set(uni):                 # coins we hold must stay loaded even if volume fell
        try:
            uni[c] = data.load(2400, f"{c}/USDT", "1d", DAY)
        except Exception as e:
            log(f"WARNING: cannot load held coin {c}: {e}")
    forming_cache = {}
    for name in STRATEGIES:
        try:
            run_one(name, uni, today, now, forming_cache)
        except Exception as e:                 # one broken challenger must not stop the champion
            log(f"[{name}] ERROR {type(e).__name__}: {e}")
    for r in leaderboard():
        if r.get("promote"):
            log(f"PROMOTION CANDIDATE: {r['name']} beats {CHAMPION} (t={r['vs_champion_t']:.2f} "
                f"over {r['overlap_days']} days). Review before switching.")


if __name__ == "__main__":
    main()
