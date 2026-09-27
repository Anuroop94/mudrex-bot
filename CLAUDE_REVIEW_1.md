# Claude review 1 of Codex's Phase 0 (uncommitted diff, 2026-09-27 ~22:45 IST)

Scope: config.py, execution.py, live_trader.py, s1.py, README.md, tests, trade_policy.py, test_trade_policy.py,
AGENTS.md, OWNER_TRADING_PLAN.md. All 8 suites pass (153 tests) with the current .env.

Verified by running code, not by reading only:

1. **MEDIUM - test suites silently depend on the real .env.** config.py now forces `LIVE_TRADING_ENABLED` from
   the file, overriding the value tests set before import. Reproduced with a temp copy: test sets `true`, `.env`
   says `false` -> `false`. When the owner turns live off, every live-path test turns into REFUSED and fails;
   tests also read the real `.env` (AGENTS.md rule 3). Fix: in `MUDREX_TEST_MODE=1` do not load `.env` at all
   (or never override an already-set value in test mode).
2. **MEDIUM - non-finite P&L passes the Rs500 stop.** `new_set_block_reason(0, float("nan"))` returns None.
   Unknown/NaN/None P&L must block new sets (fail closed), matching execution's existing "P&L unknown" rule.
3. **MEDIUM - a 4th-set approval is reusable.** `SetApproval` is checked by cycle + set_number + expiry only.
   If that set fails or half-fills, `completed_sets` stays 3 and the same approval authorizes another attempt
   until it expires. Also it is not bound to WHAT was approved (coin/side/size). Make approvals single-use and
   bound to a set id / hash of the proposed orders, and count ATTEMPTED sets (any order sent) toward the limit,
   so a failing set cannot loop.
4. **LOW - two sources of the Rs500 rule.** `s1.DAILY_CAP_INR` and `trade_policy.DAILY_*_LIMIT_INR` are separate
   constants. Import from trade_policy (AGENTS.md rule 2).
5. **LOW - S1 research numbers changed silently.** `s1.DAILY_CAP_PCT` is now 500/5000 = 10% and `s1_audit.py`
   BASE uses it, so re-running the audit no longer reproduces the documented +232% (made with 5%). Pin the
   audit's own `cap_pct=0.05` or update the docstring after re-running.
6. **LOW - dashboard.** `dashboard.py` shows `entryMode` "Every buy needs your Telegram approval" and does not show
   the migration block. Claude will update the dashboard (owner split: Claude wires the dashboard).
7. **NOTE for Phase 2** (not bugs yet): `validate_bracket` has no liquidation check (stop must sit before
   liquidation), no minimum reward/risk, no maximum stop distance vs the per-set Rs budget.

Good: migration gate is a code constant with no env bypass (tested); gate sits in `run_open` only, so closes and
protective exits still work; IST cycle id is correct across 18:30 UTC; side-aware bracket validation; README and
plan are honest that the new strategy does not exist yet.
