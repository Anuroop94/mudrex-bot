# Claude handoff — 2026-09-27

## Objective

Continue reviewing and completing this trading bot against the owner's requirements, without enabling live trading until every safety blocker is resolved and independently verified.

Owner requirements:

1. Trade both LONG and SHORT according to market direction. Do not hold simultaneous opposite positions on the same symbol.
2. Target at least 2 completed trade sets per 24-hour IST cycle, with a hard maximum of 3 autonomous entries. Never force a weak trade merely to reach the target.
3. Trades 1–3 require no approval. Any proposed fourth trade must be rejected unless a separate, explicit owner-approved policy is implemented.
4. Use a strict 24-hour cycle with an exact IST cycle boundary.
5. Every entry must have a validated fixed target and stop-loss before submission.
6. Stop new entries when cycle P&L reaches either +INR 500 or -INR 500. Collective planned stop exposure must never exceed the remaining INR 500 daily loss budget.
7. Learning must initially remain shadow-only and may rank or veto candidates only. It must never increase quantity, leverage, trade count, or risk limits.
8. All operational updates must use Telegram with durable delivery and retries.
9. Leverage may vary per coin, but only within exchange limits and the daily collective stop-loss budget.
10. The tradable universe means all positively identified, sufficiently liquid crypto futures; unknown/non-crypto instruments fail closed.

## Mandatory safety state

- Keep `STOP` present.
- Keep `trade_policy.AUTONOMOUS_HEDGE_READY = False`.
- Do not start live services, call live order APIs, inspect secrets, or remove either activation gate.
- Use fake/offline tests with `MUDREX_TEST_MODE=1`.
- Do not treat backtests or shadow learning as permission to trade live.

## Completed changes

- `execution.py`
  - Durable leased SQLite Telegram outbox with dedupe event IDs, retry state, and restart persistence.
  - Mandatory Telegram delivery canaries before leverage changes and order submission.
  - Every initial/retried order POST rechecks positions, ownership, trade caps, exact cycle baseline, price drift, hedge rate, risk, and collective stop reserve.
  - Final pre-submit validation checks bracket placement against fresh price, minimum 1.5 reward/risk with tick tolerance, and live notional against the reserved plan.
  - Production alert callbacks must return exactly `True`; permissive test sinks are test-only.
- `watcher.py`
  - Ordinary operational updates use the durable outbox and deterministic day/message deduplication.
  - Outbox retries avoid recursive re-enqueueing.
  - Approval messages with buttons remain direct so immutable plan retries preserve their controls.
  - Missing required Telegram configuration fails closed; manual positions produce warnings.
- `approver.py`
  - Telegram offsets are saved only after successful idempotent handling.
  - Alert delivery reports success explicitly.
- `live_trader.py`
  - Dynamic universe and shadow learner integrated.
  - Requires at least 400 contiguous closed bars.
  - Execution/reconciliation messages route through Telegram rather than console-only output.
  - Bot-owned held coins remain in the management universe even after leaving the entry universe; they may be held/closed but cannot create new entries.
- `live_universe.py`
  - Explicit crypto assets are accepted; non-crypto and unknown assets are rejected.
  - Missing asset class is allowed only for the reviewed 12-coin allowlist.
  - Positive finite contract specs and at least 12,000,000 USDT of 24-hour notional liquidity are required.
  - Eligible membership is archived immutably per cycle and can be replayed.
- `bounded_learning.py`
  - Remains shadow-only with `PROMOTED = False`.
  - Uses only outcomes closed before the decision time, minimum sample sizes, stale-data fallback, degradation rollback, and rank/veto-only output.
  - Strategy confidence—not model score—continues to control base risk calculations.
- `adaptive_backtest.py`
  - Side-aware gap and stop-first exits, bracket re-anchoring, cumulative funding, and intra-loop cap enforcement.
  - Same-bar sequence is open gaps/exits, open-only entry decisions, intraday range evaluation, then funding for survivors.
- The previous go-live verdict was withdrawn.

## Verification

All testing was offline/fake. No live API was invoked.

- Final impacted rerun: `test_execution.py` 84 passed; `test_core.py` 31 passed.
- Supporting suites previously passed: adversarial 13, adversarial books 6, adversarial close 10, adversarial fence 8, trade policy 6, Mudrex brackets 3, S3 sanity 7, adaptive backtest 6, live universe 4, bounded learning 3, adaptive risk 13.
- Combined verified test inventory: 194 passing tests across the completed runs.
- No commit was made.

## Unresolved blockers

1. **Exact IST boundary valuation:** a carried position requires a trusted exact cycle-boundary mark. No dependable exchange timestamp/price source was established. The current fail-closed behavior may block the next cycle's entries; do not replace it with a nearby local timestamp.
2. **Learning promotion:** there is no approved walk-forward evidence, immutable model artifact, or model-specific promotion/rollback decision. Keep learning shadow-only.
3. **Backtest boundary accuracy:** daily candles approximate the 05:30 IST decision boundary unless explicitly aligned. Results are research only.
4. **Independent review:** the local Claude CLI review attempt failed with HTTP 401 because its OAuth token had expired. Therefore Claude has not approved these changes.
5. **Activation:** the bot is not live-ready. `STOP` and the migration flag must remain unchanged.

## Requested Claude work

1. Read `AGENTS.md`, this file, `CLAUDE_REVIEW_2.md`, and the complete current diff before editing.
2. Perform an independent safety review of order submission, retry/reconciliation behavior, Telegram durability, position ownership, exact cycle accounting, brackets, variable leverage, and collective stop reserve.
3. Resolve only issues that preserve or strengthen fail-closed behavior.
4. Add regression tests for every fix and run the full fake/offline suite.
5. Do not claim live readiness merely because tests pass. Document any remaining assumption explicitly.
6. Leave `STOP` present and `AUTONOMOUS_HEDGE_READY = False` unless the owner separately authorizes a formal activation review after all blockers are resolved.

## Files intentionally left untouched

- Runtime/research artifacts such as `approver_heartbeat.json`, `s4_research.py`, and `s4_results_daily.txt` were not modified.
- Secrets, `.env` contents, runtime databases, logs, heartbeat state, and live exchange state were not inspected.
