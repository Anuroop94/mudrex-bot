"""Tests of the archived research strategies (backtest, risk, paper T1, paper_portfolio, S2).
Run from the repo root: python archive/test_archived.py  (no network)"""
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]                  # archived modules + the live repo root

import backtest  # noqa: E402
import config  # noqa: E402
import risk  # noqa: E402
import strategy  # noqa: E402

W = config.WARMUP
P = dict(config.DEFAULT_PARAMS)


def flat(n, px=100.0):
    return [[i * 900, px, px + 0.5, px - 0.5, px, 1.0] for i in range(n)]


def random_walk(n, seed=1):
    random.seed(seed)
    c, px = [], 100.0
    for i in range(n):
        o = px
        px *= 1 + random.gauss(0, 0.004)
        c.append([i * 900, o, max(o, px) * 1.001, min(o, px) * 0.999, px, random.uniform(1, 3)])
    return c


def forced_long_ind(n, cross_at):
    """Indicators that produce exactly one long signal at close of bar cross_at."""
    return dict(p=P, close=[100.0] * n, fast=[0.0] * cross_at + [1.0] * (n - cross_at),
                slow=[0.0] * n, trend=[0.0] * n, atr=[1.0] * n)


def test_trailing_stop_ratchets():
    n = W + 10
    c = flat(n)
    c[W + 1] = [c[W + 1][0], 100.0, 104.0, 99.9, 103.5, 1.0]   # runs up: trail -> 104 - 2 = 102
    c[W + 2] = [c[W + 2][0], 103.5, 103.6, 101.0, 101.5, 1.0]  # dips to 101 -> hits trailed 102
    p = {**P, "rr": 0, "trail_atr": 2.0}
    r = backtest.run(c, forced_long_ind(n, W), p, 0, n)
    t = r["trade_list"][0]
    assert t["reason"] == "sl" and t["exit_time"] == c[W + 2][0]
    assert math.isclose(t["exit"], 102.0 * (1 - config.SLIPPAGE))
    assert t["pnl"] > 0  # trailed stop locked in profit


def test_backtest_carry_equals_one_continuous_run_and_marks_drawdown():
    c = random_walk(3000, seed=4)
    p = {**P, "entry": "breakout", "adx_min": 0, "trend": 0}
    ind = strategy.indicators(c, p)
    whole = backtest.run(c, ind, p, 0, 3000)
    assert whole["trades"] >= 3
    a = backtest.run(c, ind, p, 0, 1500, carry=True)
    b = backtest.run(c, ind, p, 1500, 3000, a["equity"], a["peak"], state=a["state"])
    assert [t["entry_time"] for t in a["trade_list"] + b["trade_list"]] == [t["entry_time"] for t in whole["trade_list"]]
    assert math.isclose(b["equity"], whole["equity"])
    # marked drawdown: a long that dips 5% and recovers shows a drawdown even though it closes flat
    n = W + 10
    fc = flat(n)
    fc[W + 2] = [fc[W + 2][0], 100.0, 100.5, 94.0, 95.0, 1.0]
    r = backtest.run(fc, forced_long_ind(n, W), {**P, "rr": 0, "sl_atr": 50}, 0, n)
    assert r["max_dd"] > 0.001


def test_size():
    # 1% of 1000 = 10 risk / 2 stop = 5 qty, leverage cap 1000*3/100 = 30 -> 5
    assert risk.size(1000, 100, 98) == 5.0
    # leverage cap binds: risk says 100, cap says 30
    assert risk.size(1000, 100, 99.9) == 30.0
    # floors, never rounds up
    q = risk.size(1000, 85000, 84700)  # 10/300 = 0.0333 -> 0.033
    assert q == 0.033 and q * 300 <= 10
    assert risk.size(10, 85000, 84700) == 0.0  # below MIN_QTY
    assert risk.size(1000, 100, 100) == 0.0    # zero stop distance
    assert risk.size(1000, 100, 98, 0.5) == 2.5


def test_risk_mult_and_day_cap():
    assert risk.risk_mult(1000, 1000) == 1.0
    assert risk.risk_mult(890, 1000) == 0.5
    assert risk.risk_mult(800, 1000) == 0.0
    assert risk.day_blocked(970, 1000) and not risk.day_blocked(971, 1000)


def test_sl_wins_when_both_hit():
    n = W + 10
    c = flat(n)
    c[W + 1] = [c[W + 1][0], 100.0, 110.0, 90.0, 100.0, 1.0]  # touches TP and SL
    r = backtest.run(c, forced_long_ind(n, W), P, 0, n)
    assert r["trades"] == 1
    t = r["trade_list"][0]
    assert t["reason"] == "sl" and t["side"] == 1
    entry = 100 * (1 + config.SLIPPAGE)
    assert math.isclose(t["entry"], entry)
    assert math.isclose(t["exit"], (entry - 1.5) * (1 - config.SLIPPAGE))
    # loss ~= risked amount + costs, never more than ~1.2x the 1% target
    assert -0.012 * config.START_EQUITY < t["pnl"] < -0.009 * config.START_EQUITY
    assert math.isclose(r["equity"], config.START_EQUITY + t["pnl"])


