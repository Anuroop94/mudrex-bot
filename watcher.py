"""24/7 watcher. Never places, changes or closes orders: it only READS Mudrex (GET) and writes local files
(journal plans for you to approve, status, alerts). Every CHECK_SEC it:
  - computes BOT-only equity and today's P&L vs the 5% caps (execution.bot_equity / caps_state): allocation
    Rs 5,000 + realized/unrealized P&L of bot-owned positions. Deposits, withdrawals, manual trades excluded.
    When a cap is hit it alerts you and records a CLOSE-ALL plan that you approve (caps never close by themselves).
  - notices bot positions that closed, journals them (journal.csv) and runs the performance guard (guard.json)
  - warns about manual positions on basket coins and bot positions without a stop-loss
  - after the daily close (05:35 IST) generates the S1 plan and sends it with Approve/Reject buttons;
    plan_day is saved only after the plan was generated AND sent; failures retry with bounded backoff
  - writes watch_status.json (heartbeat) and alerts if the approver heartbeat goes stale
"""
import csv
import json
import os
import subprocess
import time
import traceback
from datetime import datetime

import config
import execution as ex
import s1
from mudrex_client import Ambiguous, ApiError, Client

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "watch_state.json")
STATUS_PATH = os.path.join(HERE, "watch_status.json")
LOG_PATH = os.path.join(HERE, "watcher.log")
APPROVER_HEARTBEAT = os.path.join(HERE, "approver_heartbeat.json")
JOURNAL_PATH = os.path.join(HERE, "journal.csv")
CHECK_SEC = 300
DAY = 86400
PLAN_AFTER = "05:35"
STALE_SEC = 900
# performance guard, from the S1 backtest (6 years, 127 trades): worst losing streak 10, worst drawdown 30.4%
WARN_STREAK, TRIP_STREAK, WARN_DD, TRIP_DD = 10, 13, 0.25, 0.35
JOURNAL_FIELDS = ["coin", "opened", "closed", "days", "entry", "exit", "pnl_inr", "outcome", "btc_mood", "signal"]


def ist_str(fmt="%Y-%m-%d %H:%M"):
    return time.strftime(fmt, time.gmtime(time.time() + config.IST_OFFSET))


def log(msg):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{ist_str()} IST  {msg}\n")


def notify(msg, buttons=None):
    """Telegram (if configured) + Windows balloon + log. Returns False only if Telegram is configured and the
    message could not be delivered (callers that must be sure, like the daily plan, retry on False)."""
    log(f"NOTIFY {msg}")
    import telegram_bot
    delivered = (not telegram_bot.enabled()) or telegram_bot.send(msg, buttons) is not None
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

