"""Self-learning layer: walk-forward re-tuning.

Every TEST_DAYS the bot re-scores the GRID on the previous TRAIN_DAYS, then trades the next TEST_DAYS
with the winner. Those test days were never seen during tuning, so the stitched test results are an
honest estimate of what learning earns live. Compared against never re-tuning (DEFAULT_PARAMS).

Rules: a candidate replaces current params only if it beats them by SWITCH_MARGIN on the train window.
If nothing on the grid is profitable with MIN_TRADES on the train window, the bot sits out (no edge = no trades).
Learning only picks strategy params; risk limits in config.py are never touched.
"""
import csv
import itertools
import os
import time

import backtest
import config
import strategy

NEG = float("-inf")


def score(r):
    """Return/drawdown. Ineligible: too few trades or not profitable."""
    if r["trades"] < config.MIN_TRADES or r["net_return"] <= 0:
        return NEG
    return r["net_return"] / max(r["max_dd"], 0.01)


def grid():
    keys = list(config.GRID)
    for vals in itertools.product(*(config.GRID[k] for k in keys)):
        p = {**config.DEFAULT_PARAMS, **dict(zip(keys, vals))}
        if p["fast"] < p["slow"]:
            yield p


def choose(candles, cache, start, end, current):
    """Pick params for the next window from bars [start, end). Returns (params|None, reason, best_score)."""
    scores = []
    for p in grid():
        r = backtest.run(candles, strategy.indicators(candles, p, cache), p, start, end)
        scores.append((score(r), p))
    best_s, best = max(scores, key=lambda x: x[0])
    if best_s == NEG:
        return None, "sit out: no profitable params on train window", best_s
    cur_s = next((s for s, p in scores if p == current), None)
    if cur_s is None:
        r = backtest.run(candles, strategy.indicators(candles, current, cache), current, start, end)
        cur_s = score(r)
    if cur_s == NEG or best_s > cur_s * config.SWITCH_MARGIN:
        return best, "switch", best_s
    return current, "keep", cur_s


def walk(candles, log_path=None):
    sec = config.INTERVAL_SEC
    n_train, n_test = config.TRAIN_DAYS * 86400 // sec, config.TEST_DAYS * 86400 // sec
    cache = {}
    first = i = config.WARMUP + n_train
    equity = peak = config.START_EQUITY
    current, rows, trades, halted = config.DEFAULT_PARAMS, [], [], False
    state, curve = None, [config.START_EQUITY]

    while i + n_test <= len(candles) and not halted:
        params, reason, s = choose(candles, cache, i - n_train, i, current)
        last = i + 2 * n_test > len(candles)
        # ponytail: a "sit out" window that inherits an open position runs under the last params and may also
        # enter; add a no-entries flag to backtest.run if this legacy strategy is ever revived.
        if params or (state and state["pos"]):          # an open position keeps running under the last params
            params = params or current
            # continuous: an open position and pending decisions carry into the next window (like live)
            r = backtest.run(candles, strategy.indicators(candles, params, cache), params, i, i + n_test, equity,
                             peak, state=state, carry=not last)
            equity, peak, halted, state = r["equity"], r["peak"], r["halted"], r["state"]
            trades += r["trade_list"]
            curve += r["curve"][1:]
            current = params
        else:
            state = None
        rows.append(dict(
            window_start=time.strftime("%Y-%m-%d", time.gmtime(candles[i][0])),
            decision=reason, train_score=f"{s:.3f}" if s != NEG else "",
            **({k: params[k] for k in config.GRID} if params else {k: "" for k in config.GRID}),
            test_trades=r["trades"] if params else 0,
            test_return=f"{r['net_return']:+.4f}" if params else "0",
            equity=f"{equity:.2f}"))
        i += n_test

    if not rows:
        raise ValueError(f"need > {first + n_test} candles for one walk-forward window, have {len(candles)}")
    if log_path:
        with open(log_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    p = config.DEFAULT_PARAMS
    base = backtest.run(candles, strategy.indicators(candles, p, cache), p, first, i)
    learned = backtest._stats(trades, config.START_EQUITY, equity, curve)      # marked, stitched equity curve
    learned.update(equity=equity, peak=peak, halted=halted, trade_list=trades)
    return learned, base, rows


if __name__ == "__main__":
    import data
    c = data.load()
    t0 = time.time()
    here = os.path.dirname(os.path.abspath(__file__))
    learned, base, rows = walk(c, os.path.join(here, "learning_log.csv"))
    print(f"{len(rows)} windows in {time.time() - t0:.0f}s  (log: learning_log.csv)")
    for r in rows:
        print(f"  {r['window_start']} {r['decision']:<52} trades={r['test_trades']:<3} "
              f"ret={r['test_return']:>8} equity={r['equity']}")
    print(f"\nSelf-learning (out-of-sample): {backtest.fmt(learned)}")
    print(f"Fixed default params        : {backtest.fmt(base)}")
