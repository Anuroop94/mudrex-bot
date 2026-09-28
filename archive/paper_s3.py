"""PAPER trading of S3, the set trader (owner's choice 2026-09-27: S1 live, S3 paper only). Never places orders.

Variant: breakout above the previous 24h high, target 1 x daily ATR, stop 0.5 x daily ATR, max 48h, only when
the coin's own daily trend is up and BTC mood is good; sets of up to 2 coins; the next set starts only after the
current one is fully closed; sets start 08:00-23:00 IST; the 5% daily line blocks new sets. Its backtest LOST
(May 2021 - Sep 2025: -76%), so this is for watching, not money. Fresh Rs 2,500 paper account from START.
Run hourly (scheduled task MudrexPaperS3): replays every closed hour since START (deterministic, no drift).
"""
import json
import os
import time

import config
import s3_research as s3

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "s3_paper_state.json")
LOG_PATH = os.path.join(HERE, "s3_paper.log")
VARIANT = dict(kind="BRK", tp=1.0, sl=0.5, hold=48, scale="d", filters=True)
START = 1790467200          # 2026-09-27 00:00 UTC: paper account starts here


def ist(t):
    return time.strftime("%d %b %H:%M", time.gmtime(t + config.IST_OFFSET))


def run():
    D = s3.load()
    r = s3.run(D, VARIANT["kind"], VARIANT["tp"], VARIANT["sl"], VARIANT["hold"], scale=VARIANT["scale"],
               filters=VARIANT["filters"], start=START)
    today = (time.time() + config.IST_OFFSET) // 86400
    st = dict(at=time.time(), variant=VARIANT, start_inr=s3.ALLOC, equity_inr=round(r["equity"], 2),
              value_inr=round(r["equity"] + sum(p["u"] for p in r["open"]), 2),
              sets_total=r["sets"], sets_today=sum(1 for e in r["log"] if (e["t"] + config.IST_OFFSET) // 86400 == today),
              blocked_today=r["blocked"],
              open_set=[dict(coin=p["c"], entry=p["entry"], target=p["tp"], stop=p["sl"], since=ist(p["t"]),
                             pnl_inr=round(p["u"], 2)) for p in r["open"]],
              trades=[dict(coin=t["coin"], opened=ist(t["entry_t"]), closed=ist(t["exit_t"]), why=t["why"],
                           pnl_inr=round(t["net"], 2)) for t in r["trades"]][-50:])
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_PATH)
    line = (f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(time.time() + config.IST_OFFSET))} IST  S3 paper: "
            f"Rs {st['value_inr']:,.0f} (incl. open), {st['sets_total']} sets ({st['sets_today']} today), "
            f"open: {', '.join(p['coin'] for p in st['open_set']) or 'none'}")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    run()
