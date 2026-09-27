"""S1 audit (Codex + Freebuff reviews, 2026-09-27): ONE continuous, stateful simulation of S1 as it is traded live.

Parity with the live path (live_trader.plan + execution.run_open):
  - decision at day d's close with s1 signals, rounding on the bot's current equity, notional = w x LEV x
    min(equity, Rs 5,000), BTC mood filter (unknown mood blocks entries), re-arm after a stop only once the trend
    resets, 5% daily caps that block NEW entries (measured from IST midnight), execution drift check (2%),
    HEDGE_BUFFER sizing, total bot exposure <= LEV x Rs 5,000, exchange stop = fill - 3 x ATR, gaps fill at the open.
  - fills happen `delay_h` hours after the daily open (approval latency) on HOURLY bars; stops are checked hourly;
    funding is charged per hour held. Days without hourly data fall back to the daily bar (fill at the open).
  - every trade's P&L is NET: fees + GST, slippage and funding included.
Positions carry over across the dev / hold-out boundary (no reset). The post-SPLIT year has been looked at many
times, so it is reported as DESCRIPTIVE only; the development period is the evidence-grade part.

ponytail: known limits: today's Mudrex contract specs and a constant 102 INR/USDT are applied to history; the
universe is today's survivors (delisted coins are missing, which flatters long-only results).

Run: python s1_audit.py [parity stress sizing loo mood volcap]   (no argument = all)
"""
import math
import random
import statistics as st
import sys

import config
import data
import execution
import pick_coins
import portfolio as pf
import s1

DAY, HOUR = 86400, 3600
RATE = config.INR_PER_USDT
FEE = config.TAKER_FEE * (1 + config.GST)
N_TRIALS = 60   # rough count of strategy variants tried during the research (for the deflated Sharpe)

BASE = dict(basket=s1.BASKET, lev=s1.LEV, sl_atr=s1.SL_ATR, delay_h=0, slip=pf.SLIPPAGE,
            fund=config.FUNDING_PER_DAY, stop_slip=0.0, rounding="capped", risk=None, agg_risk=None,
            mood="one", volcap=False, caps=True, no_chase=None, alloc=s1.CAPITAL_CAP_INR, cap_pct=s1.DAILY_CAP_PCT)


# ---------- data

def load_all(basket):
    coins = sorted(set(basket) | set(s1.BASKET))
    daily = {c: data.load(2400, f"{c}/USDT", "1d", DAY) for c in coins}
    hourly = {}
    for c in coins:
        try:
            hourly[c] = {x[0]: x for x in data.load(2400, f"{c}/USDT", "1h", HOUR)}
        except Exception as e:                       # noqa: BLE001 - fall back to daily bars
            print(f"  no hourly data for {c}: {e}")
            hourly[c] = {}
    btc = data.load(2400, "BTC/USDT", "1d", DAY)
    rows = {r["symbol"].removesuffix("USDT"): r for r in pick_coins.listing()}
    specs = {c: dict(step=float(rows[c]["quantity_step"]), min_qty=float(rows[c]["min_contract"]),
                     min_notional=float(rows[c]["min_notional_value"])) for c in coins}
    return dict(daily=daily, hourly=hourly, btc=btc, specs=specs)


def btc_vol(btc):
    """{day: 30-day annualized BTC volatility}."""
    closes = [x[4] for x in btc]
    out = {}
    for i in range(31, len(btc)):
        rets = [closes[j] / closes[j - 1] - 1 for j in range(i - 29, i + 1)]
        out[btc[i][0]] = st.pstdev(rets) * math.sqrt(365)
    return out


# ---------- the simulation

