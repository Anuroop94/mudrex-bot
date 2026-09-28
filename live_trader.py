"""LIVE trader for the bounded two-sided strategy. REAL MONEY entry path is migration-gated.

  python live_trader.py plan        READ-ONLY on the exchange. Computes today's S1 orders, records the plan in the
                                    execution journal (execution.db) and writes live_plan.json. Places nothing.
  python live_trader.py execute     Operator command for a recorded plan; policy still enforces set approvals.
  python live_trader.py reconcile   After a crash/restart: resolves unfinished orders by client_order_id.

Nothing executes unless LIVE_TRADING_ENABLED=true is set in .env (default false), no STOP file exists, and the
explicit autonomous migration gate in trade_policy.py has been certified. Sets 1-3 then need no owner tap; an
extra set needs a current Telegram approval bound to its immutable proposal.
All order safety (locking, reconciliation, ownership, fill-aware stops, caps, drift) lives in execution.py.
"""
import json
import math
import os
import sys
import time

import adaptive_risk
import bounded_learning
import config
import data
import execution as ex
import pick_coins
import portfolio as pf
import s1
import s4
import trade_policy
import live_universe
from mudrex_client import Client

HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_PATH = os.path.join(HERE, "live_plan.json")
STATE_PATH = os.path.join(HERE, "live_state.json")
DAY = pf.DAY
HOUR = 3600


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def read_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return dict(armed={})


# ---------- pure planning logic (unit-tested)

def bad_data(uni, last_closed, days=400):
    """Coins whose daily history is stale (no bar for the decision day) or has gaps in the last `days` days."""
    bad = set()
    for c, cs in uni.items():
        recent = [x[0] for x in cs if x[0] > last_closed - days * DAY]
        if (len(recent) < days or recent[-1] != last_closed or
                any(b - a != DAY for a, b in zip(recent, recent[1:]))):
            bad.add(c)
    return bad


