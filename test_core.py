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
    atrs = {c: 0.02 for c in s1.BASKET}
    targets = {"XRP": 0.2, "ADA": 0.2}
    o = {x["coin"]: x for x in lt.build_orders(targets, {}, set(), prices, atrs, specs, 20000, {}, None, 102)}
    # Quantity comes from the fixed rupee stop budget, independent of equity; leverage only caps margin.
    assert o["XRP"]["action"] == "OPEN" and o["XRP"]["planned_risk_inr"] <= 250
    assert o["XRP"]["leverage"] == 5 and abs(o["XRP"]["est_stop"] - 1.47) < 1e-9
    # High-but-allowed volatility widens the stop, reduces leverage and shrinks quantity instead of using Rs150.
    o = {x["coin"]: x for x in lt.build_orders(targets, {}, set(), prices, {c: 0.1 for c in s1.BASKET}, specs,
                                               5000, {}, None, 102)}
    assert o["XRP"]["action"] == "OPEN" and o["XRP"]["leverage"] == 2
    assert o["XRP"]["planned_risk_inr"] <= 250 and o["XRP"]["notional_inr"] < 2000
    # stale/gappy data: no decision on that coin, not even a CLOSE of a held position
    D = 86400
    good = [[i * D, 1, 1, 1, 1, 0] for i in range(500)]
    assert lt.bad_data({"XRP": good, "ADA": good[:-1], "DOGE": good[:300] + good[301:]}, 499 * D) == {"ADA", "DOGE"}
    o = {x["coin"]: x for x in lt.build_orders(targets, {"XRP": "p1"}, set(), prices, atrs, specs, 5000, {}, None,
                                               102, bad={"XRP"})}
    assert o["XRP"]["action"] == "SKIP" and "stale" in o["XRP"]["reason"]
    # entries blocked (cap/guard/STOP): no entries, exits of BOT-OWNED positions still planned
    o = {x["coin"]: x for x in lt.build_orders(targets, {"LINK": "pos-1"}, set(), prices, atrs, specs, 5000, {},
                                               "daily loss cap hit", 102)}
    assert o["XRP"]["action"] == "SKIP" and o["LINK"] == dict(
        action="CLOSE", coin="LINK", position_id="pos-1",
        reason="trend exit / side flip; opposite entry waits for a later set")
    # manual position on a coin: nothing planned on it at all; stopped-out coin not re-bought; owned + up = HOLD
    o = {x["coin"]: x for x in lt.build_orders(targets, {"ADA": "p2"}, {"XRP"}, prices, atrs, specs, 5000,
                                               {"XRP": False}, None, 102)}
    assert o["XRP"]["action"] == "SKIP" and "manual" in o["XRP"]["reason"] and o["ADA"]["action"] == "HOLD"
    o = {x["coin"]: x for x in lt.build_orders(targets, {}, set(), prices, atrs, specs, 5000, {"XRP": False},
                                               None, 102)}
    assert o["XRP"]["action"] == "SKIP" and "stopped out" in o["XRP"]["reason"]
    # A bot-owned coin that is no longer entry-eligible remains in the management universe. It may be closed,
    # but the same coin cannot be used for a new entry until a later cycle selects it again.
    old_targets = {"XRP": 0.2, "OLD": -0.2}
    old_specs = {"XRP": dict(spec), "OLD": dict(spec)}
    old_prices, old_atrs = {"XRP": 1.5, "OLD": 1.5}, {"XRP": 0.02, "OLD": 0.02}
    o = {x["coin"]: x for x in lt.build_orders(
        old_targets, {"OLD": {"id": "old-pos", "side": "LONG"}}, set(), old_prices, old_atrs,
        old_specs, 5000, {}, None, 102, basket=("XRP", "OLD"), entry_basket=("XRP",))}
    assert o["OLD"]["action"] == "CLOSE" and o["OLD"]["position_id"] == "old-pos"
    o = {x["coin"]: x for x in lt.build_orders(
        old_targets, {}, set(), old_prices, old_atrs, old_specs, 5000, {}, None, 102,
        basket=("XRP", "OLD"), entry_basket=("XRP",))}
    assert o["OLD"]["action"] == "SKIP" and "not eligible" in o["OLD"]["reason"]


