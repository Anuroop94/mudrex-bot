# Owner trading contract and implementation plan

Status: **migration in progress; autonomous production entries are blocked**.

This document translates the owner's eight instructions into testable behavior. `trade_policy.py` contains the machine-enforced constants and gates.

## Contract

1. The strategy operates on both market directions: a qualified uptrend may open a LONG and a qualified downtrend may open a SHORT. A set is one directional trade, not a simultaneous same-symbol LONG+SHORT pair. Choppy/uncertain markets produce no trade.
2. The target is two qualified sets per IST calendar day; three is the autonomous maximum. The bot never invents a low-quality trade merely to reach two.
3. Sets 1-3 require no approval after the autonomous strategy is certified. Every set after the third requires a new Telegram approval bound to that set and an expiry time. Approval cannot override the Rs500 P&L stop or any safety gate.
4. Each cycle runs from 00:00:00 through 23:59:59 Asia/Kolkata time. Restarting the process does not reset counters or P&L.
5. Every new trade must have exchange-verified stop-loss and take-profit orders. If either protection cannot be verified, do not open the set; if a fill occurred, execute the existing fail-safe exit procedure.
6. At realized plus unrealized bot P&L of -Rs500 or +Rs500 for the cycle, block new sets for the rest of that cycle. Protective exits remain allowed. Gaps, slippage, fees, and exchange failures mean Rs500 is a stop threshold, not a guarantee that final loss cannot exceed it.
7. Learning may score, rank, or veto candidates using closed historical outcomes and market-regime features. It may not self-edit, self-deploy, increase leverage/risk/trade count, loosen stops, or bypass any policy gate. A walk-forward evaluation and rollback baseline are required before promotion.
8. Plans, fills, rejected/ambiguous orders, protection verification, exits, cap events, daily summaries, failures, and recovery actions must be journaled and sent to Telegram. Safety-critical messages use retry plus local persistence; Telegram failure blocks new entries but never blocks a risk-reducing exit.

## Delivery phases

### Phase 0 — freeze and contract (done)

- Turn on `STOP` and leave it on.
- Add the authoritative policy module and agent instructions.
- Fix the configuration precedence bug so a stale parent environment cannot override the repository's explicit live-trading setting.
- Change the live day cap from percentage-based to fixed Rs500.
- Add a production migration gate and policy unit tests.

### Phase 1 — hedge-set ledger (implemented; activation blocked)

- Add durable cycle and set tables to the execution database.
- Give each directional trade a durable set ID and side (`LONG` or `SHORT`).
- Count a set only after its entry, stop-loss, and take-profit are exchange-verified.
- Recover partial orders after a crash; never silently count or duplicate them.

### Phase 2 — two-sided bracket execution (implemented against fake exchange)

- Confirmed SHORT bracket behavior with the fake exchange. A minimum-risk live check still needs separate owner approval.
- Implement side-aware entry, stop, target, sizing, leverage, liquidation, and close validation.
- If either bracket cannot be verified after a fill, neutralize the position through the existing fail-safe exit path.
- Add fake-exchange, timeout, retry, crash, and adversarial concurrency tests.

### Phase 3 — 24-hour cycle, autonomous dispatch and Telegram (implemented; shadow validation pending)

- Evaluate candidates throughout the IST cycle, with cooldowns and no overlapping set creation.
- Enforce target two / autonomous maximum three, per-set approval above three, and fixed P&L stops from the database, not process memory.
- Journal Telegram delivery state and retry critical events idempotently.
- Publish start-of-cycle, each decision, each state transition, cap stop, and end-of-cycle summary.

### Phase 4 — bounded learning (candidate ranking/veto implemented; outcome model pending)

- Store immutable decision-time features, regime, rationale, fills, fees, slippage, and outcome.
- Train/evaluate offline with time-ordered walk-forward splits and compare against the frozen baseline.
- Permit the promoted model only to rank or veto otherwise valid sets.
- Auto-disable on drift, missing data, degraded performance, or schema/version mismatch.

### Phase 5 — controlled activation

- Run paper/shadow mode through multiple complete cycles.
- Review the diff, tests, audit trail, Telegram delivery, and simulated cap behavior with the owner.
- The owner alone removes `STOP` and enables live trading.

## Chosen risk design

- Stop distance is volatility-tiered ATR: 1.5x normal, 2x elevated, 2.5x high; extreme volatility is vetoed.
- Target is at least 1.5:1 reward/risk. Candidate risk is at most Rs250.
- Gross realized cycle losses plus every active verified stop reserve plus candidate risk/cost buffer cannot exceed
  Rs500. Winning trades never refill that loss allowance.
- Per-coin isolated leverage is selected from volatility, confidence and exchange constraints, hard-capped at 5x.
  Quantity is computed from rupee risk and stop distance only, so higher leverage cannot secretly enlarge the loss.

