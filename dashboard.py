"""Local read-only dashboard: the bot's face. Run: python dashboard.py  ->  http://127.0.0.1:8765

Shows live Mudrex account (read-only GETs), selected coins, scan results, learning decisions and logs.
Bound to 127.0.0.1 only. The API secret stays in this process; the browser never sees it.
"""
import csv
import glob
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import paper_portfolio

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
API = "https://trade.mudrex.com/fapi/v1"
ACCOUNT_TTL = 30  # seconds; wallet endpoints allow 2 req/s, we use far less
_account_cache = {"t": 0, "data": None}


def _api_get(path):
    req = urllib.request.Request(API + path, headers={"X-Authentication": os.environ["MUDREX_API_SECRET"]})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("data")


def account():
    if not os.environ.get("MUDREX_API_SECRET"):
        return {"error": "MUDREX_API_SECRET not set in .env"}
    if time.time() - _account_cache["t"] < ACCOUNT_TTL:
        return _account_cache["data"]
    out = {}
    for cur in ("INR", "USDT"):
        try:
            out[cur] = dict(funds=_api_get(f"/futures/funds?trade_currency={cur}"),
                            positions=_api_get(f"/futures/positions?trade_currency={cur}") or [])
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            out[cur] = {"error": f"{type(e).__name__}: {getattr(e, 'code', '')} {e}"}
    _account_cache.update(t=time.time(), data=out)
    return out


