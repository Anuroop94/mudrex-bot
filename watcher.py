"""24/7 READ-ONLY watcher. Never places, changes or closes orders (no order endpoints in this file).

Every CHECK_SEC it reads your Mudrex INR futures wallet and positions and:
  - tracks today's P&L (IST day) against the caps: +/-5% of the day's starting equity (s1.DAILY_CAP_PCT).
    When either is hit it writes caps.json; live_trader.py plan then allows exits only for the rest of the day.
  - notices positions that closed (stop-loss hit or manual) and reports their result
  - warns if an S1 position has no stop-loss on the exchange
  - after each daily close (05:30 IST) runs the read-only S1 plan and tells you how many orders are waiting
It shows a Windows notification for anything you should act on, and writes watch_status.json for the dashboard.

Run: pythonw watcher.py   (started automatically at Windows logon by the MudrexWatcher scheduled task)
ponytail: day P&L = equity now - equity at first check of the IST day, so a deposit/withdrawal during the day
looks like profit/loss. Deposit before 00:00 IST or restart the day by deleting watch_state.json.
"""
import json
import os
import subprocess
import time
import traceback
import urllib.error
import urllib.request

import config
import pick_coins
import s1

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "watch_state.json")
STATUS_PATH = os.path.join(HERE, "watch_status.json")
CAPS_PATH = os.path.join(HERE, "caps.json")
LOG_PATH = os.path.join(HERE, "watcher.log")
CHECK_SEC = 300
DAY = 86400
API = "https://trade.mudrex.com/fapi/v1"


def ist_now():
    return time.time() + config.IST_OFFSET


def ist_str(fmt="%Y-%m-%d %H:%M"):
    return time.strftime(fmt, time.gmtime(ist_now()))


def log(msg):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{ist_str()} IST  {msg}\n")


def notify(msg, buttons=None):
    """Telegram message (if configured) + Windows balloon notification (fire-and-forget) + log line."""
    log(f"NOTIFY {msg}")
    import telegram_bot
    telegram_bot.send(msg, buttons)
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


def get(path):
    req = urllib.request.Request(API + path, headers={"X-Authentication": os.environ["MUDREX_API_SECRET"]})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.load(r)
    if not body.get("success"):
        raise RuntimeError(f"{path}: {body.get('errors')}")
    return body["data"]


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


def check(st):
    funds = get("/futures/funds?trade_currency=INR")
    positions = get("/futures/positions?trade_currency=INR") or []
    prices = {r["symbol"]: float(r["price"]) for r in pick_coins.listing()}
    upnl, pos_view = 0.0, {}
    for p in positions:
        sym, q, e = p["symbol"], float(p["quantity"]), float(p["entry_price"])
        side = 1 if p["order_type"] == "LONG" else -1
        hr = float(p.get("entry_hedge_rate") or config.INR_PER_USDT)
        px = prices.get(sym, e)
        u = side * q * (px - e) * hr
        upnl += u
        sl = float((p.get("stoploss") or {}).get("price") or 0)
        pos_view[p["id"]] = dict(symbol=sym, side=p["order_type"], qty=q, entry=e, price=px, sl=sl or None,
                                 upnl_inr=round(u, 2), bot=sym.removesuffix("USDT") in s1.BASKET and side == 1)
    equity = float(funds["balance"]) + float(funds["locked_amount"]) + upnl

    today = ist_str("%Y-%m-%d")
    if st.get("day") != today:                      # new IST day: reset caps
        st.update(day=today, day_start_equity=equity, cap_hit=None)
        save(CAPS_PATH, dict(day=today, cap_hit=None))
        log(f"new day {today}: start equity Rs {equity:,.2f}")
    day_pnl = equity - st["day_start_equity"]

    if not st.get("cap_hit"):
        cap = s1.daily_cap_inr(st["day_start_equity"])
        hit = ("loss" if day_pnl <= -cap else "profit" if day_pnl >= cap else None)
        if hit:
            st["cap_hit"] = hit
            save(CAPS_PATH, dict(day=today, cap_hit=hit, day_pnl=round(day_pnl, 2)))
            notify(f"Daily {hit} cap hit: Rs {day_pnl:+,.0f} today. No new trades until tomorrow. "
                   f"Open positions keep their stop-losses.")
        elif day_pnl <= -0.8 * cap and st.get("warned_80") != today:
            st["warned_80"] = today
            notify(f"Warning: down Rs {-day_pnl:,.0f} today (loss cap Rs {cap:,.0f}).")

    prev = st.get("positions", {})
    for pid, p in prev.items():                     # closed since last check
        if pid not in pos_view:
            notify(f"Position closed: {p['symbol']} {p['side']} (last seen P&L Rs {p['upnl_inr']:+,.0f}). "
                   f"Check Mudrex for the final result.")
    for pid, p in pos_view.items():
        if p["bot"] and not p["sl"] and pid not in st.get("warned_sl", []):
            st.setdefault("warned_sl", []).append(pid)
            notify(f"{p['symbol']} has NO stop-loss on Mudrex. Add one in the app now.")
    st["positions"] = pos_view

    # after the daily close (05:30 IST) prepare the read-only S1 plan once per day
    if ist_str("%H:%M") >= "05:35" and st.get("plan_day") != today:
        st["plan_day"] = today
        import live_trader                           # read-only plan() only; the order step is never called here
        try:
            p = live_trader.plan()
            todo = [o for o in p["orders"] if o["action"] in ("OPEN", "CLOSE")]
            if todo:
                lines = [f"{o['action']} {o['coin']}" + (f" ~Rs {o['notional_inr']:,} stop {o['stop']}"
                                                         if o["action"] == "OPEN" else "") for o in todo]
                notify(f"S1: {len(todo)} order(s) ready for today (expires in 3h):\n" + "\n".join(lines),
                       buttons=[[("Approve", f"approve:{p['created_at']}"), ("Reject", f"reject:{p['created_at']}")]])
            else:
                notify("S1: no orders today. Positions unchanged.")
        except SystemExit as e:
            log(f"plan failed: {e}")

    journal_and_guard(st, pos_view)
    save(STATUS_PATH, dict(at=time.time(), ok=True, equity_inr=round(equity, 2),
                           guard=load(GUARD_PATH, {}), journal=journal_summary(),
                           day=today, day_start_equity=round(st["day_start_equity"], 2), day_pnl=round(day_pnl, 2),
                           profit_cap=round(s1.daily_cap_inr(st["day_start_equity"])),
                           loss_cap=round(s1.daily_cap_inr(st["day_start_equity"])), cap_hit=st.get("cap_hit"),
                           positions=list(pos_view.values()), check_every_sec=CHECK_SEC))


