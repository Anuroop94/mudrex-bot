"""Daily portfolio simulator for continuous-position strategies (volatility targeting, ensembles).

Universe: every Mudrex contract with >= MIN_USD_VOL_24H today and daily kline history. Each day the strategy
may only hold the TOP_N coins ranked by trailing 30-day median dollar volume (rotational, no hand-picked list).
Weights decided at day d close, traded at day d+1 open, earn open(d+1) -> open(d+2).
Costs: |weight change| * (fee*(1+GST) + slippage); funding on |weight| every day held.

ponytail: survivorship bias. Universe = coins listed and liquid TODAY; coins delisted since are missing,
which flatters every long-only result. Fix needs a historical listings archive we do not have.
"""
import calendar
import math
import statistics as st
from collections import deque

import config
import data
import pick_coins
import strategy

DAY = 86400
MIN_USD_VOL_24H = 2_000_000       # paper used $2M median daily volume
SLIPPAGE = 0.0005                 # 0.05%/side: wider than majors-only tests, universe includes thinner coins
SPLIT = calendar.timegm((2025, 9, 25, 0, 0, 0))  # development data < SPLIT <= locked hold-out


def prior_extremes(xs, n):
    """hi[i], lo[i] = max/min of xs[i-n:i] (the n values BEFORE i); None until n values exist. O(len) deques."""
    hi, lo = [None] * len(xs), [None] * len(xs)
    qmax, qmin = deque(), deque()
    for i in range(len(xs)):
        if i >= n:
            hi[i], lo[i] = xs[qmax[0]], xs[qmin[0]]
        # add xs[i] for future windows, drop indices that fall out of [i-n+1, i]
        while qmax and xs[qmax[-1]] <= xs[i]:
            qmax.pop()
        while qmin and xs[qmin[-1]] >= xs[i]:
            qmin.pop()
        qmax.append(i)
        qmin.append(i)
        while qmax[0] <= i - n:
            qmax.popleft()
        while qmin[0] <= i - n:
            qmin.popleft()
    return hi, lo


def cost_per_turnover():
    return config.TAKER_FEE * (1 + config.GST) + SLIPPAGE


def load_universe(days=2400, min_vol=MIN_USD_VOL_24H):
    rows = pick_coins.listing()
    coins = [r["symbol"].removesuffix("USDT") for r in rows if float(r["volume"]) * float(r["price"]) >= min_vol]
    out = {}
    for n, c in enumerate(coins, 1):
        try:
            cs = data.load(days, f"{c}/USDT", "1d", DAY)
        except Exception as e:  # symbol without kline data
            print(f"  skip {c}: {e}", flush=True)
            continue
        if len(cs) >= 60:
            out[c] = cs
        if n % 25 == 0:
            print(f"  loaded {n}/{len(coins)}", flush=True)
    return out


# ---------- signals: each returns {day_ts: target weight in [-1, 1]} for one coin, decided at that day's close

def zarattini(candles, lookbacks=(5, 10, 20, 30, 60, 90, 150, 250, 360), target_vol=0.25, vol_len=90,
              allow_short=False, ann=365):
    """Ensemble Donchian (close-based) with midpoint trailing stop, sized to target vol, cap 1x."""
    closes = [c[4] for c in candles]
    n = len(closes)
    rets = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, n)]
    state = {L: dict(side=0, stop=None) for L in lookbacks}
    ext = {L: prior_extremes(closes, L) for L in lookbacks}   # prior L closes, excludes today
    out = {}
    for i in range(1, n):
        votes = 0
        for L in lookbacks:
            s = state[L]
            hi, lo = ext[L][0][i], ext[L][1][i]
            if hi is None:
                continue
            mid = (hi + lo) / 2
            if s["side"] == 1:
                s["stop"] = max(s["stop"], mid)
                if closes[i] < s["stop"]:
                    s["side"] = 0
            elif s["side"] == -1:
                s["stop"] = min(s["stop"], mid)
                if closes[i] > s["stop"]:
                    s["side"] = 0
            if s["side"] == 0:
                if closes[i] > hi:
                    s.update(side=1, stop=mid)
                elif allow_short and closes[i] < lo:
                    s.update(side=-1, stop=mid)
            votes += s["side"]
        if i < vol_len:
            continue
        vol = st.pstdev(rets[i - vol_len + 1:i + 1]) * math.sqrt(ann)   # ann = bars per year
        scale = min(target_vol / vol, 1.0) if vol > 0 else 0.0
        out[candles[i][0]] = votes / len(lookbacks) * scale
    return out


