import { BookOpen, CheckCircle2, CircleAlert, FlaskConical, MessageCircle, ScrollText, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { sign, type Check, type Live, type Point } from "../lib/live";

// ---------- shared parts

export function Head({ eyebrow, title, note, badge }: { eyebrow: string; title: ReactNode; note?: ReactNode; badge?: ReactNode }) {
  return (
    <div className="section-title-row">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h2>{title}</h2>
        {note && <p>{note}</p>}
      </div>
      {badge && <span className="badge badge-neutral">{badge}</span>}
    </div>
  );
}

export function Table({ head, rows, empty = "Nothing yet." }: { head: string[]; rows: ReactNode[][]; empty?: string }) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>{head.map((h) => <th key={h}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr><td colSpan={head.length} className="empty-row">{empty}</td></tr>
          )}
          {rows.map((r, i) => (
            <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export const pnl = (s: string) => <span className={sign(s)}>{s}</span>;

export const sideBadge = (side: string) => (
  <span className={`badge ${side === "LONG" ? "badge-good" : "badge-negative"}`}>{side === "LONG" ? "▲ Long" : "▼ Short"}</span>
);

export function Chart({ points, label = "Balance" }: { points: Point[]; label?: string }) {
  if (points.length < 2)
    return <div className="chart-empty">Not enough data for a chart yet — it fills in as the bot runs.</div>;
  const up = points[points.length - 1]!.value >= points[0]!.value;
  const color = up ? "var(--chart-green)" : "var(--chart-red)";
  const id = `fill-${label.replace(/\W/g, "")}-${up ? "u" : "d"}`;
  return (
    <div className="equity-chart" role="img" aria-label={`${label} over time, in rupees`}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 14, right: 12, left: 3, bottom: 0 }}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.2} />
              <stop offset="100%" stopColor={color} stopOpacity={0.01} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="4 5" />
          <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={30} tick={{ fill: "var(--muted)", fontSize: 12 }} tickMargin={10} />
          <YAxis domain={["auto", "auto"]} tickFormatter={(v: number) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`}
            tickLine={false} axisLine={false} width={72} tick={{ fill: "var(--muted)", fontSize: 12 }} />
          <Tooltip content={<ChartTip series={label} />} cursor={{ stroke: "var(--muted)", strokeDasharray: "4 4" }} />
          <Area type="monotone" dataKey="value" stroke={color} strokeWidth={2.5} fill={`url(#${id})`} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function ChartTip({ active, payload, label, series }: { active?: boolean; payload?: { value: number }[]; label?: string; series: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <span>{label}</span>
      <strong>₹{payload[0]!.value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</strong>
      <small>{series}</small>
    </div>
  );
}

const icon = { ok: CheckCircle2, warn: CircleAlert, bad: XCircle };

export function CheckList({ items }: { items: Check[] }) {
  return (
    <ul className="check-list">
      {items.map((c) => {
        const I = icon[c.state];
        return (
          <li key={c.name} className={`check-${c.state}`}>
            <I size={19} aria-hidden="true" />
            <div>
              <div className="check-top"><strong>{c.name}</strong><span>{c.detail}</span></div>
              <small>{c.help}</small>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export function Stat({ label, value, note, tone = "" }: { label: string; value: ReactNode; note?: ReactNode; tone?: string }) {
  return (
    <div className="stat">
      <span className="summary-label">{label}</span>
      <strong className={tone}>{value}</strong>
      {note && <span className="summary-note">{note}</span>}
    </div>
  );
}

// ---------- Trades tab: real (live) trades the bot closed

export function TradesView({ t }: { t: Live["trades"] }) {
  return (
    <div className="dashboard-grid">
      <section className="card span-12">
        <Head eyebrow="Real money · Mudrex" title="Trade history" note="Every real trade the bot has closed, newest first." badge={`${t.count} closed`} />
        <div className="stat-row">
          <Stat label="Closed trades" value={t.count} />
          <Stat label="Win rate" value={t.winRate} note={`${t.wins} won · ${t.count - t.wins} lost`} />
          <Stat label="Total result" value={t.total} tone={sign(t.total)} />
          <Stat label="Best trade" value={t.best} tone={sign(t.best)} />
          <Stat label="Worst trade" value={t.worst} tone={sign(t.worst)} />
        </div>
        <Table
          head={["Coin", "Opened", "Closed", "Held (days)", "Entry price", "Exit price", "Result"]}
          empty="No real trades closed yet. They appear here as soon as the bot closes one."
          rows={t.rows.map((r) => [
            <strong key="c">{r.win ? "✅" : "❌"} {r.coin}</strong>, r.opened, r.closed, r.days, `$${r.entry}`, `$${r.exit}`, pnl(r.pnl),
          ])}
        />
      </section>
    </div>
  );
}

// ---------- Paper tab: S4 and S1 with pretend money

export function PaperView({ paper }: { paper: Live["paper"] }) {
  const { s1, s4 } = paper;
  return (
    <div className="dashboard-grid">
      <section className="card span-12 explain">
        <FlaskConical size={18} />
        <p>
          <strong>Paper trading = practice with pretend money.</strong> Each strategy follows its real rules on real
          prices, but no order ever reaches Mudrex. It shows how a strategy behaves before real money is used.
          A few days of results mean very little — look at weeks and months.
        </p>
      </section>

      <section className="card span-7">
        <Head eyebrow="Paper · S4 intraday sets · updates hourly" title={<>S4 paper account <span className={sign(s4.total)}>{s4.total}</span></>}
          note={`Started with ${s4.start} on 28 Sep · ${s4.coins} coins watched · updated ${s4.updated}`}
          badge={s4.blocked ? "Resting (₹500 line hit)" : "Active"} />
        <div className="stat-row">
          <Stat label="Value now" value={s4.value} note="includes open trade" />
          <Stat label="Profit / loss" value={s4.pnl} tone={sign(s4.pnl)} />
          <Stat label="Win rate" value={s4.winRate} note={`${s4.wins} won · ${s4.losses} lost`} />
          <Stat label="Sets" value={`${s4.setsToday} today`} note={`${s4.setsTotal} in total`} />
        </div>
        <Chart points={s4.curve} label="S4 paper value" />
      </section>

      <section className="card span-5">
        <Head eyebrow="Paper S4 · open now" title="Open paper trade" note="S4 holds at most one set at a time." />
        {s4.open.length === 0 && <p className="muted">No open paper trade. S4 waits for a strong mover.</p>}
        {s4.open.map((o) => (
          <div className="idea" key={o.coin}>
            <div className="idea-top">{sideBadge(o.side)}<strong>{o.coin}</strong><span className="muted">since {o.since}</span></div>
            <div className="idea-grid">
              <Stat label="Entry" value={`$${o.entry}`} />
              <Stat label="🎯 Target" value={`$${o.target}`} />
              <Stat label="🛑 Stop-loss" value={`$${o.stop}`} />
              <Stat label="Result so far" value={o.pnl} tone={sign(o.pnl)} />
            </div>
          </div>
        ))}
      </section>

      <section className="card span-12">
        <Head eyebrow="Paper S4 · closed" title="S4 paper trades" note="Why each trade ended: target reached, stop-loss hit, or the 72-hour limit." badge={`${s4.trades.length} shown`} />
        <Table
          head={["Coin", "Direction", "Opened", "Closed", "Entry", "Exit", "Why it closed", "Result"]}
          empty="No closed paper trades yet."
          rows={s4.trades.map((t) => [<strong key="c">{t.coin}</strong>, sideBadge(t.side), t.opened, t.closed, `$${t.entry}`, `$${t.exit}`, t.why, pnl(t.pnl)])}
        />
      </section>

      <section className="card span-7">
        <Head eyebrow="Paper · S1 daily trend · updates daily 05:40 IST" title={<>S1 paper account <span className={sign(s1.total)}>{s1.total}</span></>}
          note={`Started with ${s1.start} · decided on the ${s1.decision} daily close · updated ${s1.updated}`}
          badge={`${s1.positions.length} open`} />
        <div className="stat-row">
          <Stat label="Value now" value={s1.equity} />
          <Stat label="Return" value={s1.total} tone={sign(s1.total)} />
          <Stat label="Holding" value={s1.positions.map((p) => p.coin).join(", ") || "nothing"} />
        </div>
        <Chart points={s1.curve} label="S1 paper value" />
      </section>

      <section className="card span-5">
        <Head eyebrow="Paper S1 · positions" title="What S1 holds" note={`Today's plan: ${s1.plan.join(" · ") || "no orders"}`} />
        <Table head={["Coin", "Entry", "Stop-loss", "Value", "Since"]} empty="S1 holds nothing right now."
          rows={s1.positions.map((p) => [<strong key="c">{p.coin}</strong>, `$${p.entry}`, `$${p.stop}`, p.value, p.since])} />
      </section>

      <section className="card span-12">
        <Head eyebrow="Paper S1 · test versions" title="S1 and its test versions" note={s1.rule} badge={`${s1.rows.length} versions`} />
        <Table
          head={["Version", "What it changes", "Days", "Return", "Worst drop", "Closed trades", "Status"]}
          rows={s1.rows.map((r) => [
            <strong key="n">{r.name}</strong>, r.desc, r.days, pnl(r.total), r.maxDd, r.trades,
            <span key="v" className={`badge ${r.champion ? "badge-good" : r.verdict.startsWith("PROMOTE") ? "badge-warning" : "badge-neutral"}`}>{r.verdict}</span>,
          ])}
        />
      </section>
    </div>
  );
}

// ---------- How it works: strategy, rules, glossary, Telegram

const GLOSSARY: [string, string][] = [
  ["Long (buy)", "You buy a coin hoping its price goes UP. Profit if it rises, loss if it falls."],
  ["Short (sell)", "You sell a coin you borrowed, hoping its price goes DOWN. Profit if it falls, loss if it rises."],
  ["Stop-loss (SL)", "A price where the trade closes automatically to cap the loss. Placed on Mudrex, so it works even if this PC is off."],
  ["Target / take-profit (TP)", "A price where the trade closes automatically to lock in the profit."],
  ["Set", "One trade idea taken by the bot (S4 takes one coin per set). The bot aims for 2 sets a day, max 3 on its own."],
  ["Leverage", "Borrowed buying power. 2× means a ₹1,000 margin controls ₹2,000 of coin. It magnifies gains AND losses; the bot sizes trades so the rupee risk stays fixed."],
  ["ATR (daily move)", "Average True Range: how much a coin normally moves in a day. Targets and stops are set as multiples of it, so jumpy coins get wider stops."],
  ["200-day average", "Bitcoin's average price over the last 200 days. Above it = healthy market (bot buys); below it = weak market (bot shorts)."],
  ["Dry run", "The bot does everything except placing the order. Trade ideas are shown, nothing is bought or sold."],
  ["Paper trading", "Trading with pretend money on real prices to test a strategy safely."],
  ["P&L", "Profit and Loss: how much money was made (+) or lost (−)."],
  ["Drawdown", "How far the balance has fallen from its highest point."],
  ["Fees", "Mudrex charges a small fee plus GST on every buy and sell, and a funding fee while a trade is open. All results here include them."],
];

const COMMANDS: [string, string][] = [
  ["/status", "Balance, today's result, safety switches and open trades."],
  ["/plan", "Ask the bot for a fresh trade plan at current prices."],
  ["/stop", "Emergency brake: turns the STOP switch on. Stop-losses on Mudrex stay active."],
  ["Approve / Reject", "Buttons under a trade message. Only needed for a 4th trade in a day."],
  ["Resume", "Only on the PC (python ops.py resume) — never from the phone, for safety."],
];

export function HowView({ s }: { s: Live["strategy"] }) {
  return (
    <div className="dashboard-grid">
      <section className="card span-7">
        <Head eyebrow={`Live strategy · ${s.live}`} title={s.name} note="What the bot does, step by step." />
        <ol className="steps">{s.steps.map((x, i) => <li key={i}>{x}</li>)}</ol>
        <p className="read-only-note warn-note"><CircleAlert size={15} />{s.evidence}</p>
      </section>
      <section className="card span-5">
        <Head eyebrow="Owner's rules" title="Safety limits" note="Fixed in the bot's code. The bot cannot change them itself." />
        <div className="rule-list">
          {s.rules.map(([k, v, h]) => (
            <div className="rule" key={k}><div><span>{k}</span><strong>{v}</strong></div><small>{h}</small></div>
          ))}
        </div>
      </section>
      <section className="card span-7">
        <Head eyebrow="Plain words" title={<><BookOpen size={18} className="inline-icon" /> Trading words explained</>} />
        <dl className="glossary">
          {GLOSSARY.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
        </dl>
      </section>
      <section className="card span-5">
        <Head eyebrow="Your remote control" title={<><MessageCircle size={18} className="inline-icon" /> Telegram commands</>}
          note="Every trade, close and warning is also sent to your Telegram." />
        <div className="rule-list">
          {COMMANDS.map(([k, v]) => <div className="rule" key={k}><div><code>{k}</code></div><small>{v}</small></div>)}
        </div>
        <p className="read-only-note"><CircleAlert size={15} />This dashboard is read-only: it can never place, change or cancel a trade.</p>
      </section>
    </div>
  );
}

export function LogsView({ logs }: { logs: Record<string, string[]> }) {
  const about: Record<string, string> = {
    "watcher.log": "The 5-minute checker: plans, alerts, errors.",
    "approver.log": "Your Telegram taps and commands.",
    "s4_paper.log": "Hourly S4 paper results.",
    "s1_paper.log": "Daily S1 paper results.",
  };
  return (
    <div className="dashboard-grid">
      {Object.entries(logs).map(([name, lines]) => (
        <details className="card details-card" key={name} open={name === "watcher.log"}>
          <summary>
            <span>
              <span className="eyebrow">Log · last {lines.length} lines · newest first</span>
              <strong>{name}</strong>
              <small>{about[name] ?? ""}</small>
            </span>
            <ScrollText size={18} />
          </summary>
          <div className="details-body">
            <pre className="log-lines">{lines.slice().reverse().join("\n") || "empty"}</pre>
          </div>
        </details>
      ))}
    </div>
  );
}