# ---------- trade journal + performance guard (features 2 and 4)

GUARD_PATH = os.path.join(HERE, "guard.json")
JOURNAL_PATH = os.path.join(HERE, "journal.csv")
JOURNAL_FIELDS = ["coin", "opened", "closed", "days", "entry", "exit", "pnl_inr", "outcome", "btc_mood", "signal"]
# limits from the S1 backtest (6 years, 127 trades): worst losing streak 10, worst drawdown 30.4%
WARN_STREAK, TRIP_STREAK = 10, 13
WARN_DD, TRIP_DD = 0.25, 0.35


def bot_position_ids():
    """Position ids opened by live_trader (its orders carry client_order_id 's1-...')."""
    orders = get("/futures/orders/history?trade_currency=INR&limit=100") or []
    return {o["future_position_uuid"] for o in orders
            if (o.get("client_order_id") or "").startswith("s1-") and o.get("future_position_uuid")}


def entry_context(coin, opened_ts):
    """S1's view on the entry day: BTC mood and the coin's trend signal (for learning which entries work)."""
    import data
    import portfolio as pf
    day = opened_ts // DAY * DAY - DAY
    btc = [x for x in data.load(2400, "BTC/USDT", "1d", DAY) if x[0] <= day]
    cs = [x for x in data.load(2400, f"{coin}/USDT", "1d", DAY) if x[0] <= day]
    sig = pf.zarattini(cs, **s1.SIGNAL_KW).get(day, 0.0) if cs else 0.0
    return ("good" if s1.btc_mood_ok(btc, day) else "bad"), round(sig, 3)


def journal_rows():
    if not os.path.exists(JOURNAL_PATH):
        return []
    import csv
    with open(JOURNAL_PATH, newline="") as f:
        return list(csv.DictReader(f))


