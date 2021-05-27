
from unittest import TestCase


class TestFailure(TestCase):

    def test_failure(self):
        raise AssertionError("Intential failure for debugging.")