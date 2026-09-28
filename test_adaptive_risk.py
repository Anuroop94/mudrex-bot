import math

import unittest

import adaptive_risk
import trade_policy
from adaptive_risk import (
    DAILY_RISK_LIMIT_INR,
    gross_realized_losses,
    plan_trade,
    remaining_daily_risk,
)


def plan(**overrides):
    args = dict(side="LONG", entry=100.0, atr=1.0, confidence=1.0,
                exchange_max_leverage=5.0, candidate_cost_buffer_inr=5.0,
                inr_per_price_unit=1.0, qty_step=0.01, min_qty=0.01,
                min_notional_inr=1.0)
    args.update(overrides)
    return plan_trade(**args)


class AdaptiveRiskTests(unittest.TestCase):
  def test_daily_budget_499_500_boundary_and_cost_buffer(self):
    self.assertAlmostEqual(remaining_daily_risk([-499.0], []), 1.0)
    self.assertEqual(remaining_daily_risk([-500.0], []), 0)
    self.assertAlmostEqual(remaining_daily_risk([], [], 1.0), 499.0)
    self.assertIsNone(plan(realized_pnls=[-499.0]))  # buffer leaves no candidate room
    self.assertIsNotNone(plan(realized_pnls=[-494.0]))


  def test_profits_do_not_replenish_gross_loss_capacity(self):
    self.assertEqual(gross_realized_losses([-100.0, 250.0, -25.0]), 125.0)
    self.assertEqual(remaining_daily_risk([-100.0, 10_000.0], []), 400.0)


  def test_losses_active_stops_candidate_and_cost_fit_cycle_cap(self):
    result = plan(realized_pnls=[-100.0], active_stop_risks=[100.0],
                  candidate_cost_buffer_inr=5.0)
    self.assertIsNotNone(result)
    self.assertLessEqual(100 + 100 + result.planned_stop_risk_inr, DAILY_RISK_LIMIT_INR)
    self.assertIsNone(plan(realized_pnls=[-250.0], active_stop_risks=[250.0],
                           candidate_cost_buffer_inr=5.0))


  def test_bracket_geometry_and_minimum_reward_risk(self):
   for side in ("LONG", "SHORT"):
    result = plan(side=side)
    self.assertIsNotNone(result)
    if side == "LONG":
        self.assertLess(result.stop_loss, 100)
        self.assertGreater(result.take_profit, 100)
    else:
        self.assertLess(result.take_profit, 100)
        self.assertGreater(result.stop_loss, 100)
    self.assertGreaterEqual(abs(result.take_profit - 100), 1.5 * abs(100 - result.stop_loss))


  def test_higher_volatility_widens_stop_and_reduces_leverage_and_quantity(self):
    moderate = plan(atr=3.0)  # 3% ATR/price, 2 ATR stop
    high = plan(atr=6.0)      # 6% ATR/price, 2.5 ATR stop
    self.assertTrue(moderate and high)
    self.assertEqual(high.volatility_tier, "high")
    self.assertLess(high.leverage, moderate.leverage)
    self.assertLess(high.quantity, moderate.quantity)
    with self.assertRaisesRegex(ValueError, "extreme volatility"):
        plan(atr=9.0)


  def test_leverage_setting_never_changes_quantity_or_stop_risk(self):
    low = plan(exchange_max_leverage=1.0)
    high = plan(exchange_max_leverage=5.0)
    self.assertTrue(low and high)
    self.assertNotEqual(low.leverage, high.leverage)
    self.assertEqual(low.quantity, high.quantity)
    self.assertEqual(low.planned_stop_risk_inr, high.planned_stop_risk_inr)


  def test_non_finite_candidate_input_fails_closed(self):
   for field, value in [("entry", math.nan), ("atr", math.inf), ("confidence", -math.inf),
                        ("exchange_max_leverage", math.nan), ("candidate_cost_buffer_inr", math.inf)]:
    with self.subTest(field=field), self.assertRaisesRegex(ValueError, "finite"):
        plan(**{field: value})


  def test_under_minimum_quantity_is_rejected(self):
    self.assertIsNone(plan(atr=1.0, inr_per_price_unit=1_000_000.0, min_qty=1.0))


  def test_low_confidence_only_reduces_risk_and_leverage(self):
    confident = plan(confidence=1.0)
    uncertain = plan(confidence=0.6)
    self.assertTrue(confident and uncertain)
    self.assertLessEqual(uncertain.planned_stop_risk_inr, confident.planned_stop_risk_inr)
    self.assertLessEqual(uncertain.leverage, confident.leverage)


  def test_higher_hourly_funding_reduces_quantity_and_is_in_risk_reserve(self):
    low = plan(funding_fee_perc_hour=0.0)
    high = plan(funding_fee_perc_hour=0.001, expected_hold_hours=24)
    self.assertTrue(low and high)
    self.assertLess(high.quantity, low.quantity)
    pure_stop_risk = high.quantity * abs(100.0 - high.stop_loss)
    self.assertGreater(high.planned_stop_risk_inr, pure_stop_risk + 5.0)
    self.assertLessEqual(high.planned_stop_risk_inr, adaptive_risk.MAX_CANDIDATE_RISK_INR)


  def test_daily_risk_limit_comes_from_trade_policy(self):
    self.assertEqual(adaptive_risk.DAILY_RISK_LIMIT_INR, trade_policy.DAILY_LOSS_LIMIT_INR)


  def test_telegram_copy_uses_policy_loss_and_autonomy_limits(self):
    import telegram_bot

    old_loss = trade_policy.DAILY_LOSS_LIMIT_INR
    old_autonomous = trade_policy.AUTONOMOUS_SETS_PER_CYCLE
    try:
        trade_policy.DAILY_LOSS_LIMIT_INR = 777.0
        trade_policy.AUTONOMOUS_SETS_PER_CYCLE = 4
        p = dict(plan_id="p1", live_enabled=True, completed_sets=0,
                 orders=[dict(action="OPEN", coin="BTC", side="LONG", leverage=2,
                              notional_inr=100, est_stop=99, est_target=102,
                              planned_risk_inr=10)])
        text, buttons = telegram_bot.plan_message(p)
        self.assertIn("daily cap ₹777", text)
        self.assertIn("sets 1-4", text)
        self.assertIsNone(buttons)
    finally:
        trade_policy.DAILY_LOSS_LIMIT_INR = old_loss
        trade_policy.AUTONOMOUS_SETS_PER_CYCLE = old_autonomous


  def test_weak_signal_is_vetoed_not_forced_to_meet_trade_target(self):
    self.assertIsNone(plan(confidence=0.54))


if __name__ == "__main__":
    unittest.main()
