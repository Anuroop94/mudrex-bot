"""Durable, restart-safe execution for S1 live orders (SQLite journal). REAL MONEY path.

Plan states : PLANNED -> APPROVED -> EXECUTING -> COMPLETE | PARTIAL | FAILED | RECONCILE_REQUIRED
Order states: PLANNED -> SUBMITTED -> ACCEPTED -> FILLED -> VERIFIED   (or FAILED)
Rules:
  - Exactly one approver wins: claim() takes an exclusive SQLite write lock (BEGIN IMMEDIATE).
  - Every order has a unique client_order_id, written to the journal BEFORE the request is sent.
  - A timeout / transport error / 5xx is UNKNOWN, not failed: look the order up by client_order_id and poll.
    An order that cannot be confirmed either way becomes RECONCILE_REQUIRED and is NEVER resubmitted blindly.
  - 423/429: bounded backoff; look up by client_order_id before any resubmission.
  - Ownership = Mudrex position UUID recorded from our own filled orders. Never inferred from symbol/direction.
    A basket symbol with any position we do not own (manual long or short) is refused and alerted.
  - Before the plan and before EVERY order: STOP file, LIVE_TRADING_ENABLED, positions/ownership, free margin,
    bot caps, guard, asset spec, live price (drift check) and the current INR hedge rate are re-read.
  - Stops are fill-aware: stop = actual fill - SL_ATR*ATR, validated fill > stop > liquidation, then verified on
    the exchange. If a stop cannot be verified, no further entries are made and you are alerted.
  - Daily caps are bot-only: 5% of the bot's day-start equity (Rs 5,000 allocation + bot realized/unrealized
    P&L). Deposits, withdrawals and manual positions do not count. Caps BLOCK NEW ENTRIES; they never close
    positions automatically (closing needs a human-approved plan).
"""
import json
import math
import os
import sqlite3
import time

import config
import s1
from mudrex_client import Ambiguous, Locked, Rejected

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "execution.db")
STOP_PATH = os.path.join(HERE, "STOP")
GUARD_PATH = os.path.join(HERE, "guard.json")
PLAN_MAX_AGE = 3 * 3600
MAX_DRIFT = 0.02            # refuse an entry if the live price moved >2% from the planned price
POLL_TRIES, LOOKUP_TRIES = 10, 6
TERMINAL_OK = {"FILLED"}
TERMINAL_BAD = {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"}
FINAL_PLAN = {"COMPLETE", "PARTIAL", "FAILED", "RECONCILE_REQUIRED"}


def live_enabled():
    return os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"


# ---------- journal

def db(path=None):
    con = sqlite3.connect(path or DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY, created_at INTEGER, decision_day TEXT, state TEXT,
        approved_by TEXT, approved_at INTEGER, payload TEXT, note TEXT);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, plan_id INTEGER, seq INTEGER, coin TEXT, action TEXT,
        client_order_id TEXT UNIQUE, state TEXT, position_id TEXT, planned_price REAL, planned_notional_inr REAL,
        atr REAL, qty TEXT, exchange_order_id TEXT, fill_price REAL, filled_qty REAL, stop_price TEXT,
        error TEXT, updated_at INTEGER);
    CREATE TABLE IF NOT EXISTS owned(position_id TEXT PRIMARY KEY, coin TEXT, client_order_id TEXT,
        opened_at INTEGER, closed_at INTEGER);
    CREATE TABLE IF NOT EXISTS ledger(day TEXT PRIMARY KEY, start_equity REAL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at INTEGER, kind TEXT, msg TEXT);
    """)
    return con


def event(con, kind, msg, alert=None):
    con.execute("INSERT INTO events(at, kind, msg) VALUES(?,?,?)", (int(time.time()), kind, msg))
    if alert:
        alert(msg)


def set_order(con, oid, **kw):
    kw["updated_at"] = int(time.time())
    con.execute(f"UPDATE orders SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), oid))


def record_plan(con, decision_day, orders, payload):
    """Store a new PLANNED plan. orders: dicts with coin, action, position_id?, planned_price, notional_inr, atr."""
    now = int(time.time())
    cur = con.execute("INSERT INTO plans(created_at, decision_day, state, payload) VALUES(?,?,?,?)",
                      (now, decision_day, "PLANNED", json.dumps(payload)))
    pid = cur.lastrowid
    for i, o in enumerate(orders):
        cid = f"s1-{pid}-{i}-{o['coin']}-{o['action'][0]}"[:64]
        con.execute("""INSERT INTO orders(plan_id, seq, coin, action, client_order_id, state, position_id,
                       planned_price, planned_notional_inr, atr, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, i, o["coin"], o["action"], cid, "PLANNED", o.get("position_id"), o.get("planned_price"),
                     o.get("notional_inr"), o.get("atr"), now))
    return pid


