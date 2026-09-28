import { createFileRoute } from "@tanstack/react-router";
import {
  Activity, AlertCircle, BriefcaseBusiness, CalendarClock, Gauge, Info, Moon, ShieldCheck, TrendingDown,
  TrendingUp, Wallet, type LucideIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { hms, sign, useLive, useTick, type Idea, type Live } from "../lib/live";
import { Chart, CheckList, Head, HowView, LogsView, PaperView, Stat, Table, TradesView, sideBadge } from "../components/Views";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Mudrex Bot Dashboard" },
      { name: "description", content: "Read-only dashboard for the Mudrex trading bot: live status, trades, paper trading and safety." },
    ],
  }),
  component: Dashboard,
});

type View = "overview" | "trades" | "paper" | "how" | "logs";
const VIEWS: [View, string][] = [
  ["overview", "Overview"], ["trades", "Trade history"], ["paper", "Paper trading"], ["how", "How it works"], ["logs", "Logs"],
];
type Range = keyof Live["charts"];

function Connecting({ error }: { error: string | null }) {
  return (
    <div className="app-shell">
      <header className={`statusbar ${error ? "status-problem" : "status-warning"}`} role="status" aria-live="polite">
        <div className="status-inner">
          <span className="status-mark" aria-hidden="true"><AlertCircle size={16} /></span>
          <div className="status-copy">
            <strong>{error ? "Cannot reach the bot" : "Connecting to the bot…"}</strong>
            <span>{error ? `Start the dashboard: python dashboard.py in the mudrex-bot folder (${error}).` : "Loading live data."}</span>
          </div>
        </div>
      </header>
    </div>
  );
}

