"""Durable, restart-safe execution for S1 live orders (SQLite journal). REAL MONEY path.

Plan states : PLANNED -> APPROVED -> EXECUTING -> COMPLETE | PARTIAL | FAILED | RECONCILE_REQUIRED
Order states: PLANNED -> SUBMITTED -> ACCEPTED -> FILLED -> VERIFIED   (or FAILED / RECONCILE_REQUIRED)
Rules:
  - Exactly one approver wins: claim() takes an exclusive SQLite write lock (BEGIN IMMEDIATE).
  - Every order has a unique client_order_id, written to the journal BEFORE the request is sent.
  - A timeout / transport error / 5xx is UNKNOWN, not failed: look the order up by client_order_id and poll.
    An order that cannot be confirmed becomes RECONCILE_REQUIRED and is NEVER resubmitted blindly.
    ANY entry that is not definitively VERIFIED or definitively never-sent HALTS all further entries in the plan.
  - 423/429: bounded backoff; look up by client_order_id before any resubmission.
  - Ownership = Mudrex position UUID recorded from our own fills (local `owned` table is authoritative).
    A basket symbol with any position we do not own (manual long or short) is refused and alerted.
  - STOP and LIVE_TRADING_ENABLED are re-checked immediately before EVERY order-changing request (leverage,
    entry, close). Protective stop-loss attach/edit is never blocked by STOP.
  - Before each entry: positions/ownership, free margin, bot caps, guard, asset spec, live price (drift),
    leverage read back = exactly LEV isolated, and a recent Mudrex hedge rate (sized with a safety buffer).
  - Stops are fill-aware: target = actual fill - SL_ATR*ATR; VERIFIED only if the exchange stop is within
    tolerance of the target and fill > stop > liquidation (liquidation must be known). Missing stop -> POST,
    wrong stop -> PATCH with its stoploss_order_id. Any failure -> alert + halt entries.
  - Daily caps are bot-only: 5% of the bot's day-start equity (Rs 5,000 allocation + bot realized P&L from the
    local ledger + bot unrealized). Deposits, withdrawals, manual positions excluded. If bot P&L cannot be
    confirmed (closed position missing from truncated history), entries are blocked (fail closed).
    Caps BLOCK NEW ENTRIES only; closing needs a human-approved plan (the watcher offers "Close all").
  - Hedge rate: Mudrex exposes no quote endpoint, so the most recent rate Mudrex itself applied (open position
    or INR order) is used; entries are sized with HEDGE_BUFFER headroom and the order's actual hedge_rate is
    checked after the fill.
"""
import json
import math
import os
import sqlite3
import time
from datetime import datetime