def test_tp_and_fees():
    n = W + 10
    c = flat(n)
    c[W + 2] = [c[W + 2][0], 100.0, 104.0, 99.9, 103.0, 1.0]  # only TP (entry+3) hit
    r = backtest.run(c, forced_long_ind(n, W), P, 0, n)
    t = r["trade_list"][0]
    assert t["reason"] == "tp"
    gross = (t["exit"] - t["entry"]) * t["qty"]
    expected_fees = (t["entry"] + t["exit"]) * t["qty"] * config.TAKER_FEE * (1 + config.GST)
    expected_fees += t["entry"] * t["qty"] * config.FUNDING_PER_DAY * (t["exit_time"] - t["entry_time"]) / 86400
    assert t["exit_time"] > t["entry_time"]  # funding term is actually exercised
    assert math.isclose(t["fees"], expected_fees)
    assert math.isclose(t["pnl"], gross - expected_fees)


def test_entry_is_next_bar_open():
    n = W + 10
    c = flat(n)
    c[W + 1][1] = 101.0  # next open differs from signal bar close
    r = backtest.run(c, forced_long_ind(n, W), P, 0, n)
    assert math.isclose(r["trade_list"][0]["entry"], 101.0 * (1 + config.SLIPPAGE))
    assert r["trade_list"][0]["entry_time"] == c[W + 1][0]


def test_paper_matches_backtest():
    import paper
    c = random_walk(4000, seed=7)
    for p in ({**P, "entry": "breakout", "rr": 0, "trail_atr": 2.0, "sl_atr": 2.0, "don_n": 20},
              {**P, "entry": "breakout", "rr": 0, "trail_atr": 3.0, "sl_atr": 3.0, "don_n": 55, "trend": 0}):
        bt = [t for t in backtest.run(c, strategy.indicators(c, p), p, 0, len(c))["trade_list"] if t["reason"] != "end"]
        pp = paper.replay(c, p)["closed"]
        assert len(bt) > 10 and len(bt) == len(pp), (len(bt), len(pp))
        for a, b in zip(bt, pp):
            for k in ("side", "qty", "entry_time", "exit_time", "reason"):
                assert a[k] == b[k], (k, a, b)
            for k in ("entry", "exit", "pnl", "fees"):
                assert math.isclose(a[k], b[k], rel_tol=1e-9), (k, a, b)


def test_paper_never_places_orders():
    import inspect
    import paper
    src = inspect.getsource(paper)
    assert "futures/order" not in src and "Request(" not in src and "urlopen" not in src


def test_portfolio_paper_matches_simulate():
    import inspect
    import paper_portfolio as pp
    import portfolio
    uni = {f"C{k}": [[i * 86400, x[1], x[2], x[3], x[4], 1e6 * (k + 1)] for i, x in enumerate(random_walk(800, seed=k))]
           for k in range(6)}
    days = sorted({x[0] for cs in uni.values() for x in cs})
    for name, (_, _, params) in pp.STRATEGIES.items():
        params = dict(params)
        top_n, band = params.pop("top_n"), params.pop("band", 0.0)
        ref = portfolio.simulate(uni, portfolio.zarattini, top_n, band=band, **params)
        ctx = portfolio.prepare(uni, portfolio.zarattini, **params)
        st = pp.new_state()
        pp.advance(st, ctx, days[:-1], top_n=top_n, band=band)   # decision on days[-2] realizes to days[-1] open
        assert math.isclose(st["equity"], ref["curve"][-1][1], rel_tol=1e-12), name
        assert ref["exposure_days"] > 50, name
    src = inspect.getsource(pp)
    assert "futures/order" not in src and "Request(" not in src and "urlopen" not in src


def test_s2_model_learns_and_never_peeks():
    import numpy as np
    import s2
    rng = np.random.default_rng(0)
    X = rng.normal(size=(2000, 3))
    y = (X[:, 0] + 0.2 * rng.normal(size=2000) > 0).astype(float)
    p = s2.Model().fit(X, y).predict(X)
    assert ((p > 0.5) == (y > 0.5)).mean() > 0.9           # learns an obvious pattern
    # walk-forward: every scored setup's model was trained only on outcomes known before its month began
    rows, t = [], 0
    for k in range(1200):
        t += 12 * 3600
        f = {name: float(rng.normal()) for name in s2.FEATURES}
        rows.append(dict(t=t, exit_t=t + 48 * 3600, y=int(f["brk_atr"] > 0), f=f, R=0.0))
    seen = []
    orig = s2.Model.fit

    def spy(self, X, y, **kw):
        seen.append(len(y))
        return orig(self, X, y, **kw)
    s2.Model.fit = spy
    try:
        s2.score_walk_forward(rows, min_train_days=60)
    finally:
        s2.Model.fit = orig
    scored = [r for r in rows if r["p"] is not None]
    assert scored and seen
    for r in scored:
        month_start = min(x["t"] for x in rows if (x["t"] + config.IST_OFFSET) // (30 * 86400)
                          == (r["t"] + config.IST_OFFSET) // (30 * 86400))
        assert sum(1 for x in rows if x["exit_t"] < month_start) in seen


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} passed")
