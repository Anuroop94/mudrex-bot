# Go-live report (27 Sep, 1:45 AM)

## Verdict: READY. Codex final check says **GO**. Live trading is still **OFF** until you switch it on.
No real order was placed tonight. Nothing trades until you do the go-live steps at the bottom.

## What happened tonight
Codex and my own reviewers (Sonnet reviewer + Haiku tester, cheap models as you asked) went back and forth
until nothing serious was left:

| Round | Found | Result |
|---|---|---|
| Codex 7 (12:37 AM) | 5 high + 5 medium, mostly about closes you approve | all fixed |
| Own review | 2 real + 2 minor | all fixed |
| Codex 8 (1:03 AM) | 2 high: an automatic exit and your close could overlap; one bad reading could end a close | fixed |
| Own review | 2 more (two processes at the same instant; a silent wait) | fixed |
| Codex 9 (1:24 AM) | 1 high (a finished exit stopped counting as "closing") | fixed |
| Own review | none serious | - |
| **Codex 10 (1:36 AM)** | **none - verdict GO** | - |

What the bot now guarantees (all tested against a fake Mudrex, never the real one):
- **Buys only with your approval.** A buy plan is valid 15 minutes; price and plan age are re-checked right
  before every order. Tapping an old plan only sends you a fresh one.
- **Automatic protective exit (your rule):** if a buy you approved cannot get its stop-loss confirmed, costs
  more than approved, breaks the loss limits or has an error, the bot closes THAT position by itself. It never
  opens anything, never touches your manual positions, never closes twice (one "closing" lock per position,
  10-minute wait between tries, max 3 tries, then it asks you).
- **A close only counts as done when Mudrex's own history says so.** One missing reading never fools it.
- **Crash-safe:** after a crash or restart the approver finishes safely within ~5 minutes; it can never
  work on the same plan as another process.
- **Limits:** Rs 5,000 allocation, 2x isolated, stop 3 x ATR, max 7% loss per trade and 21% if all stops hit,
  5% daily line (blocks new buys), performance guard, BTC mood filter, unknown data = no new buys.

**Tests: 137, all passing** (29 core + 71 execution + 13 + 10 + 8 + 6 attack tests).
Real read-only check: builds today's plan correctly and refuses to trade while live is off.

## Honest picture of the strategy
- Fair test (May 2021 - Sep 2025, exact live rules, all costs): **+232%**, worst fall 25%, worst day -Rs 563.
  Statistics say "suggestive, not proven" (~44% after allowing for how many strategies were tried).
- Last 12 months (looked at many times, descriptive only): +4.3%.
- About 20-25 trades a year; expect quiet weeks and losing streaks. No strategy wins in every market.

## Risks you accept by going live
- If every stop-loss on today's plan were hit: about **Rs 930 (18.6%)**. A price that jumps past a stop can
  lose more.
- The 5% daily line only **stops new buys**; it does not close positions (you get a "Close all" button).
- The bot needs this PC **on and awake**. Stop-losses on Mudrex work even when the PC is off.
- One thing only a real trade can confirm: that Mudrex history uses the same position id as the open
  position (it does in the data we can see). If not, the bot fails safe: it blocks new buys and alerts you.

## Go-live steps (only you do these)
1. Read this report and the README "Switches" table.
2. Windows: Settings -> System -> Power -> "When plugged in, put my device to sleep after" -> **Never**.
3. In `.env` set `LIVE_TRADING_ENABLED=true` (change nothing else).
4. Tell Claude "start the approver": it registers the `MudrexApprover` task (runs at logon) and starts it.
5. Each morning ~05:35 IST: Telegram sends the plan. Tap **Approve** within 15 minutes, or ignore it.
6. Emergency: send `/stop` in Telegram (or `python ops.py stop` on the PC). Your exchange stop-losses stay on.

## Still open (not blockers)
- New dashboard (Freebuff/Lovable): wire it to the bot when you share it.
- Paper trading keeps running daily; S1 and 5 challengers are compared automatically.
