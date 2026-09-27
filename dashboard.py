"""The bot's dashboard: one process, one URL.  Run: python dashboard.py  ->  http://127.0.0.1:8765

Serves the UI in ui/ (built with `npx vite build` in ui/, output ui/dist/client) and its data at /api/ui.
Read-only: bot files, the execution journal and cached market data. Bound to 127.0.0.1 only; the Mudrex API
secret never reaches the browser.
"""
import json
import mimetypes
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
UI_DIST = os.path.join(HERE, "ui", "dist", "client")


def live_limits():
    """The limits that actually govern live S1, read from the modules that enforce them."""
    import execution
    import s1
    import watcher
    return dict(strategy=s1.NAME, allocation_inr=s1.CAPITAL_CAP_INR, leverage=s1.LEV,
                max_exposure_inr=s1.LEV * s1.CAPITAL_CAP_INR, safety_stop=f"{s1.SL_ATR} x ATR below the fill",
                max_trade_stop_risk=s1.MAX_TRADE_STOP_RISK, max_total_stop_risk=s1.MAX_TOTAL_STOP_RISK,
                daily_cap_pct=s1.DAILY_CAP_PCT,
                daily_cap_note="blocks NEW buys only; closing needs your approval (Close all); not a maximum loss",
                guard=f"{watcher.TRIP_STREAK} losses in a row or {watcher.TRIP_DD:.0%} drawdown",
                buy_plan_valid_min=execution.ENTRY_MAX_AGE // 60, max_price_drift=execution.MAX_DRIFT,
                taker_fee=config.TAKER_FEE, gst=config.GST, inr_per_usdt=config.INR_PER_USDT)



# ---------- /api/ui: live data in the exact shape of the Freebuff/Lovable UI (src/data/mudrex-demo.json)

