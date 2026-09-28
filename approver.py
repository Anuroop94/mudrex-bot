"""Telegram approvals for S1. Places orders ONLY when YOU tap Approve on a specific plan.

Buttons (sent by watcher.py with each daily plan):  Approve -> runs that plan via execution.execute();
Reject -> marks it rejected. Commands:
  /status  bot equity, today's P&L vs caps, positions, guard, STOP state
  /stop    kill switch: creates the STOP file (blocks every order immediately)
  /resume  NOT available remotely; resume on the PC:  python ops.py resume
Security: telegram_bot.authorized() requires a private chat == TELEGRAM_CHAT_ID and sender == TELEGRAM_USER_ID.
The approval claims the plan under the execution journal's exclusive lock, so a terminal YES and a Telegram tap
(or two taps) can never both execute. Writes approver_heartbeat.json every loop for the watcher's stale check.
Run: pythonw approver.py   (MudrexApprover scheduled task, at logon) - only after you have reviewed README.md.
"""
import json
import os
import time
import traceback

import config  # noqa: F401  (loads .env)
import execution as ex
import telegram_bot as tg

HERE = os.path.dirname(os.path.abspath(__file__))
OFFSET_PATH = os.path.join(HERE, "approver_offset.json")
HEARTBEAT_PATH = os.path.join(HERE, "approver_heartbeat.json")
LOG_PATH = os.path.join(HERE, "approver.log")
RECONCILE_EVERY = 300


def log(msg):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + config.IST_OFFSET)) + f" IST  {msg}\n")


def status_text():
    path = os.path.join(HERE, "watch_status.json")
    if not os.path.exists(path):
        return "watcher has not reported yet"
    with open(path) as f:
        s = json.load(f)
    pos = "\n".join(f"  {p['symbol']} {p['side']} P&L Rs {p['upnl_inr']:+,.0f} "
                    f"SL {p['sl'] or 'NONE'} TP {p.get('tp') or 'NONE'}"
                    f"{'' if p.get('bot') else ' (manual)'}" for p in s.get("positions", [])) or "  none"
    b = s.get("bot", {})
    return (f"Bot equity Rs {b.get('equity', 0):,.0f} | today {b.get('day_pnl', 0):+,.0f} (caps +/-{b.get('cap', 0):,.0f})"
            f"\nCap hit: {b.get('cap_hit') or 'no'} | guard: {'TRIPPED' if s.get('guard', {}).get('tripped') else 'ok'}"
            f" | STOP: {'ON' if os.path.exists(ex.STOP_PATH) else 'off'} | live enabled: {ex.live_enabled()}"
            f"\nPositions:\n{pos}")


def tg_alert(msg):
    """Execution alerts: an undelivered Telegram message raises, so execution.event() journals it as alert_failed."""
    if tg.send(msg) is None:
        raise ConnectionError("Telegram message not delivered")
    return True


def fresh_plan():
    """A new plan at current prices (read-only on the exchange), sent with its own Approve/Reject buttons."""
    import live_trader
    p = live_trader.plan(__import__("mudrex_client").Client(), ex.db())
    tg.send(*tg.plan_message(p))
    return p