def test_btc_mood_fails_closed_on_missing_data():
    import s1
    D = 86400
    up = [[i * D, 0, 0, 0, 100 + i, 0] for i in range(300)]         # steady uptrend: mood GOOD
    assert s1.btc_mood(up, 299 * D) is True
    down = up[:250] + [[i * D, 0, 0, 0, 50.0, 0] for i in range(250, 300)]
    assert s1.btc_mood(down, 299 * D) is False
    assert s1.btc_mood(up, 400 * D) is None                           # day not in data
    assert s1.btc_mood(up[:100], 99 * D) is None                      # not enough history
    gap = up[:280] + up[281:]                                         # a missing day inside the 200-day window
    assert s1.btc_mood(gap, 299 * D) is None
    assert s1.entries_only_for_held({"XRP": 0.1, "ADA": 0.1}, {"ADA": {}}) == {"ADA": 0.1}


def test_live_orders_identical_at_any_equity_above_the_cap():
    """Rounding and sizing use the same capped equity: a profitable bot must not shrink orders below minimum."""
    import live_trader as lt
    import s1
    specs = {c: dict(step=0.1, min_qty=0.1, min_notional=5.0) for c in s1.BASKET}
    closes = {c: {0: 1.5} for c in s1.BASKET}
    ctx = dict(sig={c: {0: s} for c, s in zip(s1.BASKET, (0.2, 0.3, 0.4, 0.6, 0.8, 1.0))})
    books = []
    for eq in (5000, 7500, 10000, 20000):
        t = s1.targets(ctx, closes, 0, s1.sizing_equity(eq), specs)
        books.append(lt.build_orders(t, {}, set(), {c: 1.5 for c in s1.BASKET}, {c: 0.02 for c in s1.BASKET},
                                     specs, eq, {}, None, 102))
    assert all(b == books[0] for b in books)
    opened = [o for o in books[0] if o["action"] == "OPEN"]
    assert len(opened) == 2 and sum(o["planned_risk_inr"] for o in opened) <= 500
    assert sum(o["notional_inr"] / o["leverage"] for o in opened) <= s1.CAPITAL_CAP_INR + 0.01


def test_paper_mood_gate_two_close_reentry():
    import paper_s1
    D = 86400
    closes = [100.0 + i for i in range(250)] + [50.0] + [400.0, 400.0]      # up, one crash day, recovery
    btc = [[i * D, 0, 0, 0, x, 0] for i, x in enumerate(closes)]
    st = dict(S=dict(pos={}))
    seq = [paper_s1.mood_gate(st, btc, d * D, "two") for d in (249, 250, 251, 252)]
    assert seq == [True, False, False, True]                               # 2nd close above re-enables
    assert [paper_s1.mood_gate({}, btc, d * D, True) for d in (250, 251)] == [False, True]
    assert paper_s1.gated(dict(S=dict(pos={"ADA": {}})), {"XRP": 0.1, "ADA": 0.1}, btc, 999 * D, True) == {"ADA": 0.1}


def test_paper_promotion_needs_two_monthly_reviews():
    import tempfile
    import paper_s1
    assert paper_s1.PROMOTE_T > 2.0                                    # Bonferroni-corrected, stricter than 2
    assert paper_s1.weekly_t([0.001] * 14) == 0.0                      # no variation: no evidence
    path = os.path.join(tempfile.mkdtemp(), "promo.json")
    rows = [dict(name="S1-x", promote=True)]
    oct1, oct2, nov1 = 1790812800, 1790899200, 1793491200             # 2026-10-01, 2026-10-02, 2026-11-01 UTC
    assert paper_s1.monthly_review(oct2, rows, path) == []           # not a review day
    assert paper_s1.monthly_review(oct1, rows, path) == []           # first pass: quarantine
    assert paper_s1.monthly_review(nov1, rows, path) == ["S1-x"]     # second review in a row: reported