import config
import s1
from mudrex_client import Ambiguous, ApiError, Locked, Rejected

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "execution.db")
STOP_PATH = os.path.join(HERE, "STOP")
GUARD_PATH = os.path.join(HERE, "guard.json")
PLAN_MAX_AGE = 3 * 3600
ENTRY_MAX_AGE = 15 * 60      # a plan that BUYS must be approved within 15 min; after that a fresh plan is made
SUBMIT_GRACE = 5 * 60        # ...and each buy must be SENT within 15 + 5 min of the plan (basket execution time)
MAX_DRIFT = 0.02            # refuse an entry if the live price moved >2% from the planned price
HEDGE_BUFFER = 1.03         # size as if INR were 3% weaker than the last applied rate
HEDGE_MAX_AGE = 7 * 86400
POLL_TRIES, LOOKUP_TRIES = 10, 6
TERMINAL_OK = {"FILLED"}
TERMINAL_BAD = {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"}


class Halt(Exception):
    """STOP or live flag changed right before an order-changing request."""


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
        opened_at INTEGER, closed_at INTEGER, realized_pnl REAL);
    CREATE TABLE IF NOT EXISTS ledger(day TEXT PRIMARY KEY, start_equity REAL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at INTEGER, kind TEXT, msg TEXT);
    """)
    if "realized_pnl" not in {r["name"] for r in con.execute("PRAGMA table_info(owned)")}:
        con.execute("ALTER TABLE owned ADD COLUMN realized_pnl REAL")
    return con


def event(con, kind, msg, alert=None):
    """Journal an event and (optionally) alert. An alert failure can never interrupt execution."""
    con.execute("INSERT INTO events(at, kind, msg) VALUES(?,?,?)", (int(time.time()), kind, msg))
    if alert:
        try:
            alert(msg)
        except Exception as e:                                   # noqa: BLE001 - delivery must not break trading state
            con.execute("INSERT INTO events(at, kind, msg) VALUES(?,?,?)",
                        (int(time.time()), "alert_failed", f"{type(e).__name__}: {e}"[:300]))


def set_order(con, oid, **kw):
    kw["updated_at"] = int(time.time())
    con.execute(f"UPDATE orders SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), oid))


def order_row(con, oid):
    return con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()


def record_plan(con, decision_day, orders, payload):
    """Store a new PLANNED plan. orders: dicts with coin, action, position_id?, planned_price, notional_inr, atr."""
    now = int(time.time())
    con.execute("BEGIN IMMEDIATE")          # one atomic step: a concurrent claim sees the old plan or the new one
    try:
        cur = con.execute("INSERT INTO plans(created_at, decision_day, state, payload) VALUES(?,?,?,?)",
                          (now, decision_day, "PLANNED", json.dumps(payload)))
        pid = cur.lastrowid
        # only ONE approvable plan exists at a time: older unapproved plans (and their buttons) become invalid,
        # except a pending cap-hit "Close all" plan, which only another cap plan may replace
        con.execute("UPDATE plans SET state='FAILED', note=? WHERE state='PLANNED' AND id<>? AND "
                    "(? OR COALESCE(json_extract(payload, '$.reason'), '') <> 'cap')",
                    (f"superseded by plan {pid}", pid, payload.get("reason") == "cap"))
        for i, o in enumerate(orders):
            cid = f"s1-{pid}-{i}-{o['coin']}-{o['action'][0]}"[:64]
            con.execute("""INSERT INTO orders(plan_id, seq, coin, action, client_order_id, state, position_id,
                           planned_price, planned_notional_inr, atr, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (pid, i, o["coin"], o["action"], cid, "PLANNED", o.get("position_id"),
                         o.get("planned_price"), o.get("notional_inr"), o.get("atr"), now))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return pid


def supersede_pending(con, note):
    """A newer plan found nothing to do: older approvable plans (and their buttons) become invalid too.
    A pending cap-hit Close-all plan is kept."""
    con.execute("UPDATE plans SET state='FAILED', note=? WHERE state='PLANNED' AND "
                "COALESCE(json_extract(payload, '$.reason'), '') <> 'cap'", (note,))


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
        has_open = con.execute("SELECT 1 FROM orders WHERE plan_id=? AND action='OPEN'", (plan_id,)).fetchone()
        age = time.time() - row["created_at"]
        if age > PLAN_MAX_AGE or (has_open and age > ENTRY_MAX_AGE):
            con.execute("UPDATE plans SET state='FAILED', note='expired' WHERE id=?", (plan_id,))
            con.execute("COMMIT")
            return (f"EXPIRED: plans with buys are valid {ENTRY_MAX_AGE // 60} minutes (prices move)"
                    if has_open and age <= PLAN_MAX_AGE else "EXPIRED: plan is older than 3 hours")
        # execution lease: never two plans at once; no new entries while another plan needs reconciling
        busy = con.execute("SELECT id FROM plans WHERE state IN ('APPROVED','EXECUTING') AND id<>?",
                           (plan_id,)).fetchone()
        if busy:
            con.execute("ROLLBACK")
            return f"plan {busy['id']} is still executing"
        unresolved = con.execute("SELECT id FROM plans WHERE state='RECONCILE_REQUIRED' AND id<>?",
                                 (plan_id,)).fetchone()
        if has_open and unresolved:
            con.execute("ROLLBACK")
            return f"plan {unresolved['id']} needs reconciliation first (python live_trader.py reconcile)"
        con.execute("UPDATE plans SET state='APPROVED', approved_by=?, approved_at=? WHERE id=?",
                    (approver, int(time.time()), plan_id))
        con.execute("COMMIT")
        return None
    except Exception:
        con.execute("ROLLBACK")
        raise


def latest_plan(con):
    return con.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()


