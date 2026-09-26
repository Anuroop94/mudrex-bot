"""Deterministic execution-safety tests against a local fake Mudrex server. Never contacts the live API.
Run: python test_execution.py
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

os.environ["LIVE_TRADING_ENABLED"] = "true"          # tests exercise the live path against the FAKE server only

import execution as ex                                  # noqa: E402
import fake_mudrex                                      # noqa: E402
import s1                                               # noqa: E402
from mudrex_client import Client                        # noqa: E402

PRICES = {"XRP": 1.5, "ADA": 0.26, "DOGE": 0.1, "LINK": 14.0, "AVAX": 11.0, "TRX": 0.34}
NOSLEEP = lambda s: None                                # noqa: E731


def setup(prices=PRICES):
    tmp = tempfile.mkdtemp()
    ex.STOP_PATH, ex.GUARD_PATH = os.path.join(tmp, "STOP"), os.path.join(tmp, "guard.json")
    fake = fake_mudrex.FakeMudrex(prices)
    client = Client(base=fake.url, secret="test", timeout=0.5, sleep=NOSLEEP)
    con = ex.db(os.path.join(tmp, "exec.db"))
    return tmp, fake, client, con


def plan(con, coins, notional=1000.0, action="OPEN", position_ids=None):
    orders = [dict(coin=c, action=action, planned_price=PRICES[c], notional_inr=notional, atr=PRICES[c] * 0.06,
                   position_id=(position_ids or {}).get(c)) for c in coins]
    return ex.record_plan(con, "2026-09-26", orders, {})


def states(con, pid):
    return {r["coin"]: (r["state"], r["error"]) for r in con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,))}


def test_happy_path_fill_aware_stop():
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP", "ADA"])
    final, _ = ex.execute(con, client, pid, "test", NOSLEEP)
    assert final == "COMPLETE", states(con, pid)
    for pos in fake.positions:
        fill, stop, liq = float(pos["entry_price"]), float(pos["stoploss"]["price"]), float(pos["liquidation_price"])
        assert liq < stop < fill
    assert len(ex.owned_ids(con)) == 2
    fake.stop()


def test_simultaneous_terminal_and_telegram_approval_threads():
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    results, path = [], os.path.join(tmp, "exec.db")
    barrier = threading.Barrier(2)

    def approve(who):
        c = ex.db(path)
        barrier.wait()
        results.append(ex.claim(c, pid, who))
    threads = [threading.Thread(target=approve, args=(w,)) for w in ("terminal", "telegram")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r is None for r in results) == [False, True], results
    fake.stop()


def test_duplicate_taps_from_separate_processes():
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    code = ("import sys, execution as ex; c = ex.db(sys.argv[1]); r = ex.claim(c, int(sys.argv[2]), sys.argv[3]); "
            "print('WON' if r is None else 'LOST')")
    here = os.path.dirname(os.path.abspath(__file__))
    procs = [subprocess.Popen([sys.executable, "-c", code, os.path.join(tmp, "exec.db"), str(pid), f"tap{i}"],
                              cwd=here, stdout=subprocess.PIPE, text=True) for i in range(4)]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert outs.count("WON") == 1, outs
    fake.stop()


def _simulate_crash(con, pid, order_state):
    con.execute("UPDATE plans SET state='EXECUTING', approved_by='x', approved_at=? WHERE id=?",
                (int(time.time()) - 3600, pid))
    con.execute("UPDATE orders SET state=? WHERE plan_id=?", (order_state, pid))


def test_crash_before_submission():
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    _simulate_crash(con, pid, "PLANNED")
    ex.reconcile(con, client, NOSLEEP)
    assert states(con, pid)["XRP"][0] == "FAILED" and fake.submits == 0
    fake.stop()


def test_crash_after_submission_and_after_202():
    for journal_state in ("SUBMITTED", "ACCEPTED"):
        tmp, fake, client, con = setup()
        pid = plan(con, ["XRP"])
        row = con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,)).fetchone()
        fake.create_order("XRPUSDT", dict(client_order_id=row["client_order_id"], quantity="600"))  # it DID land
        _simulate_crash(con, pid, journal_state)
        ex.reconcile(con, client, NOSLEEP)
        st = states(con, pid)["XRP"]
        assert st[0] == "VERIFIED", (journal_state, st)
        assert fake.submits == 1                                   # reconciled, never resubmitted
        assert con.execute("SELECT state FROM plans WHERE id=?", (pid,)).fetchone()[0] == "COMPLETE"
        fake.stop()


def test_timeout_after_exchange_accepted():
    tmp, fake, client, con = setup()
    fake.faults["order"] = ["apply_timeout"]
    pid = plan(con, ["XRP"])
    final, _ = ex.execute(con, client, pid, "test", NOSLEEP)
    assert final == "COMPLETE", states(con, pid)
    assert fake.submits == 1 and len(fake.positions) == 1         # found by client_order_id, not resubmitted
    fake.stop()


def test_423_then_success_looks_up_before_resubmit():
    tmp, fake, client, con = setup()
    fake.faults["order"] = ["423", "423"]
    slept = []
    pid = plan(con, ["XRP"])
    final, _ = ex.execute(con, client, pid, "test", slept.append)
    assert final == "COMPLETE" and len(fake.positions) == 1, states(con, pid)
    cid = con.execute("SELECT client_order_id FROM orders WHERE plan_id=?", (pid,)).fetchone()[0]
    seq = [(m, p.rsplit("/", 1)[-1]) for m, p, c in fake.requests
           if (m == "POST" and p.endswith("/futures/order")) or (p.endswith("/orders/detail") and c == cid)]
    posts = [i for i, (m, _) in enumerate(seq) if m == "POST"]
    assert len(posts) == 3                                         # 423, 423, accepted
    for a, b in zip(posts, posts[1:]):
        assert any(seq[i][0] == "GET" for i in range(a + 1, b)), seq   # lookup between every resubmission
    assert slept and max(slept) <= 8                                # bounded backoff happened
    fake.stop()


def test_500_applied_and_500_not_applied():
    tmp, fake, client, con = setup()
    fake.faults["order"] = ["apply_500"]
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE" and fake.submits == 1
    fake.stop()
    tmp, fake, client, con = setup()
    fake.faults["order"] = ["500"]
    pid = plan(con, ["XRP"])
    final, _ = ex.execute(con, client, pid, "t", NOSLEEP)
    assert final == "RECONCILE_REQUIRED" and fake.submits == 0 and not fake.positions   # never blindly resubmitted
    fake.stop()


def test_restart_reconciliation_by_client_order_id():
    tmp, fake, client, con = setup()
    fake.faults["order"] = ["timeout"]                             # not applied, unknown to us
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "RECONCILE_REQUIRED"
    con.execute("UPDATE plans SET approved_at=? WHERE id=?", (int(time.time()) - 3600, pid))
    ex.reconcile(con, client, NOSLEEP)                             # restart: lookup says it never landed
    assert states(con, pid)["XRP"][0] == "FAILED" and not fake.positions
    fake.stop()


def test_manual_long_and_short_in_every_basket_symbol():
    for side in ("LONG", "SHORT"):
        tmp, fake, client, con = setup()
        alerts = []
        for c in s1.BASKET:
            fake.add_manual(c, side)
        manual_ids = {p["id"] for p in fake.positions}
        pid = plan(con, s1.BASKET)
        ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
        assert all(s == "FAILED" and "manual" in e for s, e in states(con, pid).values())
        assert fake.submits == 0 and {p["id"] for p in fake.positions} == manual_ids
        assert len([a for a in alerts if "manual position" in a]) == len(s1.BASKET)
        # a CLOSE for a manual position is refused too
        pid2 = plan(con, ["XRP"], action="CLOSE", position_ids={"XRP": next(iter(manual_ids))})
        ex.execute(con, client, pid2, "t", NOSLEEP)
        assert states(con, pid2)["XRP"][0] == "FAILED" and {p["id"] for p in fake.positions} == manual_ids
        fake.stop()


def test_stop_file_midway_through_basket():
    tmp, fake, client, con = setup()
    fake.hooks["after_fill"] = lambda o: open(ex.STOP_PATH, "w").close()
    pid = plan(con, ["XRP", "ADA", "DOGE"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    st = states(con, pid)
    assert st["XRP"][0] == "VERIFIED" and "STOP" in st["ADA"][1] and "STOP" in st["DOGE"][1]
    assert fake.submits == 1
    fake.stop()


def test_gap_makes_planned_stop_invalid_stop_follows_fill():
    tmp, fake, client, con = setup()
    fake.fill_price["XRP"] = 1.5 * 0.97          # fills 3% lower, still within drift check of live price 1.5
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE"
    pos = fake.positions[0]
    fill, stop = float(pos["entry_price"]), float(pos["stoploss"]["price"])
    assert abs(stop - (fill - s1.SL_ATR * 1.5 * 0.06)) < 0.001    # re-anchored to the actual fill
    fake.stop()


def test_price_drift_rejected():
    tmp, fake, client, con = setup()
    fake.prices["XRP"] = 1.5 * 1.05
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert "drift" in states(con, pid)["XRP"][1] and fake.submits == 0
    fake.stop()


def test_missing_stop_is_attached_from_fill():
    tmp, fake, client, con = setup()
    fake.drop_order_stop = True                   # entry arrives WITHOUT a stop; bot must attach one
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE"
    pos = fake.positions[0]
    assert float(pos["liquidation_price"]) < float(pos["stoploss"]["price"]) < float(pos["entry_price"])
    fake.stop()


def test_missing_or_failed_stop_attachment_halts_entries():
    tmp, fake, client, con = setup()
    fake.riskorder_ok = False
    fake.drop_order_stop = True                   # no stop from the order AND attaching one fails
    alerts = []
    pid = plan(con, ["XRP", "ADA"])
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    st = states(con, pid)
    assert st["XRP"][0] == "FAILED" and "stop" in st["XRP"][1]
    assert "halted" in st["ADA"][1] and fake.submits == 1
    assert any("UNPROTECTED" in a or "NOT verified" in a for a in alerts)
    fake.stop()


def test_wallet_larger_than_allocation_is_capped():
    tmp, fake, client, con = setup()
    fake.balance = 500000.0
    pid = plan(con, ["XRP"], notional=10 ** 7)
    ex.execute(con, client, pid, "t", NOSLEEP)
    pos = fake.positions[0]
    notional_inr = float(pos["quantity"]) * float(pos["entry_price"]) * 102
    assert notional_inr <= s1.LEV * s1.CAPITAL_CAP_INR + 1
    fake.stop()


def test_unknown_fill_halts_later_entries():
    tmp, fake, client, con = setup()
    fake.never_fill = True                                   # accepted (202) but never reaches a terminal status
    alerts = []
    pid = plan(con, ["XRP", "ADA"])
    final, _ = ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    st = states(con, pid)
    assert st["XRP"][0] == "RECONCILE_REQUIRED" and "halted" in st["ADA"][1] and fake.submits == 1
    assert final == "RECONCILE_REQUIRED"
    fake.stop()


def test_exchange_errors_after_fill_halt_and_alert():
    tmp, fake, client, con = setup()
    fake.hooks["after_fill"] = lambda o: fake.faults.__setitem__("positions", ["500"] * 50)
    alerts = []
    pid = plan(con, ["XRP", "ADA"])
    final, _ = ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    st = states(con, pid)
    assert st["XRP"][0] == "RECONCILE_REQUIRED" and "halted" in st["ADA"][1] and fake.submits == 1
    assert final == "RECONCILE_REQUIRED" and any("reconcile" in a for a in alerts)
    fake.stop()


def test_wrong_existing_stop_is_amended_with_patch():
    tmp, fake, client, con = setup()
    fake.fill_price["XRP"] = 1.5 * 0.97                      # order's own stop is now off-target for the fill
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE"
    assert any(m == "PATCH" for m, p, c in fake.requests)
    assert not any(m == "POST" and p.endswith("/riskorder") for m, p, c in fake.requests)
    fake.stop()


def test_wrong_existing_stop_and_failed_amend_halts():
    tmp, fake, client, con = setup()
    fake.fill_price["XRP"] = 1.5 * 0.97
    fake.riskorder_ok = False
    alerts = []
    pid = plan(con, ["XRP", "ADA"])
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    st = states(con, pid)
    assert st["XRP"][0] == "FAILED" and "not verified" in st["XRP"][1] and "halted" in st["ADA"][1]
    assert fake.submits == 1 and any("UNPROTECTED" in a for a in alerts)
    fake.stop()


def test_leverage_not_verified_refuses_entry():
    tmp, fake, client, con = setup()
    fake.leverage_stuck = 5
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert "leverage not verified" in states(con, pid)["XRP"][1] and fake.submits == 0
    fake.stop()


def test_stop_arriving_right_before_order_post():
    tmp, fake, client, con = setup()
    fake.hooks["on_leverage"] = lambda: open(ex.STOP_PATH, "w").close()
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert "STOP" in states(con, pid)["XRP"][1] and fake.submits == 0
    fake.stop()


def test_missing_liquidation_price_fails_closed():
    tmp, fake, client, con = setup()
    fake.no_liq = True
    alerts = []
    pid = plan(con, ["XRP", "ADA"])
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    st = states(con, pid)
    assert st["XRP"][0] == "RECONCILE_REQUIRED" and "halted" in st["ADA"][1]
    assert any("liquidation" in a for a in alerts)
    fake.stop()


def test_unconfirmed_bot_pnl_blocks_entries():
    tmp, fake, client, con = setup()
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at, closed_at) VALUES('gone','XRP','s1-x',1,2)")
    pid = plan(con, ["ADA"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert "P&L unconfirmed" in states(con, pid)["ADA"][1] and fake.submits == 0
    fake.stop()


def test_daily_cap_counts_losses_realized_before_restart():
    tmp, fake, client, con = setup()
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at, closed_at, realized_pnl) "
                "VALUES('p1','XRP','s1-x',?,?,-300)", (int(time.time()) - 60, int(time.time()) - 30))
    pid = plan(con, ["ADA"])                                  # fresh process/day: no ledger row yet
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert "loss cap" in states(con, pid)["ADA"][1] and fake.submits == 0   # -300 today > 5% of 5000
    fake.stop()


def test_every_write_names_inr_currency():
    tmp, fake, client, con = setup()
    fake.fill_price["XRP"] = 1.5 * 0.97                       # forces a stop PATCH too
    pid = plan(con, ["XRP", "ADA"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE"
    close = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id=next(iter(ex.owned_ids(con))))], {})
    ex.execute(con, client, close, "t", NOSLEEP)
    kinds = {p.rsplit("/", 1)[-1] for m, p, c in fake.requests if m in ("POST", "PATCH")}
    assert {"order", "leverage", "riskorder", "close"} <= kinds and fake.bad_currency == []
    fake.stop()


def test_position_mismatch_after_fill_halts():
    tmp, fake, client, con = setup()
    fake.qty_skew = 2.0                                      # exchange position is twice what we filled
    alerts = []
    pid = plan(con, ["XRP", "ADA"])
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    st = states(con, pid)
    assert st["XRP"][0] == "RECONCILE_REQUIRED" and "mismatch" in st["XRP"][1] and "halted" in st["ADA"][1]
    assert float(fake.positions[0]["stoploss"]["price"]) > 0   # still protected by a stop
    fake.stop()


def test_margin_type_must_be_exactly_isolated():
    for mt in (None, "isolated", "CROSS", ""):
        tmp, fake, client, con = setup()
        fake.margin_type = mt
        pid = plan(con, ["XRP"])
        ex.execute(con, client, pid, "t", NOSLEEP)
        assert "leverage not verified" in states(con, pid)["XRP"][1] and fake.submits == 0, mt
        fake.stop()


def test_missing_or_excessive_applied_fx_halts():
    for rate, why in (("", "rate missing"), ("130", "exceeds")):
        tmp, fake, client, con = setup()
        fake.applied_rate = rate
        alerts = []
        pid = plan(con, ["XRP", "ADA"])
        ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
        st = states(con, pid)
        assert st["XRP"][0] == "RECONCILE_REQUIRED" and "halted" in st["ADA"][1], (rate, st)
        assert any("INR" in a for a in alerts)
        fake.stop()


def test_one_plan_at_a_time_lease_and_supersede():
    tmp, fake, client, con = setup()
    old = plan(con, ["XRP"])
    new = plan(con, ["ADA"])
    assert con.execute("SELECT state FROM plans WHERE id=?", (old,)).fetchone()[0] == "FAILED"   # superseded
    assert "already FAILED" in ex.claim(con, old, "late tap")
    con.execute("UPDATE plans SET state='EXECUTING' WHERE id=?", (new,))
    third = plan(con, ["DOGE"])
    con.execute("UPDATE plans SET state='EXECUTING' WHERE id=?", (new,))   # record_plan only supersedes PLANNED
    assert "still executing" in ex.claim(con, third, "t")
    fake.stop()


def test_unresolved_plan_blocks_entries_but_not_closes():
    tmp, fake, client, con = setup()
    stuck = plan(con, ["XRP"])
    con.execute("UPDATE plans SET state='RECONCILE_REQUIRED' WHERE id=?", (stuck,))
    entry = plan(con, ["ADA"])
    assert "reconciliation" in ex.claim(con, entry, "t")
    close = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id="p")], {})
    assert ex.claim(con, close, "t") is None
    fake.stop()


def test_alert_failure_never_breaks_execution():
    tmp, fake, client, con = setup()

    def broken(msg):
        raise ConnectionError("telegram down")
    pid = plan(con, ["XRP"])
    final, _ = ex.execute(con, client, pid, "t", NOSLEEP, broken)
    assert final == "COMPLETE"
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='alert_failed'").fetchone()[0] >= 1
    fake.stop()


def test_watcher_flags_moved_stop_and_retries_cap_close_delivery():
    import watcher
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE"
    fake.positions[0]["stoploss"]["price"] = "1.0"            # someone moved the stop far from the verified level
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at, closed_at, realized_pnl) "
                "VALUES('lost','ADA','s1-y',?,?,-400)", (int(time.time()) - 60, int(time.time()) - 30))
    sent, st = [], {}
    orig = (watcher.notify, watcher.STATUS_PATH, watcher.LOG_PATH, watcher.JOURNAL_PATH, ex.GUARD_PATH)
    watcher.STATUS_PATH, watcher.LOG_PATH, watcher.JOURNAL_PATH = (os.path.join(tmp, n) for n in
                                                                   ("ws.json", "w.log", "j.csv"))
    watcher.notify = lambda msg, buttons=None: sent.append((msg, buttons)) or len(sent) > 2   # first sends fail
    try:
        watcher.check(st, client, con, make_plan=lambda: dict(plan_id=None, orders=[], created_at=time.time()))
        assert any("stop moved" in m for m, _ in sent)
        assert "cap_day" not in st and st.get("cap_plan")          # cap-close alert not delivered -> retry kept
        cap_pid = st["cap_plan"]["plan_id"]
        watcher.check(st, client, con, make_plan=lambda: dict(plan_id=None, orders=[], created_at=time.time()))
        assert st.get("cap_day") and any(b and f"approve:{cap_pid}" in str(b) for _, b in sent)
        assert con.execute("SELECT COUNT(*) FROM plans WHERE payload LIKE '%cap%'").fetchone()[0] == 1   # no dupes
    finally:
        watcher.notify, watcher.STATUS_PATH, watcher.LOG_PATH, watcher.JOURNAL_PATH, ex.GUARD_PATH = orig
    fake.stop()


def test_live_disabled_by_default_refuses():
    tmp, fake, client, con = setup()
    os.environ["LIVE_TRADING_ENABLED"] = "false"
    try:
        pid = plan(con, ["XRP"])
        assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "REFUSED" and fake.submits == 0
    finally:
        os.environ["LIVE_TRADING_ENABLED"] = "true"
    fake.stop()


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__, flush=True)
    print(f"{len(tests)} passed")
