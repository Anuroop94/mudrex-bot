# Owner decisions for the autonomous set trader (2026-09-27, answers to OWNER_TRADING_PLAN.md)

Recorded by Claude from the owner's answers in chat. Codex builds; Claude reviews and tests each piece.

## Decisions

1. **Hedge = both directions.** Each trade is a LONG or a SHORT on one coin, chosen by market direction
   (uptrend: buy, downtrend: sell, chop: no trade). Not a same-symbol long+short pair.
2. **Minimum two is a target, not forced.** Take a set only when the setup passes the filters; 0-1 sets on a
   weak day is acceptable. (`FORCE_MINIMUM_TRADES = False` stays.)
3. **Sets 1-3 automatic; a 4th (or later) set needs owner approval in Telegram** (Approve/Reject, bound to that
   set, expiring). So `HUMAN_OVERRIDE_ABOVE_MAX` must become True with an approval gate, not a hard block.
4. **Work split:** Codex implements everything; Claude reviews diffs, runs/extends adversarial tests and wires
   the dashboard (`ui/`, `dashboard.py /api/ui`).

Unchanged owner rules: IST 00:00-24:00 cycle, fixed stop-loss AND take-profit on every trade (exchange-verified),
stop opening sets at -Rs500 or +Rs500 (realized + open), learning may only rank/veto, all updates to Telegram,
trade all coins that pass safety filters.

## Facts from live read-only checks (use these, don't re-guess)

- **Mudrex is one-way per symbol.** Owner's QNTUSDT orders on 2026-09-27: SHORT 0.77 at 00:42:59Z then LONG 0.77
  at 00:43:46Z share `future_position_uuid` 01a0e050-...; the LONG closed the SHORT. Same-symbol hedging is
  impossible (and would only pay fees).
- **744 symbols listed** (`pick_coins.listing()`), including stock tokens (e.g. AAPLUSDT) and thin coins.
  Universe needs filters: 24h volume, listing age, no stock/commodity tokens, min notional fits per-trade size.
- `/v1/futures/orders/detail?client_order_id=` **always 404s**, even for filled orders; detail resolves only by
  `order_id`. Fixed in commit 86e01f9: `order_by_client_id` falls back to INR order history; `order_by_id`;
  reconcile never calls an order younger than 15 min "not placed". Keep this for SHORT orders too.
- **Positions carry no mark price.** Fixed in fa4c8dc: `Client.positions()` fills `mark_price` from the asset
  `price`. Without it open P&L reads 0 and the Rs500 stop can't see open losses.
- Position objects have `stoploss` and `takeprofit` {price, order_id, order_type}; the take-profit side exists
  on Mudrex, but setting it via API is not yet exercised by the bot. Verify on the fake + minimum-risk check.
- Asset objects include `funding_fee_perc` and `funding_interval` (hourly funding matters for shorts too).

## Claude's recommendations (evidence, not rules)

- **Backtest before building on it.** The closest earlier design (S3: 1h breakout sets, long-only, 6 coins,
  fixed target/stop) lost 61-90% in development after costs in every variant, and its paper run is -6.5% on day
  one. Two-sided + wider universe + regime filter must beat costs in a walk-forward backtest (same cost model:
  taker fee + GST + slippage both sides + hourly funding) before any live money.
- **Per-set risk budget ~Rs150** (stop distance x size), so three losing sets stay under the Rs500 stop even
  with some slippage; check remaining loss budget before each set.
- **Dry run first:** 3-7 full IST cycles with live prices and Telegram "would have traded" messages, no orders.
- Existing S1 XRP position (plan 7) keeps its exchange stop; decide explicitly whether S1 is retired or runs on
  a separate allocation so the Rs500 cycle P&L is not mixed.