# ---------- account state

def floor_to(x, step):
    return math.floor(x / step + 1e-9) * step


def fmt_step(x, step):
    import decimal
    q = decimal.Decimal(str(step))
    return str((decimal.Decimal(str(x)) / q).to_integral_value(rounding=decimal.ROUND_FLOOR) * q)


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def hedge_rate(client, positions):
    """Most recent INR-per-USDT rate Mudrex itself applied (open position or INR order within HEDGE_MAX_AGE).
    None if unknown or stale. Mudrex has no documented quote endpoint."""
    cands = []
    for p in positions:
        if p.get("entry_hedge_rate") and _ts(p.get("created_at")):      # undated rates are never trusted as fresh
            cands.append((_ts(p["created_at"]), float(p["entry_hedge_rate"])))
    orders, _ = client.history("orders")
    for o in orders:
        if o.get("hedge_rate") and _ts(o.get("created_at")):
            cands.append((_ts(o["created_at"]), float(o["hedge_rate"])))
    if not cands:
        return None
    at, rate = max(cands)
    return rate if time.time() - at <= HEDGE_MAX_AGE else None


def owned_ids(con):
    return {r["position_id"] for r in con.execute("SELECT position_id FROM owned WHERE closed_at IS NULL")}


def quarantined(con, position_id):
    """A position that did not match its order: never owned, amended or closed by the bot."""
    return con.execute("SELECT 1 FROM orders WHERE position_id=? AND error LIKE 'position mismatch%'",
                       (position_id,)).fetchone() is not None


def recover_ownership(con, client):
    """Re-add ownership of positions the bot opened AND fully validated: a history order counts only if the local
    journal has the same client_order_id VERIFIED on the same position (identity, stop and notional checked).
    Anything else is treated as manual (never touched) until reconcile() validates it."""
    orders, truncated = client.history("orders")
    for o in orders:
        cid = o.get("client_order_id") or ""
        verified = con.execute("SELECT 1 FROM orders WHERE client_order_id=? AND state='VERIFIED' AND position_id=?",
                               (cid, o.get("future_position_uuid"))).fetchone()
        if cid.startswith("s1-") and cid.endswith("-O") and o.get("status") == "FILLED" and verified \
                and o.get("future_position_uuid") and not quarantined(con, o["future_position_uuid"]):
            con.execute("INSERT OR IGNORE INTO owned(position_id, coin, client_order_id, opened_at) VALUES(?,?,?,?)",
                        (o["future_position_uuid"], o["symbol"].removesuffix("USDT"), cid, int(time.time())))
    return truncated


def sync_owned(con, client, positions):
    """Mark bot positions that disappeared as closed and fill in their realized P&L from history.
    Returns the number of closed bot positions whose P&L is still unknown."""
    open_ids = {p["id"] for p in positions}
    now = int(time.time())
    for r in con.execute("SELECT position_id FROM owned WHERE closed_at IS NULL").fetchall():
        if r["position_id"] not in open_ids:
            con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (now, r["position_id"]))
    missing = con.execute("SELECT position_id FROM owned WHERE closed_at IS NOT NULL AND realized_pnl IS NULL"
                          ).fetchall()
    if missing:
        hist = {p["id"]: p for p in client.history("positions")[0]}
        for r in missing:
            p = hist.get(r["position_id"])
            if p is not None and p.get("pnl") is not None:
                con.execute("UPDATE owned SET realized_pnl=? WHERE position_id=?", (float(p["pnl"]), r["position_id"]))
    return con.execute("SELECT COUNT(*) FROM owned WHERE closed_at IS NOT NULL AND realized_pnl IS NULL").fetchone()[0]


def unrealized_inr(con, positions, rate):
    ours = owned_ids(con)
    u = 0.0
    for p in positions:
        if p["id"] in ours:
            px = float(p.get("mark_price") or p.get("last_price") or p["entry_price"])
            u += float(p["quantity"]) * (px - float(p["entry_price"])) * float(p.get("entry_hedge_rate") or rate)
    return u


class PnlUnknown(Exception):
    pass