def ema_tsmom(candles, pairs=((8, 32), (16, 64), (32, 128)), target_vol=0.25, vol_len=90, allow_short=False):
    """Time-series momentum: average sign of fast-minus-slow EMA over several horizons, sized to target vol, cap 1x."""
    closes = [c[4] for c in candles]
    n = len(closes)
    rets = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, n)]
    emas = [(strategy.ema(closes, f), strategy.ema(closes, s), s) for f, s in pairs]
    warm = 3 * max(s for _, s in pairs)
    out = {}
    for i in range(max(warm, vol_len), n):
        votes = sum((1 if ef[i] > es[i] else -1) for ef, es, _ in emas) / len(pairs)
        if not allow_short:
            votes = max(votes, 0.0)
        vol = st.pstdev(rets[i - vol_len + 1:i + 1]) * math.sqrt(365)
        out[candles[i][0]] = votes * (min(target_vol / vol, 1.0) if vol > 0 else 0.0)
    return out


def basket_targets(basket, closes, equity_usd=None, specs=None):
    """Target function for a fixed basket: weight = signal / len(basket). With equity_usd + specs, weights are
    rounded to what the exchange accepts: below half the smallest order -> 0, else at least the smallest order,
    then rounded to the quantity step. ponytail: uses constant equity for rounding, not the running balance."""
    n = len(basket)

    def fn(ctx, d, w):
        out = {}
        for c in basket:
            x = ctx["sig"].get(c, {}).get(d, 0.0) / n
            if x and equity_usd and specs:
                px = closes[c].get(d)
                if not px:
                    continue
                s = specs[c]
                unit = max(s["min_notional"], s["min_qty"] * px) / equity_usd
                step = s["step"] * px / equity_usd
                if abs(x) < unit / 2:
                    continue
                x = math.copysign(max(unit, round(abs(x) / step) * step), x)
            if x:
                out[c] = x
        return out
    return fn


def simulate_trades(universe, signal_fn, target_fn, start=None, end=None, sl_atr=0.0, tp_atr=0.0, lev=1.0,
                    atr_len=14, bar_days=1.0, **kw):
    """Trade-by-trade basket simulator. Entry size = |target weight| * lev, fixed for the trade (no daily resizing).
    Exits: stop at entry -/+ sl_atr*ATR, target at entry +/- tp_atr*ATR (0 = off; stop wins if both touch in one
    bar; gaps fill at the open), or the signal turning off/flipping (next open). After a stop/target exit the coin
    re-arms only once its signal has gone flat, so it does not re-buy the same move.
    Weights are fractions of equity marked daily; costs on entry and exit notional; funding on |weight| per day."""
    ctx = prepare(universe, signal_fn, **kw)
    bars, atrs = trade_lookups(universe, atr_len)
    days = sorted({x[0] for cs in universe.values() for x in cs})
    days = [d for d in days if (start is None or d >= start) and (end is None or d < end)]
    S = new_trade_state(universe)
    curve, daily = [], []
    for k in range(len(days) - 2):
        d, d1, d2 = days[k], days[k + 1], days[k + 2]
        x_all = target_fn(ctx, d, {c: p["side"] * p["w"] for c, p in S["pos"].items()})
        r = trade_day(S, bars, atrs, list(universe), x_all, d, d1, d2, sl_atr, tp_atr, lev, bar_days)
        daily.append(r)
        curve.append((d1, S["equity"]))
    tr = S["closed"]
    out = dict(curve=curve, daily=daily, contrib={}, trades=len(tr), closed=tr,
               win_rate=sum(t["pnl"] > 0 for t in tr) / len(tr) if tr else 0, min_equity=S["low"], **stats(daily))
    out["total"] = S["equity"] - 1
    return out


def trade_lookups(universe, atr_len=14):
    bars = {c: {x[0]: x for x in cs} for c, cs in universe.items()}
    atrs = {c: dict(zip([x[0] for x in cs], strategy.atr(cs, atr_len))) for c, cs in universe.items()}
    return bars, atrs


def new_trade_state(coins):
    """JSON-serialisable state for trade_day: open positions, re-arm flags, equity (fraction of start)."""
    return dict(pos={}, armed={c: True for c in coins}, equity=1.0, low=1.0, closed=[])


