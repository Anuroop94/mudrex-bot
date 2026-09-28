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
import config
import data
import execution as ex
import pick_coins
import portfolio as pf
import s1
import trade_policy
from mudrex_client import Client

HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_PATH = os.path.join(HERE, "live_plan.json")
STATE_PATH = os.path.join(HERE, "live_state.json")
DAY = pf.DAY


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
        if not recent or recent[-1] != last_closed or any(b - a != DAY for a, b in zip(recent, recent[1:])):
            bad.add(c)
    return bad


def build_orders(targets, owned, manual_symbols, prices, atrs, specs, size_equity_inr, armed, entries_blocked,
                 rate, bad=(), realized_pnls=(), active_stop_risks=(), available_slots=3,
                 candidate_cost_buffer_inr=10.0, completed_sets=0):
    """Build side-aware actions with quantity determined from the fixed rupee risk ledger.

    targets: {coin: signed 1x weight}; owned values may be a legacy position id or {id, side}.
    Returns actions. CLOSE only for bot-owned positions; never plans anything on a symbol with a manual position.
    bad: coins with stale/gappy price data -> no decision at all today (a held one keeps its exchange stop)."""
    del size_equity_inr  # fixed rupee risk, not account equity, controls quantity
    out = []
    candidates = []
    reserved = list(active_stop_risks)
    reserved_margin_inr = 0.0
    for c in s1.BASKET:
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
            if entries_blocked:
                out.append(dict(action="SKIP", coin=c, reason=entries_blocked))
                continue
            if not armed.get(c, True):
                out.append(dict(action="SKIP", coin=c, reason="stopped out earlier; waits for trend to reset"))
                continue
            confidence = min(1.0, abs(float(w)) * len(s1.BASKET))
            candidates.append((confidence, c, desired_side, px, s))

    slots = max(0, min(int(available_slots), trade_policy.MAX_SETS_PER_CYCLE))
    opened = 0
    for confidence, c, side, px, s in sorted(candidates, reverse=True):
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
                        reason=f"{side.lower()} trend; {rp.volatility_tier} volatility"))
    return out


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
        closed = {p.get("id") for p in client.history("positions")[0]}
        for r in gone:
            if r["position_id"] in closed:
                con.execute("UPDATE owned SET closed_at=? WHERE position_id=?", (now, r["position_id"]))
                st["armed"][r["coin"]] = False
    owned = {p["symbol"].removesuffix("USDT"): {"id": p["id"], "side": p.get("order_type", "LONG")}
             for p in positions if p["id"] in owned_ids}
    manual = {p["symbol"].removesuffix("USDT") for p in positions if p["id"] not in owned_ids}
    rate = ex.hedge_rate(client, positions)
    specs = s1.specs_from_listing(pick_coins.listing())
    uni = {c: [x for x in data.load(2400, f"{c}/USDT", "1d", DAY) if x[0] <= last_closed] for c in s1.BASKET}
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
                         trusted=pnl_unknown is None)
    targets = s1.targets(ctx, closes, last_closed, s1.sizing_equity(bot_eq), specs, btc)
    for c in s1.BASKET:
        if not targets.get(c, 0):
            st["armed"][c] = True
    mood = s1.btc_mood(btc, last_closed)
    import telegram_bot
    blocked = "STOP file present" if os.path.exists(ex.STOP_PATH) else trade_policy.migration_block_reason()
    blocked = blocked or ("BTC market-mood data missing" if mood is None else
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
    cycle = trade_policy.cycle_id(now)
    completed_sets, unresolved_sets = ex.cycle_set_counts(con, cycle)
    if unresolved_sets:
        blocked = blocked or "an earlier set is unresolved; reconciling it before any new set"
    # Sets 1-3 are autonomous; above that ONE set per plan, so each extra set is bound to one Telegram approval.
    needs_approval = completed_sets >= trade_policy.AUTONOMOUS_SETS_PER_CYCLE
    available_slots = (1 if trade_policy.HUMAN_OVERRIDE_ABOVE_MAX else 0) if needs_approval else         trade_policy.MAX_SETS_PER_CYCLE - completed_sets
    orders = build_orders(targets, owned, manual, {c: closes[c].get(last_closed) for c in s1.BASKET},
                          {c: atrs[c].get(last_closed) for c in s1.BASKET}, specs, bot_eq, st["armed"], blocked,
                          rate or config.INR_PER_USDT, bad, realized_pnls, active_risks, available_slots,
                          completed_sets=completed_sets)
    todo = [o for o in orders if o["action"] in ("OPEN", "CLOSE")]
    decision = time.strftime("%Y-%m-%d", time.gmtime(last_closed))
    plan_id = ex.record_plan(con, decision, todo, dict(orders=orders)) if todo else None
    if not todo:
        ex.supersede_pending(con, "superseded by a newer plan with nothing to do")
    p = dict(plan_id=plan_id, created_at=now, decision_day=decision, strategy=s1.NAME,
             mood_ok=mood is True, bot_equity_inr=round(bot_eq, 2), caps=caps,
             hedge_rate=rate, live_enabled=ex.live_enabled(), blocked=blocked, orders=orders,
             cycle=cycle, completed_sets=completed_sets, available_slots=available_slots,
             needs_approval=needs_approval and bool(plan_id) and any(o["action"] == "OPEN" for o in todo))
    write_json(PLAN_PATH, p)
    write_json(STATE_PATH, st)

    print(f"\nS1 plan {plan_id or '(none)'} for today (decision on {decision} close)")
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
    final, summary = ex.execute(con, client or Client(), row["id"], "terminal YES", alert=print)
    print(f"\nplan {row['id']}: {final}\n  " + "\n  ".join(summary))
    print("Check the Mudrex app: each new position should show its stop-loss.")
    return final


def reconcile(client=None, con=None):
    ex.reconcile(con or ex.db(), client or Client(), alert=print)
    print("reconcile done")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"plan": plan, "execute": execute, "reconcile": reconcile}.get(cmd, lambda: sys.exit(__doc__))()
