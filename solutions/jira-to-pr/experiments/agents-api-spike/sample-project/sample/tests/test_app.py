import unittest

from sample.app import identity


class IdentityTests(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(identity(7), 7)


if __name__ == "__main__":
    unittest.main()

from sample.app import add

class AdditionTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, -3), -1)
