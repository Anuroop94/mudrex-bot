import unittest

from fake_mudrex import FakeMudrex
from mudrex_client import Client


class MudrexBracketTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMudrex({"BTC": 100.0})
        self.client = Client(base=self.fake.url, secret="test")

    def tearDown(self):
        self.fake.stop()

    def test_market_orders_send_and_read_back_long_short_brackets(self):
        for side, stop, target in (("LONG", "90", "120"), ("SHORT", "110", "80")):
            created = self.client.place_market("BTCUSDT", "0.1", f"test-{side}", side, stop, target)
            self.client.order_by_id(created["order_id"])
            pos = self.client.positions()[0]
            self.assertEqual(pos["order_type"], side)
            self.assertEqual(float(pos["stoploss"]["price"]), float(stop))
            self.assertTrue(pos["stoploss"]["order_id"])
            self.assertEqual(float(pos["takeprofit"]["price"]), float(target))
            self.assertTrue(pos["takeprofit"]["order_id"])
            body = self.fake.orders[f"test-{side}"]["_body"]
            self.assertEqual(body["order_type"], side)
            self.assertTrue(body["is_stoploss"] and body["is_takeprofit"])
            self.client.close_position(pos["id"])
            history, _ = self.client.history("positions")
            self.assertEqual(history[0]["position_type"], side)

    def test_wrong_side_and_liquidation_geometry_are_rejected(self):
        for cid, side, stop, target in (("bad-long", "LONG", "110", "90"),
                                        ("bad-short", "SHORT", "90", "110"),
                                        ("bad-liq", "SHORT", "150", "80")):
            created = self.client.place_market("BTCUSDT", "0.1", cid, side, stop, target)
            self.assertEqual(self.client.order_by_id(created["order_id"])["status"], "REJECTED")
        self.assertFalse(self.client.positions())

    def test_long_compatibility_wrapper(self):
        created = self.client.place_market_long("BTCUSDT", "0.1", "legacy-long", stop="90")
        self.client.order_by_id(created["order_id"])
        pos = self.client.positions()[0]
        self.assertEqual(pos["order_type"], "LONG")
        self.assertEqual(float(pos["stoploss"]["price"]), 90)
        self.assertEqual(float(pos["takeprofit"]["price"]), 0)


if __name__ == "__main__":
    unittest.main()
