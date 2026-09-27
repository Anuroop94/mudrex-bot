"""Minimal Telegram Bot API client (stdlib). Reads TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_USER_ID from .env.

Setup (you do this once):
  1. In Telegram, message @BotFather -> /newbot -> put the token in .env as TELEGRAM_BOT_TOKEN=...
  2. Send /start to your new bot from your own account (a private chat, not a group).
  3. Run  python telegram_bot.py setup  -> prints your chat id and user id; add both to .env as
     TELEGRAM_CHAT_ID=...  and  TELEGRAM_USER_ID=...
Requests are POSTs with a JSON body. The token is part of the URL path because the Telegram API requires it;
it is never logged. Only a private chat whose chat id AND sender id match .env is ever accepted.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import config  # noqa: F401  (loads .env)
import trade_policy


def enabled():
    return all(os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_USER_ID"))


def call(method, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", method="POST",
                                 data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = json.load(r)
        return body.get("result") if body.get("ok") else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def send(text, buttons=None):
    """Send to the configured chat. buttons: [[(label, callback_data), ...], ...] -> inline keyboard."""
    if not enabled():
        return None
    kw = dict(chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text[:4000])
    if buttons:
        kw["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
    return call("sendMessage", **kw)


def plan_message(p):
    """Render every plan update; first-three qualified sets are informational, not approval-gated."""
    todo = [o for o in p["orders"] if o["action"] in ("OPEN", "CLOSE")]
    if not todo:
        return "S1: no orders today." + (f" New entries blocked: {p['blocked']}." if p.get("blocked") else ""), None
    if not (p.get("plan_id") and p.get("live_enabled")):
        return f"S1 plan {p.get('plan_id')}: {len(todo)} order(s), but LIVE_TRADING_ENABLED is false.", None
    opens = [o for o in todo if o["action"] == "OPEN"]
    lines = [f"{o['action']} {o['coin']}" + (f" {o.get('side', 'LONG')} {o.get('leverage', 0):g}x"
                                             f" ~Rs {o['notional_inr']:,.0f}, SL {o['est_stop']},"
                                             f" TP {o.get('est_target')}, risk Rs {o.get('planned_risk_inr', 0):,.0f}"
                                             if o["action"] == "OPEN" else "") for o in todo]
    risk = sum(o.get("planned_risk_inr", 0) for o in opens)
    text = (f"S1 plan {p['plan_id']}: {len(todo)} order(s)\n" + "\n".join(lines) +
            (f"\nPlanned collective reserve for these sets: Rs {risk:,.0f}; cycle hard cap Rs {trade_policy.DAILY_LOSS_LIMIT_INR:,.0f}."
             f"\nValid 15 min." if opens else
             "\nValid 3 hours."))
    if p.get("blocked"):
        return text + f"\nBLOCKED: {p['blocked']}. Nothing will be placed.", None
    if opens and p.get("attempted_sets", 0) < trade_policy.AUTONOMOUS_SETS_PER_CYCLE:
        return (text + f"\nThese qualified sets are autonomous (sets 1-{trade_policy.AUTONOMOUS_SETS_PER_CYCLE}); "
                "no approval tap is required.", None)
    return text, [[("Approve", f"approve:{p['plan_id']}"), ("Reject", f"reject:{p['plan_id']}")]]


def updates(offset, timeout=0):
    return call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["callback_query", "message"]) or []


def answer(callback_id, text):
    call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])


def edit(chat_id, message_id, text):
    call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000])


def authorized(update):
    """True only for a private chat == TELEGRAM_CHAT_ID from sender == TELEGRAM_USER_ID."""
    chat_id, user_id = os.environ.get("TELEGRAM_CHAT_ID", ""), os.environ.get("TELEGRAM_USER_ID", "")
    if not (chat_id and user_id):
        return False
    cq, msg = update.get("callback_query"), update.get("message")
    if cq:
        chat, sender = (cq.get("message") or {}).get("chat", {}), cq.get("from", {})
    elif msg:
        chat, sender = msg.get("chat", {}), msg.get("from", {})
    else:
        return False
    return (str(chat.get("id")) == chat_id and chat.get("type") == "private"
            and str(sender.get("id")) == user_id and not sender.get("is_bot"))


if __name__ == "__main__" and sys.argv[1:] == ["setup"]:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        sys.exit("put TELEGRAM_BOT_TOKEN=... in .env first (from @BotFather)")
    seen = {((u.get("message") or {}).get("chat", {}).get("id"), (u.get("message") or {}).get("from", {}).get("id"),
             (u.get("message") or {}).get("chat", {}).get("type")) for u in updates(0) if u.get("message")}
    if not seen:
        print("none yet: send /start to your bot from your own account, then run this again")
    for chat, user, kind in seen:
        print(f"chat id {chat} ({kind}), user id {user}")
