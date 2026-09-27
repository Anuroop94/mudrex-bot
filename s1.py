"""Strategy S1, chosen by the user on 2026-09-26: single source of truth for paper, live and backtest.

Every day after the 05:30 IST close, for each basket coin, 9 Donchian trend "judges" (5..360 days) vote UP/not;
position = votes x volatility scaling (50% vol target), rounded to what Mudrex accepts. Buy-only. Exits: the
judges' trailing midpoint (trend exit, next open) or a safety stop-loss on Mudrex at 3x ATR below entry.
No fixed target. Exposure 2x (Mudrex leverage 2). Capital capped at Rs 5,000. Daily profit/loss caps 5% each.
Market mood: no positions while BTC is below its 200-day average; unknown mood blocks new entries.
Evidence (s1_audit.py, 2026-09-27; ONE continuous run of these exact live rules with hourly stops, net of all
costs; data starts May 2021): development May 2021 - Sep 2025 +232% (27.6%/yr, max DD 25%, worst day -Rs 563,
111 trades, 34% net win rate), Newey-West t 1.79, deflated Sharpe 44%: suggestive, NOT proven. The year after
(+4.3%) was looked at many times and is descriptive only. Earlier figures (+327%, +277%, +14%, +9.8%) came
from simulations that did not match these rules and are withdrawn. Backtests are not guarantees.
"""
import config
import portfolio as pf

NAME = "S1 trend ensemble: buy-only, safety stop 3xATR, 2x"
BASKET = ["XRP", "ADA", "DOGE", "LINK", "AVAX", "TRX"]
SIGNAL_KW = dict(target_vol=0.5)          # zarattini() defaults: 9 lookbacks, long-only
LEV = 2
SL_ATR = 3
CAPITAL_CAP_INR = 5000
DAILY_CAP_PCT = 0.05     # user rule: daily profit cap AND loss cap = 5% of the day's starting equity (raise to 0.10 later)


# Hard loss budgets (safety nets, not sizing rules): money lost if stops fill exactly at their level, as a share of
# bot equity. Set just above the worst seen in s1_audit.py measured on the Rs 5,000 sizing base (per trade 6.7%,
# all open positions 20.2%), so they never changed a historical trade; they stop anything unusual. (The first
# figures, 6% / 15%, were measured on grown equity and would have blocked normal S1 baskets.) Gaps through a stop
# can still lose more.
MAX_TRADE_STOP_RISK = 0.07
MAX_TOTAL_STOP_RISK = 0.21


def stop_risk_inr(notional_inr, entry, stop):
    return notional_inr * max(0.0, entry - stop) / entry


def sizing_equity(equity_inr):
    """Equity used BOTH to round weights to Mudrex minimums and to size orders (capped at the allocation).
    Rounding on the full equity but sizing on the capped one shrank min-size orders below the exchange minimum
    whenever the bot was in profit (s1_audit.py 2026-09-27: 233 of 337 buy signals lost)."""
    return min(equity_inr, CAPITAL_CAP_INR)


def daily_cap_inr(day_start_equity_inr):
    """Rupee size of both daily caps for a day that started with this equity."""
    return DAILY_CAP_PCT * day_start_equity_inr


MOOD_SMA = 200   # market mood: no positions while BTC closes below its 200-day average (tested: better in both periods)


def btc_mood(btc_candles, d):
    """True / False: BTC's close on day d is at/above / below its 200-day simple average.
    None: day d's bar or 200 days of history is missing. Unknown mood fails closed for NEW entries
    (callers block them) but does not force exits."""
    idx = {x[0]: i for i, x in enumerate(btc_candles)}
    i = idx.get(d)
    if i is None or i < MOOD_SMA - 1 or any(btc_candles[j][0] - btc_candles[j - 1][0] != 86400
                                            for j in range(i - MOOD_SMA + 2, i + 1)):
        return None
    avg = sum(x[4] for x in btc_candles[i - MOOD_SMA + 1:i + 1]) / MOOD_SMA
    return btc_candles[i][4] >= avg


def btc_mood_ok(btc_candles, d):
    return btc_mood(btc_candles, d) is True


def entries_only_for_held(x_all, held):
    """Unknown market mood: keep what is held, open nothing new."""
    return {c: x for c, x in x_all.items() if c in held}


def targets(ctx, closes, d, equity_inr, specs, btc=None, basket=None):
    """1x weights decided at day d's close, rounded for an account of equity_inr (before the LEV multiplier).
    btc: BTC daily candles; when given and the market mood is bad, S1 wants no positions. When the mood is
    unknown (btc_mood None) targets are returned unchanged and the CALLER must block new entries."""
    if btc is not None and btc_mood(btc, d) is False:
        return {}
    tf = pf.basket_targets(basket or BASKET, closes, equity_inr / config.INR_PER_USDT, specs)
    return tf(ctx, d, {})


def specs_from_listing(rows, coins=None):
    by = {r["symbol"].removesuffix("USDT"): r for r in rows}
    return {c: dict(step=float(by[c]["quantity_step"]), min_qty=float(by[c]["min_contract"]),
                    min_notional=float(by[c]["min_notional_value"]), price_step=float(by[c]["price_step"]),
                    max_leverage=float(by[c]["max_leverage"]), price=float(by[c]["price"]))
            for c in (coins or BASKET)}