def claim(con, plan_id, approver):
    """Atomically move PLANNED -> APPROVED. Returns None on success or the reason it was refused.
    BEGIN IMMEDIATE takes the write lock, so two approvers (processes or threads) cannot both win."""
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT state, created_at FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            con.execute("ROLLBACK")
            return "unknown plan"
        if row["state"] != "PLANNED":
            con.execute("ROLLBACK")
            return f"plan is already {row['state']}"
        if time.time() - row["created_at"] > PLAN_MAX_AGE:
            con.execute("UPDATE plans SET state='FAILED', note='expired' WHERE id=?", (plan_id,))
            con.execute("COMMIT")
            return "plan is older than 3 hours"
        con.execute("UPDATE plans SET state='APPROVED', approved_by=?, approved_at=? WHERE id=?",
                    (approver, int(time.time()), plan_id))
        con.execute("COMMIT")
        return None
    except Exception:
        con.execute("ROLLBACK")
        raise


def latest_plan(con):
    return con.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()


# ---------- live account snapshot

def floor_to(x, step):
    return math.floor(x / step + 1e-9) * step


def fmt_step(x, step):
    import decimal
    q = decimal.Decimal(str(step))
    return str((decimal.Decimal(str(x)) / q).to_integral_value(rounding=decimal.ROUND_FLOOR) * q)


def hedge_rate(client, positions):
    """Current INR per USDT from Mudrex itself (open positions or most recent INR order). None if unknown."""
    rates = [float(p["entry_hedge_rate"]) for p in positions if p.get("entry_hedge_rate")]
    if rates:
        return rates[-1]
    for o in sorted(client.get("/v1/futures/orders/history", {"trade_currency": "INR", "limit": 20}) or [],
                    key=lambda o: o["created_at"], reverse=True):
        if o.get("hedge_rate"):
            return float(o["hedge_rate"])
    return None


def owned_ids(con):
    return {r["position_id"] for r in con.execute("SELECT position_id FROM owned WHERE closed_at IS NULL")}


def recover_ownership(con, client):
    """Rebuild ownership from ALL order history (paginated): our filled orders carry client_order_id 's1-...'."""
    for o in client.history("orders"):
        cid = o.get("client_order_id") or ""
        if cid.startswith("s1-") and o.get("status") == "FILLED" and o.get("future_position_uuid") \
                and cid.endswith("-O"):
            con.execute("INSERT OR IGNORE INTO owned(position_id, coin, client_order_id, opened_at) VALUES(?,?,?,?)",
                        (o["future_position_uuid"], o["symbol"].removesuffix("USDT"), cid, int(time.time())))


def bot_equity(con, client, positions, rate):
    """Bot-only equity: allocation + realized P&L of our closed positions + unrealized of our open ones."""
    ours = {r["position_id"] for r in con.execute("SELECT position_id FROM owned")}
    realized = sum(float(p.get("pnl") or 0) for p in client.history("positions") if p["id"] in ours)
    unreal = 0.0
    for p in positions:
        if p["id"] in ours:
            px = float(p.get("mark_price") or p.get("last_price") or p["entry_price"])
            unreal += float(p["quantity"]) * (px - float(p["entry_price"])) * float(p.get("entry_hedge_rate") or rate)
    return s1.CAPITAL_CAP_INR + realized + unreal


