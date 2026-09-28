"""24/7 watcher and bounded autonomous dispatcher. Every CHECK_SEC it:
  - computes BOT-only equity and today's P&L vs fixed +/-Rs500 caps (execution.bot_equity / caps_state): allocation
    Rs 5,000 + realized/unrealized P&L of bot-owned positions. Deposits, withdrawals, manual trades excluded.
    When a cap is hit it alerts you and records a CLOSE-ALL plan that you approve (caps never close by themselves).
  - notices bot positions that closed, journals them (journal.csv) and runs the performance guard (guard.json)
  - warns about manual positions and bot positions without a valid exchange stop-loss AND take-profit
  - after the daily close (05:35 IST) evaluates again at a bounded interval through the current IST cycle,
    sends every decision, and (only after migration certification) dispatches eligible sets 1-3 autonomously;
    each extra set gets an exact Telegram approval button; the next evaluation is scheduled only after the
    current decision was generated AND sent, while delivery failures retry the same immutable plan
  - writes watch_status.json (heartbeat) and alerts if the approver heartbeat goes stale
"""
import csv
import hashlib
import json
import os
import subprocess
import time
import traceback
from datetime import datetime

import config
import execution as ex
import s1
import trade_policy
from mudrex_client import Ambiguous, ApiError, Client

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "watch_state.json")
STATUS_PATH = os.path.join(HERE, "watch_status.json")
LOG_PATH = os.path.join(HERE, "watcher.log")
APPROVER_HEARTBEAT = os.path.join(HERE, "approver_heartbeat.json")
JOURNAL_PATH = os.path.join(HERE, "journal.csv")
CHECK_SEC = 300
PLAN_INTERVAL_SEC = 15 * 60
DAY = 86400
PLAN_AFTER = "05:35"
STALE_SEC = 900
# performance guard thresholds set from the original S1 backtest (worst losing streak 10, worst drawdown ~30%)
WARN_STREAK, TRIP_STREAK, WARN_DD, TRIP_DD = 10, 13, 0.25, 0.35
JOURNAL_FIELDS = ["coin", "opened", "closed", "days", "entry", "exit", "pnl_inr", "outcome", "btc_mood", "signal"]


def ist_str(fmt="%Y-%m-%d %H:%M"):
    return time.strftime(fmt, time.gmtime(time.time() + config.IST_OFFSET))


def log(msg):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{ist_str()} IST  {msg}\n")


def _telegram_send(msg, buttons=None):
    """Low-level delivery callback. Callers that need durability must queue before using it."""
    import telegram_bot
    return telegram_bot.send(msg, buttons) is not None if telegram_bot.enabled() else False


def notify(msg, buttons=None, *, con=None, dedupe_key=None):
    """Telegram + local notice.

    Plain watcher updates use the durable execution outbox. Interactive messages retain their own immutable-plan
    retry state because the generic outbox intentionally stores no approval buttons.
    """
    log(f"NOTIFY {msg}")
    if buttons:
        delivered = _telegram_send(msg, buttons)
    elif not trade_policy.TELEGRAM_UPDATES_REQUIRED:
        delivered = _telegram_send(msg)
    else:
        outbox = con or ex.db()
        key = dedupe_key or (f"watcher:{ist_str('%Y-%m-%d')}:" +
                             hashlib.sha256(msg.encode("utf-8")).hexdigest())
        oid = ex.enqueue_alert(outbox, msg, dedupe_key=key, critical=True)
        delivered = ex.deliver_alert(outbox, _telegram_send, oid)
    safe = msg.replace("'", "").replace('"', "")[:240]
    ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
          "$n=New-Object System.Windows.Forms.NotifyIcon; $n.Icon=[System.Drawing.SystemIcons]::Information; "
          f"$n.Visible=$true; $n.ShowBalloonTip(15000,'Mudrex bot','{safe}',"
          "[System.Windows.Forms.ToolTipIcon]::Info); Start-Sleep 16; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass
    if not delivered:
        log("Telegram delivery FAILED for the message above")
    return delivered


def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


# ---------- daily plan with retry (plan_day saved only after success)

