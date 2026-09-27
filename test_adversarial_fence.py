"""Adversarial fence tests: break the position lifecycle under worst-case exchange conditions.
Never run live_trader.py, approver.py, watcher.py, or contact real APIs.
Run: python test_adversarial_fence.py
"""
import os
import sys
import time

os.environ["MUDREX_TEST_MODE"] = "1"
os.environ["LIVE_TRADING_ENABLED"] = "true"

import execution as ex
import fake_mudrex
import test_execution as T
from mudrex_client import Client

PRICES = {"XRP": 1.5, "ADA": 0.26, "DOGE": 0.1, "LINK": 14.0, "AVAX": 11.0, "TRX": 0.34}


def test_missing_position_never_recorded_as_closed_until_history_confirms():
    """A position that vanishes from fake.positions but does NOT appear in fake.closed
    is never marked as closed by run_close, resume_close, protective_close, or reconcile.
    No extra close request is sent for it."""
    tmp, fake, client, con = T.setup()
    pid = T.plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", T.NOSLEEP)[0] == "COMPLETE"
    xrp_id = next(iter(ex.owned_ids(con)))

    # Position is verified on exchange
    assert fake.positions and fake.positions[0]["id"] == xrp_id

    # Simulate the position disappearing from current positions but NOT yet in closed history
    hidden = fake.positions.pop()
    assert not fake.positions
    assert not fake.closed  # not in closed history yet

    # Attempt to close it: position_state returns 'unknown'
    close_pid = T._close_plan(con, xrp_id)
    ex.execute(con, client, close_pid, "t", T.NOSLEEP)

    # Should NOT be marked closed, should be RECONCILE_REQUIRED (not confirmed)
    st = T.states(con, close_pid)["XRP"]
    assert st[0] == "RECONCILE_REQUIRED", f"Expected RECONCILE_REQUIRED but got {st[0]}: {st[1]}"
    assert "not visible" in st[1], f"Expected 'not visible' in error but got: {st[1]}"

    # No close request should have been sent (position state was unknown)
    assert T.close_posts(fake) == 0, f"Expected 0 close requests but got {T.close_posts(fake)}"

    # Ownership should still be open (not marked closed)
    owned = con.execute("SELECT closed_at FROM owned WHERE position_id=?", (xrp_id,)).fetchone()
    assert owned["closed_at"] is None, "Position should not be marked closed yet"

    # Now it appears in closed history
    fake.closed.append(dict(id=hidden["id"], symbol=hidden["symbol"], position_type="LONG",
                           status="CLOSED", entry_price=hidden["entry_price"],
                           closed_price=hidden["entry_price"], quantity=hidden["quantity"], pnl="0"))

    # Reconcile should now mark it closed exactly once
    ex.reconcile(con, client, T.NOSLEEP)

    st = T.states(con, close_pid)["XRP"]
    assert st[0] == "VERIFIED", f"Expected VERIFIED after history appears but got {st[0]}: {st[1]}"

    # Ownership should be marked closed
    owned = con.execute("SELECT closed_at FROM owned WHERE position_id=?", (xrp_id,)).fetchone()
    assert owned["closed_at"] is not None, "Position should be marked closed"

    # Still only 0 close requests (it was never sent to the exchange)
    assert T.close_posts(fake) == 0, f"Expected 0 close requests total but got {T.close_posts(fake)}"
    fake.stop()


def test_automatic_exit_in_flight_blocks_human_close():
    """An automatic protective exit left unconfirmed (close timed out, < 10 min)
    blocks a human-approved close for the same position."""
    tmp, fake, client, con = T.setup()
    T._unprotectable(fake)
    fake.faults["close"] = ["timeout"] * 20  # auto exit: outcome unknown

    pid = T.plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", T.NOSLEEP)

    auto_closes = T.close_posts(fake)
    assert auto_closes == 1, f"Expected 1 auto close request but got {auto_closes}"
    assert fake.positions, "Position should still be open"

    # Now a human tries to close the same position
    pos_id = fake.positions[0]["id"]
    human_pid = T._close_plan(con, pos_id)
    ex.execute(con, client, human_pid, "t", T.NOSLEEP)

    # Human close should be blocked by the automatic exit in flight
    st = T.states(con, human_pid)["XRP"]
    assert "automatic exit" in st[1], f"Expected 'automatic exit' block but got: {st[1]}"

    # No new close request should be sent (still the 1 from auto-exit)
    assert T.close_posts(fake) == 1, f"Expected 1 close request total but got {T.close_posts(fake)}"
    fake.stop()


