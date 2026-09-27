import { useEffect, useState } from "react";
import type demo from "../data/mudrex-demo.json";

// Live bot data from the Mudrex bot's read-only dashboard server (python dashboard.py -> /api/ui).
// Same shape as src/data/mudrex-demo.json plus live status fields.
export type Live = typeof demo & {
  status: {
    live: boolean;
    stop: boolean;
    ok: boolean;
    watcherAgeSec: number | null;
    approverAgeSec: number | null;
    issues: string[];
  };
  plan: typeof demo.plan & { id: number | null; state: string | null; needsAction: boolean; message: string };
  strategy: typeof demo.strategy & { target: string; tradeRisk: string };
  paper: Paper;
};

export type Paper = {
  s1: {
    rows: {
      name: string;
      desc: string;
      champion: boolean;
      days: number;
      total: string;
      maxDd: string;
      trades: number;
      t: string;
      verdict: string;
    }[];
    equity: string;
    decision: string;
    plan: string[];
    rule: string;
    positions: { coin: string; entry: string; stop: string; value: string; since: string }[];
  };
  s3: {
    variant: string;
    start: string;
    equity: string;
    total: string;
    setsTotal: number;
    setsToday: number;
    blocked: boolean;
    updated: string;
    open: { coin: string; detail: string }[];
    trades: { coin: string; opened: string; closed: string; why: string; pnl: string }[];
  };
  research: { name: string; desc: string; days: number; total: string; maxDd: string }[];
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

export const sign = (s: string) => (s.startsWith("−") || s.startsWith("-") ? "negative" : s.startsWith("+") ? "positive" : "");
