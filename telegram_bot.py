"""Minimal Telegram Bot API client (stdlib). Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env.

Setup (you do this once):
  1. In Telegram, message @BotFather -> /newbot -> copy the token into .env as TELEGRAM_BOT_TOKEN=...
  2. Send any message (e.g. /start) to your new bot.
  3. Run  python telegram_bot.py setup   -> it prints your chat id; put it in .env as TELEGRAM_CHAT_ID=...
Only messages and button taps from TELEGRAM_CHAT_ID are ever accepted.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import config  # noqa: F401  (loads .env)


def enabled():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def call(method, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    q = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()})
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}?{q}", timeout=40) as r:
            body = json.load(r)
        return body.get("result") if body.get("ok") else None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def send(text, buttons=None):
    """Send to the configured chat. buttons: [[(label, callback_data), ...], ...] -> inline keyboard."""
    if not enabled():
        return None
    kw = dict(chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text[:4000])
    if buttons:
        kw["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
    return call("sendMessage", **kw)


def updates(offset, timeout=0):
    return call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["callback_query", "message"]) or []


def answer(callback_id, text):
    call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])


def edit(chat_id, message_id, text):
    call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000])


if __name__ == "__main__" and sys.argv[1:] == ["setup"]:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        sys.exit("put TELEGRAM_BOT_TOKEN=... in .env first (from @BotFather)")
    seen = {(u.get("message") or {}).get("chat", {}).get("id") for u in updates(0)}
    seen.discard(None)
    print("chat ids that messaged your bot:", seen or "none yet: send /start to your bot, then run this again")