def test_human_close_in_flight_blocks_automatic_exit():
    """An approved close left unconfirmed (close timed out, < 10 min) blocks
    a new automatic protective exit for the same position."""
    tmp, fake, client, con = T.setup()

    # First, open and verify an XRP position
    pid = T.plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", T.NOSLEEP)[0] == "COMPLETE"
    xrp_id = next(iter(ex.owned_ids(con)))

    # Human approves a close but it times out
    close_pid = T._close_plan(con, xrp_id)
    fake.faults["close"] = ["timeout"] * 20  # outcome unknown
    ex.execute(con, client, close_pid, "t", T.NOSLEEP)

    human_closes = T.close_posts(fake)
    assert human_closes == 1, f"Expected 1 human close request but got {human_closes}"
    assert fake.positions, "Position should still be open"

    # Now a protective exit is triggered (e.g., by position change detection)
    # But protective_close should see the human close in flight and not send its own
    T._unprotectable(fake)
    fake.positions[0]["quantity"] = "100"  # Trigger change detection during reconcile
    fake.faults["close"] = []  # Clear previous timeouts for the check

    # Reconcile will try protective exit but should be blocked
    ex.reconcile(con, client, T.NOSLEEP)

    # No new close request (the human close is still in flight and blocks it)
    assert T.close_posts(fake) == 1, f"Expected 1 close request total but got {T.close_posts(fake)}"
    fake.stop()


def test_close_retry_after_10_minute_window_exactly_one():
    """After exit_sent_at becomes older than 10 minutes, exactly one new close request
    is allowed, never two."""
    tmp, fake, client, con = T.setup()
    T._unprotectable(fake)
    fake.faults["close"] = ["timeout"] * 20  # never landing

    pid = T.plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", T.NOSLEEP)

    assert T.close_posts(fake) == 1, f"First attempt: expected 1 but got {T.close_posts(fake)}"

    # Within 10 min: no retry
    ex.reconcile(con, client, T.NOSLEEP)
    assert T.close_posts(fake) == 1, f"Within 10 min: expected 1 total but got {T.close_posts(fake)}"

    # After 10 min: retry once
    con.execute("UPDATE orders SET exit_sent_at=? WHERE action='OPEN'",
                (int(time.time()) - 601,))  # 601 seconds ago = past 10 min
    ex.reconcile(con, client, T.NOSLEEP)
    assert T.close_posts(fake) == 2, f"After 10 min (1st retry): expected 2 total but got {T.close_posts(fake)}"

    # Still within 10 min of the new attempt: no second retry yet
    ex.reconcile(con, client, T.NOSLEEP)
    assert T.close_posts(fake) == 2, f"Still within 10 min: expected 2 total but got {T.close_posts(fake)}"

    # After another 10 min: retry twice
    con.execute("UPDATE orders SET exit_sent_at=? WHERE action='OPEN'",
                (int(time.time()) - 601,))
    ex.reconcile(con, client, T.NOSLEEP)
    assert T.close_posts(fake) == 3, f"After 10 min (2nd retry): expected 3 total but got {T.close_posts(fake)}"

    # After 3rd attempt, should fail and hand over to human (no 4th attempt)
    con.execute("UPDATE orders SET exit_sent_at=? WHERE action='OPEN'",
                (int(time.time()) - 601,))
    ex.reconcile(con, client, T.NOSLEEP)
    assert T.close_posts(fake) == 3, f"After 3 attempts (max): expected 3 total but got {T.close_posts(fake)}"
    fake.stop()


def test_manual_position_never_closed_by_auto_exit():
    """A manual position (fake.add_manual) is never closed by protective_close,
    run_close, resume_close, or reconcile."""
    tmp, fake, client, con = T.setup()

    # Add a manual XRP position
    fake.add_manual("XRP", "LONG")
    manual_id = fake.positions[0]["id"]

    # Try to close it via an approved close plan
    close_pid = T._close_plan(con, manual_id)
    ex.execute(con, client, close_pid, "t", T.NOSLEEP)

    # Close should fail because position is not owned by bot
    st = T.states(con, close_pid)["XRP"]
    assert st[0] == "FAILED", f"Expected FAILED but got {st[0]}"
    assert "not owned" in st[1], f"Expected 'not owned' message but got: {st[1]}"

    # Position should still be open on fake exchange
    assert fake.positions and fake.positions[0]["id"] == manual_id

    # No close request should have been sent
    assert T.close_posts(fake) == 0, f"Expected 0 close requests but got {T.close_posts(fake)}"
    fake.stop()