def caps_state(con, equity, now=None):
    """Bot day P&L vs 5% caps. Day = IST calendar day; start equity recorded on first check of the day."""
    day = time.strftime("%Y-%m-%d", time.gmtime((now or time.time()) + config.IST_OFFSET))
    row = con.execute("SELECT start_equity FROM ledger WHERE day=?", (day,)).fetchone()
    if row is None:
        con.execute("INSERT INTO ledger(day, start_equity) VALUES(?,?)", (day, equity))
        start = equity
    else:
        start = row["start_equity"]
    cap = s1.DAILY_CAP_PCT * start
    pnl = equity - start
    return dict(day=day, start=start, pnl=pnl, cap=cap,
                hit="loss" if pnl <= -cap else "profit" if pnl >= cap else None)


def guard_tripped():
    if os.path.exists(GUARD_PATH):
        with open(GUARD_PATH) as f:
            return bool(json.load(f).get("tripped"))
    return False


def snapshot(con, client, coin):
    positions = client.positions()
    rate = hedge_rate(client, positions)
    asset = client.asset(coin + "USDT")
    funds = client.funds()
    owned = owned_ids(con)
    manual = [p for p in positions if p["symbol"] == coin + "USDT" and p["id"] not in owned]
    eq = bot_equity(con, client, positions, rate or config.INR_PER_USDT)
    return dict(positions=positions, rate=rate, asset=asset, free=float(funds["balance"]), owned=owned,
                manual=manual, caps=caps_state(con, eq), price=float(asset["price"]))


# ---------- order lifecycle

def lookup_until_known(client, cid, sleep, tries=LOOKUP_TRIES):
    for i in range(tries):
        try:
            o = client.order_by_client_id(cid)
            if o is not None:
                return o
        except (Ambiguous, Locked):
            pass
        sleep(min(2 ** i, 8))
    return None


def submit_with_reconcile(con, client, row, send, sleep, alert):
    """Send a create-order request with a pre-journaled client_order_id and classify the outcome.
    Returns exchange order dict (ACCEPTED) or None; sets FAILED / RECONCILE_REQUIRED itself."""
    cid = row["client_order_id"]
    for attempt in range(3):
        existing = client.order_by_client_id(cid) if attempt else None      # before ANY resubmit: look up
        if existing:
            set_order(con, row["id"], state="ACCEPTED", exchange_order_id=existing.get("id") or existing.get("order_id"))
            return existing
        set_order(con, row["id"], state="SUBMITTED")
        try:
            resp = send()
            set_order(con, row["id"], state="ACCEPTED", exchange_order_id=(resp or {}).get("order_id"))
            return resp or {}
        except Rejected as e:
            if e.status == 409:                                              # cid already exists: it was accepted
                o = client.order_by_client_id(cid)
                set_order(con, row["id"], state="ACCEPTED", exchange_order_id=(o or {}).get("id"))
                return o or {}
            set_order(con, row["id"], state="FAILED", error=f"rejected {e.status}: {e.errors}")
            return None
        except Locked:
            sleep(min(2 ** attempt, 8))                                      # bounded; loop looks up first
            continue
        except Ambiguous as e:                                               # UNKNOWN: never resubmit blindly
            o = lookup_until_known(client, cid, sleep)
            if o:
                set_order(con, row["id"], state="ACCEPTED", exchange_order_id=o.get("id") or o.get("order_id"))
                return o
            set_order(con, row["id"], state="RECONCILE_REQUIRED", error=f"ambiguous ({e.status}), not found yet")
            event(con, "reconcile", f"{row['coin']}: order {cid} outcome unknown; not resubmitted", alert)
            return None
    set_order(con, row["id"], state="FAILED", error="exchange busy (423/429) after retries")
    return None


