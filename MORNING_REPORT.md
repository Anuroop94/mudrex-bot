# Morning report — 2026-09-28

## Bottom line

The bot is **built, tested and safe to dry-run**. It is **not trading**: `STOP` is present and
`trade_policy.AUTONOMOUS_HEDGE_READY = False`. Only you can switch it on (steps below).

| | Status |
|---|---|
| Strategy | **S4 intraday momentum sets** (your rule: intraday, LONG + SHORT, all liquid coins) |
| Automatic trades | sets 1-3 per IST day without a tap; a 4th needs your Telegram tap |
| Every trade | fixed stop-loss AND take-profit placed on Mudrex with the order |
| Daily limit | no new sets after -Rs500 or +Rs500 (IST 00:00-24:00) |
| Tests | 213 offline tests pass (fake exchange only) |
| Reviews | Claude + Sonnet reviewer + Haiku tester each round; Codex review 03:05: "safe to dry-run, not for live" -> its 4 findings fixed and **verified by Codex**; its one follow-up finding fixed too (see below) |

## What S4 does

Every 15 minutes, around the clock: among the verified liquid crypto futures, it takes the single strongest
24-hour mover in the allowed direction — **buy** only when BTC is above its 200-day average and the coin's own
daily trend is up, **sell (short)** only when BTC is below it and the coin's trend is down. Target = 2 x the coin's
daily range (ATR), stop = 1 x. Rs150 at risk per set. A set is closed by its target, its stop, or after 72 hours.
The next set starts only after the previous one has closed.

Dry run on live data last night (no order sent): it would have bought **SUI** at 1.2495, stop 1.1645,
target 1.4195, 10 SUI (~Rs1,274), 1x, risk Rs97.

## Honest evidence (please read)

- 28 intraday designs tested on 40 coins with all costs. **27 lost money**, most wiped out the Rs5,000.
  Even with zero fees the hourly setups had no edge.
- S4 is the only one that made money: +66% in 2022-2025 — but re-run hours later with **one coin changed**
  it made **+20%** (worst drop 54%). The edge is **unproven**; treat it as an experiment.
- It trades about **0.5 sets a day**, not 2. Forcing 2+ sets a day lost money in every test.

## What I fixed overnight (all committed)

1. **Day-start balance** — Codex required an equity reading at exactly midnight to the second, which never
   exists, so the bot would have blocked every trade every day (and 20 safety tests failed). Now it is computed
   exactly from Mudrex's own timestamps and Mudrex's price at exactly 00:00 IST; unknown = blocked.
2. **4th set approval** — it was being executed before you could tap; now it waits for your tap.
3. **Repeated trades** — a trade that filled and was then force-closed did not count, so the bot could re-enter
   every 15 minutes; now it counts.
4. **Telegram flood** — ~75 identical messages a day; now only when something changes, and approval buttons
   never point at a replaced plan.
5. **Backtest bug** (Codex) — confidence was inflated, over-stating results.
6. **S4 would have closed your old XRP trade** after 72h — now S4 only manages trades it opened itself.
7. Watcher now checks positions before retrying Telegram; S4 plans 24h (not only after 05:35).
8. Haiku break-test: bad exchange numbers (rate 0, NaN balance, max leverage 0) now skip instead of crashing.
9. Codex review (03:05) found 4 money-safety gaps, all fixed with tests:
   - an order Mudrex reports as CANCELLED/EXPIRED after a **partial fill** was ignored (could be left without
     SL/TP) -> now verified or exited, and it counts as a set;
   - a trade carried past midnight reserved risk from its entry, not from midnight's price -> a profit given back
     could breach today's Rs500; now reserved from Mudrex's midnight price;
   - a local reading at 00:00:00.9 could become the day-start balance -> exchange data only;
   - a carried trade without its exchange INR rate was still called "exact" -> now blocked.
10. Codex follow-up: day-start balances saved by the old code stay "trusted" after an upgrade -> a one-time
    upgrade step now marks them untrusted so they are recomputed from Mudrex data.

## Your decisions before going live

1. **XRP (old S1 trade)**: it stays open with its Mudrex stop 1.2288 and no target. S4 will not touch it and it
   blocks nothing, but its risk counts in the Rs500 budget. Keep it, or close it yourself in the Mudrex app.
2. **Dry run first (recommended 2-3 days)**: while STOP is present the watcher still sends S4 plans to Telegram
   marked "BLOCKED: STOP file present" — that shows exactly what it would trade.
3. **Go live** (only you): tell me/Codex to set `AUTONOMOUS_HEDGE_READY = True` (a reviewed one-line change),
   keep `LIVE_TRADING_ENABLED=true` in `.env`, then send `/resume` in Telegram (removes STOP).
   Kill switch at any time: `/stop`.

## Still unverified (cannot be tested offline)

- Placing an order **with take-profit and stop-loss together** on the real Mudrex API has never been done by
  the bot. Watch the first real trade in the Mudrex app: it must show both SL and TP. If not, the bot exits it.
- Daily-bar backtests approximate intraday fills; slippage on smaller coins may be worse than 0.05%.
- Learning is shadow-only (records and scores, never trades).

## After restarting the PC

The watcher and Telegram approver start by themselves at login and load the new code (the database adds its
new columns automatically). Expect one Telegram warning that the old XRP trade "has NO take-profit": that is
correct (S1 trades never had one) and harmless; its stop-loss is still on Mudrex. The dashboard: double-click `start-dashboard.cmd` -> http://127.0.0.1:8765.