def journal_and_guard(st, pos_view):
    import csv
    from datetime import datetime
    ids = bot_position_ids()
    st.setdefault("bot_ids", [])
    st["bot_ids"] = sorted(set(st["bot_ids"]) | ids)
    done = set(st.setdefault("journaled", []))
    hist = get("/futures/positions/history?trade_currency=INR&limit=100") or []
    ts = lambda s: int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    new = []
    for p in hist:
        if p["id"] not in st["bot_ids"] or p["id"] in done:
            continue
        coin, pnl = p["symbol"].removesuffix("USDT"), float(p["pnl"])
        o, c = ts(p["created_at"]), ts(p["updated_at"])
        mood, sig = entry_context(coin, o)
        new.append(dict(coin=coin, opened=time.strftime("%Y-%m-%d", time.gmtime(o)),
                        closed=time.strftime("%Y-%m-%d", time.gmtime(c)), days=round((c - o) / DAY, 1),
                        entry=p["entry_price"], exit=p["closed_price"], pnl_inr=round(pnl, 2),
                        outcome="win" if pnl > 0 else "loss", btc_mood=mood, signal=sig))
        done.add(p["id"])
    if new:
        exists = os.path.exists(JOURNAL_PATH)
        with open(JOURNAL_PATH, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if not exists:
                w.writeheader()
            w.writerows(sorted(new, key=lambda r: r["closed"]))
        for r in new:
            notify(f"S1 trade closed: {r['coin']} {r['outcome']} Rs {r['pnl_inr']:+,.0f} after {r['days']} days.")
    st["journaled"] = sorted(done)

    # performance guard on bot trades only
    rows = journal_rows()
    streak = 0
    for r in rows:
        streak = streak + 1 if float(r["pnl_inr"]) <= 0 else 0
    open_pnl = sum(p["upnl_inr"] for pid, p in pos_view.items() if pid in st["bot_ids"])
    bot_eq = s1.CAPITAL_CAP_INR + sum(float(r["pnl_inr"]) for r in rows) + open_pnl
    st["bot_peak"] = max(st.get("bot_peak", s1.CAPITAL_CAP_INR), bot_eq)
    dd = 1 - bot_eq / st["bot_peak"]
    guard = load(GUARD_PATH, dict(tripped=False))
    guard.update(streak=streak, drawdown=round(dd, 4), bot_equity_inr=round(bot_eq, 2), checked=time.time())
    if not guard.get("tripped") and (streak >= TRIP_STREAK or dd >= TRIP_DD):
        guard.update(tripped=True, reason=f"{streak} losses in a row" if streak >= TRIP_STREAK
                     else f"drawdown {dd:.0%} from peak", at=time.time())
        notify(f"PERFORMANCE GUARD TRIPPED ({guard['reason']}): live results are worse than 6 years of backtest. "
               f"S1 will only exit, no new buys, until you review.")
    elif not guard.get("tripped") and (streak >= WARN_STREAK or dd >= WARN_DD) and st.get("guard_warned") != today_str():
        st["guard_warned"] = today_str()
        notify(f"Guard warning: {streak} losses in a row, {dd:.0%} below peak. Still within backtest history.")
    save(GUARD_PATH, guard)

    # weekly lessons (Monday, once)
    if time.strftime("%a", time.gmtime(ist_now())) == "Mon" and st.get("weekly") != today_str() and rows:
        st["weekly"] = today_str()
        notify(weekly_summary(rows))


def today_str():
    return ist_str("%Y-%m-%d")


def journal_summary():
    rows = journal_rows()
    if not rows:
        return dict(trades=0)
    pnl = [float(r["pnl_inr"]) for r in rows]
    return dict(trades=len(rows), wins=sum(x > 0 for x in pnl), total_inr=round(sum(pnl), 2), last=rows[-5:])


def weekly_summary(rows):
    pnl = [float(r["pnl_inr"]) for r in rows]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    by_coin = {}
    for r in rows:
        by_coin[r["coin"]] = by_coin.get(r["coin"], 0) + float(r["pnl_inr"])
    good = [float(r["pnl_inr"]) for r in rows if r["btc_mood"] == "good"]
    return (f"Weekly S1 review: {len(rows)} trades, {len(wins)} wins ({len(wins) / len(rows):.0%}, backtest 36%), "
            f"total Rs {sum(pnl):+,.0f}. Avg win Rs {sum(wins) / max(len(wins), 1):+,.0f}, avg loss "
            f"Rs {sum(losses) / max(len(losses), 1):+,.0f}. Best coin {max(by_coin, key=by_coin.get)}, "
            f"worst {min(by_coin, key=by_coin.get)}. Trades in good BTC mood: {len(good)}.")


def main():
    st = load(STATE_PATH, {})
    log("watcher started")
    notify("Mudrex watcher started: checking every 5 minutes (read-only).")
    fails = 0
    while True:
        try:
            check(st)
            save(STATE_PATH, st)
            fails = 0
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError, ValueError, KeyError) as e:
            fails += 1
            log(f"check failed ({fails}): {type(e).__name__}: {e}")
            save(STATUS_PATH, dict(load(STATUS_PATH, {}), at_error=time.time(), ok=False, error=str(e)[:200]))
            if fails == 3:
                notify("Watcher cannot reach Mudrex (3 tries). Stop-losses on Mudrex still protect open trades.")
        except Exception:                            # never die silently; log and keep watching
            log("unexpected error:\n" + traceback.format_exc())
        time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
