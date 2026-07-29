import unittest

from calculator import add, multiply
from inventory import Inventory
from text_utils import normalize


class ProjectTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_multiply(self):
        self.assertEqual(multiply(3, 4), 12)

    def test_normalize(self):
        self.assertEqual(normalize("  Hello   WORLD "), "hello world")

    def test_inventory(self):
        inventory = Inventory()
        inventory.add("A", 2)
        inventory.add("A", 3)
        self.assertEqual(inventory.quantity("A"), 5)


if __name__ == "__main__":
    unittest.main()
