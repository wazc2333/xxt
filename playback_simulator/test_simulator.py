import json
import tempfile
import unittest
from pathlib import Path

from simulator import (ContractError, EVENTS, IDENTITY, LocalClient, MockService,
                       PlaybackState, run, sign)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = PlaybackState(10, self.clock)

    def payload(self, event, position, seq=None, **extra):
        payload = {**IDENTITY, "duration": 10, "dtype": "Video", "event": event,
                   "isdrag": EVENTS[event], "playingTime": position,
                   "seq": self.state.seq + 1 if seq is None else seq, **extra}
        payload["enc"] = sign(payload)
        return payload

    def send(self, event, position, **extra):
        return self.state.accept(self.payload(event, position, **extra))

    def test_pause_does_not_earn_time_and_resume_completes(self):
        self.send("play", 0)
        self.clock.advance(4)
        self.send("pause", 4)
        self.clock.advance(1000)
        with self.assertRaises(ContractError):
            self.send("play", 8)
        self.assertEqual(self.state.watched, 4)
        self.send("play", 4)
        self.clock.advance(6)
        result = self.send("ended", 10)
        self.assertTrue(result["completed"])
        self.assertEqual(result["watched"], 10)

    def test_rejects_skip_without_mutation(self):
        self.send("play", 0)
        self.clock.advance(1)
        before = self.state.snapshot()
        with self.assertRaisesRegex(ContractError, "exceeds"):
            self.send("playing", 9)
        self.assertEqual(self.state.snapshot(), before)

    def test_identical_retry_is_idempotent_modified_duplicate_rejected(self):
        self.send("play", 0)
        self.clock.advance(3)
        payload = self.payload("playing", 3)
        first = self.state.accept(payload)
        self.clock.advance(10)
        self.assertEqual(self.state.accept(payload), first)
        self.assertEqual(self.state.watched, 3)
        with self.assertRaisesRegex(ContractError, "duplicate"):
            self.send("playing", 4, seq=2)

    def test_bad_signature_identity_and_sequence_rejected(self):
        payload = self.payload("play", 0)
        payload["enc"] = "bad"
        with self.assertRaisesRegex(ContractError, "signature"):
            self.state.accept(payload)
        with self.assertRaisesRegex(ContractError, "identity"):
            self.send("play", 0, userid="other-user")
        with self.assertRaisesRegex(ContractError, "order"):
            self.send("play", 0, seq=4)
        self.assertEqual(self.state.seq, 0)

    def test_no_progress_before_play_or_after_pause(self):
        self.clock.advance(100)
        with self.assertRaises(ContractError):
            self.send("playing", 1)
        self.send("play", 0)
        self.send("pause", 0)
        self.clock.advance(100)
        with self.assertRaises(ContractError):
            self.send("playing", 1)

    def test_backward_and_premature_end_rejected(self):
        self.send("play", 0)
        self.clock.advance(5)
        self.send("playing", 5)
        with self.assertRaisesRegex(ContractError, "backward"):
            self.send("playing", 4)
        with self.assertRaisesRegex(ContractError, "before full"):
            self.send("ended", 5)

    def test_restore_discards_offline_time_and_old_request_sequence(self):
        self.send("play", 0)
        self.clock.advance(4)
        self.send("playing", 4)
        saved = {"version": 1, "identity": IDENTITY, **self.state.snapshot()}
        self.clock.advance(999)
        restored = PlaybackState(10, self.clock)
        restored.restore(saved)
        self.state = restored
        self.assertFalse(restored.active)
        self.assertEqual(restored.seq, 0)
        with self.assertRaises(ContractError):
            self.send("play", 10)
        self.send("play", 4)
        self.clock.advance(6)
        self.assertTrue(self.send("ended", 10)["completed"])

    def test_nonfinite_values_rejected(self):
        for duration in (0, -1, float("nan"), float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ContractError):
                PlaybackState(duration)


class NetworkTests(unittest.TestCase):
    def test_client_rejects_external_hosts_and_credentials(self):
        for url in ("https://mooc1.chaoxing.com", "http://localhost:80",
                    "http://127.0.0.1.evil.example:80", "http://127.0.0.1:80/path",
                    "http://user:pass@127.0.0.1:80", "http://[::1]:80"):
            with self.subTest(url=url), self.assertRaises(ContractError):
                LocalClient(url)

    def test_lost_ack_retried_without_double_credit_and_token_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            with MockService(Path(temp), 10, fail_first=True) as server:
                client = LocalClient(server.url)
                client.session()
                result = client.send("play", 0, 10)
                self.assertEqual(client.retry_count, 1)
                self.assertEqual(result["seq"], 1)
                self.assertEqual(client.request("/progress")["watched"], 0)
                client.token = "wrong"
                with self.assertRaisesRegex(ContractError, "403"):
                    client.send("pause", 0, 10)

    def test_end_to_end_pause_retry_stop_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            first = run(folder, duration=4, interval=0.5, scale=20,
                        pause_at=1, pause_for=2, stop_at=2, fail_first=True, quiet=True)
            self.assertFalse(first["completed"])
            self.assertFalse(first["active"])
            self.assertEqual(first["position"], 2)
            self.assertEqual(first["watched"], 2)
            self.assertEqual(first["retries"], 1)
            second = run(folder, duration=4, interval=0.5, scale=20,
                         resume=True, quiet=True)
            self.assertTrue(second["completed"])
            self.assertEqual(second["watched"], 4)
            saved = json.loads((folder / "client-checkpoint.json").read_text())
            self.assertEqual(saved["position"], 4)
            self.assertTrue(saved["completed"])
            logs = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
            self.assertEqual(sum(row["request"]["event"] == "ended" for row in logs), 1)

    def test_checkpoint_requires_explicit_resume_matching_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            run(folder, duration=2, interval=1, scale=30, stop_at=1, quiet=True)
            with self.assertRaisesRegex(ContractError, "state exists"):
                run(folder, duration=2, quiet=True)
            with self.assertRaisesRegex(ContractError, "duration differs"):
                run(folder, duration=3, resume=True, quiet=True)

    def test_interrupt_reconciles_committed_but_unacknowledged_progress(self):
        from unittest.mock import patch

        original = LocalClient.send
        interrupted = False

        def send_then_interrupt(client, event, position, duration):
            nonlocal interrupted
            result = original(client, event, position, duration)
            if event == "playing" and not interrupted:
                interrupted = True
                # Represents interruption after commit with a stale client seq.
                client.seq -= 1
                raise KeyboardInterrupt
            return result

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(LocalClient, "send", send_then_interrupt):
                result = run(folder, duration=2, interval=1, scale=30, quiet=True)
            self.assertEqual(result["position"], 1)
            self.assertFalse(result["active"])
            self.assertFalse(result["completed"])
            result = run(folder, duration=2, interval=1, scale=30, resume=True, quiet=True)
            self.assertTrue(result["completed"])

    def test_report_redirect_is_never_followed(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "https://mooc1.chaoxing.com/")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = LocalClient(f"http://127.0.0.1:{server.server_port}")
            with self.assertRaisesRegex(ContractError, "redirects"):
                client.request("/redirect")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