def test_live_execute_requires_yes():
    import tempfile
    import execution as ex
    import live_trader as lt
    tmp = tempfile.mkdtemp()
    ex.STOP_PATH = os.path.join(tmp, "STOP")
    con = ex.db(os.path.join(tmp, "e.db"))
    ex.record_plan(con, "x", [dict(coin="XRP", action="OPEN", planned_price=1.5, notional_inr=1000, atr=0.1)], {})
    ran = []
    orig, env = ex.execute, os.environ.get("LIVE_TRADING_ENABLED")
    ex.execute = lambda *a, **k: ran.append(a) or ("COMPLETE", [])
    os.environ["LIVE_TRADING_ENABLED"] = "true"
    try:
        for answer in ("yes", "y", "YES please", ""):
            try:
                lt.execute(client=object(), con=con, confirm=lambda *_: answer)
                raise AssertionError("execute must abort without exact YES")
            except SystemExit:
                pass
        assert not ran
        open(ex.STOP_PATH, "w").close()          # kill switch blocks before even asking
        try:
            lt.execute(client=object(), con=con, confirm=lambda *_: "YES")
            raise AssertionError("STOP must block")
        except SystemExit as e:
            assert "STOP" in str(e) and not ran
        os.remove(ex.STOP_PATH)
        os.environ["LIVE_TRADING_ENABLED"] = "false"   # live flag off (the default) blocks too
        try:
            lt.execute(client=object(), con=con, confirm=lambda *_: "YES")
            raise AssertionError("LIVE_TRADING_ENABLED=false must block")
        except SystemExit as e:
            assert "LIVE_TRADING_ENABLED" in str(e) and not ran
    finally:
        ex.execute = orig
        if env is None:
            os.environ.pop("LIVE_TRADING_ENABLED", None)
        else:
            os.environ["LIVE_TRADING_ENABLED"] = env


def test_watcher_has_no_direct_exchange_writes():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher.py"), encoding="utf-8").read()
    for banned in ("place_market_long", "close_position", "set_stoploss", "set_leverage", ".post(",
                   "urllib.request", "claim("):
        assert banned not in src, banned
    assert "auto_execute" in src and "ex.execute(" in src  # only the durable executor may place autonomous sets


def test_watcher_plan_failure_retry_and_repeats_within_cycle():
    import watcher
    st, calls = {}, []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("mudrex down")
        return dict(plan_id=None, orders=[], blocked=None, live_enabled=False)
    import tempfile
    orig, orig_log = watcher.notify, watcher.LOG_PATH
    watcher.notify = lambda *a, **k: True
    watcher.LOG_PATH = os.path.join(tempfile.mkdtemp(), "watcher.log")     # never write the real log
    try:
        clock = 1_000_000.0
        assert watcher.maybe_plan(st, flaky, now_hm="06:00", today="2026-09-28", now=clock) is False
        assert "plan_day" not in st and st["plan_fails"] == 1        # failure NOT recorded as done
        assert watcher.maybe_plan(st, flaky, now_hm="06:00", today="2026-09-28", now=clock) is False
        st["plan_retry_at"] = 0
        assert watcher.maybe_plan(st, flaky, now_hm="06:00", today="2026-09-28", now=clock) is True
        assert st["plan_day"] == "2026-09-28" and len(calls) == 2
        assert watcher.maybe_plan(st, flaky, now_hm="06:01", today="2026-09-28", now=clock + 60) is False
        assert watcher.maybe_plan(st, flaky, now_hm="06:15", today="2026-09-28",
                                  now=clock + watcher.PLAN_INTERVAL_SEC) is True
        assert len(calls) == 3
        import live_trader
        strategy, live_trader.STRATEGY = live_trader.STRATEGY, "S1"     # S1 waits for the 05:30 IST daily close
        try:
            assert watcher.maybe_plan({}, flaky, now_hm="05:00", today="2026-09-29", now=clock) is False
        finally:
            live_trader.STRATEGY = strategy
    finally:
        watcher.notify, watcher.LOG_PATH = orig, orig_log


def test_watcher_undelivered_plan_is_resent_not_regenerated():
    import tempfile
    import watcher
    st, made, sends, executions = {}, [], [], []

    def make():
        made.append(1)
        return dict(plan_id=len(made), created_at=time.time(), live_enabled=True, blocked=None, completed_sets=0,
                    orders=[dict(action="OPEN", coin="XRP", side="LONG", leverage=2, notional_inr=1000,
                                 planned_price=1.5, est_stop=1.2, est_target=1.95, planned_risk_inr=210)])
    orig, orig_log = watcher.notify, watcher.LOG_PATH
    watcher.LOG_PATH = os.path.join(tempfile.mkdtemp(), "watcher.log")
    watcher.notify = lambda msg, buttons=None: sends.append(buttons) or len(sends) > 1   # 1st send fails
    try:
        auto = lambda p: executions.append(p["plan_id"]) or ("COMPLETE", ["verified"])  # noqa: E731
        assert watcher.maybe_plan(st, make, now_hm="06:00", today="2026-09-28", auto_execute=auto) is False
        assert "plan_day" not in st                                  # not marked delivered
        st["plan_retry_at"] = 0
        assert watcher.maybe_plan(st, make, now_hm="06:00", today="2026-09-28", auto_execute=auto) is True
        assert len(made) == 1                                        # same plan resent, no duplicate plan
        assert sends[0] is sends[1] is None and executions == [1]    # execution starts only after delivery succeeds
    finally:
        watcher.notify, watcher.LOG_PATH = orig, orig_log