def run(D, **over):
    P = dict(BASE, **over)
    basket = P["basket"]
    uni = {c: D["daily"][c] for c in basket}
    ctx = pf.prepare(uni, pf.zarattini, **s1.SIGNAL_KW)
    _, atrs = pf.trade_lookups(uni)
    closes = {c: {x[0]: x[4] for x in cs} for c, cs in uni.items()}
    dbars = {c: {x[0]: x for x in cs} for c, cs in uni.items()}
    hb = D["hourly"]
    vol = btc_vol(D["btc"]) if P["volcap"] else {}
    days = sorted({x[0] for cs in uni.values() for x in cs})

    cash, pos, armed = float(P["alloc"]), {}, {c: True for c in basket}
    mood_on, mood_streak, lev_hi = True, 0, True
    marks, trades, skipped_small, signals = [], [], 0, 0
    hourly_days = 0

    def price_at(c, t):
        b = hb[c].get(t)
        return b[1] if b else None

    def equity(t):
        u = 0.0
        for c, p in pos.items():
            px = price_at(c, t) or closes[c].get(t - DAY) or p["entry"]
            u += p["qty"] * (px - p["entry"]) * RATE
        return cash + u

    def close(c, px, t, why):
        nonlocal cash
        p = pos.pop(c)
        fee = p["qty"] * px * RATE * FEE
        gross = p["qty"] * (px - p["entry"]) * RATE
        cash += gross - fee
        trades.append(dict(coin=c, entry_t=p["t"], exit_t=t, why=why,
                           net=gross - fee - p["fee_in"] - p["funding"], notional=p["qty"] * p["entry"] * RATE))

    def walk(h0, h1):
        """Hourly stop checks + funding for every open position over [h0, h1)."""
        nonlocal cash
        for c in list(pos):
            p = pos[c]
            for h in range(max(h0, p["t"]), h1, HOUR):
                b = hb[c].get(h)
                if not b:
                    continue
                f = p["qty"] * b[1] * RATE * P["fund"] / 24
                cash -= f
                p["funding"] += f
                if b[3] <= p["sl"]:
                    close(c, min(b[1], p["sl"]) * (1 - P["slip"] - P["stop_slip"]), h, "stop-loss")
                    armed[c] = False
                    break

    for k in range(len(days) - 1):
        d, d1 = days[k], days[k] + DAY
        if not any(d1 in dbars[c] for c in basket):
            continue
        use_h = all(price_at(c, d1) is not None for c in basket if d1 in dbars[c])
        hourly_days += use_h
        ist_start = d1 - 5 * HOUR                                          # 19:00 UTC ~= IST midnight
        day_start_eq = equity(ist_start) if use_h else equity(d1)
        plan_eq = equity(d1)
        marks.append((d1, plan_eq))

        # ---- decision at d's close (the plan)
        m = s1.btc_mood(D["btc"], d)
        if P["mood"] == "two":
            if m is False:
                mood_on, mood_streak = False, 0
            elif m is True:
                mood_streak += 1
                if not mood_on and mood_streak >= 2:
                    mood_on = True
            mood_ok = mood_on if m is not None else None
        elif P["mood"] == "none":
            mood_ok = True
        else:
            mood_ok = m
        lev = P["lev"]
        if P["volcap"]:
            v = vol.get(d)
            if v is not None:
                lev_hi = v < 0.8 if not lev_hi else v <= 1.0
            lev = P["lev"] if lev_hi else 1.0
        size_eq = min(plan_eq, P["alloc"])
        want = {}
        if mood_ok is not False:
            if P["rounding"] in ("live", "capped"):
                round_eq = plan_eq if P["rounding"] == "live" else size_eq    # capped: same equity as sizing
                want = pf.basket_targets(basket, closes, round_eq / RATE, D["specs"])(ctx, d, {})
            else:
                want = {c: ctx["sig"][c].get(d, 0.0) / len(basket) for c in basket}
                want = {c: x for c, x in want.items() if x > 0}
        for c in basket:
            if want.get(c, 0) <= 0:
                armed[c] = True

        # ---- execution at fill time t_fill (approval latency); stops keep working while you decide
        t_fill = d1 + P["delay_h"] * HOUR if use_h else d1
        if use_h:
            walk(d1, t_fill)

        def fill_px(c):
            return price_at(c, t_fill) if use_h else dbars[c].get(d1, [None, None])[1]

        for c in list(pos):                                               # closes first (execution order)
            if want.get(c, 0) <= 0 and fill_px(c):
                close(c, fill_px(c) * (1 - P["slip"]), t_fill, "trend exit")
        blocked = mood_ok is None
        if P["caps"]:
            now_eq = equity(t_fill) if use_h else plan_eq
            if abs(now_eq - day_start_eq) >= P["cap_pct"] * day_start_eq:
                blocked = True
        for c in basket:
            w, px = want.get(c, 0), fill_px(c)
            if w <= 0 or c in pos or not armed[c] or not px or d not in atrs[c] or d not in closes[c]:
                continue
            signals += 1
            if blocked:
                continue
            ref, atr = closes[c][d], atrs[c][d]
            if abs(px / ref - 1) > execution.MAX_DRIFT:
                continue
            if P["no_chase"] is not None and px > ref + P["no_chase"] * atr:
                continue
            notional = w * lev * size_eq
            if P["risk"]:
                notional = min(notional, P["risk"] * plan_eq / (P["sl_atr"] * atr / px))
            held = sum(p["qty"] * p["entry"] * RATE for p in pos.values())
            if held + notional > P["lev"] * P["alloc"]:
                continue
            spec = D["specs"][c]
            qty = math.floor(notional / (RATE * execution.HEDGE_BUFFER) / px / spec["step"] + 1e-9) * spec["step"]
            if qty < spec["min_qty"] or qty * px < spec["min_notional"]:
                skipped_small += 1
                continue
            if P["agg_risk"]:
                risk_now = sum(p["qty"] * p["risk_px"] * RATE for p in pos.values())
                if risk_now + qty * P["sl_atr"] * atr * RATE > P["agg_risk"] * plan_eq:
                    continue
            entry = px * (1 + P["slip"])
            fee_in = qty * entry * RATE * FEE
            cash -= fee_in
            pos[c] = dict(qty=qty, entry=entry, sl=px - P["sl_atr"] * atr, risk_px=P["sl_atr"] * atr, t=t_fill,
                          fee_in=fee_in, funding=0.0)

        # ---- rest of the day's price path: hourly stops + funding (daily bar fallback)
        if use_h:
            walk(t_fill, d1 + DAY)
            continue
        for c in list(pos):
            p, b = pos[c], dbars[c].get(d1)
            if not b:
                continue
            f = p["qty"] * b[1] * RATE * P["fund"]
            cash -= f
            p["funding"] += f
            if b[3] <= p["sl"]:
                close(c, min(b[1], p["sl"]) * (1 - P["slip"] - P["stop_slip"]), d1, "stop-loss")
                armed[c] = False

    daily = [(marks[i + 1][0], marks[i + 1][1] / marks[i][1] - 1) for i in range(len(marks) - 1)]
    return dict(P=P, daily=daily, trades=trades, skipped_small=skipped_small, signals=signals,
                hourly_share=hourly_days / max(1, len(marks)), end_equity=marks[-1][1] if marks else P["alloc"])