def build_orders(targets, owned, manual_symbols, prices, atrs, specs, size_equity_inr, armed, entries_blocked,
                 rate, bad=(), realized_pnls=(), active_stop_risks=(), available_slots=3,
                 candidate_cost_buffer_inr=10.0, completed_sets=0, basket=None, model_decisions=None,
                 entry_basket=None):
    """Build side-aware actions with quantity determined from the fixed rupee risk ledger.

    targets: {coin: signed 1x weight}; owned values may be a legacy position id or {id, side}.
    Returns actions. CLOSE only for bot-owned positions; never plans anything on a symbol with a manual position.
    bad: coins with stale/gappy price data -> no decision at all today (a held one keeps its exchange stop)."""
    del size_equity_inr  # fixed rupee risk, not account equity, controls quantity
    out = []
    candidates = []
    reserved = list(active_stop_risks)
    reserved_margin_inr = 0.0
    basket = tuple(basket or s1.BASKET)
    entry_basket = frozenset(entry_basket if entry_basket is not None else basket)
    model_decisions = model_decisions or {}
    for c in basket:
        if c in bad:
            out.append(dict(action="SKIP", coin=c, reason="price data stale or missing days: no decision today"))
            continue
        w, px, s = targets.get(c, 0.0), prices[c], specs[c]
        desired_side = "LONG" if w > 0 else "SHORT" if w < 0 else None
        held = owned.get(c)
        held_id = held.get("id") if isinstance(held, dict) else held
        held_side = str(held.get("side") or "LONG").upper() if isinstance(held, dict) else "LONG"
        if c in manual_symbols:
            out.append(dict(action="SKIP", coin=c, reason="you hold a manual position on this coin; bot stays out"))
        elif held_id and desired_side != held_side:
            out.append(dict(action="CLOSE", coin=c, position_id=held_id,
                            reason="trend exit / side flip; opposite entry waits for a later set"))
        elif held_id:
            out.append(dict(action="HOLD", coin=c, reason=f"{held_side.lower()} trend still active"))
        elif desired_side:
            if c not in entry_basket:
                out.append(dict(action="SKIP", coin=c, reason="not eligible for a new entry in this cycle"))
                continue
            if entries_blocked:
                out.append(dict(action="SKIP", coin=c, reason=entries_blocked))
                continue
            if not armed.get(c, True):
                out.append(dict(action="SKIP", coin=c, reason="stopped out earlier; waits for trend to reset"))
                continue
            confidence = min(1.0, abs(float(w)) * len(basket))
            learned = model_decisions.get((c, desired_side), bounded_learning.Decision())
            if learned.active and learned.veto:
                out.append(dict(action="SKIP", coin=c, reason=learned.reason, model_score=learned.score))
                continue
            candidates.append((learned.score if learned.active else 0.5, confidence, c, desired_side, px, s, learned))

    slots = max(0, min(int(available_slots), trade_policy.MAX_SETS_PER_CYCLE))
    opened = 0
    for _, confidence, c, side, px, s, learned in sorted(candidates, reverse=True):
        if opened >= slots or (completed_sets + opened >= trade_policy.TARGET_SETS_PER_CYCLE and confidence < 0.85):
            out.append(dict(action="SKIP", coin=c, reason="daily set target filled; third set needs a very strong signal"))
            continue
        funding_rate = s.get("funding_fee_perc_hour")
        if funding_rate is None:
            funding_rate = config.FUNDING_PER_DAY / 24.0
        try:
            rp = adaptive_risk.plan_trade(
                side=side, entry=px, atr=atrs[c], confidence=confidence,
                exchange_max_leverage=s.get("max_leverage", adaptive_risk.HARD_MAX_LEVERAGE),
                realized_pnls=realized_pnls,
                active_stop_risks=reserved, candidate_cost_buffer_inr=candidate_cost_buffer_inr,
                funding_fee_perc_hour=funding_rate,
                inr_per_price_unit=rate, qty_step=s["step"], min_qty=s["min_qty"],
                min_notional_inr=s["min_notional"] * rate,
            )
        except (TypeError, ValueError) as e:
            out.append(dict(action="SKIP", coin=c, reason=f"adaptive risk veto: {e}"))
            continue
        if rp is None:
            out.append(dict(action="SKIP", coin=c, reason="signal/risk budget cannot size a safe exchange order"))
            continue
        remaining_margin_inr = max(0.0, s1.CAPITAL_CAP_INR - reserved_margin_inr)
        max_qty = math.floor((rp.leverage * remaining_margin_inr / rate / px) / s["step"] + 1e-12) * s["step"]
        qty = min(rp.quantity, max_qty)
        notional_inr = qty * px * rate
        modeled_risk = qty * rp.risk_per_unit_inr + candidate_cost_buffer_inr
        if qty < s["min_qty"] or notional_inr < s["min_notional"] * rate or modeled_risk <= 0:
            out.append(dict(action="SKIP", coin=c, reason="capital cap makes the risk-sized order too small"))
            continue
        reserved.append(modeled_risk)
        reserved_margin_inr += notional_inr / rp.leverage
        opened += 1
        out.append(dict(action="OPEN", coin=c, side=side, planned_price=px,
                        notional_inr=round(notional_inr, 2), qty=qty, atr=atrs[c],
                        stop_loss=rp.stop_loss, take_profit=rp.take_profit,
                        est_stop=round(rp.stop_loss, 8), est_target=round(rp.take_profit, 8),
                        leverage=rp.leverage, planned_risk_inr=round(modeled_risk, 2),
                        volatility_tier=rp.volatility_tier, confidence=round(confidence, 3),
                        model_score=round(learned.score, 3), model_active=learned.active,
                        reason=f"{side.lower()} trend; {rp.volatility_tier} volatility; {learned.reason}"))
    return out



# ---------- S4: intraday momentum sets (owner 2026-09-27)

STRATEGY = "S4"          # "S4" intraday sets (owner's rule: intraday); "S1" = daily two-sided trend (Codex build)


