# Codex handoff to Claude — 2026-09-27

## Safety state

- `STOP` is present. Keep it present.
- `trade_policy.AUTONOMOUS_HEDGE_READY` is `False`. Do not enable it.
- Do not start watcher/approver, place orders, or contact Telegram/Mudrex in tests.
- Do not read or modify `.env`, secrets, runtime DBs, logs, heartbeats, journals, or state files.
- Tests: `MUDREX_TEST_MODE=1`, `PYTHONDONTWRITEBYTECODE=1`, FakeMudrex/localhost only.
- No commit was made.

## Changes made in this pass

- `adaptive_risk.py`, `live_trader.py`, `s1.py`, `telegram_bot.py`, `config.py`
  - Rs500 risk source and autonomous count come from `trade_policy.py`.
  - Variable leverage remains margin-only; quantity is rupee-risk sized.
  - Risk reserve includes base costs and hourly funding over a 24-hour assumed hold.
  - Extreme volatility is an explicit veto; research `DD_HALVE` is restored to `0.10`.
- `watcher.py`, `test_core.py`
  - Planning now re-evaluates every 15 minutes after 05:35 IST throughout the cycle, rather than once daily.
  - A Telegram delivery failure retries the same immutable plan. The next interval starts only after delivery.
- `execution.py`, `test_execution.py`
  - Counts only `COMPLETE` sets (entry + SL + TP verified).
  - Unresolved attempted submissions block later set writes; definite failures do not consume a set number.
  - Definite 423/429 exchange-busy responses are retryable only within the same executing plan.
  - Terminal rejection/failure and successful fail-safe exit mark the set `FAILED`.
  - The stale undefined `attempted` counter in the approval message was corrected to `completed`.
- `adaptive_backtest.py` (new, audit-only; not used live)
  - Exact-rule audit scaffold covers two-sided signals, adaptive brackets/risk, variable leverage, fees,
    slippage, funding, Rs500 controls, and walk-forward summaries.
  - SHORT P&L direction and same-bar lookahead were corrected: prior closed-bar signal/regime/ATR, next-open
    entry, and brackets shifted to the actual slipped entry. This file still needs dedicated regression tests.

## Tests completed

- `python test_core.py` with the two test environment variables: **30 passed**.
- `python -m unittest -v test_adaptive_risk`: **13 passed**.
- Four direct FakeMudrex execution tests passed: fourth-set approval, failed-vs-unresolved accounting,
  definite rejection state, and existing 423 retry.
- The earlier full fake-only run passed **171 tests**, but that predates the latest execution/backtest edits and
  is not final verification. `pytest` is not installed.

## Next work, in order

1. Finish `live_trader.plan()` integration. It still gates with `attempted_sets` and
   `max(0, 3-attempted)`. Use COMPLETE-only counts plus unresolved fail-closed status from `execution.py`.
   Emit only one OPEN set per proposal so every set 4+ is bound to one exact Telegram approval. Preserve the
   funding-aware `rp.risk_per_unit_inr` calculation.
2. Add `test_adaptive_backtest.py` synthetic regressions proving: no lookahead, correct SHORT sign, side-aware
   stop/target/gap behavior, bracket re-anchoring after slippage, funding/fees, Rs500 cap, and leverage not
   changing risk quantity. Do not publish performance numbers before these pass and walk-forward output is
   reviewed.
3. Correct `s1.py` evidence text. The old +232% result belongs to legacy long-only fixed-2x/3ATR/no-target
   rules, not the new strategy.
4. Run all fake-only scripts below and review failures without weakening safety assertions.
5. Run a Claude diff review. `claude.exe` exists at
   `C:\Users\anuro\.local\bin\claude.exe`. The prompt must forbid secrets/runtime access and live actions.
6. Keep production blocked. Safe all-coin universe promotion and the outcome-trained rank/veto model are not
   complete. Learning must never increase leverage/risk/count or loosen brackets.

## Full fake-only commands

```powershell
$env:MUDREX_TEST_MODE='1'
$env:PYTHONDWRITEBYTECODE='1'
python test_core.py
python test_execution.py
python test_adversarial.py
python test_adversarial_books.py
python test_adversarial_close.py
python test_adversarial_fence.py
python test_trade_policy.py
python -m unittest -v test_adaptive_risk
python test_mudrex_brackets.py
python test_s3_sanity.py
```

## Still intentionally blocked

- Live planning still uses six-coin `s1.BASKET`; it does not yet safely filter the full listing for liquidity,
  history, affordability, and non-stock/non-commodity assets.
- Confidence is still signal-derived; no closed-outcome model is promoted.
- Telegram delivery journaling/retry and isolated watcher dispatch need final adversarial review.
- A real minimum-risk bracket check, shadow cycles, removing `STOP`, and enabling live trading require explicit
  owner action later. This handoff authorizes none of them.
