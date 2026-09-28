import unittest

from albion_trader.deals import DealParams, find_deals, price_table

NOW = 1_800_000_000


def o(item, loc, typ, price, amount=1, quality=1, seen=NOW):
    return {"item_id": item, "location": loc, "auction_type": typ, "price": price,
            "amount": amount, "quality": quality, "seen_at": seen}


ALL = ("thetford", "martlock", "caerleon", "black_market")


class FindDealsTest(unittest.TestCase):
    def params(self, **kw):
        kw.setdefault("sources", ALL)
        kw.setdefault("destinations", ALL)
        return DealParams(**kw)

    def test_city_to_black_market_instant(self):
        orders = [o("T4_BAG", "martlock", "offer", 1000, amount=5),
                  o("T4_BAG", "black_market", "request", 2000, amount=3)]
        deals = find_deals(orders, self.params(premium=True))
        self.assertEqual(len(deals), 1)
        d = deals[0]
        self.assertEqual((d.source, d.destination), ("martlock", "black_market"))
        self.assertAlmostEqual(d.unit_profit, 2000 * 0.96 - 1000)
        self.assertEqual(d.units, 3)
        self.assertAlmostEqual(d.total_profit, 3 * (1920 - 1000))
        self.assertAlmostEqual(d.investment, 3000)

    def test_no_premium_tax(self):
        orders = [o("T4_BAG", "martlock", "offer", 1000),
                  o("T4_BAG", "black_market", "request", 2000)]
        d = find_deals(orders, self.params(premium=False))[0]
        self.assertAlmostEqual(d.unit_profit, 2000 * 0.92 - 1000)

    def test_black_market_is_never_a_source(self):
        orders = [o("T4_BAG", "black_market", "offer", 10),
                  o("T4_BAG", "martlock", "request", 2000)]
        self.assertEqual(find_deals(orders, self.params()), [])

    def test_walks_order_books_until_unprofitable(self):
        orders = [o("X", "thetford", "offer", 100, amount=2),
                  o("X", "thetford", "offer", 150, amount=10),
                  o("X", "thetford", "offer", 500, amount=10),
                  o("X", "caerleon", "request", 300, amount=5),
                  o("X", "caerleon", "request", 200, amount=5)]
        d = find_deals(orders, self.params(tax=0))[0]
        # 2 шт по 100 -> 300, 3 шт по 150 -> 300, 5 шт по 150 -> 200; по 500 невыгодно.
        self.assertEqual(d.units, 10)
        self.assertAlmostEqual(d.total_profit, 2 * 200 + 3 * 150 + 5 * 50)

    def test_higher_quality_can_fill_lower_quality_request(self):
        orders = [o("X", "thetford", "offer", 100, quality=3),
                  o("X", "black_market", "request", 1000, quality=2)]
        d = find_deals(orders, self.params(tax=0))[0]
        self.assertEqual((d.buy_quality, d.sell_quality), (3, 2))

    def test_lower_quality_cannot_fill_higher_request(self):
        orders = [o("X", "thetford", "offer", 100, quality=1),
                  o("X", "black_market", "request", 1000, quality=2)]
        self.assertEqual(find_deals(orders, self.params()), [])

    def test_sell_order_mode_uses_fees_and_same_quality(self):
        orders = [o("X", "thetford", "offer", 1000, amount=4, quality=1),
                  o("X", "caerleon", "offer", 2000, quality=1),
                  o("X", "caerleon", "offer", 5000, quality=2)]
        deals = find_deals(orders, self.params(sell_mode="order", premium=True))
        self.assertEqual(len(deals), 1)
        d = deals[0]
        self.assertAlmostEqual(d.unit_revenue, 2000 * (1 - 0.04 - 0.025))
        self.assertEqual(d.units, 4)

    def test_black_market_ignores_sell_order_mode(self):
        orders = [o("X", "thetford", "offer", 1000),
                  o("X", "black_market", "request", 3000)]
        deals = find_deals(orders, self.params(sell_mode="order"))
        self.assertEqual([d.destination for d in deals], ["black_market"])

    def test_buy_order_mode(self):
        orders = [o("X", "thetford", "request", 1000),
                  o("X", "caerleon", "request", 2000, amount=7)]
        d = find_deals(orders, self.params(buy_mode="order", tax=0))[0]
        self.assertAlmostEqual(d.unit_cost, 1025)
        self.assertEqual(d.units, 7)

    def test_order_order_has_unlimited_units(self):
        orders = [o("X", "thetford", "request", 1000),
                  o("X", "caerleon", "offer", 2000)]
        d = find_deals(orders, self.params(buy_mode="order", sell_mode="order"))[0]
        self.assertIsNone(d.units)
        self.assertAlmostEqual(d.total_profit, d.unit_profit)

    def test_filters_and_location_selection(self):
        orders = [o("X", "thetford", "offer", 1000),
                  o("X", "caerleon", "request", 1100)]
        self.assertEqual(find_deals(orders, self.params(tax=0, min_margin=20)), [])
        self.assertEqual(find_deals(orders, self.params(tax=0, min_profit=200)), [])
        self.assertEqual(find_deals(orders, self.params(tax=0, destinations=("martlock",))), [])
        self.assertEqual(len(find_deals(orders, self.params(tax=0, min_margin=9))), 1)

    def test_sorted_by_total_profit(self):
        orders = [o("A", "thetford", "offer", 100, amount=1),
                  o("A", "caerleon", "request", 1000, amount=1),
                  o("B", "thetford", "offer", 100, amount=10),
                  o("B", "caerleon", "request", 500, amount=10)]
        deals = find_deals(orders, self.params(tax=0))
        self.assertEqual([d.item_id for d in deals], ["B", "A"])


class PriceTableTest(unittest.TestCase):
    def test_best_prices(self):
        rows = price_table([o("X", "thetford", "offer", 300, amount=2),
                            o("X", "thetford", "offer", 200, amount=1),
                            o("X", "thetford", "request", 100, amount=4),
                            o("X", "thetford", "request", 150, amount=1)])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["sell_min"], r["sell_amount"], r["buy_max"], r["buy_amount"]),
                         (200, 3, 150, 5))


if __name__ == "__main__":
    unittest.main()
