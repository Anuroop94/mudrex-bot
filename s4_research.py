"""S4 intraday SET trader research (owner 2026-09-27: intraday, at least 2 trades a day, LONG and SHORT, all liquid
coins, fixed target AND stop on every trade, stop opening sets at -Rs500/+Rs500 per IST day, max 3 sets a day).

A SET = one or more coins entered at the same hour (next hour's open after a closed-hour signal), each with a fixed
take-profit and stop-loss in ATR(1h) multiples and a maximum hold. The next set starts only after every position of
the current set is closed. 24-hour cycle (the bot runs sets 1-3 without a tap). No forced trades.
Money: Rs 5,000 allocation; each set risks SET_RISK rupees at its stops (split across its coins); total notional
<= MAX_LEV x Rs 5,000; Mudrex minimum order; taker fee + GST + 0.05% slippage each side; hourly funding always paid.
DEV = before portfolio.SPLIT (evidence, split in yearly folds). POST = after (descriptive only).
Universe = today's top coins by volume with >= 2 years of daily history: survivorship bias (flatters results).

Run: python s4_research.py [n_coins]      (default 40)
"""
import calendar
import math
import sys
import time

import config
import data
import pick_coins
import portfolio as pf
import s1
import s1_audit as audit
import strategy

HOUR, DAY = 3600, 86400
RATE = config.INR_PER_USDT
FEE = config.TAKER_FEE * (1 + config.GST)
SLIP = pf.SLIPPAGE
FUND_H = config.FUNDING_PER_DAY / 24
ALLOC, MAX_LEV = 5000.0, 3
SET_RISK, DAY_STOP, MAX_SETS = 150.0, 500.0, 3
HOURLY_DAYS = 1300


def universe(n):
    rows = {r["symbol"].removesuffix("USDT"): r for r in pick_coins.listing()}
    out = []
    turnover = lambda c: float(rows[c].get("volume") or 0) * float(rows[c].get("price") or 0)  # noqa: E731
    for c in sorted(rows, key=lambda c: -turnover(c)):          # volume is in coin units: rank by USDT turnover
        if len(out) >= n:
            break
        try:
            d = data.load(2400, f"{c}/USDT", "1d", DAY)
        except Exception:                                           # noqa: BLE001 - no candles
            continue
        if len(d) >= 730 and d[-1][0] > time.time() - 3 * DAY:
            out.append(c)
    return out, rows


def load(coins, rows):
    btc = data.load(2400, "BTC/USDT", "1d", DAY)
    D = dict(coins=[], f={}, trend={}, btc=btc, specs={})
    kw = dict(s1.SIGNAL_KW, allow_short=True)
    for c in coins:
        try:
            cs = data.load(HOURLY_DAYS, f"{c}/USDT", "1h", HOUR)
            daily = data.load(2400, f"{c}/USDT", "1d", DAY)
        except Exception:                                           # noqa: BLE001
            continue
        if len(cs) < 24 * 400:
            continue
        closes = [x[4] for x in cs]
        hh, ll = pf.prior_extremes([x[2] for x in cs], 24)[0], pf.prior_extremes([x[3] for x in cs], 24)[1]
        sma, sd = [None] * len(cs), [None] * len(cs)
        for i in range(20, len(cs)):
            w = closes[i - 19:i + 1]
            m = sum(w) / 20
            sma[i], sd[i] = m, math.sqrt(sum((x - m) ** 2 for x in w) / 20)
        D["f"][c] = dict(t=[x[0] for x in cs], o=[x[1] for x in cs], h=[x[2] for x in cs], l=[x[3] for x in cs],
                         c=closes, atr=strategy.atr(cs, 14), rsi=strategy.rsi(closes, 14), hh=hh, ll=ll,
                         sma=sma, sd=sd, idx={x[0]: i for i, x in enumerate(cs)})
        D["trend"][c] = pf.zarattini(daily, **kw)
        D.setdefault("datr", {})[c] = dict(zip([x[0] for x in daily], strategy.atr(daily, 14)))
        r = rows[c]
        D["specs"][c] = dict(min_notional=float(r["min_notional_value"]), min_qty=float(r["min_contract"]),
                             step=float(r["quantity_step"]))
        D["coins"].append(c)
    return D


