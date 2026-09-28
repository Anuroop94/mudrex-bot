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
  - Before each entry: positions/ownership, free margin, fixed rupee-risk ledger, guard, asset spec, live price
    (drift), side-aware variable leverage read back in isolated mode, and a recent Mudrex hedge rate.
  - Both stop and target are fill-aware and side-aware. VERIFIED requires exchange SL and TP within tolerance and
    both before liquidation. Missing/wrong protection is repaired once; failure alerts, halts and protective-exits.
  - Daily caps are bot-only: fixed -Rs500 / +Rs500 thresholds over the IST day, measured from day-start equity
    using the local ledger plus bot unrealized P&L. Deposits, withdrawals, and manual positions are excluded. If bot P&L cannot be
    confirmed (closed position missing from truncated history), entries are blocked (fail closed).
    Caps BLOCK NEW ENTRIES only; closing needs a human-approved plan (the watcher offers "Close all").
  - PROTECTIVE EXIT (owner's standing rule, 2026-09-27): a bot-owned position from an approved entry that cannot
    be protected (stop not confirmed, liquidation unknown), breaks the approved notional, the loss budgets or the
    allocation after the fill, or hits an unexpected error, is closed at once without a further tap. It only
    ever exits the bot's own validated position; it never opens anything and is not blocked by STOP.
  - Hedge rate: Mudrex exposes no quote endpoint, so the most recent rate Mudrex itself applied (open position
    or INR order) is used; entries are sized with HEDGE_BUFFER headroom and the order's actual hedge_rate is
    checked after the fill.
"""
import calendar
import json
import hashlib
import math
import os
import sqlite3
import time
import uuid
from datetime import datetime

import config
import adaptive_risk
import s1
import trade_policy
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
LEASE = 300                 # a running execute() renews this before every order; reconcile skips leased plans
TERMINAL_OK = {"FILLED"}
TERMINAL_BAD = {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"}
OUTBOX_LEASE = 60
ALLOW_TEST_ALERT_SINK = False       # tests may explicitly opt in; production never changes this constant


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
    CREATE TABLE IF NOT EXISTS marks(at INTEGER PRIMARY KEY, equity REAL);
    CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value REAL);
    CREATE TABLE IF NOT EXISTS trade_sets(id INTEGER PRIMARY KEY, plan_id INTEGER, order_id INTEGER,
        cycle TEXT NOT NULL, set_number INTEGER, proposal_id TEXT UNIQUE NOT NULL, coin TEXT NOT NULL,
        side TEXT NOT NULL, state TEXT NOT NULL, planned_risk_inr REAL NOT NULL,
        attempted_at INTEGER, completed_at INTEGER);
    CREATE TABLE IF NOT EXISTS set_approvals(proposal_id TEXT PRIMARY KEY, cycle TEXT NOT NULL,
        expires_at INTEGER NOT NULL, channel TEXT NOT NULL, approved_by TEXT NOT NULL,
        approved_at INTEGER NOT NULL, consumed_at INTEGER);
    CREATE TABLE IF NOT EXISTS telegram_outbox(id INTEGER PRIMARY KEY, event_id INTEGER UNIQUE,
        dedupe_key TEXT UNIQUE, msg TEXT NOT NULL, critical INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL DEFAULT 0,
        delivered_at INTEGER, last_error TEXT, lease_token TEXT, lease_until INTEGER);
    CREATE INDEX IF NOT EXISTS telegram_outbox_pending ON telegram_outbox(delivered_at, next_attempt_at, created_at);
    CREATE TABLE IF NOT EXISTS universe_snapshots(cycle TEXT PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL);
    """)
    for table, col in (("owned", "realized_pnl REAL"), ("plans", "lease_until INTEGER"), ("plans", "lease_token TEXT"),
                       ("ledger", "trusted INTEGER DEFAULT 1"), ("orders", "exit_sent_at INTEGER"),
                       ("orders", "exit_attempts INTEGER DEFAULT 0"),
                       ("marks", "trusted INTEGER DEFAULT 1"), ("orders", "side TEXT DEFAULT 'LONG'"),
                       ("orders", "planned_stop REAL"), ("orders", "planned_target REAL"),
                       ("orders", "planned_leverage REAL"), ("orders", "planned_risk_inr REAL"),
                       ("orders", "planned_qty REAL"), ("orders", "set_id INTEGER"),
                       ("orders", "target_price TEXT"),
                       # exchange facts for the exact IST-boundary baseline (caps_state): Mudrex open/close times,
                       # and the position's side/qty/entry/rate while it was open
                       ("owned", "ex_opened_at INTEGER"), ("owned", "ex_closed_at INTEGER"), ("owned", "ex_side TEXT"),
                       ("owned", "ex_qty REAL"), ("owned", "ex_entry REAL"), ("owned", "ex_rate REAL"),
                       ("orders", "strategy TEXT"),
                       # Mudrex price at the IST boundary for a position carried across it (risk reserve)
                       ("owned", "ex_boundary_at INTEGER"), ("owned", "ex_boundary_px REAL")):                 # which strategy opened it (S4 manages its own)
        if col.split()[0] not in {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    if con.execute("SELECT 1 FROM kv WHERE key='marks_v2'").fetchone() is None:
        con.execute("UPDATE marks SET trusted=0")                  # marks written before 1998532 may be estimates
        con.execute("INSERT INTO kv(key, value) VALUES('marks_v2', 1)")
    if con.execute("SELECT 1 FROM kv WHERE key='ledger_v2'").fetchone() is None:
        # baselines written before equity marks existed are only exact if no bot position was open
        if con.execute("SELECT 1 FROM owned WHERE closed_at IS NULL").fetchone():
            con.execute("UPDATE ledger SET trusted=0 WHERE day=?", (ist_day(),))
        con.execute("INSERT INTO kv(key, value) VALUES('ledger_v2', 1)")
    if con.execute("SELECT 1 FROM kv WHERE key='ledger_v3'").fetchone() is None:
        # baselines written before the exchange-evidence rule (local marks, fallback INR rate) are not proof:
        # recompute them (an untrusted day is re-evaluated by caps_state and upgraded only on exchange evidence)
        con.execute("UPDATE ledger SET trusted=0")
        con.execute("INSERT INTO kv(key, value) VALUES('ledger_v3', 1)")
    return con


def enqueue_alert(con, msg, *, event_id=None, dedupe_key=None, critical=True):
    now = int(time.time())
    con.execute("INSERT OR IGNORE INTO telegram_outbox(event_id,dedupe_key,msg,critical,created_at,next_attempt_at) "
                "VALUES(?,?,?,?,?,0)", (event_id, dedupe_key, msg, int(critical), now))
    if event_id is not None:
        return con.execute("SELECT id FROM telegram_outbox WHERE event_id=?", (event_id,)).fetchone()[0]
    return con.execute("SELECT id FROM telegram_outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()[0]


def _claim_alert(con, row_id=None, critical_only=False):
    """Lease one due undelivered message. Sending is at-least-once; the event id makes duplicates visible."""
    now, token = int(time.time()), uuid.uuid4().hex
    con.execute("BEGIN IMMEDIATE")
    try:
        where, args = ["delivered_at IS NULL", "next_attempt_at<=?", "COALESCE(lease_until,0)<?"], [now, now]
        if row_id is not None:
            where.append("id=?")
            args.append(row_id)
        if critical_only:
            where.append("critical=1")
        row = con.execute("SELECT * FROM telegram_outbox WHERE " + " AND ".join(where) +
                          " ORDER BY created_at,id LIMIT 1", args).fetchone()
        if row:
            con.execute("UPDATE telegram_outbox SET lease_token=?,lease_until=? WHERE id=?",
                        (token, now + OUTBOX_LEASE, row["id"]))
        con.execute("COMMIT")
        return row, token
    except Exception:
        con.execute("ROLLBACK")
        raise


def deliver_alert(con, alert, row_id=None, critical_only=False):
    row, token = _claim_alert(con, row_id, critical_only)
    if row is None:
        return True
    try:
        if alert is None:
            raise ConnectionError("Telegram callback unavailable")
        delivered = alert(f"[event {row['event_id'] or row['id']}] {row['msg']}")
        if delivered is not True and not (ALLOW_TEST_ALERT_SINK and delivered is None):
            raise ConnectionError("Telegram callback did not explicitly confirm delivery")
        con.execute("UPDATE telegram_outbox SET delivered_at=?,lease_token=NULL,lease_until=NULL,last_error=NULL "
                    "WHERE id=? AND lease_token=?", (int(time.time()), row["id"], token))
        return True
    except Exception as e:
        attempts = int(row["attempts"] or 0) + 1
        con.execute("UPDATE telegram_outbox SET attempts=?,next_attempt_at=?,last_error=?,lease_token=NULL,"
                    "lease_until=NULL WHERE id=? AND lease_token=?",
                    (attempts, int(time.time()) + min(30 * 2 ** min(attempts, 6), 3600),
                     f"{type(e).__name__}: {e}"[:300], row["id"], token))
        con.execute("INSERT INTO events(at,kind,msg) VALUES(?,?,?)",
                    (int(time.time()), "alert_failed", f"outbox {row['id']}: {type(e).__name__}: {e}"[:300]))
        return False


def retry_alerts(con, alert, limit=20, critical_only=False):
    ok = True
    for _ in range(limit):
        due = con.execute("SELECT id FROM telegram_outbox WHERE delivered_at IS NULL AND next_attempt_at<=? "
                          + ("AND critical=1 " if critical_only else "") + "ORDER BY created_at,id LIMIT 1",
                          (int(time.time()),)).fetchone()
        if due is None:
            break
        if not deliver_alert(con, alert, due["id"], critical_only):
            ok = False
            break
    return ok


def pending_alerts(con, critical_only=False):
    return con.execute("SELECT COUNT(*) FROM telegram_outbox WHERE delivered_at IS NULL" +
                       (" AND critical=1" if critical_only else "")).fetchone()[0]


def event(con, kind, msg, alert=None, *, critical=True, dedupe_key=None):
    """Journal and durably queue an alert. Delivery failure never interrupts protection or closing."""
    cur = con.execute("INSERT INTO events(at, kind, msg) VALUES(?,?,?)", (int(time.time()), kind, msg))
    if alert is not None:
        oid = enqueue_alert(con, msg, event_id=cur.lastrowid, dedupe_key=dedupe_key, critical=critical)
        return deliver_alert(con, alert, oid)
    return True


def set_order(con, oid, **kw):
    kw["updated_at"] = int(time.time())
    con.execute(f"UPDATE orders SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), oid))


def set_trade_set_state(con, row, state, completed_at=None):
    """Advance the durable set in step with a terminal/known submission outcome."""
    if row["set_id"]:
        con.execute("UPDATE trade_sets SET state=?, completed_at=? WHERE id=?",
                    (state, completed_at, row["set_id"]))


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
        cycle = trade_policy.cycle_id(now)
        for i, o in enumerate(orders):
            cid = f"s1-{pid}-{i}-{o['coin']}-{o['action'][0]}"[:64]
            side, stop, target, lev, risk, set_id = "LONG", None, None, None, None, None
            if o["action"] == "OPEN":
                side = str(o.get("side") or "LONG").upper()
                entry, atr = float(o["planned_price"]), float(o["atr"])
                stop = float(o.get("stop_loss") or (entry - s1.SL_ATR * atr if side == "LONG"
                                                     else entry + s1.SL_ATR * atr))
                distance = abs(entry - stop)
                target = float(o.get("take_profit") or (entry + 1.5 * distance if side == "LONG"
                                                         else entry - 1.5 * distance))
                trade_policy.validate_bracket(side, entry, stop, target)
                lev = float(o.get("leverage") or s1.LEV)
                risk = float(o.get("planned_risk_inr") or
                             (float(o["notional_inr"]) * distance / entry))
                if not all(math.isfinite(x) and x > 0 for x in (lev, risk)):
                    raise ValueError("planned leverage and risk must be finite and positive")
                proposal = dict(cycle=cycle, plan_id=pid, seq=i, coin=o["coin"], side=side,
                                entry=entry, stop=stop, target=target, leverage=lev, risk=round(risk, 8))
                proposal_id = hashlib.sha256(json.dumps(proposal, sort_keys=True,
                                                        separators=(",", ":")).encode()).hexdigest()
                cur_set = con.execute("""INSERT INTO trade_sets(plan_id, cycle, proposal_id, coin, side, state,
                                      planned_risk_inr) VALUES(?,?,?,?,?,'PLANNED',?)""",
                                      (pid, cycle, proposal_id, o["coin"], side, risk))
                set_id = cur_set.lastrowid
            cur_order = con.execute("""INSERT INTO orders(plan_id, seq, coin, action, client_order_id, state,
                           position_id, planned_price, planned_notional_inr, atr, updated_at, side, planned_stop,
                           planned_target, planned_leverage, planned_risk_inr, planned_qty, set_id, strategy)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (pid, i, o["coin"], o["action"], cid, "PLANNED", o.get("position_id"),
                            o.get("planned_price"), o.get("notional_inr"), o.get("atr"), now, side, stop,
                            target, lev, risk, o.get("qty"), set_id, o.get("strategy")))
            if set_id:
                con.execute("UPDATE trade_sets SET order_id=? WHERE id=?", (cur_order.lastrowid, set_id))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return pid


def journal_set_approval(con, plan_id, approver, ttl=15 * 60):
    """Bind one Telegram approval to one immutable planned set. It is consumed at first submit."""
    row = con.execute("""SELECT s.proposal_id, s.cycle FROM trade_sets s JOIN plans p ON p.id=s.plan_id
                         WHERE s.plan_id=? AND s.state='PLANNED' AND p.state='PLANNED'""", (plan_id,)).fetchall()
    if len(row) != 1:
        return "approval must name a plan containing exactly one unattempted set"
    now = int(time.time())
    con.execute("""INSERT INTO set_approvals(proposal_id, cycle, expires_at, channel, approved_by, approved_at)
                   VALUES(?,?,?,'telegram',?,?) ON CONFLICT(proposal_id) DO UPDATE SET expires_at=excluded.expires_at,
                   approved_by=excluded.approved_by, approved_at=excluded.approved_at, consumed_at=NULL""",
                (row[0]["proposal_id"], row[0]["cycle"], now + ttl, approver, now))
    return None


def cycle_set_counts(con, cycle, exclude_set_id=None):
    """Return (sets used, unresolved writes) for one durable cycle.

    Used = verified COMPLETE sets plus FAILED sets whose entry FILLED (money moved: a fill followed by a fail-safe
    exit still counts, so a coin whose bracket keeps failing cannot be re-entered every 15 minutes, paying fees
    each round). A FAILED set that never filled (definite rejection) does not count. An attempted row in any
    other nonterminal state is ambiguous/in-flight and blocks a later set until reconciliation.
    """
    completed = con.execute("SELECT COUNT(*) FROM trade_sets s LEFT JOIN orders o ON o.id=s.order_id "
                            "WHERE s.cycle=? AND (s.state='COMPLETE' OR (s.state='FAILED' AND "
                            "COALESCE(o.filled_qty, 0) > 0))", (cycle,)).fetchone()[0]
    params = [cycle]
    exclusion = ""
    if exclude_set_id is not None:
        exclusion = " AND id<>?"
        params.append(exclude_set_id)
    unresolved = con.execute("SELECT COUNT(*) FROM trade_sets WHERE cycle=? AND attempted_at IS NOT NULL "
                              "AND state NOT IN ('COMPLETE','FAILED','RETRYABLE')" + exclusion,
                              params).fetchone()[0]
    return completed, unresolved


def plan_requires_set_approval(con, plan_id):
    """True only when this exact single-set plan would become set 4+ in its current cycle."""
    rows = con.execute("SELECT cycle FROM trade_sets WHERE plan_id=? AND state='PLANNED'", (plan_id,)).fetchall()
    if len(rows) != 1:
        return False
    completed, _unresolved = cycle_set_counts(con, rows[0]["cycle"])
    return completed >= trade_policy.AUTONOMOUS_SETS_PER_CYCLE


def authorize_set_submission(con, row):
    """Atomically assign/consume the next cycle set immediately before the first exchange write."""
    if not row["set_id"]:
        return None
    con.execute("BEGIN IMMEDIATE")
    try:
        item = con.execute("SELECT * FROM trade_sets WHERE id=?", (row["set_id"],)).fetchone()
        if item is None:
            why = "set journal missing"
        elif item["state"] == "COMPLETE":
            why = "set is already complete; refusing duplicate submission"
        elif item["state"] == "ATTEMPTED":
            why = "prior submission is unresolved; reconcile before any new set or retry"
        elif item["state"] == "RETRYABLE":
            # A definite exchange-busy response proves no order was accepted. Permit
            # only this same in-progress plan to retry; its per-set approval was bound
            # and consumed at the first write attempt.
            plan_state = con.execute("SELECT state FROM plans WHERE id=?", (item["plan_id"],)).fetchone()
            if item["cycle"] != trade_policy.cycle_id():
                why = "planned set belongs to an expired 24-hour IST cycle"
            else:
                why = None if plan_state and plan_state["state"] == "EXECUTING" else "known-not-submitted attempt is no longer retryable in this plan"
            if why is None:
                con.execute("UPDATE trade_sets SET state='ATTEMPTED', attempted_at=? WHERE id=?",
                            (int(time.time()), item["id"]))
        elif item["cycle"] != trade_policy.cycle_id():
            why = "planned set belongs to an expired 24-hour IST cycle"
        else:
            completed, unresolved = cycle_set_counts(con, item["cycle"], exclude_set_id=item["id"])
            why = None
            if unresolved:
                why = "another set submission is unresolved; reconcile it before opening a new set"
            if why is None and completed >= trade_policy.AUTONOMOUS_SETS_PER_CYCLE:
                approval = con.execute("SELECT * FROM set_approvals WHERE proposal_id=?",
                                       (item["proposal_id"],)).fetchone()
                now = int(time.time())
                if approval is None or approval["channel"] != "telegram" or approval["cycle"] != item["cycle"] \
                        or approval["expires_at"] <= now or approval["consumed_at"] is not None:
                    why = (f"owner Telegram approval required for set {completed + 1}; approval must be current "
                           "and bound to this exact proposal")
                else:
                    con.execute("UPDATE set_approvals SET consumed_at=? WHERE proposal_id=?",
                                (now, item["proposal_id"]))
            if why is None:
                now = int(time.time())
                con.execute("UPDATE trade_sets SET set_number=?, state='ATTEMPTED', attempted_at=? WHERE id=?",
                            (completed + 1, now, item["id"]))
        if why:
            con.execute("ROLLBACK")
            return why
        con.execute("COMMIT")
        return None
    except Exception:
        con.execute("ROLLBACK")
        raise


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
        token = uuid.uuid4().hex                  # the lease is taken in the SAME transaction as the approval
        con.execute("UPDATE plans SET state='APPROVED', approved_by=?, approved_at=?, lease_until=?, lease_token=? "
                    "WHERE id=?", (approver, int(time.time()), int(time.time()) + LEASE, token, plan_id))
        con.execute("COMMIT")
        LEASES[plan_id] = token
        return None
    except Exception:
        con.execute("ROLLBACK")
        raise


# ---------- execution lease (fencing): only the holder of a plan's token may change that plan's orders

LEASES = {}                  # plan_id -> token held by THIS process


class LeaseLost(Exception):
    """Another process took over this plan (our lease expired). Stop touching it immediately."""


def renew(con, plan_id):
    """Extend our lease; raises LeaseLost if the plan's token is no longer ours. Called before every order
    request and inside every wait loop, so a live run never looks dead to reconcile()."""
    token = LEASES.get(plan_id)
    if token is None:
        return
    cur = con.execute("UPDATE plans SET lease_until=? WHERE id=? AND lease_token=?",
                      (int(time.time()) + LEASE, plan_id, token))
    if cur.rowcount != 1:
        LEASES.pop(plan_id, None)
        raise LeaseLost(f"plan {plan_id}: execution lease lost")


def take_lease(con, plan_id):
    """reconcile(): take over a plan only if nobody holds a live lease on it (atomic compare-and-set)."""
    token = uuid.uuid4().hex
    now = int(time.time())
    cur = con.execute("UPDATE plans SET lease_until=?, lease_token=? WHERE id=? AND COALESCE(lease_until, 0) < ?",
                      (now + LEASE, token, plan_id, now))
    if cur.rowcount == 1:
        LEASES[plan_id] = token
        return True
    return False


def release(con, plan_id):
    token = LEASES.pop(plan_id, None)
    if token:
        con.execute("UPDATE plans SET lease_until=NULL WHERE id=? AND lease_token=?", (plan_id, token))


def latest_plan(con):
    return con.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()


# ---------- account state

def floor_to(x, step):
    return math.floor(x / step + 1e-9) * step


def ceil_to(x, step):
    return math.ceil(x / step - 1e-9) * step


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


def exchange_ts(value):
    """Mudrex ISO time ('2026-09-27T16:00:11Z') -> epoch seconds, or None if missing/unparseable."""
    try:
        return calendar.timegm(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def record_exchange_facts(con, p, closed=False, position_id=None):
    """Keep Mudrex's own open/close times and the position's side/qty/entry/rate on the owned row (never overwrite
    a known value). These, not local discovery times, decide what was open at the IST boundary."""
    side = str(p.get("order_type") or p.get("position_type") or "").upper() or None
    con.execute("""UPDATE owned SET ex_opened_at=COALESCE(ex_opened_at, ?), ex_side=COALESCE(ex_side, ?),
                   ex_qty=COALESCE(ex_qty, ?), ex_entry=COALESCE(ex_entry, ?), ex_rate=COALESCE(ex_rate, ?)
                   WHERE position_id=?""",
                (exchange_ts(p.get("created_at")), side, _num(p.get("quantity")), _num(p.get("entry_price")),
                 _num(p.get("entry_hedge_rate")), position_id or p["id"]))
    if closed:                          # a history row has its OWN id: always pass the owned position_id
        con.execute("UPDATE owned SET ex_closed_at=COALESCE(ex_closed_at, ?) WHERE position_id=?",
                    (exchange_ts(p.get("updated_at")), position_id or p["id"]))


def history_by_owned(con, rows):
    """{owned position_id: Mudrex closed-position history row}.

    Live Mudrex (2026-09-28) gives a CLOSED position a NEW id in its position history (open XRP position
    01a0e398-6712..., its history row 01a0e434-98b6...: same symbol, side, entry, quantity and created_at).
    So a row matches an owned position by id, or else by an exact fingerprint of the bot's own opening fill:
    symbol, side, entry price, quantity and open time. An ambiguous or missing match stays unmatched, so the
    caller keeps that position unconfirmed (fail closed)."""
    rows = [r for r in rows if str(r.get("status") or "CLOSED").upper() == "CLOSED"]
    by_id = {r.get("id"): r for r in rows}
    out, used = {}, set()
    owned = con.execute("SELECT w.position_id, w.coin, w.opened_at, w.ex_opened_at, o.side, o.fill_price, "
                        "o.filled_qty FROM owned w LEFT JOIN orders o ON o.client_order_id=w.client_order_id"
                        ).fetchall()
    for w in owned:
        if w["position_id"] in by_id:
            out[w["position_id"]] = by_id[w["position_id"]]
            used.add(w["position_id"])
    for w in owned:
        if w["position_id"] in out:
            continue
        cands = [r for r in rows if r.get("id") not in used and _same_position(w, r)]
        if len(cands) == 1:
            out[w["position_id"]] = cands[0]
            used.add(cands[0].get("id"))
    return out


def _same_position(w, r):
    fill, qty = w["fill_price"], w["filled_qty"]
    if not fill or not qty or r.get("symbol") != f"{w['coin']}USDT":
        return False
    if str(r.get("position_type") or r.get("order_type") or "").upper() != str(w["side"] or "LONG").upper():
        return False
    entry, q, t = _num(r.get("entry_price")), _num(r.get("quantity")), exchange_ts(r.get("created_at"))
    if entry is None or q is None or t is None:
        return False
    if abs(entry - fill) > max(1e-9, 1e-6 * fill) or abs(q - qty) > 1e-6 * qty + 1e-12:
        return False
    if w["ex_opened_at"]:
        return abs(t - w["ex_opened_at"]) <= 5
    # the bot notices a fill after it happens: the exchange open time is at or before our discovery time
    return w["opened_at"] is not None and w["opened_at"] - 2 * 86400 <= t <= w["opened_at"] + 60


def sync_owned(con, client, positions):
    """Mark bot positions closed ONLY when Mudrex closed-position history lists them, and fill in realized P&L.
    Returns how many bot positions have unknown P&L: closed without P&L yet, or vanished but not confirmed
    closed (either way bot equity is unknown and entries are blocked)."""
    open_ids = {p["id"] for p in positions}
    now = int(time.time())
    ours = owned_ids(con)
    for p in positions:
        if p["id"] in ours:
            record_exchange_facts(con, p)
    # a position marked closed from one incomplete snapshot that shows up again (P&L never confirmed) is reopened,
    # so it stays bot-owned and monitored instead of silently turning "manual"
    for r in con.execute("SELECT position_id FROM owned WHERE closed_at IS NOT NULL AND realized_pnl IS NULL"
                         ).fetchall():
        if r["position_id"] in open_ids:
            con.execute("UPDATE owned SET closed_at=NULL WHERE position_id=?", (r["position_id"],))
    absent = [r["position_id"] for r in con.execute("SELECT position_id FROM owned WHERE closed_at IS NULL")
              if r["position_id"] not in open_ids]
    missing = con.execute("SELECT position_id FROM owned WHERE closed_at IS NOT NULL AND realized_pnl IS NULL"
                          ).fetchall()
    no_close_time = con.execute("SELECT position_id FROM owned WHERE closed_at IS NOT NULL AND ex_closed_at IS NULL"
                                ).fetchall()
    unconfirmed = 0
    if absent or missing or no_close_time:
        hist = history_by_owned(con, client.history("positions")[0])
        for pid in absent + [r["position_id"] for r in missing + no_close_time]:
            if pid in hist:
                record_exchange_facts(con, hist[pid], closed=True, position_id=pid)
        for pid in absent:                     # closed only when Mudrex history lists it; else still unknown
            if pid in hist:
                con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (now, pid))
            else:
                unconfirmed += 1
        missing = con.execute("SELECT position_id FROM owned WHERE closed_at IS NOT NULL AND realized_pnl IS NULL"
                              ).fetchall()
        for r in missing:
            p = hist.get(r["position_id"])
            if p is not None and p.get("pnl") is not None:
                value = float(p["pnl"])
                if math.isfinite(value):
                    con.execute("UPDATE owned SET realized_pnl=? WHERE position_id=?", (value, r["position_id"]))
    return unconfirmed + con.execute("SELECT COUNT(*) FROM owned WHERE closed_at IS NOT NULL AND realized_pnl IS NULL"
                                     ).fetchone()[0]


def unrealized_inr(con, positions, rate):
    ours = owned_ids(con)
    u = 0.0
    for p in positions:
        if p["id"] in ours:
            px = float(p.get("mark_price") or p.get("last_price") or p["entry_price"])
            direction = 1 if str(p.get("order_type")).upper() == "LONG" else -1
            value = (float(p["quantity"]) * (px - float(p["entry_price"])) * direction
                     * float(p.get("entry_hedge_rate") or rate))
            if not math.isfinite(value):
                raise PnlUnknown(f"non-finite open P&L for {p.get('symbol')}")
            u += value
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
    equity = s1.CAPITAL_CAP_INR + float(realized) + unrealized_inr(con, positions, rate)
    if not math.isfinite(equity):
        raise PnlUnknown("bot equity is non-finite")
    return equity


def ist_day(t=None):
    return time.strftime("%Y-%m-%d", time.gmtime((t or time.time()) + config.IST_OFFSET))


def boundary_equity(con, midnight, open_price_at=None):
    """Exact bot equity at the IST boundary from exchange evidence, or None (unknown: the caller fails closed).

    Rs allocation + realized P&L of bot positions closed before `midnight` + every position OPEN at `midnight`
    valued at Mudrex's candle open at exactly `midnight` (open_price_at(coin, midnight)).
    Before/after midnight is decided by Mudrex's own times (ex_opened_at / ex_closed_at). A local discovery time
    is used only where it is PROOF: the bot always notices a fill or a close after it happens, so a local time
    earlier than midnight proves the event was before midnight; a later local time proves nothing."""
    total = float(s1.CAPITAL_CAP_INR)
    for r in con.execute("SELECT * FROM owned").fetchall():
        closed_before = ((r["ex_closed_at"] is not None and r["ex_closed_at"] < midnight) or
                         (r["closed_at"] is not None and r["closed_at"] < midnight))
        if closed_before:
            if r["realized_pnl"] is None or not math.isfinite(r["realized_pnl"]):
                return None
            total += r["realized_pnl"]
            continue
        if r["closed_at"] is not None and r["ex_closed_at"] is None:
            return None                                             # closed, but when? unknown
        if r["ex_opened_at"] is not None:
            opened_before = r["ex_opened_at"] < midnight
        elif r["opened_at"] is not None and r["opened_at"] < midnight:
            opened_before = True
        else:
            return None                                             # discovered after midnight, open time unknown
        if not opened_before:
            continue                                                # opened in this cycle: not in the baseline
        if open_price_at is None or None in (r["ex_qty"], r["ex_entry"], r["ex_side"], r["ex_rate"]):
            return None                                             # includes an unknown INR rate: not exact
        try:
            px = open_price_at(r["coin"], midnight)
        except Exception:                                           # noqa: BLE001 - any failure means unknown
            return None
        if px is None or not math.isfinite(px) or px <= 0:
            return None
        d = 1 if r["ex_side"] == "LONG" else -1 if r["ex_side"] == "SHORT" else 0
        if not d:
            return None
        total += r["ex_qty"] * (px - r["ex_entry"]) * d * r["ex_rate"]
        con.execute("UPDATE owned SET ex_boundary_at=?, ex_boundary_px=? WHERE position_id=?",
                    (midnight, px, r["position_id"]))
    return total if math.isfinite(total) else None


def caps_state(con, equity, unreal=0.0, now=None, trusted=True, open_price_at=None):
    """Bot cycle P&L vs fixed rupee caps. The day-start baseline is exact or UNTRUSTED (entries fail closed):
      - boundary_equity(): exchange open/close times, positions carried across midnight valued at Mudrex's candle
        open at exactly midnight (open_price_at). Local equity marks are never used as a baseline.
    An untrusted baseline is re-evaluated on later calls and upgraded once exact evidence is available."""
    now = now or time.time()
    day = ist_day(now)
    # trusted=False: equity was an estimate (bot P&L unconfirmed); such a mark can never become a baseline
    con.execute("INSERT OR REPLACE INTO marks(at, equity, trusted) VALUES(?,?,?)", (int(now), equity, int(trusted)))
    con.execute("DELETE FROM marks WHERE at < ?", (int(now) - 3 * 86400,))
    row = con.execute("SELECT start_equity, trusted FROM ledger WHERE day=?", (day,)).fetchone()
    if row is None or not row["trusted"]:
        midnight = int((now + config.IST_OFFSET) // 86400 * 86400 - config.IST_OFFSET)
        # exchange evidence only: a local equity mark (even one stamped 00:00:00) is never the baseline, and an
        # unconfirmed bot P&L (trusted=False) can never produce a trusted one
        exact = boundary_equity(con, midnight, open_price_at) if trusted else None
        if exact is not None:
            start, trusted = exact, 1
        else:
            before = con.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM owned WHERE closed_at IS NOT NULL "
                                 "AND closed_at < ?", (midnight,)).fetchone()[0]
            start, trusted = s1.CAPITAL_CAP_INR + before + unreal, 0     # diagnostic estimate only: fails closed
        con.execute("INSERT OR REPLACE INTO ledger(day, start_equity, trusted) VALUES(?,?,?)", (day, start, trusted))
    else:
        start, trusted = row["start_equity"], row["trusted"]
    cap = trade_policy.DAILY_LOSS_LIMIT_INR
    pnl = equity - start
    return dict(day=day, start=start, pnl=pnl, cap=cap, baseline_ok=bool(trusted),
                hit="loss" if pnl <= -cap else "profit" if pnl >= cap else None)


def update_peak(con, equity):
    """Highest bot equity ever seen, kept in the database (survives lost watcher state)."""
    con.execute("INSERT INTO kv(key, value) VALUES('bot_peak', ?) ON CONFLICT(key) DO UPDATE SET "
                "value=max(value, excluded.value)", (max(equity, s1.CAPITAL_CAP_INR),))
    return con.execute("SELECT value FROM kv WHERE key='bot_peak'").fetchone()[0]


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

def lookup_until_known(client, cid, sleep, tries=LOOKUP_TRIES, con=None, plan_id=None):
    """The order, or None only if EVERY lookup got a definitive 'not found' (404).
    Raises Ambiguous if any lookup failed (timeout/5xx/423): a partly unanswered lookup must never be read as
    'not placed' (the exchange may be slow to show it, or the failing call may have been the one that knew)."""
    failed = 0
    for i in range(tries):
        if con is not None and plan_id is not None:
            renew(con, plan_id)                                        # long lookups never outlive our fence
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


def submit_with_reconcile(con, client, row, send, sleep, alert, fresh=None):
    """Send a create-order request with a pre-journaled client_order_id and classify the outcome.
    Returns the exchange order dict (ACCEPTED) or None; sets FAILED / RECONCILE_REQUIRED itself.
    Before ANY resubmission the order is looked up by client_order_id."""
    cid = row["client_order_id"]
    for attempt in range(3):
        if attempt:
            existing = lookup_until_known(client, cid, sleep, tries=2, con=con, plan_id=row["plan_id"])
            if existing:
                set_order(con, row["id"], state="ACCEPTED",
                          exchange_order_id=existing.get("id") or existing.get("order_id"))
                return existing
        renew(con, row["plan_id"])
        why = fresh() if fresh else None                               # plan age + live price, before EVERY attempt
        if why:
            set_order(con, row["id"], state="FAILED", error=why)
            return None
        gate()                                                         # STOP / live flag, right before sending
        why = authorize_set_submission(con, row)
        if why:
            set_order(con, row["id"], state="FAILED", error=why)
            return None
        set_order(con, row["id"], state="SUBMITTED")
        try:
            resp = send()
            set_order(con, row["id"], state="ACCEPTED", exchange_order_id=(resp or {}).get("order_id"))
            return resp or {}
        except Rejected as e:
            if e.status == 409:                                        # cid already exists: it was accepted
                o = lookup_until_known(client, cid, sleep, con=con, plan_id=row["plan_id"])
                set_order(con, row["id"], state="ACCEPTED", exchange_order_id=(o or {}).get("id"))
                return o or {}
            set_order(con, row["id"], state="FAILED", error=f"rejected {e.status}: {e.errors}")
            set_trade_set_state(con, row, "FAILED")
            return None
        except Locked:
            set_order(con, row["id"], state="PLANNED")                 # 423/429: exchange did not take it
            set_trade_set_state(con, row, "RETRYABLE")
            renew(con, row["plan_id"])
            sleep(min(2 ** attempt, 8))
            continue
        except Ambiguous as e:                                         # UNKNOWN: never resubmit blindly
            o = lookup_until_known(client, cid, sleep, con=con, plan_id=row["plan_id"])
            if o:
                set_order(con, row["id"], state="ACCEPTED", exchange_order_id=o.get("id") or o.get("order_id"))
                return o
            set_order(con, row["id"], state="RECONCILE_REQUIRED", error=f"ambiguous ({e.status}), not found yet")
            event(con, "reconcile", f"{row['coin']}: order {cid} outcome unknown; not resubmitted", alert)
            return None
    set_order(con, row["id"], state="FAILED", error="exchange busy (423/429) after retries")
    set_trade_set_state(con, row, "FAILED")
    return None


def poll_fill(con, client, row, sleep):
    for i in range(POLL_TRIES):
        renew(con, row["plan_id"])                                   # a live wait keeps the lease
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
            if moved_money(o):
                return partial_fill(con, row, o)
            set_order(con, row["id"], state="FAILED", error=f"order {status}")
            set_trade_set_state(con, row, "FAILED")
            return None
        sleep(min(1 + i, 5))
    set_order(con, row["id"], state="RECONCILE_REQUIRED", error="no terminal status yet")
    return None


def moved_money(o):
    """A CANCELLED/EXPIRED/... order that still filled something (or opened a position) moved money."""
    try:
        q = float(o.get("filled_quantity") or 0)
    except (TypeError, ValueError):
        q = float("nan")
    return not (math.isfinite(q) and q == 0) or bool(o.get("future_position_uuid"))


def partial_fill(con, row, o):
    """Treat a terminal order with a fill as FILLED, so the fill is verified (SL+TP) or exited, and the set counts.
    Unreadable fill details: RECONCILE_REQUIRED (entries stay blocked). Returns the order or None."""
    fp, fq = _num(o.get("filled_price")), _num(o.get("filled_quantity"))
    if fp is None or fq is None:
        set_order(con, row["id"], state="RECONCILE_REQUIRED",
                  error=f"order {o.get('status')} with an unreadable partial fill")
        return None
    set_order(con, row["id"], state="FILLED", fill_price=fp, filled_qty=fq, position_id=o.get("future_position_uuid"),
              error=f"order {o.get('status')} after a partial fill of {fq}")
    return o


def stop_tolerance(fill, step):
    return max(2 * step, 0.002 * fill)


def unprotected(con, oid, row, why, alert):
    """A filled position whose bracket is not confirmed: never FAILED (that would release the entry block)."""
    set_order(con, oid, state="RECONCILE_REQUIRED", error=why)
    event(con, "bracket", f"{row['coin']}: {why}. POSITION MAY BE UNPROTECTED - check Mudrex now. "
                       f"New entries are blocked until this is resolved.", alert)
    return False


def identity_problems(pos, row):
    """Differences between an exchange position and the order that opened it (empty list = same position)."""
    problems = []
    if pos.get("symbol") != row["coin"] + "USDT":
        problems.append(f"symbol {pos.get('symbol')}")
    expected_side = str(row["side"] or "LONG").upper()
    if str(pos.get("order_type") or "").upper() != expected_side:
        problems.append(f"side {pos.get('order_type')}")
    if pos.get("trade_currency") not in (None, "INR"):
        problems.append(f"currency {pos.get('trade_currency')}")
    try:
        if float(pos.get("leverage")) != float(row["planned_leverage"] or s1.LEV):
            problems.append(f"leverage {pos.get('leverage')}")
    except (TypeError, ValueError):
        problems.append(f"leverage {pos.get('leverage')}")
    try:
        qty_ok = row["filled_qty"] and abs(float(pos["quantity"]) - row["filled_qty"]) <= 1e-6 * abs(row["filled_qty"]) + 1e-12
    except (KeyError, TypeError, ValueError):
        qty_ok = False
    if not qty_ok:
        problems.append(f"qty {pos.get('quantity')} vs filled {row['filled_qty']}")
    return problems

def verify_entry(con, client, oid, sleep, alert):
    """Confirm position identity, quantity, and both exchange bracket legs at fill-derived prices.
    Returns True when protected; the caller sets VERIFIED only after the approved-notional check also passes,
    so a crash in between leaves the order FILLED (re-checked by reconcile), never VERIFIED unchecked."""
    row = order_row(con, oid)
    pos = next((p for p in client.positions() if p["id"] == row["position_id"]), None)
    if pos is None:
        set_order(con, oid, state="RECONCILE_REQUIRED", error="filled but position not visible")
        event(con, "reconcile", f"{row['coin']}: filled but position not visible yet", alert)
        return False
    fill = row["fill_price"]
    problems = identity_problems(pos, row)
    if problems:                                     # not the position we approved: no ownership, no writes to it
        set_order(con, oid, state="RECONCILE_REQUIRED", error="position mismatch: " + "; ".join(problems))
        con.execute("DELETE FROM owned WHERE position_id=? AND closed_at IS NULL", (pos["id"],))
        event(con, "reconcile", f"{row['coin']}: position does not match the order ({'; '.join(problems)}). "
                                f"The bot will not touch it. Check its stop-loss in Mudrex now; entries halted.", alert)
        return False
    con.execute("INSERT OR IGNORE INTO owned(position_id, coin, client_order_id, opened_at) VALUES(?,?,?,?)",
                (pos["id"], row["coin"], row["client_order_id"], int(time.time())))
    side = str(row["side"] or "LONG").upper()
    try:
        liq = float(pos.get("liquidation_price"))
    except (TypeError, ValueError):
        liq = float("nan")
    valid_liq = math.isfinite(liq) and ((side == "LONG" and 0 < liq < fill) or
                                        (side == "SHORT" and liq > fill))
    if not valid_liq:
        return unprotected(con, oid, row, f"liquidation price unknown/invalid ({liq}); bracket NOT verified", alert)
    step = float(client.asset(row["coin"] + "USDT")["price_step"])
    stop_distance = abs(float(row["planned_price"]) - float(row["planned_stop"]))
    target_distance = abs(float(row["planned_target"]) - float(row["planned_price"]))
    wanted_stop = (floor_to(fill - stop_distance, step) if side == "LONG" else
                   ceil_to(fill + stop_distance, step))
    wanted_target = (ceil_to(fill + target_distance, step) if side == "LONG" else
                     floor_to(fill - target_distance, step))
    try:
        trade_policy.validate_bracket(side, fill, wanted_stop, wanted_target)
    except ValueError as e:
        return unprotected(con, oid, row, f"no valid fill-derived bracket ({e})", alert)
    if (side == "LONG" and wanted_stop <= liq) or (side == "SHORT" and wanted_stop >= liq):
        return unprotected(con, oid, row,
                           f"stop {wanted_stop} is beyond liquidation {liq} at fill {fill}", alert)
    tol = stop_tolerance(fill, step)

    def values(position):
        sl = position.get("stoploss") or {}
        tp = position.get("takeprofit") or {}
        try:
            return sl, tp, float(sl.get("price") or 0), float(tp.get("price") or 0)
        except (TypeError, ValueError):
            return sl, tp, float("nan"), float("nan")

    def stop_ok(value):
        geometry = liq < value < fill if side == "LONG" else fill < value < liq
        return math.isfinite(value) and geometry and abs(value - wanted_stop) <= tol

    def target_ok(value):
        geometry = value > fill if side == "LONG" else 0 < value < fill
        return math.isfinite(value) and geometry and abs(value - wanted_target) <= tol

    sl_info, tp_info, current_stop, current_target = values(pos)
    if not (stop_ok(current_stop) and target_ok(current_target)):
        renew(con, row["plan_id"])
        try:
            edit_stop = current_stop > 0 and not stop_ok(current_stop)
            edit_target = current_target > 0 and not target_ok(current_target)
            if edit_stop or edit_target:
                client.edit_bracket(
                    pos["id"],
                    stop_order_id=sl_info.get("order_id") if edit_stop else None,
                    stop=fmt_step(wanted_stop, step) if edit_stop else None,
                    target_order_id=tp_info.get("order_id") if edit_target else None,
                    target=fmt_step(wanted_target, step) if edit_target else None,
                )
            missing_stop = not math.isfinite(current_stop) or current_stop <= 0
            missing_target = not math.isfinite(current_target) or current_target <= 0
            if missing_stop or missing_target:
                client.set_bracket(
                    pos["id"],
                    stop=fmt_step(wanted_stop, step) if missing_stop else None,
                    target=fmt_step(wanted_target, step) if missing_target else None,
                    sl_cid=f"{row['client_order_id']}-SL" if missing_stop else None,
                    tp_cid=f"{row['client_order_id']}-TP" if missing_target else None,
                )
        except (ApiError, ValueError) as e:
            event(con, "bracket", f"{row['coin']}: bracket attach/edit error {e}", None)
        for _ in range(3):
            renew(con, row["plan_id"])
            sleep(1)
            pos = next((p for p in client.positions() if p["id"] == row["position_id"]), None)
            if pos is None:
                break
            sl_info, tp_info, current_stop, current_target = values(pos)
            if stop_ok(current_stop) and target_ok(current_target):
                break
    if stop_ok(current_stop) and target_ok(current_target):
        set_order(con, oid, stop_price=fmt_step(current_stop, step),
                  target_price=fmt_step(current_target, step))
        return True
    return unprotected(con, oid, row,
                       f"bracket not verified (stop {current_stop}/{wanted_stop}, "
                       f"target {current_target}/{wanted_target})", alert)


def protect_and_check(con, client, row, o, sleep, alert):
    """Stop first (protection before accounting), then the approved-notional check and the post-fill loss budget
    / exposure check; VERIFIED only if all pass. A bot-owned position (identity validated) that cannot be
    protected or breaks a limit is EXITED at once: owner's standing rule (2026-09-27), protective exits need no
    extra approval. A position that did not match its order is never touched (alert only)."""
    protected = verify_entry(con, client, row["id"], sleep, alert)
    within = fill_within_approval(con, order_row(con, row["id"]), o, alert)
    budget = protected and within and after_fill_budget(con, client, order_row(con, row["id"]), o, alert)
    if protected and within and budget:
        set_order(con, row["id"], state="VERIFIED")
        if row["set_id"]:
            con.execute("UPDATE trade_sets SET state='COMPLETE', completed_at=? WHERE id=?",
                        (int(time.time()), row["set_id"]))
        return True
    cur = order_row(con, row["id"])
    if cur["position_id"] and not quarantined(con, cur["position_id"]) and cur["position_id"] in owned_ids(con):
        # protective_close re-checks that the position is bound to THIS order and refuses (with an alert) if not
        protective_close(con, client, cur, cur["error"] or "limits not confirmed", sleep, alert)
    return False


def pos_rate(p, rate=None):
    """INR rate Mudrex applied to an existing position, or None if missing/invalid (callers fail closed)."""
    try:
        r = float(p.get("entry_hedge_rate") or 0)
    except (TypeError, ValueError):
        r = 0.0
    return r if r > 0 and math.isfinite(r) else None


def cycle_realized_pnls(con, now=None):
    """Confirmed outcomes observed in the current IST cycle; unknown/non-finite fails closed."""
    now = now or time.time()
    midnight = int((now + config.IST_OFFSET) // 86400 * 86400 - config.IST_OFFSET)
    rows = con.execute("SELECT realized_pnl FROM owned WHERE closed_at>=?", (midnight,)).fetchall()
    values = []
    for row in rows:
        if row["realized_pnl"] is None:
            raise PnlUnknown("current-cycle realized P&L is not confirmed")
        value = float(row["realized_pnl"])
        if not math.isfinite(value):
            raise PnlUnknown("current-cycle realized P&L is non-finite")
        values.append(value)
    return values


def active_stop_risks(con, positions, owned=None):
    """Conservative verified rupee stop risks for bot-owned open positions."""
    owned = owned if owned is not None else owned_ids(con)
    risks = []
    for p in positions:
        if p["id"] not in owned:
            continue
        try:
            side = str(p["order_type"]).upper()
            entry, qty = float(p["entry_price"]), float(p["quantity"])
            stop = float((p.get("stoploss") or {}).get("price") or 0)
            rate = pos_rate(p)
        except (KeyError, TypeError, ValueError):
            raise PnlUnknown(f"stop risk unknown for held {p.get('symbol')}")
        valid = (side == "LONG" and 0 < stop < entry) or (side == "SHORT" and stop > entry)
        if not valid or rate is None or not all(math.isfinite(x) and x > 0 for x in (entry, qty, stop)):
            raise PnlUnknown(f"verified stop/rate missing for held {p.get('symbol')}")
        actual = qty * abs(entry - stop) * rate
        # A position carried across the IST boundary can lose, inside THIS cycle, everything from Mudrex's price
        # at the boundary down to its stop (a profit at midnight given back counts against today's Rs500).
        r = con.execute("SELECT opened_at, ex_opened_at, ex_boundary_at, ex_boundary_px FROM owned "
                        "WHERE position_id=?", (p["id"],)).fetchone()
        midnight = int((time.time() + config.IST_OFFSET) // 86400 * 86400 - config.IST_OFFSET)
        opened = (r["ex_opened_at"] or r["opened_at"]) if r else None
        if opened is None or opened < midnight:
            if r is None or r["ex_boundary_at"] != midnight or not r["ex_boundary_px"]:
                raise PnlUnknown(f"boundary price unknown for carried {p.get('symbol')}")
            bpx = float(r["ex_boundary_px"])
            actual = max(actual, qty * max(0.0, (bpx - stop) if side == "LONG" else (stop - bpx)) * rate)
        planned = con.execute("""SELECT planned_risk_inr FROM orders WHERE client_order_id=(
                              SELECT client_order_id FROM owned WHERE position_id=?) ORDER BY id DESC LIMIT 1""",
                              (p["id"],)).fetchone()
        reserve = float(planned[0]) if planned and planned[0] is not None else actual
        if not math.isfinite(reserve) or reserve < 0:
            raise PnlUnknown(f"risk reserve invalid for held {p.get('symbol')}")
        risks.append(max(actual, reserve))
    return risks


def unknown_rate(positions, owned):
    """Name of a held bot position whose applied INR rate is unknown (its size in rupees cannot be trusted)."""
    return next((p["symbol"] for p in positions if p["id"] in owned and pos_rate(p) is None), None)


def position_margin_inr(position, fallback_rate=None):
    """Conservative isolated margin; unknown or invalid leverage/rate fails closed."""
    rate = pos_rate(position) or fallback_rate
    try:
        qty = float(position["quantity"])
        entry = float(position["entry_price"])
        leverage = float(position.get("leverage"))
        rate = float(rate)
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        raise PnlUnknown(f"margin inputs unknown for held {position.get('symbol')}") from e
    if not all(math.isfinite(x) and x > 0 for x in (qty, entry, leverage, rate)):
        raise PnlUnknown(f"margin inputs invalid for held {position.get('symbol')}")
    return qty * entry * rate / leverage


def after_fill_budget(con, client, row, o, alert):
    """Loss budgets and total exposure recomputed from the ACTUAL fill, applied INR rate and verified stop.
    Unknown bot equity fails the check (fail closed)."""
    positions = client.positions()
    rate = float(o.get("hedge_rate") or 0) or config.INR_PER_USDT
    owned = owned_ids(con)
    try:
        eq = bot_equity(con, client, positions, rate)
    except PnlUnknown as e:
        why = f"bot balance unknown after the fill ({e})"
    else:
        fill, qty = row["fill_price"], float(row["filled_qty"])
        try:
            realized = cycle_realized_pnls(con)
        except PnlUnknown as e:
            why = f"current-cycle P&L unknown ({e})"
        else:
            planned_pure = (float(row["planned_notional_inr"]) *
                            abs(float(row["planned_price"]) - float(row["planned_stop"])) /
                            float(row["planned_price"]))
            buffer = max(0.0, float(row["planned_risk_inr"] or planned_pure) - planned_pure)
            why = over_loss_budget([p for p in positions if p["id"] != row["position_id"]], owned, rate, eq,
                                   qty * fill * rate, fill, float(row["stop_price"]), row["side"], realized,
                                   buffer, con=con, candidate_risk_limit=row["planned_risk_inr"])
        others = [p for p in positions if p["id"] != row["position_id"]]
        bad = unknown_rate(others, owned)
        why = why or (f"INR rate unknown for held {bad}" if bad else None)
        try:
            margin = sum(position_margin_inr(p, rate if p["id"] == row["position_id"] else None)
                         for p in positions if p["id"] in owned)
        except PnlUnknown as e:
            margin, why = 0.0, why or str(e)
        if not why and margin > s1.CAPITAL_CAP_INR:
            why = f"allocation cap: bot margin Rs {margin:,.0f} after this fill"
    if why:
        set_order(con, row["id"], state="RECONCILE_REQUIRED", error=f"after fill: {why}")
        return False
    return True


RECONCILE_GRACE = 15 * 60     # an order younger than this is never called 'not placed' from history absence
EXIT_RETRY_AFTER = 600       # an unconfirmed close is re-sent only after 10 min with the position still open
EXIT_MAX_ATTEMPTS = 3


def exit_authorized(con, row):
    """Protective-exit authority is bound to THIS order: its position must be recorded as opened by this
    order's client_order_id, still open, and not quarantined as a mismatch."""
    return bool(row["position_id"]) and not quarantined(con, row["position_id"]) and con.execute(
        "SELECT 1 FROM owned WHERE position_id=? AND client_order_id=? AND closed_at IS NULL",
        (row["position_id"], row["client_order_id"])).fetchone() is not None


def position_state(client, pid, con=None):
    """('open', position) if Mudrex shows it; ('closed', None) only if it is absent AND listed in Mudrex's closed
    position history (authoritative); ('unknown', None) otherwise - one incomplete snapshot never ends an exit."""
    pos = next((p for p in client.positions() if p["id"] == pid), None)
    if pos is not None:
        return "open", pos
    rows = client.history("positions")[0]
    closed = set(history_by_owned(con, rows)) if con is not None else {p.get("id") for p in rows}
    return ("closed" if pid in closed else "unknown"), None


def exit_in_flight(con, pid, exclude_id):
    """Another order (an automatic exit or an approved close) may still be closing this position: it sent a close
    less than EXIT_RETRY_AFTER ago and is not finished, or it is an approved close about to be sent."""
    # ANY close sent in the last EXIT_RETRY_AFTER counts, even if that order already finished (a finished exit
    # must still fence off a second close); plus an approved close that is about to be sent
    r = con.execute("SELECT o.plan_id, o.action FROM orders o JOIN plans p ON p.id=o.plan_id WHERE o.position_id=? "
                    "AND o.id<>? AND (o.exit_sent_at > ? OR (o.action='CLOSE' AND o.state='PLANNED' AND "
                    "p.state IN ('APPROVED','EXECUTING')))",
                    (pid, exclude_id, int(time.time()) - EXIT_RETRY_AFTER)).fetchone()
    return (f"another {'automatic exit' if r['action'] == 'OPEN' else 'close'} (plan {r['plan_id']}) is still "
            f"closing this position") if r else None


def exited(con, row, why, alert):
    con.execute("UPDATE owned SET closed_at=? WHERE position_id=? AND closed_at IS NULL",
                (int(time.time()), row["position_id"]))
    set_order(con, row["id"], state="FAILED", error=f"exited automatically: {why}"[:300])
    set_trade_set_state(con, row, "FAILED")
    event(con, "protect", f"{row['coin']}: protective exit done; position closed.", alert)
    return True

def claim_exit(con, row, state=None):
    """Atomically take the position-level exit fence and journal the close attempt BEFORE sending. Returns None
    if this order may send now, else why not. One transaction: two processes can never both pass."""
    con.execute("BEGIN IMMEDIATE")
    try:
        busy = exit_in_flight(con, row["position_id"], row["id"])
        if not busy and con.execute("SELECT 1 FROM owned WHERE position_id=? AND closed_at IS NOT NULL",
                                    (row["position_id"],)).fetchone():
            busy = "position already recorded as closed"
        if busy:
            con.execute("ROLLBACK")
            return busy
        cur = order_row(con, row["id"])
        extra = ", state=?" if state else ""
        con.execute(f"UPDATE orders SET exit_sent_at=?, exit_attempts=?, updated_at=?{extra} WHERE id=?",
                    (int(time.time()), (cur["exit_attempts"] or 0) + 1, int(time.time()),
                     *([state] if state else []), row["id"]))
        con.execute("COMMIT")
        return None
    except Exception:
        con.execute("ROLLBACK")
        raise


UNKNOWN_ALERT_AFTER = 1800   # a position neither visible nor in Mudrex history this long -> tell the human


def still_unknown(con, row, alert):
    """A close whose position is neither visible nor confirmed closed: keep waiting (never assume closed), but
    never silently: the human is alerted once after UNKNOWN_ALERT_AFTER."""
    err = row["error"] or ""
    if "not visible" not in err:                       # OPEN rows keep the marker that routes them back here
        set_order(con, row["id"], state="RECONCILE_REQUIRED", error=("protective exit: " if row["action"] == "OPEN"
                                                                     else "") + "position not visible; closure "
                                                                                "not confirmed")
    elif "[alerted]" not in err and time.time() - (row["updated_at"] or 0) > UNKNOWN_ALERT_AFTER:
        con.execute("UPDATE orders SET error=? WHERE id=?", (err + " [alerted]", row["id"]))
        event(con, "reconcile", f"{row['coin']}: position has not been visible or confirmed closed for 30 min. "
                                f"Check it in the Mudrex app; new buys stay blocked until this is resolved.", alert)

def protective_close(con, client, row, why, sleep, alert):
    """Exit a bot-owned position whose approved entry could not be protected or broke a limit. Not blocked by
    STOP (it only reduces risk). Order-bound (exit_authorized) and identity re-validated right before every
    close request. Idempotent: the attempt is journaled BEFORE sending; a close is re-sent only after
    EXIT_RETRY_AFTER with the position still open, at most EXIT_MAX_ATTEMPTS times. Gone = exited."""
    row, pid, now = order_row(con, row["id"]), row["position_id"], int(time.time())
    if not exit_authorized(con, row):
        set_order(con, row["id"], state="RECONCILE_REQUIRED", error="protective exit refused: position not bound "
                                                                     "to this order")
        event(con, "protect", f"{row['coin']}: cannot auto-exit (not this order's position). Check Mudrex now.", alert)
        return False
    state, pos = position_state(client, pid, con)
    if state == "closed":
        return exited(con, row, why, alert)
    busy = exit_in_flight(con, pid, row["id"])
    if pos is not None and not busy:
        problems = identity_problems(pos, row)
        if problems:
            set_order(con, row["id"], state="RECONCILE_REQUIRED",
                      error=f"protective exit refused: position changed ({'; '.join(problems)})"[:300])
            event(con, "protect", f"{row['coin']}: position changed ({'; '.join(problems)}); the bot will NOT "
                                  f"close it. Check Mudrex now.", alert)
            return False
        attempts, sent = row["exit_attempts"] or 0, row["exit_sent_at"] or 0
        if not sent or now - sent >= EXIT_RETRY_AFTER:
            if attempts >= EXIT_MAX_ATTEMPTS:
                set_order(con, row["id"], state="RECONCILE_REQUIRED",
                          error=f"protective exit not confirmed after {attempts} tries: {why}"[:300])
                event(con, "protect", f"{row['coin']}: automatic exit failed {attempts} times. "
                                      f"CLOSE IT IN THE MUDREX APP NOW.", alert)
                return False
            if not attempts:
                event(con, "protect", f"{row['coin']}: {why}. EXITING this position now (automatic protective "
                                      f"exit).", alert)
            renew(con, row["plan_id"])                                 # our fence is live right before sending
            busy = claim_exit(con, row)                                # atomic: journal BEFORE sending
        if pos is not None and not busy and (not sent or now - sent >= EXIT_RETRY_AFTER):
            try:
                client.close_position(pid)
            except Rejected as e:
                set_order(con, row["id"], state="RECONCILE_REQUIRED",
                          error=f"protective exit rejected: {e.errors}"[:300])
                event(con, "protect", f"{row['coin']}: protective exit REJECTED. CLOSE IT IN THE MUDREX APP NOW.",
                      alert)
                return False
            except (Ambiguous, Locked):
                pass                                                   # unknown: verify by position state
    last = state
    for i in range(POLL_TRIES):
        renew(con, row["plan_id"])                                   # a live wait keeps the lease
        try:
            last = position_state(client, pid, con)[0]
        except (Ambiguous, Locked):
            last = "unknown"
        if last == "closed":
            return exited(con, row, why, alert)
        sleep(min(1 + i, 5))
    if last == "unknown":                                              # vanished but not yet in history: wait
        still_unknown(con, order_row(con, row["id"]), alert)
        return False
    was = order_row(con, row["id"])["error"] or ""
    set_order(con, row["id"], state="RECONCILE_REQUIRED", error=f"protective exit not confirmed: {why}"[:300])
    if "protective exit not confirmed" not in was:                     # alert once, not every 5-minute pass
        event(con, "protect", f"{row['coin']}: protective exit NOT confirmed yet. Check Mudrex now.", alert)
    return False


def close_problem(con, pos, row):
    """Why a human-approved CLOSE must NOT be sent for this position right now (None = OK): it must still be the
    bot-owned position its verified opening order created (same symbol/side/currency/leverage/quantity), and no
    other close for it may be unresolved (no double close during another close's uncertainty window)."""
    own = con.execute("SELECT client_order_id FROM owned WHERE position_id=? AND closed_at IS NULL",
                      (row["position_id"],)).fetchone()
    if own is None or quarantined(con, row["position_id"]):
        return "position not owned by the bot"
    opener = con.execute("SELECT * FROM orders WHERE client_order_id=? AND action='OPEN'",
                         (own["client_order_id"],)).fetchone()
    if opener is None or not opener["fill_price"] or opener["state"] not in ("VERIFIED", "RECONCILE_REQUIRED"):
        return "its opening order never confirmed a fill"
    problems = identity_problems(pos, opener)
    if problems:
        return "position changed since it was opened (" + "; ".join(problems) + ")"
    return exit_in_flight(con, row["position_id"], row["id"])


def run_close(con, client, row, sleep, alert):
    if row["position_id"] not in owned_ids(con):
        set_order(con, row["id"], state="FAILED", error="refused: position not owned by the bot")
        return
    state, pos = position_state(client, row["position_id"], con)
    if state == "closed":
        set_order(con, row["id"], state="VERIFIED", error="already closed (stop hit?)")
        con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (int(time.time()), row["position_id"]))
        return
    if state == "unknown":                             # not visible, not in history yet: never assume closed
        still_unknown(con, row, alert)
        return
    why = close_problem(con, pos, row)
    if why:
        set_order(con, row["id"], state="FAILED", error=f"refused: {why}"[:300])
        event(con, "close", f"{row['coin']}: close NOT sent: {why}. Check Mudrex.", alert)
        return
    renew(con, row["plan_id"])
    gate()
    busy = claim_exit(con, row, "SUBMITTED")                          # atomic fence + journal before sending
    if busy:
        set_order(con, row["id"], state="FAILED", error=f"refused: {busy}"[:300])
        event(con, "close", f"{row['coin']}: close NOT sent: {busy}.", alert)
        return
    try:
        client.close_position(row["position_id"])
        set_order(con, row["id"], state="ACCEPTED")
    except Rejected as e:
        set_order(con, row["id"], state="FAILED", error=f"close rejected {e.status}: {e.errors}")
        return
    except (Ambiguous, Locked):
        pass                                                           # unknown: verify by position state
    for i in range(POLL_TRIES):
        renew(con, row["plan_id"])                                   # a live wait keeps the lease
        try:
            gone = position_state(client, row["position_id"], con)[0] == "closed"
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
    migration = trade_policy.migration_block_reason()
    if migration:
        return fail(con, row["id"], migration)
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
    caps = caps_state(con, eq, unrealized_inr(con, positions, rate), open_price_at=client.open_price_at)
    if caps["hit"]:
        return fail(con, row["id"], f"daily {caps['hit']} cap hit")
    if not caps["baseline_ok"]:
        return fail(con, row["id"], "today's starting balance unknown (bot was not watching at midnight)")
    if guard_tripped():
        return fail(con, row["id"], "performance guard tripped")
    a = client.asset(sym)
    price, planned = float(a["price"]), row["planned_price"]
    if planned and abs(price / planned - 1) > MAX_DRIFT:
        return fail(con, row["id"], f"price drifted {price / planned - 1:+.1%} since plan")
    step, min_qty, min_notional = float(a["quantity_step"]), float(a["min_contract"]), float(a["min_notional_value"])
    side = str(row["side"] or "LONG").upper()
    lev = float(row["planned_leverage"] or s1.LEV)
    exchange_max = float(a.get("max_leverage") or lev)
    if side not in {"LONG", "SHORT"} or not math.isfinite(lev) or not (1 <= lev <= 5 and lev <= exchange_max):
        return fail(con, row["id"], f"invalid planned side/leverage ({side}, {lev}x; exchange max {exchange_max}x)")
    # Leverage only converts the already risk-sized planned notional into margin. Quantity remains bounded by the
    # immutable planned_qty below, and the live risk is separately required not to exceed planned_risk_inr.
    notional_inr = min(row["planned_notional_inr"], lev * s1.CAPITAL_CAP_INR)
    bad = unknown_rate(positions, owned)
    if bad:
        return fail(con, row["id"], f"INR rate unknown for held {bad}: cannot size safely")
    try:
        held_margin = sum(position_margin_inr(p) for p in positions if p["id"] in owned)
    except PnlUnknown as e:
        return fail(con, row["id"], str(e))
    if held_margin + notional_inr / lev > s1.CAPITAL_CAP_INR:
        return fail(con, row["id"], f"allocation cap: margin Rs {held_margin:,.0f} + Rs {notional_inr / lev:,.0f} "
                                    f"would exceed Rs {s1.CAPITAL_CAP_INR:,.0f}")
    live_max_qty = notional_inr / (rate * HEDGE_BUFFER) / price
    planned_qty = float(row["planned_qty"] or live_max_qty)
    qty = floor_to(min(planned_qty, live_max_qty), step)
    if qty < min_qty or qty * price < min_notional:
        return fail(con, row["id"], "below Mudrex minimum at live price")
    if qty * price * rate * HEDGE_BUFFER / lev > float(client.funds()["balance"]):
        return fail(con, row["id"], "not enough free margin")
    pstep = float(a["price_step"])
    stop_distance = abs(float(row["planned_price"]) - float(row["planned_stop"]))
    target_distance = abs(float(row["planned_target"]) - float(row["planned_price"]))
    stop_value = floor_to(price - stop_distance, pstep) if side == "LONG" else ceil_to(price + stop_distance, pstep)
    target_value = floor_to(price + target_distance, pstep) if side == "LONG" else floor_to(price - target_distance, pstep)
    initial_stop, initial_target = fmt_step(stop_value, pstep), fmt_step(target_value, pstep)
    try:
        trade_policy.validate_bracket(side, price, initial_stop, initial_target)
        realized = cycle_realized_pnls(con)
    except (ValueError, PnlUnknown) as e:
        return fail(con, row["id"], f"risk inputs unavailable: {e}")
    pure_planned = float(row["planned_notional_inr"]) * stop_distance / float(row["planned_price"])
    cost_buffer = max(0.0, float(row["planned_risk_inr"] or pure_planned) - pure_planned)
    why = over_loss_budget(positions, owned, rate, eq, qty * price * rate, price, float(initial_stop), side,
                           realized, cost_buffer, con=con, candidate_risk_limit=row["planned_risk_inr"])
    if why:
        return fail(con, row["id"], why)
    qty_s = fmt_step(qty, step)
    set_order(con, row["id"], qty=qty_s)
    if not ensure_entry_alerts(con, row, alert, "before leverage"):
        return fail(con, row["id"], "Telegram delivery unavailable; entry not sent")
    renew(con, row["plan_id"])
    gate()
    try:
        client.set_leverage(sym, lev)
    except Rejected as e:
        return fail(con, row["id"], f"leverage rejected: {e.errors}")
    except (Locked, Ambiguous):
        pass                                                           # verified below either way
    saved = client.leverage(sym)
    if saved is None or saved[0] != lev or saved[1] != "ISOLATED":
        return fail(con, row["id"], f"leverage not verified as {lev}x isolated (got {saved})")
    accepted = submit_with_reconcile(con, client, order_row(con, row["id"]),
                                     lambda: client.place_market(sym, qty_s, row["client_order_id"], side,
                                                                 initial_stop, initial_target),
                                     sleep, alert, fresh=lambda: entry_submit_block_reason(
                                         con, client, row, sym, qty, float(initial_stop), float(initial_target),
                                         side, cost_buffer, alert))
    if accepted is None:
        return order_row(con, row["id"])["state"] == "FAILED"        # unknown outcome -> halt
    o = poll_fill(con, client, row, sleep)
    if o is None:
        return order_row(con, row["id"])["state"] == "FAILED"
    return protect_and_check(con, client, row, o, sleep, alert)


def over_loss_budget(positions, owned, rate, equity, notional_inr, price, stop, side="LONG",
                     realized_pnls=(), candidate_cost_buffer=0.0, con=None, candidate_risk_limit=None):
    """Compatibility gate for the fixed Rs500 collective cycle risk budget.

    `equity` is intentionally ignored: the owner's rupee cap does not grow with account equity.
    """
    del equity
    try:
        values = [float(notional_inr), float(price), float(stop), float(candidate_cost_buffer)]
        if not all(math.isfinite(x) for x in values) or price <= 0 or notional_inr < 0 or candidate_cost_buffer < 0:
            return "loss budget: candidate risk inputs are invalid"
        side = str(side).upper()
        if notional_inr == 0:
            new = candidate_cost_buffer
        else:
            valid = (side == "LONG" and 0 < stop < price) or (side == "SHORT" and stop > price)
            if not valid:
                return f"loss budget: invalid {side} stop"
            new = notional_inr * abs(price - stop) / price + candidate_cost_buffer
    except (TypeError, ValueError):
        return "loss budget: candidate risk inputs are invalid"
    if new > adaptive_risk.MAX_CANDIDATE_RISK_INR + 1e-9:
        return f"loss budget: candidate stop risk Rs {new:,.0f} > Rs {adaptive_risk.MAX_CANDIDATE_RISK_INR:,.0f}"
    if candidate_risk_limit is not None:
        try:
            limit = float(candidate_risk_limit)
        except (TypeError, ValueError):
            return "loss budget: planned candidate risk is invalid"
        if not math.isfinite(limit) or limit < 0 or new > limit + 0.01:
            return f"loss budget: live candidate risk Rs {new:,.2f} exceeds planned Rs {limit:,.2f}"
    try:
        if con is not None:
            held = sum(active_stop_risks(con, positions, owned))
        else:  # legacy pure callers: production always supplies the journal so original reserves are retained
            held = 0.0
            for p in positions:
                if p["id"] not in owned:
                    continue
                r = pos_rate(p)
                if r is None:
                    return f"loss budget: INR rate unknown for held {p['symbol']}"
                n, entry = float(p["quantity"]) * float(p["entry_price"]) * r, float(p["entry_price"])
                sl = float((p.get("stoploss") or {}).get("price") or 0)
                pside = str(p.get("order_type") or "LONG").upper()
                if not ((pside == "LONG" and 0 < sl < entry) or (pside == "SHORT" and sl > entry)):
                    return f"loss budget: verified stop missing for held {p['symbol']}"
                held += n * abs(entry - sl) / entry
    except PnlUnknown as e:
        return f"loss budget: {e}"
    try:
        losses = adaptive_risk.gross_realized_losses(realized_pnls)
    except ValueError:
        return "loss budget: current-cycle P&L is unknown"
    total = losses + held + new
    if total > trade_policy.DAILY_LOSS_LIMIT_INR + 1e-9:
        return (f"loss budget: realized losses plus all stops would lose Rs {total:,.0f} > "
                f"Rs {trade_policy.DAILY_LOSS_LIMIT_INR:,.0f}")
    return None


def ensure_entry_alerts(con, row, alert, stage):
    if alert is None and ALLOW_TEST_ALERT_SINK:
        alert = lambda _msg: True
    oid = enqueue_alert(con, f"{row['coin']} {row['side']}: entry checks passed ({stage}).",
                        dedupe_key=f"entry-ready:{row['client_order_id']}:{stage}", critical=True)
    deliver_alert(con, alert, oid)
    retry_alerts(con, alert, critical_only=True)
    return pending_alerts(con, critical_only=True) == 0


def entry_submit_block_reason(con, client, row, sym, qty, stop, target, side, cost_buffer, alert):
    """Recompute every mutable financial/ownership input immediately before each entry POST or retry."""
    created = con.execute("SELECT created_at FROM plans WHERE id=?", (row["plan_id"],)).fetchone()[0]
    if time.time() - created > ENTRY_MAX_AGE + SUBMIT_GRACE:
        return "refused: plan too old by the time this order was due"
    positions, owned = client.positions(), owned_ids(con)
    if any(p["symbol"] == sym for p in positions):
        return "position appeared on this symbol just before sending"
    rate = hedge_rate(client, positions)
    if not rate:
        return "no recent INR hedge rate just before sending"
    try:
        eq = bot_equity(con, client, positions, rate)
        caps = caps_state(con, eq, unrealized_inr(con, positions, rate), open_price_at=client.open_price_at)
        realized = cycle_realized_pnls(con)
    except PnlUnknown as e:
        return f"bot P&L unconfirmed just before sending: {e}"
    if not caps["baseline_ok"]:
        return "today's exact starting balance unavailable just before sending"
    if caps["hit"]:
        return f"daily {caps['hit']} cap hit just before sending"
    asset = client.asset(sym)
    price, planned = float(asset["price"]), row["planned_price"]
    if planned and abs(price / planned - 1) > MAX_DRIFT:
        return f"price drifted {price / planned - 1:+.1%} just before sending"
    try:
        trade_policy.validate_bracket(side, price, stop, target)
        loss_distance = abs(price - stop)
        reward_distance = abs(target - price)
        tick = float(asset.get("price_step") or 0.0)
        if reward_distance + max(1e-12, tick) < adaptive_risk.MIN_REWARD_RISK * loss_distance:
            return f"reward/risk fell below {adaptive_risk.MIN_REWARD_RISK:g}:1 just before sending"
    except ValueError as e:
        return f"invalid live bracket just before sending: {e}"
    live_notional = qty * price * rate
    try:
        if live_notional > float(row["planned_notional_inr"]) + 0.01:
            return "live notional exceeds the planned cost reserve just before sending"
    except (TypeError, ValueError):
        return "planned notional is invalid just before sending"
    why = over_loss_budget(positions, owned, rate, eq, live_notional, price, stop, side, realized,
                           cost_buffer, con=con, candidate_risk_limit=row["planned_risk_inr"])
    if why:
        return why + " just before sending"
    if not ensure_entry_alerts(con, row, alert, "before order"):
        return "Telegram delivery unavailable just before sending"
    return None


def stale_at_submit(con, client, row, sym):
    """Compatibility freshness check used by older callers/tests."""
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
    try:
        leverage = float(row["planned_leverage"] or s1.LEV)
        allowed = min(float(row["planned_notional_inr"]), leverage * s1.CAPITAL_CAP_INR)
    except (TypeError, ValueError, OverflowError):
        allowed = 0.0
    if all(math.isfinite(x) for x in (applied, actual, allowed)) and applied > 0 and allowed > 0 and actual <= allowed:
        return True
    cur = order_row(con, row["id"])
    set_order(con, row["id"], state="RECONCILE_REQUIRED",
              error=(cur["error"] + "; " if cur["error"] else "") +
              ("applied INR rate missing" if applied <= 0 else f"INR notional {actual:,.0f} > allowed {allowed:,.0f}"))
    event(con, "hedge", f"{row['coin']}: " + ("Mudrex did not report the INR rate it applied" if applied <= 0 else
                        f"filled INR notional Rs {actual:,.0f} exceeds the approved Rs {allowed:,.0f}")
          + ". Entries halted; reduce the position in Mudrex if needed.", alert)
    return False


def fail_safe_exit(con, client, row, why, sleep, alert):
    """After an unexpected error on an ENTRY: exit its position if it is bot-owned and validated (never raises)."""
    cur = order_row(con, row["id"])
    if row["action"] != "OPEN" or not cur["position_id"] or cur["position_id"] not in owned_ids(con) \
            or quarantined(con, cur["position_id"]):
        return
    try:
        protective_close(con, client, cur, why, sleep, alert)
    except Exception as e:                                             # noqa: BLE001
        event(con, "protect", f"{row['coin']}: protective exit failed ({type(e).__name__}). "
                              f"CLOSE IT IN THE MUDREX APP NOW.", alert)


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
    event(con, "execute", f"plan {plan_id} approved by {approver}", alert)
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
            renew(con, plan_id)
            if row["action"] == "CLOSE":
                run_close(con, client, row, sleep, alert)
                if order_row(con, row["id"])["state"] != "VERIFIED":
                    entries_allowed = False
            elif not entries_allowed:
                set_order(con, row["id"], state="FAILED", error="halted: an earlier order is unconfirmed")
            else:
                entries_allowed = run_open(con, client, row, sleep, alert)
        except LeaseLost as e:                   # another process now owns this plan: touch nothing more
            event(con, "execute", f"plan {plan_id}: {e}; stopped, the new lease holder finishes it", alert)
            return "LEASE_LOST", [str(e)]
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
            fail_safe_exit(con, client, row, f"unexpected {type(e).__name__} after the fill", sleep, alert)
    final = plan_outcome(con, plan_id)
    con.execute("UPDATE plans SET state=? WHERE id=?", (final, plan_id))
    release(con, plan_id)
    summary = [f"{r['action']} {r['coin']}: {r['state']}" + (f" ({r['error']})" if r["error"] else "")
               for r in con.execute("SELECT * FROM orders WHERE plan_id=? ORDER BY seq", (plan_id,))]
    event(con, "execute", f"plan {plan_id} finished {final}: " + "; ".join(summary), alert)
    return final, summary


def resume_close(con, client, row, alert):
    """A human-approved CLOSE left unfinished by a crash. Gone -> VERIFIED. Still open -> the approval covers
    finishing it: re-send only if never sent or the last send was >= EXIT_RETRY_AFTER ago (journaled first),
    at most EXIT_MAX_ATTEMPTS times, then hand over to the human. Only ever closes a bot-owned position."""
    now = int(time.time())
    state, pos = position_state(client, row["position_id"], con)
    if state == "closed":
        set_order(con, row["id"], state="VERIFIED")
        con.execute("UPDATE owned SET closed_at=? WHERE position_id=? AND closed_at IS NULL",
                    (now, row["position_id"]))
        return
    if state == "unknown":
        still_unknown(con, row, alert)                                 # wait for a definite answer, never silently
        return
    why = close_problem(con, pos, row)
    if why:
        set_order(con, row["id"], state="FAILED", error=f"refused: {why}"[:300])
        event(con, "reconcile", f"{row['coin']}: approved close NOT finished: {why}. Check Mudrex.", alert)
        return
    try:
        gate()                                                         # a human close respects STOP / live flag
    except Halt as h:
        set_order(con, row["id"], state="RECONCILE_REQUIRED", error=f"close paused: {h}")
        return
    attempts, sent = row["exit_attempts"] or 0, row["exit_sent_at"] or 0
    if sent and now - sent < EXIT_RETRY_AFTER:
        return                                                         # wait: the earlier close may still land
    if attempts >= EXIT_MAX_ATTEMPTS:
        set_order(con, row["id"], state="RECONCILE_REQUIRED",
                  error=f"approved close not confirmed after {attempts} tries")
        event(con, "reconcile", f"{row['coin']}: approved close failed {attempts} times. "
                                f"CLOSE IT IN THE MUDREX APP NOW.", alert)
        return
    renew(con, row["plan_id"])
    busy = claim_exit(con, row, "SUBMITTED")                          # atomic fence + journal before sending
    if busy:
        return                                                         # another exit owns it right now
    event(con, "reconcile", f"{row['coin']}: finishing the approved close (attempt {attempts + 1}).", alert)
    try:
        client.close_position(row["position_id"])
    except Rejected as e:
        set_order(con, row["id"], state="RECONCILE_REQUIRED", error=f"close rejected {e.status}: {e.errors}"[:300])
        event(con, "reconcile", f"{row['coin']}: approved close REJECTED. Check Mudrex now.", alert)
    except (Ambiguous, Locked):
        pass                                                           # next reconcile checks the position


def reconcile(con, client, sleep=time.sleep, alert=None, min_age=0):
    """After a crash/restart: resolve every order that is not terminal, by client_order_id. Never resubmits
    entries. Works ONE plan at a time under its own fenced lease (skips any plan a live run still holds).
    Unsent entries of a crashed run are FAILED (a buy always needs a fresh approval); an approved CLOSE that was
    never sent is finished (resume_close: identity re-checked, STOP/live respected)."""
    now = int(time.time())
    cands = [r["id"] for r in con.execute(
        "SELECT id FROM plans WHERE state IN ('EXECUTING','APPROVED','RECONCILE_REQUIRED') AND approved_at <= ?",
        (now - min_age,))]
    for pid in cands:
        if not take_lease(con, pid):                                   # a live run holds it: not ours
            continue
        try:
            reconcile_plan(con, client, pid, sleep, alert)
            if pid in LEASES:                                          # still ours: finish the bookkeeping
                con.execute("UPDATE orders SET state='FAILED', error='not submitted (run interrupted)' "
                            "WHERE plan_id=? AND state='PLANNED' AND action='OPEN'", (pid,))
                con.execute("UPDATE plans SET state=? WHERE id=?", (plan_outcome(con, pid), pid))
        finally:
            release(con, pid)


def reconcile_plan(con, client, pid, sleep, alert):
    for row in con.execute("SELECT * FROM orders WHERE plan_id=? AND (state IN ('SUBMITTED','ACCEPTED','FILLED',"
                           "'RECONCILE_REQUIRED') OR (state='PLANNED' AND action='CLOSE')) "
                           "ORDER BY action='OPEN', seq", (pid,)).fetchall():         # closes first, like execute()
        try:
            renew(con, pid)
            if row["action"] == "OPEN" and "protective exit" in (row["error"] or ""):
                protective_close(con, client, row, row["error"], sleep, alert)   # finish an unconfirmed exit
                continue
            if row["action"] == "CLOSE":
                resume_close(con, client, row, alert)
                continue
            o = lookup_until_known(client, row["client_order_id"], sleep, con=con, plan_id=pid)   # raises if inconclusive
            renew(con, pid)
            if o is None and row["exchange_order_id"]:
                o = client.order_by_id(row["exchange_order_id"])      # Mudrex acknowledged it: never "not placed"
            if o is None:
                if time.time() - (row["updated_at"] or 0) < RECONCILE_GRACE:
                    continue                                           # order history may lag a just-sent order
                set_order(con, row["id"], state="FAILED", error="not found on exchange after restart")
                set_trade_set_state(con, row, "FAILED")
                continue
            if o.get("status") in TERMINAL_BAD and moved_money(o):
                if partial_fill(con, row, o) is not None:
                    protect_and_check(con, client, order_row(con, row["id"]), o, sleep, alert)
                continue
            if o.get("status") in TERMINAL_BAD:
                set_order(con, row["id"], state="FAILED", error=f"order {o['status']}")
                set_trade_set_state(con, row, "FAILED")
                continue
            if o.get("status") not in TERMINAL_OK:
                continue                                               # still working: next reconcile
            if row["state"] != "FILLED" or not row["fill_price"]:
                set_order(con, row["id"], state="FILLED", fill_price=float(o["filled_price"]),
                          filled_qty=float(o["filled_quantity"]), position_id=o.get("future_position_uuid"))
            protect_and_check(con, client, row, o, sleep, alert)
        except ApiError as e:
            event(con, "reconcile", f"{row['coin']}: reconcile deferred ({e})", alert)
        except LeaseLost as e:                                         # someone else owns the plan now
            event(con, "reconcile", f"{row['coin']}: {e}; left to the lease holder", None)
            return
        except Exception as e:                                         # noqa: BLE001 - malformed exchange data etc.
            event(con, "reconcile", f"{row['coin']}: reconcile error {type(e).__name__}: {e}"[:300], alert)
            fail_safe_exit(con, client, row, f"reconcile could not verify it ({type(e).__name__})", sleep, alert)
