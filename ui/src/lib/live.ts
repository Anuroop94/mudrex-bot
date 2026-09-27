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
