"""Pure tests for the owner policy. No network, secrets or runtime state."""
from datetime import datetime, timedelta, timezone

import trade_policy as p


def test_cycle_is_ist_calendar_day():
    # 18:31 UTC is 00:01 on the next day in India.
    assert p.cycle_id(datetime(2026, 9, 27, 18, 31, tzinfo=timezone.utc)) == "2026-09-28"


def test_absolute_daily_pnl_stops():
    assert p.pnl_stop_reason(-499.99) is None
    assert "loss" in p.pnl_stop_reason(-500)
    assert p.pnl_stop_reason(499.99) is None
    assert "profit" in p.pnl_stop_reason(500)


def test_three_sets_is_the_autonomous_maximum():
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    approval = p.SetApproval(p.cycle_id(now), 4, now + timedelta(minutes=5), "proposal-4")
    assert p.new_set_block_reason(0, 0) is None
    assert p.new_set_block_reason(2, 0) is None
    assert "approval" in p.new_set_block_reason(3, 0)
    assert p.new_set_block_reason(3, 0, approval=True, at=now) is not None
    assert p.new_set_block_reason(3, 0, approval=approval, proposal_id="wrong", at=now) is not None
    assert p.new_set_block_reason(3, 0, approval=approval, proposal_id="proposal-4", at=now) is None
    assert p.new_set_block_reason(4, 0, approval=approval, at=now) is not None
    assert p.new_set_block_reason(3, 0, approval=approval, at=approval.expires_at) is not None
    assert "loss" in p.new_set_block_reason(3, -500, approval=approval, proposal_id="proposal-4", at=now)


def test_long_and_short_brackets_are_side_aware():
    assert p.validate_bracket("LONG", 100, 95, 110).side == "LONG"
    assert p.validate_bracket("SHORT", 100, 105, 90).side == "SHORT"
    for args in (("LONG", 100, 105, 90), ("SHORT", 100, 95, 110)):
        try:
            p.validate_bracket(*args)
        except ValueError:
            pass
        else:
            raise AssertionError("wrong-side bracket was accepted")
    for bad in (float("nan"), float("inf"), None):
        try:
            p.validate_bracket("LONG", 100, 95, bad)
        except ValueError:
            pass
        else:
            raise AssertionError("non-finite bracket was accepted")


def test_unknown_daily_pnl_fails_closed():
    assert "unavailable" in p.pnl_stop_reason(float("nan"))
    assert "unavailable" in p.pnl_stop_reason(None)


def test_production_entries_stay_blocked_during_migration():
    # A production environment variable must never bypass this code gate.
    import os
    old_env = os.environ.get("MUDREX_TEST_MODE")
    old_ready = p.AUTONOMOUS_HEDGE_READY
    try:
        os.environ["MUDREX_TEST_MODE"] = "1"
        assert "migration" in p.migration_block_reason()
        p.AUTONOMOUS_HEDGE_READY = True
        assert p.migration_block_reason() is None
    finally:
        p.AUTONOMOUS_HEDGE_READY = old_ready
        if old_env is None:
            os.environ.pop("MUDREX_TEST_MODE", None)
        else:
            os.environ["MUDREX_TEST_MODE"] = old_env


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} policy tests passed")
