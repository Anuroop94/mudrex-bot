"""Mudrex futures REST client with explicit outcome classes. No secrets are ever logged or put in URLs.

Outcomes:
  - returns data            : request succeeded
  - Rejected(status, errors): the exchange definitively refused (4xx except 408/409/423/429)
  - Locked                  : 423/429 - busy; caller backs off and LOOKS UP before any retry
  - Ambiguous               : timeout, transport error or 5xx - the request MAY have been applied.
                              Callers must reconcile (look up by client_order_id), never blindly resubmit.
Only GETs are retried automatically (they are safe). POSTs are never retried here.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import config  # noqa: F401  (loads .env)

BASE = os.environ.get("MUDREX_BASE_URL", "https://trade.mudrex.com/fapi")
TIMEOUT = 15


class ApiError(Exception):
    def __init__(self, status, errors):
        super().__init__(f"HTTP {status}: {errors}")
        self.status, self.errors = status, errors


class Rejected(ApiError):
    pass


class Locked(ApiError):
    pass


class Ambiguous(ApiError):
    pass


class Client:
    def __init__(self, base=None, secret=None, timeout=TIMEOUT, sleep=time.sleep):
        self.base = base or BASE
        self.secret = secret if secret is not None else os.environ.get("MUDREX_API_SECRET", "")
        self.timeout, self.sleep = timeout, sleep

    # ---------- transport
    def _raw(self, method, path, params=None, body=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Authentication": self.secret, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                errs = json.loads(e.read()).get("errors")
            except ValueError:
                errs = None
            if e.code in (423, 429):
                raise Locked(e.code, errs)
            if e.code >= 500 or e.code == 408:
                raise Ambiguous(e.code, errs)
            raise Rejected(e.code, errs)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise Ambiguous(0, f"{type(e).__name__}")
        except ValueError:
            raise Ambiguous(0, "invalid JSON response")
        if not payload.get("success", False):
            raise Rejected(200, payload.get("errors"))
        return payload.get("data")

    def get(self, path, params=None, tries=4):
        """GETs are idempotent: retry transport/5xx/423/429 with bounded exponential backoff."""
        for i in range(tries):
            try:
                return self._raw("GET", path, params)
            except (Ambiguous, Locked):
                if i == tries - 1:
                    raise
                self.sleep(min(2 ** i, 8))

    def post(self, path, body):
        return self._raw("POST", path, body=body)

    # ---------- reads
    def funds(self):
        return self.get("/v1/futures/funds", {"trade_currency": "INR"})

    def positions(self):
        """Open INR positions. Live Mudrex omits any mark price, so each gets `mark_price` from the asset's live
        `price` (one quick GET, no retries). If that lookup fails it stays unset and callers fall back to entry."""
        ps = self.get("/v1/futures/positions", {"trade_currency": "INR"}) or []
        for p in ps:
            if not p.get("mark_price"):
                try:
                    p["mark_price"] = (self.get(f"/v1/futures/{p['symbol']}", {"is_symbol": ""}, tries=1) or {}).get("price")
                except ApiError:
                    pass
        return ps

    def asset(self, symbol):
        return self.get(f"/v1/futures/{symbol}", {"is_symbol": ""})

    def order_by_client_id(self, cid):
        """Order detail, or None if Mudrex has no order with this client_order_id.
        Live Mudrex (2026-09-27) answers 404 on /orders/detail?client_order_id= even for a FILLED order: detail
        only resolves by order_id. So a 404 falls back to the INR order history, whose rows carry client_order_id
        and the same fields (status, filled_price, filled_quantity, future_position_uuid). Absence from a
        truncated history is NOT proof: raise Ambiguous so callers reconcile instead of treating it as unplaced."""
        try:
            return self.get("/v1/futures/orders/detail", {"client_order_id": cid})
        except Rejected as e:
            if e.status != 404:
                raise
        rows, truncated = self.history("orders")
        for r in rows:
            if r.get("client_order_id") == cid:
                return r
        if truncated:
            raise Ambiguous(0, f"{cid} not in the last {self.HISTORY_LIMIT} orders (history truncated)")
        return None

    def order_by_id(self, order_id):
        """Order detail by Mudrex's own order id (the only key /orders/detail resolves). A 404 raises Rejected:
        an order Mudrex acknowledged is never treated as 'not placed'."""
        return self.get("/v1/futures/orders/detail", {"order_id": order_id})

    HISTORY_LIMIT = 500

    def history(self, kind):
        """INR 'orders' or 'positions' history. Mudrex documents only `limit` (no cursor/offset), so this returns
        (rows, truncated). truncated=True means older rows may be missing: callers must not treat absence as fact."""
        rows = self.get(f"/v1/futures/{kind}/history", {"trade_currency": "INR", "limit": self.HISTORY_LIMIT}) or []
        return rows, len(rows) >= self.HISTORY_LIMIT

    def leverage(self, symbol):
        """Saved (leverage, margin_type) for symbol in INR, or None if not configured (404)."""
        try:
            d = self.get(f"/v1/futures/{symbol}/leverage", {"is_symbol": "", "trade_currency": "INR"})
        except Rejected as e:
            if e.status == 404:
                return None
            raise
        return float(d["leverage"]), d.get("margin_type")

    # ---------- writes (never auto-retried)
    def set_leverage(self, symbol, lev):
        return self.post(f"/v1/futures/{symbol}/leverage?is_symbol",
                         {"margin_type": "ISOLATED", "leverage": str(lev), "trade_currency": "INR"})

    def place_market(self, symbol, qty, cid, side, stop=None, target=None):
        """Place a market LONG or SHORT with optional atomic exchange bracket fields."""
        side = str(side).upper()
        if side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        body = {"trigger_type": "MARKET", "order_type": side, "quantity": qty, "trade_currency": "INR",
                "client_order_id": cid}
        if stop is not None:
            body.update(is_stoploss=True, stoploss_price=stop)
        if target is not None:
            body.update(is_takeprofit=True, takeprofit_price=target)
        return self.post(f"/v2/futures/order?symbol={symbol}", body)

    def place_market_long(self, symbol, qty, cid, stop=None):
        """Compatibility wrapper for existing LONG callers."""
        return self.place_market(symbol, qty, cid, "LONG", stop=stop)

    # Every write names trade_currency=INR explicitly: Mudrex defaults POST/PATCH bodies to USDT when omitted.
    def close_position(self, position_id):
        return self.post(f"/v1/futures/positions/{position_id}/close", {"trade_currency": "INR"})

    def set_stoploss(self, position_id, price, sl_cid):
        """Attach a stop to a position that has none (POST)."""
        return self.post(f"/v1/futures/positions/{position_id}/riskorder",
                         {"is_stoploss": True, "stoploss_price": price, "order_source": "API",
                          "stoploss_client_order_id": sl_cid, "trade_currency": "INR"})

    def set_bracket(self, position_id, stop=None, target=None, sl_cid=None, tp_cid=None):
        """Attach one or both risk orders to an existing position."""
        if stop is None and target is None:
            raise ValueError("at least one of stop or target is required")
        body = {"order_source": "API", "trade_currency": "INR"}
        if stop is not None:
            body.update(is_stoploss=True, stoploss_price=stop)
            if sl_cid is not None:
                body["stoploss_client_order_id"] = sl_cid
        if target is not None:
            body.update(is_takeprofit=True, takeprofit_price=target)
            if tp_cid is not None:
                body["takeprofit_client_order_id"] = tp_cid
        return self.post(f"/v1/futures/positions/{position_id}/riskorder", body)

    def edit_stoploss(self, position_id, sl_order_id, price):
        """Amend an existing stop (PATCH, requires the stop's own order id)."""
        return self._raw("PATCH", f"/v1/futures/positions/{position_id}/riskorder",
                         body={"is_stoploss": True, "stoploss_price": price, "stoploss_order_id": sl_order_id,
                               "trade_currency": "INR"})

    def edit_bracket(self, position_id, stop_order_id=None, stop=None, target_order_id=None, target=None):
        """Amend either or both risk orders using their exchange order ids."""
        if stop is None and target is None:
            raise ValueError("at least one of stop or target is required")
        body = {"trade_currency": "INR"}
        if stop is not None:
            if not stop_order_id:
                raise ValueError("stop_order_id is required when amending stop")
            body.update(is_stoploss=True, stoploss_price=stop, stoploss_order_id=stop_order_id)
        if target is not None:
            if not target_order_id:
                raise ValueError("target_order_id is required when amending target")
            body.update(is_takeprofit=True, takeprofit_price=target, takeprofit_order_id=target_order_id)
        return self._raw("PATCH", f"/v1/futures/positions/{position_id}/riskorder", body=body)
