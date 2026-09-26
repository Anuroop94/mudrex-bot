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
        return self.get("/v1/futures/positions", {"trade_currency": "INR"}) or []

    def asset(self, symbol):
        return self.get(f"/v1/futures/{symbol}", {"is_symbol": ""})

    def order_by_client_id(self, cid):
        """Order detail, or None if Mudrex has no order with this client_order_id."""
        try:
            return self.get("/v1/futures/orders/detail", {"client_order_id": cid})
        except Rejected as e:
            if e.status == 404:
                return None
            raise

    def history(self, kind, max_pages=50):
        """All INR 'orders' or 'positions' history, paginated with the epoch-ms created_at cursor."""
        rows, offset, seen = [], None, set()
        for _ in range(max_pages):
            params = {"trade_currency": "INR", "limit": 100}
            if offset is not None:
                params["offset"] = offset
            page = self.get(f"/v1/futures/{kind}/history", params) or []
            fresh = [r for r in page if r["id"] not in seen]
            if not fresh:
                break
            rows += fresh
            seen.update(r["id"] for r in fresh)
            if len(page) < 100:
                break
            oldest = min(r["created_at"] for r in fresh)
            offset = _epoch_ms(oldest)
        return rows

    # ---------- writes (never auto-retried)
    def set_leverage(self, symbol, lev):
        return self.post(f"/v1/futures/{symbol}/leverage?is_symbol",
                         {"margin_type": "ISOLATED", "leverage": str(lev), "trade_currency": "INR"})

    def place_market_long(self, symbol, qty, cid, stop=None):
        body = {"trigger_type": "MARKET", "order_type": "LONG", "quantity": qty, "trade_currency": "INR",
                "client_order_id": cid}
        if stop:
            body.update(is_stoploss=True, stoploss_price=stop)
        return self.post(f"/v2/futures/order?symbol={symbol}", body)

    def close_position(self, position_id):
        return self.post(f"/v1/futures/positions/{position_id}/close", {})

    def set_stoploss(self, position_id, price, sl_cid):
        return self.post(f"/v1/futures/positions/{position_id}/riskorder",
                         {"is_stoploss": True, "stoploss_price": price, "order_source": "API",
                          "stoploss_client_order_id": sl_cid})


def _epoch_ms(iso):
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
