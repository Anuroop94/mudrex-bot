"""Adversarial tests targeting the 8 critical protective-exit guarantees.
Never contacts live API. Run: python test_adversarial.py
"""
import os
import sys
import tempfile
import threading
import time

os.environ["MUDREX_TEST_MODE"] = "1"
os.environ["LIVE_TRADING_ENABLED"] = "true"

import execution as ex
import fake_mudrex
import trade_policy
from mudrex_client import Client

trade_policy.AUTONOMOUS_HEDGE_READY = True  # explicit in-process fake-exchange test unlock

PRICES = {"XRP": 1.5, "ADA": 0.26, "DOGE": 0.1, "LINK": 14.0, "AVAX": 11.0, "TRX": 0.34}
NOSLEEP = lambda s: None


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


def states(con, pid):
    return {r["coin"]: (r["state"], r["error"]) for r in con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,))}


def close_posts(fake):
    return sum(1 for m, p, c in fake.requests if m == "POST" and p.endswith("/close"))


def _unprotectable(fake):
    fake.riskorder_ok, fake.drop_order_stop = False, True


def test_protective_exit_never_closes_manual_position():
    """A manual position must never be closed by protective exit during entry or protective exit."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    # Add manual positions on different coins
    fake.add_manual("ADA", "LONG")              # manual position
    fake.add_manual("DOGE", "LONG")
    manual_ids = {p["id"] for p in fake.positions}
    # Try to open bot positions on different coins
    pid = plan(con, ["XRP"])  # XRP has no manual position
    ex.execute(con, client, pid, "test", NOSLEEP)
    st = states(con, pid)["XRP"]
    # Bot's position should have exited (unprotectable), but manual positions on ADA/DOGE must remain untouched
    assert st[0] == "FAILED" and "exited automatically" in st[1], st
    # Verify manual positions still exist (were never closed)
    assert len(fake.positions) == 2, f"should have 2 manual positions left, got {len(fake.positions)}"
    for p in fake.positions:
        assert p["id"] in manual_ids, f"manual position {p['id']} should not have been closed"
    fake.stop()


def test_protective_exit_refuses_position_not_owned_by_this_order():
    """A position owned by a DIFFERENT order's client_order_id must not be closed."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    real_close = ex.protective_close

    def change_then_close(con_, client_, row, why, sleep, alert):
        # After verify, rebind the position to a different order
        con_.execute("UPDATE owned SET client_order_id='s1-other-plan-O' WHERE position_id=?",
                     (row["position_id"],))
        return real_close(con_, client_, row, why, sleep, alert)

    ex.protective_close = change_then_close
    try:
        pid1 = plan(con, ["XRP"])
        ex.execute(con, client, pid1, "t", NOSLEEP)
        st = states(con, pid1)["XRP"]
        # Should refuse the close because position is not bound to this order's client_order_id
        assert "not bound" in st[1] or "refused" in st[1], st
        assert close_posts(fake) == 0, "no close should have been sent"
    finally:
        ex.protective_close = real_close
    fake.stop()


def test_close_never_sent_twice_within_10_minutes():
    """Within 10 min of exit_sent_at, close must not be resent even if position still open."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    fake.faults["close"] = ["timeout"] * 10      # every close times out, never applied
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert close_posts(fake) == 1
    # Manually run reconcile right away (well before 10 min)
    ex.reconcile(con, client, NOSLEEP)
    assert close_posts(fake) == 1, "close should NOT be resent within 10 min"
    fake.stop()


def test_close_resent_after_10_minutes_but_max_3_times():
    """After 10 min, close is resent. After 3 attempts total, hand over to user."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    fake.faults["close"] = ["timeout"] * 50     # all timeouts
    alerts = []
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    c1 = close_posts(fake)
    assert c1 == 1, f"first close should be sent, got {c1}"
    # Simulate 10 min elapsed
    con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    c2 = close_posts(fake)
    assert c2 == 2, f"resent after 10 min: expected 2, got {c2}"
    # Another 10 min elapsed
    con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    c3 = close_posts(fake)
    assert c3 == 3, f"resent 3rd time: expected 3, got {c3}"
    # Another 10 min elapsed - but now it should give up
    con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    c4 = close_posts(fake)
    assert c4 == 3, f"should NOT resend 4th time: expected 3, got {c4}"
    assert any("failed 3 times" in a for a in alerts), f"expected 'failed 3 times' in alerts: {alerts}"
    fake.stop()


