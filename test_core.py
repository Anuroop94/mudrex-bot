"""Run: python test_core.py  (no network, no deps)"""
import math
import os
import random
import time

import backtest
import config
import data
import risk
import strategy

W = config.WARMUP
P = dict(config.DEFAULT_PARAMS)


def flat(n, px=100.0):
    return [[i * 900, px, px + 0.5, px - 0.5, px, 1.0] for i in range(n)]


def forced_long_ind(n, cross_at):
    """Indicators that produce exactly one long signal at close of bar cross_at."""
    return dict(p=P, close=[100.0] * n, fast=[0.0] * cross_at + [1.0] * (n - cross_at),
                slow=[0.0] * n, trend=[0.0] * n, atr=[1.0] * n)


def test_ema():
    assert strategy.ema([5.0] * 10, 3) == [5.0] * 10
    assert strategy.ema([1.0, 2.0, 3.0], 1) == [1.0, 2.0, 3.0]
    e = strategy.ema([0.0, 10.0], 3)  # k = 0.5
    assert e == [0.0, 5.0]


def random_walk(n, seed=1):
    random.seed(seed)
    c, px = [], 100.0
    for i in range(n):
        o = px
        px *= 1 + random.gauss(0, 0.004)
        c.append([i * 900, o, max(o, px) * 1.001, min(o, px) * 0.999, px, random.uniform(1, 3)])
    return c


def test_no_lookahead():
    c, k = random_walk(1500), 1200
    variants = [P, {**P, "entry": "breakout", "adx_min": 20, "vol_mult": 1.2},
                {**P, "entry": "rsi", "trend": 0, "adx_max": 30}, {**P, "entry": "bb"}]
    for p in variants:
        a, b = strategy.indicators(c[:k], p), strategy.indicators(c, p)
        for key in ("fast", "slow", "hh", "ll", "trend", "atr", "adx", "vol_avg", "rsi", "mid", "sd"):
            if key in a:
                assert math.isclose(a[key][k - 1], b[key][k - 1]), key
        sa = [strategy.signal(a, i) for i in range(k)]
        assert sa == [strategy.signal(b, i) for i in range(k)]
        assert any(sa), f"variant never signals: {p['entry']}"


def test_adx_trend_vs_chop():
    trend = [[i, 100 + i, 101 + i, 99.5 + i, 100.8 + i, 1] for i in range(300)]
    chop = [[i, 100, 101, 99, 100 + (1 if i % 2 else -1), 1] for i in range(300)]
    assert strategy.adx(trend, 14)[-1] > 50
    assert strategy.adx(chop, 14)[-1] < 20


def test_rsi_stdev_and_meanrev_exit():
    assert strategy.rsi([float(i) for i in range(50)], 14)[-1] == 100.0
    assert strategy.rsi([float(50 - i) for i in range(50)], 14)[-1] < 1
    assert strategy.stdev([7.0] * 30, 20)[-1] == 0.0
    assert math.isclose(strategy.stdev([1.0, 3.0] * 10, 20)[-1], 1.0)
    # bb: price dumps below lower band -> long; exits once back at the mean
    closes = [100.0] * 40 + [90.0] + [95.0, 101.0]
    c = [[i, x, x, x, x, 1.0] for i, x in enumerate(closes)]
    ind = strategy.indicators(c, {**P, "entry": "bb", "trend": 0})
    assert strategy.signal(ind, 40) == 1
    assert not strategy.exit_signal(ind, 41, 1) and strategy.exit_signal(ind, 42, 1)


def test_breakout_fresh_only():
    n = 30
    c = flat(n)
    for i in (25, 26, 27):  # three closes above the prior 20-bar high
        c[i] = [c[i][0], 100.0, 102.0 + i, 99.9, 101.0 + i, 1.0]
    p = {**P, "entry": "breakout", "don_n": 20, "trend": 5}
    ind = strategy.indicators(c, p)
    assert [strategy.cross(ind, i) for i in range(n)].count(1) == 1  # only the first break fires
    assert strategy.cross(ind, 25) == 1


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


def test_portfolio_extremes_lookahead_and_costs():
    import portfolio
    random.seed(3)
    xs = [random.random() for _ in range(300)]
    hi, lo = portfolio.prior_extremes(xs, 20)
    for i in range(20, 300):
        assert hi[i] == max(xs[i - 20:i]) and lo[i] == min(xs[i - 20:i])
    assert hi[19] is None
    c = [[i * 86400, x[1], x[2], x[3], x[4], 1.0] for i, x in enumerate(random_walk(900, seed=5))]
    full, part = portfolio.zarattini(c, allow_short=True), portfolio.zarattini(c[:700], allow_short=True)
    assert all(math.isclose(full[t], part[t]) for t in part) and any(part.values())
    # flat price, always fully long 1 coin: equity loses exactly one entry cost + daily funding
    flat_c = [[i * 86400, 100.0, 100.0, 100.0, 100.0, 1e6] for i in range(100)]
    r = portfolio.simulate({"X": flat_c}, lambda cs: {x[0]: 1.0 for x in cs}, top_n=1, min_history=0)
    days = r["exposure_days"]      # first ~19 days hold nothing: volume ranking needs 20 days of history
    assert days == len(r["daily"]) - 19
    expected = (1 - portfolio.cost_per_turnover() - config.FUNDING_PER_DAY) * (1 - config.FUNDING_PER_DAY) ** (days - 1)
    assert math.isclose(r["curve"][-1][1], expected, rel_tol=1e-9)


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


