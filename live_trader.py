"""LIVE trader for strategy S1 (s1.py). REAL MONEY. Commands, all run by you, never scheduled:

  python live_trader.py plan        READ-ONLY on the exchange. Computes today's S1 orders, records the plan in the
                                    execution journal (execution.db) and writes live_plan.json. Places nothing.
  python live_trader.py execute     Shows the latest plan and runs it ONLY after you type YES.
  python live_trader.py reconcile   After a crash/restart: resolves unfinished orders by client_order_id.

Nothing executes unless LIVE_TRADING_ENABLED=true is set in .env (default false) and no STOP file exists.
All order safety (locking, reconciliation, ownership, fill-aware stops, caps, drift) lives in execution.py.
"""
import json
import math
import os
import sys
import time

import config
import data
import execution as ex
import pick_coins
import portfolio as pf
import s1
from mudrex_client import Client

HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_PATH = os.path.join(HERE, "live_plan.json")
STATE_PATH = os.path.join(HERE, "live_state.json")
DAY = pf.DAY


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def read_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return dict(armed={})


# ---------- pure planning logic (unit-tested)

def build_orders(targets, owned, manual_symbols, prices, atrs, specs, size_equity_inr, armed, entries_blocked,
                 rate):
    """targets: {coin: 1x weight}; owned: {coin: position_id} of bot-owned open positions.
    Returns actions. CLOSE only for bot-owned positions; never plans anything on a symbol with a manual position."""
    size_eq = min(size_equity_inr, s1.CAPITAL_CAP_INR)
    out = []
    for c in s1.BASKET:
        w, px, s = targets.get(c, 0.0), prices[c], specs[c]
        if c in manual_symbols:
            out.append(dict(action="SKIP", coin=c, reason="you hold a manual position on this coin; bot stays out"))
        elif c in owned and w <= 0:
            out.append(dict(action="CLOSE", coin=c, position_id=owned[c], reason="trend exit / market mood"))
        elif c in owned:
            out.append(dict(action="HOLD", coin=c, reason="trend still up"))
        elif w > 0:
            if entries_blocked:
                out.append(dict(action="SKIP", coin=c, reason=entries_blocked))
                continue
            if not armed.get(c, True):
                out.append(dict(action="SKIP", coin=c, reason="stopped out earlier; waits for trend to reset"))
                continue
            notional_inr = w * s1.LEV * size_eq
            qty = math.floor(notional_inr / rate / px / s["step"] + 1e-9) * s["step"]
            if qty < s["min_qty"] or qty * px < s["min_notional"]:
                out.append(dict(action="SKIP", coin=c, reason="below Mudrex minimum order"))
                continue
            out.append(dict(action="OPEN", coin=c, planned_price=px, notional_inr=round(notional_inr, 2),
                            atr=atrs[c], est_stop=round(px - s1.SL_ATR * atrs[c], 6), reason="trend up"))
    return out


# ---------- plan

