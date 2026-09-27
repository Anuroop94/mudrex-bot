"""Adversarial tests for human-approved CLOSE guarantees. Never contacts live API.
Run: python test_adversarial_close.py
"""
import os
import sys
import tempfile
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


def _one_verified_xrp():
    tmp, fake, client, con = setup()
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    return tmp, fake, client, con, next(iter(ex.owned_ids(con)))


def _close_plan(con, pos_id):
    return ex.record_plan(con, "d", [dict(coin="XRP", action="CLOSE", position_id=pos_id)], {})


def test_close_refused_for_manual_position():
    """An approved CLOSE must never be sent for a manual (non-bot-owned) position."""
    tmp, fake, client, con = setup()
    # Create a bot-owned position first
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    bot_xrp = next(iter(ex.owned_ids(con)))

    # Now add a manual position on ADA
    fake.add_manual("ADA", "LONG")
    manual_id = fake.positions[-1]["id"]

    # Try to approve a CLOSE for the manual position
    pid = _close_plan(con, manual_id)
    ex.execute(con, client, pid, "t", NOSLEEP)

    # Verify close was NOT sent and order is FAILED
    st = states(con, pid)["XRP"]
    assert st[0] == "FAILED" and "not owned" in st[1], st
    assert close_posts(fake) == 0
    fake.stop()


def test_close_refused_if_position_quantity_changed():
    """An approved CLOSE must be refused if position quantity changed since opening."""
    tmp, fake, client, con, xrp = _one_verified_xrp()
    pid = _close_plan(con, xrp)

    # Change the position quantity in the exchange
    fake.positions[0]["quantity"] = "99"

    ex.execute(con, client, pid, "t", NOSLEEP)
    st = states(con, pid)["XRP"]
    assert st[0] == "FAILED" and "position changed" in st[1], st
    assert close_posts(fake) == 0
    fake.stop()


def test_close_refused_if_another_close_unresolved():
    """No second CLOSE while another CLOSE for same position is unresolved."""
    tmp, fake, client, con, xrp = _one_verified_xrp()

    # Create first close plan
    first = _close_plan(con, xrp)
    fake.faults["close"] = ["timeout"] * 20
    ex.execute(con, client, first, "t", NOSLEEP)
    assert states(con, first)["XRP"][0] == "RECONCILE_REQUIRED"
    assert close_posts(fake) == 1

    # Try second close on same position
    second = _close_plan(con, xrp)
    ex.execute(con, client, second, "t", NOSLEEP)

    # Second close must be refused
    st = states(con, second)["XRP"]
    assert st[0] == "FAILED" and "still closing" in st[1], st
    assert close_posts(fake) == 1  # no additional close sent
    fake.stop()


def test_close_never_sent_within_10_minutes_of_previous():
    """An approved CLOSE never sent twice within 10 minutes."""
    tmp, fake, client, con, xrp = _one_verified_xrp()
    pid = _close_plan(con, xrp)

    fake.faults["close"] = ["timeout"] * 50
    alerts = []
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    c1 = close_posts(fake)
    assert c1 == 1

    # Immediately call reconcile (well before 10 min)
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    c2 = close_posts(fake)
    assert c2 == 1, f"close must not resend within 10 min: {c2} posts"
    fake.stop()


def test_close_resent_after_10min_but_max_3_times():
    """After 10 min gap, CLOSE resent. After 3 attempts, handover to human."""
    tmp, fake, client, con, xrp = _one_verified_xrp()
    pid = _close_plan(con, xrp)

    fake.faults["close"] = ["timeout"] * 50
    alerts = []
    ex.execute(con, client, pid, "t", NOSLEEP, alerts.append)
    c1 = close_posts(fake)
    assert c1 == 1

    for attempt in (2, 3):
        con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))
        ex.reconcile(con, client, NOSLEEP, alerts.append)
        c = close_posts(fake)
        assert c == attempt, f"attempt {attempt}: expected {c} posts, got {c}"

    # 4th attempt: should NOT resend (max 3)
    con.execute("UPDATE orders SET exit_sent_at=? WHERE plan_id=?", (int(time.time()) - 601, pid))
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    c4 = close_posts(fake)
    assert c4 == 3, "should NOT resend 4th time"
    assert any("failed 3 times" in a for a in alerts)
    fake.stop()


