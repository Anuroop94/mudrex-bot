"""Local fake Mudrex futures server for integration tests. Never contacts the real API.

Fault injection: server.faults[key] = [action, ...] consumed one per matching request, key in
  "order" (create order), "riskorder", "close", "detail", "positions".
Actions: "timeout" (hang longer than the client timeout, NOT applied), "apply_timeout" (apply, then hang),
         "500" (not applied), "apply_500" (applied, then 500), "423" (not applied).
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
        self.specs = {}
        for c, p in self.prices.items():
            self.specs[c] = dict(quantity_step="0.1", min_contract="0.1", min_notional_value="5",
                                 price_step="0.0001", price=str(p))
        self.balance = balance
        self.positions = []          # open positions (dicts in Mudrex shape)
        self.closed = []             # positions history
        self.orders = {}             # client_order_id -> order
        self.faults = {}
        self.fill_price = {}         # coin -> forced fill price (gap simulation)
        self.riskorder_ok = True
        self.drop_order_stop = False
        self.submits = 0
        self.hooks = {}              # "after_fill" -> fn(order)
        self.lock = threading.Lock()
        seed = dict(id="seed", client_order_id="manual-1", status="FILLED", symbol="XRPUSDT", hedge_rate="102",
                    created_at="2026-09-01T00:00:00Z", future_position_uuid="old")
        self.orders["manual-1"] = seed
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
                    quantity=str(qty), entry_price=str(px), leverage=str(lev), liquidation_price=str(liq),
                    entry_hedge_rate="102", created_at="2026-09-27T00:00:00Z",
                    stoploss=dict(price=str(stop) if stop else "0"), status="OPEN")

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
            coin = symbol.removesuffix("USDT")
            px = self.fill_price.get(coin, self.prices[coin])
            qty = float(body["quantity"])
            stop = float(body["stoploss_price"]) if body.get("is_stoploss") else None
            if stop is not None and (stop >= px or self.drop_order_stop):
                stop = None                   # invalid stop not kept / simulated: exchange dropped the stop
            pos = self._position(coin, "LONG", qty, px, stop=stop)
            self.positions.append(pos)
            oid = str(uuid.uuid4())
            self.orders[cid] = dict(id=oid, client_order_id=cid, status="FILLED", symbol=symbol, hedge_rate="102",
                                    filled_price=str(px), filled_quantity=str(qty), future_position_uuid=pos["id"],
                                    created_at="2026-09-27T00:00:00Z")
        if "after_fill" in self.hooks:
            self.hooks["after_fill"](self.orders[cid])
        return 202, {"success": True, "data": {"order_id": oid, "status": "CREATED", "client_order_id": cid}}

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

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
                p = u.path
                if p.endswith("/futures/funds"):
                    return self._send(200, {"success": True, "data": {"balance": str(fake.balance),
                                                                       "locked_amount": "0"}})
                if p.endswith("/futures/positions"):
                    return self._apply("positions", lambda: (200, {"success": True, "data": fake.positions}))
                if p.endswith("/futures/orders/detail"):
                    def detail():
                        o = fake.orders.get(q.get("client_order_id"))
                        return (200, {"success": True, "data": o}) if o else \
                            (404, {"success": False, "errors": [{"text": "order not found"}]})
                    return self._apply("detail", detail)
                if p.endswith("/futures/orders/history"):
                    return self._send(200, {"success": True, "data": list(fake.orders.values())})
                if p.endswith("/futures/positions/history"):
                    return self._send(200, {"success": True, "data": fake.closed})
                sym = p.rsplit("/", 1)[-1]
                coin = sym.removesuffix("USDT")
                if coin in fake.specs:
                    return self._send(200, {"success": True, "data": dict(fake.specs[coin],
                                                                           price=str(fake.prices[coin]))})
                return self._send(404, {"success": False, "errors": [{"text": "not found"}]})

            def do_POST(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                p = u.path
                if p.endswith("/futures/order"):
                    return self._apply("order", lambda: fake.create_order(q["symbol"], body))
                if p.endswith("/leverage"):
                    return self._send(200, {"success": True, "data": {"leverage": body.get("leverage")}})
                if p.endswith("/riskorder"):
                    pid = p.split("/")[-2]

                    def risk():
                        pos = next((x for x in fake.positions if x["id"] == pid), None)
                        if pos is None:
                            return 404, {"success": False, "errors": [{"text": "Position not found"}]}
                        if not fake.riskorder_ok or float(body["stoploss_price"]) <= float(pos["liquidation_price"]):
                            return 400, {"success": False, "errors": [{"text": "invalid stop"}]}
                        pos["stoploss"] = dict(price=body["stoploss_price"])
                        return 200, {"success": True, "data": {"position_id": pid, "status": "CREATED"}}
                    return self._apply("riskorder", risk)
                if p.endswith("/close"):
                    pid = p.split("/")[-2]

                    def close():
                        pos = next((x for x in fake.positions if x["id"] == pid), None)
                        if pos is None:
                            return 404, {"success": False, "errors": [{"text": "Position not found"}]}
                        fake.positions.remove(pos)
                        fake.closed.append(dict(pos, pnl="0", closed_price=pos["entry_price"]))
                        return 200, {"success": True, "data": {"position_id": pid, "status": "CREATED"}}
                    return self._apply("close", close)
                return self._send(404, {"success": False})
        return H