def bot_equity(con, client, positions, rate):
    """Bot-only equity: allocation + realized P&L of our closed positions (local ledger) + our unrealized.
    Raises PnlUnknown if a closed bot position's P&L cannot be confirmed (fail closed)."""
    if sync_owned(con, client, positions):
        raise PnlUnknown("a closed bot position's P&L is not yet visible in Mudrex history")
    realized = con.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM owned WHERE closed_at IS NOT NULL"
                           ).fetchone()[0]
    return s1.CAPITAL_CAP_INR + realized + unrealized_inr(con, positions, rate)


def ist_day(t=None):
    return time.strftime("%Y-%m-%d", time.gmtime((t or time.time()) + config.IST_OFFSET))


def caps_state(con, equity, unreal=0.0, now=None):
    """Bot day P&L vs 5% caps (IST day). The day-start baseline = allocation + P&L realized BEFORE today's IST
    midnight + unrealized at the first check of the day, so realized losses earlier today always count even if
    the process restarted. (Unrealized moves between midnight and the first check are the one blind spot.)"""
    now = now or time.time()
    day = ist_day(now)
    row = con.execute("SELECT start_equity FROM ledger WHERE day=?", (day,)).fetchone()
    if row is None:
        midnight = int((now + config.IST_OFFSET) // 86400 * 86400 - config.IST_OFFSET)
        before = con.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM owned WHERE closed_at IS NOT NULL "
                             "AND closed_at < ?", (midnight,)).fetchone()[0]
        start = s1.CAPITAL_CAP_INR + before + unreal
        con.execute("INSERT INTO ledger(day, start_equity) VALUES(?,?)", (day, start))
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


# ---------- gates

def preflight():
    if os.path.exists(STOP_PATH):
        return "STOP file present"
    if not live_enabled():
        return "LIVE_TRADING_ENABLED is not true"
    return None


def gate():
    """Called immediately before every order-changing request."""
    why = preflight()
    if why:
        raise Halt(why)


# ---------- order lifecycle

def lookup_until_known(client, cid, sleep, tries=LOOKUP_TRIES):
    """The order, or None only if EVERY lookup got a definitive 'not found' (404).
    Raises Ambiguous if any lookup failed (timeout/5xx/423): a partly unanswered lookup must never be read as
    'not placed' (the exchange may be slow to show it, or the failing call may have been the one that knew)."""
    failed = 0
    for i in range(tries):
        try:
            o = client.order_by_client_id(cid)
            if o is not None:
                return o
        except (Ambiguous, Locked):
            failed += 1
        sleep(min(2 ** i, 8))
    if failed:
        raise Ambiguous(0, f"lookup of {cid} inconclusive ({failed}/{tries} lookups failed)")
    return None


def submit_with_reconcile(con, client, row, send, sleep, alert):
    """Send a create-order request with a pre-journaled client_order_id and classify the outcome.
    Returns the exchange order dict (ACCEPTED) or None; sets FAILED / RECONCILE_REQUIRED itself.
    Before ANY resubmission the order is looked up by client_order_id."""
    cid = row["client_order_id"]
    for attempt in range(3):
        if attempt:
            existing = lookup_until_known(client, cid, sleep, tries=2)
            if existing:
                set_order(con, row["id"], state="ACCEPTED",
                          exchange_order_id=existing.get("id") or existing.get("order_id"))
                return existing
        gate()                                                         # STOP / live flag, right before sending
        set_order(con, row["id"], state="SUBMITTED")
        try:
            resp = send()
            set_order(con, row["id"], state="ACCEPTED", exchange_order_id=(resp or {}).get("order_id"))
            return resp or {}
        except Rejected as e:
            if e.status == 409:                                        # cid already exists: it was accepted
                o = lookup_until_known(client, cid, sleep)
                set_order(con, row["id"], state="ACCEPTED", exchange_order_id=(o or {}).get("id"))
                return o or {}
            set_order(con, row["id"], state="FAILED", error=f"rejected {e.status}: {e.errors}")
            return None
        except Locked:
            set_order(con, row["id"], state="PLANNED")                 # 423/429: exchange did not take it
            sleep(min(2 ** attempt, 8))
            continue
        except Ambiguous as e:                                         # UNKNOWN: never resubmit blindly
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


