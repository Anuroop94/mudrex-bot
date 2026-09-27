# Claude review 2 of Codex's uncommitted changes (2026-09-27, diff quiet since 16:10 local)

Reviewers: Claude (owner rules + strategy), Sonnet agent (order path safety), Haiku agent (tests + edge inputs).
Tests: all 10 suites pass, 171 tests (FakeMudrex only).

## Review 1 follow-up
Fixed: #1 hermetic tests (.env not loaded in MUDREX_TEST_MODE), #2 NaN/None P&L blocks, #3 4th-set approval is
single-use, proposal-hash bound, consumed at submit, attempted sets count, #4 one Rs500 constant (trade_policy),
#6 dashboard wording. Open: #5 `s1_audit.py` still uses `s1.DAILY_CAP_PCT` (now 10%), so the documented S1 audit
numbers no longer reproduce.

## Order-path safety (Sonnet): no CRITICAL/HIGH defect
SHORT geometry (stop above entry, below liquidation), P&L signs, TP verification (missing TP -> repair ->
RECONCILE_REQUIRED), no opposite-side order ever used to close (close_position only), OPEN refused on any symbol
with an existing position (one-way safe), STOP + migration gate block every OPEN, protective exits still work.

## Findings
1. **HIGH - owner rules 2, 4, 7, 9 are not implemented; this is S1 with shorts, not the set trader.**
   The plan is still made ONCE per day at 05:35 IST (`watcher.maybe_plan`) on the same 6 coins (`s1.BASKET`).
   "Sets 1-3" are up to three coins opened together at 05:35, not sets through the 24-hour cycle where the next
   set starts after the previous one closes. No all-coin universe (rule 9). "Confidence" is
   `abs(signal weight) * 6`, not learning from past trades (rule 7).
2. **HIGH - untested strategy change on the real-money path.** S1's only evidence (+232% dev) is LONG-only, no
   take-profit, 3xATR stop, 2x. The new rules (shorts below BTC 200d, 1.5-2.5 ATR stops, fixed 1.5R target,
   rupee-risk sizing, 1-5x leverage) have no backtest. Claude's audit of S1 with fixed targets (s1_audit.py
   `target` section) showed a +2% target cuts dev +232% to +1.8%; +10% to +91%. `s1.py` docstring still cites
   +232% as evidence for the changed rules: misleading. Backtest the exact live rules (same cost model) before
   `AUTONOMOUS_HEDGE_READY` can ever be set.
3. **MEDIUM - the 4th-set approval path is unreachable.** `live_trader.py` caps slots at `MAX_SETS_PER_CYCLE -
   attempted`, so the planner never proposes a 4th set; `plan_requires_set_approval` / `journal_set_approval` are
   tested but dead in the real flow (owner decision 3). Fails toward fewer trades, not more.
4. **LOW - flat Rs10 cost buffer** ignores funding (`funding_fee_perc`, hourly), which shorts can pay for hours.
5. **LOW - `adaptive_risk.plan_trade` raises on extreme volatility** although its docstring says it returns None.
   Safe today because `build_orders` catches ValueError -> SKIP; fix the docstring or return None.
6. **LOW - unrelated change:** `config.DD_HALVE` 0.10 -> 0.05 (research-only setting) in this diff; AGENTS.md
   rule 5 says one task per change.
7. **NOTE - the watcher now places orders** (was read-only); `ex.execute` runs inside the watcher process after
   certification. Any crash there must not stop monitoring; consider running dispatch in its own guarded call
   with its own heartbeat.
