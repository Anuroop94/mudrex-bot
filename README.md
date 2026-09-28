# mudrex-bot runbook

**Migration status:** the bounded two-sided implementation is staged, but production entries remain code-blocked by
`trade_policy.AUTONOMOUS_HEDGE_READY = False`. Keep `STOP` present through review and shadow testing. Backtests and
paper fills are not guarantees.

The strategy evaluates every positively verified, liquid crypto future in the point-in-time entry universe for a
qualified LONG or SHORT. Explicit stocks/commodities and unknown classifications fail closed; rows without asset
metadata are limited to the reviewed crypto allowlist. The IST calendar day is the
durable 24-hour cycle. It targets two qualified sets, may take a third only with a strong signal, and never fabricates
a weak trade to meet the target. After certification, sets 1-3 are autonomous; an extra set requires a fresh Telegram
approval bound to exactly one immutable proposal. Every entry must have an exchange-verified stop and target.

## Safety switches (all default to SAFE)
| Switch | Where | Effect |
|---|---|---|
| `LIVE_TRADING_ENABLED` | `.env` (absent = `false`) | Must be exactly `true` or nothing is ever sent to Mudrex |
| `STOP` file | this folder | Kill switch: blocks every order, checked before the plan AND before each order |
| Performance guard | `guard.json` (`tripped`) | Set by the watcher when live results break backtest limits; blocks new entries |
| Daily caps | `execution.db` ledger | Fixed -Rs500 / +Rs500 bot P&L thresholds per IST day; blocks new entries. Caps do not guarantee final loss because open positions, gaps, slippage, fees, or exchange failures can move P&L beyond a threshold |
| Collective stop ledger | `adaptive_risk.py` + `execution.py` | gross realized losses + verified risk at every active stop + candidate risk/cost buffer must stay at or below Rs500. A candidate is capped at Rs250. Profits do not replenish this loss allowance |
| Variable leverage | `adaptive_risk.py` + `execution.py` | per-coin isolated leverage is selected from confidence, volatility and exchange limits, hard-capped at 5x and read back before entry. Leverage never increases risk-sized quantity |
| Protective exit | `execution.py` | if an owned fill cannot get both protections verified, has unsafe liquidation geometry, or breaks amount/risk/margin constraints, the bot exits that position without another tap. STOP cannot block risk-reducing protection or exit |
| Day-start balance | `execution.caps_state` | exact IST-midnight mark, or exact flat-at-boundary ledger value; a nearby mark is never substituted and an unvalued carry blocks new entries |
| Telegram delivery | `execution.telegram_outbox` | durable leased retries; all older critical updates and a current entry canary must be delivered before leverage/order mutation |
| Bounded learning | `bounded_learning.py` | shadow by default; after explicit promotion it may only rank/veto base-qualified candidates and never changes risk inputs |
| Data freshness | `live_trader.bad_data` | a coin with missing/gappy daily history gets no decision; unknown BTC regime blocks new entries |

## Modes
- **Backtest / research**: `python s4_research.py` (S4), `python s1_audit.py` (S1) (no account access).
- **Paper**: `python paper_s4.py` (S4, hourly) and `python paper_s1.py` (S1 + challengers, daily) - simulated fills only.
- **Archive**: `archive/` holds the retired strategies (T1, Z8 portfolio bots, S2, S3) and their research code, kept
  for reference only. Nothing runs them. Their tests: `python archive/test_archived.py`.
- **Dry run (live data, no orders)**: `python live_trader.py plan` - reads the account, records a plan, places nothing.
- **Live**: unavailable during migration even if `LIVE_TRADING_ENABLED=true`; the policy gate intentionally blocks new entries.

## Daily routine
1. ~05:35 IST the watcher builds the qualified plan and delivers it to Telegram before any autonomous dispatch.
2. After certification, eligible sets 1-3 execute without a tap. An extra one-set plan shows Approve/Reject and its
   one-time approval expires after 15 minutes. Close-only plans remain explicitly owner-approved.
3. `python dashboard.py` (or `start-dashboard.cmd`) -> http://127.0.0.1:8765: plain-English status, next trade idea,
   open trades, trade history, safety checklist, bot health, S4/S1 paper trading, how-it-works glossary, logs.

