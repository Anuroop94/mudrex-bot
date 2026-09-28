"""Offline tests for strategy S4 (s4.py) and its live planner (live_trader.s4_orders). No network.
Run: MUDREX_TEST_MODE=1 python test_s4.py
"""
import os

os.environ["MUDREX_TEST_MODE"] = "1"

import live_trader as lt      # noqa: E402
import s4                     # noqa: E402
import trade_policy           # noqa: E402

HOUR, DAY = 3600, 86400
NOW = 1_800_000_000 // HOUR * HOUR + 600               # 10 minutes into an hour
LAST = NOW // HOUR * HOUR - HOUR                       # last closed hour
SPECS = {c: dict(step=0.1, min_qty=0.1, min_notional=5.0, max_leverage=10.0) for c in ("XRP", "ADA")}


def hourly(start_px, drift, n=200):
    """n closed hourly candles ending at LAST."""
    out, px = [], start_px
    for k in range(n):
        t = LAST - (n - 1 - k) * HOUR
        o, px = px, px * (1 + drift)
        out.append([t, o, max(o, px) * 1.002, min(o, px) * 0.998, px, 1.0])
    return out


def btc(up, n=260):
    day0 = (NOW // DAY) * DAY - DAY - (n - 1) * DAY
    return [[day0 + k * DAY, 0, 0, 0, 100 * (1.002 if up else 0.998) ** k, 0] for k in range(n)]


def D(coins, up=True):
    day = (NOW // DAY) * DAY - DAY
    d = dict(coins=[], f={}, trend={}, btc=btc(up))
    for c, drift in coins.items():
        d["f"][c] = s4.coin_features(hourly(1.0 if c == "XRP" else 0.3, drift))
        d["trend"][c] = {day: 0.5 if drift > 0 else -0.5}
        d["coins"].append(c)
    return d


def orders(**kw):
    args = dict(owned={}, opened_at={}, D=D({"XRP": 0.002, "ADA": 0.001}), datr={"XRP": 0.05, "ADA": 0.015},
                specs=SPECS, now=NOW, blocked=None, rate=100.0, bot_eq=5000.0)
    args.update(kw)
    return lt.s4_orders(**args)


def test_opens_one_long_set_with_fixed_bracket_and_risk():
    o = [x for x in orders() if x["action"] == "OPEN"]
    assert len(o) == 1 and o[0]["coin"] == "XRP" and o[0]["side"] == "LONG"      # strongest 24h mover
    x = o[0]
    assert x["stop_loss"] < x["planned_price"] < x["take_profit"]
    assert abs((x["take_profit"] - x["planned_price"]) / (x["planned_price"] - x["stop_loss"]) - 2.0) < 1e-9
    risk = x["qty"] * s4.SL_DATR * 0.05 * 100.0
    assert risk <= s4.SET_RISK_INR + 1e-6 and x["planned_risk_inr"] <= s4.SET_RISK_INR + 10 + 1e-6
    assert 1 <= x["leverage"] <= 10 and x["notional_inr"] <= s4.MAX_NOTIONAL_LEV * 5000 + 1e-6
    assert abs(x["qty"] * 10 - round(x["qty"] * 10)) < 1e-9                      # on the 0.1 quantity step
    trade_policy.validate_bracket("LONG", x["planned_price"], x["stop_loss"], x["take_profit"])


def test_shorts_only_when_btc_mood_is_bad():
    o = [x for x in orders(D=D({"XRP": -0.002, "ADA": 0.001}, up=False)) if x["action"] == "OPEN"]
    assert len(o) == 1 and o[0]["side"] == "SHORT" and o[0]["coin"] == "XRP"
    assert o[0]["take_profit"] < o[0]["planned_price"] < o[0]["stop_loss"]
    # a falling coin while BTC is above its 200-day average: no short is allowed
    o = [x for x in orders(D=D({"XRP": -0.002}, up=True)) if x["action"] == "OPEN"]
    assert o == []


def test_sets_are_sequential_and_time_exit_closes():
    held = {"ADA": {"id": "p1", "side": "LONG"}}
    young = orders(owned=held, opened_at={"p1": NOW - 10 * HOUR})
    assert [x["action"] for x in young] == ["HOLD", "SKIP"] and "next set" in young[-1]["reason"]
    old = orders(owned=held, opened_at={"p1": NOW - s4.HOLD_H * HOUR})
    assert old[0]["action"] == "CLOSE" and old[0]["position_id"] == "p1"
    assert not any(x["action"] == "OPEN" for x in old)                          # no new set until it has closed


def test_legacy_position_is_never_time_exited_and_does_not_block_s4():
    legacy = {"XRP": {"id": "old", "side": "LONG"}}
    out = orders(owned=legacy, opened_at={"old": NOW - 30 * DAY}, s4_ids=set())
    assert out[0]["action"] == "HOLD" and "another strategy" in out[0]["reason"]
    assert not any(x["action"] == "CLOSE" for x in out)
    new = [x for x in out if x["action"] == "OPEN"]
    assert new and new[0]["coin"] != "XRP" and new[0]["strategy"] == "S4"      # never the held coin (one-way)


def test_s4_position_with_unknown_open_time_is_closed():
    out = orders(owned={"ADA": {"id": "p9", "side": "LONG"}}, opened_at={}, s4_ids={"p9"})
    assert out[0]["action"] == "CLOSE" and "unknown open time" in out[0]["reason"]


def test_blocked_or_no_slots_or_no_budget_never_opens():
    assert not any(x["action"] == "OPEN" for x in orders(blocked="STOP file present"))
    assert not any(x["action"] == "OPEN" for x in orders(available_slots=0))
    # Rs500 day budget already used by losses: nothing left to risk
    assert not any(x["action"] == "OPEN" for x in orders(realized_pnls=(-495.0,)))
    small = [x for x in orders(realized_pnls=(-400.0,)) if x["action"] == "OPEN"]
    assert small and small[0]["planned_risk_inr"] <= 500 - 400 + 1e-6              # risk shrinks to what is left


def test_stale_hour_means_no_decision():
    d = D({"XRP": 0.002})
    d["f"]["XRP"]["idx"].pop(LAST)                                              # the last closed hour is missing
    assert not any(x["action"] == "OPEN" for x in orders(D=d))


def test_watcher_auto_runs_close_only_plans_only_after_certification():
    import tempfile
    import watcher
    runs, sends = [], []
    orig = (watcher.notify, watcher.LOG_PATH, trade_policy.AUTONOMOUS_HEDGE_READY)
    watcher.LOG_PATH = os.path.join(tempfile.mkdtemp(), "watcher.log")
    watcher.notify = lambda msg, buttons=None: sends.append(buttons) or True
    plan = lambda: dict(plan_id=5, created_at=NOW, live_enabled=True, blocked="daily loss cap hit",  # noqa: E731
                        completed_sets=1, orders=[dict(action="CLOSE", coin="ADA", position_id="p1")])
    auto = lambda p: runs.append(p["plan_id"]) or ("COMPLETE", [])  # noqa: E731
    try:
        trade_policy.AUTONOMOUS_HEDGE_READY = False
        watcher.maybe_plan({}, plan, now_hm="06:00", today="2026-09-28", now=0, auto_execute=auto)
        assert runs == []                                    # not certified: closes wait for a human tap
        trade_policy.AUTONOMOUS_HEDGE_READY = True
        watcher.maybe_plan({}, plan, now_hm="06:00", today="2026-09-28", now=0, auto_execute=auto)
        assert runs == [5]                                   # certified: a risk-reducing close runs even when blocked
    finally:
        watcher.notify, watcher.LOG_PATH, trade_policy.AUTONOMOUS_HEDGE_READY = orig


def test_identical_close_plan_is_reused_not_recorded_every_15_minutes():
    import tempfile
    import time as _t
    import execution as ex
    con = ex.db(os.path.join(tempfile.mkdtemp(), "exec.db"))
    close = [dict(action="CLOSE", coin="ADA", position_id="p1", reason="S4 maximum hold of 72h reached")]
    pid = ex.record_plan(con, "d", close, {})
    now = int(_t.time())
    assert lt.reuse_close_plan(con, close, now) == pid                 # same close, still pending: same plan
    other = [dict(action="CLOSE", coin="XRP", position_id="p2", reason="x")]
    assert lt.reuse_close_plan(con, other, now) is None                # different close: a new plan
    assert lt.reuse_close_plan(con, close, now + ex.PLAN_MAX_AGE) is None   # nearly expired: a fresh plan
    opens = [dict(action="OPEN", coin="ADA")]
    assert lt.reuse_close_plan(con, opens, now) is None                # entries always use fresh prices


def test_s4_positions_are_identified_from_their_opening_order():
    import tempfile
    import execution as ex
    con = ex.db(os.path.join(tempfile.mkdtemp(), "exec.db"))
    o = [x for x in orders() if x["action"] == "OPEN"][0]
    pid = ex.record_plan(con, "d", [o], {})
    cid = con.execute("SELECT client_order_id FROM orders WHERE plan_id=?", (pid,)).fetchone()[0]
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at) VALUES('s4pos',?,?,1)", (o["coin"], cid))
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at) VALUES('legacy','XRP','s1-7-0-XRP-O',1)")
    assert lt.s4_position_ids(con) == {"s4pos"}


def test_s4_plans_around_the_clock_but_s1_waits_for_the_daily_close():
    import tempfile
    import watcher
    made = []
    orig = (watcher.notify, watcher.LOG_PATH, lt.STRATEGY)
    watcher.LOG_PATH = os.path.join(tempfile.mkdtemp(), "watcher.log")
    watcher.notify = lambda msg, buttons=None: True
    plan = lambda: made.append(1) or dict(plan_id=None, created_at=NOW, live_enabled=True, blocked=None, orders=[])  # noqa: E731
    try:
        lt.STRATEGY = "S4"
        assert watcher.maybe_plan({}, plan, now_hm="02:00", today="2026-09-28", now=0) and made
        lt.STRATEGY, made[:] = "S1", []
        assert not watcher.maybe_plan({}, plan, now_hm="02:00", today="2026-09-28", now=0) and not made
    finally:
        watcher.notify, watcher.LOG_PATH, lt.STRATEGY = orig


def test_research_and_live_share_one_setup_function():
    import s4_research
    assert s4_research.setups is s4.setups


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} passed")