def s4_orders(owned, opened_at, D, datr, specs, now, blocked, rate, bot_eq, realized_pnls=(),
              active_stop_risks=(), available_slots=3, cost_buffer_inr=10.0, s4_ids=None):
    """S4 plan actions (s4.py). Held S4 positions: CLOSE once s4.HOLD_H hours old (or open time unknown), else
    HOLD (the exchange stop-loss / take-profit exits them). A new set ONLY when no S4 position is open: sets are
    sequential. Bot positions opened by ANOTHER strategy (the legacy S1 XRP trade) are never time-exited by S4:
    they keep their exchange stop until the owner decides; their stop risk still counts in active_stop_risks.
    owned: {coin: {id, side}}; opened_at: {position_id: exchange open time}; s4_ids: position ids S4 opened
    (None = all, for tests); D: s4.setups input for coins that may be ENTERED; datr: {coin: last daily ATR}."""
    out = []
    mine = {c: h for c, h in owned.items() if s4_ids is None or h["id"] in s4_ids}
    for c, h in sorted(owned.items()):
        if c not in mine:
            out.append(dict(action="HOLD", coin=c, reason="opened by another strategy: S4 never time-exits it; "
                                                         "it keeps its exchange stop-loss (owner decides)"))
            continue
        opened = opened_at.get(h["id"])
        age = now - opened if opened else None
        if age is None or age >= s4.HOLD_H * HOUR:
            out.append(dict(action="CLOSE", coin=c, position_id=h["id"],
                            reason=f"S4 maximum hold of {s4.HOLD_H}h reached" if age is not None else
                            "S4 position with unknown open time: closed so it cannot be held without limit"))
        else:
            out.append(dict(action="HOLD", coin=c, reason=f"S4 set open {age / 3600:.0f}h; exchange SL/TP manage it"))
    why = ("a set is open; the next set starts after it closes" if mine else
           None if blocked else "daily set limit reached" if available_slots <= 0 else None)
    if why:
        return out + [dict(action="SKIP", coin="-", reason=why)]
    # Blocked (STOP = dry run, caps, ...): still find the setup and show it as a PREVIEW. A PREVIEW is never
    # recorded as a plan and never reaches execution (only OPEN/CLOSE are); it lets the owner watch the strategy.
    action = "PREVIEW" if blocked else "OPEN"
    t = now // HOUR * HOUR - HOUR                                  # last CLOSED hour
    held = set(owned)                                              # one-way per symbol: never a held coin
    found = s4.setups(dict(D, coins=[c for c in D["coins"] if c not in held]), t)
    if not found:
        return out + [dict(action="SKIP", coin="-", reason="no qualified intraday setup this hour")]
    _, c, side = found[0]
    s, a, px = specs[c], datr.get(c), D["f"][c]["c"][-1]

    def pos(x):                                                   # finite and > 0, else None (fail closed)
        try:
            x = float(x)
        except (TypeError, ValueError):
            return None
        return x if math.isfinite(x) and x > 0 else None
    a, px, rate, bot_eq = pos(a), pos(px), pos(rate), pos(bot_eq)
    step, max_ex = pos(s.get("step")), pos(s.get("max_leverage"))
    if None in (a, px, rate, bot_eq, step, max_ex) or pos(s.get("min_qty")) is None \
            or pos(s.get("min_notional")) is None:
        return out + [dict(action="SKIP", coin=c, reason="price, ATR, rate, balance or contract spec invalid")]
    d = 1 if side == "LONG" else -1
    stop, target = px - d * s4.SL_DATR * a, px + d * s4.TP_DATR * a
    try:
        trade_policy.validate_bracket(side, px, stop, target)
        remaining = adaptive_risk.remaining_daily_risk(realized_pnls, active_stop_risks, cost_buffer_inr)
    except ValueError as e:
        return out + [dict(action="SKIP", coin=c, reason=f"bracket/risk veto: {e}")]
    risk = min(s4.SET_RISK_INR, remaining)
    per_unit = s4.SL_DATR * a * rate
    qty = math.floor(risk / per_unit / step + 1e-9) * step
    alloc = min(float(bot_eq), float(s1.CAPITAL_CAP_INR))
    qty = min(qty, math.floor(s4.MAX_NOTIONAL_LEV * alloc / (px * rate) / step + 1e-9) * step)
    notional = qty * px * rate
    max_lev = min(max_ex, adaptive_risk.HARD_MAX_LEVERAGE)
    lev = max(1, math.ceil(notional / alloc - 1e-9)) if alloc > 0 else 0
    if qty < s["min_qty"] or qty * px < s["min_notional"] or not lev or lev > max_lev:
        return out + [dict(action="SKIP", coin=c, reason="risk-sized order below Mudrex minimum or above margin")]
    planned_risk = qty * per_unit + cost_buffer_inr
    out.append(dict(action=action, coin=c, side=side, planned_price=px, notional_inr=round(notional, 2), qty=qty,
                    atr=a, stop_loss=stop, take_profit=target, est_stop=round(stop, 8), est_target=round(target, 8),
                    leverage=float(lev), planned_risk_inr=round(planned_risk, 2), confidence=1.0, strategy="S4",
                    volatility_tier="s4", reason=f"S4 {side.lower()} momentum (strongest 24h mover)"
                    + (f"; not placed: {blocked}" if blocked else "")))
    return out



