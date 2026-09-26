"""Telegram approvals for S1. Places orders ONLY when you tap Approve on today's plan in your own chat.

Buttons (sent by watcher.py with each daily plan):  Approve -> places that plan;  Reject -> discards it.
Commands you can send the bot:
  /status  equity, today's P&L vs caps, open positions, guard state
  /stop    kill switch: creates the STOP file, blocks all order placing until /resume
  /resume  removes the STOP file
Security: only updates from TELEGRAM_CHAT_ID are processed; everything else is ignored. An approval is bound to
one specific plan (its id), which must be unexecuted, under 3 hours old, and pass every live_trader check.
Run: pythonw approver.py   (MudrexApprover scheduled task, at logon)
"""
import json
import os
import time
import traceback

import config  # noqa: F401  (loads .env)
import live_trader as lt
import telegram_bot as tg

HERE = os.path.dirname(os.path.abspath(__file__))
OFFSET_PATH = os.path.join(HERE, "approver_offset.json")
LOG_PATH = os.path.join(HERE, "approver.log")


def log(msg):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + config.IST_OFFSET)) + f" IST  {msg}\n")


def status_text():
    path = os.path.join(HERE, "watch_status.json")
    if not os.path.exists(path):
        return "watcher has not reported yet"
    with open(path) as f:
        s = json.load(f)
    pos = "\n".join(f"  {p['symbol']} {p['side']} P&L Rs {p['upnl_inr']:+,.0f} stop {p['sl'] or 'NONE'}"
                    for p in s.get("positions", [])) or "  none"
    g = s.get("guard", {})
    return (f"Equity Rs {s['equity_inr']:,.0f} | today {s['day_pnl']:+,.0f} "
            f"(caps +/-{s['loss_cap']:,})\nCap hit: {s.get('cap_hit') or 'no'} | guard: "
            f"{'TRIPPED' if g.get('tripped') else 'ok'} | STOP file: {'yes' if os.path.exists(lt.STOP_PATH) else 'no'}\n"
            f"Positions:\n{pos}")


def handle(u, chat_id):
    cq = u.get("callback_query")
    msg = u.get("message")
    if cq:
        if str((cq.get("message") or {}).get("chat", {}).get("id")) != chat_id:
            return
        action, _, plan_id = (cq.get("data") or "").partition(":")
        if not os.path.exists(lt.PLAN_PATH):
            tg.answer(cq["id"], "no plan found")
            return
        with open(lt.PLAN_PATH) as f:
            p = json.load(f)
        m = cq["message"]
        if action == "reject":
            if str(p["created_at"]) == plan_id and not p["executed"]:
                p["executed"], p["rejected"] = True, True
                lt.write_json(lt.PLAN_PATH, p)
            tg.answer(cq["id"], "rejected")
            tg.edit(m["chat"]["id"], m["message_id"], m.get("text", "") + "\n\nREJECTED. Nothing placed.")
            log(f"plan {plan_id} rejected via Telegram")
            return
        if action != "approve":
            return
        why = lt.check_plan(p, plan_id)
        if why:
            tg.answer(cq["id"], f"not placed: {why}")
            tg.edit(m["chat"]["id"], m["message_id"], m.get("text", "") + f"\n\nNOT PLACED: {why}")
            log(f"approval for {plan_id} refused: {why}")
            return
        tg.answer(cq["id"], "placing orders...")
        todo = [o for o in p["orders"] if o["action"] in ("OPEN", "CLOSE")]
        results = lt.place(p, todo, approved_by=f"telegram chat {chat_id}")
        tg.edit(m["chat"]["id"], m["message_id"], m.get("text", "") + "\n\nAPPROVED. Results:\n" + "\n".join(results))
        return
    if msg and str(msg.get("chat", {}).get("id")) == chat_id:
        text = (msg.get("text") or "").strip().lower()
        if text == "/status":
            tg.send(status_text())
        elif text == "/stop":
            open(lt.STOP_PATH, "w").close()
            log("STOP file created via Telegram")
            tg.send("Kill switch ON: no orders will be placed until /resume. Stop-losses on Mudrex stay active.")
        elif text == "/resume":
            if os.path.exists(lt.STOP_PATH):
                os.remove(lt.STOP_PATH)
            log("STOP file removed via Telegram")
            tg.send("Kill switch OFF: approvals work again.")
        elif text in ("/start", "/help"):
            tg.send("Mudrex S1 bot. Daily plans arrive with Approve/Reject buttons.\n/status  /stop  /resume")


def main():
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    offset = 0
    if os.path.exists(OFFSET_PATH):
        with open(OFFSET_PATH) as f:
            offset = json.load(f)["offset"]
    log("approver started" + ("" if tg.enabled() else " (Telegram not configured yet; idle)"))
    while True:
        if not tg.enabled():
            time.sleep(60)
            continue
        try:
            for u in tg.updates(offset, timeout=30):
                offset = u["update_id"] + 1
                with open(OFFSET_PATH, "w") as f:
                    json.dump({"offset": offset}, f)   # saved before handling: a crash never replays an approval
                handle(u, chat_id)
        except Exception:
            log("error:\n" + traceback.format_exc())
            time.sleep(10)


if __name__ == "__main__":
    main()
