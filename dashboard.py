"""The bot's dashboard: one process, one URL.  Run: python dashboard.py  ->  http://127.0.0.1:8765

Serves the UI in ui/ (built with `npx vite build` in ui/, output ui/dist/client) and its data at /api/ui.
Read-only: bot files, the execution journal and cached market data. Bound to 127.0.0.1 only; the Mudrex API
secret never reaches the browser. Every number comes from the bot's own files; nothing is invented.
"""
import csv
import json
import mimetypes
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
UI_DIST = os.path.join(HERE, "ui", "dist", "client")
STALE_SEC = 900


def _json(name):
    try:
        with open(os.path.join(HERE, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _csv(name):
    try:
        with open(os.path.join(HERE, name), newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _ist(ts, fmt):
    return time.strftime(fmt, time.gmtime(ts + config.IST_OFFSET))


def _rs(x, sign=False):
    x = float(x or 0)
    return ("+" if sign and x > 0 else "−" if x < 0 else "") + f"₹{abs(x):,.2f}"


def _pct(x):
    return f"{x:+.2%}"


def _px(x):
    try:
        return f"{float(x):,.6g}"
    except (TypeError, ValueError):
        return "—"


def _age(path):
    try:
        return round(time.time() - os.path.getmtime(os.path.join(HERE, path)))
    except OSError:
        return None


def _tail(name, lines=60):
    try:
        with open(os.path.join(HERE, name), "rb") as f:
            raw = f.read()
    except OSError:
        return []
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"      # PowerShell redirects add BOMs
    return raw.decode(enc, errors="replace").splitlines()[-lines:]


def _curve(marks, since, bucket, fmt):
    """Last equity mark per bucket (seconds) since `since`."""
    pts = {}
    for at, eq in marks:
        if at >= since:
            pts[(at + config.IST_OFFSET) // bucket] = (at, eq)
    pts = [pts[k] for k in sorted(pts)]
    if not pts:
        return dict(change="—", startLabel="No data yet", points=[])
    a, b = pts[0][1], pts[-1][1]
    return dict(change=f"{b / a - 1:+.1%}" if a else "—", startLabel=f"Started the period at {_rs(a)}",
                points=[dict(label=_ist(t, fmt), value=round(e, 2)) for t, e in pts])


def _card(o):
    """A trade idea (OPEN/PREVIEW order from the planner) in plain words."""
    long_ = o.get("side", "LONG") == "LONG"
    entry, tp, sl = o.get("planned_price"), o.get("est_target") or o.get("take_profit"), o.get("est_stop") or o.get("stop_loss")
    pct = lambda b: f"{(float(b) / float(entry) - 1):+.1%}" if entry and b else ""  # noqa: E731
    return dict(coin=o["coin"], side="LONG" if long_ else "SHORT",
                verb="Buy (bet price goes UP)" if long_ else "Sell short (bet price goes DOWN)",
                entry=_px(entry), target=_px(tp), targetPct=pct(tp), stop=_px(sl), stopPct=pct(sl),
                size=_rs(o.get("notional_inr")), leverage=f"{o.get('leverage') or 1:g}×",
                risk=_rs(o.get("planned_risk_inr")), placed=o["action"] == "OPEN")


# ---------- paper trading: S1 (daily trend) and S4 (intraday sets); no real money

def paper_ui():
    import paper_s1
    s1p = _json("s1_paper_state.json") or {}
    snap = s1p.get("snapshot") or {}
    rows = [dict(name=r["name"], desc=r["desc"], champion=r["champion"], days=r["days"], total=_pct(r["total"]),
                 maxDd=f"{r['max_dd']:.1%}", trades=r["trades"],
                 verdict="main S1" if r["champion"] else "PROMOTE?" if r.get("promote") else
                 f"testing ({r['overlap_days']}/{paper_s1.PROMOTE_DAYS} days)")
            for r in paper_s1.leaderboard()]
    s1_start = float(s1p.get("start_inr") or 5000)
    s1_eq = float(snap.get("equity_inr") or s1_start)
    s1_curve = [dict(label=r["date"][5:], value=round(r["equity_inr"], 2)) for r in s1p.get("ledger", [])]

    s4p = _json("s4_paper_state.json") or {}
    s4_start = float(s4p.get("start_inr") or 5000)
    s4_val = float(s4p.get("value_inr") or s4_start)
    wins, losses = int(s4p.get("wins") or 0), int(s4p.get("losses") or 0)
    s4_trades = s4p.get("trades", [])
    return dict(
        s1=dict(start=_rs(s1_start), equity=_rs(s1_eq), total=_pct(s1_eq / s1_start - 1), rows=rows,
                decision=snap.get("decision") or "—", plan=snap.get("plan") or [],
                updated=_ist(snap["at"], "%d %b %H:%M IST") if snap.get("at") else "never", curve=s1_curve,
                rule=(f"A test version replaces S1 only after {paper_s1.PROMOTE_DAYS}+ days side by side, "
                      f"{paper_s1.PROMOTE_TRADES}+ closed trades and a clearly better result, and only with your OK."),
                positions=[dict(coin=c, entry=_px(p["entry"]), stop=_px(p["sl"]), value=_rs(p["value_inr"]),
                                since=p["since"]) for c, p in (snap.get("positions") or {}).items()]),
        s4=dict(start=_rs(s4_start), value=_rs(s4_val), total=_pct(s4_val / s4_start - 1),
                pnl=_rs(s4_val - s4_start, True), setsTotal=s4p.get("sets_total", 0),
                setsToday=s4p.get("sets_today", 0), blocked=bool(s4p.get("blocked_today")),
                wins=wins, losses=losses, winRate=f"{wins / (wins + losses):.0%}" if wins + losses else "—",
                coins=len(s4p.get("coins", [])),
                updated=_ist(s4p["at"], "%d %b %H:%M IST") if s4p.get("at") else "never — runs every hour",
                curve=[dict(label=_ist(p["t"], "%d %b %H:%M"), value=p["v"]) for p in s4p.get("curve", [])],
                open=[dict(coin=p["coin"], side=p["side"], entry=_px(p["entry"]), target=_px(p["target"]),
                           stop=_px(p["stop"]), since=p["since"], pnl=_rs(p["pnl_inr"], True))
                      for p in s4p.get("open_set", [])],
                trades=[dict(coin=t["coin"], side=t.get("side", ""), opened=t["opened"], closed=t["closed"],
                             why={"target": "hit target 🎯", "stop": "hit stop-loss 🛑", "time": "72h time limit ⏱️"}
                             .get(t["why"], t["why"]), entry=_px(t.get("entry")), exit=_px(t.get("exit")),
                             pnl=_rs(t["pnl_inr"], True)) for t in reversed(s4_trades[-30:])]),
        logs={n: _tail(n) for n in ("watcher.log", "approver.log", "s4_paper.log", "s1_paper.log")},
    )


EVENT_TONE = {"stop": "amber", "cap": "amber", "execute": "blue", "reconcile": "green"}


def ui():
    """Read-only. Every value comes from the bot's own files/journal; nothing is invented or sampled."""
    import data
    import execution as ex
    import live_trader
    import s1
    import s4
    import trade_policy as tp
    now = time.time()
    w = _json("watch_status.json") or {}
    bot = w.get("bot") or {}
    eq = float(bot.get("equity") or 0)
    pnl = float(bot.get("day_pnl") or 0)
    start = float(bot.get("day_start") or eq or 1)
    cap = float(bot.get("cap") or tp.DAILY_LOSS_LIMIT_INR)
    rate = config.INR_PER_USDT
    stop_on = os.path.exists(ex.STOP_PATH)
    live_on = bool(w.get("live_enabled"))

    positions = []
    for p in w.get("positions", []):
        coin = p["symbol"].removesuffix("USDT")
        move = (p["price"] / p["entry"] - 1) * (1 if p["side"] == "LONG" else -1) if p["entry"] else 0
        positions.append(dict(coin=coin, side=p["side"], bot=bool(p.get("bot")), price=_px(p["price"]),
                              entry=_px(p["entry"]), value=_rs(p["qty"] * p["price"] * rate),
                              leverage=f"{p.get('leverage') or 1:g}×", pnl=_rs(p["upnl_inr"], True),
                              pnlPct=f"{move:+.1%}", stop=_px(p.get("sl")) if p.get("sl") else "none",
                              target=_px(p.get("tp")) if p.get("tp") else "none",
                              protected=bool(p.get("sl"))))

    con = ex.db()
    try:
        marks = con.execute("SELECT at, equity FROM marks ORDER BY at").fetchall()
        events = con.execute("SELECT at, kind, msg FROM events ORDER BY id DESC LIMIT 12").fetchall()
        sets_done, _ = ex.cycle_set_counts(con, tp.cycle_id(now))
    finally:
        con.close()
    marks = [(int(a), float(e)) for a, e in marks] + ([(int(w["at"]), eq)] if w.get("at") and eq else [])

    # market mood: BTC vs its 200-day average decides LONG-only or SHORT-only days
    btc = data._read(data._path("BTC/USDT", "1d"))
    market = dict(mood="unknown", btcPrice="—", btcChange="—", diff="—", marker=50,
                  text="BTC daily data is missing, so the bot cannot judge the market and opens nothing.")
    if len(btc) >= s1.MOOD_SMA + 1:
        last, prev = btc[-1], btc[-2]
        avg = sum(x[4] for x in btc[-s1.MOOD_SMA:]) / s1.MOOD_SMA
        diff = last[4] / avg - 1
        mood = s1.btc_mood(btc, last[0])
        market.update(mood="up" if mood else "down" if mood is False else "unknown",
                      btcPrice=f"${last[4]:,.0f}", btcChange=f"{last[4] / prev[4] - 1:+.2%}", diff=f"{diff:+.1%}",
                      marker=round(min(100, max(0, 50 + diff * 250))),
                      text=("Bitcoin is ABOVE its 200-day average: the market is healthy, so the bot only BUYS "
                            "(bets on prices going up)." if mood else
                            "Bitcoin is BELOW its 200-day average: the market is weak, so the bot only SELLS SHORT "
                            "(bets on prices going down)." if mood is False else market["text"]),
                      asOf=_ist(last[0] + 86400, "%d %b"))

    # what the bot wants to trade next (the latest plan; PREVIEW = would trade, but STOP/dry run blocks it)
    lp = _json("live_plan.json") or {}
    fresh = lp.get("created_at") and now - lp["created_at"] < 3 * 3600
    ideas = [_card(o) for o in lp.get("orders", []) if o["action"] in ("OPEN", "PREVIEW")] if fresh else []
    closes = [dict(coin=o["coin"], reason=o.get("reason", "")) for o in lp.get("orders", [])
              if o["action"] == "CLOSE"] if fresh else []
    blocked = lp.get("blocked") if fresh else None
    ws = _json("watch_state.json") or {}
    next_at = ws.get("plan_next_at")

    # closed real trades (watcher journal) and their scorecard
    journal = _csv("journal.csv")
    pnls = [float(r["pnl_inr"]) for r in journal if r.get("pnl_inr")]
    trades = [dict(coin=r["coin"], opened=r["opened"], closed=r["closed"], days=r["days"], entry=_px(r["entry"]),
                   exit=_px(r["exit"]), pnl=_rs(r["pnl_inr"], True), win=r.get("outcome") == "win")
              for r in reversed(journal[-30:])]

    hb = _json("approver_heartbeat.json") or {}
    watch_age = round(now - w["at"]) if w.get("at") else None
    appr_age = round(now - hb["at"]) if hb.get("at") else None
    guard = w.get("guard") or {}
    s4_age = _age("s4_paper_state.json")
    s1_age = _age("s1_paper_state.json")
    ok = lambda b: "ok" if b else "bad"   # noqa: E731
    health = [
        dict(name="Watcher", state=ok(watch_age is not None and watch_age < STALE_SEC),
             detail=f"checked Mudrex {_ago(watch_age)}" if watch_age is not None else "not running",
             help="Checks your Mudrex account every 5 minutes, sends Telegram alerts and makes plans."),
        dict(name="Telegram approver", state=ok(appr_age is not None and appr_age < STALE_SEC),
             detail=f"alive {_ago(appr_age)}" if appr_age is not None else "not running",
             help="Listens for your Approve / Reject taps and /status, /plan, /stop commands."),
        dict(name="S4 paper trader", state=ok(s4_age is not None and s4_age < 2 * 3600),
             detail=f"updated {_ago(s4_age)}" if s4_age is not None else "not run yet",
             help="Runs every hour and trades S4 with pretend money."),
        dict(name="S1 paper trader", state=ok(s1_age is not None and s1_age < 26 * 3600),
             detail=f"updated {_ago(s1_age)}" if s1_age is not None else "not run yet",
             help="Runs once a day after 05:30 IST and trades S1 with pretend money."),
    ]
    safety = [
        dict(name="STOP switch", state="warn" if stop_on else "ok",
             detail="ON — dry run, no real orders" if stop_on else "off — orders allowed",
             help="Emergency brake. While it is ON the bot never places an order. /stop in Telegram turns it on."),
        dict(name="Live trading setting", state="ok" if live_on else "warn",
             detail="enabled" if live_on else "disabled", help="Master setting in .env. Off = the bot can never trade."),
        dict(name="Autonomous sets certified", state="ok" if tp.AUTONOMOUS_HEDGE_READY else "warn",
             detail="yes" if tp.AUTONOMOUS_HEDGE_READY else "not yet (owner switches it on)",
             help="Until certified, the bot shows trades as a dry run instead of placing them by itself."),
        dict(name="Daily ±₹500 limit", state="warn" if bot.get("cap_hit") else "ok",
             detail=f"hit ({bot['cap_hit']}) — resting until midnight" if bot.get("cap_hit") else
             f"{abs(pnl) / cap:.0%} used" if cap else "—",
             help="After losing or making ₹500 in a day the bot stops opening trades until 00:00 IST."),
        dict(name="Losing-streak guard", state="bad" if guard.get("tripped") else "ok",
             detail=f"TRIPPED: {guard.get('reason')}" if guard.get("tripped") else
             f"{guard.get('streak', 0)} losses in a row · {float(guard.get('drawdown') or 0):.0%} below peak",
             help="Pauses new trades if results get worse than anything seen in testing."),
        dict(name="Every position has a stop-loss", state=ok(all(p["protected"] for p in positions if p["bot"])),
             detail="yes" if all(p["protected"] for p in positions if p["bot"]) else "NO — check Mudrex now",
             help="Every bot trade carries a stop-loss and a target on Mudrex itself, even if this PC is off."),
        dict(name="Profit & loss confirmed", state="warn" if w.get("pnl_unknown") else "ok",
             detail=w.get("pnl_unknown") or "yes",
             help="If Mudrex has not confirmed a result yet, new trades wait for safety."),
    ]
    issues = [f"{h['name']}: {h['detail']}" for h in health[:2] + safety if h["state"] == "bad"]

    # one plain-English sentence: what is the bot doing right now?
    if issues:
        tone, title = "bad", "Something needs a look"
        detail = " · ".join(issues)
    elif bot.get("cap_hit"):
        tone, title = "warn", "Resting for the rest of today"
        detail = f"The daily ₹{cap:,.0f} limit was reached. Trading starts again at 00:00 IST."
    elif stop_on:
        tone, title = "warn", "Dry run — watching the market, placing nothing"
        detail = ("The STOP switch is on, so trade ideas are only shown (here and in Telegram), never placed. "
                  "Paper trading keeps running with pretend money.")
    elif not live_on or not tp.AUTONOMOUS_HEDGE_READY:
        tone, title = "warn", "Watching only — live trading is not switched on"
        detail = "The bot plans trades but cannot place them until the owner enables live trading."
    else:
        tone, title = "good", "Trading live and protected"
        detail = f"{sum(p['bot'] for p in positions)} bot trade(s) open, each with a stop-loss and a target on Mudrex."

    L = dict(risk=s4.SET_RISK_INR, tp=s4.TP_DATR, sl=s4.SL_DATR, hold=s4.HOLD_H)
    midnight = (now + config.IST_OFFSET) // 86400 * 86400 + 86400 - config.IST_OFFSET
    return dict(
        now=round(now), dateLabel=_ist(now, "%A, %d %B %Y"),
        markedAt=_ist(w["at"], "%H:%M IST") if w.get("at") else "never",
        headline=dict(tone=tone, title=title, detail=detail, stop=stop_on, live=live_on and not stop_on),
        money=dict(equity=_rs(eq), equityChange=_pct(eq / s1.CAPITAL_CAP_INR - 1) if eq else "—",
                   allocation=_rs(s1.CAPITAL_CAP_INR), today=_rs(pnl, True), todayPct=_pct(pnl / start) if start else "—",
                   limit=f"₹{cap:,.0f}", limitMarker=round(min(100, max(0, 50 + 50 * pnl / cap))) if cap else 50,
                   dayStart=_rs(start)),
        today=dict(setsDone=sets_done, setsTarget=tp.TARGET_SETS_PER_CYCLE, setsAuto=tp.AUTONOMOUS_SETS_PER_CYCLE,
                   resetInSec=round(midnight - now), nextCheckInSec=round(next_at - now) if next_at else None),
        next=dict(ideas=ideas, closes=closes, blocked=blocked,
                  dryRun=bool(blocked and "STOP" in str(blocked)),
                  planTime=_ist(lp["created_at"], "%H:%M IST") if lp.get("created_at") else "never",
                  setsDone=lp.get("completed_sets"), needsApproval=bool(lp.get("needs_approval")) and bool(fresh)),
        positions=positions,
        trades=dict(rows=trades, count=len(pnls), wins=sum(p > 0 for p in pnls),
                    winRate=f"{sum(p > 0 for p in pnls) / len(pnls):.0%}" if pnls else "—",
                    total=_rs(sum(pnls), True), best=_rs(max(pnls), True) if pnls else "—",
                    worst=_rs(min(pnls), True) if pnls else "—"),
        charts={"1D": _curve(marks, now - 86400, 3600, "%H:%M"),
                "7D": _curve(marks, now - 7 * 86400, 86400, "%d %b"),
                "30D": _curve(marks, now - 30 * 86400, 86400, "%d %b"),
                "All": _curve(marks, 0, 86400, "%d %b")},
        market=market,
        health=health, safety=safety,
        strategy=dict(
            live=live_trader.STRATEGY,
            name="S4 · Intraday momentum sets" if live_trader.STRATEGY == "S4" else "S1 · Daily trend basket",
            steps=[
                "Every hour, look at all liquid coins on Mudrex.",
                "Check the market mood (Bitcoin vs its 200-day average) to decide: only buys, or only short sells.",
                "Pick the ONE coin moving hardest in that direction over the last 24 hours.",
                f"Enter with a fixed target ({L['tp']:g}× the coin's normal daily move) and a fixed stop-loss "
                f"({L['sl']:g}×), both placed on Mudrex with the order.",
                f"Exit at the target, the stop-loss, or after {L['hold']} hours — whichever comes first.",
                "Only one trade (a \"set\") at a time. Next set starts after the last one closes.",
            ],
            rules=[
                ["Money at risk per trade", f"₹{L['risk']:,.0f}", "The most one trade can lose if its stop-loss is hit (plus fees)."],
                ["Daily stop", f"±₹{tp.DAILY_LOSS_LIMIT_INR:,.0f}", "After losing or making this much in a day, no new trades until midnight."],
                ["Trades per day", f"aim {tp.TARGET_SETS_PER_CYCLE}, max {tp.AUTONOMOUS_SETS_PER_CYCLE} without asking",
                 "A 4th trade needs your Approve tap in Telegram. Weak days are skipped, never forced."],
                ["Money used", f"up to ₹{s1.CAPITAL_CAP_INR:,} (max {s4.MAX_NOTIONAL_LEV}× in positions)", "The bot never uses more than this allocation."],
                ["Day resets", "00:00 IST", "Trade counts and the ₹500 limit start fresh every midnight India time."],
                ["Direction", "LONG and SHORT", "Buys in healthy markets, sells short in weak ones."],
            ],
            evidence=("Honest note: S4 won in backtests (+66% over 3 years) but only +20% when a single coin was "
                      "swapped. Its edge is NOT proven. That is why it is running as a dry run and on paper first."),
        ),
        activity=[dict(kind=e["kind"], tone=EVENT_TONE.get(e["kind"], "gray"),
                       detail=re.sub(r"telegram user \d+", "you (Telegram)", e["msg"])[:240],
                       time=_ist(e["at"], "%d %b %H:%M"))
                  for e in events],
        paper=paper_ui(),
    )


def _ago(sec):
    if sec is None:
        return "never"
    return f"{sec}s ago" if sec < 90 else f"{round(sec / 60)} min ago" if sec < 5400 else f"{sec / 3600:.1f} h ago"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/ui":
            body, ctype = json.dumps(ui()).encode(), "application/json"
        else:
            name = "_shell.html" if path in ("/", "/index.html") else path.lstrip("/")
            file = os.path.realpath(os.path.join(UI_DIST, name))
            if not file.startswith(os.path.realpath(UI_DIST) + os.sep) or not os.path.isfile(file):
                if path == "/":
                    body, ctype = b"UI not built yet: run  npx vite build  in the ui folder.", "text/plain"
                else:
                    self.send_error(404)
                    return
            else:
                with open(file, "rb") as f:
                    body = f.read()
                ctype = mimetypes.guess_type(file)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Dashboard: http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
