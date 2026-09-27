"""Deterministic execution-safety tests against a local fake Mudrex server. Never contacts the live API.
Run: python test_execution.py
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

os.environ["MUDREX_TEST_MODE"] = "1"
os.environ["LIVE_TRADING_ENABLED"] = "true"          # tests exercise the live path against the FAKE server only

import execution as ex                                  # noqa: E402
import fake_mudrex                                      # noqa: E402
import s1                                               # noqa: E402
import trade_policy                                     # noqa: E402
from mudrex_client import Ambiguous, Client, Rejected   # noqa: E402

trade_policy.AUTONOMOUS_HEDGE_READY = True              # explicit in-process fake-exchange test unlock

PRICES = {"XRP": 1.5, "ADA": 0.26, "DOGE": 0.1, "LINK": 14.0, "AVAX": 11.0, "TRX": 0.34}
NOSLEEP = lambda s: None                                # noqa: E731


def setup(prices=PRICES):
    tmp = tempfile.mkdtemp()
    ex.STOP_PATH, ex.GUARD_PATH = os.path.join(tmp, "STOP"), os.path.join(tmp, "guard.json")
    fake = fake_mudrex.FakeMudrex(prices)
    client = Client(base=fake.url, secret="test", timeout=0.5, sleep=NOSLEEP)
    con = ex.db(os.path.join(tmp, "exec.db"))
    return tmp, fake, client, con


def plan(con, coins, notional=1000.0, action="OPEN", position_ids=None, atr_pct=0.06):
    orders = [dict(coin=c, action=action, planned_price=PRICES[c], notional_inr=notional, atr=PRICES[c] * atr_pct,
                   position_id=(position_ids or {}).get(c)) for c in coins]
    return ex.record_plan(con, "2026-09-26", orders, {})


def exited(st, fake):
    """The protective-exit rule: the order ends FAILED with the reason and no position is left open."""
    return st[0] == "FAILED" and "exited automatically" in st[1] and not fake.positions


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


def test_short_entry_has_verified_two_sided_bracket_and_variable_leverage():
    tmp, fake, client, con = setup()
    px = PRICES["XRP"]
    pid = ex.record_plan(con, "d", [dict(coin="XRP", action="OPEN", side="SHORT", planned_price=px,
                                         notional_inr=1000, atr=0.02, stop_loss=px + 0.04,
                                         take_profit=px - 0.06, leverage=3, planned_risk_inr=40)], {})
    final, _ = ex.execute(con, client, pid, "autonomous test", NOSLEEP)
    assert final == "COMPLETE", states(con, pid)
    pos = fake.positions[0]
    assert pos["order_type"] == "SHORT" and float(pos["leverage"]) == 3
    assert float(pos["takeprofit"]["price"]) < float(pos["entry_price"]) < float(pos["stoploss"]["price"])
    row = con.execute("SELECT stop_price, target_price FROM orders WHERE plan_id=?", (pid,)).fetchone()
    assert row["stop_price"] and row["target_price"]
    fake.stop()


def test_fourth_set_needs_exact_single_use_telegram_approval():
    tmp, fake, client, con = setup()
    cycle = trade_policy.cycle_id()
    for n in range(1, 4):
        pid = plan(con, [s1.BASKET[n - 1]])
        con.execute("UPDATE trade_sets SET set_number=?, state='COMPLETE', attempted_at=?, completed_at=? WHERE plan_id=?",
                    (n, int(time.time()), int(time.time()), pid))
        con.execute("UPDATE plans SET state='FAILED' WHERE id=?", (pid,))
    pid = plan(con, ["LINK"])
    row = con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,)).fetchone()
    assert ex.plan_requires_set_approval(con, pid)
    assert "Telegram approval required" in ex.authorize_set_submission(con, row)
    assert ex.journal_set_approval(con, pid, "telegram owner") is None
    assert ex.authorize_set_submission(con, row) is None
    item = con.execute("SELECT set_number FROM trade_sets WHERE plan_id=?", (pid,)).fetchone()
    approval = con.execute("SELECT consumed_at FROM set_approvals WHERE proposal_id=(SELECT proposal_id FROM "
                           "trade_sets WHERE plan_id=?)", (pid,)).fetchone()
    assert item["set_number"] == 4 and approval["consumed_at"] is not None and cycle == trade_policy.cycle_id()
    fake.stop()


def test_failed_attempts_do_not_count_but_unresolved_attempts_block():
    tmp, fake, client, con = setup()
    for n in range(3):
        pid = plan(con, [s1.BASKET[n]])
        con.execute("UPDATE trade_sets SET state=? WHERE plan_id=?",
                    ("COMPLETE" if n < 2 else "FAILED", pid))
        con.execute("UPDATE plans SET state='FAILED' WHERE id=?", (pid,))
    pid = plan(con, ["LINK"])
    row = con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,)).fetchone()
    assert not ex.plan_requires_set_approval(con, pid)
    assert ex.authorize_set_submission(con, row) is None
    assert con.execute("SELECT set_number FROM trade_sets WHERE plan_id=?", (pid,)).fetchone()[0] == 3

    # Simulate a write whose exchange outcome is not yet known. It does not count as
    # a completed set, but it blocks every subsequent set pending reconciliation.
    con.execute("UPDATE trade_sets SET state='ATTEMPTED', attempted_at=? WHERE plan_id=?",
                (int(time.time()), pid))
    con.execute("UPDATE plans SET state='FAILED' WHERE id=?", (pid,))
    next_pid = plan(con, ["AVAX"])
    next_row = con.execute("SELECT * FROM orders WHERE plan_id=?", (next_pid,)).fetchone()
    assert "unresolved" in ex.authorize_set_submission(con, next_row)
    fake.stop()


def test_definite_rejection_marks_attempted_set_failed():
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    row = con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,)).fetchone()
    def reject():
        raise Rejected(400, [{"text": "definite test rejection"}])
    assert ex.submit_with_reconcile(con, client, row, reject, NOSLEEP, None) is None
    set_row = con.execute("SELECT state, attempted_at FROM trade_sets WHERE plan_id=?", (pid,)).fetchone()
    assert set_row["state"] == "FAILED" and set_row["attempted_at"] is not None
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
        fake.create_order("XRPUSDT", dict(client_order_id=row["client_order_id"], quantity="6"))    # it DID land
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
    ex.reconcile(con, client, NOSLEEP)                             # young: history may lag, not judged yet
    assert states(con, pid)["XRP"][0] == "RECONCILE_REQUIRED"
    con.execute("UPDATE orders SET updated_at=? WHERE plan_id=?", (int(time.time()) - 3600, pid))
    ex.reconcile(con, client, NOSLEEP)                             # restart: lookup says it never landed
    assert states(con, pid)["XRP"][0] == "FAILED" and not fake.positions
    fake.stop()


def test_inconclusive_lookup_never_releases_an_unknown_order():
    tmp, fake, client, con = setup()
    fake.faults["order"] = ["timeout"]
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "RECONCILE_REQUIRED"
    con.execute("UPDATE plans SET approved_at=? WHERE id=?", (int(time.time()) - 3600, pid))
    fake.faults["detail"] = ["500"] * 100                          # exchange cannot answer the lookup
    ex.reconcile(con, client, NOSLEEP)
    assert states(con, pid)["XRP"][0] == "RECONCILE_REQUIRED"      # deferred, not "not found"
    assert "reconciliation" in ex.claim(con, plan(con, ["ADA"]), "t")   # entries stay blocked
    fake.faults["detail"] = ["500"] * 4                            # ONE failed lookup mixed with 404s
    ex.reconcile(con, client, NOSLEEP)
    assert states(con, pid)["XRP"][0] == "RECONCILE_REQUIRED"      # still unknown, never "safely absent"
    fake.faults["detail"] = []
    con.execute("UPDATE orders SET updated_at=? WHERE plan_id=?", (int(time.time()) - 3600, pid))   # past grace
    ex.reconcile(con, client, NOSLEEP)                             # every lookup answers 404: absent
    assert states(con, pid)["XRP"][0] == "FAILED"
    fake.stop()


def test_recovery_checks_fill_against_approved_notional():
    tmp, fake, client, con = setup()
    fake.applied_rate = "130"
    pid = plan(con, ["XRP"])
    row = con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,)).fetchone()
    fake.create_order("XRPUSDT", dict(client_order_id=row["client_order_id"], quantity="6"))
    _simulate_crash(con, pid, "ACCEPTED")                          # crashed after fill, before the notional check
    alerts = []
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    st = states(con, pid)["XRP"]
    assert exited(st, fake) and "allowed" in st[1], st            # over its approval: exited automatically
    assert any("exceeds the approved" in a for a in alerts) and any("EXITING" in a for a in alerts)
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
    assert exited(st["XRP"], fake) and "stop" in st["XRP"][1]    # unprotected fill: exited automatically
    assert "halted" in st["ADA"][1] and fake.submits == 1
    assert any("UNPROTECTED" in a or "NOT verified" in a for a in alerts)
    fake.stop()


def test_wallet_larger_than_allocation_is_capped():
    tmp, fake, client, con = setup()
    fake.balance = 500000.0
    pid = ex.record_plan(con, "d", [dict(coin="XRP", action="OPEN", planned_price=1.5, notional_inr=10 ** 7,
                                         atr=0.003)], {})                   # tight stop: loss budget not binding
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
    assert exited(st["XRP"], fake) and "not verified" in st["XRP"][1] and "halted" in st["ADA"][1]
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
    assert exited(st["XRP"], fake) and "halted" in st["ADA"][1]
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
                "VALUES('p1','XRP','s1-x',?,?,-500)", (int(time.time()) - 60, int(time.time()) - 30))
    pid = plan(con, ["ADA"])                                  # fresh process/day: no ledger row yet
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert "loss cap" in states(con, pid)["ADA"][1] and fake.submits == 0   # absolute Rs500 owner limit
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
    pos_id = fake.positions[0]["id"]
    assert pos_id not in ex.owned_ids(con)                     # quarantined: never bot-owned
    assert not any(p.endswith("/riskorder") for m, p, c in fake.requests)   # its stop was never touched
    ex.recover_ownership(con, client)
    assert pos_id not in ex.owned_ids(con)                     # history recovery cannot re-own it either
    close = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id=pos_id)], {})
    con.execute("UPDATE plans SET state='PLANNED' WHERE id=?", (close,))
    ex.execute(con, client, close, "t", NOSLEEP)
    assert "not owned" in states(con, close)["XRP"][1] and fake.positions   # the bot refuses to close it
    fake.stop()


def test_crash_between_stop_check_and_notional_check_is_rechecked():
    tmp, fake, client, con = setup()
    real = ex.fill_within_approval
    ex.fill_within_approval = lambda *a: (_ for _ in ()).throw(SystemExit("process killed"))   # a real crash
    try:
        pid = plan(con, ["XRP"])
        ex.execute(con, client, pid, "t", NOSLEEP)
    except SystemExit:
        pass
    finally:
        ex.fill_within_approval = real
    assert states(con, pid)["XRP"][0] != "VERIFIED"               # stop verified alone is not enough
    ex.reconcile(con, client, NOSLEEP)
    assert states(con, pid)["XRP"][0] == "FILLED"                  # crashed run's lease still live: not touched
    con.execute("UPDATE plans SET lease_until=? WHERE id=?", (int(time.time()) - 1, pid))   # lease expired
    ex.reconcile(con, client, NOSLEEP)                             # no 15-minute wait any more
    assert states(con, pid)["XRP"][0] == "VERIFIED" and fake.submits == 1
    fake.stop()


def test_closes_run_first_and_unconfirmed_close_halts_entries():
    tmp, fake, client, con = setup()
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    xrp = next(iter(ex.owned_ids(con)))
    pid = ex.record_plan(con, "d", [dict(coin="ADA", action="OPEN", planned_price=PRICES["ADA"], notional_inr=1000,
                                         atr=PRICES["ADA"] * 0.06),
                                    dict(coin="XRP", action="CLOSE", position_id=xrp)], {})
    fake.faults["close"] = ["500"]                              # close outcome unknown, position still open
    before = fake.submits
    ex.execute(con, client, pid, "t", NOSLEEP)
    st = states(con, pid)
    assert st["XRP"][0] == "RECONCILE_REQUIRED" and "unconfirmed" in st["ADA"][1] and fake.submits == before
    fake.stop()


def test_total_bot_exposure_capped_across_positions():
    tmp, fake, client, con = setup()
    assert ex.execute(con, client, plan(con, ["XRP"], notional=6000, atr_pct=0.005), "t", NOSLEEP)[0] == "COMPLETE"
    pid = plan(con, ["ADA"], notional=6000, atr_pct=0.005)
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert "allocation cap" in states(con, pid)["ADA"][1] and len(fake.positions) == 1
    fake.stop()


def test_buy_plans_expire_after_15_minutes_close_plans_after_3_hours():
    tmp, fake, client, con = setup()
    buy = plan(con, ["XRP"])
    con.execute("UPDATE plans SET created_at=? WHERE id=?", (int(time.time()) - 16 * 60, buy))
    assert ex.claim(con, buy, "t").startswith("EXPIRED") and fake.submits == 0
    close = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id="p")], {})
    con.execute("UPDATE plans SET created_at=? WHERE id=?", (int(time.time()) - 60 * 60, close))
    assert ex.claim(con, close, "t") is None                       # a 1-hour-old close plan is still valid
    fake.stop()


def test_freshness_rechecked_right_before_the_order_is_sent():
    for hook in ("price", "age"):
        tmp, fake, client, con = setup()
        pid = plan(con, ["XRP"])
        if hook == "price":
            fake.hooks["on_leverage"] = lambda: fake.prices.__setitem__("XRP", PRICES["XRP"] * 1.05)
        else:
            fake.hooks["on_leverage"] = lambda: ex.db(os.path.join(tmp, "exec.db")).execute(   # server thread
                "UPDATE plans SET created_at=? WHERE id=?", (int(time.time()) - 21 * 60, pid))
        ex.execute(con, client, pid, "t", NOSLEEP)
        st = states(con, pid)["XRP"]
        assert st[0] == "FAILED" and ("drifted" in st[1] if hook == "price" else "too old" in st[1]), st
        assert fake.submits == 0
        fake.stop()


def test_recovery_only_owns_positions_the_journal_verified():
    tmp, fake, client, con = setup()
    fake.create_order("XRPUSDT", dict(client_order_id="s1-99-0-XRP-O", quantity="6"))
    fake._maybe_fill(fake.orders["s1-99-0-XRP-O"])              # filled on the exchange, unknown to the journal
    ex.recover_ownership(con, client)
    assert fake.positions and not ex.owned_ids(con)              # treated as manual: never touched
    fake.stop()


def test_watcher_reoffers_expired_cap_close_and_flags_stuck_orders():
    import watcher
    tmp, fake, client, con = setup()
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at, closed_at, realized_pnl) "
                "VALUES('lost','ADA','s1-y',?,?,-600)", (int(time.time()) - 60, int(time.time()) - 30))
    sent, st = [], {}
    orig = (watcher.notify, watcher.STATUS_PATH, watcher.LOG_PATH, watcher.JOURNAL_PATH)
    watcher.STATUS_PATH, watcher.LOG_PATH, watcher.JOURNAL_PATH = (os.path.join(tmp, n) for n in
                                                                   ("ws.json", "w.log", "j.csv"))
    watcher.notify = lambda msg, buttons=None: sent.append((msg, buttons)) or True
    mk = lambda: dict(plan_id=None, orders=[], created_at=time.time())      # noqa: E731
    closes = lambda: [b for _, b in sent if b and "Close all" in str(b)]      # noqa: E731
    try:
        watcher.check(st, client, con, make_plan=mk)
        first = st["cap_sent"]["plan_id"]
        watcher.check(st, client, con, make_plan=mk)
        assert len(closes()) == 1                                  # delivered once, not repeated
        con.execute("UPDATE plans SET state='FAILED', note='expired' WHERE id=?", (first,))
        watcher.check(st, client, con, make_plan=mk)
        assert len(closes()) == 2 and st["cap_sent"]["plan_id"] != first      # expired -> fresh Close all
        con.execute("UPDATE plans SET state='FAILED', note='rejected by user' WHERE id=?", (st["cap_sent"]["plan_id"],))
        watcher.check(st, client, con, make_plan=mk)
        assert len(closes()) == 2                                  # "Keep" is respected
        con.execute("UPDATE orders SET state='RECONCILE_REQUIRED', updated_at=? WHERE plan_id=1",
                    (int(time.time()) - 3600,))
        watcher.check(st, client, con, make_plan=mk)
        assert any("unfinished order" in m for m, _ in sent)
    finally:
        watcher.notify, watcher.STATUS_PATH, watcher.LOG_PATH, watcher.JOURNAL_PATH = orig
    fake.stop()


def test_loss_budgets_per_trade_and_total():
    tmp, fake, client, con = setup()
    wide = ex.record_plan(con, "d", [dict(coin="XRP", action="OPEN", planned_price=1.5, notional_inr=2000,
                                          atr=0.1)], {})                     # stop 0.3 below: 20% x Rs 2000 = Rs 400
    ex.execute(con, client, wide, "t", NOSLEEP)
    assert "loss budget" in states(con, wide)["XRP"][1] and fake.submits == 0   # > 7% of Rs 5,000
    for c in ["XRP", "ADA", "DOGE"]:                             # each ~Rs 1,070 at 18% stop = ~Rs 198
        pid = ex.record_plan(con, "d", [dict(coin=c, action="OPEN", planned_price=PRICES[c], notional_inr=1100,
                                             atr=PRICES[c] * 0.06)], {})
        ex.execute(con, client, pid, "t", NOSLEEP)
    last = states(con, pid)[c]
    assert last[0] == "FAILED" and "all stops" in last[1], last             # third exceeds fixed Rs500 collective risk
    assert len(fake.positions) == 2
    fake.stop()


def test_post_fill_budget_breach_exits_automatically():
    tmp, fake, client, con = setup()
    real = trade_policy.DAILY_LOSS_LIMIT_INR
    fake.hooks["after_fill"] = lambda o: setattr(trade_policy, "DAILY_LOSS_LIMIT_INR", 1.0)
    alerts = []
    try:
        pid = plan(con, ["XRP", "ADA"])
        ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    finally:
        trade_policy.DAILY_LOSS_LIMIT_INR = real
    st = states(con, pid)
    assert exited(st["XRP"], fake) and "loss budget" in st["XRP"][1] and "halted" in st["ADA"][1], st
    assert any("EXITING" in a for a in alerts) and any("exit done" in a for a in alerts)
    fake.stop()


def test_protective_exit_runs_even_with_stop_file_and_never_touches_manual():
    tmp, fake, client, con = setup()
    fake.riskorder_ok, fake.drop_order_stop = False, True
    fake.add_manual("DOGE", "LONG")
    fake.hooks["after_fill"] = lambda o: open(ex.STOP_PATH, "w").close()   # kill switch arrives after the fill
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert states(con, pid)["XRP"][1].startswith("exited automatically")  # risk-reducing exit still ran
    assert [p["symbol"] for p in fake.positions] == ["DOGEUSDT"]          # the manual position is untouched
    fake.stop()


def test_reconcile_survives_malformed_data_and_continues():
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP", "ADA"])
    rows = {r["coin"]: r for r in con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,))}
    for c in ("XRP", "ADA"):
        fake.create_order(c + "USDT", dict(client_order_id=rows[c]["client_order_id"], quantity="6"))
    _simulate_crash(con, pid, "ACCEPTED")
    fake.hooks["after_fill"] = lambda o: o["symbol"] == "XRPUSDT" and fake.specs["XRP"].pop("price_step")
    alerts = []
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    st = states(con, pid)
    assert st["ADA"][0] == "VERIFIED", st                            # one bad record does not block the rest
    assert any("reconcile error" in a for a in alerts)
    assert st["XRP"][1].startswith("exited automatically"), st       # unverifiable validated fill: fail-safe exit
    fake.stop()


def _unprotectable(fake):
    fake.riskorder_ok, fake.drop_order_stop = False, True


def close_posts(fake):
    return sum(1 for m, p, c in fake.requests if m == "POST" and p.endswith("/close"))


def test_protective_exit_is_order_bound_and_revalidated():
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    real = ex.verify_entry

    def verify_then_rebind(con_, client_, oid, sleep, alert):
        ok = real(con_, client_, oid, sleep, alert)
        con_.execute("UPDATE owned SET client_order_id='s1-other-plan-O'")      # now owned by ANOTHER order
        return ok
    ex.verify_entry = verify_then_rebind
    try:
        pid = plan(con, ["XRP"])
        ex.execute(con, client, pid, "t", NOSLEEP)
    finally:
        ex.verify_entry = real
    assert close_posts(fake) == 0 and fake.positions                          # not this order's: never closed
    assert "not bound" in states(con, pid)["XRP"][1]
    fake.stop()
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    real_close = ex.protective_close

    def change_then_close(con_, client_, row, why, sleep, alert):
        fake.positions[0]["quantity"] = "99"                                   # user changed it in the app
        return real_close(con_, client_, row, why, sleep, alert)
    ex.protective_close = change_then_close
    try:
        pid = plan(con, ["XRP"])
        ex.execute(con, client, pid, "t", NOSLEEP)
    finally:
        ex.protective_close = real_close
    assert close_posts(fake) == 0 and "position changed" in states(con, pid)["XRP"][1]
    fake.stop()


def test_unknown_balance_after_fill_exits():
    tmp, fake, client, con = setup()
    fake.hooks["after_fill"] = lambda o: ex.db(os.path.join(tmp, "exec.db")).execute(
        "INSERT INTO owned(position_id, coin, client_order_id, opened_at, closed_at) VALUES('gone','LINK','s1-q',1,2)")
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    st = states(con, pid)["XRP"]
    assert exited(st, fake) and "balance unknown" in st[1], st
    fake.stop()


def test_protective_exit_is_not_resent_until_proven_needed():
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    fake.faults["close"] = ["timeout"] * 10                    # every close times out and is NOT applied
    alerts = []
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    assert close_posts(fake) == 1 and "not confirmed" in states(con, pid)["XRP"][1]
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    assert close_posts(fake) == 1                              # within 10 min: waits, no second close
    for n in (2, 3):
        con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))
        ex.reconcile(con, client, NOSLEEP, alerts.append)
        assert close_posts(fake) == n                          # re-sent only after 10 min, still open
    con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    assert close_posts(fake) == 3 and any("failed 3 times" in a for a in alerts)   # then it hands over to you
    fake.stop()


def test_crashed_approved_close_is_finished_by_reconcile():
    tmp, fake, client, con = setup()
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    xrp = next(iter(ex.owned_ids(con)))
    pid = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id=xrp)], {})
    _simulate_crash(con, pid, "SUBMITTED")                 # crashed before the close was sent
    alerts = []
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    assert close_posts(fake) == 1 and any("finishing the approved close" in a for a in alerts)
    ex.reconcile(con, client, NOSLEEP, alerts.append)      # position gone now
    assert states(con, pid)["XRP"][0] == "VERIFIED" and not fake.positions
    assert con.execute("SELECT state FROM plans WHERE id=?", (pid,)).fetchone()[0] == "COMPLETE"
    # a close that never lands: retried after 10 min, max 3 times, then handed to the human
    tmp, fake, client, con = setup()
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    xrp = next(iter(ex.owned_ids(con)))
    pid = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id=xrp)], {})
    _simulate_crash(con, pid, "SUBMITTED")
    fake.faults["close"] = ["timeout"] * 10
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    assert close_posts(fake) == 1                          # within 10 min: not re-sent
    for n in (2, 3, 3):
        con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))
        ex.reconcile(con, client, NOSLEEP, alerts.append)
        assert close_posts(fake) == n, (n, close_posts(fake))
    assert "not confirmed after 3 tries" in states(con, pid)["XRP"][1]
    fake.stop()


def _one_verified_xrp():
    tmp, fake, client, con = setup()
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    return tmp, fake, client, con, next(iter(ex.owned_ids(con)))


def _close_plan(con, pos_id):
    return ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id=pos_id)], {})


def test_no_second_close_while_another_close_is_unresolved():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    first = _close_plan(con, xrp)
    fake.faults["close"] = ["timeout"] * 20                          # first close: outcome unknown
    ex.execute(con, client, first, "t", NOSLEEP)
    assert states(con, first)["XRP"][0] == "RECONCILE_REQUIRED" and close_posts(fake) == 1
    second = _close_plan(con, xrp)
    ex.execute(con, client, second, "t", NOSLEEP)
    assert close_posts(fake) == 1 and "still closing" in states(con, second)["XRP"][1]
    fake.stop()


def test_approved_close_refused_if_position_changed():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    fake.positions[0]["quantity"] = "99"                             # you added to it in the app
    pid = _close_plan(con, xrp)
    alerts = []
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    assert close_posts(fake) == 0 and "position changed" in states(con, pid)["XRP"][1]
    assert any("close NOT sent" in a for a in alerts)
    fake.stop()


def test_crash_before_approved_close_was_sent_is_finished_and_respects_stop():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    pid = _close_plan(con, xrp)
    _simulate_crash(con, pid, "PLANNED")                              # approved, crashed before sending
    open(ex.STOP_PATH, "w").close()
    ex.reconcile(con, client, NOSLEEP)
    assert close_posts(fake) == 0 and "paused" in states(con, pid)["XRP"][1]   # human close obeys STOP
    os.remove(ex.STOP_PATH)
    ex.reconcile(con, client, NOSLEEP)
    assert close_posts(fake) == 1
    ex.reconcile(con, client, NOSLEEP)
    assert states(con, pid)["XRP"][0] == "VERIFIED" and not fake.positions
    fake.stop()


def test_estimated_equity_never_becomes_a_trusted_baseline():
    tmp, fake, client, con = setup()
    D, off = 86400, ex.config.IST_OFFSET
    midnight = (int(time.time()) + off) // D * D - off + D
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at) VALUES('p','XRP','s1-z',1)")
    ex.caps_state(con, 6000, 0, now=midnight - 300, trusted=False)   # P&L unconfirmed: an estimate
    assert not ex.caps_state(con, 5000, 0, now=midnight + 3600)["baseline_ok"]
    fake.stop()


def test_held_position_without_inr_rate_blocks_entries():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    fake.positions[0]["entry_hedge_rate"] = ""
    pid = plan(con, ["ADA"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert "rate unknown" in states(con, pid)["ADA"][1] and len(fake.positions) == 1
    fake.stop()


def test_price_rechecked_before_a_resend_after_423():
    tmp, fake, client, con = setup()
    fake.faults["order"] = ["423"]
    moved = lambda s: fake.prices.__setitem__("XRP", PRICES["XRP"] * 1.05)   # price jumps during the back-off
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", moved)
    st = states(con, pid)["XRP"]
    assert st[0] == "FAILED" and "drifted" in st[1] and not fake.positions, st
    fake.stop()


def test_you_can_still_close_a_position_whose_entry_needs_reconciling():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    con.execute("UPDATE orders SET state='RECONCILE_REQUIRED' WHERE action='OPEN'")   # e.g. auto-exit gave up
    pid = _close_plan(con, xrp)
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE" and not fake.positions
    fake.stop()


def test_human_close_waits_while_an_automatic_exit_is_in_flight():
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    fake.faults["close"] = ["timeout"] * 20                          # automatic exit sent, outcome unknown
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert close_posts(fake) == 1 and fake.positions
    pos_id = fake.positions[0]["id"]
    human = _close_plan(con, pos_id)
    ex.execute(con, client, human, "t", NOSLEEP)
    assert close_posts(fake) == 1 and "automatic exit" in states(con, human)["XRP"][1]   # no second request
    fake.stop()


def test_one_missing_snapshot_never_ends_a_close():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    hidden = fake.positions.pop()                                   # Mudrex briefly omits it, not in history
    pid = _close_plan(con, xrp)
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert states(con, pid)["XRP"][0] == "RECONCILE_REQUIRED" and close_posts(fake) == 0   # not "closed"
    fake.positions.append(hidden)                                   # it is back
    ex.reconcile(con, client, NOSLEEP)
    ex.reconcile(con, client, NOSLEEP)
    assert close_posts(fake) == 1 and states(con, pid)["XRP"][0] == "VERIFIED" and not fake.positions
    fake.stop()


def test_close_never_waits_silently_on_an_invisible_position():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    fake.positions.pop()                                            # gone, but never shows up in history
    pid = _close_plan(con, xrp)
    alerts = []
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    assert not any("30 min" in a for a in alerts)                   # not yet
    con.execute("UPDATE orders SET updated_at=? WHERE plan_id=?", (int(time.time()) - 1801, pid))
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    assert sum("30 min" in a for a in alerts) == 1                  # told once, not every pass
    assert states(con, pid)["XRP"][0] == "RECONCILE_REQUIRED" and close_posts(fake) == 0
    fake.stop()


def test_exit_fence_claim_is_atomic():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    a, b = _close_plan(con, xrp), None
    row_a = con.execute("SELECT * FROM orders WHERE plan_id=?", (a,)).fetchone()
    con.execute("UPDATE plans SET state='APPROVED' WHERE id=?", (a,))
    b = ex.record_plan(con, "d2", [dict(coin="XRP", action="CLOSE", position_id=xrp)], {"reason": "cap"})
    row_b = con.execute("SELECT * FROM orders WHERE plan_id=?", (b,)).fetchone()
    con2 = ex.db(os.path.join(tmp, "exec.db"))                      # a second process
    assert ex.claim_exit(con, row_a) is None                        # first one takes the fence
    assert "still closing" in ex.claim_exit(con2, row_b)            # second one sees it, never both
    fake.stop()


def test_a_finished_exit_still_fences_a_second_close():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    a = _close_plan(con, xrp)
    row_a = con.execute("SELECT * FROM orders WHERE plan_id=?", (a,)).fetchone()
    con.execute("UPDATE plans SET state='APPROVED' WHERE id=?", (a,))
    assert ex.claim_exit(con, row_a) is None
    con.execute("UPDATE orders SET state='VERIFIED' WHERE id=?", (row_a["id"],))   # A finished its close
    b = ex.record_plan(con, "d2", [dict(coin="XRP", action="CLOSE", position_id=xrp)], {"reason": "cap"})
    row_b = con.execute("SELECT * FROM orders WHERE plan_id=?", (b,)).fetchone()
    assert "still closing" in ex.claim_exit(con, row_b)             # B still cannot send a second close
    con.execute("UPDATE orders SET exit_sent_at=? WHERE id=?", (int(time.time()) - 601, row_a["id"]))
    con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (int(time.time()), xrp))
    assert "already recorded as closed" in ex.claim_exit(con, row_b)
    fake.stop()


def test_vanished_position_is_not_closed_in_the_books_without_history():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    fake.positions.pop()                                            # not visible, not in history
    try:
        ex.bot_equity(con, client, client.positions(), 102)
        raise AssertionError("equity should be unknown")
    except ex.PnlUnknown:
        pass                                                        # entries blocked, position still bot-owned
    assert con.execute("SELECT closed_at FROM owned WHERE position_id=?", (xrp,)).fetchone()[0] is None
    fake.stop()


def test_position_missing_from_one_snapshot_is_reopened():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (int(time.time()), xrp))   # a bad snapshot
    ex.sync_owned(con, client, client.positions())
    assert xrp in ex.owned_ids(con)                                   # back to bot-owned and monitored
    fake.stop()


def test_existing_positions_use_their_own_inr_rate():
    pos = [dict(id="a", symbol="XRPUSDT", quantity="100", entry_price="1", entry_hedge_rate="130",
                stoploss=dict(price="0.8"))]
    # 100 x 1 x 130 = Rs 13,000 at a 20% stop = Rs 2,600 > 21% of 5,000, even though today's rate says 102
    assert "all stops" in ex.over_loss_budget(pos, {"a"}, 102, 5000, 0, 1, 1)
    assert ex.pos_rate(dict(entry_hedge_rate=""), 102) is None           # unknown rate is never guessed
    no_rate = [dict(pos[0], entry_hedge_rate="")]
    assert "rate unknown" in ex.over_loss_budget(no_rate, {"a"}, 102, 5000, 0, 1, 1)   # fails closed


def test_collective_gate_keeps_original_stop_reserve_and_candidate_limit():
    tmp, fake, client, con, xrp = _one_verified_xrp()
    pos = client.positions()
    entry = float(pos[0]["entry_price"])
    pos[0]["stoploss"]["price"] = str(entry - 0.001)  # trailed close: actual stop risk is now tiny
    con.execute("UPDATE orders SET planned_risk_inr=200 WHERE position_id=?", (xrp,))
    why = ex.over_loss_budget(pos, {xrp}, 102, 5000, 2000, 1, 0.9, "LONG", [-100], 10,
                              con=con, candidate_risk_limit=210)
    assert "all stops" in why                           # 100 realized + 200 reserve + 210 candidate > 500
    assert "exceeds planned" in ex.over_loss_budget([], set(), 102, 5000, 2000, 1, 0.89, "LONG", [], 0,
                                                     con=con, candidate_risk_limit=200)
    fake.stop()


def test_execution_lease_is_fenced():
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    assert ex.claim(con, pid, "t") is None
    assert not ex.take_lease(con, pid)                         # live lease: reconcile cannot take it
    con.execute("UPDATE plans SET lease_until=? WHERE id=?", (int(time.time()) - 1, pid))
    mine = ex.LEASES.pop(pid)
    assert ex.take_lease(con, pid)                             # expired: another process takes over
    ex.LEASES[pid] = mine                                      # ... while the old run thinks it still owns it
    try:
        ex.renew(con, pid)
        raise AssertionError("old run kept going")
    except ex.LeaseLost:
        pass                                                   # the old run stops instead of racing
    fake.stop()


def test_legacy_baseline_marked_untrusted_on_upgrade():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "old.db")
    con = ex.db(path)
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at) VALUES('p','XRP','s1-a',1)")
    con.execute("INSERT INTO ledger(day, start_equity, trusted) VALUES(?, 5000, 1)", (ex.ist_day(),))
    con.execute("DELETE FROM kv WHERE key='ledger_v2'")        # as if written by the old version
    con = ex.db(path)
    assert con.execute("SELECT trusted FROM ledger WHERE day=?", (ex.ist_day(),)).fetchone()[0] == 0


def test_daily_baseline_from_midnight_mark_or_blocked():
    tmp, fake, client, con = setup()
    D, off = 86400, ex.config.IST_OFFSET
    midnight = (int(time.time()) + off) // D * D - off + D           # next IST midnight
    ex.caps_state(con, 5000, 0, now=midnight - 300)                   # watcher mark 5 min before midnight
    c = ex.caps_state(con, 4900, 0, now=midnight + 3600)
    assert c["baseline_ok"] and c["start"] == 5000 and round(c["pnl"]) == -100
    con.execute("INSERT INTO owned(position_id, coin, client_order_id, opened_at) VALUES('p','XRP','s1-z',1)")
    c = ex.caps_state(con, 4600, -400, now=midnight + D + 7200)       # no mark before the next midnight
    assert not c["baseline_ok"]                                       # unknown start: callers block new buys
    assert ex.update_peak(con, 6000) == 6000 and ex.update_peak(con, 5500) == 6000   # peak kept in the DB
    fake.stop()


def test_nothing_to_do_plan_invalidates_older_approvals():
    tmp, fake, client, con = setup()
    old = plan(con, ["XRP"])
    cap = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id="p")], {"reason": "cap"})
    ex.supersede_pending(con, "newer plan: nothing to do")
    assert "already FAILED" in ex.claim(con, old, "late tap") and fake.submits == 0
    assert con.execute("SELECT state FROM plans WHERE id=?", (cap,)).fetchone()[0] == "PLANNED"
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_trader.py")).read()
    assert "ex.supersede_pending(con" in src                       # plan() calls it when nothing is to do
    fake.stop()


def test_cap_close_plan_is_not_superseded_by_an_entry_plan():
    tmp, fake, client, con = setup()
    cap = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id="p")], {"reason": "cap"})
    plan(con, ["ADA"])
    assert con.execute("SELECT state FROM plans WHERE id=?", (cap,)).fetchone()[0] == "PLANNED"
    newer_cap = ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id="p")], {"reason": "cap"})
    assert con.execute("SELECT state FROM plans WHERE id=?", (cap,)).fetchone()[0] == "FAILED" and newer_cap
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
        assert exited(st["XRP"], fake) and "halted" in st["ADA"][1], (rate, st)
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


def test_false_alert_result_is_journaled_without_breaking_execution():
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    final, _ = ex.execute(con, client, pid, "t", NOSLEEP, lambda msg: False)
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
                "VALUES('lost','ADA','s1-y',?,?,-600)", (int(time.time()) - 60, int(time.time()) - 30))
    sent, st = [], {}
    orig = (watcher.notify, watcher.STATUS_PATH, watcher.LOG_PATH, watcher.JOURNAL_PATH, ex.GUARD_PATH)
    watcher.STATUS_PATH, watcher.LOG_PATH, watcher.JOURNAL_PATH = (os.path.join(tmp, n) for n in
                                                                   ("ws.json", "w.log", "j.csv"))
    watcher.notify = lambda msg, buttons=None: sent.append((msg, buttons)) or len(sent) > 2   # first sends fail
    try:
        watcher.check(st, client, con, make_plan=lambda: dict(plan_id=None, orders=[], created_at=time.time()))
        assert any("bracket moved" in m for m, _ in sent)
        assert "cap_day" not in st and st.get("cap_plan")          # cap-close alert not delivered -> retry kept
        cap_pid = st["cap_plan"]["plan_id"]
        watcher.check(st, client, con, make_plan=lambda: dict(plan_id=None, orders=[], created_at=time.time()))
        assert st.get("cap_day") and any(b and f"approve:{cap_pid}" in str(b) for _, b in sent)
        assert con.execute("SELECT COUNT(*) FROM plans WHERE payload LIKE '%cap%'").fetchone()[0] == 1   # no dupes
        assert sum("bracket moved" in m for m, _ in sent) == 2       # undelivered bracket alert was retried
        watcher.check(st, client, con, make_plan=lambda: dict(plan_id=None, orders=[], created_at=time.time()))
        assert sum("bracket moved" in m for m, _ in sent) == 2       # delivered once -> not repeated
        good = fake.positions[0]["stoploss"]["price"] = con.execute(
            "SELECT stop_price FROM orders WHERE plan_id=?", (pid,)).fetchone()[0]
        watcher.check(st, client, con, make_plan=lambda: dict(plan_id=None, orders=[], created_at=time.time()))
        fake.positions[0]["stoploss"]["price"] = "1.0"            # the same problem comes back later
        watcher.check(st, client, con, make_plan=lambda: dict(plan_id=None, orders=[], created_at=time.time()))
        assert good and sum("bracket moved" in m for m, _ in sent) == 3  # re-armed after healthy check
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


def test_client_id_lookup_uses_history_like_live_mudrex():
    """Live 2026-09-27: detail?client_order_id= is 404 even for a FILLED order. The first real plan ended
    RECONCILE_REQUIRED, then reconcile marked the filled XRP order FAILED ('not found'). Lookups must use history."""
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE" and len(fake.positions) == 1
    o = client.order_by_client_id(f"s1-{pid}-0-XRP-O")
    assert o["status"] == "FILLED" and o["future_position_uuid"] == fake.positions[0]["id"]
    assert client.order_by_client_id("never-sent") is None
    client.HISTORY_LIMIT = 1                                     # truncated history: absence proves nothing
    try:
        client.order_by_client_id("never-sent")
        assert False, "truncated history must be Ambiguous"
    except Ambiguous:
        pass
    fake.stop()


def test_open_pnl_uses_live_price_not_entry():
    """Live Mudrex positions carry no mark price: without the asset price, open P&L (and the daily limit) read 0."""
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", NOSLEEP)[0] == "COMPLETE"
    fake.prices["XRP"] *= 0.9                                     # price falls 10% after the fill
    ps = client.positions()
    assert float(ps[0]["mark_price"]) == fake.prices["XRP"]
    assert ex.unrealized_inr(con, ps, 102.0) < 0
    fake.stop()


def test_reconcile_never_fails_a_young_or_acknowledged_order():
    """History may lag a just-sent order: absence only means 'not placed' after RECONCILE_GRACE, and an order
    with a Mudrex order id is resolved by that id, never called 'not placed'."""
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP", "ADA"])
    con.execute("UPDATE plans SET state='RECONCILE_REQUIRED', approved_at=1, approved_by='t' WHERE id=?", (pid,))
    con.execute("UPDATE orders SET state='RECONCILE_REQUIRED', updated_at=? WHERE plan_id=?", (int(time.time()), pid))
    con.execute("UPDATE orders SET exchange_order_id='gone' WHERE plan_id=? AND coin='ADA'", (pid,))
    state = lambda c: con.execute("SELECT state FROM orders WHERE plan_id=? AND coin=?", (pid, c)).fetchone()[0]  # noqa: E731
    ex.reconcile(con, client, NOSLEEP)
    assert state("XRP") == "RECONCILE_REQUIRED" and state("ADA") == "RECONCILE_REQUIRED"
    con.execute("UPDATE orders SET updated_at=? WHERE plan_id=?", (int(time.time()) - ex.RECONCILE_GRACE - 1, pid))
    ex.reconcile(con, client, NOSLEEP)
    assert state("XRP") == "FAILED"                       # old and never seen: really not placed
    assert state("ADA") == "RECONCILE_REQUIRED"           # acknowledged id that 404s: stays open, alerts
    fake.stop()


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__, flush=True)
    print(f"{len(tests)} passed")