def poll_fill(con, client, row, sleep):
    for i in range(POLL_TRIES):
        try:
            o = client.order_by_client_id(row["client_order_id"])
        except (Ambiguous, Locked):
            o = None
        status = (o or {}).get("status", "")
        if status in TERMINAL_OK:
            set_order(con, row["id"], state="FILLED", fill_price=float(o["filled_price"]),
                      filled_qty=float(o["filled_quantity"]), position_id=o.get("future_position_uuid"))
            return o
        if status in TERMINAL_BAD:
            set_order(con, row["id"], state="FAILED", error=f"order {status}")
            return None
        sleep(min(1 + i, 5))
    set_order(con, row["id"], state="RECONCILE_REQUIRED", error="no terminal status yet")
    return None


def verify_entry(con, client, row, sleep, alert):
    """Confirm the position, quantity and a valid exchange stop computed from the ACTUAL fill."""
    row = con.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()
    pos = next((p for p in client.positions() if p["id"] == row["position_id"]), None)
    if pos is None:
        set_order(con, row["id"], state="RECONCILE_REQUIRED", error="filled but position not visible")
        return False
    con.execute("INSERT OR IGNORE INTO owned(position_id, coin, client_order_id, opened_at) VALUES(?,?,?,?)",
                (pos["id"], row["coin"], row["client_order_id"], int(time.time())))
    fill, liq = row["fill_price"], float(pos.get("liquidation_price") or 0)
    spec = client.asset(row["coin"] + "USDT")
    step = float(spec["price_step"])
    stop = floor_to(fill - s1.SL_ATR * row["atr"], step)
    if not (liq < stop < fill):
        set_order(con, row["id"], state="FAILED", error=f"stop {stop} invalid (fill {fill}, liq {liq})")
        event(con, "stop", f"{row['coin']}: cannot place a valid stop (fill {fill}, liq {liq}). "
                           f"POSITION IS UNPROTECTED - add a stop in the Mudrex app now.", alert)
        return False
    current = float((pos.get("stoploss") or {}).get("price") or 0)
    if not (liq < current < fill and abs(current - stop) <= 2 * step):
        try:
            client.set_stoploss(pos["id"], fmt_step(stop, step), f"{row['client_order_id']}-SL")
        except (Rejected, Locked, Ambiguous) as e:
            event(con, "stop", f"{row['coin']}: stop attach error {e}", None)
        for _ in range(3):
            sleep(1)
            pos = next((p for p in client.positions() if p["id"] == row["position_id"]), None)
            current = float(((pos or {}).get("stoploss") or {}).get("price") or 0)
            if liq < current < fill:
                break
    if liq < current < fill:
        set_order(con, row["id"], state="VERIFIED", stop_price=fmt_step(current, step))
        return True
    set_order(con, row["id"], state="FAILED", error="stop-loss could not be verified on the exchange")
    event(con, "stop", f"{row['coin']}: stop-loss NOT verified. POSITION MAY BE UNPROTECTED - check Mudrex now.",
          alert)
    return False


def run_close(con, client, row, sleep, alert):
    if row["position_id"] not in owned_ids(con):
        set_order(con, row["id"], state="FAILED", error="refused: position not owned by the bot")
        return
    if not any(p["id"] == row["position_id"] for p in client.positions()):
        set_order(con, row["id"], state="VERIFIED", error="already closed (stop hit?)")
        con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (int(time.time()), row["position_id"]))
        return
    set_order(con, row["id"], state="SUBMITTED")
    try:
        client.close_position(row["position_id"])
        set_order(con, row["id"], state="ACCEPTED")
    except Rejected as e:
        set_order(con, row["id"], state="FAILED", error=f"close rejected {e.status}: {e.errors}")
        return
    except (Ambiguous, Locked):
        pass                                                         # outcome unknown: verify by position state
    for i in range(POLL_TRIES):
        try:
            gone = not any(p["id"] == row["position_id"] for p in client.positions())
        except (Ambiguous, Locked):
            gone = False
        if gone:
            set_order(con, row["id"], state="VERIFIED")
            con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (int(time.time()), row["position_id"]))
            return
        sleep(min(1 + i, 5))
    set_order(con, row["id"], state="RECONCILE_REQUIRED", error="close not confirmed")
    event(con, "reconcile", f"{row['coin']}: close not confirmed; check Mudrex", alert)