def _json(name):
    try:
        with open(os.path.join(HERE, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _ist(ts, fmt):
    return time.strftime(fmt, time.gmtime(ts + config.IST_OFFSET))


def _rs(x, sign=False):
    s = f"{abs(x):,.2f}"
    return ("+" if sign and x > 0 else "−" if x < 0 else "") + "₹" + s


def _curve(marks, since, bucket, fmt):
    """Last equity mark per bucket (seconds) since `since`."""
    pts = {}
    for at, eq in marks:
        if at >= since:
            pts[(at + config.IST_OFFSET) // bucket] = (at, eq)
    pts = [pts[k] for k in sorted(pts)]
    if not pts:
        return dict(change="—", startLabel="No data yet", endLabel="", points=[])
    a, b = pts[0][1], pts[-1][1]
    return dict(change=f"{b / a - 1:+.1%}", startLabel=f"Start {_rs(a)}", endLabel=f"End {_rs(b)}",
                points=[dict(label=_ist(t, fmt), value=round(e, 2)) for t, e in pts])


def _tail(name, lines=40):
    try:
        with open(os.path.join(HERE, name), "rb") as f:
            raw = f.read()
    except OSError:
        return []
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"      # PowerShell redirects add BOMs
    return raw.decode(enc, errors="replace").splitlines()[-lines:]


def _pct(x):
    return f"{x:+.2%}"


def paper_ui():
    """Paper trading (no real money): S1 challengers, S3 set trader, older research bots, recent logs."""
    import paper_portfolio
    import paper_s1
    s1p = (_json("s1_paper_state.json") or {}).get("snapshot") or {}
    rows = [dict(name=r["name"], desc=r["desc"], champion=r["champion"], days=r["days"], total=_pct(r["total"]),
                 maxDd=f"{r['max_dd']:.1%}", trades=r["trades"],
                 t="—" if r["champion"] else f"{r['vs_champion_t']:.2f}",
                 verdict="champion" if r["champion"] else "PROMOTE?" if r.get("promote") else
                 f"collecting ({r['overlap_days']}/{paper_s1.PROMOTE_DAYS} days)")
            for r in paper_s1.leaderboard()]
    s3 = _json("s3_paper_state.json") or {}
    s3_eq, s3_start = float(s3.get("equity_inr") or 0), float(s3.get("start_inr") or 0) or 1
    research = [dict(name=r["name"], desc=r["desc"], days=r["days"], total=_pct(r["total"]),
                     maxDd=f"{r['max_dd']:.1%}") for r in paper_portfolio.leaderboard()]
    t1 = _json("paper_state.json")
    if t1 and t1.get("start_equity"):
        research.append(dict(name="T1", desc="Donchian breakout + ATR trail (first research bot)",
                             days=len({c.get("day") for c in t1.get("closed", [])}), maxDd="—",
                             total=_pct(t1["equity"] / t1["start_equity"] - 1)))
    return dict(
        s1=dict(rows=rows, equity=_rs(s1p.get("equity_inr") or 0), decision=s1p.get("decision") or "—",
                plan=s1p.get("plan") or [],
                rule=(f"A challenger replaces S1 only after {paper_s1.PROMOTE_DAYS}+ days side by side, "
                      f"{paper_s1.PROMOTE_TRADES}+ closed trades, weekly t ≥ {paper_s1.PROMOTE_T:.2f} and a drawdown "
                      f"no worse than S1's + 2 points, and then only with your OK."),
                positions=[dict(coin=c, entry=f"{p['entry']:g}", stop=f"{p['sl']:.6g}", value=_rs(p["value_inr"]),
                                since=p["since"]) for c, p in (s1p.get("positions") or {}).items()]),
        s3=dict(variant="1h breakout sets: target +1×ATR, stop 0.5×ATR, max 48 h (daily ATR)",
                start=_rs(s3_start), equity=_rs(s3_eq), total=_pct(s3_eq / s3_start - 1),
                setsTotal=s3.get("sets_total", 0), setsToday=s3.get("sets_today", 0),
                blocked=bool(s3.get("blocked_today")), updated=_ist(s3["at"], "%d %b %H:%M IST") if s3.get("at") else "never",
                open=[dict(coin=p.get("coin", "?"), detail=", ".join(f"{k} {v}" for k, v in p.items() if k != "coin"))
                      for p in s3.get("open_set", [])],
                trades=[dict(coin=t["coin"], opened=t["opened"], closed=t["closed"], why=t["why"],
                             pnl=_rs(t["pnl_inr"], True)) for t in reversed(s3.get("trades", [])[-20:])]),
        research=research,
        logs={n: _tail(n) for n in ("watcher.log", "approver.log", "s1_paper.log", "s3_paper.log")},
    )


EVENT_ICON = {"stop": ("ShieldAlert", "amber"), "execute": ("Activity", "blue"),
              "reconcile": ("ShieldCheck", "green"), "cap": ("ShieldAlert", "amber")}


def ui():
    """Read-only. Every value comes from the bot's own files/journal; nothing is invented or sampled."""
    import data
    import execution as ex
    import s1
    now = time.time()
    w = _json("watch_status.json") or {}
    bot = w.get("bot") or {}
    eq = float(bot.get("equity") or 0)
    pnl = float(bot.get("day_pnl") or 0)
    start = float(bot.get("day_start") or eq or 1)
    cap = float(bot.get("cap") or 0)
    rate = config.INR_PER_USDT

    positions = []
    for p in w.get("positions", []):
        coin = p["symbol"].removesuffix("USDT")
        notional = p["qty"] * p["price"] * rate
        move = (p["price"] / p["entry"] - 1) * (1 if p["side"] == "LONG" else -1) if p["entry"] else 0
        positions.append(dict(symbol=coin, name=f"{coin} / USDT", side=p["side"],
                              leverage=f"{s1.LEV}× isolated" if p.get("bot") else "manual",
                              stopLabel=f"{s1.SL_ATR}× ATR" if p.get("bot") else "your stop",
                              price=f"{p['price']:g}", entry=f"{p['entry']:g}", notional=_rs(notional),
                              pnl=_rs(p["upnl_inr"], True), pnlPct=f"{move:+.1%}",
                              stop=f"{p['sl']:g}" if p.get("sl") else "none",
                              state="Protected" if p.get("sl") else "Stop pending"))
    bot_pos = [p for p in w.get("positions", []) if p.get("bot")]
    gross = sum(p["qty"] * p["price"] * rate for p in bot_pos)

    con = ex.db()
    try:
        marks = con.execute("SELECT at, equity FROM marks ORDER BY at").fetchall()
        prow = con.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        porders = con.execute("SELECT coin, action, state, planned_notional_inr, error FROM orders WHERE plan_id=? "
                              "ORDER BY seq", (prow["id"],)).fetchall() if prow else []
        events = con.execute("SELECT at, kind, msg FROM events ORDER BY id DESC LIMIT 8").fetchall()
    finally:
        con.close()
    marks = [(int(a), float(e)) for a, e in marks] + ([(int(w["at"]), eq)] if w.get("at") and eq else [])

    btc = data._read(data._path("BTC/USDT", "1d"))
    market = dict(status="Unknown", statusCaption="BTC daily data missing", btcPrice="—", btcChange="—",
                  averageLabel="BTC vs 200-day average", averageDifference="—", markerPercent=50,
                  breadthAbove=0, breadthTotal=len(s1.BASKET), breadthCaption="trend up in today's plan",
                  moodGate="Unknown", moodCaption="new buys blocked")
    if len(btc) >= s1.MOOD_SMA + 1:
        last, prev = btc[-1], btc[-2]
        avg = sum(x[4] for x in btc[-s1.MOOD_SMA:]) / s1.MOOD_SMA
        diff = last[4] / avg - 1
        mood = s1.btc_mood(btc, last[0])
        market.update(status="Market mood GOOD" if mood else "Market mood BAD" if mood is False else "Unknown",
                      statusCaption=f"BTC daily close {_ist(last[0] + 86400, '%d %b')} · updates daily",
                      btcPrice=f"${last[4]:,.2f}", btcChange=f"{last[4] / prev[4] - 1:+.2%}",
                      averageDifference=f"{diff:+.1%}", markerPercent=round(min(100, max(0, 50 + diff * 250))),
                      moodGate="Pass" if mood else "Blocked" if mood is False else "Unknown",
                      moodCaption="new buys allowed" if mood else "no positions / no new buys")
    lp = _json("live_plan.json") or {}
    market["breadthAbove"] = sum(o["action"] in ("OPEN", "HOLD") for o in lp.get("orders", []))

    plan_items, needs, msg = [], False, "No plan yet. The next one arrives around 05:35 IST."
    if prow:
        tone = {"OPEN": "review", "CLOSE": "review", "HOLD": "hold"}
        for o in porders:
            plan_items.append(dict(tone=tone.get(o["action"], "idle"), label=f"{o['action'].title()} {o['coin']}",
                                   detail=(o["state"] or "").replace("_", " ").lower()
                                   + (" · not sent" if "halted" in (o["error"] or "") else ""),
                                   count=_rs(o["planned_notional_inr"] or 0)))
        fresh = now - prow["created_at"] < ex.ENTRY_MAX_AGE
        needs = prow["state"] == "PLANNED" and fresh
        msg = ("Tap Approve in Telegram within 15 min to place these orders." if needs else
               f"Plan {prow['id']} is {prow['state'].replace('_', ' ').lower()}. Send /plan in Telegram for a fresh one.")
    if not plan_items:
        plan_items = [dict(tone="idle", label="No orders", detail="nothing to do today")]
    hb = _json("approver_heartbeat.json") or {}
    watch_age = now - w["at"] if w.get("at") else None
    appr_age = now - hb["at"] if hb.get("at") else None
    issues = []
    if watch_age is None or watch_age > 900:
        issues.append("Watcher is not reporting (data may be stale)")
    if appr_age is None or appr_age > 900:
        issues.append("Telegram approver is not running: Approve buttons will not work")
    if w.get("stop"):
        issues.append("STOP switch is ON: no new orders")
    if w.get("pnl_unknown"):
        issues.append(f"Bot P&L unknown: {w['pnl_unknown']}")
    if (w.get("guard") or {}).get("tripped"):
        issues.append("Performance guard tripped: new buys paused")
    issues += [f"{p['symbol']} has no stop-loss" for p in positions if p["stop"] == "none"]

    L = live_limits()
    return dict(
        dateLabel=_ist(now, "%A, %d %B %Y"),
        markedAt=_ist(w["at"], "%H:%M IST") if w.get("at") else "never",
        status=dict(live=bool(w.get("live_enabled")), stop=bool(w.get("stop")), ok=not issues,
                    watcherAgeSec=round(watch_age) if watch_age is not None else None,
                    approverAgeSec=round(appr_age) if appr_age is not None else None, issues=issues),
        metrics=dict(equityMajor=_rs(eq).split(".")[0], equityDecimal="." + _rs(eq).split(".")[1],
                     equityChange=f"{eq / s1.CAPITAL_CAP_INR - 1:+.2%}",
                     equityComparison=f"vs ₹{s1.CAPITAL_CAP_INR:,} allocation",
                     todayPnlMajor=_rs(pnl, True).split(".")[0], todayPnlDecimal="." + _rs(pnl).split(".")[1],
                     todayPnlChange=f"{pnl / start:+.2%}", todayPnlCaption="since 00:00 IST",
                     positionCount=len(bot_pos), basketSize=len(s1.BASKET), positionCaption="S1 basket",
                     grossExposure=_rs(gross), exposureRatio=f"{gross / eq:.2f}×" if eq else "—",
                     exposureCaption="of bot equity"),
        charts={"1D": _curve(marks, now - 86400, 3600, "%H:%M"),
                "7D": _curve(marks, now - 7 * 86400, 86400, "%d %b"),
                "30D": _curve(marks, now - 30 * 86400, 86400, "%d %b")},
        market=market,
        risk=dict(dailyPnl=_rs(pnl, True), threshold=f"±{_rs(cap)}",
                  status="CAP HIT" if bot.get("cap_hit") else "WITHIN LIMIT",
                  utilizationPercent=round(min(100, max(0, 50 + 50 * pnl / cap))) if cap else 50,
                  utilizationLabel=f"{abs(pnl) / cap:.0%} of daily limit used" if cap else "",
                  resetLabel="Resets at 00:00 IST", entryRule="New buys blocked at the limit",
                  entryRuleDetail="Hitting it offers Close all in Telegram; closing needs your tap."),
        plan=dict(id=prow["id"] if prow else None, state=prow["state"] if prow else None, needsAction=needs,
                  message=msg, title=f"Plan {prow['id']}" if prow else "No plan yet",
                  decisionLabel=f"Decision on {prow['decision_day']} close" if prow else "",
                  time=_ist(prow["created_at"], "%d %b %H:%M IST") if prow else "", items=plan_items),
        strategy=dict(name="S1 Trend Ensemble", description="Buy-only · daily · 6-coin basket",
                      leverage=f"{L['leverage']}×", stop=f"{s1.SL_ATR}× ATR", btcFilter="200-day average",
                      allocationCap=f"₹{L['allocation_inr']:,}", signal="9 Donchian trend judges (5-360 days)",
                      marketFilter="BTC at/above its 200-day average", basket=" · ".join(s1.BASKET),
                      entryMode="Every buy needs your Telegram approval", stopModel=L["safety_stop"],
                      target="None: exits on trend end, mood, stop-loss or daily limit",
                      tradeRisk=f"{L['max_trade_stop_risk']:.0%} per trade · {L['max_total_stop_risk']:.0%} total"),
        positions=positions,
        activity=[dict(icon=EVENT_ICON.get(e["kind"], ("Activity", "gray"))[0], title=e["kind"].title(),
                       detail=e["msg"][:220], tone=EVENT_ICON.get(e["kind"], ("Activity", "gray"))[1],
                       time=_ist(e["at"], "%H:%M:%S") if now - e["at"] < 86400 else _ist(e["at"], "%d %b %H:%M"))
                  for e in events],
        notifications=[dict(icon="ShieldAlert", text=t) for t in issues]
        + ([dict(icon="Activity", text=msg)] if needs else []),
        paper=paper_ui(),
    )


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
