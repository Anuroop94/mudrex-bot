"""Daily PAPER trader. Strategy T1: Donchian breakout + ATR trailing stop, trend EMA100, daily candles.
Never places orders: this file contains no order endpoint.

Run once a day after 00:00 UTC (05:30 IST):   python paper.py
Missed days are caught up in order on the next run. State: paper_state.json, trades: paper_trades.csv, log: paper.log

Per coin, params are re-tuned every RETUNE_DAYS on the last TRAIN_DAYS (same walk-forward rule as the test).
Execution mirrors backtest.run bar for bar: decide at daily close, fill at next open, SL/trail checked on
completed bars (SL wins ties), fees + GST + slippage + funding. test_core.test_paper_matches_backtest proves it.

ponytail: MAX_POSITIONS caps count, not correlation; 5 longs on coins that move together is one big bet.
"""
import bisect
import csv
import json
import os
import sys
import time

import config
import data
import mtf
import pick_coins
import risk
import scan
import strategy
import walkforward as wf

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "paper_state.json")
TRADES_PATH = os.path.join(HERE, "paper_trades.csv")
LOG_PATH = os.path.join(HERE, "paper.log")

TF = mtf.TF_1D
VARIANT = "T1 Breakout + trailing, trend EMA100"
FIXED, GRID = mtf.variants(100)[VARIANT]
assert FIXED["rr"] == 0 and "rr" not in GRID, "paper steps implement trailing/stop exits only, no take-profit"
COINS = mtf.COINS
PAPER_EQUITY_INR = 10_000   # paper capital; Rs 1000 is below most coins' minimum at 1% risk on daily stops
MAX_POSITIONS = 5
RETUNE_DAYS = 90
DAY = 86400
TRADE_FIELDS = ["coin", "side", "qty", "entry", "exit", "entry_time", "exit_time", "pnl", "pnl_inr", "fees", "reason"]


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(time.time() + config.IST_OFFSET))} IST  {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def day_str(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


# ---------- core steps: pure functions on a state dict (shared by live paper run and parity test) ----------

def new_state(equity):
    return dict(equity=equity, peak=equity, halted=False, positions={}, pending={}, closed=[])


def _close(st, coin, px_raw, t, reason):
    pos = st["positions"].pop(coin)
    side, qty = pos["side"], pos["qty"]
    px = px_raw * (1 - side * config.SLIPPAGE)
    f = risk.fee(px * qty) + pos["entry"] * qty * config.FUNDING_PER_DAY * (t - pos["time"]) / DAY
    gross = side * (px - pos["entry"]) * qty
    st["equity"] += gross - f
    st["peak"] = max(st["peak"], st["equity"])
    st["closed"].append(dict(coin=coin, side=side, qty=qty, entry=pos["entry"], exit=px, entry_time=pos["time"],
                             exit_time=t, pnl=gross - f - pos["entry_fee"], fees=f + pos["entry_fee"], reason=reason))


def open_step(st, t, opens, specs=None):
    """At bar t's open: execute pending exits, then pending entries (in COINS order, capped by MAX_POSITIONS)."""
    d = (t + config.IST_OFFSET) // DAY
    if d != st.get("day"):
        st["day"], st["day_equity"] = d, st["equity"]
    for coin, pend in list(st["pending"].items()):
        if pend.get("exit") and coin in st["positions"] and coin in opens:
            _close(st, coin, opens[coin], t, "signal")
    mult = risk.risk_mult(st["equity"], st["peak"])
    if mult == 0:
        st["halted"] = True
    for coin, pend in list(st["pending"].items()):
        side = pend.get("entry")
        if not side or coin in st["positions"] or coin not in opens or st["halted"]:
            continue
        if risk.day_blocked(st["equity"], st["day_equity"]):
            continue
        if len(st["positions"]) >= MAX_POSITIONS:
            continue
        entry = opens[coin] * (1 + side * config.SLIPPAGE)
        sl = entry - side * pend["sl_atr"] * pend["atr"]
        s = (specs or {}).get(coin, {})
        qty = risk.size(st["equity"], entry, sl, mult, s.get("step"), s.get("min_qty"), s.get("min_notional"))
        if qty <= 0:
            continue
        fee = risk.fee(entry * qty)
        st["equity"] -= fee
        st["positions"][coin] = dict(side=side, qty=qty, entry=entry, sl=sl, entry_fee=fee, time=t,
                                     trail_atr=pend["trail_atr"])
    st["pending"] = {}


def bar_step(st, coin, bar, atr_now):
    """Completed bar: stop check (gap-aware), then ratchet trailing stop for the next bar."""
    pos = st["positions"].get(coin)
    if not pos:
        return
    t, o, h, l = bar[0], bar[1], bar[2], bar[3]
    if pos["side"] == 1 and l <= pos["sl"]:
        _close(st, coin, min(o, pos["sl"]), t, "sl")
        return
    if pos["side"] == -1 and h >= pos["sl"]:
        _close(st, coin, max(o, pos["sl"]), t, "sl")
        return
    if pos["trail_atr"]:
        d = pos["trail_atr"] * atr_now
        pos["sl"] = max(pos["sl"], h - d) if pos["side"] == 1 else min(pos["sl"], l + d)


def close_step(st, coin, ind, i, p, allow_entry=True):
    """At bar i's close: queue exit/entry for the next open. allow_entry=False = coin is sitting out."""
    pend = {}
    pos = st["positions"].get(coin)
    if pos and strategy.exit_signal(ind, i, pos["side"]):
        pend["exit"] = True
    sig = strategy.signal(ind, i) if allow_entry else 0
    if sig and (pos is None or pend.get("exit")):
        pend.update(entry=sig, atr=ind["atr"][i], sl_atr=p["sl_atr"], trail_atr=p["trail_atr"])
    if pend:
        st["pending"][coin] = pend


def replay(candles, p, start=None):
    """Single-coin replay through the paper steps; used to prove parity with backtest.run."""
    st = new_state(config.START_EQUITY)
    ind = strategy.indicators(candles, p)
    start = max(config.WARMUP, 1) if start is None else start
    for i in range(start, len(candles)):
        bar = candles[i]
        open_step(st, bar[0], {"X": bar[1]})
        bar_step(st, "X", bar, ind["atr"][i])
        if i < len(candles) - 1:
            close_step(st, "X", ind, i, p)
    return st


# ---------- live paper run ----------

def default_params():
    return {**config.DEFAULT_PARAMS, **FIXED, **{k: v[0] for k, v in GRID.items()}}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    eq = PAPER_EQUITY_INR / config.INR_PER_USDT
    st = new_state(eq)
    st.update(start_equity=eq, started=time.time(), params={}, tuned={}, last_closed=None, open_done=None)
    return st


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_PATH)


def append_trades(rows):
    new = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({**{k: r[k] for k in TRADE_FIELDS if k in r}, "pnl_inr": round(r["pnl"] * config.INR_PER_USDT, 2),
                        "entry_time": day_str(r["entry_time"]), "exit_time": day_str(r["exit_time"])})


def contract_specs():
    rows = pick_coins.listing()
    if not rows:
        log("WARNING: no API secret; using default minimums")
        return {}
    by = {r["symbol"].removesuffix("USDT"): r for r in rows}
    return {c: dict(step=float(by[c]["quantity_step"]), min_qty=float(by[c]["min_contract"]),
                    min_notional=float(by[c]["min_notional_value"])) for c in COINS if c in by}


def retune(st, coin, candles, i_end, day_ts):
    """Re-tune coin on the TRAIN_DAYS of candles strictly before index i_end (no peeking at day_ts).
    Returns True if params changed (indicators must be rebuilt)."""
    last = st["tuned"].get(coin)
    if last and day_ts - last < RETUNE_DAYS * DAY:
        return False
    st["tuned"][coin] = day_ts
    n_train = TF["train_days"] * DAY // TF["sec"]
    if i_end < config.WARMUP + n_train:
        st["params"][coin] = None
        log(f"{coin}: not enough history to tune, sitting out")
        return True
    current = st["params"].get(coin) or default_params()
    params, reason, _ = wf.choose(candles, {}, i_end - n_train, i_end, current)
    st["params"][coin] = params
    shown = {k: params[k] for k in GRID} if params else "none"
    log(f"{coin}: retune ({day_str(day_ts)}) -> {reason}; params {shown}")
    return True


def _log_opens(st, before, t):
    for c in set(st["positions"]) - before:
        p = st["positions"][c]
        log(f"{day_str(t)} OPEN {c} {'LONG' if p['side'] == 1 else 'SHORT'} qty {p['qty']:g} @ {p['entry']:.6g}, "
            f"stop {p['sl']:.6g}")


def run(replay_days=None):
    """replay_days=None: live paper day. replay_days=N: fresh paper account replaying the last N days."""
    scan.setup(TF)                    # walk-forward windows + neutral sizing for tuning runs
    config.GRID = GRID
    st = load_state()
    specs = contract_specs()
    now = int(time.time())
    today = now // DAY * DAY
    closed, forming, inds, use = {}, {}, {}, {}
    for c in COINS:
        closed[c] = data.load(TF["days"], f"{c}/USDT", "1d", DAY)
        f = [x for x in data.fetch(today, now, f"{c}/USDT", "1d", DAY) if x[0] == today]
        if f:
            forming[c] = f[0]
    index = {c: {x[0]: i for i, x in enumerate(closed[c])} for c in COINS}
    ts_list = {c: [x[0] for x in closed[c]] for c in COINS}
    all_ts = sorted({x[0] for c in COINS for x in closed[c]})

    first_run = st["last_closed"] is None and replay_days is None
    if replay_days:
        todo = [t for t in all_ts if t >= today - replay_days * DAY]
        st["open_done"] = todo[0] - DAY
        log(f"REPLAY of {len(todo)} days from {day_str(todo[0])} with Rs {PAPER_EQUITY_INR:,}; variant {VARIANT}")
    elif first_run:
        todo = all_ts[-1:]                 # start live: only decide on the latest close
        st["open_done"] = todo[0]
        log(f"paper trading started with Rs {PAPER_EQUITY_INR:,} (${st['equity']:.2f}); variant {VARIANT}")
    else:
        todo = [t for t in all_ts if t > st["last_closed"]]

    def ensure_tuned(c, t):
        """Tune if due as of day t, using data before t; (re)build indicators when params change."""
        i_end = bisect.bisect_left(ts_list[c], t)   # candles strictly before day t

        if retune(st, c, closed[c], i_end, t) or c not in inds:
            # sitting-out coins (params None) still get indicators so open positions keep stops and exits
            use[c] = st["params"].get(c) or default_params()
            inds[c] = strategy.indicators(closed[c], use[c]) if len(closed[c]) > config.WARMUP else None

    n_before = len(st["closed"])
    for t in todo:
        for c in COINS:
            ensure_tuned(c, t)
        if t > (st["open_done"] or 0):
            before = set(st["positions"])
            open_step(st, t, {c: closed[c][index[c][t]][1] for c in COINS if t in index[c]}, specs)
            st["open_done"] = t
            _log_opens(st, before, t)
        for c in COINS:
            if t in index[c] and inds[c]:
                i = index[c][t]
                if not first_run:
                    bar_step(st, c, closed[c][i], inds[c]["atr"][i])
                close_step(st, c, inds[c], i, use[c], allow_entry=st["params"].get(c) is not None)
        st["last_closed"] = t
    if st["halted"]:
        log("HALTED: 20% drawdown from peak. No new entries until you reset the state file")
    if forming and today > (st["open_done"] or 0):
        before = set(st["positions"])
        open_step(st, today, {c: b[1] for c, b in forming.items()}, specs)
        st["open_done"] = today
        _log_opens(st, before, today)

    new_trades = st["closed"][n_before:]
    for tr in new_trades:
        log(f"{day_str(tr['exit_time'])} CLOSE {tr['coin']} {tr['reason']} pnl ${tr['pnl']:+.3f} "
            f"(Rs {tr['pnl'] * config.INR_PER_USDT:+.1f})")
    append_trades(new_trades)
    st["closed"] = []                      # persisted in CSV; keep state small

    marks = {c: (forming[c][4] if c in forming else closed[c][-1][4]) for c in COINS}
    st["snapshot"] = dict(
        at=now, marks=marks,
        unrealized={c: p["side"] * (marks[c] - p["entry"]) * p["qty"] for c, p in st["positions"].items()},
        pending={c: v for c, v in st["pending"].items()})
    save_state(st)
    upnl = sum(st["snapshot"]["unrealized"].values())
    log(f"equity ${st['equity']:.2f} realized, ${st['equity'] + upnl:.2f} marked; "
        f"{len(st['positions'])} open, {len(st['pending'])} queued; processed {len(todo)} day(s)")


def use_files(prefix):
    """Point state/trades/log at <prefix>_*. Replay uses its own files so live paper state is untouched."""
    global STATE_PATH, TRADES_PATH, LOG_PATH
    STATE_PATH, TRADES_PATH, LOG_PATH = (os.path.join(HERE, f"{prefix}{s}")
                                         for s in ("_state.json", "_trades.csv", ".log"))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--replay":
        use_files("replay")
        for p in (STATE_PATH, TRADES_PATH, LOG_PATH):
            if os.path.exists(p):
                os.remove(p)       # replay always starts from a fresh account
        run(int(sys.argv[2]))
    else:
        run()
