"""Minimal Telegram Bot API client (stdlib). Reads TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_USER_ID from .env.

Setup (you do this once):
  1. In Telegram, message @BotFather -> /newbot -> put the token in .env as TELEGRAM_BOT_TOKEN=...
  2. Send /start to your new bot from your own account (a private chat, not a group).
  3. Run  python telegram_bot.py setup  -> prints your chat id and user id; add both to .env as
     TELEGRAM_CHAT_ID=...  and  TELEGRAM_USER_ID=...
Requests are POSTs with a JSON body. The token is part of the URL path because the Telegram API requires it;
it is never logged. Only a private chat whose chat id AND sender id match .env is ever accepted.
"""
import html
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



# ---------- message style: every message is plain text; render() turns it into Telegram HTML at send time,
# so a message stored in the durable outbox keeps its style and no text can break delivery.

ICONS = (("kill switch", "🛑"), ("stop file", "🛑"), ("guard tripped", "🚨"), ("unprotected", "🚨"),
         ("no stop-loss", "🚨"), ("warning", "⚠️"), ("cannot", "⚠️"), ("not running", "⚠️"), ("stopped", "⚠️"),
         ("not confirmed", "⚠️"), ("failed", "❌"), ("refused", "❌"), ("closed", "✅"), ("finished", "✅"),
         ("started", "🟢"))
EMOJI_START = ("🟢", "🔴", "🟡", "👀", "⛔", "💤", "🔒", "📊", "🛑", "🚨", "⚠️", "❌", "✅", "🤖", "🔔", "ℹ️")


def render(text):
    """Plain text -> Telegram HTML. First line = bold title (an icon is added if it has none); lines between ```
    fences = monospace block, so numbers line up. Everything is HTML-escaped."""
    lines = str(text).split("\n")
    if lines and not lines[0].startswith(EMOJI_START) and lines[0].strip() != "```":
        low = lines[0].lower()
        lines[0] = next((i for k, i in ICONS if k in low), "🔔") + " " + lines[0]
    out, pre = [], False
    for n, line in enumerate(lines):
        if line.strip() == "```":
            out.append("</pre>" if pre else "<pre>")
            pre = not pre
            continue
        e = html.escape(line, quote=False)
        out.append(f"<b>{e}</b>" if n == 0 and not pre else e)
    if pre:
        out.append("</pre>")
    return "\n".join(out).replace("<pre>\n", "<pre>").replace("\n</pre>", "</pre>")

def send(text, buttons=None):
    """Send to the configured chat. buttons: [[(label, callback_data), ...], ...] -> inline keyboard."""
    if not enabled():
        return None
    kw = dict(chat_id=os.environ["TELEGRAM_CHAT_ID"], text=render(str(text)[:3500]), parse_mode="HTML")
    if buttons:
        kw["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
    return call("sendMessage", **kw)


def _tag(p):
    return str(p.get("strategy") or "S1").split()[0]


def _px(x):
    try:
        return f"{float(x):,.6g}"
    except (TypeError, ValueError):
        return "?"


def _pct(a, b):
    try:
        return f"{(float(b) / float(a) - 1) * 100:+.1f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def _order_block(o):
    """Aligned card for one OPEN, or one line for a CLOSE."""
    if o["action"] == "CLOSE":
        return [f"🔒 Close {o['coin']} — {o.get('reason', '')}"]
    side = o.get("side", "LONG")
    entry = o.get("planned_price")
    qty = o.get("qty")
    size = f"{qty:g} {o['coin']} = ₹{o['notional_inr']:,.0f}" if qty else f"₹{o['notional_inr']:,.0f}"
    return ["```",
            f"Coin    {o['coin']}  {side}  {o.get('leverage', 1):g}x",
            f"Entry   {_px(entry)}",
            f"Target  {_px(o.get('est_target')):<12} {_pct(entry, o.get('est_target'))}",
            f"Stop    {_px(o.get('est_stop')):<12} {_pct(entry, o.get('est_stop'))}",
            f"Size    {size}",
            f"Risk    ₹{o.get('planned_risk_inr', 0):,.0f}",
            "```"]


def plan_message(p):
    """(text, buttons) for a plan. Plain text with ``` blocks; render() styles it when it is sent."""
    tag = _tag(p)
    todo = [o for o in p["orders"] if o["action"] in ("OPEN", "CLOSE")]
    if not todo:
        return (f"💤 {tag} · nothing to do now" +
                (f"\n⛔ New trades blocked: {p['blocked']}" if p.get("blocked") else "")), None
    if not (p.get("plan_id") and p.get("live_enabled")):
        return (f"⚠️ {tag} plan {p.get('plan_id')}: {len(todo)} order(s)\nLIVE_TRADING_ENABLED is false: "
                "nothing can be placed."), None
    opens = [o for o in todo if o["action"] == "OPEN"]
    blocked = p.get("blocked")
    first = opens[0] if opens else None
    side_icon = "🔴" if first and first.get("side") == "SHORT" else "🟢"
    if not opens:
        title = f"🔒 {tag} · closing"
    elif blocked and "STOP" in str(blocked):
        title = f"👀 {tag} DRY RUN · would trade" + (f" {first['coin']} {first.get('side', 'LONG')}" if first else "")
    elif blocked:
        title = f"⛔ {tag} · set blocked"
    elif p.get("needs_approval"):
        title = f"🟡 {tag} · extra set needs your OK"
    else:
        title = f"{side_icon} {tag} · new set" + (f" · {first['coin']} {first.get('side', 'LONG')}" if first else "")
    lines = [title, f"Plan {p['plan_id']}"]
    for o in todo:
        lines += _order_block(o)
    risk = sum(o.get("planned_risk_inr", 0) for o in opens)
    if opens:
        lines.append(f"🧮 Risk reserved ₹{risk:,.0f} · daily cap ₹{trade_policy.DAILY_LOSS_LIMIT_INR:,.0f}")
    if not opens and trade_policy.migration_block_reason() is None:
        return "\n".join(lines + ["🤖 Risk-reducing close: runs automatically (no tap needed)."]), None
    if blocked and opens:
        return "\n".join(lines + [f"⛔ Blocked: {blocked}", "Nothing will be placed."]), None
    if opens and not p.get("needs_approval"):
        return "\n".join(lines + [f"🤖 Automatic (sets 1-{trade_policy.AUTONOMOUS_SETS_PER_CYCLE}): "
                                   "no tap needed."]), None
    lines.append("👆 Tap Approve within 15 min." if opens else "👆 Tap Approve to close (valid 3 hours).")
    return "\n".join(lines), [[("✅ Approve", f"approve:{p['plan_id']}"), ("✖️ Reject", f"reject:{p['plan_id']}")]]


def updates(offset, timeout=0):
    return call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["callback_query", "message"]) or []


def answer(callback_id, text):
    call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])


def edit(chat_id, message_id, text):
    call("editMessageText", chat_id=chat_id, message_id=message_id, text=render(str(text)[:3500]), parse_mode="HTML")


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