def maybe_plan(st, make_plan, now_hm=None, today=None, auto_execute=None, now=None):
    """Generate and send one cycle decision when due, then dispatch an eligible autonomous set.

    The daily strategy input still changes only after the 05:30 IST candle closes, but the set ledger and
    positions can change throughout the day. Re-evaluating every 15 minutes lets the next bounded set become
    eligible without creating concurrent plans. A failed Telegram delivery retries the same immutable plan.
    """
    today = today or ist_str("%Y-%m-%d")
    now = time.time() if now is None else float(now)
    if (now_hm or ist_str("%H:%M")) < PLAN_AFTER:
        return False
    if st.get("plan_cycle") == today and now < st.get("plan_next_at", 0):
        return False
    if now < st.get("plan_retry_at", 0):
        return False
    try:
        pend = st.get("pending_plan")
        if pend and pend.get("_day") == today and now - pend.get("created_at", 0) < ex.ENTRY_MAX_AGE:
            p = pend                                  # delivery retry: resend the SAME plan, never make a new one
        else:
            p = dict(make_plan(), _day=today)
            st["pending_plan"] = p
        import telegram_bot
        text, buttons = telegram_bot.plan_message(p)
        opens = [o for o in p.get("orders", []) if o.get("action") == "OPEN"]
        actionable = bool(p.get("plan_id") and p.get("live_enabled") and not p.get("blocked"))
        # Re-planning runs every 15 min: repeat a non-actionable message (nothing to do / blocked) only when it
        # changes, or Telegram gets ~75 identical messages a day. Actionable plans are always sent.
        sig = [p.get("blocked"), sorted((o.get("action"), o.get("coin"), o.get("side")) for o in p.get("orders", [])
                                        if o.get("action") in ("OPEN", "CLOSE"))]
        repeat = ((not actionable and buttons is None and st.get("last_plan_sig") == sig) or
                  bool(p.get("plan_id") and p.get("plan_id") == st.get("last_plan_id")))   # same plan: sent already
        sent = True if repeat else notify(text, buttons=buttons)
        if not sent:
            raise ConnectionError("Telegram delivery failed")                 # retry later; plan_day not saved
        # Keep plan_day for old dashboards, but plan_cycle/plan_next_at control the repeated cycle schedule.
        st["plan_day"], st["plan_cycle"] = today, today
        st["plan_last_at"], st["plan_next_at"] = now, now + PLAN_INTERVAL_SEC
        st["plan_fails"], st["plan_retry_at"] = 0, 0
        st.pop("pending_plan", None)
        st["last_plan_sig"], st["last_plan_id"] = sig, p.get("plan_id")
        # A set above the autonomous three waits for its own Telegram tap (approver runs it); never auto-run it.
        # A plan that only CLOSES bot positions (e.g. S4 maximum hold) is risk-reducing: it runs by itself once the
        # autonomous mode is certified (execution still refuses while STOP is present).
        closes = [o for o in p.get("orders", []) if o.get("action") == "CLOSE"]
        auto_close = (bool(closes) and not opens and p.get("plan_id") and p.get("live_enabled")
                      and trade_policy.migration_block_reason() is None)
        if auto_execute and ((opens and actionable and not p.get("needs_approval")) or auto_close):
            try:
                final, summary = auto_execute(p)
                notify(f"Autonomous plan {p['plan_id']} finished {final}:\n" + "\n".join(summary))
            except Exception as e:  # delivery succeeded; never create a duplicate plan after an execution error
                log(f"autonomous execution error: {type(e).__name__}: {e}")
                notify(f"Autonomous plan {p['plan_id']} hit {type(e).__name__}; entries are halted. Check status.")
        return True
    except Exception as e:
        st["plan_fails"] = st.get("plan_fails", 0) + 1
        st["plan_retry_at"] = now + min(60 * 2 ** st["plan_fails"], 3600)
        log(f"plan failed ({st['plan_fails']}): {type(e).__name__}: {e}")
        if st["plan_fails"] == 3:
            notify(f"Could not prepare today's S1 plan after 3 tries ({type(e).__name__}). Retrying; check the PC.")
        return False


# ---------- journal + guard (bot positions only)