def test_live_build_orders_safety():
    import live_trader as lt
    import s1
    spec = dict(step=0.1, min_qty=0.1, min_notional=5.0, price_step=0.0001, max_leverage=75, price=1.5)
    specs = {c: dict(spec) for c in s1.BASKET}
    prices = {c: 1.5 for c in s1.BASKET}
    atrs = {c: 0.1 for c in s1.BASKET}
    targets = {"XRP": 0.2, "ADA": 0.2}
    o = {x["coin"]: x for x in lt.build_orders(targets, {}, prices, atrs, specs, 20000, 20000, {}, False)}
    # sized on the Rs 5,000 cap even though equity is Rs 20,000: 0.2 * 2x * 5000 = Rs 2000 notional
    assert o["XRP"]["action"] == "OPEN" and abs(o["XRP"]["notional_inr"] - 2000) <= 1.5 * config.INR_PER_USDT * 0.1
    assert o["XRP"]["notional_inr"] <= 2000 and o["XRP"]["margin_inr"] == round(o["XRP"]["notional_inr"] / 2)
    assert 1.2 - 0.0001 <= float(o["XRP"]["stop"]) <= 1.2   # 3x ATR below, rounded down to price step
    # loss stop: no entries, exits still allowed
    held = {"LINK": dict(id="p1", qty=1.0, entry=1.4, sl=1.2)}
    o = {x["coin"]: x for x in lt.build_orders(targets, held, prices, atrs, specs, 5000, 5000, {}, True)}
    assert o["XRP"]["action"] == "SKIP" and o["LINK"]["action"] == "CLOSE"
    # stopped-out coin is not re-bought; margin budget respected; held + still up = HOLD
    o = {x["coin"]: x for x in lt.build_orders(targets, {"ADA": dict(id="p2", qty=1, entry=1, sl=0.9)}, prices,
                                               atrs, specs, 5000, 500, {"XRP": False}, False)}
    assert o["XRP"]["action"] == "SKIP" and o["ADA"]["action"] == "HOLD"
    o = {x["coin"]: x for x in lt.build_orders(targets, {}, prices, atrs, specs, 5000, 500, {}, False)}
    assert o["XRP"]["action"] == "SKIP" and "margin" in o["XRP"]["reason"]


def test_live_execute_requires_yes():
    import builtins
    import tempfile
    import live_trader as lt
    tmp = tempfile.mkdtemp()
    lt.PLAN_PATH, lt.STATE_PATH, lt.STOP_PATH = (os.path.join(tmp, n) for n in ("plan.json", "state.json", "STOP"))
    lt.write_json(lt.PLAN_PATH, dict(created_at=time.time(), executed=False, strategy="t", decision_day="x",
                                     orders=[dict(action="OPEN", coin="XRP", qty="1", notional_inr=100, stop="1")]))
    called = []
    orig_api, orig_input = lt.api, builtins.input
    lt.api = lambda *a, **k: called.append(a)
    builtins.input = lambda *_: "yes please"
    try:
        lt.execute()
        raise AssertionError("execute must abort without exact YES")
    except SystemExit:
        pass
    finally:
        lt.api, builtins.input = orig_api, orig_input
    assert not called
    open(lt.STOP_PATH, "w").close()      # kill switch blocks even before asking
    try:
        lt.execute()
        raise AssertionError("STOP file must block")
    except SystemExit as e:
        assert "STOP" in str(e)


def test_watcher_is_read_only():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher.py")).read()
    for banned in ("/v2/futures/order", "futures/order?", "/close", "POST", "PATCH", "DELETE", "execute(",
                   "riskorder", "leverage?", "method="):
        assert banned not in src, banned
    assert "urllib.request.Request(API + path, headers=" in src     # the only HTTP call: a plain GET


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


def test_telegram_approval_security():
    import tempfile
    import approver
    import live_trader as lt
    import telegram_bot as tg
    tmp = tempfile.mkdtemp()
    lt.PLAN_PATH, lt.STATE_PATH, lt.STOP_PATH = (os.path.join(tmp, n) for n in ("plan.json", "state.json", "STOP"))
    plan = dict(created_at=int(time.time()), executed=False, strategy="t", decision_day="x",
                orders=[dict(action="OPEN", coin="XRP", qty="1", notional_inr=100, stop="1")])
    lt.write_json(lt.PLAN_PATH, plan)
    placed, sent = [], []
    orig = (lt.place, tg.answer, tg.edit, tg.send)
    lt.place = lambda p, todo, approved_by: placed.append(approved_by) or ["ok"]
    tg.answer = lambda *a: sent.append(a)
    tg.edit = lambda *a: sent.append(a)
    tg.send = lambda *a, **k: sent.append(a)

    def tap(chat, data):
        approver.handle(dict(callback_query=dict(id="q", data=data, message=dict(chat=dict(id=chat), message_id=1,
                                                                                text="plan"))), "111")
    try:
        tap(999, f"approve:{plan['created_at']}")          # stranger: ignored
        assert not placed
        tap(111, "approve:123")                             # wrong plan id: refused
        assert not placed
        open(lt.STOP_PATH, "w").close()
        tap(111, f"approve:{plan['created_at']}")           # kill switch: refused
        assert not placed
        os.remove(lt.STOP_PATH)
        tap(111, f"approve:{plan['created_at']}")           # owner, right plan: placed once
        assert placed == ["telegram chat 111"]
        plan["executed"] = True
        lt.write_json(lt.PLAN_PATH, plan)
        tap(111, f"approve:{plan['created_at']}")           # second tap: already executed
        assert len(placed) == 1
    finally:
        lt.place, tg.answer, tg.edit, tg.send = orig


def test_closed_only_and_gaps():
    c = flat(3)
    assert len(data.closed_only(c, now=2 * 900 + 899)) == 2
    assert len(data.closed_only(c, now=3 * 900)) == 3
    assert data.gaps([[0], [900], [2700]]) == 1


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} passed")