def trade_day(S, bars, atrs, coins, x_all, d, d1, d2, sl_atr=0.0, tp_atr=0.0, lev=1.0, bar_days=1.0):
    """One bar of the trade-by-trade model (shared by simulate_trades and the paper bot). Named for daily bars;
    bar_days = bar length in days (1/24 for 1h) so funding is charged per time held, not per bar.
    x_all: target weights decided at d's close. Orders fill at d1's open; stop/target checked on d1's bar;
    open positions marked to d2's open. Mutates S, returns the bar's portfolio return."""
    cost, fund = cost_per_turnover(), config.FUNDING_PER_DAY * bar_days
    pending = {}
    for c in coins:
        x = x_all.get(c, 0.0)
        side = (x > 0) - (x < 0)
        if side == 0:
            S["armed"][c] = True
        p = S["pos"].get(c)
        if p and side != p["side"]:
            pending[c] = dict(exit=True)
        if side and S["armed"].get(c, True) and (not p or side != p["side"]) and d in atrs.get(c, {}):
            pending.setdefault(c, {}).update(entry=side, w=abs(x) * lev, atr=atrs[c][d])
    r = 0.0

    def close(c, p, px, reason):
        """pnl is NET of entry/exit costs and funding (fraction of starting equity at entry)."""
        costs = p.get("costs", 0.0) + cost * p["w"]
        S["closed"].append(dict(coin=c, side=p["side"], w=p["w"], entry=p["entry"], exit=px, entry_day=p["day"],
                                exit_day=d1, reason=reason, costs=costs,
                                pnl=p["side"] * (px / p["entry"] - 1) * p["w"] - costs))
        S["pos"].pop(c)

    for c in coins:
        b1, b2 = bars.get(c, {}).get(d1), bars.get(c, {}).get(d2)
        if not b1:
            continue
        o1, h1, l1 = b1[1], b1[2], b1[3]
        p, pend = S["pos"].get(c), pending.get(c, {})
        if p and pend.get("exit"):
            r += p["side"] * p["w"] * (o1 / p["mark"] - 1) - cost * p["w"]
            close(c, p, o1, "trend exit")
            p = None
        if pend.get("entry") and not p:
            s = pend["entry"]
            p = S["pos"][c] = dict(side=s, w=pend["w"], entry=o1, mark=o1, day=d1, costs=cost * pend["w"],
                                   sl=o1 - s * sl_atr * pend["atr"] if sl_atr else None,
                                   tp=o1 + s * tp_atr * pend["atr"] if tp_atr else None)
            r -= cost * p["w"]
        if not p:
            continue
        s, px, why = p["side"], None, None
        if p["sl"] is not None and ((s == 1 and l1 <= p["sl"]) or (s == -1 and h1 >= p["sl"])):
            px, why = (min(o1, p["sl"]) if s == 1 else max(o1, p["sl"])), "stop-loss"
        elif p["tp"] is not None and ((s == 1 and h1 >= p["tp"]) or (s == -1 and l1 <= p["tp"])):
            px, why = p["tp"], "target"
        if px is not None:
            r += s * p["w"] * (px / p["mark"] - 1) - cost * p["w"] - fund * p["w"]
            p["costs"] = p.get("costs", 0.0) + fund * p["w"]
            close(c, p, px, why)
            S["armed"][c] = False
        elif b2:
            r += s * p["w"] * (b2[1] / p["mark"] - 1) - fund * p["w"]
            p["costs"] = p.get("costs", 0.0) + fund * p["w"]
            p["mark"] = b2[1]
    S["equity"] = max(S["equity"] * (1 + r), 0.0)
    S["low"] = min(S["low"], S["equity"])
    return r


# ---------- simulator

def prepare(universe, signal_fn, **kw):
    """Per-coin signals and lookups shared by simulate() and the paper bot."""
    return dict(coins=list(universe),
                sig={c: signal_fn(cs, **kw) for c, cs in universe.items()},
                opens={c: {x[0]: x[1] for x in cs} for c, cs in universe.items()},
                dvol={c: {x[0]: x[4] * x[5] for x in cs} for c, cs in universe.items()},
                first={c: cs[0][0] for c, cs in universe.items()})


def targets(ctx, d, w, top_n, min_history=365, band=0.0):
    """Target weights decided at day d's close: top_n coins by 30-day median $ volume known at d,
    weight = signal / top_n. band: keep a held coin's weight if the new one is within band*|current|."""
    elig = []
    for c in ctx["coins"]:
        if d - ctx["first"][c] < min_history * DAY or d not in ctx["sig"][c]:
            continue
        dv = ctx["dvol"][c]
        vols = [dv[x] for x in range(d - 29 * DAY, d + DAY, DAY) if x in dv]
        if len(vols) >= 20:
            elig.append((st.median(vols), c))
    chosen = {c for _, c in sorted(elig, reverse=True)[:top_n]}
    target = {c: ctx["sig"][c][d] / top_n for c in chosen if ctx["sig"][c][d]}
    if band:
        for c, x in target.items():
            cur = w.get(c, 0)
            if cur and x * cur > 0 and abs(x - cur) <= band * abs(cur):
                target[c] = cur
    return target


