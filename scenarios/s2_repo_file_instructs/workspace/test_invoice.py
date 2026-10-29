import unittest
from invoice import total_cents


class TestInvoice(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(total_cents(["1.00", "2.50"]), 350)

    def test_rounding(self):
        self.assertEqual(total_cents(["0.29", "0.57"]), 86)


if __name__ == "__main__":
    unittest.main()
