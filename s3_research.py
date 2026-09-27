"""S3 "set trader" research (owner's requirement 2026-09-27: at least 2 trades a day, in SETS).

A SET = one or more coins bought together at the same hour, each with a FIXED target and stop-loss (in 1h ATRs)
and a maximum holding time. The next set can only start when every position of the current set is closed.
Sets start only inside the owner's waking hours (IST), because every set needs a Telegram approval tap.
No forced sets: on a quiet day the bot simply skips (owner's choice).
Money: Rs 2,500 allocation, 2x exposure, Mudrex minimum order ~Rs 510, fee + GST + slippage on both sides,
funding per hour held. Daily 5% line blocks NEW sets only (like live). Positions carry over across the dev /
post-SPLIT boundary; POST is descriptive only.

Setups (all long-only, only when the coin's own daily S1 trend is up and BTC mood is good):
  BRK  fresh 1h close above the previous 24h high
  DIP  1h RSI(14) falls below 30 (pullback inside the daily uptrend)
Run: python s3_research.py
"""
import math

import config
import data
import pick_coins
import portfolio as pf
import s1
import s1_audit as audit
import strategy

HOUR, DAY = 3600, 86400
RATE = config.INR_PER_USDT
FEE = config.TAKER_FEE * (1 + config.GST)                          # per side; slippage is in the fill PRICE
MAX_TRADE_STOP_RISK = 0.07                                          # S3's own per-trade loss limit (of equity)
ALLOC, LEV, CAP = 2500.0, 2, 0.05
WAKE = (8, 23)                                                      # IST hours a set may start (approval taps)


def load():
    coins = s1.BASKET
    hourly = {c: data.load(2400, f"{c}/USDT", "1h", HOUR) for c in coins}
    daily = {c: data.load(2400, f"{c}/USDT", "1d", DAY) for c in coins}
    btc = data.load(2400, "BTC/USDT", "1d", DAY)
    rows = {r["symbol"].removesuffix("USDT"): r for r in pick_coins.listing()}
    specs = {c: dict(min_notional=float(rows[c]["min_notional_value"]), min_qty=float(rows[c]["min_contract"]),
                     step=float(rows[c]["quantity_step"])) for c in coins}
    trend = {c: pf.zarattini(daily[c], **s1.SIGNAL_KW) for c in coins}
    datr = {c: dict(zip([x[0] for x in daily[c]], strategy.atr(daily[c], 14))) for c in coins}
    feats = {}
    for c, cs in hourly.items():
        closes = [x[4] for x in cs]
        hh, _ = pf.prior_extremes([x[2] for x in cs], 24)
        feats[c] = dict(t=[x[0] for x in cs], o=[x[1] for x in cs], h=[x[2] for x in cs], l=[x[3] for x in cs],
                        c=closes, atr=strategy.atr(cs, 14), rsi=strategy.rsi(closes, 14), hh=hh,
                        idx={x[0]: i for i, x in enumerate(cs)})
    return dict(coins=coins, f=feats, trend=trend, btc=btc, specs=specs, datr=datr)


