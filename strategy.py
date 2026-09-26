"""Entries: EMA crossover or Donchian breakout, always with a trend-EMA filter, optional ADX and volume
filters. Pure functions: value at index i uses only candles[0..i]."""


def ema(xs, n):
    k = 2 / (n + 1)
    out, e = [], xs[0]
    for x in xs:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x - (xs[i - n] if i >= n else 0)
        out.append(s / min(i + 1, n))
    return out


def atr(candles, n):
    """Wilder ATR."""
    out, a, prev = [], None, None
    for _, _, h, l, cl, _ in candles:
        tr = h - l if prev is None else max(h - l, abs(h - prev), abs(l - prev))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
        prev = cl
    return out


def adx(candles, n):
    """Wilder ADX: trend strength 0-100, direction-agnostic. <20 = chop, >25 = trending."""
    out, prev = [], None
    tr_s = pdm_s = mdm_s = adx_v = None
    for _, _, h, l, cl, _ in candles:
        if prev is None:
            out.append(0.0)
            prev = (h, l, cl)
            continue
        ph, pl, pc = prev
        up, dn = h - ph, pl - l
        pdm = up if up > dn and up > 0 else 0.0
        mdm = dn if dn > up and dn > 0 else 0.0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if tr_s is None:
            tr_s, pdm_s, mdm_s = tr, pdm, mdm
        else:
            tr_s = (tr_s * (n - 1) + tr) / n
            pdm_s = (pdm_s * (n - 1) + pdm) / n
            mdm_s = (mdm_s * (n - 1) + mdm) / n
        pdi = 100 * pdm_s / tr_s if tr_s else 0.0
        mdi = 100 * mdm_s / tr_s if tr_s else 0.0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0
        adx_v = dx if adx_v is None else (adx_v * (n - 1) + dx) / n
        out.append(adx_v)
        prev = (h, l, cl)
    return out


def highest(xs, n):
    """Max of xs[i-n+1..i]. ponytail: O(len*n) naive scan, fine for n<=100; monotonic deque if n grows."""
    return [max(xs[max(0, i - n + 1):i + 1]) for i in range(len(xs))]


def lowest(xs, n):
    return [min(xs[max(0, i - n + 1):i + 1]) for i in range(len(xs))]


def rsi(closes, n):
    """Wilder RSI 0-100."""
    out, up, dn = [50.0], None, None
    for a, b in zip(closes, closes[1:]):
        g, l = max(b - a, 0.0), max(a - b, 0.0)
        up = g if up is None else (up * (n - 1) + g) / n
        dn = l if dn is None else (dn * (n - 1) + l) / n
        out.append(100.0 if dn == 0 else 100 - 100 / (1 + up / dn))
    return out


def stdev(xs, n):
    """Rolling population std via E[x^2]-E[x]^2, clamped at 0 against float error."""
    m, m2 = sma(xs, n), sma([x * x for x in xs], n)
    return [max(b - a * a, 0.0) ** 0.5 for a, b in zip(m, m2)]


def indicators(candles, p, cache=None):
    """cache: dict shared across param sets so each indicator length is computed once."""
    cache = {} if cache is None else cache
    closes = cache.setdefault("close", [c[4] for c in candles])

    def get(kind, n):
        if (kind, n) not in cache:
            cache[(kind, n)] = {
                "ema": lambda: ema(closes, n),
                "atr": lambda: atr(candles, n),
                "adx": lambda: adx(candles, n),
                "hh": lambda: highest([c[2] for c in candles], n),
                "ll": lambda: lowest([c[3] for c in candles], n),
                "vol": lambda: sma([c[5] for c in candles], n),
                "sma": lambda: sma(closes, n),
                "std": lambda: stdev(closes, n),
                "rsi": lambda: rsi(closes, n),
            }[kind]()
        return cache[(kind, n)]

    ind = dict(p=p, close=closes, atr=get("atr", p["atr_len"]))
    if p["trend"]:
        ind["trend"] = get("ema", p["trend"])
    e = p["entry"]
    if e == "ema":
        ind.update(fast=get("ema", p["fast"]), slow=get("ema", p["slow"]))
    elif e == "breakout":
        ind.update(hh=get("hh", p["don_n"]), ll=get("ll", p["don_n"]))
    elif e == "rsi":
        ind["rsi"] = get("rsi", p["rsi_len"])
    elif e == "bb":
        ind.update(mid=get("sma", p["bb_n"]), sd=get("std", p["bb_n"]))
    else:
        raise ValueError(f"unknown entry {e!r}")
    if p["adx_min"] or p["adx_max"]:
        ind["adx"] = get("adx", p["adx_len"])
    if p["vol_mult"]:
        ind["volume"] = cache.setdefault("volume", [c[5] for c in candles])
        ind["vol_avg"] = get("vol", p["vol_len"])
    return ind


def cross(ind, i):
    """Raw entry event at bar i, no filters; +1 = go long, -1 = go short, 0 none. Fires once (fresh).
    ema: fast/slow cross. breakout: close first breaks previous don_n-bar high/low.
    rsi: RSI first drops below rsi_lo (buy dip) / rises above rsi_hi (sell rip).
    bb: close first falls below lower band (buy) / rises above upper band (sell)."""
    if i < 2:
        return 0
    p, c = ind["p"], ind["close"]
    e = p["entry"]
    if e == "ema":
        f, s = ind["fast"], ind["slow"]
        if f[i] > s[i] and f[i - 1] <= s[i - 1]:
            return 1
        if f[i] < s[i] and f[i - 1] >= s[i - 1]:
            return -1
        return 0
    if e == "breakout":
        hh, ll = ind["hh"], ind["ll"]
        if c[i] > hh[i - 1] and c[i - 1] <= hh[i - 2]:
            return 1
        if c[i] < ll[i - 1] and c[i - 1] >= ll[i - 2]:
            return -1
        return 0
    if e == "rsi":
        r = ind["rsi"]
        if r[i] < p["rsi_lo"] <= r[i - 1]:
            return 1
        if r[i] > p["rsi_hi"] >= r[i - 1]:
            return -1
        return 0
    m, sd, k = ind["mid"], ind["sd"], p["bb_k"]
    if c[i] < m[i] - k * sd[i] and c[i - 1] >= m[i - 1] - k * sd[i - 1]:
        return 1
    if c[i] > m[i] + k * sd[i] and c[i - 1] <= m[i - 1] + k * sd[i - 1]:
        return -1
    return 0


def exit_signal(ind, i, side):
    """True = close the open `side` position at next open.
    Trend entries exit on an opposite event; mean-reversion entries exit once price is back at the mean."""
    e = ind["p"]["entry"]
    if e == "rsi":
        return ind["rsi"][i] >= 50 if side == 1 else ind["rsi"][i] <= 50
    if e == "bb":
        c, m = ind["close"][i], ind["mid"][i]
        return c >= m if side == 1 else c <= m
    return cross(ind, i) == -side


def signal(ind, i):
    """Entry signal at close of bar i: raw event in the trend direction, passing enabled filters."""
    x = cross(ind, i)
    if not x:
        return 0
    p, c = ind["p"], ind["close"][i]
    if p["trend"]:
        t = ind["trend"][i]
        if (x == 1 and c <= t) or (x == -1 and c >= t):
            return 0
    if p["adx_min"] and ind["adx"][i] < p["adx_min"]:
        return 0
    if p["adx_max"] and ind["adx"][i] > p["adx_max"]:
        return 0
    if p["vol_mult"] and ind["volume"][i] < p["vol_mult"] * ind["vol_avg"][i]:
        return 0
    return x
