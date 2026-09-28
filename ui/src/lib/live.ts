import { useEffect, useState } from "react";

// Live bot data from the bot's read-only dashboard server (python dashboard.py -> /api/ui).
export type Check = { name: string; state: "ok" | "warn" | "bad"; detail: string; help: string };
export type Point = { label: string; value: number };
export type Curve = { change: string; startLabel: string; points: Point[] };
export type Idea = {
  coin: string; side: "LONG" | "SHORT"; verb: string; entry: string; target: string; targetPct: string;
  stop: string; stopPct: string; size: string; leverage: string; risk: string; placed: boolean;
};

export type Live = {
  now: number;
  dateLabel: string;
  markedAt: string;
  headline: { tone: "good" | "warn" | "bad"; title: string; detail: string; stop: boolean; live: boolean };
  money: {
    equity: string; equityChange: string; allocation: string; today: string; todayPct: string;
    limit: string; limitMarker: number; dayStart: string;
  };
  today: { setsDone: number; setsTarget: number; setsAuto: number; resetInSec: number; nextCheckInSec: number | null };
  next: {
    ideas: Idea[]; closes: { coin: string; reason: string }[]; blocked: string | null; dryRun: boolean;
    planTime: string; setsDone: number | null; needsApproval: boolean;
  };
  positions: {
    coin: string; side: string; bot: boolean; price: string; entry: string; value: string; leverage: string;
    pnl: string; pnlPct: string; stop: string; target: string; protected: boolean;
  }[];
  trades: {
    rows: { coin: string; opened: string; closed: string; days: string; entry: string; exit: string; pnl: string; win: boolean }[];
    count: number; wins: number; winRate: string; total: string; best: string; worst: string;
  };
  charts: Record<"1D" | "7D" | "30D" | "All", Curve>;
  market: { mood: "up" | "down" | "unknown"; btcPrice: string; btcChange: string; diff: string; marker: number; text: string; asOf?: string };
  health: Check[];
  safety: Check[];
  strategy: { live: string; name: string; steps: string[]; rules: [string, string, string][]; evidence: string };
  activity: { kind: string; tone: string; detail: string; time: string }[];
  paper: Paper;
};

export type Paper = {
  s1: {
    start: string; equity: string; total: string; decision: string; plan: string[]; updated: string; rule: string;
    curve: Point[];
    rows: { name: string; desc: string; champion: boolean; days: number; total: string; maxDd: string; trades: number; verdict: string }[];
    positions: { coin: string; entry: string; stop: string; value: string; since: string }[];
  };
  s4: {
    start: string; value: string; total: string; pnl: string; setsTotal: number; setsToday: number; blocked: boolean;
    wins: number; losses: number; winRate: string; coins: number; updated: string; curve: Point[];
    open: { coin: string; side: string; entry: string; target: string; stop: string; since: string; pnl: string }[];
    trades: { coin: string; side: string; opened: string; closed: string; why: string; entry: string; exit: string; pnl: string }[];
  };
  logs: Record<string, string[]>;
};

export function useLive(everyMs = 30000) {
  const [data, setData] = useState<Live | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updated, setUpdated] = useState<Date | null>(null);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch("/api/ui", { cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = (await r.json()) as Live;
        if (alive) {
          setData(d);
          setError(null);
          setUpdated(new Date());
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    };
    void load();
    const t = setInterval(() => void load(), everyMs);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [everyMs]);
  return { data, error, updated };
}

/** Seconds since the last data load, ticking every second (for live countdowns). */
export function useTick(since: Date | null) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  return since ? Math.floor((Date.now() - since.getTime()) / 1000) : 0;
}

export function hms(sec: number | null) {
  if (sec === null || sec < 0) return "—";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

export const sign = (s: string) => (s.startsWith("−") || s.startsWith("-") ? "negative" : s.startsWith("+") ? "positive" : "");
