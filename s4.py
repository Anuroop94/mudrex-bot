"""Strategy S4: intraday momentum SETS (owner 2026-09-27: intraday, LONG and SHORT, all liquid coins, fixed target
AND stop on every trade). Single source of truth for research (s4_research.py) and live (live_trader.py).

At the close of each hour: among eligible coins, the single strongest 24-hour mover in the permitted direction
(LONG only when BTC closes at/above its 200-day average AND the coin's own daily trend is up; SHORT only when BTC is
below it AND the coin's daily trend is down). It is entered at the next price, with take-profit 2.0 x and stop-loss
1.0 x the coin's last closed DAILY ATR, and closed after at most 72 hours. One set at a time: the next set starts
only after the previous one has closed. Risk Rs150 per set at the stop.

Evidence (s4_research.py, 40 coins, hourly, costs = fee+GST+0.05% slippage each side + hourly funding; 2026-09-27):
28 intraday variants tested; 27 lost (most ruined the Rs5,000). This one: dev 2022-10..2025-09 +66% (2023 +41%,
2024 +23%, 2025 -4%), max drawdown 46%, Newey-West t 0.99 (NOT significant; 1 winner of 28 is luck-level),
0.52 sets/day, only 7% of days with 2+ sets. Post-split year +30% (descriptive).
FRAGILE: re-run hours later, one of the 40 coins changed (TIA for FIL, today's volume ranking): dev +19.6%,
drawdown 54%, t 0.51. Treat the edge as unproven. Backtests are not guarantees.
"""
import math

import portfolio as pf
import s1
import strategy

HOUR, DAY = 3600, 86400
NAME = "S4 intraday momentum sets: strongest coin, LONG/SHORT, TP 2x / SL 1x daily ATR, max 72h"
KIND, TP_DATR, SL_DATR, HOLD_H = "MOM", 2.0, 1.0, 72
SET_RISK_INR = 150.0          # rupees lost at the stop per set (before costs)
MAX_NOTIONAL_LEV = 3          # total notional <= 3 x the Rs5,000 allocation (as tested)
HOURLY_DAYS_LIVE = 40         # hourly history the live planner needs (24h return, 14h ATR/RSI, 20h bands)


def coin_features(cs):
    """Indicators from CLOSED hourly candles [t, o, h, l, c, v]; index i uses bars <= i only."""
    closes = [x[4] for x in cs]
    hh, ll = pf.prior_extremes([x[2] for x in cs], 24)[0], pf.prior_extremes([x[3] for x in cs], 24)[1]
    sma, sd = [None] * len(cs), [None] * len(cs)
    for i in range(20, len(cs)):
        w = closes[i - 19:i + 1]
        m = sum(w) / 20
        sma[i], sd[i] = m, math.sqrt(sum((x - m) ** 2 for x in w) / 20)
    return dict(t=[x[0] for x in cs], o=[x[1] for x in cs], h=[x[2] for x in cs], l=[x[3] for x in cs],
                c=closes, atr=strategy.atr(cs, 14), rsi=strategy.rsi(closes, 14), hh=hh, ll=ll,
                sma=sma, sd=sd, idx={x[0]: i for i, x in enumerate(cs)})


def setups(D, t, kind=KIND, regime_filter=True):
    """[(score, coin, side)] for signals at the CLOSE of hour t (entry at t+1's open), best first.
    D: coins, f (coin_features per coin), trend (daily signed signal by day), btc (daily candles)."""
    day = (t // DAY) * DAY - DAY                                    # last CLOSED daily bar
    mood = s1.btc_mood(D["btc"], day)
    if mood is None:
        return []
    out = []
    for c in D["coins"]:
        f = D["f"][c]
        i = f["idx"].get(t)
        if i is None or i < 30 or not f["atr"][i]:
            continue
        tr = D["trend"][c].get(day, 0)
        up_ok = tr > 0 and (mood or not regime_filter)
        dn_ok = tr < 0 and (not mood or not regime_filter)
        cl, a = f["c"][i], f["atr"][i]
        if kind == "BRK":
            if up_ok and f["hh"][i] and cl > f["hh"][i] and f["c"][i - 1] <= (f["hh"][i - 1] or 1e18):
                out.append(((cl - f["hh"][i]) / a, c, "LONG"))
            if dn_ok and f["ll"][i] and cl < f["ll"][i] and f["c"][i - 1] >= (f["ll"][i - 1] or 0):
                out.append(((f["ll"][i] - cl) / a, c, "SHORT"))
        elif kind == "PULL":
            if up_ok and f["rsi"][i] < 30 <= f["rsi"][i - 1]:
                out.append((30 - f["rsi"][i], c, "LONG"))
            if dn_ok and f["rsi"][i] > 70 >= f["rsi"][i - 1]:
                out.append((f["rsi"][i] - 70, c, "SHORT"))
        elif kind == "MR" and f["sd"][i]:                         # fades extremes: ignores BTC mood on purpose,
            z = (cl - f["sma"][i]) / f["sd"][i]                     # only refuses to fade the coin's own daily trend
            if z < -2.5 and f["rsi"][i] < 25 and tr >= 0:
                out.append((-z, c, "LONG"))
            if z > 2.5 and f["rsi"][i] > 75 and tr <= 0:
                out.append((z, c, "SHORT"))
        elif kind == "MOM" and i >= 24:
            r24 = cl / f["c"][i - 24] - 1
            if up_ok and r24 > 0:
                out.append((r24 / (a / cl), c, "LONG"))
            if dn_ok and r24 < 0:
                out.append((-r24 / (a / cl), c, "SHORT"))
    out.sort(reverse=True)
    if kind == "MOM":                                               # momentum: only the single strongest coin
        out = out[:1]
    return out
