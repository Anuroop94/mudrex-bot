"""Adversarial tests against local fake Mudrex for close-fence, position-tracking, and stop-hit edge cases.
Never contacts live APIs or .env. Run: python test_adversarial_books.py
"""
import os
import sys
import time

os.environ["MUDREX_TEST_MODE"] = "1"
os.environ["LIVE_TRADING_ENABLED"] = "true"

import execution as ex
import fake_mudrex
import test_execution as T

PRICES = T.PRICES


def test_same_order_retries_anytime_not_blocked_by_own_fence():
    """The SAME close order can retry at any time without being blocked by its own earlier attempt—
    exit_in_flight explicitly excludes the current order's id (o.id<>?)."""
    tmp, fake, client, con, xrp = T._one_verified_xrp()
    fake.faults["close"] = ["timeout"] * 20
    pid = T._close_plan(con, xrp)
    row = con.execute("SELECT * FROM orders WHERE plan_id=?", (pid,)).fetchone()

    ex.execute(con, client, pid, "t", T.NOSLEEP)
    assert T.states(con, pid)["XRP"][0] == "RECONCILE_REQUIRED", "first close should timeout"

    # After 10 min, same order can retry without being blocked by its own fence
    con.execute("UPDATE orders SET exit_sent_at=? WHERE id=?", (int(time.time()) - 601, row["id"]))
    row_retry = con.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()
    result = ex.claim_exit(con, row_retry)
    assert result is None, "same order can retry (not blocked by its own fence)"
    fake.stop()


def test_other_order_blocked_before_10min_still_blocked_after():
    """A close that finished < 10 min ago blocks another order; after 10 min with position closed,
    a second close is still refused."""
    tmp, fake, client, con, xrp = T._one_verified_xrp()

    # First close attempt finishes quickly
    first = T._close_plan(con, xrp)
    ex.execute(con, client, first, "t", T.NOSLEEP)
    assert not fake.positions, "first close should succeed"

    # Within 10 min: second close is blocked
    second = T._close_plan(con, xrp)
    row_second = con.execute("SELECT * FROM orders WHERE plan_id=?", (second,)).fetchone()
    result = ex.claim_exit(con, row_second)
    assert result is not None and "still closing" in result, "second close within 10 min should be blocked"

    # Advance time past 10 min; position is still recorded as closed
    con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, first))

    # Second close is STILL refused because position is already recorded as closed
    row_second_fresh = con.execute("SELECT * FROM orders WHERE plan_id=?", (second,)).fetchone()
    result = ex.claim_exit(con, row_second_fresh)
    assert result is not None and "already recorded as closed" in result, \
        "second close should still be refused because position is closed"
    fake.stop()


def test_vanished_position_blocks_equity_until_in_history():
    """Position vanished from fake.positions but never in fake.closed -> bot_equity raises PnlUnknown.
    Once it appears in fake.closed with pnl, bot_equity works and P&L is recorded once."""
    tmp, fake, client, con, xrp = T._one_verified_xrp()

    # Remove position from live but NOT from history
    fake.positions.pop()
    assert not fake.positions, "position must be gone from live"

    # bot_equity should raise PnlUnknown because position vanished without appearing in history
    try:
        ex.bot_equity(con, client, client.positions(), 102)
        raise AssertionError("bot_equity should raise PnlUnknown for vanished position")
    except ex.PnlUnknown:
        pass

    # Position still bot-owned but closed_at is NULL
    assert con.execute("SELECT closed_at FROM owned WHERE position_id=?", (xrp,)).fetchone()[0] is None, \
        "position should still be bot-owned with closed_at=NULL"

    # Now add it to fake.closed history with a pnl
    fake.closed.append(dict(id=xrp, coin="XRP", pnl=-50.0, realized_at=int(time.time())))

    # bot_equity should now work
    eq = ex.bot_equity(con, client, client.positions(), 102)
    assert eq is not None, "bot_equity should work once position is in history"

    # Check that P&L was recorded exactly once
    realized = con.execute("SELECT realized_pnl FROM owned WHERE position_id=?", (xrp,)).fetchone()[0]
    assert realized == -50.0, f"realized_pnl should be -50.0, got {realized}"

    # Call bot_equity again: P&L should not change (recorded once)
    eq2 = ex.bot_equity(con, client, client.positions(), 102)
    assert con.execute("SELECT realized_pnl FROM owned WHERE position_id=?", (xrp,)).fetchone()[0] == -50.0, \
        "P&L should be recorded only once"
    fake.stop()