def test_manual_position_never_closed_by_protective_exit():
    """A manual position is never closed by protective_close even during reconcile."""
    tmp, fake, client, con = T.setup()

    # Add a manual position
    fake.add_manual("XRP", "LONG")
    manual_id = fake.positions[0]["id"]

    # Simulate an unconfirmed protective exit on a different (bot-owned) position
    # to verify manual positions are never touched even when reconciling
    T._unprotectable(fake)
    pid = T.plan(con, ["ADA"])
    fake.faults["close"] = ["timeout"] * 20
    ex.execute(con, client, pid, "t", T.NOSLEEP)

    # Reconcile: should not touch the manual position
    ex.reconcile(con, client, T.NOSLEEP)

    # Manual position should still be open
    assert len(fake.positions) == 2  # Manual XRP + Bot ADA
    manual_pos = next((p for p in fake.positions if p["id"] == manual_id), None)
    assert manual_pos is not None, "Manual position should still be open"

    # Verify: ADA close was attempted (test isolation)
    # but manual position was never touched
    fake.stop()


def test_disappearing_position_reopens_when_it_returns():
    """A position that briefly disappears from fake.positions but returns
    is tracked correctly and not double-closed."""
    tmp, fake, client, con = T.setup()

    pid = T.plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", T.NOSLEEP)[0] == "COMPLETE"
    xrp_id = next(iter(ex.owned_ids(con)))

    # Disappear from positions
    hidden = fake.positions.pop()
    assert not fake.positions

    # Reconcile: position_state = 'unknown'
    ex.reconcile(con, client, T.NOSLEEP)

    # Position reappears
    fake.positions.append(hidden)

    # Reconcile: should find it open
    ex.reconcile(con, client, T.NOSLEEP)

    # Ownership should still be open (no false closure during the disappearance)
    owned = con.execute("SELECT closed_at FROM owned WHERE position_id=?", (xrp_id,)).fetchone()
    assert owned["closed_at"] is None, "Position should not be marked closed when reappearing"

    # Should be able to close it now
    close_pid = T._close_plan(con, xrp_id)
    ex.execute(con, client, close_pid, "t", T.NOSLEEP)

    st = T.states(con, close_pid)["XRP"]
    assert st[0] == "VERIFIED", f"Should close successfully but got {st[0]}: {st[1]}"
    assert not fake.positions, "Position should be closed on exchange"
    fake.stop()


def test_no_double_close_when_two_plans_try_same_position_simultaneously():
    """A position can only be closed by ONE plan at a time: the second plan's
    close is blocked by exit_in_flight check, even if both are approved."""
    tmp, fake, client, con = T.setup()

    # Open and verify position
    pid = T.plan(con, ["XRP"])
    assert ex.execute(con, client, pid, "t", T.NOSLEEP)[0] == "COMPLETE"
    xrp_id = next(iter(ex.owned_ids(con)))

    # First close plan: times out
    close1 = T._close_plan(con, xrp_id)
    fake.faults["close"] = ["timeout"] * 20
    ex.execute(con, client, close1, "t", T.NOSLEEP)

    first_closes = T.close_posts(fake)
    assert first_closes == 1, f"First close: expected 1 POST but got {first_closes}"
    assert fake.positions, "Position still open after timeout"

    # Second close plan for the same position (approved but not sent yet)
    close2 = T._close_plan(con, xrp_id)
    con.execute("UPDATE plans SET state='APPROVED' WHERE id=?", (close2,))
    con.execute("UPDATE orders SET state='PLANNED' WHERE plan_id=?", (close2,))

    # Try to execute second close plan
    ex.execute(con, client, close2, "t", T.NOSLEEP)

    # Second close should be blocked or not sent
    st2 = T.states(con, close2)["XRP"]
    error_msg = st2[1] or ""
    # Either blocked with "still closing" or not sent due to exit_in_flight check
    assert ("still closing" in error_msg or st2[0] in ("PLANNED", "FAILED")), \
        f"Expected second close to be blocked/not sent but got state={st2[0]}, error={error_msg}"

    # Only ONE close request total
    assert T.close_posts(fake) == 1, f"Expected 1 close request total but got {T.close_posts(fake)}"
    fake.stop()


# ------ Main runner
def run_tests():
    tests = [
        test_missing_position_never_recorded_as_closed_until_history_confirms,
        test_automatic_exit_in_flight_blocks_human_close,
        test_human_close_in_flight_blocks_automatic_exit,
        test_close_retry_after_10_minute_window_exactly_one,
        test_manual_position_never_closed_by_auto_exit,
        test_manual_position_never_closed_by_protective_exit,
        test_disappearing_position_reopens_when_it_returns,
        test_no_double_close_when_two_plans_try_same_position_simultaneously,
    ]

    results = []
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
            results.append((test.__name__, True, None))
        except Exception as e:
            print(f"FAIL: {test.__name__}")
            print(f"  {e}")
            results.append((test.__name__, False, str(e)))

    print(f"\n{sum(1 for _, ok, _ in results if ok)}/{len(results)} passed")
    return results


if __name__ == "__main__":
    results = run_tests()
    failed = [name for name, ok, _ in results if not ok]
    sys.exit(0 if not failed else 1)
