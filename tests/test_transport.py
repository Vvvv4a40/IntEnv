"""Cancellation/deadline races with fake HTTPSConnection: no sockets or timer threads."""

import unittest
from unittest.mock import MagicMock, patch

from envi.cancellation import CancellationToken
from envi.errors import AssistantError, CancelledError
from envi.providers.groq import StandardHttpTransport


class ManualTimer:
    def __init__(self, seconds, callback):
        self.seconds = seconds
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.socket = MagicMock()
        self.connection.sock = self.socket
        self.response = MagicMock()
        self.response.status = 200
        self.response.getheaders.return_value = [("Content-Type", "application/json")]
        self.response.read1.side_effect = [b"{}", b""]
        self.connection.getresponse.return_value = self.response
        self.timers = []
        self.token = CancellationToken()
        self.transport = StandardHttpTransport()

    def make_timer(self, seconds, callback):
        timer = ManualTimer(seconds, callback)
        self.timers.append(timer)
        return timer

    def send(self):
        with patch("envi.providers.groq.http.client.HTTPSConnection", return_value=self.connection), \
                patch("envi.providers.groq.ssl.create_default_context", return_value=object()), \
                patch("envi.providers.groq.Timer", side_effect=self.make_timer):
            return self.transport.send("https://api.groq.com/openai/v1/chat/completions",
                                       {"Authorization": "Bearer fake"}, b"{}", 30, self.token)

    def assert_timer_cleaned(self):
        self.assertEqual(1, len(self.timers))
        self.assertTrue(self.timers[0].started)
        self.assertTrue(self.timers[0].cancelled)
        self.assertTrue(self.timers[0].daemon)

    def test_success_disables_implicit_reconnect_and_cleans_timer_registration(self):
        result = self.send()
        self.assertEqual(200, result.status)
        self.assertEqual(b"{}", result.body)
        self.assertFalse(self.connection.auto_open)
        self.connection.connect.assert_called_once_with()
        self.connection.request.assert_called_once_with("POST", "/openai/v1/chat/completions", body=b"{}",
                                                         headers={"Authorization": "Bearer fake"})
        self.assert_timer_cleaned()
        closes = self.connection.close.call_count
        self.token.cancel()
        self.assertEqual(closes, self.connection.close.call_count)

    def test_cancel_during_connect_never_sends_request_or_reconnects(self):
        def connect():
            self.assertFalse(self.connection.auto_open)
            self.token.cancel()

        self.connection.connect.side_effect = connect
        with self.assertRaises(CancelledError):
            self.send()
        self.connection.connect.assert_called_once_with()
        self.connection.request.assert_not_called()
        self.connection.getresponse.assert_not_called()
        self.assert_timer_cleaned()

    def test_cancel_during_request_never_receives_or_reconnects(self):
        def request(*args, **kwargs):
            self.assertFalse(self.connection.auto_open)
            self.token.cancel()

        self.connection.request.side_effect = request
        with self.assertRaises(CancelledError):
            self.send()
        self.assertEqual(1, self.connection.connect.call_count)
        self.assertEqual(1, self.connection.request.call_count)
        self.connection.getresponse.assert_not_called()
        self.socket.shutdown.assert_called_once()
        self.assert_timer_cleaned()

    def test_timer_expiry_during_request_closes_socket_and_has_no_retry(self):
        def request(*args, **kwargs):
            self.timers[0].fire()
            raise OSError("fake closed socket")

        self.connection.request.side_effect = request
        with self.assertRaises(AssistantError) as caught:
            self.send()
        self.assertIn("вовремя", str(caught.exception))
        self.assertEqual(1, self.connection.connect.call_count)
        self.assertEqual(1, self.connection.request.call_count)
        self.connection.getresponse.assert_not_called()
        self.socket.shutdown.assert_called_once()
        self.assert_timer_cleaned()

    def test_timeout_waiting_for_response_has_no_retry(self):
        self.connection.getresponse.side_effect = TimeoutError("fake timeout")
        with self.assertRaises(AssistantError) as caught:
            self.send()
        self.assertIn("вовремя", str(caught.exception))
        self.assertEqual(1, self.connection.connect.call_count)
        self.assertEqual(1, self.connection.request.call_count)
        self.assertEqual(1, self.connection.getresponse.call_count)
        self.assert_timer_cleaned()

    def test_total_deadline_expired_after_upload_stops_before_response(self):
        with patch("envi.providers.groq.time.monotonic", side_effect=[10.0, 10.1, 41.0]):
            with self.assertRaises(AssistantError):
                self.send()
        self.connection.getresponse.assert_not_called()
        self.assertEqual(1, self.connection.request.call_count)
        self.assert_timer_cleaned()


if __name__ == "__main__":
    unittest.main()
