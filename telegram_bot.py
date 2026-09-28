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
EMOJI_START = ("🟢", "🔴", "🟡", "👀", "⛔", "💤", "🔒", "📊", "🛑", "🚨", "⚠️", "❌", "✅", "🤖", "🔔", "ℹ️",
               "⚡", "▶️", "🔌", "💰")


def render(text):
    """Plain text -> Telegram HTML. The first line is the bold headline (an icon is added if it has none); a line
    written as *text* is bold too; lines between ``` fences are monospace. Everything is HTML-escaped."""
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
        bold = not pre and (n == 0 or (len(line) > 2 and line.startswith("*") and line.endswith("*")))
        if bold and n:
            line = line[1:-1]
        e = html.escape(line, quote=False)
        out.append(f"<b>{e}</b>" if bold else e)
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


def _order_lines(o):
    """Clean trade card for one OPEN/PREVIEW, or one line for a CLOSE."""
    if o["action"] == "CLOSE":
        return [f"🔒 *CLOSE {o['coin']}*", f"📝 {o.get('reason', '')}"]
    short = o.get("side") == "SHORT"
    entry, qty = o.get("planned_price"), o.get("qty")
    size = f"{qty:g} {o['coin']} · ₹{o['notional_inr']:,.0f}" if qty else f"₹{o['notional_inr']:,.0f}"
    return [f"*{'🔴 SELL (short)' if short else '🟢 BUY'} {o['coin']} near {_px(entry)}*",
            "",
            f"🎯 Target  {_px(o.get('est_target'))}  ({_pct(entry, o.get('est_target'))})",
            f"🛑 SL  {_px(o.get('est_stop'))}  ({_pct(entry, o.get('est_stop'))})",
            "",
            f"📦 Size  {size} · {o.get('leverage', 1):g}x",
            f"⚖️ Risk  ₹{o.get('planned_risk_inr', 0):,.0f} (incl. fees)"]


def _limit_line(opens):
    risk = sum(o.get("planned_risk_inr", 0) for o in opens)
    return f"🧮 Reserved ₹{risk:,.0f} of today's ₹{trade_policy.DAILY_LOSS_LIMIT_INR:,.0f} limit"


def _blocked_line(blocked):
    return ("⛔ Not placed — STOP is on (dry run)" if "STOP" in str(blocked) else f"⛔ Not placed — {blocked}")


def plan_message(p):
    """(text, buttons) for a plan: clean lines with emojis; render() styles it when it is sent."""
    tag = _tag(p)
    todo = [o for o in p["orders"] if o["action"] in ("OPEN", "CLOSE")]
    previews = [o for o in p["orders"] if o["action"] == "PREVIEW"]
    if not todo and previews:                              # blocked (e.g. STOP = dry run): what it WOULD trade
        lines = [f"👀 DRY RUN · {tag} would trade", ""]
        for o in previews:
            lines += _order_lines(o) + [""]
        return "\n".join(lines + [_blocked_line(p.get("blocked"))]), None
    if not todo:
        return (f"💤 {tag} · no trade right now" +
                (f"\n\n⛔ New trades blocked — {p['blocked']}" if p.get("blocked") else "")), None
    if not (p.get("plan_id") and p.get("live_enabled")):
        return (f"⚠️ {tag} · plan {p.get('plan_id')} not placed\n\nLIVE_TRADING_ENABLED is false: "
                "nothing can be placed."), None
    opens = [o for o in todo if o["action"] == "OPEN"]
    blocked = p.get("blocked")
    if not opens:
        title = f"🔒 {tag} · CLOSING A TRADE"
    elif blocked and "STOP" in str(blocked):
        title = f"👀 DRY RUN · {tag} would trade"
    elif blocked:
        title = f"⛔ {tag} · TRADE BLOCKED"
    elif p.get("needs_approval"):
        title = f"🟡 {tag} · EXTRA TRADE NEEDS YOUR OK"
    else:
        title = f"⚡ {tag} · NEW TRADE"
    lines = [title, ""]
    for o in todo:
        lines += _order_lines(o) + [""]
    if opens:
        lines.append(_limit_line(opens))
    if not opens and trade_policy.migration_block_reason() is None:
        return "\n".join(lines + ["🤖 Risk-reducing close: runs automatically"]), None
    if blocked and opens:
        return "\n".join(lines + [_blocked_line(blocked)]), None
    if opens and not p.get("needs_approval"):
        return "\n".join(lines + [f"🤖 Placing automatically (sets 1-{trade_policy.AUTONOMOUS_SETS_PER_CYCLE} "
                                   "need no tap)"]), None
    lines.append("👆 Tap Approve within 15 min" if opens else "👆 Tap Approve to close (valid 3 hours)")
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
