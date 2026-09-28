"""Strategy S1, chosen by the user on 2026-09-26: single source of truth for paper, live and backtest.

Every day after the 05:30 IST close, for each basket coin, 9 Donchian trend "judges" (5..360 days) vote UP/not;
position = votes x volatility scaling (50% vol target), rounded to what Mudrex accepts. This is the legacy
two-sided strategy; production entries are migration-gated until the owner's new contract is certified. Exits: the
judges' trailing midpoint (trend exit, next open) plus a volatility-adaptive exchange bracket on every entry.
Leverage varies by coin and volatility but never increases the risk-sized quantity. Capital is capped at Rs 5,000.
Market mood: LONG entries only while BTC closes at/above its 200-day average, SHORT entries only below it;
unknown mood blocks new entries.
Evidence status: the historical `s1_audit.py` run is NOT evidence for the current owner contract or this strategy
configuration. It models legacy long-only S1 with fixed 3x ATR stop, no take-profit, fixed leverage, legacy
percentage caps, and older execution assumptions; it does not implement adaptive risk sizing, two-sided regime
selection, or the owner’s fixed-Rs500 cycle P&L stop. Its reported returns/statistics and earlier variants are
withdrawn as support for live activation. `adaptive_backtest.py` is the separate research-only path for evaluating
the newer rules; its daily-bar results are not evidence of exchange execution and are not guarantees.
"""
import config
import portfolio as pf
from trade_policy import DAILY_LOSS_LIMIT_INR

NAME = "S1 two-sided trend ensemble: adaptive bracket and leverage"
BASKET = ["XRP", "ADA", "DOGE", "LINK", "AVAX", "TRX"]
SIGNAL_KW = dict(target_vol=0.5, allow_short=True)  # 9 lookbacks; positive=LONG, negative=SHORT
LEV = 2
SL_ATR = 3
CAPITAL_CAP_INR = 5000
DAILY_CAP_INR = DAILY_LOSS_LIMIT_INR  # compatibility alias; authoritative value lives in trade_policy.py
DAILY_CAP_PCT = DAILY_CAP_INR / CAPITAL_CAP_INR  # research/dashboard compatibility; live uses the absolute cap


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
    """Absolute rupee size of both daily caps. The argument remains for API compatibility."""
    return DAILY_CAP_INR


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
    btc: BTC daily candles select the permitted hedge direction: LONG at/above the 200-day average and SHORT
    below it. Unknown mood returns signals unchanged and the CALLER must block new entries."""
    tf = pf.basket_targets(basket or BASKET, closes, equity_inr / config.INR_PER_USDT, specs)
    raw = tf(ctx, d, {})
    mood = btc_mood(btc, d) if btc is not None else None
    if mood is True:
        return {coin: weight for coin, weight in raw.items() if weight > 0}
    if mood is False:
        return {coin: weight for coin, weight in raw.items() if weight < 0}
    return raw


def specs_from_listing(rows, coins=None):
    by = {r["symbol"].removesuffix("USDT"): r for r in rows}
    return {c: dict(step=float(by[c]["quantity_step"]), min_qty=float(by[c]["min_contract"]),
                    min_notional=float(by[c]["min_notional_value"]), price_step=float(by[c]["price_step"]),
                    max_leverage=float(by[c]["max_leverage"]), price=float(by[c]["price"]),
                    funding_fee_perc_hour=(float(by[c]["funding_fee_perc"])
                                           if by[c].get("funding_fee_perc") is not None else None))
            for c in (coins or BASKET)}
