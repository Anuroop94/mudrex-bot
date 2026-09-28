import sqlite3

import live_universe as u


def row(symbol, cls=None, volume=20_000_000):
    r = dict(symbol=symbol, price="1", volume=str(volume), quantity_step="1", min_contract="1",
             min_notional_value="1", price_step="0.01", max_leverage="5")
    if cls is not None:
        r["asset_class"] = cls
    return r


def test_unknown_and_non_crypto_fail_closed():
    s = u.select_rows([row("XRPUSDT"), row("NEWUSDT"), row("AAPLUSDT", "stock"), row("GOLDUSDT", "commodity")])
    assert s.coins == ("XRP",)
    assert set(s.rejected) == {"NEW", "AAPL", "GOLD"}


def test_explicit_crypto_and_all_qualifying_rows_are_kept():
    s = u.select_rows([row("NEWUSDT", "crypto"), row("ETHUSDT"), row("SOLUSDT")])
    assert s.coins == ("NEW", "ETH", "SOL")


def test_snapshot_is_immutable_per_cycle():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE universe_snapshots(cycle TEXT PRIMARY KEY,created_at INTEGER,payload TEXT)")
    first = u.archive(con, "c", u.select_rows([row("XRPUSDT")]), 1)
    second = u.archive(con, "c", u.select_rows([row("ETHUSDT")]), 2)
    assert first == second and first["coins"] == ["XRP"]


def test_cycle_replay_excludes_later_arrivals_and_missing_members():
    snapshot = {"coins": ["BTC", "ETH"], "rejected": {}}
    current = u.Selection((row("SOLUSDT"), row("BTCUSDT")), {})
    replayed = u.replay(current, snapshot)
    assert replayed.coins == ("BTC",)
    assert "ETH" in replayed.rejected


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests: t(); print("ok ", t.__name__)
    print(f"{len(tests)} passed")