## Emergency shutdown
1. **Phone**: send `/stop` to the bot (creates `STOP`). **PC**: `python ops.py stop`.
2. Positions keep their exchange stop-losses. To exit now, close them in the Mudrex app.
3. Optional: set `LIVE_TRADING_ENABLED=false` in `.env`.

## Resume (local only)
`python ops.py resume` removes `STOP`. Resume is deliberately **not** available from Telegram.
If the guard tripped, review `journal.csv`, then edit `guard.json` to `"tripped": false` yourself.

## Recovery after a crash / power cut
Run `python live_trader.py reconcile`. It looks up every unfinished order by its `client_order_id` on Mudrex,
records fills, verifies (or repairs) both stop-loss and take-profit, and marks never-sent orders FAILED. It **never
blindly resubmits** an ambiguous entry.
Plans stuck in `RECONCILE_REQUIRED` mean Mudrex could not confirm an order: check the Mudrex app, then reconcile again.
Journal: `execution.db` (including `plans`, `orders`, `owned`, `ledger`, `events`, `telegram_outbox`, and
`universe_snapshots`).

## Manual trading
The bot only touches positions it opened (tracked by Mudrex position ID). If you hold a manual position (long or
short) on an S1 coin, the bot refuses to trade that coin and alerts you. Manual positions never count toward caps.

## Key rotation
1. Mudrex -> API management -> **rotate** the secret (old one stops working immediately).
2. Put the new secret in `.env` as `MUDREX_API_SECRET=...` (never paste it into chat or commit it).
3. Telegram: `/revoke` in @BotFather -> new token -> `.env` `TELEGRAM_BOT_TOKEN=...`.
4. Restart: `Stop-ScheduledTask`/`Start-ScheduledTask` for `MudrexWatcher` (and `MudrexApprover` if used).

## Background tasks (Windows Task Scheduler)
| Task | Runs | Places orders? |
|---|---|---|
| `MudrexPaperBot` | daily 20:15 local (05:45 IST): S1 paper (`paper_s1.py`) | No |
| `MudrexPaperS4` | hourly: S4 paper (`paper_s4.py`) | No |
| `MudrexWatcher` | at logon, 24/7: alerts, caps, guard, plan/dispatch | Sets 1-3 only after certification; currently migration-blocked |
| `MudrexApprover` | at logon, 24/7 | Extra single-set and close-only actions after exact Telegram approval |

## Known limits (documented, fail-closed where possible)
- **INR rate**: Mudrex has no documented quote endpoint. The bot uses the most recent rate Mudrex itself applied
  (open position or INR order, max 7 days old), sizes with 3% headroom, and alerts if an order's applied rate
  differs by more than 3%. No recent rate -> no entries.
- **History**: order/position history supports only `limit` (no pagination). The local `owned` table (written on
  every verified fill) is authoritative; if a closed bot position's P&L is not visible, new entries are blocked.
- **Daily cap baseline**: exact IST-midnight equity is required for a carried position. Without a trustworthy exact
  boundary valuation, new entries fail closed for that cycle. A pre-midnight or first-check estimate is not used.
- **Universe**: the 24-hour liquidity threshold is 12,000,000 USDT notional. Unknown asset classes are excluded;
  current-name heuristics never promote an unreviewed symbol.
- **Learning**: `bounded_learning.PROMOTED` remains false until an independent walk-forward review approves it.
- **Adaptive bracket**: normal volatility uses 1.5 ATR, elevated 2 ATR and high 2.5 ATR; extreme volatility is
  vetoed. Target is at least 1.5 times stop distance. Both legs are side-aware, adjusted to the fill and verified.
- **Rs500 is a threshold, not a guarantee**: gaps, slippage, fees and exchange outages can produce a larger loss.

## Tests
`python test_core.py` (strategy, planning, dashboard, Telegram auth), `python test_s4.py` (S4) and
`python test_execution.py` (fake Mudrex: LONG/SHORT brackets, variable leverage, exact extra-set approval, races,
crashes, timeouts, manual positions, STOP, gaps, protection failures, collective risk and margin). With
`MUDREX_TEST_MODE=1`, neither test contacts the live API or loads `.env`.
