import unittest

from envi.cancellation import CancellationToken
from envi.errors import CancelledError


class CancellationTests(unittest.TestCase):
    def test_registered_callbacks_fire_once_and_unregister_works(self):
        token = CancellationToken()
        calls = []
        token.register(lambda: calls.append("kept"))
        unregister = token.register(lambda: calls.append("removed"))
        unregister()
        token.cancel()
        token.cancel()
        self.assertEqual(["kept"], calls)
        self.assertTrue(token.is_cancelled)
        with self.assertRaises(CancelledError):
            token.check()

    def test_registration_after_cancel_runs_immediately(self):
        token = CancellationToken()
        token.cancel()
        calls = []
        token.register(lambda: calls.append("closed"))
        self.assertEqual(["closed"], calls)

    def test_failing_cleanup_does_not_prevent_other_cleanups(self):
        token = CancellationToken()
        calls = []

        def fail():
            raise OSError("fake socket close failure")

        token.register(fail)
        token.register(lambda: calls.append("closed"))
        token.cancel()
        self.assertEqual(["closed"], calls)

    def test_unregister_is_idempotent_for_repeated_callback(self):
        token = CancellationToken()
        calls = []

        def callback():
            calls.append("closed")

        unregister = token.register(callback)
        token.register(callback)
        unregister()
        unregister()
        token.cancel()
        self.assertEqual(["closed"], calls)

    def test_failing_cleanup_after_cancel_is_nonfatal(self):
        token = CancellationToken()
        token.cancel()

        def fail():
            raise OSError("fake socket close failure")

        unregister = token.register(fail)
        unregister()
        self.assertTrue(token.is_cancelled)


if __name__ == "__main__":
    unittest.main()