# ---------- statistics (Codex #4: no IID t-stat on its own)

def newey_west_t(x, lags=10):
    n, m = len(x), st.mean(x)
    e = [v - m for v in x]
    var = sum(v * v for v in e) / n
    for L in range(1, lags + 1):
        cov = sum(e[i] * e[i - L] for i in range(L, n)) / n
        var += 2 * (1 - L / (lags + 1)) * cov
    return m / math.sqrt(var / n) if var > 0 else 0.0


def block_bootstrap(x, block=7, n=2000, seed=7):
    """Weekly moving-block bootstrap: 90% interval of the annualized mean return and P(mean > 0)."""
    rnd, L = random.Random(seed), len(x)
    means = []
    for _ in range(n):
        s = []
        while len(s) < L:
            i = rnd.randrange(0, L - block)
            s.extend(x[i:i + block])
        means.append(st.mean(s[:L]) * 365)
    means.sort()
    return means[int(0.05 * n)], means[int(0.95 * n)], sum(v > 0 for v in means) / n


def deflated_sharpe_prob(x, trials=N_TRIALS, sr_trial_sd=0.5 / math.sqrt(365)):
    """Bailey & Lopez de Prado: P(true Sharpe > best-of-`trials` luck), daily units. sr_trial_sd is an
    assumed spread of daily Sharpe across the variants tried (0.5 annual)."""
    n, m, s = len(x), st.mean(x), st.pstdev(x)
    if not s or n < 30:
        return 0.0
    sr = m / s
    g3 = sum(((v - m) / s) ** 3 for v in x) / n
    g4 = sum(((v - m) / s) ** 4 for v in x) / n
    emc = 0.5772156649
    z = lambda p: st.NormalDist().inv_cdf(p)                                    # noqa: E731
    sr0 = sr_trial_sd * ((1 - emc) * z(1 - 1 / trials) + emc * z(1 - 1 / (trials * math.e)))
    den = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    return st.NormalDist().cdf((sr - sr0) * math.sqrt(n - 1) / den)