def test_apply_timeout_on_close_ends_as_exited_not_resent():
    """close applied but then timed out (apply_timeout): must end as FAILED exited, never re-sent."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    fake.faults["close"] = ["apply_timeout"]    # applied but then times out
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    assert close_posts(fake) == 1
    st = states(con, pid)["XRP"]
    # The position should be gone (close was applied), so it should be marked as exited
    assert not fake.positions, "position should have been closed (apply_timeout means it went through)"
    assert st[0] == "FAILED" and "exited automatically" in st[1]
    fake.stop()


def test_close_applied_late_recorded_as_exited_once():
    """Close is sent, marked unconfirmed, then a later check finds it gone: recorded as exited exactly once."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    sent_close = []

    def close_side_effect(o):
        # First send times out
        if not sent_close:
            sent_close.append(True)
            time.sleep(1.5)  # simulate timeout
        # But the position IS actually closed after this

    fake.faults["close"] = ["timeout"]  # timeout but don't apply
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    close1 = close_posts(fake)
    # Position still open because close timed out and was not applied
    assert len(fake.positions) == 1
    st = states(con, pid)["XRP"]
    assert st[0] == "RECONCILE_REQUIRED" and "not confirmed" in st[1]

    # Now simulate the close actually succeeding on the exchange (it also appears in Mudrex's closed history)
    p = fake.positions.pop()
    fake.closed.append(dict(id=p["id"], symbol=p["symbol"], status="CLOSED", pnl="0"))

    # Reconcile should find the position gone
    ex.reconcile(con, client, NOSLEEP)
    close2 = close_posts(fake)
    # After the position disappears, it should be marked as FAILED with exited msg
    st = states(con, pid)["XRP"]
    assert st[0] == "FAILED" and "exited automatically" in st[1], st
    # Verify close was not resent (it was within 10 min and position now gone)
    assert close2 == close1, f"close should not resend when position is already gone: {close1} vs {close2}"
    fake.stop()


def test_reconcile_never_touches_plan_while_lease_held():
    """reconcile() must skip any plan with a live lease (another execute() in progress)."""
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    # Simulate execute() holding a live lease
    ex.claim(con, pid, "executor1")
    # Manually set to RECONCILE_REQUIRED to see if reconcile would try to touch it
    con.execute("UPDATE plans SET state='RECONCILE_REQUIRED' WHERE id=?", (pid,))
    # Now try reconcile
    ex.reconcile(con, client, NOSLEEP)
    # Verify the order was NOT touched (still PLANNED state unmodified by reconcile)
    order = con.execute("SELECT state FROM orders WHERE plan_id=?", (pid,)).fetchone()
    assert order["state"] == "PLANNED", "reconcile should skip leased plans"
    fake.stop()


def test_run_loses_lease_stops_without_modifying_orders():
    """If a run's lease is taken over by reconcile(), it stops (LeaseLost) without touching orders."""
    tmp, fake, client, con = setup()
    pid = plan(con, ["XRP"])
    ex.claim(con, pid, "executor1")  # claim the plan
    old_token = ex.LEASES[pid]

    # Verify we can renew with the right token
    ex.renew(con, pid)  # should succeed

    # Now simulate reconcile taking over (updating with a different token)
    new_token = "different_token"
    con.execute("UPDATE plans SET lease_token=? WHERE id=?", (new_token, pid))

    # Now the old run tries to renew with the old token (which is now invalid)
    ex.LEASES[pid] = old_token  # restore old token to simulate old run's state
    try:
        ex.renew(con, pid)
        raise AssertionError("LeaseLost not raised")
    except ex.LeaseLost:
        pass  # expected

    fake.stop()