def journal_and_guard(st, con, client, bot_open_upnl):
    # closed bot positions with confirmed P&L come from the local ledger (execution.sync_owned); timestamps are
    # our own observation times, because Mudrex position history has no documented timestamps
    hist = {p["id"]: p for p in client.history("positions")[0]}
    done = set(st.setdefault("journaled", []))
    rows = []
    for r in con.execute("SELECT * FROM owned WHERE closed_at IS NOT NULL AND realized_pnl IS NOT NULL").fetchall():
        if r["position_id"] in done:
            continue
        p = hist.get(r["position_id"], {})
        o, c, pnl = r["opened_at"] or r["closed_at"], r["closed_at"], float(r["realized_pnl"])
        rows.append(dict(coin=r["coin"], opened=time.strftime("%Y-%m-%d", time.gmtime(o)),
                         closed=time.strftime("%Y-%m-%d", time.gmtime(c)), days=round((c - o) / DAY, 1),
                         entry=p.get("entry_price", ""), exit=p.get("closed_price", ""), pnl_inr=round(pnl, 2),
                         outcome="win" if pnl > 0 else "loss", btc_mood="", signal=""))
        done.add(r["position_id"])
    if rows:
        new_file = not os.path.exists(JOURNAL_PATH)
        with open(JOURNAL_PATH, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(sorted(rows, key=lambda r: r["closed"]))
        for r in rows:
            notify(f"S1 trade closed: {r['coin']} {r['outcome']} Rs {r['pnl_inr']:+,.0f} after {r['days']} days.")
    st["journaled"] = sorted(done)
    # guard statistics come from the database (one row per position), never from the CSV, so a crash between
    # writing journal.csv and saving watcher state cannot double-count a trade
    pnls = [r[0] for r in con.execute("SELECT realized_pnl FROM owned WHERE closed_at IS NOT NULL AND "
                                      "realized_pnl IS NOT NULL ORDER BY closed_at, position_id")]
    streak = 0
    for pnl in pnls:
        streak = streak + 1 if pnl <= 0 else 0
    bot_eq = s1.CAPITAL_CAP_INR + sum(pnls) + bot_open_upnl
    dd = 1 - bot_eq / ex.update_peak(con, bot_eq)             # peak lives in the database, not watcher state
    guard = load(ex.GUARD_PATH, dict(tripped=False))
    guard.update(streak=streak, drawdown=round(dd, 4), checked=time.time())
    if not guard.get("tripped") and (streak >= TRIP_STREAK or dd >= TRIP_DD):
        guard.update(tripped=True, at=time.time(),
                     reason=f"{streak} losses in a row" if streak >= TRIP_STREAK else f"drawdown {dd:.0%}")
        notify(f"PERFORMANCE GUARD TRIPPED ({guard['reason']}): worse than anything in the backtest. "
               f"No new entries until you review and reset guard.json.")
    elif not guard.get("tripped") and (streak >= WARN_STREAK or dd >= WARN_DD) and st.get("guard_warned") != ist_str("%Y-%m-%d"):
        st["guard_warned"] = ist_str("%Y-%m-%d")
        notify(f"Guard warning: {streak} losses in a row, {dd:.0%} below peak (still within backtest history).")
    save(ex.GUARD_PATH, guard)
    return guard


STARTED = time.time()


def check_approver(st, now=None):
    """Alert once if the Telegram approver is expected (Telegram configured) but its heartbeat is stale, or
    missing after a startup grace period. Returns the alert text or None."""
    import telegram_bot
    if not telegram_bot.enabled():
        return None
    now = now or time.time()
    hb = load(APPROVER_HEARTBEAT, None)
    if hb is None:
        if now - STARTED > STALE_SEC and not st.get("approver_missing_warned"):
            st["approver_missing_warned"] = True
            msg = "Telegram approver is not running (no heartbeat). Approve buttons will not work until it starts."
            notify(msg)
            return msg
        return None
    if now - hb["at"] > STALE_SEC and st.get("approver_stale_warned", 0) < hb["at"]:
        st["approver_stale_warned"] = hb["at"]
        msg = "Telegram approver looks stopped (no heartbeat for 15 min). Approvals will not work until it restarts."
        notify(msg)
        return msg
    return None


# ---------- one check

def check(st, client=None, con=None, make_plan=None):
    client = client or Client()
    con = con or ex.db()
    ex.recover_ownership(con, client)
    positions = client.positions()
    owned = ex.owned_ids(con)
    rate = ex.hedge_rate(client, positions) or config.INR_PER_USDT
    view = []
    for p in positions:
        px = float(p.get("mark_price") or p.get("last_price") or p["entry_price"])
        side = 1 if p["order_type"] == "LONG" else -1
        u = side * float(p["quantity"]) * (px - float(p["entry_price"])) * float(p.get("entry_hedge_rate") or rate)
        view.append(dict(id=p["id"], symbol=p["symbol"], side=p["order_type"], qty=float(p["quantity"]),
                         entry=float(p["entry_price"]), price=px, upnl_inr=round(u, 2), bot=p["id"] in owned,
                         leverage=float(p.get("leverage") or 0) or None,
                         sl=float((p.get("stoploss") or {}).get("price") or 0) or None,
                         tp=float((p.get("takeprofit") or {}).get("price") or 0) or None))
    pnl_unknown = None
    try:
        bot_eq = ex.bot_equity(con, client, positions, rate)
    except ex.PnlUnknown as e:
        pnl_unknown = str(e)
        bot_eq = s1.CAPITAL_CAP_INR + ex.unrealized_inr(con, positions, rate) + con.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM owned WHERE realized_pnl IS NOT NULL").fetchone()[0]
        if st.get("pnl_unknown_warned") != ist_str("%Y-%m-%d"):
            st["pnl_unknown_warned"] = ist_str("%Y-%m-%d")
            notify(f"Bot P&L not confirmed yet ({pnl_unknown}): new entries are blocked until Mudrex shows it.")
    caps = ex.caps_state(con, bot_eq, ex.unrealized_inr(con, positions, rate), trusted=pnl_unknown is None,
                         open_price_at=client.open_price_at)

    verified = {r["position_id"]: (r["stop_price"], r["target_price"]) for r in con.execute(
        "SELECT position_id, stop_price, target_price FROM orders WHERE action='OPEN' AND stop_price IS NOT NULL")}
    for v in view:
        coin = v["symbol"].removesuffix("USDT")
        if not v["bot"] and v["id"] not in st.setdefault("warned_manual", []):
            st["warned_manual"].append(v["id"])
            notify(f"Manual {v['side']} on {coin}: the bot will not trade {coin} while it is open.")
        if not v["bot"]:
            continue
        wanted = verified.get(v["id"])
        want_sl = float(wanted[0]) if wanted and wanted[0] else None
        want_tp = float(wanted[1]) if wanted and wanted[1] else None
        side_ok = ((v["sl"] < v["price"] < v["tp"]) if v["side"] == "LONG" else
                   (v["tp"] < v["price"] < v["sl"])) if v["sl"] and v["tp"] else False
        moved = ((v["sl"] and want_sl and abs(v["sl"] - want_sl) > ex.stop_tolerance(v["entry"], 0)) or
                 (v["tp"] and want_tp and abs(v["tp"] - want_tp) > ex.stop_tolerance(v["entry"], 0)))
        problem = ("has NO stop-loss" if not v["sl"] else
                   "has NO take-profit" if not v["tp"] else
                   f"has invalid {v['side']} bracket SL {v['sl']} / TP {v['tp']} at price {v['price']}"
                   if not side_ok else
                   f"bracket moved to SL {v['sl']} / TP {v['tp']} (verified {want_sl} / {want_tp})"
                   if moved else None)
        key = f"{v['id']}:{problem}"
        if not problem:                               # healthy again: re-arm, so a new failure alerts again
            st["warned_sl"] = [k for k in st.get("warned_sl", []) if not k.startswith(f"{v['id']}:")]
        elif key not in st.setdefault("warned_sl", []):
            if notify(f"{coin}: bot position {problem} on Mudrex. Check it in the app now."):
                st["warned_sl"].append(key)           # handled only once delivered; otherwise retried next check

    sent_cap = st.get("cap_sent")
    if caps["hit"] and sent_cap and sent_cap.get("day") == caps["day"] == st.get("cap_day"):
        row = con.execute("SELECT state, note FROM plans WHERE id=?", (sent_cap["plan_id"],)).fetchone()
        if row and row["state"] == "FAILED" and (row["note"] or "") != "rejected by user":
            st.pop("cap_day", None)                   # expired/failed without a Keep: offer Close all again

    if caps["hit"] and st.get("cap_day") != caps["day"]:
        bot_open = [v for v in view if v["bot"]]
        msg = (f"Daily {caps['hit']} cap hit: bot Rs {caps['pnl']:+,.0f} today (cap {caps['cap']:,.0f}). "
               f"No new entries today.")
        if bot_open:
            pend = st.get("cap_plan")
            alive = pend and con.execute("SELECT state FROM plans WHERE id=?", (pend.get("plan_id"),)).fetchone()
            if not pend or pend.get("day") != caps["day"] or not alive or alive[0] != "PLANNED":
                pid = ex.record_plan(con, caps["day"], [dict(coin=v["symbol"].removesuffix("USDT"), action="CLOSE",
                                                             position_id=v["id"]) for v in bot_open], {"reason": "cap"})
                st["cap_plan"] = pend = dict(day=caps["day"], plan_id=pid, n=len(bot_open))
            sent = notify(msg + f" Close all {pend['n']} bot position(s)?",
                          buttons=[[("Close all", f"approve:{pend['plan_id']}"), ("Keep", f"reject:{pend['plan_id']}")]])
        else:
            sent = notify(msg)
        if sent:                                      # mark handled only after confirmed delivery (else retry)
            st["cap_day"] = caps["day"]
            st["cap_sent"] = st.pop("cap_plan", None)
    elif caps["pnl"] <= -0.8 * caps["cap"] and st.get("warned_80") != caps["day"]:
        st["warned_80"] = caps["day"]
        notify(f"Warning: bot down Rs {-caps['pnl']:,.0f} today (loss cap Rs {caps['cap']:,.0f}).")

    if not caps["baseline_ok"] and st.get("baseline_warned") != caps["day"] and notify(
            "Today's starting balance is unknown (the bot was not watching at midnight while positions were open). "
            "No new buys today; tomorrow starts normally."):
        st["baseline_warned"] = caps["day"]
    stuck = con.execute("SELECT DISTINCT plan_id FROM orders WHERE state IN "
                        "('SUBMITTED','ACCEPTED','FILLED','RECONCILE_REQUIRED') AND updated_at < ?",
                        (int(time.time()) - 1200,)).fetchall()
    for r in stuck:                                   # read-only nudge; the approver reconciles automatically
        key = f"stuck:{r['plan_id']}"
        if key not in st.setdefault("warned_stuck", []) and notify(
                f"Plan {r['plan_id']} has an unfinished order (>20 min). A position may lack a checked stop-loss. "
                f"Check Mudrex; on the PC run: python live_trader.py reconcile"):
            st["warned_stuck"].append(key)

    guard = journal_and_guard(st, con, client, sum(v["upnl_inr"] for v in view if v["bot"]))
    # queued Telegram retries AFTER the position/bracket checks: a Telegram outage (up to one 40 s timeout per
    # retry pass) must never delay stop-loss monitoring
    ex.retry_alerts(con, _telegram_send)
    if make_plan is None:
        import live_trader
        make_plan = lambda: live_trader.plan(client, con)   # noqa: E731
    maybe_plan(st, make_plan,
               auto_execute=lambda p: ex.execute(con, client, p["plan_id"], "autonomous cycle",
                                                  alert=_telegram_send))

    check_approver(st)
    save(STATUS_PATH, dict(at=time.time(), ok=True, positions=view, guard=guard, check_every_sec=CHECK_SEC,
                           pnl_unknown=pnl_unknown,
                           bot=dict(equity=round(bot_eq, 2), day=caps["day"], day_start=round(caps["start"], 2),
                                    day_pnl=round(caps["pnl"], 2), cap=round(caps["cap"], 2), cap_hit=caps["hit"]),
                           live_enabled=ex.live_enabled(), stop=os.path.exists(ex.STOP_PATH)))


def main():
    st = load(STATE_PATH, {})
    log("watcher started")
    notify("Mudrex watcher started: checking every 5 minutes.")
    fails = 0
    while True:
        try:
            check(st)
            save(STATE_PATH, st)
            fails = 0
        except (ApiError, Ambiguous, OSError, ValueError, KeyError) as e:
            fails += 1
            log(f"check failed ({fails}): {type(e).__name__}: {e}")
            save(STATUS_PATH, dict(load(STATUS_PATH, {}), at_error=time.time(), ok=False, error=str(e)[:200]))
            if fails == 3:
                notify("Watcher cannot reach Mudrex (3 tries). Exchange stop-losses still protect bot positions.")
        except Exception:
            log("unexpected error:\n" + traceback.format_exc())
        time.sleep(min(CHECK_SEC * (2 ** min(fails, 3)), 1800) if fails else CHECK_SEC)


if __name__ == "__main__":
    main()