def summarize(r, a=None, b=None):
    xs = [v for t, v in r["daily"] if (a is None or t >= a) and (b is None or t < b)]
    tr = [t for t in r["trades"] if (a is None or t["exit_t"] >= a) and (b is None or t["exit_t"] < b)]
    if len(xs) < 30:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for v in xs:
        eq *= 1 + v
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    m, s = st.mean(xs), st.pstdev(xs)
    wins = [t["net"] for t in tr if t["net"] > 0]
    losses = [t["net"] for t in tr if t["net"] <= 0]
    lo, hi, p_pos = block_bootstrap(xs)
    return dict(total=eq - 1, cagr=eq ** (365 / len(xs)) - 1, max_dd=mdd, sharpe=m / s * math.sqrt(365) if s else 0,
                t_iid=m / (s / math.sqrt(len(xs))) if s else 0, t_nw=newey_west_t(xs), boot_lo=lo, boot_hi=hi,
                p_pos=p_pos, dsr=deflated_sharpe_prob(xs), trades=len(tr),
                win=len(wins) / len(tr) if tr else 0, avg_win=st.mean(wins) if wins else 0,
                avg_loss=st.mean(losses) if losses else 0, worst_day=min(xs) * s1.CAPITAL_CAP_INR)


def line(label, r):
    parts = []
    for name, a, b in (("DEV", None, pf.SPLIT), ("POST*", pf.SPLIT, None)):
        s = summarize(r, a, b)
        if not s:
            parts.append(f"{name} n/a")
            continue
        parts.append(f"{name} {s['total']:+7.1%} CAGR {s['cagr']:+6.1%} DD {s['max_dd']:5.1%} Sh {s['sharpe']:5.2f} "
                     f"tNW {s['t_nw']:4.2f} tr {s['trades']:>3} win {s['win']:4.0%}")
    return f"{label:<44} " + " | ".join(parts)


def detail(label, r):
    s = summarize(r, None, pf.SPLIT)
    print(f"\n{label}  (development period, continuous state; hourly execution on {r['hourly_share']:.0%} of days)")
    print(f"  total {s['total']:+.1%}  CAGR {s['cagr']:+.1%}  max DD {s['max_dd']:.1%}  worst day Rs {s['worst_day']:+,.0f}")
    print(f"  Sharpe {s['sharpe']:.2f}  t: IID {s['t_iid']:.2f} vs Newey-West {s['t_nw']:.2f}  "
          f"bootstrap 90% annual return [{s['boot_lo']:+.1%}, {s['boot_hi']:+.1%}]  P(edge>0) {s['p_pos']:.0%}")
    print(f"  deflated Sharpe (vs luck of ~{N_TRIALS} variants tried): {s['dsr']:.0%}")
    print(f"  {s['trades']} trades, NET win rate {s['win']:.0%}, avg net win Rs {s['avg_win']:+,.0f}, "
          f"avg net loss Rs {s['avg_loss']:+,.0f}; buy-signal coin-days skipped as too small for Mudrex: {r['skipped_small']}/{r['signals']}")
    p = summarize(r, pf.SPLIT, None)
    if p:
        print(f"  POST-SPLIT (descriptive only, not evidence): {p['total']:+.1%}  DD {p['max_dd']:.1%}  "
              f"{p['trades']} trades")


