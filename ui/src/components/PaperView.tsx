import { FlaskConical, ScrollText } from "lucide-react";
import type { ReactNode } from "react";
import { sign, type Paper } from "../lib/live";

function Head({ eyebrow, title, note, badge }: { eyebrow: string; title: string; note: string; badge?: string }) {
  return (
    <div className="section-title-row">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h2>{title}</h2>
        <p>{note}</p>
      </div>
      {badge && <span className="badge badge-neutral">{badge}</span>}
    </div>
  );
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={head.length}>Nothing yet.</td>
            </tr>
          )}
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td key={j}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const pnl = (s: string) => <span className={sign(s)}>{s}</span>;

export function PaperView({ paper }: { paper: Paper }) {
  const { s1, s3 } = paper;
  return (
    <div className="dashboard-grid">
      <section className="card positions-card">
        <Head
          eyebrow="Paper trading · no real money"
          title="S1 and its challengers"
          note={s1.rule}
          badge={`${s1.rows.length} bots`}
        />
        <Table
          head={["Bot", "What it changes", "Days", "Return", "Worst drop", "Closed trades", "Weekly t vs S1", "Status"]}
          rows={s1.rows.map((r) => [
            <strong key="n">{r.name}</strong>,
            r.desc,
            r.days,
            pnl(r.total),
            r.maxDd,
            r.trades,
            r.t,
            <span key="v" className={`badge ${r.champion ? "badge-good" : r.verdict.startsWith("PROMOTE") ? "badge-warning" : "badge-neutral"}`}>
              {r.verdict}
            </span>,
          ])}
        />
      </section>

      <section className="card equity-card">
        <Head
          eyebrow="Paper S1 (champion)"
          title={`Paper equity ${s1.equity}`}
          note={`Decision on ${s1.decision} close · plan: ${s1.plan.join(", ") || "nothing"}`}
          badge={`${s1.positions.length} open`}
        />
        <Table
          head={["Coin", "Entry", "Stop-loss", "Value", "Since"]}
          rows={s1.positions.map((p) => [<strong key="c">{p.coin}</strong>, `$${p.entry}`, `$${p.stop}`, p.value, p.since])}
        />
      </section>

      <section className="card regime-card">
        <Head
          eyebrow="Paper S3 · your 2-trades-a-day ask"
          title={`${s3.equity} (${s3.total})`}
          note={`${s3.variant}. Started with ${s3.start}. Updated ${s3.updated}.`}
          badge={s3.blocked ? "Blocked today" : "Active"}
        />
        <div className="settings-grid">
          <div className="setting-row">
            <span>Sets today / total</span>
            <strong>
              {s3.setsToday} / {s3.setsTotal}
            </strong>
          </div>
          <div className="setting-row">
            <span>Open set</span>
            <strong>{s3.open.map((o) => o.coin).join(", ") || "none"}</strong>
          </div>
        </div>
        <Table
          head={["Coin", "Opened", "Closed", "Why", "P&L"]}
          rows={s3.trades.map((t) => [<strong key="c">{t.coin}</strong>, t.opened, t.closed, t.why, pnl(t.pnl)])}
        />
      </section>

      <section className="card positions-card">
        <Head
          eyebrow="Older research bots · paper"
          title="Research paper bots"
          note="Earlier strategies still paper trading daily for comparison. None of them trades real money."
          badge={`${paper.research.length} bots`}
        />
        <Table
          head={["Bot", "Description", "Days", "Return", "Worst drop"]}
          rows={paper.research.map((r) => [<strong key="n">{r.name}</strong>, r.desc, r.days, pnl(r.total), r.maxDd])}
        />
        <p className="read-only-note">
          <FlaskConical size={15} />
          Paper results are forward tests only. A handful of days says nothing yet.
        </p>
      </section>
    </div>
  );
}

export function LogsView({ logs }: { logs: Record<string, string[]> }) {
  return (
    <div className="dashboard-grid">
      {Object.entries(logs).map(([name, lines]) => (
        <details className="card details-card" key={name} open={name === "watcher.log"}>
          <summary>
            <span>
              <span className="eyebrow">Log · last {lines.length} lines</span>
              <strong>{name}</strong>
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