def setups(D, t, kind, regime_filter=True):
    """[(score, coin, side)] for signals at the CLOSE of hour t (entry at t+1's open), best first."""
    day = (t // DAY) * DAY - DAY                                    # last CLOSED daily bar
    mood = s1.btc_mood(D["btc"], day)
    if mood is None:
        return []
    out = []
    for c in D["coins"]:
        f = D["f"][c]
        i = f["idx"].get(t)
        if i is None or i < 30 or not f["atr"][i]:
            continue
        tr = D["trend"][c].get(day, 0)
        up_ok = tr > 0 and (mood or not regime_filter)
        dn_ok = tr < 0 and (not mood or not regime_filter)
        cl, a = f["c"][i], f["atr"][i]
        if kind == "BRK":
            if up_ok and f["hh"][i] and cl > f["hh"][i] and f["c"][i - 1] <= (f["hh"][i - 1] or 1e18):
                out.append(((cl - f["hh"][i]) / a, c, "LONG"))
            if dn_ok and f["ll"][i] and cl < f["ll"][i] and f["c"][i - 1] >= (f["ll"][i - 1] or 0):
                out.append(((f["ll"][i] - cl) / a, c, "SHORT"))
        elif kind == "PULL":
            if up_ok and f["rsi"][i] < 30 <= f["rsi"][i - 1]:
                out.append((30 - f["rsi"][i], c, "LONG"))
            if dn_ok and f["rsi"][i] > 70 >= f["rsi"][i - 1]:
                out.append((f["rsi"][i] - 70, c, "SHORT"))
        elif kind == "MR" and f["sd"][i]:                         # fades extremes: ignores BTC mood on purpose,
                                                                    # only refuses to fade the coin's own daily trend
            z = (cl - f["sma"][i]) / f["sd"][i]
            if z < -2.5 and f["rsi"][i] < 25 and tr >= 0:
                out.append((-z, c, "LONG"))
            if z > 2.5 and f["rsi"][i] > 75 and tr <= 0:
                out.append((z, c, "SHORT"))
        elif kind == "MOM" and i >= 24:
            r24 = cl / f["c"][i - 24] - 1
            if up_ok and r24 > 0:
                out.append((r24 / (a / cl), c, "LONG"))
            if dn_ok and r24 < 0:
                out.append((-r24 / (a / cl), c, "SHORT"))
    out.sort(reverse=True)
    if kind == "MOM":                                               # momentum: only the single strongest per side
        out = out[:1]
    return out


def run(D, kind, tp, sl, hold, max_coins=2, regime_filter=True, start=None, end=None, scale="h", target_first=False):
    """scale 'h': target/stop in 1h ATRs; 'd': in the last closed DAILY ATR (wider, so costs are a smaller share)."""
    eq, open_pos, trades, marks, ruined = ALLOC, [], [], [], None
    day, day_start, sets_today, blocked, sets = None, eq, 0, False, 0
    per_day = {}
    all_t = [min(f["t"]) for f in D["f"].values()]
    t0 = max(min(all_t) + 400 * HOUR, start or 0)
    t1 = min(max(f["t"]) for f in D["f"].values()) if end is None else end
    pending = None

    def mark():
        return eq + sum(p["u"] for p in open_pos)

    for t in range(t0 - t0 % HOUR, t1, HOUR):
        ist_day = (t + config.IST_OFFSET) // DAY
        if ist_day != day:
            if day is not None:
                marks.append((t, mark()))
            day, day_start, sets_today, blocked = ist_day, mark(), 0, False
        if ruined is None and eq < ALLOC * 0.2 and not open_pos:
            ruined = t                                              # can no longer post margin: account finished
        if pending and not open_pos and not blocked and sets_today < MAX_SETS and ruined is None:
            chosen = pending[:max_coins]
            risk_each = SET_RISK / len(chosen)
            notional_left = MAX_LEV * min(eq, ALLOC)
            for _, c, side in chosen:
                f = D["f"][c]
                i = f["idx"].get(t)
                if i is None or not f["atr"][i - 1]:
                    continue
                a = f["atr"][i - 1] if scale == "h" else D["datr"][c].get((t // DAY) * DAY - DAY)
                if not a:
                    continue
                d = 1 if side == "LONG" else -1
                px = f["o"][i] * (1 + d * SLIP)
                spec = D["specs"][c]
                qty = math.floor(risk_each / (sl * a * RATE) / spec["step"]) * spec["step"]
                qty = min(qty, math.floor(notional_left / (px * RATE) / spec["step"]) * spec["step"])
                if qty < spec["min_qty"] or qty * px < spec["min_notional"]:
                    continue
                n = qty * px * RATE
                notional_left -= n
                eq -= n * FEE
                open_pos.append(dict(c=c, side=side, d=d, qty=qty, entry=px, tp=px + d * tp * a, sl=px - d * sl * a,
                                     t=t, deadline=t + hold * HOUR, cost=n * FEE, fund=0.0, u=0.0))
            if open_pos:
                sets += 1
                sets_today += 1
                per_day[ist_day] = per_day.get(ist_day, 0) + 1
        pending = None
        still = []
        for p in open_pos:
            f = D["f"][p["c"]]
            i = f["idx"].get(t)
            if i is None:
                still.append(p)
                continue
            o, h, lo, d = f["o"][i], f["h"][i], f["l"][i], p["d"]
            if t >= p["deadline"]:
                x, why = o, "time"
            elif (o - p["sl"]) * d <= 0:
                x, why = o, "stop"
            elif (o - p["tp"]) * d >= 0:
                x, why = o, "target"
            else:
                fund = p["qty"] * o * RATE * FUND_H
                eq -= fund
                p["fund"] += fund
                hit_sl = lo <= p["sl"] if d == 1 else h >= p["sl"]
                hit_tp = h >= p["tp"] if d == 1 else lo <= p["tp"]
                if hit_sl and not (hit_tp and target_first):
                    x, why = p["sl"], "stop"                       # stop first when both touch: conservative
                elif hit_tp:
                    x, why = p["tp"], "target"
                else:
                    p["u"] = p["qty"] * (f["c"][i] - p["entry"]) * d * RATE
                    still.append(p)
                    continue
            x *= 1 - d * SLIP
            gross = p["qty"] * (x - p["entry"]) * d * RATE
            fee = p["qty"] * x * RATE * FEE
            eq += gross - fee
            trades.append(dict(coin=p["c"], side=p["side"], entry_t=p["t"], exit_t=t, why=why,
                               net=gross - fee - p["cost"] - p["fund"]))
        open_pos = still
        if abs(mark() - day_start) >= DAY_STOP:
            blocked = True                                          # owner's Rs500 line: no NEW sets today
        if not open_pos and not blocked and sets_today < MAX_SETS:
            found = setups(D, t, kind, regime_filter)
            if found:
                pending = found
    daily = [(marks[k + 1][0], marks[k + 1][1] / marks[k][1] - 1) for k in range(len(marks) - 1)]
    days = max(1, len(marks))
    return dict(daily=daily, trades=trades, sets=sets, days=days, equity=eq, ruined=ruined,
                sets_per_day=sets / days, days_with_2=sum(v >= 2 for v in per_day.values()) / days)


def folds(r):
    out = []
    for y in range(2023, 2026):
        a = calendar.timegm((y, 1, 1, 0, 0, 0))                # UTC, like pf.SPLIT (PC clock is US Eastern)
        b = min(calendar.timegm((y + 1, 1, 1, 0, 0, 0)), pf.SPLIT)
        xs = [v for t, v in r["daily"] if a <= t < b]
        if len(xs) > 30:
            eq = 1.0
            for v in xs:
                eq *= 1 + v
            out.append(f"{y}:{eq - 1:+.0%}")
    return " ".join(out)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    coins, rows = universe(n)
    D = load(coins, rows)
    print(f"S4 intraday sets. {len(D['coins'])} coins: {' '.join(D['coins'])}")
    print("DEV evidence (yearly folds) | POST descriptive. Rs5,000, set risk Rs150, <=3 sets/day, Rs500 day stop.")
    wide = len(sys.argv) > 2 and sys.argv[2] == "daily"
    grid = [(k, tp, sl, hold) for k in ("BRK", "PULL", "MR", "MOM")
            for tp, sl, hold in (((1.0, 0.5, 24), (1.0, 1.0, 48), (2.0, 1.0, 72)) if wide else
                                 ((1.0, 1.0, 12), (2.0, 1.0, 24), (1.5, 1.0, 12), (3.0, 1.5, 48)))]
    for kind, tp, sl, hold in grid:
        r = run(D, kind, tp, sl, hold, scale="d" if wide else "h")
        label = f"{kind} tp{tp} sl{sl} {'dailyATR' if wide else '1hATR'} hold{hold}h"
        print(audit.line(label, dict(daily=r["daily"], trades=r["trades"]))
              + f" | sets/day {r['sets_per_day']:.2f}, days with 2+ sets {r['days_with_2']:.0%} | folds {folds(r)}"
              + (f" | RUINED {time.strftime('%Y-%m-%d', time.gmtime(r['ruined']))}" if r["ruined"] else ""),
              flush=True)


if __name__ == "__main__":
    main()