def run_open(con, client, row, sleep, alert):
    """Returns False if the plan must stop making entries (stop verification failure)."""
    snap = snapshot(con, client, row["coin"])
    sym = row["coin"] + "USDT"
    if snap["manual"]:
        set_order(con, row["id"], state="FAILED", error="refused: manual/unowned position on this symbol")
        event(con, "manual", f"{row['coin']}: you have a manual position on {sym}; the bot will not trade it.", alert)
        return True
    if any(p["symbol"] == sym and p["id"] in snap["owned"] for p in snap["positions"]):
        set_order(con, row["id"], state="FAILED", error="bot already holds this symbol")
        return True
    if snap["caps"]["hit"]:
        set_order(con, row["id"], state="FAILED", error=f"daily {snap['caps']['hit']} cap hit")
        return True
    if guard_tripped():
        set_order(con, row["id"], state="FAILED", error="performance guard tripped")
        return True
    if not snap["rate"]:
        set_order(con, row["id"], state="FAILED", error="no current INR hedge rate from Mudrex")
        return True
    price, planned = snap["price"], row["planned_price"]
    if planned and abs(price / planned - 1) > MAX_DRIFT:
        set_order(con, row["id"], state="FAILED", error=f"price drifted {price / planned - 1:+.1%} since plan")
        return True
    a = snap["asset"]
    step, min_qty, min_notional = float(a["quantity_step"]), float(a["min_contract"]), float(a["min_notional_value"])
    notional_inr = min(row["planned_notional_inr"], s1.LEV * s1.CAPITAL_CAP_INR)
    qty = floor_to(notional_inr / snap["rate"] / price, step)
    if qty < min_qty or qty * price < min_notional:
        set_order(con, row["id"], state="FAILED", error="below Mudrex minimum at live price")
        return True
    if qty * price * snap["rate"] / s1.LEV > snap["free"]:
        set_order(con, row["id"], state="FAILED", error="not enough free margin")
        return True
    initial_stop = fmt_step(floor_to(price - s1.SL_ATR * row["atr"], float(a["price_step"])), float(a["price_step"]))
    qty_s = fmt_step(qty, step)
    set_order(con, row["id"], qty=qty_s)
    try:
        client.set_leverage(sym, s1.LEV)
    except Rejected as e:
        set_order(con, row["id"], state="FAILED", error=f"leverage rejected: {e.errors}")
        return True
    except (Locked, Ambiguous):
        pass                        # setting leverage is idempotent; if it did not apply, the order still uses 1x+
    accepted = submit_with_reconcile(con, client, con.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone(),
                                     lambda: client.place_market_long(sym, qty_s, row["client_order_id"], initial_stop),
                                     sleep, alert)
    if accepted is None:
        return con.execute("SELECT state FROM orders WHERE id=?", (row["id"],)).fetchone()["state"] != "RECONCILE_REQUIRED"
    if poll_fill(con, client, row, sleep) is None:
        return True
    return verify_entry(con, client, row, sleep, alert)


def plan_outcome(con, plan_id):
    states = [r["state"] for r in con.execute("SELECT state FROM orders WHERE plan_id=?", (plan_id,))]
    if any(s in ("RECONCILE_REQUIRED", "SUBMITTED", "ACCEPTED", "FILLED") for s in states):
        return "RECONCILE_REQUIRED"
    if all(s == "VERIFIED" for s in states):
        return "COMPLETE"
    if any(s == "VERIFIED" for s in states):
        return "PARTIAL"
    return "FAILED"


