"""LIVE trader for strategy S1 (s1.py). REAL MONEY. Two commands, both run by you, never scheduled:

  python live_trader.py plan      READ-ONLY. Reads your INR futures wallet + positions, computes today's S1
                                  orders, writes live_plan.json and prints it. Places nothing. Safe any time.
  python live_trader.py execute   Shows the saved plan and places it ONLY after you type YES.

Safety rules enforced in code:
  - only basket coins (s1.BASKET) are ever traded; your manual positions are never touched
  - capital cap Rs 5,000 for sizing, even if the wallet holds more
  - Mudrex leverage set to 2 on every coin before entry; every entry carries a safety stop-loss (3x ATR)
  - daily loss stop: if equity fell >= Rs 1,000 since the previous plan, only exits are allowed that day
  - kill switch: a file named STOP in this folder blocks execute
  - a plan expires after 3 hours and can be executed at most once
"""
import decimal
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

import config
import data
import pick_coins
import portfolio as pf
import s1

HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_PATH = os.path.join(HERE, "live_plan.json")
STATE_PATH = os.path.join(HERE, "live_state.json")
LOG_PATH = os.path.join(HERE, "live_orders.log")
STOP_PATH = os.path.join(HERE, "STOP")
API = "https://trade.mudrex.com/fapi"
PLAN_MAX_AGE = 3 * 3600
DAY = pf.DAY


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + config.IST_OFFSET))} IST  {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def api(method, path, body=None):
    """Mudrex call. Returns parsed JSON; HTTP errors come back as {'success': False, 'http': code, 'errors': ...}."""
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Authentication": os.environ["MUDREX_API_SECRET"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except ValueError:
            err = {}
        return {"success": False, "http": e.code, "errors": err.get("errors", err)}


def fmt(x, step):
    """Decimal string of x rounded DOWN to a multiple of step (what the exchange accepts)."""
    q = decimal.Decimal(str(step))
    return str((decimal.Decimal(str(x)) / q).to_integral_value(rounding=decimal.ROUND_FLOOR) * q)


# ---------- pure planning logic (unit-tested)

def build_orders(targets, positions, prices, atrs, specs, equity_inr, free_inr, armed, loss_stop):
    """targets: {coin: 1x weight}; positions: {coin: {id, qty, entry, sl}} basket-only.
    Returns a list of actions. Never sizes more than CAPITAL_CAP_INR, never exceeds free margin."""
    size_eq = min(equity_inr, s1.CAPITAL_CAP_INR)
    margin_left = free_inr
    out = []
    for c in s1.BASKET:
        w, pos, px, s = targets.get(c, 0.0), positions.get(c), prices[c], specs[c]
        if pos and w <= 0:
            out.append(dict(action="CLOSE", coin=c, id=pos["id"], qty=pos["qty"], reason="trend exit"))
        elif pos:
            out.append(dict(action="HOLD", coin=c, qty=pos["qty"],
                            reason="trend still up" + ("" if pos.get("sl") else "; WARNING: no stop-loss on exchange")))
        elif w > 0:
            if loss_stop:
                out.append(dict(action="SKIP", coin=c, reason=f"daily cap ({s1.DAILY_CAP_PCT:.0%}) hit"))
                continue
            if not armed.get(c, True):
                out.append(dict(action="SKIP", coin=c, reason="stopped out earlier; waits for trend to reset"))
                continue
            notional_inr = w * s1.LEV * size_eq
            qty = math.floor(notional_inr / config.INR_PER_USDT / px / s["step"] + 1e-9) * s["step"]
            if qty < s["min_qty"] or qty * px < s["min_notional"]:
                out.append(dict(action="SKIP", coin=c, reason="below Mudrex minimum order"))
                continue
            notional_inr = qty * px * config.INR_PER_USDT
            margin = notional_inr / s1.LEV
            if margin > margin_left:
                out.append(dict(action="SKIP", coin=c, reason=f"not enough free margin (needs Rs {margin:.0f})"))
                continue
            margin_left -= margin
            stop = math.floor((px - s1.SL_ATR * atrs[c]) / s["price_step"]) * s["price_step"]
            out.append(dict(action="OPEN", coin=c, side="LONG", qty=fmt(qty, s["step"]), est_price=px,
                            notional_inr=round(notional_inr), margin_inr=round(margin),
                            stop=fmt(stop, s["price_step"]), stop_pct=round(stop / px - 1, 4), reason="trend up"))
    return out


# ---------- plan

def read_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return dict(armed={}, known=[], last_equity=None)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def account(specs):
    funds = api("GET", "/v1/futures/funds?trade_currency=INR")
    pos = api("GET", "/v1/futures/positions?trade_currency=INR")
    if not funds.get("success") or not pos.get("success"):
        sys.exit(f"cannot read account: {funds.get('errors') or pos.get('errors')}")
    f = funds["data"]
    free, locked = float(f["balance"]), float(f["locked_amount"])
    basket, manual, upnl = {}, [], 0.0
    for p in pos["data"] or []:
        coin = p["symbol"].removesuffix("USDT")
        qty, entry = float(p["quantity"]), float(p["entry_price"])
        side = 1 if p["order_type"] == "LONG" else -1
        px = specs[coin]["price"] if coin in specs else entry
        upnl += side * qty * (px - entry) * float(p.get("entry_hedge_rate") or config.INR_PER_USDT)
        sl = float((p.get("stoploss") or {}).get("price") or 0) or None
        if coin in s1.BASKET and side == 1:
            basket[coin] = dict(id=p["id"], qty=qty, entry=entry, sl=sl)
        else:
            manual.append(p["symbol"])
    return dict(free=free, locked=locked, upnl=upnl, equity=free + locked + upnl, basket=basket, manual=manual)


def plan():
    now = int(time.time())
    last_closed = now // DAY * DAY - DAY
    specs = s1.specs_from_listing(pick_coins.listing())
    acct = account(specs)
    uni = {c: [x for x in data.load(2400, f"{c}/USDT", "1d", DAY) if x[0] <= last_closed] for c in s1.BASKET}
    closes = {c: {x[0]: x[4] for x in cs} for c, cs in uni.items()}
    ctx = pf.prepare(uni, pf.zarattini, **s1.SIGNAL_KW)
    _, atrs = pf.trade_lookups(uni)
    size_eq = min(acct["equity"], s1.CAPITAL_CAP_INR)
    btc = [x for x in data.load(2400, "BTC/USDT", "1d", DAY) if x[0] <= last_closed]
    targets = s1.targets(ctx, closes, last_closed, size_eq, specs, btc)
    mood_ok = s1.btc_mood_ok(btc, last_closed)

    st = read_state()
    for c in st["known"]:               # held after our last execute, gone now, and we did not close it => stop hit
        if c not in acct["basket"] and c not in st.get("closed_by_bot", []):
            st["armed"][c] = False
    for c in s1.BASKET:
        if targets.get(c, 0) <= 0:
            st["armed"][c] = True
    loss_stop = (st["last_equity"] is not None
                 and acct["equity"] <= st["last_equity"] - s1.daily_cap_inr(st["last_equity"]))
    caps_path = os.path.join(HERE, "caps.json")     # written by watcher.py: daily +/-Rs 1,000 caps
    if os.path.exists(caps_path):
        with open(caps_path) as f:
            caps = json.load(f)
        today_ist = time.strftime("%Y-%m-%d", time.gmtime(now + config.IST_OFFSET))
        if caps.get("day") == today_ist and caps.get("cap_hit"):
            loss_stop = True                          # either cap: exits only for the rest of the day
    guard_path = os.path.join(HERE, "guard.json")   # written by watcher.py performance guard
    guard = None
    if os.path.exists(guard_path):
        with open(guard_path) as f:
            guard = json.load(f)
        if guard.get("tripped"):
            loss_stop = True                          # live results broke backtest limits: exits only
    prices = {c: closes[c][last_closed] for c in s1.BASKET}
    orders = build_orders(targets, acct["basket"], prices, {c: atrs[c][last_closed] for c in s1.BASKET},
                          specs, acct["equity"], acct["free"], st["armed"], loss_stop)
    p = dict(created_at=now, decision_day=time.strftime("%Y-%m-%d", time.gmtime(last_closed)), strategy=s1.NAME,
             account=dict(equity_inr=round(acct["equity"], 2), free_inr=acct["free"], locked_inr=acct["locked"],
                          manual_positions=acct["manual"]),
             loss_stop=loss_stop, mood_ok=mood_ok, guard=guard, orders=orders, executed=False)
    write_json(PLAN_PATH, p)
    st["last_equity"] = acct["equity"]
    write_json(STATE_PATH, st)

    print(f"\nS1 plan for today (decision on {p['decision_day']} close)")
    print(f"Wallet equity Rs {acct['equity']:,.2f} (free {acct['free']:,.2f}, in margin {acct['locked']:,.2f}); "
          f"sizing on Rs {size_eq:,.0f}")
    if acct["manual"]:
        print(f"Your manual positions (bot will NOT touch): {', '.join(acct['manual'])}")
    print(f"Market mood (BTC vs 200-day average): {'GOOD, trading allowed' if mood_ok else 'BAD, S1 holds no positions'}")
    if guard and guard.get("tripped"):
        print(f"PERFORMANCE GUARD TRIPPED: {guard.get('reason')}. Exits only until you review and reset guard.json.")
    elif loss_stop:
        print(f"DAILY CAP ACTIVE ({s1.DAILY_CAP_PCT:.0%}): exits only today.")
    for o in orders:
        if o["action"] == "OPEN":
            print(f"  OPEN  {o['coin']:<5} LONG {o['qty']} (~Rs {o['notional_inr']:,}, margin Rs {o['margin_inr']:,}) "
                  f"stop {o['stop']} ({o['stop_pct']:+.1%})")
        else:
            print(f"  {o['action']:<5} {o['coin']:<5} {o['reason']}")
    if not any(o["action"] in ("OPEN", "CLOSE") for o in orders):
        print("  nothing to do today")
    return p


# ---------- execute

def execute():
    if not os.path.exists(PLAN_PATH):
        sys.exit("no plan: run  python live_trader.py plan  first")
    with open(PLAN_PATH) as f:
        p = json.load(f)
    if os.path.exists(STOP_PATH):
        sys.exit("STOP file present: live trading disabled. Delete the STOP file to re-enable.")
    if p["executed"]:
        sys.exit("this plan was already executed; run plan again tomorrow")
    if time.time() - p["created_at"] > PLAN_MAX_AGE:
        sys.exit("plan is older than 3 hours; run plan again")
    todo = [o for o in p["orders"] if o["action"] in ("OPEN", "CLOSE")]
    if not todo:
        sys.exit("plan has no orders to place")
    print(f"\nREAL ORDERS on your Mudrex INR futures wallet (strategy {p['strategy']}):")
    for o in todo:
        print(f"  {o['action']} {o['coin']} " + (f"LONG {o['qty']} ~Rs {o['notional_inr']:,} stop {o['stop']}"
                                                   if o["action"] == "OPEN" else f"position {o['id']}"))
    if input("\nType YES to place these real orders: ").strip() != "YES":
        sys.exit("aborted: nothing placed")
    place(p, todo, approved_by="terminal YES")


def check_plan(p, plan_id=None):
    """Reason the plan must NOT be placed, or None. Shared by terminal and Telegram approval."""
    if os.path.exists(STOP_PATH):
        return "STOP file present: live trading disabled"
    if p["executed"]:
        return "plan already executed"
    if time.time() - p["created_at"] > PLAN_MAX_AGE:
        return "plan is older than 3 hours"
    if plan_id is not None and str(p["created_at"]) != str(plan_id):
        return "this approval is for an older plan"
    if not any(o["action"] in ("OPEN", "CLOSE") for o in p["orders"]):
        return "plan has no orders"
    return None


def place(p, todo, approved_by):
    """Place an approved plan. Only called after an explicit human approval (terminal YES or Telegram button)."""
    log(f"plan {p['created_at']} approved by {approved_by}")
    p["executed"] = True                 # mark first: a crash mid-way must not allow a double execute
    write_json(PLAN_PATH, p)
    st = read_state()
    st["closed_by_bot"] = []
    results = []

    def report(msg):
        log(msg)
        results.append(msg)
    for o in todo:
        sym = o["coin"] + "USDT"
        if o["action"] == "CLOSE":
            r = api("POST", f"/v1/futures/positions/{o['id']}/close")
            if r.get("success"):
                st["closed_by_bot"].append(o["coin"])
            report(f"CLOSE {sym}: {'OK' if r.get('success') else 'FAILED ' + json.dumps(r.get('errors'))}")
            continue
        r = api("POST", f"/v1/futures/{sym}/leverage?is_symbol",
                dict(margin_type="ISOLATED", leverage=str(s1.LEV), trade_currency="INR"))
        if not r.get("success"):
            report(f"LEVERAGE {sym}: FAILED {json.dumps(r.get('errors'))}; order skipped")
            continue
        body = dict(trigger_type="MARKET", order_type="LONG", quantity=o["qty"], trade_currency="INR",
                    is_stoploss=True, stoploss_price=o["stop"], client_order_id=f"s1-{o['coin']}-{p['decision_day']}")
        r = api("POST", f"/v2/futures/order?symbol={sym}", body)
        report(f"OPEN {sym} LONG {o['qty']} stop {o['stop']}: "
               + (f"OK order {r['data'].get('order_id')}" if r.get("success") else f"FAILED {json.dumps(r.get('errors'))}"))
    specs = s1.specs_from_listing(pick_coins.listing())
    st["known"] = sorted(account(specs)["basket"])
    write_json(STATE_PATH, st)
    print("done. Check the Mudrex app to confirm each position shows its stop-loss.")
    return results


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "plan":
        plan()
    elif cmd == "execute":
        execute()
    else:
        sys.exit(__doc__)