function useHashView(): [View, (v: View) => void] {
  const read = () => {
    const h = typeof window === "undefined" ? "" : window.location.hash.slice(1);
    return (VIEWS.some(([k]) => k === h) ? h : "overview") as View;
  };
  const [view, setView] = useState<View>(read);
  useEffect(() => {
    const on = () => setView(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [view, (v) => { window.location.hash = v; setView(v); }];
}

function Kpi({ icon: I, tone, label, value, hint, children }: { icon: LucideIcon; tone: string; label: string; value: React.ReactNode; hint: React.ReactNode; children?: React.ReactNode }) {
  return (
    <section className="card kpi-card">
      <div className="kpi-top"><span className={`kpi-icon ${tone}`}><I size={18} /></span><span className="eyebrow">{label}</span></div>
      <div className="kpi-value">{value}</div>
      <div className="kpi-hint">{hint}</div>
      {children && <div className="kpi-foot">{children}</div>}
    </section>
  );
}

function IdeaCard({ o, dryRun }: { o: Idea; dryRun: boolean }) {
  return (
    <div className="idea">
      <div className="idea-top">
        {sideBadge(o.side)}<strong>{o.coin}</strong><span className="muted">{o.verb}</span>
        <span className={`badge ${o.placed && !dryRun ? "badge-good" : "badge-warning"}`}>{o.placed && !dryRun ? "being placed" : "dry run · not placed"}</span>
      </div>
      <div className="idea-grid">
        <Stat label="Entry near" value={`$${o.entry}`} />
        <Stat label="🎯 Target" value={`$${o.target}`} note={o.targetPct} tone="positive" />
        <Stat label="🛑 Stop-loss" value={`$${o.stop}`} note={o.stopPct} tone="negative" />
        <Stat label="Size" value={o.size} note={`${o.leverage} leverage`} />
        <Stat label="Max loss" value={o.risk} note="if stop-loss hits, incl. fees" />
      </div>
    </div>
  );
}

function Dashboard() {
  const [view, setView] = useHashView();
  const [range, setRange] = useState<Range>("7D");
  const { data, error, updated } = useLive();
  const tick = useTick(updated);
  if (!data) return <Connecting error={error} />;
  const { headline: h, money: m, today, next, market } = data;
  const chart = data.charts[range];
  const moodIcon = market.mood === "down" ? TrendingDown : TrendingUp;
  const botPositions = data.positions.filter((p) => p.bot).length;

  return (
    <div className="app-shell">
      <header className={`statusbar ${h.tone === "good" ? "" : h.tone === "bad" ? "status-problem" : "status-warning"}`} role="status" aria-live="polite">
        <div className="status-inner">
          <span className="status-mark" aria-hidden="true">{h.tone === "good" ? <ShieldCheck size={16} /> : <AlertCircle size={16} />}</span>
          <div className="status-copy">
            <strong>{h.title}</strong>
            <span>Last check {data.markedAt} · page refreshes every 30 s</span>
          </div>
          <span className={`badge ${h.live ? "badge-good" : "badge-warning"}`}><span className="badge-dot" aria-hidden="true" />{h.stop ? "STOP on · dry run" : h.live ? "Live" : "Live off"}</span>
        </div>
      </header>
      {error && (
        <div className="offline-banner" role="alert">
          <AlertCircle size={16} /> Lost contact with the bot ({error}). Showing data from {updated?.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }) ?? "earlier"}.
        </div>
      )}

      <main className="dashboard-shell">
        <div className="page-heading">
          <div>
            <div className="brand-lockup">
              <span className="brand-mark"><Activity size={18} strokeWidth={2.3} /></span>
              <span>mudrex <i>bot</i></span>
              <span className="environment-tag">{data.strategy.live}</span>
            </div>
            <h1>Your trading bot, <span>in plain words.</span></h1>
            <p>{data.dateLabel} · all times India (IST)</p>
          </div>
        </div>

        <nav className="range-tabs view-tabs" role="tablist" aria-label="Dashboard section">
          {VIEWS.map(([key, label]) => (
            <button key={key} type="button" role="tab" aria-selected={view === key} className={view === key ? "range-active" : ""} onClick={() => setView(key)}>{label}</button>
          ))}
        </nav>

        {view === "trades" && <TradesView t={data.trades} />}
        {view === "paper" && <PaperView paper={data.paper} />}
        {view === "how" && <HowView s={data.strategy} />}
        {view === "logs" && <LogsView logs={data.paper.logs} />}
        {view === "overview" && (
          <div className="dashboard-grid">
            <section className={`card span-12 hero hero-${h.tone}`}>
              <div className="eyebrow">What is the bot doing right now?</div>
              <h2>{h.title}</h2>
              <p>{h.detail}</p>
              <div className="hero-meta">
                <span><CalendarClock size={15} /> New trading day in <strong>{hms(today.resetInSec - tick)}</strong> (00:00 IST)</span>
                {today.nextCheckInSec !== null && <span><Moon size={15} /> Next market scan in <strong>{hms(Math.max(0, today.nextCheckInSec - tick))}</strong></span>}
              </div>
            </section>

            <div className="kpi-grid">
              <Kpi icon={Wallet} tone="indigo" label="Bot balance" value={m.equity}
                hint={<>{m.allocation} given to the bot, plus everything it has won or lost. <span className={sign(m.equityChange)}>{m.equityChange}</span> overall.</>} />
              <Kpi icon={Gauge} tone={sign(m.today) || "amber"} label="Today's result" value={<span className={sign(m.today)}>{m.today}</span>}
                hint={`${m.todayPct} since 00:00 IST. The bot rests for the day at −${m.limit} or +${m.limit}.`}>
                <div className="gauge-wrap">
                  <div className="gauge" role="img" aria-label={`Today ${m.today} between minus and plus ${m.limit}`}><span className="gauge-marker" style={{ left: `${m.limitMarker}%` }} /></div>
                  <div className="gauge-labels"><span>−{m.limit} stop</span><span>0</span><span>+{m.limit} stop</span></div>
                </div>
              </Kpi>
              <Kpi icon={BriefcaseBusiness} tone="" label="Trades today" value={<>{today.setsDone}<span className="kpi-unit">of {today.setsTarget} planned</span></>}
                hint={`Aims for ${today.setsTarget} a day, up to ${today.setsAuto} on its own. A 4th needs your Telegram OK. Weak days are skipped.`}>
                <div className="dots" aria-hidden="true">
                  {Array.from({ length: today.setsAuto + 1 }, (_, i) => <span key={i} className={i < today.setsDone ? "dot-on" : i >= today.setsAuto ? "dot-ask" : ""} />)}
                </div>
              </Kpi>
              <Kpi icon={moodIcon} tone={market.mood === "up" ? "positive" : market.mood === "down" ? "negative" : "amber"} label="Market mood"
                value={market.mood === "up" ? "Healthy ▲" : market.mood === "down" ? "Weak ▼" : "Unknown"}
                hint={market.mood === "up" ? "Bot only buys (long) today." : market.mood === "down" ? "Bot only sells short today." : "Bot opens nothing."}>
                <span className="kpi-meta">BTC {market.btcPrice} · {market.diff} vs 200-day avg</span>
              </Kpi>
            </div>

            <section className={`card span-12 ${next.needsApproval ? "action-waiting" : ""}`}>
              <Head eyebrow={`Latest plan · ${next.planTime}`} title={next.ideas.length ? "Next trade idea" : "No trade idea right now"}
                note={next.ideas.length
                  ? next.dryRun ? "The bot WOULD place this trade, but the STOP switch is on (dry run), so nothing is sent to Mudrex."
                    : next.needsApproval ? "This is a 4th trade today: tap Approve in Telegram within 15 minutes, or it is skipped."
                    : next.blocked ? `Not placed: ${next.blocked}` : "Placed automatically with its target and stop-loss."
                  : next.blocked ? `New trades are paused: ${next.blocked}` : "No coin is moving strongly enough in the allowed direction. The bot checks again every 15 minutes — no trade is better than a bad trade."}
                badge={next.needsApproval ? "Needs your tap" : undefined} />
              {next.ideas.map((o) => <IdeaCard key={o.coin} o={o} dryRun={next.dryRun} />)}
              {next.closes.map((c) => <p key={c.coin} className="muted">🔒 Closing {c.coin}: {c.reason}</p>)}
            </section>

            <section className="card span-12">
              <Head eyebrow="Real money · Mudrex INR futures" title="Open trades" badge={`${data.positions.length} open`}
                note="Every open position on your account. Ones you opened yourself are marked “yours” — the bot never touches them." />
              <Table
                head={["Coin", "Direction", "Who", "Price now", "Entry", "Value", "🎯 Target", "🛑 Stop-loss", "Result so far"]}
                empty={botPositions === 0 ? "No open trades. The bot is waiting for a good setup." : ""}
                rows={data.positions.map((p) => [
                  <strong key="c">{p.coin}</strong>, sideBadge(p.side), p.bot ? "bot" : "yours", `$${p.price}`, `$${p.entry}`,
                  <span key="v">{p.value}<span className="coin-pair"> · {p.leverage}</span></span>,
                  p.target === "none" ? "—" : `$${p.target}`,
                  p.protected ? `$${p.stop}` : <span key="s" className="badge badge-negative">none!</span>,
                  <span key="p" className={sign(p.pnl)}>{p.pnl} ({p.pnlPct})</span>,
                ])}
              />
            </section>

            <section className="card span-7">
              <div className="section-title-row">
                <div><div className="eyebrow">Real money · balance over time</div><h2>Bot balance</h2><p>{chart.startLabel} · change <span className={sign(chart.change)}>{chart.change}</span></p></div>
                <div className="range-tabs" role="group" aria-label="Chart period">
                  {(Object.keys(data.charts) as Range[]).map((r) => <button key={r} type="button" className={range === r ? "range-active" : ""} onClick={() => setRange(r)}>{r}</button>)}
                </div>
              </div>
              <Chart points={chart.points} label="Bot balance" />
            </section>

            <section className="card span-5">
              <Head eyebrow="Market" title="Market mood" note={market.asOf ? `Bitcoin daily close ${market.asOf}` : undefined} />
              <p className="mood-text">{market.text}</p>
              <div className="gauge-wrap">
                <div className="gauge" role="img" aria-label={`BTC ${market.diff} versus its 200-day average`}><span className="gauge-marker" style={{ left: `${market.marker}%` }} /></div>
                <div className="gauge-labels"><span>Weak (shorts)</span><span>BTC {market.diff}</span><span>Healthy (buys)</span></div>
              </div>
              <div className="stat-row compact">
                <Stat label="Bitcoin" value={market.btcPrice} note={<span className={sign(market.btcChange)}>{market.btcChange} yesterday</span>} />
                <Stat label="vs 200-day average" value={market.diff} />
              </div>
            </section>

            <section className="card span-7">
              <Head eyebrow="Safety" title="Safety checklist" note="Green = fine. Amber = intentionally off or worth knowing. Red = check now." />
              <CheckList items={data.safety} />
            </section>

            <section className="card span-5">
              <Head eyebrow="Is everything running?" title="Bot health" note="The bot is made of small programs running on this PC." />
              <CheckList items={data.health} />
            </section>

            <section className="card span-12">
              <Head eyebrow="Journal" title="Recent activity" note="The latest events from the bot's own records." badge={`${data.activity.length} events`} />
              {data.activity.length === 0 && <p className="muted">Nothing recorded yet.</p>}
              <ol className="activity-list">
                {data.activity.map((e, i) => (
                  <li className="activity-item" key={i}>
                    <span className={`activity-icon event-${e.tone}`} aria-hidden="true"><Info size={16} /></span>
                    <div className="activity-copy">
                      <div><span className="badge badge-neutral">{e.kind}</span><time>{e.time}</time></div>
                      <p>{e.detail}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          </div>
        )}

        <footer className="page-footer">
          <span><span className="footer-status" />Read-only: this page can never place or change a trade</span>
          <span>Updated {updated?.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }) ?? "—"} IST</span>
        </footer>
      </main>
    </div>
  );
}