def test_watcher_never_auto_runs_an_approval_set_and_does_not_repeat_idle_messages():
    import tempfile
    import watcher
    sends, runs = [], []
    orig, orig_log = watcher.notify, watcher.LOG_PATH
    watcher.LOG_PATH = os.path.join(tempfile.mkdtemp(), "watcher.log")
    watcher.notify = lambda msg, buttons=None: sends.append(buttons) or True
    auto = lambda p: runs.append(p["plan_id"]) or ("COMPLETE", [])  # noqa: E731
    opens = [dict(action="OPEN", coin="XRP", side="LONG", leverage=2, notional_inr=1000, planned_price=1.5,
                  est_stop=1.2, est_target=1.95, planned_risk_inr=210)]
    try:
        extra = lambda: dict(plan_id=9, created_at=time.time(), live_enabled=True, blocked=None,  # noqa: E731
                             completed_sets=3, needs_approval=True, orders=opens)
        assert watcher.maybe_plan({}, extra, now_hm="06:00", today="2026-09-28", now=0, auto_execute=auto)
        assert runs == [] and sends[-1]                              # 4th set: buttons sent, never auto-run
        st, n = {}, len(sends)
        idle = lambda: dict(plan_id=None, created_at=time.time(), live_enabled=True, blocked="STOP file present",  # noqa: E731
                            completed_sets=0, orders=[])
        for k in range(4):                                           # four 15-minute re-plans, nothing changes
            assert watcher.maybe_plan(st, idle, now_hm="06:00", today="2026-09-28", now=k * 901, auto_execute=auto)
        assert len(sends) == n + 1                                   # one message, not four
    finally:
        watcher.notify, watcher.LOG_PATH = orig, orig_log