def test_crashed_close_finished_by_reconcile_then_verified():
    """A crashed approved CLOSE (order PLANNED) is finished by reconcile(), then VERIFIED."""
    tmp, fake, client, con, xrp = _one_verified_xrp()
    pid = _close_plan(con, xrp)

    # Simulate crash: state = APPROVED (approved but never sent)
    con.execute("UPDATE plans SET state=?, approved_by=?, approved_at=? WHERE id=?",
                ("APPROVED", "test", int(time.time()) - 3600, pid))

    alerts = []
    ex.reconcile(con, client, NOSLEEP, alerts.append)

    # First reconcile sends the close (state = SUBMITTED)
    st = states(con, pid)["XRP"]
    assert st[0] == "SUBMITTED", f"after first reconcile, close should be sent: {st}"
    assert close_posts(fake) == 1

    # Second reconcile finds position gone, sets VERIFIED
    ex.reconcile(con, client, NOSLEEP, alerts.append)
    st = states(con, pid)["XRP"]
    assert st[0] == "VERIFIED" and not fake.positions, f"after second reconcile, should be verified: {st}"
    fake.stop()


def test_stop_file_pauses_approved_close():
    """STOP file present: reconcile() pauses an approved CLOSE (does not send it)."""
    tmp, fake, client, con, xrp = _one_verified_xrp()
    pid = _close_plan(con, xrp)

    # Simulate crash: state = APPROVED (approved but never sent)
    con.execute("UPDATE plans SET state=?, approved_by=?, approved_at=? WHERE id=?",
                ("APPROVED", "test", int(time.time()) - 3600, pid))

    # Create STOP file
    open(ex.STOP_PATH, "w").close()

    ex.reconcile(con, client, NOSLEEP)

    st = states(con, pid)["XRP"]
    assert st[0] == "RECONCILE_REQUIRED" and "paused" in st[1], st
    assert close_posts(fake) == 0  # NOT sent while STOP exists

    # Remove STOP, reconcile again
    os.remove(ex.STOP_PATH)
    ex.reconcile(con, client, NOSLEEP)

    # First reconcile after STOP removed sends the close (SUBMITTED)
    st = states(con, pid)["XRP"]
    assert st[0] == "SUBMITTED", st
    assert close_posts(fake) == 1

    # Second reconcile finds position gone, sets VERIFIED
    ex.reconcile(con, client, NOSLEEP)
    st = states(con, pid)["XRP"]
    assert st[0] == "VERIFIED" and not fake.positions, st
    fake.stop()


def test_concurrent_reconcile_never_both_act_on_same_plan():
    """Two reconcile() calls (one holding a lease manually): never both act on same plan."""
    tmp, fake, client, con = setup()

    # Open a bot position
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    xrp = next(iter(ex.owned_ids(con)))

    # Create a close plan in RECONCILE_REQUIRED state
    pid = _close_plan(con, xrp)
    con.execute("UPDATE plans SET state=?, approved_at=? WHERE id=?",
                ("RECONCILE_REQUIRED", int(time.time()) - 3600, pid))

    # Manually take a lease for this plan (simulating another process)
    taken = ex.take_lease(con, pid)
    assert taken, "should take lease"

    # Now call reconcile (which should skip this plan because lease is held)
    before = states(con, pid)["XRP"][0]
    ex.reconcile(con, client, NOSLEEP)
    after = states(con, pid)["XRP"][0]

    # The order state should NOT have changed (reconcile skipped the plan due to lease)
    assert before == after == "PLANNED", f"reconcile should skip leased plan: before={before}, after={after}"
    assert close_posts(fake) == 0  # No close sent

    fake.stop()


def test_untrusted_mark_never_becomes_baseline():
    """A mark written with trusted=False never becomes the next day's baseline."""
    tmp, fake, client, con = setup()

    # Open a position to have an open bot position
    assert ex.execute(con, client, plan(con, ["XRP"]), "t", NOSLEEP)[0] == "COMPLETE"
    xrp = next(iter(ex.owned_ids(con)))

    D, off = 86400, ex.config.IST_OFFSET
    midnight = (int(time.time()) + off) // D * D - off + D

    # Record an untrusted mark (P&L unconfirmed) just before midnight
    ex.caps_state(con, 6000, 0, now=midnight - 300, trusted=False)

    # After midnight, check if the baseline is OK
    c = ex.caps_state(con, 5000, 0, now=midnight + 3600)

    # The baseline_ok should be False because the only mark was untrusted
    assert not c["baseline_ok"], f"untrusted mark should not become baseline: {c}"

    fake.stop()


def test_close_refused_if_position_leverage_changed():
    """An approved CLOSE is refused if position leverage changed."""
    tmp, fake, client, con, xrp = _one_verified_xrp()
    pid = _close_plan(con, xrp)

    # Change position leverage
    fake.positions[0]["leverage"] = "10"

    ex.execute(con, client, pid, "t", NOSLEEP)
    st = states(con, pid)["XRP"]
    assert st[0] == "FAILED" and "position changed" in st[1], st
    assert close_posts(fake) == 0
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