def s4_position_ids(con):
    """Bot positions whose opening order was an S4 order (orders.strategy)."""
    return {r[0] for r in con.execute("SELECT w.position_id FROM owned w JOIN orders o ON "
                                      "o.client_order_id=w.client_order_id WHERE o.strategy='S4'")}


def reuse_close_plan(con, todo, now, margin=15 * 60):
    """A close-only plan identical to the pending one is REUSED instead of recorded again every 15 minutes: the
    owner's Approve button stays valid and Telegram is not flooded. Entry plans always use fresh prices."""
    if not todo or any(o["action"] != "CLOSE" for o in todo):
        return None
    prev = ex.latest_plan(con)
    if prev is None or prev["state"] != "PLANNED" or now - prev["created_at"] > ex.PLAN_MAX_AGE - margin:
        return None
    rows = con.execute("SELECT action, coin, position_id FROM orders WHERE plan_id=? ORDER BY coin",
                       (prev["id"],)).fetchall()
    want = sorted((o["action"], o["coin"], o.get("position_id")) for o in todo)
    return prev["id"] if [tuple(r) for r in rows] == want else None


# ---------- plan

def plan(client=None, con=None):
    client = client or Client()
    con = con or ex.db()
    now = int(time.time())
    last_closed = now // DAY * DAY - DAY
    ex.recover_ownership(con, client)
    positions = client.positions()
    owned_ids = ex.owned_ids(con)
    st = read_state()
    gone = [r for r in con.execute("SELECT position_id, coin FROM owned WHERE closed_at IS NULL").fetchall()
            if r["position_id"] not in {p["id"] for p in positions}]
    if gone:                                   # closed without our CLOSE => stop hit, but only if Mudrex confirms
        closed = set(ex.history_by_owned(con, client.history("positions")[0]))
        for r in gone:
            if r["position_id"] in closed:
                con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (now, r["position_id"]))
                st["armed"][r["coin"]] = False
    owned = {p["symbol"].removesuffix("USDT"): {"id": p["id"], "side": p.get("order_type", "LONG")}
             for p in positions if p["id"] in owned_ids}
    manual = {p["symbol"].removesuffix("USDT") for p in positions if p["id"] not in owned_ids}
    rate = ex.hedge_rate(client, positions)
    listing = pick_coins.listing() or []
    selection = live_universe.select_rows(listing)
    cycle = trade_policy.cycle_id(now)
    snapshot = live_universe.archive(con, cycle, selection, now)
    selection = live_universe.replay(selection, snapshot)
    entry_basket = selection.coins
    listing_by_coin = {str(r.get("symbol") or "").removesuffix("USDT"): r for r in listing}
    management_rows = list(selection.rows)
    management_missing = []
    for coin in sorted(set(owned) - set(entry_basket)):
        row = listing_by_coin.get(coin)
        try:
            s1.specs_from_listing([row] if row else [], [coin])
        except (KeyError, TypeError, ValueError):
            management_missing.append(coin)
        else:
            management_rows.append(row)
    basket = entry_basket + tuple(c for c in sorted(owned) if c not in entry_basket and c not in management_missing)
    if not basket:
        raise RuntimeError("no positively verified, liquid crypto futures are eligible")
    specs = s1.specs_from_listing(management_rows, basket)
    uni = {c: [x for x in data.load(2400, f"{c}/USDT", "1d", DAY) if x[0] <= last_closed] for c in basket}
    closes = {c: {x[0]: x[4] for x in cs} for c, cs in uni.items()}
    ctx = pf.prepare(uni, pf.zarattini, **s1.SIGNAL_KW)
    _, atrs = pf.trade_lookups(uni)
    btc = [x for x in data.load(2400, "BTC/USDT", "1d", DAY) if x[0] <= last_closed]
    pnl_unknown = None
    try:
        bot_eq = ex.bot_equity(con, client, positions, rate or config.INR_PER_USDT)
    except ex.PnlUnknown as e:
        bot_eq, pnl_unknown = float(s1.CAPITAL_CAP_INR), str(e)
    caps = ex.caps_state(con, bot_eq, ex.unrealized_inr(con, positions, rate or config.INR_PER_USDT),
                         trusted=pnl_unknown is None, open_price_at=client.open_price_at)
    targets = s1.targets(ctx, closes, last_closed, s1.sizing_equity(bot_eq), specs, btc, basket=basket)
    for c in basket:
        if not targets.get(c, 0):
            st["armed"][c] = True
    mood = s1.btc_mood(btc, last_closed)
    import telegram_bot
    blocked = "STOP file present" if os.path.exists(ex.STOP_PATH) else trade_policy.migration_block_reason()
    blocked = blocked or ("no positively verified, liquid crypto futures are eligible" if not entry_basket else
                          "BTC market-mood data missing" if mood is None else
                          "Telegram is not fully configured" if not telegram_bot.enabled() else
                          "performance guard tripped" if ex.guard_tripped() else
                          f"bot P&L unconfirmed ({pnl_unknown})" if pnl_unknown else
                          f"daily {caps['hit']} cap hit" if caps["hit"] else
                          "today's starting balance unknown (not watched at midnight)" if not caps["baseline_ok"] else
                          "no recent INR hedge rate from Mudrex" if not rate else None)
    bad = bad_data(uni, last_closed)
    try:
        realized_pnls = ex.cycle_realized_pnls(con)
        active_risks = ex.active_stop_risks(con, positions, owned_ids)
    except ex.PnlUnknown as e:
        realized_pnls, active_risks = (), ()
        blocked = blocked or f"collective risk is unknown ({e})"
    completed_sets, unresolved_sets = ex.cycle_set_counts(con, cycle)
    if unresolved_sets:
        blocked = blocked or "an earlier set is unresolved; reconciling it before any new set"
    # Sets 1-3 are autonomous; above that ONE set per plan, so each extra set is bound to one Telegram approval.
    needs_approval = completed_sets >= trade_policy.AUTONOMOUS_SETS_PER_CYCLE
    available_slots = (1 if trade_policy.HUMAN_OVERRIDE_ABOVE_MAX else 0) if needs_approval else         trade_policy.MAX_SETS_PER_CYCLE - completed_sets
    if STRATEGY == "S4":
        D = dict(coins=[], f={}, trend={}, btc=btc)
        last_hour = now // HOUR * HOUR - HOUR
        for c in entry_basket:
            if c in manual or c in bad:
                continue
            try:
                cs = data.load(s4.HOURLY_DAYS_LIVE, f"{c}/USDT", "1h", HOUR)
            except Exception:                                   # noqa: BLE001 - no data: no decision for it
                continue
            if cs and cs[-1][0] == last_hour:                   # stale hourly data: no decision for this coin
                D["f"][c], D["trend"][c] = s4.coin_features(cs), ctx["sig"].get(c, {})
                D["coins"].append(c)
        opened_at = {r["position_id"]: r["ex_opened_at"] or r["opened_at"]
                     for r in con.execute("SELECT position_id, opened_at, ex_opened_at FROM owned")}
        s4_ids = s4_position_ids(con)
        orders = s4_orders(owned, opened_at, D, {c: atrs[c].get(last_closed) for c in basket}, specs, now, blocked,
                           rate or config.INR_PER_USDT, bot_eq, realized_pnls, active_risks, available_slots,
                           s4_ids=s4_ids)
    else:
        candidate_keys = [(c, "LONG" if targets.get(c, 0) > 0 else "SHORT")
                          for c in entry_basket if targets.get(c, 0)]
        learned = bounded_learning.decisions_from_db(con, candidate_keys, now)
        orders = build_orders(targets, owned, manual, {c: closes[c].get(last_closed) for c in basket},
                              {c: atrs[c].get(last_closed) for c in basket}, specs, bot_eq, st["armed"], blocked,
                              rate or config.INR_PER_USDT, bad, realized_pnls, active_risks, available_slots,
                              completed_sets=completed_sets, basket=basket, model_decisions=learned,
                              entry_basket=entry_basket)
    orders.extend(dict(action="HOLD", coin=c,
                       reason="held coin is absent from usable listing data; exchange bracket remains monitored")
                  for c in management_missing)
    todo = [o for o in orders if o["action"] in ("OPEN", "CLOSE")]
    decision = time.strftime("%Y-%m-%d", time.gmtime(last_closed))
    plan_id = reuse_close_plan(con, todo, now) or (ex.record_plan(con, decision, todo, dict(orders=orders))
                                                   if todo else None)
    if not todo:
        ex.supersede_pending(con, "superseded by a newer plan with nothing to do")
    p = dict(plan_id=plan_id, created_at=now, decision_day=decision, strategy=s4.NAME if STRATEGY == "S4" else s1.NAME,
             mood_ok=mood is True, bot_equity_inr=round(bot_eq, 2), caps=caps,
             hedge_rate=rate, live_enabled=ex.live_enabled(), blocked=blocked, orders=orders,
             cycle=cycle, completed_sets=completed_sets, available_slots=available_slots,
             needs_approval=needs_approval and bool(plan_id) and any(o["action"] == "OPEN" for o in todo))
    write_json(PLAN_PATH, p)
    write_json(STATE_PATH, st)

    print(f"\n{STRATEGY} plan {plan_id or '(none)'} (daily inputs from the {decision} close)")
    print(f"Bot equity Rs {bot_eq:,.2f} (allocation Rs {s1.CAPITAL_CAP_INR:,} + bot P&L); "
          f"today {caps['pnl']:+,.0f} vs caps +/-{caps['cap']:,.0f}; INR/USDT {rate or 'UNKNOWN'}")
    print(f"Market regime (BTC vs 200-day average): "
          f"{'LONG' if mood else 'UNKNOWN: no new entries' if mood is None else 'SHORT'}")
    if manual:
        print(f"Manual positions (bot will NOT touch these coins): {', '.join(sorted(manual))}")
    if blocked:
        print(f"NEW ENTRIES BLOCKED: {blocked}")
    if not ex.live_enabled():
        print("LIVE_TRADING_ENABLED is false: execute will refuse.")
    for o in orders:
        extra = (f" {o['side']} ~Rs {o['notional_inr']:,.0f} at {o['leverage']}x "
                 f"(SL ~{o['est_stop']}, TP ~{o['est_target']}, risk Rs {o['planned_risk_inr']:,.0f})"
                 if o["action"] == "OPEN" else "")
        print(f"  {o['action']:<5} {o['coin']:<5} {o['reason']}{extra}")
    if not todo:
        print("  nothing to do today")
    return p