def plan(client=None, con=None):
    client = client or Client()
    con = con or ex.db()
    now = int(time.time())
    last_closed = now // DAY * DAY - DAY
    ex.recover_ownership(con, client)
    positions = client.positions()
    owned_ids = ex.owned_ids(con)
    st = read_state()
    for r in con.execute("SELECT position_id, coin FROM owned WHERE closed_at IS NULL").fetchall():
        if r["position_id"] not in {p["id"] for p in positions}:      # gone without our CLOSE => stop hit
            con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (now, r["position_id"]))
            st["armed"][r["coin"]] = False
    owned = {p["symbol"].removesuffix("USDT"): p["id"] for p in positions if p["id"] in owned_ids}
    manual = {p["symbol"].removesuffix("USDT") for p in positions if p["id"] not in owned_ids}
    rate = ex.hedge_rate(client, positions)
    specs = s1.specs_from_listing(pick_coins.listing())
    uni = {c: [x for x in data.load(2400, f"{c}/USDT", "1d", DAY) if x[0] <= last_closed] for c in s1.BASKET}
    closes = {c: {x[0]: x[4] for x in cs} for c, cs in uni.items()}
    ctx = pf.prepare(uni, pf.zarattini, **s1.SIGNAL_KW)
    _, atrs = pf.trade_lookups(uni)
    btc = [x for x in data.load(2400, "BTC/USDT", "1d", DAY) if x[0] <= last_closed]
    bot_eq = ex.bot_equity(con, client, positions, rate or config.INR_PER_USDT)
    caps = ex.caps_state(con, bot_eq)
    targets = s1.targets(ctx, closes, last_closed, bot_eq, specs, btc)
    for c in s1.BASKET:
        if targets.get(c, 0) <= 0:
            st["armed"][c] = True
    blocked = ("STOP file present" if os.path.exists(ex.STOP_PATH) else
               "performance guard tripped" if ex.guard_tripped() else
               f"daily {caps['hit']} cap hit" if caps["hit"] else
               "no current INR hedge rate from Mudrex" if not rate else None)
    orders = build_orders(targets, owned, manual, {c: closes[c][last_closed] for c in s1.BASKET},
                          {c: atrs[c][last_closed] for c in s1.BASKET}, specs, bot_eq, st["armed"], blocked,
                          rate or config.INR_PER_USDT)
    todo = [o for o in orders if o["action"] in ("OPEN", "CLOSE")]
    decision = time.strftime("%Y-%m-%d", time.gmtime(last_closed))
    plan_id = ex.record_plan(con, decision, todo, dict(orders=orders)) if todo else None
    p = dict(plan_id=plan_id, created_at=now, decision_day=decision, strategy=s1.NAME,
             mood_ok=s1.btc_mood_ok(btc, last_closed), bot_equity_inr=round(bot_eq, 2), caps=caps,
             hedge_rate=rate, live_enabled=ex.live_enabled(), blocked=blocked, orders=orders)
    write_json(PLAN_PATH, p)
    write_json(STATE_PATH, st)

    print(f"\nS1 plan {plan_id or '(none)'} for today (decision on {decision} close)")
    print(f"Bot equity Rs {bot_eq:,.2f} (allocation Rs {s1.CAPITAL_CAP_INR:,} + bot P&L); "
          f"today {caps['pnl']:+,.0f} vs caps +/-{caps['cap']:,.0f}; INR/USDT {rate or 'UNKNOWN'}")
    print(f"Market mood (BTC vs 200-day average): {'GOOD' if p['mood_ok'] else 'BAD: S1 holds no positions'}")
    if manual:
        print(f"Manual positions (bot will NOT touch these coins): {', '.join(sorted(manual))}")
    if blocked:
        print(f"NEW ENTRIES BLOCKED: {blocked}")
    if not ex.live_enabled():
        print("LIVE_TRADING_ENABLED is false: execute will refuse.")
    for o in orders:
        extra = (f" ~Rs {o['notional_inr']:,.0f} (stop ~{o['est_stop']}, re-anchored to the actual fill)"
                 if o["action"] == "OPEN" else "")
        print(f"  {o['action']:<5} {o['coin']:<5} {o['reason']}{extra}")
    if not todo:
        print("  nothing to do today")
    return p


# ---------- execute / reconcile

def execute(client=None, con=None, confirm=input):
    con = con or ex.db()
    row = ex.latest_plan(con)
    if row is None or row["state"] != "PLANNED":
        sys.exit("no pending plan: run  python live_trader.py plan  first")
    why = ex.preflight()
    if why:
        sys.exit(f"refused: {why}")
    orders = con.execute("SELECT * FROM orders WHERE plan_id=? ORDER BY seq", (row["id"],)).fetchall()
    print(f"\nREAL ORDERS on your Mudrex INR futures wallet, plan {row['id']} ({row['decision_day']}):")
    for o in orders:
        print(f"  {o['action']} {o['coin']}" + (f" ~Rs {o['planned_notional_inr']:,.0f}" if o["action"] == "OPEN" else ""))
    if confirm("\nType YES to place these real orders: ").strip() != "YES":
        sys.exit("aborted: nothing placed")
    final, summary = ex.execute(con, client or Client(), row["id"], "terminal YES", alert=print)
    print(f"\nplan {row['id']}: {final}\n  " + "\n  ".join(summary))
    print("Check the Mudrex app: each new position should show its stop-loss.")
    return final


def reconcile(client=None, con=None):
    ex.reconcile(con or ex.db(), client or Client(), alert=print)
    print("reconcile done")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"plan": plan, "execute": execute, "reconcile": reconcile}.get(cmd, lambda: sys.exit(__doc__))()
