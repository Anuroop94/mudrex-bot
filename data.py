"""Public Mudrex kline fetch with a local CSV cache. Candle = [open_time, open, high, low, close, volume]."""
import csv
import json
import os
import time
import urllib.error
import urllib.request

import config

KLINE_URL = "https://trade.mudrex.com/fapi/v1/price/kline"
MAX_PER_REQ = 1440
RETRY_CODES = (429, 500, 502, 503, 504)


def _get(url, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                body = json.load(r)
            if not body.get("success"):
                raise RuntimeError(f"kline request failed: {body.get('errors')}")
            return body
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if i == tries - 1:
                raise
        time.sleep(2 ** i)


def fetch(start, end, symbol=config.SYMBOL, interval=config.INTERVAL, sec=config.INTERVAL_SEC):
    """Candles with open_time in [start, end], paginated. May include the still-forming candle."""
    out = {}
    t = start
    while t <= end:
        chunk_end = min(t + sec * (MAX_PER_REQ - 1), end)
        url = f"{KLINE_URL}?assets={symbol}&aggregation={interval}&start_time={t}&end_time={chunk_end}"
        for r in _get(url)["data"]["asset_ticks"].get(symbol.lower(), []):
            out[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:6]]
        t = chunk_end + sec
        time.sleep(0.25)  # stay far below 300 req/min
    return [out[k] for k in sorted(out)]


def closed_only(candles, now=None, sec=config.INTERVAL_SEC):
    """Drop the candle that has not closed yet; trading on it would be lookahead."""
    now = time.time() if now is None else now
    return [c for c in candles if c[0] + sec <= now]


def _path(symbol, interval):
    name = f"{symbol.replace('/', '')}_{interval}.csv"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", name)


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [[int(r[0])] + [float(x) for x in r[1:6]] for r in csv.reader(f)]


def _write(path, candles):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        csv.writer(f).writerows(candles)
    os.replace(tmp, path)  # atomic: a crash mid-write never corrupts the cache


def load(days=config.HISTORY_DAYS, symbol=config.SYMBOL, interval=config.INTERVAL, sec=config.INTERVAL_SEC):
    """Last `days` of closed candles, fetching only what the cache is missing."""
    now = int(time.time())
    start = (now - days * 86400) // sec * sec
    path = _path(symbol, interval)
    have = {c[0]: c for c in _read(path) if c[0] >= start}
    covers_start = have and min(have) <= start + sec
    fetch_from = max(have) + sec if covers_start else start
    if fetch_from <= now:
        for c in fetch(fetch_from, now, symbol, interval, sec):
            have[c[0]] = c
    candles = closed_only([have[k] for k in sorted(have)], now, sec)
    if not candles:
        raise RuntimeError(f"no candles returned for {symbol} {interval}")
    _write(path, candles)
    return candles


def gaps(candles, sec=config.INTERVAL_SEC):
    """Number of missing candles between first and last."""
    return sum((b[0] - a[0]) // sec - 1 for a, b in zip(candles, candles[1:]))


if __name__ == "__main__":
    c = load()
    print(f"{len(c)} candles, {gaps(c)} missing, "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(c[0][0]))} -> "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(c[-1][0]))} UTC")