def maybe_plan(st, make_plan, now_hm=None, today=None):
    """Generate + send today's plan once. Returns True if done. On failure, retries with bounded backoff."""
    today = today or ist_str("%Y-%m-%d")
    if (now_hm or ist_str("%H:%M")) < PLAN_AFTER or st.get("plan_day") == today:
        return False
    if time.time() < st.get("plan_retry_at", 0):
        return False
    try:
        pend = st.get("pending_plan")
        if pend and pend.get("_day") == today and time.time() - pend.get("created_at", 0) < 3600:
            p = pend                                  # delivery retry: resend the SAME plan, never make a new one
        else:
            p = dict(make_plan(), _day=today)
            st["pending_plan"] = p
        todo =[o for o in p["orders"] if o["action"] in ("OPEN", "CLOSE")]
        if todo and p.get("plan_id") and p.get("live_enabled"):
            lines = [f"{o['action']} {o['coin']}" + (f" ~Rs {o['notional_inr']:,.0f}" if o["action"] == "OPEN" else "")
                     for o in todo]
            sent = notify(f"S1 plan {p['plan_id']}: {len(todo)} order(s) (expires in 3h; stops re-anchored to fills):\n"
                          + "\n".join(lines),
                          buttons=[[("Approve", f"approve:{p['plan_id']}"), ("Reject", f"reject:{p['plan_id']}")]])
        elif todo:
            sent = notify(f"S1 plan {p['plan_id']}: {len(todo)} order(s), but LIVE_TRADING_ENABLED is false.")
        else:
            sent = notify("S1: no orders today." + (f" New entries blocked: {p['blocked']}." if p.get("blocked") else ""))
        if not sent:
            raise ConnectionError("Telegram delivery failed")                 # retry later; plan_day not saved
        st["plan_day"], st["plan_fails"], st["plan_retry_at"] = today, 0, 0     # only after generation + delivery
        st.pop("pending_plan", None)
        return True
    except Exception as e:
        st["plan_fails"] = st.get("plan_fails", 0) + 1
        st["plan_retry_at"] = time.time() + min(60 * 2 ** st["plan_fails"], 3600)
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
    all_rows = []
    if os.path.exists(JOURNAL_PATH):
        with open(JOURNAL_PATH, newline="") as f:
            all_rows = list(csv.DictReader(f))
    streak = 0
    for r in all_rows:
        streak = streak + 1 if float(r["pnl_inr"]) <= 0 else 0
    bot_eq = s1.CAPITAL_CAP_INR + sum(float(r["pnl_inr"]) for r in all_rows) + bot_open_upnl
    st["bot_peak"] = max(st.get("bot_peak", s1.CAPITAL_CAP_INR), bot_eq)
    dd = 1 - bot_eq / st["bot_peak"]
    guard = load(ex.GUARD_PATH, dict(tripped=False))
    guard.update(streak=streak, drawdown=round(dd, 4), checked=time.time())
    if not guard.get("tripped") and (streak >= TRIP_STREAK or dd >= TRIP_DD):
        guard.update(tripped=True, at=time.time(),
                     reason=f"{streak} losses in a row" if streak >= TRIP_STREAK else f"drawdown {dd:.0%}")
        notify(f"PERFORMANCE GUARD TRIPPED ({guard['reason']}): worse than 6 years of backtest. "
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
                         sl=float((p.get("stoploss") or {}).get("price") or 0) or None))
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
    caps = ex.caps_state(con, bot_eq, ex.unrealized_inr(con, positions, rate))

    for v in view:
        coin = v["symbol"].removesuffix("USDT")
        if not v["bot"] and coin in s1.BASKET and v["id"] not in st.setdefault("warned_manual", []):
            st["warned_manual"].append(v["id"])
            notify(f"Manual {v['side']} on {coin} (an S1 coin): the bot will not trade {coin} while it is open.")
        if v["bot"] and not v["sl"] and v["id"] not in st.setdefault("warned_sl", []):
            st["warned_sl"].append(v["id"])
            notify(f"{coin}: bot position has NO stop-loss on Mudrex. Add one in the app now.")

    if caps["hit"] and st.get("cap_day") != caps["day"]:
        st["cap_day"] = caps["day"]
        bot_open = [v for v in view if v["bot"]]
        msg = (f"Daily {caps['hit']} cap hit: bot Rs {caps['pnl']:+,.0f} today (cap {caps['cap']:,.0f}). "
               f"No new entries today.")
        if bot_open:
            pid = ex.record_plan(con, caps["day"], [dict(coin=v["symbol"].removesuffix("USDT"), action="CLOSE",
                                                         position_id=v["id"]) for v in bot_open], {"reason": "cap"})
            notify(msg + f" Close all {len(bot_open)} bot position(s)?",
                   buttons=[[("Close all", f"approve:{pid}"), ("Keep", f"reject:{pid}")]])
        else:
            notify(msg)
    elif caps["pnl"] <= -0.8 * caps["cap"] and st.get("warned_80") != caps["day"]:
        st["warned_80"] = caps["day"]
        notify(f"Warning: bot down Rs {-caps['pnl']:,.0f} today (loss cap Rs {caps['cap']:,.0f}).")

    guard = journal_and_guard(st, con, client, sum(v["upnl_inr"] for v in view if v["bot"]))
    if make_plan is None:
        import live_trader
        make_plan = lambda: live_trader.plan(client, con)   # noqa: E731
    maybe_plan(st, make_plan)

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