# ---------- execute / reconcile

def execute(client=None, con=None, confirm=input):
    con = con or ex.db()
    row = ex.latest_plan(con)
    if row is None or row["state"] != "PLANNED":
        sys.exit("no pending plan: run  python live_trader.py plan  first")
    why = ex.preflight()
    if why:
        sys.exit(f"refused: {why}")
    orders = con.execute("SELECT * FROM orders WHERE plan_id=? ORDER BY seq", (row["id"],)).fetchall()
    print(f"\nREAL ORDERS on your Mudrex INR futures wallet, plan {row['id']} ({row['decision_day']}):")
    for o in orders:
        print(f"  {o['action']} {o['coin']}" + (f" ~Rs {o['planned_notional_inr']:,.0f}" if o["action"] == "OPEN" else ""))
    if confirm("\nType YES to place these real orders: ").strip() != "YES":
        sys.exit("aborted: nothing placed")
    import telegram_bot
    def telegram_alert(msg):
        delivered = telegram_bot.send(msg) is not None
        print(msg)
        return delivered
    final, summary = ex.execute(con, client or Client(), row["id"], "terminal YES", alert=telegram_alert)
    print(f"\nplan {row['id']}: {final}\n  " + "\n  ".join(summary))
    print("Check the Mudrex app: each new position should show its stop-loss.")
    return final


def reconcile(client=None, con=None):
    import telegram_bot
    def telegram_alert(msg):
        delivered = telegram_bot.send(msg) is not None
        print(msg)
        return delivered
    ex.reconcile(con or ex.db(), client or Client(), alert=telegram_alert)
    print("reconcile done")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"plan": plan, "execute": execute, "reconcile": reconcile}.get(cmd, lambda: sys.exit(__doc__))()