def test_watcher_alerts_when_approver_heartbeat_missing_or_stale():
    import tempfile
    import telegram_bot as tg
    import watcher
    tmp = tempfile.mkdtemp()
    orig = (watcher.notify, watcher.APPROVER_HEARTBEAT, tg.enabled, watcher.STARTED)
    watcher.notify = lambda *a, **k: True
    watcher.APPROVER_HEARTBEAT = os.path.join(tmp, "hb.json")
    tg.enabled = lambda: True
    try:
        st = {}
        watcher.STARTED = time.time()
        assert watcher.check_approver(st) is None                    # within startup grace
        watcher.STARTED = time.time() - 3600
        assert "not running" in watcher.check_approver(st)           # missing after grace
        assert watcher.check_approver(st) is None                    # alerted once
        with open(watcher.APPROVER_HEARTBEAT, "w") as f:
            f.write('{"at": %f}' % (time.time() - 3600))
        assert "stopped" in watcher.check_approver({})               # stale heartbeat
        tg.enabled = lambda: False
        assert watcher.check_approver({}) is None                    # Telegram off: approver not expected
    finally:
        watcher.notify, watcher.APPROVER_HEARTBEAT, tg.enabled, watcher.STARTED = orig


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
    import execution as ex
    import telegram_bot as tg
    tmp = tempfile.mkdtemp()
    ex.STOP_PATH = os.path.join(tmp, "STOP")
    saved = {k: os.environ.get(k) for k in ("TELEGRAM_CHAT_ID", "TELEGRAM_USER_ID")}
    os.environ.update(TELEGRAM_CHAT_ID="111", TELEGRAM_USER_ID="222")
    ran, sent = [], []
    orig, orig_db, orig_log = (tg.answer, tg.edit, tg.send), ex.DB_PATH, approver.LOG_PATH
    ex.DB_PATH = os.path.join(tmp, "exec.db")                       # never inspect or mutate the real journal
    approver.LOG_PATH = os.path.join(tmp, "approver.log")                   # never write the real log
    tg.answer = lambda *a: sent.append(a)
    tg.edit = lambda *a: sent.append(a)
    tg.send = lambda *a, **k: sent.append(a)
    run = lambda pid, who: ran.append((pid, who)) or ("COMPLETE", [])   # noqa: E731
    replans = []
    replan = lambda: replans.append(1)                                  # noqa: E731

    def tap(chat, user, data="approve:7", kind="private", is_bot=False, runner=None):
        return approver.handle(dict(callback_query=dict(
            id="q", data=data, from_=None, **{"from": dict(id=user, is_bot=is_bot)},
            message=dict(chat=dict(id=chat, type=kind), message_id=1, text="plan"))), runner or run, replan)

    def say(text, chat=111, user=222):
        return approver.handle(dict(message=dict(text=text, chat=dict(id=chat, type="private"),
                                                 **{"from": dict(id=user)})), run, replan)
    try:
        assert tap(111, 999) == "ignored" and not ran              # right chat, wrong person
        assert tap(999, 222) == "ignored" and not ran              # right person, wrong chat
        assert tap(111, 222, kind="group") == "ignored" and not ran   # not a private chat
        assert tap(111, 222, is_bot=True) == "ignored" and not ran
        assert tap(111, 222, data="approve:x") == "ignored" and not ran
        tap(111, 222)                                              # owner: handed to execution (which locks/checks)
        assert ran == [(7, "telegram user 222")]
        say("/stop")
        assert os.path.exists(ex.STOP_PATH)                        # remote kill switch works
        say("/resume")
        assert os.path.exists(ex.STOP_PATH)                        # remote resume refused (local only)
        say("/stop", user=999)
        os.remove(ex.STOP_PATH)
        say("/stop", user=999)
        assert not os.path.exists(ex.STOP_PATH)                    # strangers cannot even stop it
        expired = lambda pid, who: ("REFUSED", ["EXPIRED: plans with buys are valid 15 minutes"])   # noqa: E731
        assert tap(111, 222, runner=expired) == "replanned" and replans == [1]   # stale tap -> fresh plan, no trade
        say("/plan")
        say("/plan", user=999)
        assert replans == [1, 1]                                   # /plan works for the owner only
        cap = ex.record_plan(ex.db(), "d", [dict(coin="XRP", action="CLOSE", position_id="p")], {"reason": "cap"})
        assert tap(111, 222, data=f"approve:{cap}", runner=expired) == "cap expired"
        assert replans == [1, 1]                                   # an expired Close-all never becomes a buy plan
    finally:
        ex.DB_PATH, approver.LOG_PATH = orig_db, orig_log
        tg.answer, tg.edit, tg.send = orig
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_closed_only_and_gaps():
    c = flat(3)
    assert len(data.closed_only(c, now=2 * 900 + 899)) == 2
    assert len(data.closed_only(c, now=3 * 900)) == 3
    assert data.gaps([[0], [900], [2700]]) == 1


def test_dashboard_serves_ui_and_never_bot_files():
    import http.client
    import tempfile
    import threading
    from http.server import ThreadingHTTPServer
    import dashboard
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "assets"))
    with open(os.path.join(tmp, "_shell.html"), "w") as f:
        f.write("<html>shell</html>")
    orig, dashboard.UI_DIST = dashboard.UI_DIST, tmp
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.read()
        assert get("/") == (200, b"<html>shell</html>")
        for bad in ("/../execution.db", "/..%2f.env", "/assets/../../dashboard.py", "/%2e%2e/.env"):
            assert get(bad)[0] == 404, bad
    finally:
        srv.shutdown()
        dashboard.UI_DIST = orig


def test_telegram_render_escapes_and_aligns():
    import telegram_bot as tg
    h = tg.render("Watcher cannot reach Mudrex <urlopen error> & more\n```\nEntry   1.25\n```\nend")
    assert h.startswith("<b>⚠️ Watcher cannot reach Mudrex &lt;urlopen error&gt; &amp; more</b>")
    assert "<pre>Entry   1.25</pre>" in h and "<urlopen" not in h
    assert tg.render("👀 S4 DRY RUN").startswith("<b>👀 S4 DRY RUN</b>")          # own icon kept, none added
    assert tg.render("```\nunclosed").endswith("</pre>")                         # never leaves a tag open
    assert tg.render("title\n*🟢 BUY LINK*\nplain").split("\n")[1] == "<b>🟢 BUY LINK</b>"     # *line* = bold


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} passed")
