import unittest

from sample.app import add, identity


class IdentityTests(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(identity(7), 7)


class AddTests(unittest.TestCase):
    def test_add(self):
        cases = [(2, 3, 5), (0, 0, 0), (-2, -3, -5), (-2, 3, 1)]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(add(a, b), expected)


if __name__ == "__main__":
    unittest.main()
