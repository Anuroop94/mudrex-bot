"""Outcome memory constrained to ranking/veto. It never supplies risk confidence or mutates policy.

The learner defaults to shadow mode. Promotion is a deliberate code/review decision (`PROMOTED=False`), never
self-deployment. Invalid, sparse, stale, or degrading history returns neutral decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

PROMOTED = False
MIN_TOTAL = 24
MIN_GROUP = 8
MAX_STALENESS = 30 * 86400


@dataclass(frozen=True)
class Decision:
    score: float = 0.5
    veto: bool = False
    active: bool = False
    reason: str = "shadow/insufficient history"


def decisions_from_db(con, candidates, decision_at=None, promoted=PROMOTED):
    """Use only outcomes available before the decision. A learned score is never a sizing input."""
    decision_at = int(decision_at or time.time())
    rows = con.execute("""SELECT o.coin,o.side,n.realized_pnl,n.closed_at
        FROM owned n JOIN orders o ON o.client_order_id=n.client_order_id
        WHERE n.closed_at IS NOT NULL AND n.closed_at<? AND n.realized_pnl IS NOT NULL
        ORDER BY n.closed_at,n.position_id""", (decision_at,)).fetchall()
    neutral = {tuple(c): Decision() for c in candidates}
    if len(rows) < MIN_TOTAL or not promoted:
        return neutral
    vals = [float(r["realized_pnl"]) for r in rows]
    if not all(math.isfinite(x) for x in vals) or decision_at - int(rows[-1]["closed_at"]) > MAX_STALENESS:
        return neutral
    # Atomic rollback signal: recent performance materially below the older baseline.
    recent = vals[-8:]
    older = vals[:-8]
    if older and sum(recent) / len(recent) < sum(older) / len(older) - 75:
        return {tuple(c): Decision(reason="rollback: recent model population degraded") for c in candidates}
    result = {}
    for candidate in candidates:
        coin, side = tuple(candidate)
        group = [r for r in rows if r["coin"] == coin and str(r["side"]).upper() == str(side).upper()]
        if len(group) < MIN_GROUP:
            result[(coin, side)] = Decision()
            continue
        pnls = [float(r["realized_pnl"]) for r in group]
        wins = sum(x > 0 for x in pnls)
        score = (wins + 2) / (len(pnls) + 4)          # deterministic beta smoothing
        veto = score < 0.35 and sum(pnls) / len(pnls) < 0
        result[(coin, side)] = Decision(score, veto, True,
                                         "veto: repeated negative outcomes" if veto else "ranked by closed outcomes")
    return result
