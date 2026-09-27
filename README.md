# mudrex-bot runbook

Strategy **S1** (see `s1.py`): daily trend ensemble on XRP, ADA, DOGE, LINK, AVAX, TRX. Buy-only, 2x, stop-loss
3xATR below the actual fill, BTC 200-day "market mood" filter, Rs 5,000 allocation, bot-only daily caps of 5%.
Backtests are not guarantees. **Every real order needs a human approval** (terminal `YES` or Telegram button).

## Safety switches (all default to SAFE)
| Switch | Where | Effect |
|---|---|---|
| `LIVE_TRADING_ENABLED` | `.env` (absent = `false`) | Must be exactly `true` or nothing is ever sent to Mudrex |
| `STOP` file | this folder | Kill switch: blocks every order, checked before the plan AND before each order |
| Performance guard | `guard.json` (`tripped`) | Set by the watcher when live results break backtest limits; blocks new entries |
| Daily caps | `execution.db` ledger | 5% of the bot's day-start equity; blocks new entries. Caps never close by themselves: the watcher sends a *Close all* plan for you to approve. So 5% is an entry-halt line, NOT a maximum loss: losses can run past it before you approve, or through a stop that gaps |
| Protective exit | `execution.py` | your standing rule (2026-09-27): if a buy you approved fills but its stop-loss cannot be confirmed, or after the fill it breaks the approved amount, a loss budget or the Rs 10,000 exposure limit, the bot EXITS that position at once, with no extra tap. It only closes the bot's own validated position, never opens anything, and runs even when STOP is set |
| Day-start balance | `execution.caps_state` | taken from the watcher's mark just before IST midnight; if the bot was not watching then while positions were open, no new buys that day |
| Loss budgets | `s1.py` + `execution.py` | a new buy is refused if its stop would lose > 6% of bot equity, or if ALL open stops together would lose > 15% (hard, checked before every order). Gaps through a stop can still lose more |
| Data freshness | `live_trader.bad_data` | a coin with a missing or gappy daily history gets no decision that day; unknown BTC mood blocks new buys |

## Modes
- **Backtest / research**: `python research_small.py`, `python research_rules.py` (no account access).
- **Paper**: `python paper_s1.py` (S1 + challengers), `python paper_portfolio.py` - simulated fills only, scheduled daily.
- **Dry run (live data, no orders)**: `python live_trader.py plan` - reads the account, records a plan, places nothing.
- **Live**: set `LIVE_TRADING_ENABLED=true` in `.env`, then approve plans with `python live_trader.py execute` + `YES`,
  or the Telegram **Approve** button (approver running).

## Daily routine
1. ~05:35 IST the watcher builds the plan and sends it (Telegram + Windows notification).
2. Review it; a plan that BUYS must be approved within 15 minutes (prices move). Later, tap Approve anyway (or send
   `/plan`): nothing trades, you get a fresh plan at current prices to approve. Close-only plans stay valid 3 hours.
3. `python dashboard.py` -> http://127.0.0.1:8765 for positions, caps, guard, paper leaderboards.

## Emergency shutdown
1. **Phone**: send `/stop` to the bot (creates `STOP`). **PC**: `python ops.py stop`.
2. Positions keep their exchange stop-losses. To exit now, close them in the Mudrex app.
3. Optional: set `LIVE_TRADING_ENABLED=false` in `.env`.

## Resume (local only)
`python ops.py resume` removes `STOP`. Resume is deliberately **not** available from Telegram.
If the guard tripped, review `journal.csv`, then edit `guard.json` to `"tripped": false` yourself.

## Recovery after a crash / power cut
Run `python live_trader.py reconcile`. It looks up every unfinished order by its `client_order_id` on Mudrex,
records fills, verifies (or attaches) stop-losses, and marks never-sent orders FAILED. It **never resubmits**.
Plans stuck in `RECONCILE_REQUIRED` mean Mudrex could not confirm an order: check the Mudrex app, then reconcile again.
Journal: `execution.db` (tables `plans`, `orders`, `owned`, `ledger`, `events`).

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
| `MudrexPaperBot` | daily 20:15 local (05:45 IST): paper bots | No |
| `MudrexWatcher` | at logon, 24/7: alerts, caps, guard, daily plan | No (only reads; records plans for approval) |
| `MudrexApprover` | at logon, 24/7 (only if you enable it) | Only on YOUR Telegram Approve tap |

## Known limits (documented, fail-closed where possible)
- **INR rate**: Mudrex has no documented quote endpoint. The bot uses the most recent rate Mudrex itself applied
  (open position or INR order, max 7 days old), sizes with 3% headroom, and alerts if an order's applied rate
  differs by more than 3%. No recent rate -> no entries.
- **History**: order/position history supports only `limit` (no pagination). The local `owned` table (written on
  every verified fill) is authoritative; if a closed bot position's P&L is not visible, new entries are blocked.
- **Daily cap baseline**: allocation + P&L realized before IST midnight + unrealized at the first check of the day.
  Unrealized moves between midnight and that first check are not counted.
- **Stops**: entries carry an initial stop; after the fill the stop is moved (PATCH) to fill - 3xATR within a small
  tolerance and verified. Liquidation price must be known, otherwise the plan halts for reconciliation.

## Tests
`python test_core.py` (strategy, backtest, paper parity, planning, Telegram auth) and
`python test_execution.py` (fake Mudrex server: races, crashes, timeouts, 423/500, manual positions, STOP midway,
gaps, stop failures, capital cap). Neither contacts the live API.