def preflight():
    if os.path.exists(STOP_PATH):
        return "STOP file present"
    if not live_enabled():
        return "LIVE_TRADING_ENABLED is not true"
    return None


def execute(con, client, plan_id, approver, sleep=time.sleep, alert=None):
    """Claim and run an approved plan. Returns (final_state, [order summaries])."""
    why = preflight()
    if why:
        return "REFUSED", [why]
    why = claim(con, plan_id, approver)
    if why:
        return "REFUSED", [why]
    con.execute("UPDATE plans SET state='EXECUTING' WHERE id=?", (plan_id,))
    event(con, "execute", f"plan {plan_id} approved by {approver}")
    recover_ownership(con, client)
    entries_allowed = True
    for row in con.execute("SELECT * FROM orders WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall():
        why = preflight()                                            # re-checked before EVERY order
        if why:
            set_order(con, row["id"], state="FAILED", error=f"halted: {why}")
            continue
        if row["action"] == "CLOSE":
            run_close(con, client, row, sleep, alert)
        elif not entries_allowed:
            set_order(con, row["id"], state="FAILED", error="halted: earlier stop-loss not verified")
        else:
            entries_allowed = run_open(con, client, row, sleep, alert)
    final = plan_outcome(con, plan_id)
    con.execute("UPDATE plans SET state=? WHERE id=?", (final, plan_id))
    summary = [f"{r['action']} {r['coin']}: {r['state']}" + (f" ({r['error']})" if r["error"] else "")
               for r in con.execute("SELECT * FROM orders WHERE plan_id=? ORDER BY seq", (plan_id,))]
    event(con, "execute", f"plan {plan_id} finished {final}: " + "; ".join(summary), alert)
    return final, summary


def reconcile(con, client, sleep=time.sleep, alert=None, min_age=900):
    """After a crash/restart: resolve every order that is not terminal, by client_order_id. Never resubmits.
    Only touches plans approved more than min_age seconds ago, so it cannot race a run still in progress."""
    stale = [r["id"] for r in con.execute(
        "SELECT id FROM plans WHERE state IN ('EXECUTING','APPROVED','RECONCILE_REQUIRED') AND approved_at <= ?",
        (int(time.time()) - min_age,))]
    if not stale:
        return
    marks = ",".join("?" * len(stale))
    for row in con.execute(f"SELECT * FROM orders WHERE plan_id IN ({marks}) AND state IN "
                           "('SUBMITTED','ACCEPTED','FILLED','RECONCILE_REQUIRED')", stale).fetchall():
        if row["action"] == "CLOSE":
            if not any(p["id"] == row["position_id"] for p in client.positions()):
                set_order(con, row["id"], state="VERIFIED")
                con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (int(time.time()), row["position_id"]))
            continue
        if row["state"] != "FILLED":
            o = lookup_until_known(client, row["client_order_id"], sleep)
            if o is None:
                # never reached the exchange (or genuinely lost): safe to mark FAILED, still no resubmission
                set_order(con, row["id"], state="FAILED", error="not found on exchange after restart")
                continue
            if o.get("status") in TERMINAL_BAD:
                set_order(con, row["id"], state="FAILED", error=f"order {o['status']}")
                continue
            if o.get("status") not in TERMINAL_OK:
                continue                                              # still working: next reconcile
            set_order(con, row["id"], state="FILLED", fill_price=float(o["filled_price"]),
                      filled_qty=float(o["filled_quantity"]), position_id=o.get("future_position_uuid"))
        verify_entry(con, client, row, sleep, alert)
    for pid in stale:
        # orders a crashed run never sent stay unsent: FAILED, never resumed without a new approval
        con.execute("UPDATE orders SET state='FAILED', error='not submitted (run interrupted)' "
                    "WHERE plan_id=? AND state='PLANNED'", (pid,))
        con.execute("UPDATE plans SET state=? WHERE id=?", (plan_outcome(con, pid), pid))