# ---------- sections

def parity(D):
    r = run(D)
    detail("S1 exactly as traded live (fills at the daily open)", r)
    return r


def stress(D):
    print("\nApproval latency, costs and stop slippage (Codex #3). No-chase = skip entry if price ran > 0.5 ATR.")
    for label, kw in [("fill +0h (baseline)", {}), ("fill +1h", dict(delay_h=1)), ("fill +3h", dict(delay_h=3)),
                      ("fill +1h, no-chase 0.5 ATR", dict(delay_h=1, no_chase=0.5)),
                      ("fill +3h, no-chase 0.5 ATR", dict(delay_h=3, no_chase=0.5)),
                      ("slippage 0.10%/side", dict(slip=0.001)), ("slippage 0.20%/side", dict(slip=0.002)),
                      ("funding 0.06%/day", dict(fund=0.0006)), ("funding 0.10%/day", dict(fund=0.001)),
                      ("stop slips 0.25% past trigger", dict(stop_slip=0.0025)),
                      ("stop slips 0.50% past trigger", dict(stop_slip=0.005)),
                      ("ALL bad: +3h, 0.2% slip, 0.1% fund, 0.5% stop", dict(delay_h=3, slip=0.002, fund=0.001,
                                                                          stop_slip=0.005))]:
        print(line(label, run(D, **kw)), flush=True)


def sizing(D):
    print("\nSizing (Codex/Freebuff #2): round on FINAL 2x notional (never up) and cap stop-risk per trade.")
    for label, kw in [("OLD live rounding (bug, before 2026-09-27)", dict(rounding="live")),
                      ("rounding on the same capped equity (current S1)", {}),
                      ("final-notional rounding, never up", dict(rounding="final")),
                      ("final + 1% stop-risk per trade", dict(rounding="final", risk=0.01)),
                      ("final + 0.5% stop-risk per trade", dict(rounding="final", risk=0.005)),
                      ("final + 1% per trade + 3% total", dict(rounding="final", risk=0.01, agg_risk=0.03))]:
        r = run(D, **kw)
        print(line(label, r) + f"  too-small coin-days {r['skipped_small']}/{r['signals']}", flush=True)


def loo(D):
    print("\nLeave-one-coin-out and per-coin NET contribution (Codex/Freebuff #5; fragility check, not selection).")
    base = run(D)
    contrib = {}
    for t in base["trades"]:
        if t["exit_t"] < pf.SPLIT:
            contrib[t["coin"]] = contrib.get(t["coin"], 0) + t["net"]
    print("  per-coin net P&L (dev): " + ", ".join(f"{c} Rs {v:+,.0f}" for c, v in sorted(contrib.items(),
                                                                                         key=lambda kv: -kv[1])))
    for c in s1.BASKET:
        print(line(f"without {c}", run(D, basket=[x for x in s1.BASKET if x != c])), flush=True)


def mood(D):
    print("\nBTC mood filter variants (#6): re-entry after 1 vs 2 closes above the 200-day average.")
    for label, kw in [("mood: 1 close (current)", {}), ("mood: 2 closes to re-enter", dict(mood="two")),
                      ("no mood filter", dict(mood="none"))]:
        print(line(label, run(D, **kw)), flush=True)


def volcap(D):
    print("\nHigh-volatility mode (#8): 1x when BTC 30-day vol > 100%, back to 2x below 80%.")
    for label, kw in [("always 2x (current)", {}), ("1x in high BTC volatility", dict(volcap=True))]:
        print(line(label, run(D, **kw)), flush=True)


SECTIONS = dict(parity=parity, stress=stress, sizing=sizing, loo=loo, mood=mood, volcap=volcap)

if __name__ == "__main__":
    want = sys.argv[1:] or list(SECTIONS)
    D = load_all(s1.BASKET)
    print("DEV = development period (before 2025-09-25): evidence-grade.  POST* = the year after: DESCRIPTIVE ONLY "
          "(looked at many times).  tNW = Newey-West t-stat (autocorrelation-robust).")
    for name in want:
        SECTIONS[name](D)