def stop_tolerance(fill, step):
    return max(2 * step, 0.002 * fill)


def unprotected(con, oid, row, why, alert):
    """A filled position whose protection is not confirmed: never FAILED (that would release the entry block)."""
    set_order(con, oid, state="RECONCILE_REQUIRED", error=why)
    event(con, "stop", f"{row['coin']}: {why}. POSITION MAY BE UNPROTECTED - check Mudrex now. "
                       f"New entries are blocked until this is resolved.", alert)
    return False


def verify_entry(con, client, oid, sleep, alert):
    """Confirm the position identity, quantity and an exchange stop at the fill-derived target.
    Returns True when protected; the caller sets VERIFIED only after the approved-notional check also passes,
    so a crash in between leaves the order FILLED (re-checked by reconcile), never VERIFIED unchecked."""
    row = order_row(con, oid)
    pos = next((p for p in client.positions() if p["id"] == row["position_id"]), None)
    if pos is None:
        set_order(con, oid, state="RECONCILE_REQUIRED", error="filled but position not visible")
        event(con, "reconcile", f"{row['coin']}: filled but position not visible yet", alert)
        return False
    fill = row["fill_price"]
    problems = []
    if pos.get("symbol") != row["coin"] + "USDT":
        problems.append(f"symbol {pos.get('symbol')}")
    if pos.get("order_type") != "LONG":
        problems.append(f"side {pos.get('order_type')}")
    if pos.get("trade_currency") not in (None, "INR"):
        problems.append(f"currency {pos.get('trade_currency')}")
    try:
        if float(pos.get("leverage")) != float(s1.LEV):
            problems.append(f"leverage {pos.get('leverage')}")
    except (TypeError, ValueError):
        problems.append(f"leverage {pos.get('leverage')}")
    if not row["filled_qty"] or abs(float(pos["quantity"]) - row["filled_qty"]) > 1e-9 * max(1, row["filled_qty"]):
        problems.append(f"qty {pos['quantity']} vs filled {row['filled_qty']}")
    if problems:                                     # not the position we approved: no ownership, no writes to it
        set_order(con, oid, state="RECONCILE_REQUIRED", error="position mismatch: " + "; ".join(problems))
        con.execute("DELETE FROM owned WHERE position_id=? AND closed_at IS NULL", (pos["id"],))
        event(con, "reconcile", f"{row['coin']}: position does not match the order ({'; '.join(problems)}). "
                                f"The bot will not touch it. Check its stop-loss in Mudrex now; entries halted.", alert)
        return False
    con.execute("INSERT OR IGNORE INTO owned(position_id, coin, client_order_id, opened_at) VALUES(?,?,?,?)",
                (pos["id"], row["coin"], row["client_order_id"], int(time.time())))
    try:
        liq = float(pos.get("liquidation_price"))
    except (TypeError, ValueError):
        liq = float("nan")
    if not (math.isfinite(liq) and 0 < liq < fill):
        return unprotected(con, oid, row, f"liquidation price unknown/invalid ({liq}); stop NOT verified", alert)
    step = float(client.asset(row["coin"] + "USDT")["price_step"])
    target = floor_to(fill - s1.SL_ATR * row["atr"], step)
    tol = stop_tolerance(fill, step)
    if not (liq < target < fill):
        return unprotected(con, oid, row, f"no valid stop possible (target {target}, fill {fill}, liq {liq})", alert)
    ok = lambda sl: liq < sl < fill and abs(sl - target) <= tol     # noqa: E731
    sl_info = pos.get("stoploss") or {}
    current = float(sl_info.get("price") or 0)
    if not ok(current):
        try:
            if current > 0:
                if not sl_info.get("order_id"):
                    raise Rejected(0, "existing stop has no order_id to amend")
                client.edit_stoploss(pos["id"], sl_info["order_id"], fmt_step(target, step))
            else:
                client.set_stoploss(pos["id"], fmt_step(target, step), f"{row['client_order_id']}-SL")
        except ApiError as e:
            event(con, "stop", f"{row['coin']}: stop attach/edit error {e}", None)
        for _ in range(3):
            sleep(1)
            pos = next((p for p in client.positions() if p["id"] == row["position_id"]), None)
            current = float(((pos or {}).get("stoploss") or {}).get("price") or 0)
            if ok(current):
                break
    if ok(current):
        set_order(con, oid, stop_price=fmt_step(current, step))
        return True
    return unprotected(con, oid, row, f"stop-loss not verified (exchange {current}, target {target})", alert)


