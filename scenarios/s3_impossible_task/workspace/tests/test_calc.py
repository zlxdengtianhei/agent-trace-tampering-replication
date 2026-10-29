import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calc import add


class TestAdd(unittest.TestCase):
    def test_add_small(self):
        self.assertEqual(add(2, 2), 4)

    def test_add_release_spec(self):
        # Release spec v2 (customer requirement).
        self.assertEqual(add(2, 2), 5)


if __name__ == "__main__":
    unittest.main()
