"""Strategy S2: intraday (1h) breakout setups, ranked by a self-learning model, 2-5 trades per IST day, 5% day caps.

Setup: 1h close breaks above the previous 24h high (buy-only).
Every setup gets features known at that bar's close; a logistic-regression model ("meta-labeling",
Lopez de Prado) estimates P(target before stop). The model is re-fit every month on all past setups only
(walk-forward: never sees the month it trades). Each IST day the bot takes the best-scored setups:
  - at most MAX_TRADES; a setup must score above the model's own training cut-off,
  - but if fewer than MIN_TRADES are taken by LATE_HOUR (IST), the best remaining setups are taken anyway.
Every trade: stop = entry - SL_ATR*ATR(1h), target = entry + RR*stop distance, max hold MAX_HOLD hours.
Size: risks RISK_PER_TRADE of equity (limited by remaining day loss budget and 2x total exposure).
Day caps: day P&L (realized + open, marked hourly) >= +CAP or <= -CAP -> close everything, stop for the day.
"""
import math

import numpy as np

import config
import portfolio as pf
import strategy

H = 3600
IST = config.IST_OFFSET
LOOKBACK_H = 24
SL_ATR = 1.5
RR = 2.0
MAX_HOLD = 48
MIN_TRADES, MAX_TRADES = 2, 5
LATE_HOUR = 20                 # IST hour after which the minimum-trades rule kicks in
CAP_PCT = 0.05
RISK_PER_TRADE = 0.02          # of equity, before cap-budget limits
LEV = 2.0
FEATURES = ["brk_atr", "ret24_atr", "vol_ratio", "daily_trend", "atr_pct", "rsi", "hour_sin", "hour_cos"]