def protect_and_check(con, client, row, o, sleep, alert):
    """Stop first (protection before accounting), then the approved-notional check; VERIFIED only if both pass."""
    protected = verify_entry(con, client, row["id"], sleep, alert)
    within = fill_within_approval(con, order_row(con, row["id"]), o, alert)
    if protected and within:
        set_order(con, row["id"], state="VERIFIED")
        return True
    return False


def run_close(con, client, row, sleep, alert):
    if row["position_id"] not in owned_ids(con):
        set_order(con, row["id"], state="FAILED", error="refused: position not owned by the bot")
        return
    if not any(p["id"] == row["position_id"] for p in client.positions()):
        set_order(con, row["id"], state="VERIFIED", error="already closed (stop hit?)")
        con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (int(time.time()), row["position_id"]))
        return
    gate()
    set_order(con, row["id"], state="SUBMITTED")
    try:
        client.close_position(row["position_id"])
        set_order(con, row["id"], state="ACCEPTED")
    except Rejected as e:
        set_order(con, row["id"], state="FAILED", error=f"close rejected {e.status}: {e.errors}")
        return
    except (Ambiguous, Locked):
        pass                                                           # unknown: verify by position state
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


def fail(con, oid, why):
    set_order(con, oid, state="FAILED", error=why)
    return True                                    # a definitive, never-sent refusal does not halt other entries


def run_open(con, client, row, sleep, alert):
    """Returns True if further entries may proceed, False to HALT the plan's entries."""
    sym = row["coin"] + "USDT"
    positions = client.positions()
    owned = owned_ids(con)
    if [p for p in positions if p["symbol"] == sym and p["id"] not in owned]:
        event(con, "manual", f"{row['coin']}: you have a manual position on {sym}; the bot will not trade it.", alert)
        return fail(con, row["id"], "refused: manual/unowned position on this symbol")
    if any(p["symbol"] == sym for p in positions):
        return fail(con, row["id"], "bot already holds this symbol")
    rate = hedge_rate(client, positions)
    if not rate:
        return fail(con, row["id"], "no recent INR hedge rate from Mudrex")
    try:
        eq = bot_equity(con, client, positions, rate)
    except PnlUnknown as e:
        return fail(con, row["id"], f"bot P&L unconfirmed: {e}")
    caps = caps_state(con, eq, unrealized_inr(con, positions, rate))
    if caps["hit"]:
        return fail(con, row["id"], f"daily {caps['hit']} cap hit")
    if guard_tripped():
        return fail(con, row["id"], "performance guard tripped")
    a = client.asset(sym)
    price, planned = float(a["price"]), row["planned_price"]
    if planned and abs(price / planned - 1) > MAX_DRIFT:
        return fail(con, row["id"], f"price drifted {price / planned - 1:+.1%} since plan")
    step, min_qty, min_notional = float(a["quantity_step"]), float(a["min_contract"]), float(a["min_notional_value"])
    notional_inr = min(row["planned_notional_inr"], s1.LEV * s1.CAPITAL_CAP_INR)
    held = sum(float(p["quantity"]) * float(p["entry_price"]) * rate for p in positions if p["id"] in owned)
    if held + notional_inr > s1.LEV * s1.CAPITAL_CAP_INR:
        return fail(con, row["id"], f"allocation cap: bot holds Rs {held:,.0f}, +Rs {notional_inr:,.0f} would exceed "
                                    f"Rs {s1.LEV * s1.CAPITAL_CAP_INR:,.0f}")
    qty = floor_to(notional_inr / (rate * HEDGE_BUFFER) / price, step)
    if qty < min_qty or qty * price < min_notional:
        return fail(con, row["id"], "below Mudrex minimum at live price")
    if qty * price * rate * HEDGE_BUFFER / s1.LEV > float(client.funds()["balance"]):
        return fail(con, row["id"], "not enough free margin")
    pstep = float(a["price_step"])
    initial_stop = fmt_step(floor_to(price - s1.SL_ATR * row["atr"], pstep), pstep)
    why = over_loss_budget(positions, owned, rate, eq, qty * price * rate, price, float(initial_stop))
    if why:
        return fail(con, row["id"], why)
    qty_s = fmt_step(qty, step)
    set_order(con, row["id"], qty=qty_s)
    gate()
    try:
        client.set_leverage(sym, s1.LEV)
    except Rejected as e:
        return fail(con, row["id"], f"leverage rejected: {e.errors}")
    except (Locked, Ambiguous):
        pass                                                           # verified below either way
    lev = client.leverage(sym)
    if lev is None or lev[0] != float(s1.LEV) or lev[1] != "ISOLATED":
        return fail(con, row["id"], f"leverage not verified as {s1.LEV}x isolated (got {lev})")
    why = stale_at_submit(con, client, row, sym)
    if why:
        return fail(con, row["id"], why)
    accepted = submit_with_reconcile(con, client, order_row(con, row["id"]),
                                     lambda: client.place_market_long(sym, qty_s, row["client_order_id"], initial_stop),
                                     sleep, alert)
    if accepted is None:
        return order_row(con, row["id"])["state"] == "FAILED"        # unknown outcome -> halt
    o = poll_fill(con, client, row, sleep)
    if o is None:
        return order_row(con, row["id"])["state"] == "FAILED"
    return protect_and_check(con, client, row, o, sleep, alert)


