import sqlite3
import time

import bounded_learning as bl
import live_trader as lt


def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""CREATE TABLE owned(position_id TEXT,client_order_id TEXT,closed_at INTEGER,realized_pnl REAL);
        CREATE TABLE orders(client_order_id TEXT,coin TEXT,side TEXT);""")
    return con


def test_shadow_and_sparse_history_are_neutral():
    con = db()
    assert bl.decisions_from_db(con, [("XRP", "LONG")], promoted=True)[("XRP", "LONG")].active is False
    assert bl.decisions_from_db(con, [("XRP", "LONG")], promoted=False)[("XRP", "LONG")].active is False


def test_only_labels_available_before_decision_are_used():
    con, now = db(), int(time.time())
    for i in range(30):
        con.execute("INSERT INTO orders VALUES(?,?,?)", (f"c{i}", "XRP", "LONG"))
        con.execute("INSERT INTO owned VALUES(?,?,?,?)", (f"p{i}", f"c{i}", now - 100 + i, 10))
    con.execute("INSERT INTO orders VALUES('future','XRP','LONG')")
    con.execute("INSERT INTO owned VALUES('future','future',?,?)", (now + 1, -10000))
    d = bl.decisions_from_db(con, [("XRP", "LONG")], now, promoted=True)[("XRP", "LONG")]
    assert d.active and not d.veto and d.score > 0.8


def test_model_score_changes_order_only_not_risk_geometry():
    specs = {c: dict(step=0.1, min_qty=0.1, min_notional=1.0, max_leverage=5) for c in ("XRP", "ADA")}
    args = dict(targets={"XRP": 0.4, "ADA": 0.4}, owned={}, manual_symbols=set(),
                prices={"XRP": 1.0, "ADA": 1.0}, atrs={"XRP": 0.02, "ADA": 0.02}, specs=specs,
                size_equity_inr=5000, armed={}, entries_blocked=None, rate=100, basket=("XRP", "ADA"),
                available_slots=2)
    a = lt.build_orders(**args, model_decisions={
        ("XRP", "LONG"): bl.Decision(0.9, False, True, "rank"),
        ("ADA", "LONG"): bl.Decision(0.1, False, True, "rank")})
    b = lt.build_orders(**args, model_decisions={
        ("XRP", "LONG"): bl.Decision(0.1, False, True, "rank"),
        ("ADA", "LONG"): bl.Decision(0.9, False, True, "rank")})
    normalize = lambda rows: {r["coin"]: (r["qty"], r["leverage"], r["stop_loss"], r["take_profit"],
                                             r["planned_risk_inr"]) for r in rows if r["action"] == "OPEN"}
    assert normalize(a) == normalize(b)
    assert [r["coin"] for r in a if r["action"] == "OPEN"] == ["XRP", "ADA"]
    assert [r["coin"] for r in b if r["action"] == "OPEN"] == ["ADA", "XRP"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests: t(); print("ok ", t.__name__)
    print(f"{len(tests)} passed")