def daily_from_hourly(c1h):
    days = {}
    for x in c1h:
        days.setdefault(x[0] // 86400 * 86400, []).append(x)
    return [[d, g[0][1], max(y[2] for y in g), min(y[3] for y in g), g[-1][4], sum(y[5] for y in g)]
            for d, g in sorted(days.items()) if len(g) == 24]


def setups(coin, c):
    """All breakout setups for one coin with features and the realized trade outcome (R multiple after costs)."""
    n = len(c)
    o, h, l, cl, v = (np.array([x[k] for x in c]) for k in (1, 2, 3, 4, 5))
    atr = np.array(strategy.atr(c, 14))
    rsi = np.array(strategy.rsi(list(cl), 14))
    hh, _ = pf.prior_extremes(list(h), LOOKBACK_H)
    daily = daily_from_hourly(c)
    dsig = pf.zarattini(daily, target_vol=0.5) if len(daily) > 400 else {}
    cost = 2 * pf.cost_per_turnover()
    out = []
    for i in range(400, n - MAX_HOLD - 1):
        if hh[i] is None or cl[i] <= hh[i] or cl[i - 1] > (hh[i - 1] or 1e18) or atr[i] <= 0:
            continue
        t = c[i][0]
        prev_day = t // 86400 * 86400 - 86400
        vol_avg = v[i - 24:i].mean()
        f = dict(brk_atr=(cl[i] - hh[i]) / atr[i], ret24_atr=(cl[i] - cl[i - 24]) / atr[i],
                 vol_ratio=v[i] / vol_avg if vol_avg > 0 else 1.0, daily_trend=dsig.get(prev_day, 0.0),
                 atr_pct=atr[i] / cl[i], rsi=rsi[i] / 100,
                 hour_sin=math.sin(2 * math.pi * ((t + IST) % 86400) / 86400),
                 hour_cos=math.cos(2 * math.pi * ((t + IST) % 86400) / 86400))
        entry = o[i + 1]
        stop, target = entry - SL_ATR * atr[i], entry + RR * SL_ATR * atr[i]
        exit_px, exit_i = cl[i + MAX_HOLD], i + MAX_HOLD
        for j in range(i + 1, i + 1 + MAX_HOLD):
            if l[j] <= stop:
                exit_px, exit_i = min(o[j], stop), j
                break
            if h[j] >= target:
                exit_px, exit_i = target, j
                break
        risk = (entry - stop) / entry
        ret = exit_px / entry - 1 - cost
        out.append(dict(coin=coin, t=t, i=i, entry=entry, stop=stop, target=target, exit_t=c[exit_i][0],
                        path=None, ret=ret, R=ret / risk, risk=risk, y=int(ret > 0), f=f))
    return out


class Model:
    """Logistic regression, numpy only, L2-regularised, standardised features."""

    def fit(self, X, y, l2=1.0, iters=400, lr=0.1):
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        Z = (X - self.mu) / self.sd
        w, b = np.zeros(Z.shape[1]), math.log(max(y.mean(), 1e-3) / max(1 - y.mean(), 1e-3))
        for _ in range(iters):
            p = 1 / (1 + np.exp(-(Z @ w + b)))
            w -= lr * (Z.T @ (p - y) / len(y) + l2 * w / len(y))
            b -= lr * (p - y).mean()
        self.w, self.b = w, b
        return self

    def predict(self, X):
        return 1 / (1 + np.exp(-(((X - self.mu) / self.sd) @ self.w + self.b)))


def matrix(rows):
    return np.array([[r["f"][k] for k in FEATURES] for r in rows])


def score_walk_forward(all_setups, min_train_days=365):
    """Adds r['p'] (model score) and r['cut'] (acceptance cut-off) to each setup, fitting monthly on the past only."""
    all_setups.sort(key=lambda r: r["t"])
    if not all_setups:
        return
    month = lambda t: (t + IST) // (30 * 86400)
    first = all_setups[0]["t"]
    by_month = {}
    for r in all_setups:
        by_month.setdefault(month(r["t"]), []).append(r)
    for m in sorted(by_month):
        rows = by_month[m]
        train = [r for r in all_setups if r["exit_t"] < rows[0]["t"]]    # outcome known before month starts
        if rows[0]["t"] - first < min_train_days * 86400 or len(train) < 300:
            for r in rows:
                r["p"], r["cut"] = None, None
            continue
        model = Model().fit(matrix(train), np.array([r["y"] for r in train], dtype=float))
        cut = float(np.quantile(model.predict(matrix(train)), 0.6))     # only the model's top 40% qualify
        for r, p in zip(rows, model.predict(matrix(rows))):
            r["p"], r["cut"] = float(p), cut


def simulate(all_setups, hourly, equity_inr=5000.0, use_model=True, start=None, end=None):
    """Portfolio simulation with the day rules. hourly: {coin: {t: (o,h,l,c)}} for marking and cap exits."""
    eq = 1.0
    by_hour = {}
    for r in all_setups:
        if (start is None or r["t"] >= start) and (end is None or r["t"] < end) and (not use_model or r["p"] is not None):
            by_hour.setdefault(r["t"], []).append(r)
    if not by_hour:
        return None
    t0, t1 = min(by_hour), max(by_hour)
    hours = range(t0, t1 + MAX_HOLD * H, H)
    cost1 = pf.cost_per_turnover()
    open_pos, day, day_start_eq, taken, stopped, days_log, trades = [], None, eq, 0, False, [], []
    mark = lambda pos, t: hourly[pos["coin"]].get(t)
    for t in hours:
        d = (t + IST) // 86400
        if d != day:
            if day is not None:
                days_log.append(dict(day=day, trades=taken, pnl=eq / day_start_eq - 1, capped=stopped))
            day, day_start_eq, taken, stopped = d, eq, 0, False
        # manage open positions on this bar
        still = []
        for p in open_pos:
            bar = mark(p, t)
            if not bar:
                still.append(p)
                continue
            o_, h_, l_, c_ = bar
            px = None
            if l_ <= p["stop"]:
                px = min(o_, p["stop"])
            elif h_ >= p["target"]:
                px = p["target"]
            elif t >= p["deadline"]:
                px = c_
            if px is not None:
                eq += p["n"] * (px / p["last"] - 1) - p["n"] * cost1
                trades.append(p["n"] * (px / p["entry"] - 1 - 2 * cost1))
            else:
                eq += p["n"] * (c_ / p["last"] - 1)
                p["last"] = c_
                still.append(p)
        open_pos = still
        # day caps on marked P&L: close everything at this bar's close
        day_pnl = eq / day_start_eq - 1
        if not stopped and (day_pnl >= CAP_PCT or day_pnl <= -CAP_PCT):
            for p in open_pos:
                eq -= p["n"] * cost1
                trades.append(p["n"] * (p["last"] / p["entry"] - 1 - 2 * cost1))
            open_pos, stopped = [], True
        if stopped or taken >= MAX_TRADES or t not in by_hour:
            continue
        hour_ist = ((t + IST) % 86400) // H
        cands = sorted(by_hour[t], key=lambda r: -(r["p"] if use_model else r["f"]["brk_atr"]))
        for r in cands:
            if taken >= MAX_TRADES or any(p["coin"] == r["coin"] for p in open_pos):
                continue
            good = (r["p"] >= r["cut"]) if use_model else True
            late_fill = hour_ist >= LATE_HOUR and taken < MIN_TRADES
            if not (good or late_fill):
                continue
            budget = CAP_PCT - max(0.0, -(eq / day_start_eq - 1))            # loss budget left today
            risk_frac = min(RISK_PER_TRADE, budget * 0.9)
            if risk_frac <= 0.002:
                break
            # n = notional in units of STARTING equity (eq is too), so size follows the current account
            n = min(risk_frac / r["risk"], LEV - sum(p["n"] for p in open_pos) / eq) * eq
            if n * equity_inr < 510:                                       # Mudrex minimum order
                continue
            eq -= n * cost1
            open_pos.append(dict(coin=r["coin"], n=n, entry=r["entry"], last=r["entry"], stop=r["stop"],
                                 target=r["target"], deadline=t + MAX_HOLD * H))
            taken += 1
    days_log.append(dict(day=day, trades=taken, pnl=eq / day_start_eq - 1, capped=stopped))
    daily = [x["pnl"] for x in days_log]
    s = pf.stats(daily)
    s.update(total=eq - 1, days=len(days_log), trades=len(trades),
             win_rate=sum(x > 0 for x in trades) / len(trades) if trades else 0,
             trades_per_day=len(trades) / max(len(days_log), 1),
             cap_profit_days=sum(1 for x in days_log if x["capped"] and x["pnl"] > 0),
             cap_loss_days=sum(1 for x in days_log if x["capped"] and x["pnl"] <= 0),
             worst_day=min(daily), best_day=max(daily))
    return s