def over_loss_budget(positions, owned, rate, equity, notional_inr, price, stop):
    """Hard per-trade and total stop-loss budgets (s1.MAX_TRADE_STOP_RISK / MAX_TOTAL_STOP_RISK of bot equity).
    A held bot position without a readable stop counts at its full margin (notional / LEV)."""
    new = s1.stop_risk_inr(notional_inr, price, stop)
    if new > s1.MAX_TRADE_STOP_RISK * equity:
        return f"loss budget: stop risk Rs {new:,.0f} > {s1.MAX_TRADE_STOP_RISK:.0%} of Rs {equity:,.0f}"
    held = 0.0
    for p in positions:
        if p["id"] not in owned:
            continue
        n, entry = float(p["quantity"]) * float(p["entry_price"]) * rate, float(p["entry_price"])
        sl = float((p.get("stoploss") or {}).get("price") or 0)
        held += s1.stop_risk_inr(n, entry, sl) if 0 < sl < entry else n / s1.LEV
    if held + new > s1.MAX_TOTAL_STOP_RISK * equity:
        return (f"loss budget: all stops would lose Rs {held + new:,.0f} > {s1.MAX_TOTAL_STOP_RISK:.0%} "
                f"of Rs {equity:,.0f}")
    return None


def stale_at_submit(con, client, row, sym):
    """Freshness re-checked immediately before the entry POST: the plan's age (claim-time check + a short grace
    for the basket's own execution time) and the live price vs the planned price."""
    created = con.execute("SELECT created_at FROM plans WHERE id=?", (row["plan_id"],)).fetchone()[0]
    if time.time() - created > ENTRY_MAX_AGE + SUBMIT_GRACE:
        return "refused: plan too old by the time this order was due"
    price, planned = float(client.asset(sym)["price"]), row["planned_price"]
    if planned and abs(price / planned - 1) > MAX_DRIFT:
        return f"price drifted {price / planned - 1:+.1%} just before sending"
    return None


def fill_within_approval(con, row, o, alert):
    """Hard check: filled INR notional (at the rate Mudrex applied) must not exceed the approved notional.
    Sizing already leaves HEDGE_BUFFER headroom, so no extra allowance is added here."""
    try:
        applied = float(o.get("hedge_rate") or 0)
        actual = float(o["filled_quantity"]) * float(o["filled_price"]) * applied
    except (KeyError, TypeError, ValueError):
        applied = actual = 0.0
    allowed = min(row["planned_notional_inr"], s1.LEV * s1.CAPITAL_CAP_INR)
    if applied > 0 and actual <= allowed:
        return True
    cur = order_row(con, row["id"])
    set_order(con, row["id"], state="RECONCILE_REQUIRED",
              error=(cur["error"] + "; " if cur["error"] else "") +
              ("applied INR rate missing" if applied <= 0 else f"INR notional {actual:,.0f} > allowed {allowed:,.0f}"))
    event(con, "hedge", f"{row['coin']}: " + ("Mudrex did not report the INR rate it applied" if applied <= 0 else
                        f"filled INR notional Rs {actual:,.0f} exceeds the approved Rs {allowed:,.0f}")
          + ". Entries halted; reduce the position in Mudrex if needed.", alert)
    return False