def read_csv(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    with open(path, newline="") as f:
        return dict(rows=list(csv.DictReader(f)), mtime=os.path.getmtime(path))


def read_text(path, lines=60):
    with open(path, "rb") as f:
        raw = f.read()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"  # PowerShell redirects add BOMs
    return raw.decode(enc, errors="replace").splitlines()[-lines:]


def scan_summary(name):
    d = read_csv(name)
    if not d:
        return None
    by = defaultdict(list)
    for r in d["rows"]:
        by[r["variant"]].append(r)
    rows = []
    for v, rs in by.items():
        nets = [float(r["net"]) for r in rs]
        rows.append(dict(variant=v, coins=len(rs), avg_net=sum(nets) / len(nets),
                         profitable=sum(n > 0 for n in nets), trades=sum(int(r["trades"]) for r in rs),
                         best=max(rs, key=lambda r: float(r["net"]))["coin"], best_net=max(nets)))
    return dict(rows=rows, per_coin=d["rows"], mtime=d["mtime"])


def paper(prefix="paper"):
    path = os.path.join(HERE, f"{prefix}_state.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        st = json.load(f)
    trades = read_csv(f"{prefix}_trades.csv")
    return dict(state=st, trades=trades["rows"] if trades else [], mtime=os.path.getmtime(path),
                inr=config.INR_PER_USDT)


def portfolio_paper():
    path = os.path.join(HERE, "portfolio_state.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        st = json.load(f)
    ledger = read_csv("portfolio_ledger.csv")
    return dict(state=st, ledger=ledger["rows"] if ledger else [], mtime=os.path.getmtime(path),
                inr=config.INR_PER_USDT)


def s1_paper():
    path = os.path.join(HERE, "s1_paper_state.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        st = json.load(f)
    trades = read_csv("s1_paper_trades.csv")
    return dict(snapshot=st.get("snapshot", {}), ledger=st.get("ledger", []), start=st["start_inr"],
                trades=trades["rows"] if trades else [], mtime=os.path.getmtime(path))


def state():
    logs = {os.path.basename(p): dict(lines=read_text(p), mtime=os.path.getmtime(p))
            for p in glob.glob(os.path.join(HERE, "*.log"))}
    return dict(
        now=time.time(),
        mode="LIVE S1 (Rs 5,000 cap): real orders only when you run execute + YES. Watcher is read-only.",
        s1=s1_paper(),
        watch=json.load(open(os.path.join(HERE, "watch_status.json"))) if os.path.exists(os.path.join(HERE, "watch_status.json")) else None,
        portfolio=portfolio_paper(),
        leaderboard=paper_portfolio.leaderboard(),
        promote_rule=f"t >= {paper_portfolio.PROMOTE_T} over >= {paper_portfolio.PROMOTE_DAYS} overlapping days",
        paper=paper("paper"),
        replay=paper("replay"),
        limits=dict(risk_per_trade=config.RISK_PCT, leverage=config.LEVERAGE, daily_loss_cap=config.DAILY_LOSS_CAP,
                    dd_halve=config.DD_HALVE, dd_halt=config.DD_HALT, taker_fee=config.TAKER_FEE, gst=config.GST,
                    inr_per_usdt=config.INR_PER_USDT),
        account=account(),
        universe=read_csv("universe.csv"),
        scans={os.path.basename(p): scan_summary(os.path.basename(p))
               for p in sorted(glob.glob(os.path.join(HERE, "*scan*.csv")), key=os.path.getmtime, reverse=True)},
        learning=read_csv("learning_log.csv"),
        logs=logs,
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            body, ctype = json.dumps(state()).encode(), "application/json"
        elif self.path == "/":
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mudrex Bot</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#16181d;--muted:#5d6472;--line:#e3e6eb;--good:#0a7d4f;--bad:#c2362b;--warn:#a86400;--accent:#3b5bdb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e7e9ee;--muted:#9aa2b1;--line:#262b35;--good:#3ecf8e;--bad:#ff6b5e;--warn:#f0a93b;--accent:#7c93ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:1}
h1{font-size:18px;margin:0}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600;background:color-mix(in srgb,var(--warn) 15%,transparent);color:var(--warn)}
main{display:grid;gap:16px;padding:16px 20px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));max-width:1400px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.wide{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px}.scroll{overflow:auto;max-height:420px}
.good{color:var(--good)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 16px}.big{font-size:26px;font-weight:700}
pre{margin:0;font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto;background:var(--bg);padding:10px;border-radius:6px}
.bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;min-width:60px}.bar>i{display:block;height:100%}
</style></head><body>
<header><div><h1>Mudrex Bot</h1><div class="muted" id="updated">loading…</div></div><span class="pill" id="mode"></span></header>
<main>
  <section class="card wide"><h2>24/7 watcher: live account today</h2><div id="watch" class="muted">watcher not running</div></section>
  <section class="card wide"><h2>S1 paper (Rs 5,000): buy-only, safety stop, 2x <span class="muted" id="s1_t"></span></h2><div id="s1" class="muted">not started</div></section>
  <section class="card wide"><h2>Leaderboard: champion vs challengers (forward paper results, new data only)</h2><div id="lb" class="muted">no ledgers yet</div></section>
  <section class="card wide"><h2>Portfolio paper trading: Z8 ensemble trend, whole market <span class="muted" id="pf_t"></span></h2><div id="pfolio" class="muted">not started</div></section>
  <section class="card wide"><h2>Paper trading (live, daily): T1 breakout <span class="muted" id="pp_t"></span></h2><div id="paper" class="muted">not started</div></section>
  <section class="card wide"><h2>Paper replay: same live code over past days <span class="muted" id="rp_t"></span></h2><div id="replay" class="muted">run: python paper.py --replay 365</div></section>
  <section class="card"><h2>Account (read-only)</h2><div id="account" class="muted">loading…</div></section>
  <section class="card"><h2>Safety limits (hard caps)</h2><div id="limits" class="kv"></div></section>
  <section class="card wide"><h2>Strategy results (out-of-sample, after fees + GST). Pass bar: pooled PF ≥ 1.10 and ≥ 60% coins profitable</h2><div id="scans" class="muted">not run yet</div></section>
  <section class="card wide"><h2>Selected coins <span class="muted" id="uni_t"></span></h2><div id="universe" class="scroll muted">not run yet</div></section>
  <section class="card wide"><h2>Self-learning decisions (last walk-forward)</h2><div id="learning" class="scroll muted">not run yet</div></section>
  <section class="card wide"><h2>Live log</h2><div id="logs" class="muted">no logs</div></section>
</main>
<script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct=(x,d=1)=>(x*100).toFixed(d)+"%";
const sign=x=>x>0?"good":x<0?"bad":"";
const ago=t=>{const s=Math.round(Date.now()/1000-t);return s<90?s+"s ago":s<5400?Math.round(s/60)+"m ago":new Date(t*1000).toLocaleString()};
function table(cols,rows){return `<table><thead><tr>${cols.map(c=>`<th>${esc(c[0])}</th>`).join("")}</tr></thead><tbody>${
  rows.map(r=>`<tr>${cols.map(c=>`<td>${c[1](r)}</td>`).join("")}</tr>`).join("")}</tbody></table>`}
function scanTable(s){ if(!s) return "not run yet";
  const rows=[...s.rows].sort((a,b)=>b.avg_net-a.avg_net);
  return table([["Variant",r=>esc(r.variant)],["Avg net / coin",r=>`<span class="${sign(r.avg_net)}">${pct(r.avg_net,2)}</span>`],
    ["Profitable coins",r=>`${r.profitable}/${r.coins} <div class="bar"><i style="width:${100*r.profitable/r.coins}%;background:var(--good)"></i></div>`],
    ["Trades",r=>r.trades],["Best coin",r=>`${esc(r.best)} <span class="${sign(r.best_net)}">${pct(r.best_net)}</span>`]],rows)}
function accountHtml(a){ if(a.error) return `<span class="bad">${esc(a.error)}</span>`;
  return Object.entries(a).map(([cur,v])=>{ if(v.error) return `<div><b>${cur}</b> <span class="bad">${esc(v.error)}</span></div>`;
    const f=v.funds||{}, sym=cur==="INR"?"₹":"$";
    const pos=v.positions.length?table([["Coin",p=>esc(p.symbol)],["Side",p=>`<span class="${p.order_type==="LONG"?"good":"bad"}">${esc(p.order_type)}</span>`],
      ["Qty",p=>esc(p.quantity)],["Entry",p=>esc(p.entry_price)],["Lev",p=>esc(p.leverage)],["Liq",p=>esc(p.liquidation_price)],
      ["SL",p=>esc(p.stoploss?.price??p.stoploss?.order_price??"—")]],v.positions):`<div class="muted">no open positions</div>`;
    return `<div style="margin-bottom:14px"><div class="muted">${cur} futures wallet</div><div class="big">${sym}${esc(f.balance??"—")}</div>
      <div class="muted">locked as margin: ${sym}${esc(f.locked_amount??"0")}</div><div style="margin-top:8px">${pos}</div></div>`}).join("")}
async function tick(){
  let s; try{ s=await (await fetch("/api/state")).json() }catch(e){ document.getElementById("updated").textContent="server offline"; return }
  document.getElementById("updated").textContent="updated "+new Date().toLocaleTimeString();
  document.getElementById("mode").textContent=s.mode;
  document.getElementById("account").innerHTML=accountHtml(s.account);
  const L=s.limits; document.getElementById("limits").innerHTML=[["Risk per trade",pct(L.risk_per_trade)],["Max leverage",L.leverage+"x"],
    ["Daily loss stop",pct(L.daily_loss_cap)],["Halve risk at drawdown",pct(L.dd_halve)],["Halt at drawdown",pct(L.dd_halt)],
    ["Fee (+GST)",pct(L.taker_fee,3)+" + "+pct(L.gst,0)],["INR / USDT",L.inr_per_usdt]].map(([k,v])=>`<span class="muted">${k}</span><b>${v}</b>`).join("");
  if(s.watch){ const W=s.watch, B=W.bot||{}, age=Date.now()/1000-W.at, alive=age<2*W.check_every_sec+60, rup=x=>"₹"+(+x||0).toLocaleString(undefined,{maximumFractionDigits:0});
    const bar=(v,cap,col)=>`<div class="bar" style="width:220px"><i style="width:${Math.min(100,Math.max(0,100*v/(cap||1)))}%;background:var(--${col})"></i></div>`;
    const status=W.stop?["KILL SWITCH ON","bad","no orders until: python ops.py resume"]:!W.live_enabled?["LIVE OFF","bad","LIVE_TRADING_ENABLED=false"]:
      B.cap_hit?["CAP HIT ("+esc(B.cap_hit)+")","bad","no new entries until tomorrow"]:["trading allowed","good","new entries allowed today"];
    document.getElementById("watch").innerHTML=`<div style="display:flex;gap:32px;flex-wrap:wrap">
      <div><div class="muted">Watcher</div><div class="big ${alive?"good":"bad"}">${alive?"RUNNING":"STOPPED"}</div><div class="muted">last check ${ago(W.at)}${W.ok===false?' · <span class="bad">'+esc(W.error||"error")+"</span>":""}</div></div>
      <div><div class="muted">Bot equity</div><div class="big">${rup(B.equity)}</div><div class="muted">Rs 5,000 allocation + bot P&amp;L · day start ${rup(B.day_start)}</div></div>
      <div><div class="muted">Bot P&amp;L today (IST ${esc(B.day||"")})</div><div class="big ${sign(B.day_pnl)}">${B.day_pnl>=0?"+":""}${rup(B.day_pnl)}</div>
        <div class="muted">profit cap ${rup(B.cap)}</div>${bar(Math.max(B.day_pnl,0),B.cap,"good")}
        <div class="muted">loss cap ${rup(B.cap)}</div>${bar(Math.max(-B.day_pnl,0),B.cap,"bad")}</div>
      <div><div class="muted">Status</div><div class="big ${status[1]}">${status[0]}</div><div class="muted">${status[2]}</div></div></div>`+
      (W.positions.length?table([["Coin",p=>esc(p.symbol)],["Side",p=>esc(p.side)],["Qty",p=>p.qty],["Entry",p=>(+p.entry).toPrecision(5)],["Now",p=>(+p.price).toPrecision(5)],
        ["Stop-loss",p=>p.sl?(+p.sl).toPrecision(5):'<span class="bad">NONE</span>'],["P&L",p=>`<span class="${sign(p.upnl_inr)}">${rup(p.upnl_inr)}</span>`],["Owner",p=>p.bot?"bot (S1)":"manual"]],W.positions):
        `<div class="muted" style="margin-top:8px">no open positions</div>`) }
  if(s.s1){ const P=s.s1, sn=P.snapshot, rup=x=>"₹"+Math.round(x).toLocaleString();
    document.getElementById("s1_t").textContent="· last run "+ago(P.mtime);
    const pos=Object.entries(sn.positions||{});
    document.getElementById("s1").innerHTML=`<div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:12px">
      <div><div class="muted">Paper equity</div><div class="big ${sign(sn.equity_inr-P.start)}">${rup(sn.equity_inr)}</div><div class="muted">start ${rup(P.start)} · ${pct(sn.equity_inr/P.start-1,2)}</div></div>
      <div><div class="muted">Days realized</div><div class="big">${P.ledger.length}</div></div>
      <div><div class="muted">Closed trades</div><div class="big">${P.trades.length}</div></div></div>
      <div class="muted">plan for next open (decided on ${esc(sn.decision)} close):</div><ul>${(sn.plan||[]).map(x=>`<li>${esc(x)}</li>`).join("")||"<li>no orders</li>"}</ul>`+
      (pos.length?table([["Coin",([c])=>esc(c)],["Since",([,p])=>esc(p.since)],["Entry",([,p])=>(+p.entry).toPrecision(5)],
        ["Stop",([,p])=>p.sl?(+p.sl).toPrecision(5):"—"],["Value",([,p])=>rup(p.value_inr)]],pos):"")+
      (P.trades.length?`<div class="muted" style="margin:10px 0 6px">closed trades</div>`+table(Object.keys(P.trades[0]).map(k=>[k,t=>esc(t[k])]),[...P.trades].reverse()):"") }
  if(s.leaderboard){ const L=[...s.leaderboard].sort((a,b)=>b.total-a.total);
    document.getElementById("lb").innerHTML=table([["Strategy",r=>`<b>${esc(r.name)}</b>${r.champion?' <span class="pill">champion</span>':""}<div class="muted">${esc(r.desc)}</div>`],
      ["Days",r=>r.days],["Return",r=>`<span class="${sign(r.total)}">${pct(r.total,2)}</span>`],["Sharpe",r=>r.days>1?r.sharpe.toFixed(2):"—"],
      ["Max DD",r=>pct(r.max_dd,1)],
      ["vs champion (t)",r=>r.champion?"—":`${(r.vs_champion_t||0).toFixed(2)} <span class="muted">over ${r.overlap_days}d</span>`],
      ["Promote?",r=>r.champion?"—":r.promote?'<span class="good">YES: review</span>':'<span class="muted">not yet</span>']],L)+
      `<div class="muted" style="margin-top:8px">Promotion rule: ${esc(s.promote_rule)}. Early numbers are noise; expect weeks before anything is meaningful.</div>` }
  if(s.portfolio){ const P=s.portfolio, st=P.state, sn=st.snapshot||{}, eq=st.equity*st.start_inr;
    const rupee=x=>"₹"+Math.round(x).toLocaleString();
    document.getElementById("pf_t").textContent="· last run "+ago(P.mtime);
    const gross=(sn.book||[]).reduce((a,b)=>a+Math.abs(b.weight),0), small=(sn.book||[]).filter(b=>b.below_min).length;
    document.getElementById("pfolio").innerHTML=`<div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:12px">
      <div><div class="muted">Paper equity</div><div class="big ${sign(st.equity-1)}">${rupee(eq)}</div><div class="muted">start ${rupee(st.start_inr)} · ${pct(st.equity-1,2)}</div></div>
      <div><div class="muted">Positions</div><div class="big">${(sn.book||[]).length}</div><div class="muted">${pct(gross)} invested · rest in cash</div></div>
      <div><div class="muted">Universe</div><div class="big">${sn.universe||"—"}</div><div class="muted">coins scanned · top 40 by volume tradable</div></div>
      <div><div class="muted">Below $5 minimum</div><div class="big ${small?"bad":""}">${small}</div><div class="muted">positions too small to place live</div></div></div>`+
      (sn.added&&sn.added.length?`<div class="muted">added at ${esc(sn.effective)} open: ${sn.added.map(esc).join(", ")}</div>`:"")+
      (sn.dropped&&sn.dropped.length?`<div class="muted">dropped: ${sn.dropped.map(esc).join(", ")}</div>`:"")+
      `<div class="scroll" style="margin-top:8px">`+table([["Coin",b=>esc(b.coin)],["Weight",b=>pct(b.weight,2)],["Value",b=>rupee(b.notional_usdt*P.inr)],
        ["Price",b=>b.price?(+b.price).toPrecision(6):"—"],["Qty",b=>b.qty?(+b.qty).toPrecision(4):"—"],
        ["Live-placeable?",b=>b.below_min?'<span class="bad">no (&lt; $5)</span>':'<span class="good">yes</span>']],sn.book||[])+"</div>"+
      (P.ledger.length?`<div class="muted" style="margin:12px 0 6px">daily ledger</div><div class="scroll">`+table(Object.keys(P.ledger[0]).map(k=>[k,r=>
        k==="return"?`<span class="${sign(+r[k])}">${pct(+r[k],3)}</span>`:esc(r[k])]),[...P.ledger].reverse())+"</div>":
        `<div class="muted" style="margin-top:8px">first realized day appears after the next daily close (05:30 IST)</div>`) }
  renderPaper(s.paper,"paper","pp_t"); renderPaper(s.replay,"replay","rp_t");
  const sc=Object.entries(s.scans); if(sc.length) document.getElementById("scans").innerHTML=sc.map(([n,v])=>
    `<div class="muted" style="margin:14px 0 6px"><b>${esc(n)}</b> · ${ago(v.mtime)}</div><div class="scroll">${scanTable(v)}</div>`).join("");
  if(s.universe){ document.getElementById("uni_t").textContent=`· ${s.universe.rows.length} coins · ${ago(s.universe.mtime)}`;
    document.getElementById("universe").innerHTML=table([["Coin",r=>esc(r.coin)],["Min order $",r=>esc(r.min_order)],["Risk at min",r=>pct(+r.risk_at_min,2)],
      ["24h vol $M",r=>(r.usd_vol_24h/1e6).toFixed(0)],["Max lev",r=>esc(r.max_leverage)]],s.universe.rows) }
  if(s.learning) document.getElementById("learning").innerHTML=table(Object.keys(s.learning.rows[0]).map(k=>[k,r=>{
      const v=r[k]; return k==="test_return"?`<span class="${sign(+v)}">${pct(+v,2)}</span>`:k==="decision"&&v.startsWith("sit")?`<span class="muted">${esc(v)}</span>`:esc(v)}]),s.learning.rows);
  const logs=Object.entries(s.logs); if(logs.length) document.getElementById("logs").innerHTML=logs.map(([n,l])=>
    `<div class="muted" style="margin:6px 0">${esc(n)} · ${ago(l.mtime)}</div><pre>${esc(l.lines.join("\n"))}</pre>`).join("");
}
function renderPaper(P,id,tid){ if(!P) return;
    const st=P.state, snap=st.snapshot||{}, inr=x=>"₹"+(x*P.inr).toLocaleString(undefined,{maximumFractionDigits:0});
    const upnl=Object.values(snap.unrealized||{}).reduce((a,b)=>a+b,0), start=st.start_equity, marked=st.equity+upnl;
    document.getElementById(tid).textContent="· last run "+ago(P.mtime);
    const closedPnl=P.trades.reduce((a,t)=>a+ +t.pnl,0), wins=P.trades.filter(t=>+t.pnl>0).length;
    const pos=Object.entries(st.positions); const pend=Object.entries(snap.pending||{});
    document.getElementById(id).innerHTML=`<div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:12px">
      <div><div class="muted">Equity (marked)</div><div class="big ${sign(marked-start)}">${inr(marked)}</div><div class="muted">start ${inr(start)} · ${pct(marked/start-1,2)}</div></div>
      <div><div class="muted">Closed trades</div><div class="big">${P.trades.length}</div><div class="muted">${wins} wins · ${inr(closedPnl)}</div></div>
      <div><div class="muted">Open / queued</div><div class="big">${pos.length} / ${pend.length}</div><div class="muted">${st.halted?'<span class="bad">HALTED</span>':"max 5 positions"}</div></div></div>`+
      (pos.length?table([["Coin",([c])=>esc(c)],["Side",([,p])=>`<span class="${p.side>0?"good":"bad"}">${p.side>0?"LONG":"SHORT"}</span>`],["Qty",([,p])=>p.qty],
        ["Entry",([,p])=>(+p.entry).toPrecision(6)],["Stop",([,p])=>(+p.sl).toPrecision(6)],["Now",([c])=>(+snap.marks[c]).toPrecision(6)],
        ["Unrealized",([c])=>`<span class="${sign(snap.unrealized[c])}">${inr(snap.unrealized[c])}</span>`]],pos):`<div class="muted">no open positions</div>`)+
      (pend.length?`<div class="muted" style="margin-top:8px">queued for next open: ${pend.map(([c,v])=>esc(c)+" "+(v.exit?"exit ":"")+(v.entry?(v.entry>0?"LONG":"SHORT"):"")).join(", ")}</div>`:"")+
      `<div class="muted" style="margin:12px 0 6px">coin settings (re-tuned every 90 days)</div>`+
      table([["Coin",([c])=>esc(c)],["Channel",([,p])=>p?p.don_n+"d":"—"],["Stop",([,p])=>p?p.sl_atr+"×ATR":"—"],["Trail",([,p])=>p?p.trail_atr+"×ATR":"—"],
        ["Status",([,p])=>p?'<span class="good">active</span>':'<span class="muted">sitting out</span>']],Object.entries(st.params))+
      (P.trades.length?`<div class="muted" style="margin:12px 0 6px">closed trades</div><div class="scroll">`+table(Object.keys(P.trades[0]).map(k=>[k,t=>k==="pnl_inr"?`<span class="${sign(+t[k])}">₹${esc(t[k])}</span>`:esc(t[k])]),[...P.trades].reverse())+"</div>":"") }
tick(); setInterval(tick,5000);
</script></body></html>"""


if __name__ == "__main__":
    print(f"Dashboard: http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
