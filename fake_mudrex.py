"""Local fake Mudrex futures server for integration tests. Never contacts the real API.

Realism: orders are accepted as CREATED (202) and only FILL after `fill_after` detail lookups (async);
history honours `limit` only (no pagination, like the documented API); a stop is attached with POST only when
none exists, and amended only with PATCH + its stoploss_order_id; leverage is stored and readable.
Every request is appended to `requests` as (method, path, client_order_id or None) for ordering assertions.

Fault injection: server.faults[key] = [action, ...] consumed one per matching request, key in
  "order", "riskorder", "close", "detail", "positions", "leverage_get".
Actions: "timeout" (hang, NOT applied), "apply_timeout" (apply, then hang), "500" (not applied),
         "apply_500" (applied, then 500), "423" (not applied).
"""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HANG = 1.5


class FakeMudrex:
    def __init__(self, prices=None, balance=5000.0):
        self.prices = dict(prices or {})     # own copy: tests move the "live" price without touching the plan
        self.specs = {c: dict(quantity_step="0.1", min_contract="0.1", min_notional_value="5", price_step="0.0001")
                      for c in self.prices}
        self.balance = balance
        self.positions, self.closed, self.orders = [], [], {}
        self.faults, self.fill_price, self.hooks, self.requests = {}, {}, {}, []
        self.riskorder_ok = True
        self.drop_order_stop = False
        self.no_liq = False
        self.fill_after = 1                  # detail lookups before a CREATED order becomes FILLED
        self.never_fill = False
        self.cid_detail = False              # live Mudrex: /orders/detail?client_order_id= is always 404
        self.leverage_store = {}
        self.leverage_stuck = None           # if set, POST leverage is ignored and this value is reported
        self.margin_type = "ISOLATED"        # reported margin type (tests: None, "isolated", "CROSS")
        self.qty_skew = 1.0                  # position quantity = filled quantity * qty_skew
        self.applied_rate = "102"            # hedge_rate reported on filled orders ("" = missing)
        self.bad_currency = []               # writes that arrived without trade_currency=INR
        self.submits = 0
        self.lock = threading.Lock()
        self.orders["manual-1"] = dict(id="seed", client_order_id="manual-1", status="FILLED", symbol="XRPUSDT",
                                       hedge_rate="102", created_at=_iso(time.time() - 3600),
                                       future_position_uuid="old")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    def add_manual(self, coin, side, qty=10.0):
        self.positions.append(self._position(coin, side, qty, self.prices[coin], manual=True))

    def _position(self, coin, side, qty, px, lev=2, stop=None, manual=False):
        liq = px * (1 - 0.9 / lev) if side == "LONG" else px * (1 + 0.9 / lev)
        return dict(id=("manual-" if manual else "") + str(uuid.uuid4()), symbol=coin + "USDT", order_type=side,
                    quantity=str(qty), entry_price=str(px), leverage=str(lev), trade_currency="INR",
                    liquidation_price="" if self.no_liq else str(liq), entry_hedge_rate="102",
                    created_at=_iso(time.time()),
                    stoploss=dict(price=str(stop), order_id=str(uuid.uuid4())) if stop else dict(price="0"),
                    status="OPEN")

    def _fault(self, key):
        with self.lock:
            q = self.faults.get(key)
            return q.pop(0) if q else None

    # ---------- behaviour
    def create_order(self, symbol, body):
        cid = body["client_order_id"]
        with self.lock:
            self.submits += 1
            if cid in self.orders:
                return 409, {"success": False, "errors": [{"code": 409, "text": "client order id already exists"}]}
            oid = str(uuid.uuid4())
            self.orders[cid] = dict(id=oid, client_order_id=cid, status="CREATED", symbol=symbol, hedge_rate="102",
                                    quantity=body["quantity"], created_at=_iso(time.time()), _body=body, _polls=0)
        return 202, {"success": True, "data": {"order_id": oid, "status": "CREATED", "client_order_id": cid}}

    def _maybe_fill(self, o):
        if o.get("status") != "CREATED" or self.never_fill:
            return
        o["_polls"] += 1
        if o["_polls"] < self.fill_after:
            return
        body, coin = o["_body"], o["symbol"].removesuffix("USDT")
        px = self.fill_price.get(coin, self.prices[coin])
        stop = float(body["stoploss_price"]) if body.get("is_stoploss") else None
        if stop is not None and (stop >= px or self.drop_order_stop):
            stop = None
        pos = self._position(coin, "LONG", float(body["quantity"]) * self.qty_skew, px, stop=stop)
        self.positions.append(pos)
        o.update(status="FILLED", filled_price=str(px), filled_quantity=body["quantity"],
                 future_position_uuid=pos["id"], hedge_rate=self.applied_rate)
        if "after_fill" in self.hooks:
            self.hooks["after_fill"](o)

    def public(self, o):
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ConnectionError, OSError):
                    pass                      # client already gave up (simulated timeout)

            def _apply(self, key, fn):
                f = fake._fault(key)
                if f == "timeout":
                    time.sleep(HANG)
                    return self._send(504, {"success": False})
                if f == "500":
                    return self._send(500, {"success": False, "errors": [{"text": "internal"}]})
                if f == "423":
                    return self._send(423, {"success": False, "errors": [{"text": "locked"}]})
                code, obj = fn()
                if f == "apply_timeout":
                    time.sleep(HANG)
                    return self._send(504, {"success": False})
                if f == "apply_500":
                    return self._send(500, {"success": False, "errors": [{"text": "internal"}]})
                return self._send(code, obj)

            def _parts(self):
                u = urlparse(self.path)
                return u.path, {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                p, q = self._parts()
                fake.requests.append(("GET", p, q.get("client_order_id")))
                ok = lambda d: (200, {"success": True, "data": d})     # noqa: E731
                if p.endswith("/futures/funds"):
                    return self._send(*ok({"balance": str(fake.balance), "locked_amount": "0"}))
                if p.endswith("/futures/positions"):
                    return self._apply("positions", lambda: ok(fake.positions))
                if p.endswith("/futures/orders/detail"):
                    def detail():
                        o = fake.orders.get(q.get("client_order_id")) if fake.cid_detail else \
                            next((o for o in fake.orders.values() if o["id"] == q.get("order_id")), None)
                        if not o:
                            return 404, {"success": False, "errors": [{"text": "order not found"}]}
                        fake._maybe_fill(o)
                        return ok(fake.public(o))
                    return self._apply("detail", detail)
                limit = int(q.get("limit", 20))
                if p.endswith("/futures/orders/history"):
                    def hist():
                        rows = sorted(fake.orders.values(), key=lambda o: o["created_at"], reverse=True)[:limit]
                        for o in rows:
                            fake._maybe_fill(o)
                        return ok([fake.public(o) for o in rows])
                    return self._apply("history", hist)
                if p.endswith("/futures/positions/history"):
                    return self._send(*ok(list(reversed(fake.closed))[:limit]))
                if p.endswith("/leverage"):
                    sym = p.split("/")[-2]

                    def lev():
                        v = fake.leverage_stuck if fake.leverage_stuck is not None else fake.leverage_store.get(sym)
                        return (404, {"success": False, "errors": [{"text": "leverage not found"}]}) if v is None \
                            else ok({"margin_type": fake.margin_type, "leverage": str(v)})
                    return self._apply("leverage_get", lev)
                coin = p.rsplit("/", 1)[-1].removesuffix("USDT")
                if coin in fake.specs:
                    return self._send(*ok(dict(fake.specs[coin], price=str(fake.prices[coin]))))
                return self._send(404, {"success": False, "errors": [{"text": "not found"}]})

            def _currency_ok(self, body, p):
                if body.get("trade_currency") != "INR":        # real API would silently treat it as USDT
                    fake.bad_currency.append(p)
                    self._send(400, {"success": False, "errors": [{"text": "position not found in USDT"}]})
                    return False
                return True

            def do_POST(self):
                p, q = self._parts()
                body = self._body()
                fake.requests.append(("POST", p, body.get("client_order_id")))
                if not self._currency_ok(body, p):
                    return None
                if p.endswith("/futures/order"):
                    return self._apply("order", lambda: fake.create_order(q["symbol"], body))
                if p.endswith("/leverage"):
                    def setlev():
                        fake.leverage_store[p.split("/")[-2]] = body.get("leverage")
                        if "on_leverage" in fake.hooks:
                            fake.hooks["on_leverage"]()
                        return 200, {"success": True, "data": {"leverage": body.get("leverage")}}
                    return self._apply("leverage_post", setlev)
                if p.endswith("/riskorder"):
                    pid = p.split("/")[-2]

                    def risk():
                        pos = next((x for x in fake.positions if x["id"] == pid), None)
                        if pos is None:
                            return 404, {"success": False, "errors": [{"text": "Position not found"}]}
                        if float(pos["stoploss"].get("price") or 0) > 0:
                            return 400, {"success": False, "errors": [{"text": "stop-loss already exists"}]}
                        if not fake.riskorder_ok or float(body["stoploss_price"]) <= float(pos["liquidation_price"] or 0):
                            return 400, {"success": False, "errors": [{"text": "invalid stop"}]}
                        pos["stoploss"] = dict(price=body["stoploss_price"], order_id=str(uuid.uuid4()))
                        return 200, {"success": True, "data": {"position_id": pid, "status": "CREATED"}}
                    return self._apply("riskorder", risk)
                if p.endswith("/close"):
                    pid = p.split("/")[-2]

                    def close():
                        pos = next((x for x in fake.positions if x["id"] == pid), None)
                        if pos is None:
                            return 404, {"success": False, "errors": [{"text": "Position not found"}]}
                        fake.positions.remove(pos)
                        fake.closed.append(dict(id=pos["id"], symbol=pos["symbol"], position_type="LONG",
                                                status="CLOSED", entry_price=pos["entry_price"],
                                                closed_price=pos["entry_price"], quantity=pos["quantity"], pnl="0"))
                        return 200, {"success": True, "data": {"position_id": pid, "status": "CREATED"}}
                    return self._apply("close", close)
                return self._send(404, {"success": False})

            def do_PATCH(self):
                p, _ = self._parts()
                body = self._body()
                fake.requests.append(("PATCH", p, None))
                if not self._currency_ok(body, p):
                    return None
                if p.endswith("/riskorder"):
                    pid = p.split("/")[-2]

                    def amend():
                        pos = next((x for x in fake.positions if x["id"] == pid), None)
                        if pos is None:
                            return 404, {"success": False, "errors": [{"text": "Position not found"}]}
                        if body.get("stoploss_order_id") != pos["stoploss"].get("order_id"):
                            return 400, {"success": False, "errors": [{"text": "risk order id missing"}]}
                        if not fake.riskorder_ok or float(body["stoploss_price"]) <= float(pos["liquidation_price"] or 0):
                            return 400, {"success": False, "errors": [{"text": "invalid stop"}]}
                        pos["stoploss"]["price"] = body["stoploss_price"]
                        return 200, {"success": True, "data": {"message": "Risk order amended successfully"}}
                    return self._apply("riskorder", amend)
                return self._send(404, {"success": False})
        return H


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
