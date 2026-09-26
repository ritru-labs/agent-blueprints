import unittest

from verify_candidate import check_passed


class VerifierGateTests(unittest.TestCase):
    def test_zero_test_success_exit_is_not_a_pass(self):
        empty = {"exit_code": 0, "test_count": 0, "ok": True, "timed_out": False}
        self.assertFalse(check_passed(empty, minimum_tests=1))

    def test_trusted_count_and_timeout_are_required(self):
        trusted = {"exit_code": 0, "test_count": 5, "ok": True, "timed_out": False}
        self.assertTrue(check_passed(trusted, minimum_tests=5, exact_tests=5))
        self.assertFalse(check_passed({**trusted, "test_count": 4}, minimum_tests=5, exact_tests=5))
        self.assertFalse(check_passed({**trusted, "timed_out": True}, minimum_tests=5, exact_tests=5))


if __name__ == "__main__":
    unittest.main()
