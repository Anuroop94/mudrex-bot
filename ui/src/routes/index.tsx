import { createFileRoute } from "@tanstack/react-router";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  ArrowUpRight,
  BriefcaseBusiness,
  Check,
  ChevronDown,
  Clock3,
  Gauge,
  Info,
  Landmark,
  Shield,
  ShieldAlert,
  ShieldCheck,
  TrendingUp,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { sign, useLive, type Live } from "../lib/live";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Mudrex Monitor — Bot Dashboard" },
      {
        name: "description",
        content:
          "Live, read-only monitor for the Mudrex S1 bot. Data comes from the bot's own journal and Mudrex read-only calls.",
      },
    ],
  }),
  component: MudrexDashboard,
});

type Range = keyof Live["charts"];
const activityIcons: Record<string, LucideIcon> = {
  Activity,
  ShieldAlert,
  ShieldCheck,
  TrendingUp,
};

const ranges: Range[] = ["1D", "7D", "30D"];

function money(value: number) {
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function Connecting({ error }: { error: string | null }) {
  return (
    <div className="app-shell">
      <header className={`statusbar ${error ? "status-problem" : "status-warning"}`} role="status" aria-live="polite">
        <div className="status-inner">
          <span className="status-mark" aria-hidden="true"><AlertCircle size={16} /></span>
          <div className="status-copy">
            <strong>{error ? "Cannot reach the bot" : "Connecting to the bot…"}</strong>
            <span>
              {error
                ? `Start the bot dashboard server: python dashboard.py in the mudrex-bot folder (${error}).`
                : "Loading live data from the bot."}
            </span>
          </div>
        </div>
      </header>
    </div>
  );
}

function age(sec: number | null) {
  if (sec === null) return "never";
  return sec < 90 ? `${sec}s ago` : `${Math.round(sec / 60)} min ago`;
}

function MudrexDashboard() {
  const [range, setRange] = useState<Range>("1D");
  const { data, error, updated } = useLive();
  if (!data) return <Connecting error={error} />;
  const status = data.status;
  const metrics = data.metrics;
  const market = data.market;
  const risk = data.risk;
  const plan = data.plan;
  const strategy = data.strategy;
  const chart = data.charts[range];
  const series = chart.points;

  const settings: [string, string][] = [
    ["Signal", strategy.signal],
    ["Market filter", strategy.marketFilter],
    ["Basket", strategy.basket],
    ["Entry mode", strategy.entryMode],
    ["Stop model", strategy.stopModel],
    ["Leverage", strategy.leverage],
    ["Safety stop", strategy.stop],
    ["BTC filter", strategy.btcFilter],
    ["Allocation cap", strategy.allocationCap],
    ["Target", strategy.target],
    ["Stop-loss budget", strategy.tradeRisk],
  ];

  return (
    <div className="app-shell">
      <header
        className={`statusbar ${status.ok ? "" : status.issues.length ? "status-problem" : "status-warning"}`}
        role="status"
        aria-live="polite"
      >
        <div className="status-inner">
          <span className="status-mark" aria-hidden="true">
            {status.ok ? <ShieldCheck size={16} /> : <AlertCircle size={16} />}
          </span>
          <div className="status-copy">
            <strong>
              {status.ok
                ? `All good — watching ${metrics.positionCount} position${metrics.positionCount === 1 ? "" : "s"} — live trading is ${status.live ? "ON" : "OFF"}`
                : status.issues.join(" · ")}
            </strong>
            <span>
              Watcher {age(status.watcherAgeSec)} · Telegram approver {age(status.approverAgeSec)} · marked{" "}
              {data.markedAt}
            </span>
          </div>
          <span className={`badge ${status.live && !status.stop ? "badge-good" : "badge-warning"}`}>
            <span className="badge-dot" aria-hidden="true" />
            {status.stop ? "STOP on" : status.live ? "Live" : "Live off"}
          </span>
        </div>
      </header>
      {error && (
        <div className="offline-banner" role="alert">
          <AlertCircle size={16} /> Lost contact with the bot ({error}). Showing data from{" "}
          {updated?.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }) ?? "earlier"}.
        </div>
      )}

      <main className="dashboard-shell">
        <div className="page-heading">
          <div>
            <div className="brand-lockup">
              <span className="brand-mark">
                <Activity size={18} strokeWidth={2.3} />
              </span>
              <span>
                mudrex <i>monitor</i>
              </span>
              <span className="environment-tag">MONITOR</span>
            </div>
            <h1>
              Your bot, <span>at a glance.</span>
            </h1>
            <p>Know what’s happening, how your money is doing, and when you need to act.</p>
          </div>
          <div className="account-meta">
            <span className="account-icon">
              <Landmark size={17} />
            </span>
            <span>
              <strong>Futures account</strong>
              <small>Mudrex · INR · live, read-only</small>
            </span>
          </div>
        </div>

        <div className="dashboard-grid">
          <section className={`card action-card ${plan.needsAction ? "action-waiting" : ""}`} aria-labelledby="action-title">
            <div className="action-symbol" aria-hidden="true">
              <Clock3 size={21} />
            </div>
            <div className="action-main">
              <div className="eyebrow">{plan.needsAction ? "One thing needs your attention" : "Latest plan"}</div>
              <h2 id="action-title">
                {plan.title}
                {plan.state ? ` — ${plan.state.replace(/_/g, " ").toLowerCase()}` : ""}
              </h2>
              <p>
                {plan.message} <span className="coin-pair">{plan.time} · {plan.decisionLabel}</span>
              </p>
              <div className="order-list">
                {plan.items.map((item) => {
                  const idle = !("count" in item) || !item.count;
                  return (
                    <div className="order-row" key={`${item.label}-${item.detail}`}>
                      <div className="order-coin">
                        <span className="coin-avatar">{(item.label.split(" ")[1] ?? item.label).slice(0, 1)}</span>
                        <strong>{item.label}</strong>
                        <span className={`badge ${item.detail.startsWith("verified") ? "badge-good" : item.detail.startsWith("failed") ? "badge-warning" : "badge-neutral"}`}>
                          {item.detail}
                        </span>
                      </div>
                      <div className="order-amount">
                        <strong>{idle ? "—" : item.count}</strong>
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="approval-instruction">
                <Info size={17} />
                <span>
                  This page is read-only. Approve, reject, /plan, /status and /stop are in Telegram.
                </span>
              </div>
            </div>
            <span className={`badge ${plan.needsAction ? "badge-warning" : "badge-neutral"}`}>
              <span className="badge-dot" aria-hidden="true" />
              {plan.needsAction ? "Action needed" : "No action"}
            </span>
          </section>

          <div className="kpi-grid">
            <section className="card kpi-card">
              <div className="kpi-top">
                <span className="kpi-icon indigo"><Wallet size={18} /></span>
                <span className="eyebrow">Bot balance</span>
              </div>
              <div className="kpi-value indigo">
                {metrics.equityMajor}<span className="decimal">{metrics.equityDecimal}</span>
              </div>
              <div className="kpi-hint">₹5,000 allocation plus the bot’s own profit and loss (open and closed). {metrics.equityChange} {metrics.equityComparison}.</div>
              <div className="kpi-foot">
                <span className="kpi-meta"><span className="mini-dot" /> Mudrex · INR account</span>
              </div>
            </section>
            <section className="card kpi-card">
              <div className="kpi-top">
                <span className="kpi-icon positive"><ArrowUpRight size={18} /></span>
                <span className="eyebrow">Money made today</span>
              </div>
              <div className={`kpi-value ${sign(metrics.todayPnlMajor)}`}>
                {metrics.todayPnlMajor}<span className="decimal">{metrics.todayPnlDecimal}</span>
              </div>
              <div className="kpi-hint">{metrics.todayPnlChange} compared with day start.</div>
              <div className="kpi-foot">
                <span className={`change-tag ${sign(metrics.todayPnlChange)}`}>
                  {metrics.todayPnlChange.startsWith("-") ? "↓ Down" : "↑ Up"} · {metrics.todayPnlChange}
                </span>
              </div>
            </section>
            <section className="card kpi-card">
              <div className="kpi-top">
                <span className="kpi-icon"><BriefcaseBusiness size={18} /></span>
                <span className="eyebrow">Open positions</span>
              </div>
              <div className="kpi-value indigo">
                {metrics.positionCount}<span className="kpi-unit">/ {metrics.basketSize}</span>
              </div>
              <div className="kpi-hint">Bot-owned S1 positions, marked {data.markedAt}.</div>
              <div className="kpi-foot">
                <span className="change-tag">{metrics.positionCaption}</span>
              </div>
            </section>
            <section className="card kpi-card">
              <div className="kpi-top">
                <span className="kpi-icon amber"><Gauge size={18} /></span>
                <span className="eyebrow">Daily limit</span>
              </div>
              <div className="kpi-value indigo">{risk.threshold}</div>
              <div className="kpi-hint">The daily profit and loss guardrail.</div>
              <div className="kpi-foot">
                <div className="gauge-wrap">
                  <div className="gauge" role="img" aria-label={`Daily P&L ${risk.dailyPnl} against a threshold of ${risk.threshold}`}>
                    <span className="gauge-marker" style={{ left: `${Math.min(100, Math.max(0, risk.utilizationPercent))}%` }} />
                  </div>
                  <div className="gauge-labels">
                    <span>−{risk.threshold.replace("±", "")}</span><span>Today {risk.dailyPnl}</span><span>+{risk.threshold.replace("±", "")}</span>
                  </div>
                </div>
              </div>
            </section>
          </div>

          <section className="card positions-card">
            <div className="section-title-row">
              <div>
                <div className="eyebrow">Live · Mudrex INR futures</div>
                <h2>Open positions</h2>
                <p>Every open position on your INR futures account. Manual ones are marked; the bot never touches them.</p>
              </div>
              <span className="badge badge-neutral">{data.positions.length} open</span>
            </div>
            <div className="table-scroll">
              <table className="data-table positions-table">
                <thead>
                  <tr>
                    <th>Coin</th><th>Side</th><th className="numeric">Mark price</th>
                    <th className="numeric">Entry</th><th className="numeric">Notional</th>
                    <th className="numeric">Stop-loss</th><th className="numeric">Unrealized P&amp;L</th>
                    <th>Protection</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.length === 0 && (
                    <tr>
                      <td colSpan={8}>No open positions.</td>
                    </tr>
                  )}
                  {data.positions.map((position) => (
                    <tr key={position.symbol}>
                      <td>
                        <div className="coin-cell">
                          <span className={`coin-avatar coin-${position.symbol.toLowerCase()}`}>{position.symbol.slice(0, 1)}</span>
                          <strong>{position.symbol}</strong><span className="coin-pair">{position.name}</span>
                        </div>
                      </td>
                      <td><span className={`badge ${position.side === "LONG" ? "badge-good" : "badge-negative"}`}>{position.side === "LONG" ? "Long" : "Short"}</span></td>
                      <td className="numeric">${position.price}</td>
                      <td className="numeric">${position.entry}</td>
                      <td className="numeric">{position.notional}<span className="coin-pair"> · {position.leverage}</span></td>
                      <td className="numeric">${position.stop}<span className="coin-pair"> · {position.stopLabel}</span></td>
                      <td className="numeric"><span className={sign(position.pnl)}>{position.pnl} {position.pnlPct}</span></td>
                      <td><span className={`badge ${position.state === "Protected" ? "badge-good" : "badge-warning"}`}>{position.state}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="card equity-card">
            <div className="section-title-row">
              <div>
                <div className="eyebrow">Performance · live</div>
                <h2>Equity curve</h2>
                <p>Bot equity, recorded by the watcher every 5 minutes.</p>
              </div>
              <div className="range-tabs" role="group" aria-label="Equity curve period">
                {ranges.map((item) => (
                  <button key={item} type="button" className={range === item ? "range-active" : ""} onClick={() => setRange(item)}>{item}</button>
                ))}
              </div>
            </div>
            <div className="paper-summary">
              <div>
                <span className="summary-label">Equity</span>
                <strong>{metrics.equityMajor}{metrics.equityDecimal}</strong>
                <span className="summary-note">{chart.startLabel}</span>
              </div>
              <div className="return-box">
                <span className="summary-label">Period return</span>
                <strong className={sign(chart.change)}>{chart.change}</strong>
                <span className={`return-note ${sign(chart.change)}`}>
                  {chart.change.startsWith("-") ? "↓ Below start" : chart.change.startsWith("+") ? "↑ Above start" : "Flat"}
                </span>
              </div>
            </div>
            <div className="chart-legend">
              <span><i className="legend-square" />Equity (₹)</span><span>{series.length} points</span>
            </div>
            <div className="equity-chart" role="img" aria-label="Bot equity history chart over time, in Indian rupees.">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={series} margin={{ top: 14, right: 12, left: 3, bottom: 0 }}>
                  <defs>
                    <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--chart-green)" stopOpacity={0.18} />
                      <stop offset="100%" stopColor="var(--chart-green)" stopOpacity={0.01} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="4 5" />
                  <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={30} tick={{ fill: "var(--muted)", fontSize: 12 }} tickMargin={10} />
                  <YAxis domain={["dataMin - 100", "dataMax + 80"]} tickFormatter={(value: number) => `₹${(value / 1000).toFixed(1)}k`} tickLine={false} axisLine={false} width={64} tick={{ fill: "var(--muted)", fontSize: 12 }} />
                  <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--muted)", strokeDasharray: "4 4" }} />
                  <Area type="monotone" dataKey="value" stroke="var(--chart-green)" strokeWidth={2.5} fill="url(#equityFill)" activeDot={{ r: 4, strokeWidth: 2 }} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div className="chart-axis-note">Time (IST) <span>•</span> Bot equity in Indian rupees</div>
            <div className="plan-preview">
              <div><span className="plan-check"><Check size={14} /></span><strong>Latest plan</strong></div>
              {plan.items.map((item) => <span key={`${item.label}-${item.detail}`}>{item.label} — {item.detail}</span>)}
            </div>
          </section>

          <section className="card regime-card">
            <div className="section-title-row">
              <div>
                <div className="eyebrow">Market context</div><h2>Regime monitor</h2><p>{market.statusCaption}</p>
              </div>
              <span className="badge badge-indigo">Live</span>
            </div>
            <div className="regime-hero">
              <span className="regime-icon" aria-hidden="true"><TrendingUp size={18} /></span>
              <div className="regime-copy"><strong>{market.status}</strong><span>BTC / USDT · {market.btcPrice}</span></div>
              <span className={`change-tag ${sign(market.btcChange)}`}>{market.btcChange}</span>
            </div>
            <div className="gauge-wrap regime-gauge">
              <div className="gauge" role="img" aria-label={`${market.averageLabel}: ${market.averageDifference}`}>
                <span className="gauge-marker" style={{ left: `${market.markerPercent}%` }} />
              </div>
              <div className="gauge-labels"><span>Below average</span><span>{market.averageDifference}</span><span>Above average</span></div>
            </div>
            <div className="leader-list regime-list">
              <article className="leader-row">
                <div className="leader-info"><div className="leader-name">Basket breadth</div><span>Coins with trend up in the latest plan</span></div>
                <div className="leader-return positive">{market.breadthAbove}/{market.breadthTotal}<span>{market.breadthCaption}</span></div>
              </article>
              <article className="leader-row">
                <div className="leader-info"><div className="leader-name">BTC mood gate</div><span>BTC daily close vs its 200-day average</span></div>
                <div className="leader-return">{market.moodGate}<span>{market.moodCaption}</span></div>
              </article>
            </div>
            <p className="fine-print">*When BTC closes below its 200-day average the bot holds nothing and buys nothing. It cannot predict the future.</p>
          </section>

          <section className="card history-card">
            <div className="section-title-row">
              <div><div className="eyebrow">Recent events</div><h2>Activity</h2><p>Latest entries from the bot’s journal.</p></div>
              <span className="badge badge-neutral">{data.activity.length} events</span>
            </div>
            <ol className="activity-list">
              {data.activity.map((item, index) => {
                const Icon = activityIcons[item.icon] ?? Activity;
                return (
                  <li className="activity-item" key={`${item.title}-${index}`}>
                    <span className={`activity-icon event-${item.tone}`} aria-hidden="true"><Icon size={16} /></span>
                    <div className="activity-copy">
                      <div><span className="badge badge-neutral">{item.title}</span><time>{item.time}</time></div>
                      <p>{item.detail}</p>
                    </div>
                  </li>
                );
              })}
            </ol>
          </section>

          <section className="card strategy-card">
            <div className="section-title-row">
              <div><div className="eyebrow">Active strategy · {status.live ? "live" : "live off"}</div><h2>{strategy.name}</h2><p>{strategy.description}</p></div>
              <span className={`badge ${status.live ? "badge-good" : "badge-warning"}`}><span className="badge-dot" />{status.live ? "Live" : "Live off"}</span>
            </div>
            <div className="settings-grid strategy-settings">
              <div className="setting-row"><span>Leverage</span><strong>{strategy.leverage}</strong></div>
              <div className="setting-row"><span>Safety stop</span><strong>{strategy.stop}</strong></div>
              <div className="setting-row"><span>BTC filter</span><strong>{strategy.btcFilter}</strong></div>
              <div className="setting-row"><span>Allocation cap</span><strong>{strategy.allocationCap}</strong></div>
            </div>
            <p className="read-only-note"><Shield size={15} />Target: {strategy.target}. Changes are made in the bot’s code, not here.</p>
          </section>

          <details className="card details-card">
            <summary>
              <span><span className="eyebrow">For the curious</span><strong>Strategy and safety settings</strong><small>Read-only · from the bot’s code · click to expand</small></span>
              <span className="details-chevron"><ChevronDown size={18} /></span>
            </summary>
            <div className="details-body">
              <div className="settings-grid">
                {settings.map(([label, value]) => <div className="setting-row" key={label}><span>{label}</span><strong>{value}</strong></div>)}
                <div className="setting-row"><span>Connection</span><strong className={error ? "negative" : "positive"}>{error ? "Lost" : "Connected"}</strong></div>
              </div>
              <p className="read-only-note"><ShieldAlert size={15} />Shown for reference. Past results are not a guarantee of future returns.</p>
            </div>
          </details>
        </div>

        <footer className="page-footer">
          <span><span className="footer-status" />Your dashboard is read-only</span>
          <span>Refreshes every 30 s · updated {updated?.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }) ?? "—"} <span>·</span> All times IST</span>
        </footer>
      </main>
    </div>
  );
}

function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { value: number }[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  const first = payload[0];
  if (!first) return null;
  return (
    <div className="chart-tooltip">
      <span>{label}</span><strong>{money(first.value)}</strong><small>Bot equity</small>
    </div>
  );
}
