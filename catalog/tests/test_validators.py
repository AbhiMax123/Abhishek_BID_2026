"""Pure unit tests for validators.py (no database needed)."""
import unittest
from decimal import Decimal

from catalog import validators as v


def row(**kw):
    base = {"sku": "ab-1", "name": "Widget", "price": "10.50", "category": "Books"}
    base.update(kw)
    return v.validate_row(base)


class HeaderTests(unittest.TestCase):
    def test_ok_headers_with_bom_and_case(self):
        headers = v.normalize_headers(["\ufeffSKU", " Name ", "PRICE", "Category", "Stock"])
        self.assertEqual(headers, ["sku", "name", "price", "category", "stock"])
        self.assertEqual(v.check_headers(headers), [])

    def test_missing_required_column(self):
        problems = v.check_headers(["sku", "name", "category"])
        self.assertIn("price", problems[0])

    def test_duplicate_columns(self):
        problems = v.check_headers(["sku", "name", "price", "category", "price"])
        self.assertTrue(any("Duplicate" in p for p in problems))

    def test_empty_header(self):
        self.assertTrue(v.check_headers([""]))


class RowTests(unittest.TestCase):
    def test_valid_row_is_normalised(self):
        r = row()
        self.assertTrue(r.ok)
        self.assertEqual((r.sku, r.price, r.category), ("AB-1", Decimal("10.50"), "Books"))

    def test_negative_price(self):
        r = row(price="-5")
        self.assertEqual([i["field"] for i in r.issues], ["price"])
        self.assertIn("negative", r.issues[0]["message"])

    def test_bad_prices(self):
        for bad in ["abc", "1e3", "1_000", "NaN", "Infinity", "", "1.234", "12345678901"]:
            self.assertFalse(row(price=bad).ok, bad)

    def test_good_prices(self):
        for good, expected in [("0", "0.00"), ("7", "7.00"), ("7.5", "7.50"), ("7.500", "7.50"), (".5", "0.50")]:
            self.assertEqual(row(price=good).price, Decimal(expected), good)

    def test_all_issues_collected(self):
        r = row(sku="", name="", price="x", category="")
        self.assertEqual({i["field"] for i in r.issues}, {"sku", "name", "price", "category"})

    def test_sku_rules(self):
        self.assertFalse(row(sku="has space").ok)
        self.assertFalse(row(sku="A" * 65).ok)
        self.assertTrue(row(sku="a.b_c-1/2").ok)

    def test_stock_and_bool(self):
        self.assertEqual(row(stock="5").stock, 5)
        self.assertIsNone(row(stock="").stock)
        self.assertFalse(row(stock="-1").ok)
        self.assertFalse(row(stock="1.5").ok)
        self.assertIs(row(is_active="YES").is_active, True)
        self.assertIs(row(is_active="0").is_active, False)
        self.assertFalse(row(is_active="maybe").ok)


if __name__ == "__main__":
    unittest.main()
