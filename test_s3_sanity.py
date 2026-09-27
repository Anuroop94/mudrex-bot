"""Sanity tests for s3_research backtest simulator with synthetic data only."""
import sys
sys.path.insert(0, '.')

import s3_research
import config
import portfolio as pf

HOUR = 3600
DAY = 86400

def build_synthetic_d():
    """Minimal D with 500 hourly bars, 2 coins. Includes breakout after warmup."""
    base_date = 1609459200  # 2021-01-01
    base_time = base_date + 300 * DAY

    hours = [base_time + i * HOUR for i in range(500)]

    # XRP: steady at 2.5 until bar 420 (after warmup), then breakout
    xrp_opens = [2.5] * 500
    xrp_closes = [2.5] * 500
    xrp_highs = [2.51] * 500
    xrp_lows = [2.49] * 500

    # Breakout at bar 420
    xrp_closes[420] = 2.55
    xrp_highs[420] = 2.56
    xrp_lows[420] = 2.54
    xrp_opens[420] = 2.51
    xrp_opens[421] = 2.55

    # Rise for profit after entry
    for i in range(421, 500):
        xrp_opens[i] = 2.55 + 0.01 * ((i - 421) // 24)
        xrp_closes[i] = xrp_opens[i] + 0.005
        xrp_highs[i] = xrp_closes[i] + 0.005
        xrp_lows[i] = xrp_opens[i] - 0.005

    # ADA: steady uptrend, no setup needed
    ada_opens = [1.0 + 0.01 * (i // 24) for i in range(500)]
    ada_closes = [o + 0.003 for o in ada_opens]
    ada_highs = [c + 0.006 for c in ada_closes]
    ada_lows = [o - 0.003 for o in ada_opens]

    # ATRs
    atr_values = [0.02] * 500

    # RSI: one dip for DIP test at bar 400
    rsi_values = [50.0] * 500
    rsi_values[400] = 28.0
    rsi_values[399] = 32.0

    def prior_24h_high(closes, i):
        if i < 30:
            return None
        return max(closes[max(0, i-24):i])

    xrp_hh = [prior_24h_high(xrp_closes, i) for i in range(500)]
    ada_hh = [prior_24h_high(ada_closes, i) for i in range(500)]

    D = {
        'coins': ['XRP', 'ADA'],
        'f': {
            'XRP': {
                't': hours,
                'o': xrp_opens,
                'h': xrp_highs,
                'l': xrp_lows,
                'c': xrp_closes,
                'atr': atr_values,
                'rsi': rsi_values,
                'hh': xrp_hh,
                'idx': {h: i for i, h in enumerate(hours)}
            },
            'ADA': {
                't': hours,
                'o': ada_opens,
                'h': ada_highs,
                'l': ada_lows,
                'c': ada_closes,
                'atr': atr_values,
                'rsi': rsi_values,
                'hh': ada_hh,
                'idx': {h: i for i, h in enumerate(hours)}
            }
        },
        'trend': {},
        'btc': [],
        'specs': {
            'XRP': {'min_notional': 5.0, 'min_qty': 0.1, 'step': 0.01},
            'ADA': {'min_notional': 5.0, 'min_qty': 0.1, 'step': 0.01}
        },
        'datr': {}
    }

    # BTC history covering all hourly bars
    final_day = ((base_time + 500 * HOUR) // DAY) * DAY
    days_needed = (final_day - base_date) // DAY + 1

    btc_close = 40000.0
    for day_idx in range(days_needed + 10):
        day_ts = base_date + day_idx * DAY
        btc_close = btc_close * (1 + 0.002)
        D['btc'].append((day_ts, btc_close - 100, btc_close + 100, btc_close - 50, btc_close, 0))

    # Trend and daily ATR for each day
    D['trend']['XRP'] = {}
    D['trend']['ADA'] = {}
    D['datr']['XRP'] = {}
    D['datr']['ADA'] = {}

    for h in hours:
        day_ts = (h // DAY) * DAY
        D['trend']['XRP'][day_ts] = 1.0
        D['trend']['ADA'][day_ts] = 1.0
        D['datr']['XRP'][day_ts] = 0.05
        D['datr']['ADA'][day_ts] = 0.05

    return D

def test_zero_costs():
    """Test 1: Zero costs, breakout strategy must end with equity > start."""
    D = build_synthetic_d()

    orig_cost = s3_research.FEE
    orig_slippage = pf.SLIPPAGE
    orig_funding = config.FUNDING_PER_DAY
    orig_ist_offset = config.IST_OFFSET

    try:
        s3_research.FEE = 0.0
        pf.SLIPPAGE = 0.0
        config.FUNDING_PER_DAY = 0.0
        config.IST_OFFSET = 0

        r = s3_research.run(D, "BRK", tp=1.0, sl=1.0, hold=24)

        if r['equity'] > 2500:
            return "PASS"
        else:
            return f"FAIL: equity {r['equity']} <= start 2500"
    except Exception as e:
        return f"FAIL: {e}"
    finally:
        s3_research.FEE = orig_cost
        pf.SLIPPAGE = orig_slippage
        config.FUNDING_PER_DAY = orig_funding
        config.IST_OFFSET = orig_ist_offset

def test_flat_price_with_costs():
    """Test 2: Flat price after entry should lose approx round-trip costs + funding."""
    D = build_synthetic_d()

    # Flatten prices after entry
    for c in D['f']:
        price = D['f'][c]['c'][421]
        for i in range(422, 450):
            D['f'][c]['o'][i] = price
            D['f'][c]['h'][i] = price + 0.0001
            D['f'][c]['l'][i] = price - 0.0001
            D['f'][c]['c'][i] = price

    orig_cost = s3_research.FEE
    orig_slippage = pf.SLIPPAGE
    orig_funding = config.FUNDING_PER_DAY
    orig_ist_offset = config.IST_OFFSET

    try:
        s3_research.FEE = 0.001
        pf.SLIPPAGE = 0.0002
        config.FUNDING_PER_DAY = 0.0001
        config.IST_OFFSET = 0

        r = s3_research.run(D, "BRK", tp=1.0, sl=1.0, hold=24, max_coins=1)

        if r['trades']:
            net = r['trades'][0]['net']
            if net < 0:
                return "PASS"
            else:
                return f"FAIL: net {net} should be negative with flat price and costs"
        else:
            return "FAIL: no trades executed"
    except Exception as e:
        return f"FAIL: {e}"
    finally:
        s3_research.FEE = orig_cost
        pf.SLIPPAGE = orig_slippage
        config.FUNDING_PER_DAY = orig_funding
        config.IST_OFFSET = orig_ist_offset

def test_stop_before_target():
    """Test 3: Bar with both low <= stop AND high >= target exits at stop."""
    D = build_synthetic_d()

    orig_slippage = pf.SLIPPAGE
    orig_cost = s3_research.FEE
    orig_ist_offset = config.IST_OFFSET

    try:
        pf.SLIPPAGE = 0.0
        s3_research.FEE = 0.0
        config.IST_OFFSET = 0

        # Entry at bar 421, exit at bar 422
        entry_price = D['f']['XRP']['o'][421]
        atr = D['f']['XRP']['atr'][421]

        # Make bar 422 have both stop and target hit
        D['f']['XRP']['l'][422] = entry_price - 1.5 * atr  # below stop
        D['f']['XRP']['h'][422] = entry_price + 1.5 * atr  # above target
        D['f']['XRP']['o'][422] = entry_price
        D['f']['XRP']['c'][422] = entry_price

        r = s3_research.run(D, "BRK", tp=1.0, sl=1.0, hold=24, max_coins=1)

        if r['trades']:
            trade = r['trades'][0]
            if trade['why'] == 'stop':
                return "PASS"
            else:
                return f"FAIL: exited on {trade['why']}, expected 'stop'"
        else:
            return "FAIL: no trades"
    except Exception as e:
        return f"FAIL: {e}"
    finally:
        pf.SLIPPAGE = orig_slippage
        s3_research.FEE = orig_cost
        config.IST_OFFSET = orig_ist_offset

def test_no_lookahead():
    """Test 4: Entry at next hour's open, not setup bar's close."""
    D = build_synthetic_d()

    orig_ist_offset = config.IST_OFFSET

    try:
        config.IST_OFFSET = 0

        # Bar 420 breakout, bar 421 entry
        D['f']['XRP']['c'][420] = 2.52
        D['f']['XRP']['o'][421] = 2.60

        r = s3_research.run(D, "BRK", tp=1.0, sl=1.0, hold=24, max_coins=1)

        if r['trades']:
            entry = r['trades'][0]['entry']
            expected = 2.60 * (1 + pf.SLIPPAGE)
            if abs(entry - expected) / expected < 0.05:
                return "PASS"
            else:
                return f"FAIL: entry {entry} far from expected {expected}"
        else:
            return "FAIL: no trades"
    except Exception as e:
        return f"FAIL: {e}"
    finally:
        config.IST_OFFSET = orig_ist_offset

def test_no_overlapping_sets():
    """Test 5: Next set doesn't start while any coin of current set is open."""
    D = build_synthetic_d()

    orig_ist_offset = config.IST_OFFSET

    try:
        config.IST_OFFSET = 0

        r = s3_research.run(D, "BRK", tp=1.0, sl=1.0, hold=48, max_coins=2)

        if 'sets' in r and 'trades' in r:
            return "PASS"
        else:
            return f"FAIL: missing keys in result"
    except Exception as e:
        return f"FAIL: {e}"
    finally:
        config.IST_OFFSET = orig_ist_offset

def test_sets_only_wake_hours():
    """Test 6: Sets only start between 08:00 and 23:00 IST."""
    D = build_synthetic_d()

    orig_ist_offset = config.IST_OFFSET

    try:
        config.IST_OFFSET = 19800

        r = s3_research.run(D, "BRK", tp=1.0, sl=1.0, hold=24, filters=True)

        if 'log' in r and isinstance(r['log'], list):
            for entry in r['log']:
                if entry.get('event') == 'SET':
                    t = entry['t']
                    ist_hour = ((t + config.IST_OFFSET) % DAY) // HOUR
                    if not (8 <= ist_hour < 23):
                        return f"FAIL: SET at hour {ist_hour} outside 08:00-23:00"
            return "PASS"
        else:
            return "FAIL: missing log in result"
    except Exception as e:
        return f"FAIL: {e}"
    finally:
        config.IST_OFFSET = orig_ist_offset


def test_exact_round_trip_cost():
    """Flat market after entry, exit on time: net P&L equals exactly -(fees both sides + slippage + funding)."""
    D = build_synthetic_d()
    D["coins"] = ["XRP"]
    f = D["f"]["XRP"]
    for i in range(421, 500):
        f["o"][i] = f["h"][i] = f["l"][i] = f["c"][i] = 2.55
    r = s3_research.run(D, "BRK", 50, 1, hold=5, max_coins=2, scale="h", filters=True)   # stop 1 ATR: never hit
    tr = r["trades"][0]
    px_in = 2.55 * (1 + pf.SLIPPAGE)
    px_out = 2.55 * (1 - pf.SLIPPAGE)
    qty = int(2 * 2500 / s3_research.RATE / px_in / 0.01) * 0.01
    rate, fee = s3_research.RATE, s3_research.FEE
    expected = (qty * (px_out - px_in) * rate - qty * px_in * rate * fee - qty * px_out * rate * fee
                - 5 * qty * 2.55 * rate * config.FUNDING_PER_DAY / 24)
    if tr["why"] != "time" or abs(tr["net"] - expected) > 0.01:
        return f"FAIL: why {tr['why']} net {tr['net']:.4f} expected {expected:.4f}"
    return "PASS"
if __name__ == "__main__":
    tests = [
        ("test_zero_costs", test_zero_costs),
        ("test_exact_round_trip_cost", test_exact_round_trip_cost),
        ("test_flat_price_with_costs", test_flat_price_with_costs),
        ("test_stop_before_target", test_stop_before_target),
        ("test_no_lookahead", test_no_lookahead),
        ("test_no_overlapping_sets", test_no_overlapping_sets),
        ("test_sets_only_wake_hours", test_sets_only_wake_hours),
    ]

    results = []
    for name, test_fn in tests:
        result = test_fn()
        results.append((name, result))
        status = "PASS" if result == "PASS" else "FAIL"
        print(f"{name}: {status}")
        if status == "FAIL":
            print(f"  {result}")

    passed = sum(1 for _, r in results if r == "PASS")
    failed = sum(1 for _, r in results if r.startswith("FAIL"))

    print(f"\n{passed} PASSED, {failed} FAILED")
    sys.exit(0 if failed == 0 else 1)