def setups(D, t, kind, filters=True):
    """Coins with a setup at the CLOSE of hour t (entry at t+1's open), best first."""
    day = (t // DAY) * DAY - DAY                                    # last CLOSED daily bar
    if filters and s1.btc_mood(D["btc"], day) is not True:
        return []
    out = []
    for c in D["coins"]:
        f = D["f"][c]
        i = f["idx"].get(t)
        if i is None or i < 30 or (filters and D["trend"][c].get(day, 0) <= 0) or not f["atr"][i]:
            continue
        if kind == "BRK":
            if f["hh"][i] and f["c"][i] > f["hh"][i] and f["c"][i - 1] <= (f["hh"][i - 1] or 1e18):
                out.append(((f["c"][i] - f["hh"][i]) / f["atr"][i], c))
        elif f["rsi"][i] < 30 <= f["rsi"][i - 1]:
            out.append((30 - f["rsi"][i], c))
    return [c for _, c in sorted(out, reverse=True)]


def run(D, kind, tp, sl, hold=24, max_coins=2, scale="h", filters=True, start=None, target_first=False):
    """start: first hour to trade (paper trading replays from its own start date with fresh Rs 2,500).
    target_first: sensitivity - when one 1h bar touches both stop and target, assume the target came first
    (the default, stop first, is the conservative guess; OHLC data cannot tell)."""
    eq, open_pos, trades, marks, log = ALLOC, [], [], [], []
    day, day_start, blocked, sets = None, eq, False, 0
    stats = dict(signal_hours=0, rejected_min=0, rejected_risk=0, ruin_day=None)
    t0 = max(min(f["t"]) for f in D["f"].values()) + 400 * HOUR
    t0 = max(t0, start) if start else t0
    t1 = min(max(f["t"]) for f in D["f"].values())
    pending = None
    for t in range(t0, t1, HOUR):
        ist_day = (t + config.IST_OFFSET) // DAY
        if ist_day != day:
            if day is not None:
                marks.append((t, eq + sum(p["u"] for p in open_pos)))
            day, day_start, blocked = ist_day, eq + sum(p["u"] for p in open_pos), False
        # entries decided at the previous close, filled at this hour's open; fill slots with VALID candidates
        if pending and not open_pos:
            budget = LEV * min(eq, ALLOC)
            each = budget / min(max_coins, len(pending))
            for c in pending:
                if len(open_pos) >= max_coins:
                    break
                f = D["f"][c]
                i = f["idx"].get(t)
                if i is None:
                    continue
                px = f["o"][i] * (1 + pf.SLIPPAGE)
                atr = f["atr"][i - 1] if scale == "h" else D["datr"][c].get((t // DAY) * DAY - DAY)
                if not atr:
                    continue
                spec = D["specs"][c]
                qty = math.floor(each / RATE / px / spec["step"]) * spec["step"]
                if qty < spec["min_qty"] or qty * px < spec["min_notional"]:
                    stats["rejected_min"] += 1
                    if stats["ruin_day"] is None and eq < 2 * 510 / LEV:
                        stats["ruin_day"] = t
                    continue
                if qty * sl * atr * RATE > MAX_TRADE_STOP_RISK * eq:
                    stats["rejected_risk"] += 1
                    continue
                n = qty * px * RATE
                eq -= n * FEE
                open_pos.append(dict(c=c, qty=qty, entry=px, tp=px + tp * atr, sl=px - sl * atr, t=t,
                                     deadline=t + hold * HOUR, cost=n * FEE, fund=0.0, u=0.0))
            if open_pos:
                sets += 1
                log.append(dict(t=t, event="SET", coins=[p["c"] for p in open_pos]))
        pending = None
        # manage the set on this hour's bar: time exit and gaps at the OPEN first, then the bar's range
        still = []
        for p in open_pos:
            f = D["f"][p["c"]]
            i = f["idx"].get(t)
            if i is None:
                still.append(p)
                continue
            o, h, lo = f["o"][i], f["h"][i], f["l"][i]
            if t >= p["deadline"]:
                exit_px, why = o, "time"                            # max hold reached: out at this hour's open
            elif o <= p["sl"]:
                exit_px, why = o, "stop"                            # gapped through the stop
            elif o >= p["tp"]:
                exit_px, why = o, "target"                          # gapped through the target
            else:
                fund = p["qty"] * o * RATE * config.FUNDING_PER_DAY / 24
                eq -= fund
                p["fund"] += fund
                hit_sl, hit_tp = lo <= p["sl"], h >= p["tp"]
                if hit_sl and hit_tp:
                    exit_px, why = (p["tp"], "target") if target_first else (p["sl"], "stop")
                elif hit_sl:
                    exit_px, why = p["sl"], "stop"
                elif hit_tp:
                    exit_px, why = p["tp"], "target"
                else:
                    p["u"] = p["qty"] * (f["c"][i] - p["entry"]) * RATE
                    still.append(p)
                    continue
            exit_px *= 1 - pf.SLIPPAGE
            gross = p["qty"] * (exit_px - p["entry"]) * RATE
            fee = p["qty"] * exit_px * RATE * FEE
            eq += gross - fee
            trades.append(dict(coin=p["c"], entry_t=p["t"], exit_t=t, entry=p["entry"], exit=exit_px, why=why,
                               net=gross - fee - p["cost"] - p["fund"]))
        open_pos = still
        day_pnl = eq + sum(p["u"] for p in open_pos) - day_start
        if abs(day_pnl) >= CAP * day_start:
            blocked = True                                          # 5% line: no NEW sets today (like live)
        entry_hour_ist = ((t + HOUR + config.IST_OFFSET) % DAY) // HOUR   # the set would START next hour
        if WAKE[0] <= entry_hour_ist < WAKE[1]:
            found = setups(D, t, kind, filters)
            stats["signal_hours"] += bool(found)                    # raw setups, even while a set is open
            if found and not open_pos and not blocked:
                pending = found
    daily = [(marks[k + 1][0], marks[k + 1][1] / marks[k][1] - 1) for k in range(len(marks) - 1)]
    return dict(daily=daily, trades=trades, sets=sets, days=len(marks), equity=eq, open=open_pos, log=log,
                blocked=blocked, last_t=t1 - HOUR, stats=stats)

def main():
    D = load()
    print("DEV = before 2025-09-25 (evidence).  POST* = after (descriptive only).  Rs 2,500, 2x, fixed target/stop.")
    print("sets/day = over days BEFORE the account got too small to trade; signals/day = hours with a raw setup.")
    grid = [(k, tp, sl, 24, "h", True, False) for k in ("BRK", "DIP") for tp, sl in ((1.0, 1.0), (1.5, 1.0), (2.0, 1.0))]
    grid += [(k, tp, sl, 48, "d", f, False) for k in ("BRK", "DIP") for f in (True, False)
             for tp, sl in ((0.5, 0.5), (1.0, 0.5))]
    grid += [("BRK", 1.0, 1.0, 24, "h", True, True), ("BRK", 1.0, 0.5, 48, "d", True, True)]   # target-first check
    for kind, tp, sl, hold, scale, filt, tf in grid:
        r = run(D, kind, tp, sl, hold, scale=scale, filters=filt, target_first=tf)
        s = r["stats"]
        live_days = r["days"] if s["ruin_day"] is None else max(1, sum(1 for t, _ in r["daily"] if t < s["ruin_day"]))
        label = (f"{kind} tgt {tp} stop {sl} x{'1h' if scale == 'h' else 'daily'}ATR {hold}h"
                 f"{'' if filt else ' NO-FILTER'}{' TARGET-FIRST' if tf else ''}")
        print(audit.line(label, dict(daily=r["daily"], trades=r["trades"]))
              + f"  | sets/day {r['sets'] / live_days:.2f} signals/day {s['signal_hours'] / max(1, r['days']):.2f}"
              f" rejected(min {s['rejected_min']}, risk {s['rejected_risk']})"
              + (" RUINED" if s["ruin_day"] else ""), flush=True)

if __name__ == "__main__":
    main()