def test_protective_exit_runs_with_stop_file_no_opens():
    """Protective exit must run even if STOP file exists. No OPENS must happen during protective exit."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    fake.hooks["after_fill"] = lambda o: open(ex.STOP_PATH, "w").close()  # STOP arrives after fill
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    st = states(con, pid)["XRP"]
    assert st[0] == "FAILED" and "exited automatically" in st[1], "protective exit should run despite STOP file"
    # Verify no POST /futures/order was sent (no OPEN attempted with STOP file)
    opens = sum(1 for m, p, c in fake.requests if m == "POST" and p.endswith("/futures/order"))
    assert opens == 1, f"only the entry should have been sent, got {opens} POST /order requests"
    fake.stop()


def test_unknown_bot_equity_after_fill_leads_to_exit():
    """After fill, if bot equity cannot be determined (missing P&L), position is auto-exited."""
    tmp, fake, client, con = setup()
    # Monkey-patch to make equity check fail only AFTER fill (using a call counter)
    real_bot_equity = ex.bot_equity
    call_count = [0]  # count calls

    def equity_fails_on_check(con_, client, positions, rate):
        call_count[0] += 1
        if call_count[0] >= 2:  # fail on 2nd and later calls (after fill in after_fill_budget)
            raise ex.PnlUnknown("a closed bot position's P&L is not yet visible")
        return real_bot_equity(con_, client, positions, rate)

    ex.bot_equity = equity_fails_on_check
    try:
        pid = plan(con, ["XRP"])
        ex.execute(con, client, pid, "t", NOSLEEP)
        st = states(con, pid)["XRP"]
        # Should exit because bot equity is unknown after the fill
        assert st[0] == "FAILED" and "exited automatically" in st[1], st
        # The error should mention balance unknown
        assert "balance unknown" in st[1], st
    finally:
        ex.bot_equity = real_bot_equity
    fake.stop()


def test_close_timeout_applied_vs_not_applied():
    """apply_timeout on close: applied but then times out -> exited. plain timeout: not applied -> remains open."""
    for fault_type in ("apply_timeout", "timeout"):
        tmp, fake, client, con = setup()
        _unprotectable(fake)
        fake.faults["close"] = [fault_type]
        pid = plan(con, ["XRP"])
        ex.execute(con, client, pid, "t", NOSLEEP)
        st = states(con, pid)["XRP"]

        if fault_type == "apply_timeout":
            # Position was closed (apply_timeout = the close went through)
            assert not fake.positions, "position should be closed"
            assert st[0] == "FAILED" and "exited automatically" in st[1]
        else:  # plain timeout
            # Position still open (timeout = not applied)
            assert fake.positions, "position should still be open"
            assert st[0] == "RECONCILE_REQUIRED" and "not confirmed" in st[1]
        fake.stop()


def test_manual_position_protection_across_reconcile():
    """Manual positions must be protected even across reconcile() calls."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    fake.add_manual("ADA", "LONG")
    fake.add_manual("DOGE", "LONG")
    manual_ids = {p["id"] for p in fake.positions}

    pid = plan(con, ["ADA"])
    ex.execute(con, client, pid, "t", NOSLEEP)

    # After execute, manual positions should remain
    assert {p["id"] for p in fake.positions if p.get("id", "").startswith("manual-")} == manual_ids

    # Reconcile should not touch them either
    ex.reconcile(con, client, NOSLEEP)
    assert {p["id"] for p in fake.positions if p.get("id", "").startswith("manual-")} == manual_ids
    fake.stop()


def test_crash_mid_protective_close_idempotent():
    """Crash mid-protective_close: on restart, it picks up where it left off (idempotent)."""
    tmp, fake, client, con = setup()
    _unprotectable(fake)
    fake.faults["close"] = ["timeout", "timeout", "500"]  # multiple faults to exhaust
    pid = plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", NOSLEEP)
    close1 = close_posts(fake)
    st = states(con, pid)["XRP"]
    assert st[0] == "RECONCILE_REQUIRED" and "not confirmed" in st[1]
    assert close1 == 1

    # Simulate crash: order state remains RECONCILE_REQUIRED
    # Manually advance time beyond 10 min
    con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))

    # Restart reconcile
    ex.reconcile(con, client, NOSLEEP)
    close2 = close_posts(fake)
    assert close2 == 2, "should have resent close after crash"
    fake.stop()


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    passed, failed = 0, 0
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}", flush=True)
            passed += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}", flush=True)
            failed += 1
            failures.append((t.__name__, str(e)))
        except Exception as e:
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}", flush=True)
            failed += 1
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))

    print(f"\n{passed} passed, {failed} failed")
    if failures:
        print("\nFailures:")
        for name, error in failures:
            print(f"  {name}: {error[:200]}")
    sys.exit(0 if failed == 0 else 1)
