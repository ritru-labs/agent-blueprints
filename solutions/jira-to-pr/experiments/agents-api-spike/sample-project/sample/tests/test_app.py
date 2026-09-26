import unittest

from sample.app import identity


class IdentityTests(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(identity(7), 7)


if __name__ == "__main__":
    unittest.main()
