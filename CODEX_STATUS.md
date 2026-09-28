# Codex status — 2026-09-27

## Safety state

- `STOP` is present; keep it present.
- `trade_policy.AUTONOMOUS_HEDGE_READY` is `False`; production entries remain code-blocked.
- No live services were started, no real order was sent, and no secret/runtime state was inspected.

## Implemented in the current pass

- Durable leased SQLite Telegram outbox with recognizable event IDs, retry state, restart persistence, and a
  mandatory pre-leverage/pre-order delivery canary. Telegram failure before exposure blocks the entry; it never
  blocks protective work or an approved close.
- Full P&L, exact daily cap, ownership, live price, hedge rate, and collective stop-reserve checks immediately
  before every initial/retried market-order POST.
- Exact IST cycle baseline rule. A nearby pre-midnight watcher mark is no longer treated as exact. A carried
  position without an exact midnight valuation makes the baseline untrusted and blocks new entries for that cycle.
- Telegram approval offset is saved only after idempotent handling succeeds.
- Fail-closed dynamic crypto universe: explicit non-crypto/unknown assets are rejected, missing asset class is
  accepted only for the reviewed crypto allowlist, all qualifying rows are retained, and membership is archived
  once per cycle. Liquidity threshold assumption: 12,000,000 USDT 24-hour notional.
- Outcome learner is wired in shadow mode. Its score is separate from strategy confidence and can only rank/veto
  after explicit reviewed promotion; it cannot change quantity, leverage, bracket, caps, or trade count.
- Adaptive backtest now has shared side-aware gap/stop-first exit rules, bracket re-anchoring helpers, cumulative
  funding attribution, and intra-loop P&L-cap recomputation.
- The old go-live verdict is withdrawn.

## Verification completed

- `test_execution.py`: 82 passed.
- `test_adaptive_backtest.py`: 6 passed.
- `test_core.py`: 31 passed.
- `test_live_universe.py`: 3 passed.
- `test_bounded_learning.py`: 2 passed before the final invariance test was added.

No commit was made.

## Deliberate constraints / remaining review

- `bounded_learning.PROMOTED=False`; it cannot self-promote. Walk-forward promotion evidence is not yet approved.
- Mudrex does not expose a documented dependable asset-class field in the inspected response contract. Unknown
  symbols therefore fail closed; “all coins” means all positively verified eligible crypto rows, never all futures.
- Exact overnight valuation currently favors safety over liveness: a carried position without an exact midnight
  mark blocks entries for that cycle.
- Daily-candle backtests only approximate the 05:30 IST decision boundary unless their bars are explicitly aligned;
  they are research, not go-live evidence.
- Run an independent diff review, then shadow test. Do not remove `STOP` or change the migration gate during review.