def handle(u, run=None, replan=None):
    """Process one Telegram update. run(plan_id, approver) executes a plan; replan() makes and sends a fresh
    plan (both injectable for tests). Approving an expired buy plan never trades: it only sends a fresh plan."""
    if not tg.authorized(u):
        return "ignored"
    default_run = run is None
    run = run or (lambda pid, who: ex.execute(ex.db(), __import__("mudrex_client").Client(), pid, who,
                                              alert=tg_alert))
    replan = replan or fresh_plan
    cq, msg = u.get("callback_query"), u.get("message")
    if cq:
        action, _, plan_id = (cq.get("data") or "").partition(":")
        m = cq["message"]
        if not plan_id.isdigit():
            return "ignored"
        who = f"telegram user {cq['from']['id']}"
        if action == "reject":
            con = ex.db()
            con.execute("UPDATE plans SET state='FAILED', note='rejected by user' WHERE id=? AND state='PLANNED'",
                        (int(plan_id),))
            tg.answer(cq["id"], "rejected")
            tg.edit(m["chat"]["id"], m["message_id"], m.get("text", "") + "\n\nREJECTED. Nothing placed.")
            log(f"plan {plan_id} rejected by {who}")
            return "rejected"
        if action != "approve":
            return "ignored"
        tg.answer(cq["id"], "checking and placing...")
        if default_run:
            con = ex.db()
            if ex.plan_requires_set_approval(con, int(plan_id)):
                why = ex.journal_set_approval(con, int(plan_id), who)
                if why:
                    tg.edit(m["chat"]["id"], m["message_id"], m.get("text", "") + f"\n\nREFUSED: {why}")
                    return "REFUSED"
        final, summary = run(int(plan_id), who)
        tg.edit(m["chat"]["id"], m["message_id"], m.get("text", "") + f"\n\n{final}:\n" + "\n".join(summary))
        log(f"plan {plan_id} approve by {who}: {final}")
        if final == "REFUSED" and summary and str(summary[0]).startswith("EXPIRED"):
            row = ex.db().execute("SELECT payload FROM plans WHERE id=?", (int(plan_id),)).fetchone()
            if row and json.loads(row["payload"] or "{}").get("reason") == "cap":
                tg.send("That Close-all plan expired. The watcher sends a fresh Close-all within 5 minutes.")
                return "cap expired"                   # the watcher re-offers the cap close (never a strategy plan)
            replan()                                   # new prices, new plan id, new buttons: tap again to trade
            log(f"plan {plan_id} expired; fresh plan sent")
            return "replanned"
        return final
    text = (msg.get("text") or "").strip().lower()
    if text == "/status":
        tg.send(status_text())
    elif text == "/stop":
        open(ex.STOP_PATH, "w").close()
        log("STOP file created via Telegram")
        tg.send("Kill switch ON: no orders will be placed. Stop-losses on Mudrex stay active.\n"
                "To resume, on the PC run:  python ops.py resume")
    elif text == "/resume":
        tg.send("Resume is only allowed on the PC:  python ops.py resume")
    elif text == "/plan":
        replan()
    elif text in ("/start", "/help"):
        tg.send("Mudrex S1 bot. Daily plans arrive with Approve/Reject buttons.\n"
                "/status  /plan (fresh plan at current prices)  /stop")
    return "handled"


def main():
    offset = 0
    if os.path.exists(OFFSET_PATH):
        with open(OFFSET_PATH) as f:
            offset = json.load(f)["offset"]
    log("approver started" + ("" if tg.enabled() else " (Telegram not fully configured: idle)"))
    warned_watcher, started, reconciled = 0, time.time(), 0
    while True:
        if time.time() - reconciled > RECONCILE_EVERY:
            reconciled = time.time()
            try:   # finishes orders of ALREADY-APPROVED plans after a crash (attach stops, verify); never submits
                ex.reconcile(ex.db(), __import__("mudrex_client").Client(), alert=tg_alert)
            except Exception:
                log("reconcile error:\n" + traceback.format_exc())
        with open(HEARTBEAT_PATH, "w") as f:
            json.dump({"at": time.time()}, f)
        ws = os.path.join(HERE, "watch_status.json")
        if not os.path.exists(ws) and time.time() - started > 900 and warned_watcher != -1:
            warned_watcher = -1
            tg.send("Watcher is not running (no status file): no daily plans or cap alerts will arrive.")
        elif os.path.exists(ws):
            with open(ws) as f:
                at = json.load(f).get("at", 0)
            if time.time() - at > 900 and warned_watcher < at:
                warned_watcher = at
                tg.send("Watcher looks stopped (no heartbeat for 15 min): no daily plans or cap alerts until it restarts.")
        if not tg.enabled():
            time.sleep(60)
            continue
        try:
            for u in tg.updates(offset, timeout=30):
                handle(u)
                offset = u["update_id"] + 1
                with open(OFFSET_PATH, "w") as f:
                    json.dump({"offset": offset}, f)   # save after idempotent handling so crashes replay safely
        except Exception:
            log("error:\n" + traceback.format_exc())
            time.sleep(10)


if __name__ == "__main__":
    main()