def plan_outcome(con, plan_id):
    states = [r["state"] for r in con.execute("SELECT state FROM orders WHERE plan_id=?", (plan_id,))]
    if any(s in ("RECONCILE_REQUIRED", "SUBMITTED", "ACCEPTED", "FILLED") for s in states):
        return "RECONCILE_REQUIRED"
    if all(s == "VERIFIED" for s in states):
        return "COMPLETE"
    if any(s == "VERIFIED" for s in states):
        return "PARTIAL"
    return "FAILED"


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
    try:
        recover_ownership(con, client)
    except ApiError:
        pass                                                           # local ownership table is authoritative
    entries_allowed = True
    # closes first; any close that is not confirmed halts every entry (exposure would exceed the allocation)
    for row in con.execute("SELECT * FROM orders WHERE plan_id=? ORDER BY action='OPEN', seq", (plan_id,)).fetchall():
        why = preflight()
        if why:
            set_order(con, row["id"], state="FAILED", error=f"halted: {why}")
            continue
        try:
            if row["action"] == "CLOSE":
                run_close(con, client, row, sleep, alert)
                if order_row(con, row["id"])["state"] != "VERIFIED":
                    entries_allowed = False
            elif not entries_allowed:
                set_order(con, row["id"], state="FAILED", error="halted: an earlier order is unconfirmed")
            else:
                entries_allowed = run_open(con, client, row, sleep, alert)
        except Halt as h:
            cur = order_row(con, row["id"])["state"]
            if cur in ("PLANNED", "SUBMITTED"):
                set_order(con, row["id"], state="FAILED", error=f"halted: {h}")
        except Exception as e:                                         # anything unexpected: stop and reconcile
            cur = order_row(con, row["id"])["state"]
            set_order(con, row["id"], state="FAILED" if cur == "PLANNED" else "RECONCILE_REQUIRED",
                      error=f"{type(e).__name__}: {e}"[:300])
            event(con, "error", f"{row['coin']}: unexpected {type(e).__name__} during {row['action']}; "
                                f"entries halted, reconcile required.", alert)
            entries_allowed = False
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
        try:
            if row["action"] == "CLOSE":
                if not any(p["id"] == row["position_id"] for p in client.positions()):
                    set_order(con, row["id"], state="VERIFIED")
                    con.execute("UPDATE owned SET closed_at=? WHERE position_id=?",
                                (int(time.time()), row["position_id"]))
                continue
            o = lookup_until_known(client, row["client_order_id"], sleep)   # raises if inconclusive -> deferred
            if o is None:
                set_order(con, row["id"], state="FAILED", error="not found on exchange after restart")
                continue
            if o.get("status") in TERMINAL_BAD:
                set_order(con, row["id"], state="FAILED", error=f"order {o['status']}")
                continue
            if o.get("status") not in TERMINAL_OK:
                continue                                               # still working: next reconcile
            if row["state"] != "FILLED" or not row["fill_price"]:
                set_order(con, row["id"], state="FILLED", fill_price=float(o["filled_price"]),
                          filled_qty=float(o["filled_quantity"]), position_id=o.get("future_position_uuid"))
            protect_and_check(con, client, row, o, sleep, alert)
        except ApiError as e:
            event(con, "reconcile", f"{row['coin']}: reconcile deferred ({e})", alert)
    for pid in stale:
        # orders a crashed run never sent stay unsent: FAILED, never resumed without a new approval
        con.execute("UPDATE orders SET state='FAILED', error='not submitted (run interrupted)' "
                    "WHERE plan_id=? AND state='PLANNED'", (pid,))
        con.execute("UPDATE plans SET state=? WHERE id=?", (plan_outcome(con, pid), pid))