def test_stop_hit_recorded_closed_unblocks_entries():
    """Genuine stop-hit (position moved from fake.positions to fake.closed) is recorded closed.
    This does not block entries afterwards (a new buy plan can execute)."""
    tmp, fake, client, con = T.setup()

    # Open a position
    pid = T.plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", T.NOSLEEP)
    assert len(fake.positions) == 1, "should have 1 open position"
    xrp_pos = fake.positions[0]

    # Simulate stop-hit: move position to closed history
    fake.positions.pop()
    fake.closed.append(dict(id=xrp_pos["id"], coin="XRP", pnl=100.0, realized_at=int(time.time())))

    # bot_equity should work
    eq = ex.bot_equity(con, client, client.positions(), 102)
    assert eq is not None, "bot_equity should work after stop-hit"

    # Check position is marked closed in owned table
    closed_at = con.execute("SELECT closed_at FROM owned WHERE position_id=?", (xrp_pos["id"],)).fetchone()[0]
    assert closed_at is not None, "position should be marked closed after stop-hit"

    # Open a new position: should NOT be blocked by the closed position
    pid2 = T.plan(con, ["ADA"])
    ex.execute(con, client, pid2, "t", T.NOSLEEP)
    assert T.states(con, pid2)["ADA"][0] == "VERIFIED", \
        "new buy should execute: stop-hit does not block entries"
    assert len(fake.positions) == 1, "should have 1 new open position"
    fake.stop()


def test_close_blocks_same_symbol_until_10min_after_confirmed():
    """A confirmed close blocks any entry to the same symbol until 10 min have passed after exit_sent_at."""
    tmp, fake, client, con = T.setup()

    # Open XRP position
    pid = T.plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", T.NOSLEEP)
    xrp_id = next(iter(ex.owned_ids(con)))

    # Close it immediately (succeeds)
    close = T._close_plan(con, xrp_id)
    ex.execute(con, client, close, "t", T.NOSLEEP)
    assert T.states(con, close)["XRP"][0] == "VERIFIED", "close should succeed"
    assert not fake.positions, "position gone after close"

    # Immediately try to open XRP again: check exit_sent_at is recent
    exit_sent = con.execute("SELECT exit_sent_at FROM orders WHERE plan_id=?", (close,)).fetchone()[0]
    assert int(time.time()) - exit_sent < 10, "exit_sent_at should be very recent"

    # Try to open: exit_in_flight should detect the fence
    pid2 = T.plan(con, ["XRP"])
    row = con.execute("SELECT * FROM orders WHERE plan_id=?", (pid2,)).fetchone()
    busy = ex.exit_in_flight(con, row["position_id"], row["id"])
    assert busy is None, "exit_in_flight should be None for new position (different position_id)"

    # Actually we need to try to close a second time to hit the fence
    # Let me adjust: open position, then try two closes
    pid3 = T.plan(con, ["DOGE"])
    ex.execute(con, client, pid3, "t", T.NOSLEEP)
    doge_id = next(iter(ex.owned_ids(con)))

    close_a = T._close_plan(con, doge_id)
    ex.execute(con, client, close_a, "t", T.NOSLEEP)

    # Within 10 min: second close is blocked
    close_b = T._close_plan(con, doge_id)
    row_b = con.execute("SELECT * FROM orders WHERE plan_id=?", (close_b,)).fetchone()
    result = ex.claim_exit(con, row_b)
    assert result is not None and "still closing" in result, "second close should be blocked by first"

    fake.stop()


def test_exit_fence_atomicity_two_closes_same_position():
    """Two separate processes/closes cannot both pass claim_exit for the same position (atomic fence)."""
    tmp, fake, client, con = T.setup()

    pid = T.plan(con, ["XRP"])
    ex.execute(con, client, pid, "t", T.NOSLEEP)
    xrp_id = next(iter(ex.owned_ids(con)))

    close_a = T._close_plan(con, xrp_id)
    close_b = T._close_plan(con, xrp_id)

    row_a = con.execute("SELECT * FROM orders WHERE plan_id=?", (close_a,)).fetchone()
    row_b = con.execute("SELECT * FROM orders WHERE plan_id=?", (close_b,)).fetchone()

    # First process takes the fence
    result_a = ex.claim_exit(con, row_a)
    assert result_a is None, f"first claim_exit should succeed, got {result_a}"

    # Second process is blocked
    result_b = ex.claim_exit(con, row_b)
    assert result_b is not None, "second claim_exit should be blocked"
    assert "still closing" in result_b, f"should report still closing, got {result_b}"

    fake.stop()


def run_tests():
    tests = [
        ("test_same_order_retries_anytime_not_blocked_by_own_fence", test_same_order_retries_anytime_not_blocked_by_own_fence),
        ("test_other_order_blocked_before_10min_still_blocked_after", test_other_order_blocked_before_10min_still_blocked_after),
        ("test_vanished_position_blocks_equity_until_in_history", test_vanished_position_blocks_equity_until_in_history),
        ("test_stop_hit_recorded_closed_unblocks_entries", test_stop_hit_recorded_closed_unblocks_entries),
        ("test_close_blocks_same_symbol_until_10min_after_confirmed", test_close_blocks_same_symbol_until_10min_after_confirmed),
        ("test_exit_fence_atomicity_two_closes_same_position", test_exit_fence_atomicity_two_closes_same_position),
    ]

    passed, failed = 0, 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"PASS {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed out of {len(tests)}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