def mini_targets(ctx, d, w, slots, each, top_n=40, min_history=365):
    """Small-account version: hold up to `slots` coins that the full strategy wants long, each at fixed weight
    `each` (e.g. the $5 exchange minimum as a fraction of equity). Keep a held coin while the full strategy
    still wants it; fill free slots with its highest-weight picks. Long-only."""
    full = {c: x for c, x in targets(ctx, d, {}, top_n, min_history).items() if x > 0}
    keep = [c for c in w if c in full][:slots]
    for c, _ in sorted(full.items(), key=lambda kv: -kv[1]):
        if len(keep) >= slots:
            break
        if c not in keep:
            keep.append(c)
    return {c: each for c in keep}


def simulate_targets(universe, signal_fn, target_fn, start=None, end=None, **kw):
    """simulate() with a custom target function target_fn(ctx, d, w) -> weights."""
    ctx = prepare(universe, signal_fn, **kw)
    days = sorted({x[0] for cs in universe.values() for x in cs})
    days = [d for d in days if (start is None or d >= start) and (end is None or d < end)]
    w, equity, curve, daily, contrib = {}, 1.0, [], [], {}
    for k in range(len(days) - 2):
        d, d1, d2 = days[k], days[k + 1], days[k + 2]
        target = target_fn(ctx, d, w)
        r, per = day_pnl(ctx, target, w, d1, d2)
        for c, v in per.items():
            contrib[c] = contrib.get(c, 0) + v
        w = target
        equity *= 1 + r
        daily.append(r)
        curve.append((d1, equity))
    return dict(curve=curve, daily=daily, contrib=contrib, **stats(daily))


def day_pnl(ctx, target, w, d1, d2):
    """Return of switching w -> target at d1's open and holding to d2's open, net of costs.
    Returns (portfolio return, {coin: contribution}); each coin carries its own costs."""
    cost, fund = cost_per_turnover(), config.FUNDING_PER_DAY
    r, per = 0.0, {}
    for c in set(target) | set(w):
        x = target.get(c, 0)
        pr = -abs(x - w.get(c, 0)) * cost - abs(x) * fund
        o = ctx["opens"].get(c, {})
        if x and d1 in o and d2 in o:
            pr += x * (o[d2] / o[d1] - 1)
        r += pr
        per[c] = pr
    return r, per


def simulate(universe, signal_fn, top_n=20, start=None, end=None, min_history=365, band=0.0, **kw):
    """Returns daily equity path + stats. Weights per coin = signal / top_n (so gross <= 1 when signals <= 1).
    band: skip resizing a held coin when the new weight is within band*|current| (cuts vol-scaling churn);
    entries, exits and direction flips always trade."""
    ctx = prepare(universe, signal_fn, **kw)
    days = sorted({x[0] for cs in universe.values() for x in cs})
    days = [d for d in days if (start is None or d >= start) and (end is None or d < end)]
    w = {}
    equity, curve, daily, contrib = 1.0, [], [], {}
    for k in range(len(days) - 2):
        d, d1, d2 = days[k], days[k + 1], days[k + 2]
        target = targets(ctx, d, w, top_n, min_history, band)
        r, per = day_pnl(ctx, target, w, d1, d2)
        for c, v in per.items():
            contrib[c] = contrib.get(c, 0) + v
        w = target
        equity *= 1 + r
        daily.append(r)
        curve.append((d1, equity))
    return dict(curve=curve, daily=daily, contrib=contrib, **stats(daily))


def stats(daily):
    if len(daily) < 30:
        return dict(cagr=0, sharpe=0, max_dd=0, tstat=0, pf=0, exposure_days=0)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in daily:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    m, s = st.mean(daily), st.pstdev(daily)
    gp, gl = sum(r for r in daily if r > 0), -sum(r for r in daily if r < 0)
    years = len(daily) / 365
    return dict(cagr=eq ** (1 / years) - 1, sharpe=m / s * math.sqrt(365) if s else 0, max_dd=mdd,
                tstat=m / (s / math.sqrt(len(daily))) if s else 0, pf=gp / gl if gl else 0,
                total=eq - 1, exposure_days=sum(1 for r in daily if r != 0))


def buy_hold(universe, start, end, top_n=20, min_history=365):
    """Benchmark: equal-weight long the same rotating top-N universe, always fully invested (no signal)."""
    return simulate(universe, lambda cs: {x[0]: 1.0 for x in cs}, top_n, start, end, min_history)


def fmt(r):
    return (f"CAGR {r['cagr']:+7.1%}  Sharpe {r['sharpe']:5.2f}  maxDD {r['max_dd']:6.1%}  "
            f"t {r['tstat']:5.2f}  PF(daily) {r['pf']:4.2f}  total {r.get('total', 0):+7.1%}")
