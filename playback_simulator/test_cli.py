"""CLI routing tests: all real transport construction and execution is intercepted."""
import contextlib
import io
import unittest
from unittest.mock import patch

import simulator


class DefaultRemoteCliTests(unittest.TestCase):
    def invoke(self, args):
        with patch("sys.argv", ["simulator.py", *args]), contextlib.redirect_stdout(io.StringIO()):
            return simulator.main()

    def test_default_constructs_real_transport_and_sends(self):
        with patch("chaoxing_client.CourseContext.load") as load, \
                patch("chaoxing_client.Transport") as transport, \
                patch("chaoxing_client.run_remote", return_value={"intercepted": True}) as run_remote, \
                patch("chaoxing_client.preview") as preview:
            self.assertEqual(self.invoke([]), 0)
            transport.assert_called_once()
            run_remote.assert_called_once()
            self.assertIs(run_remote.call_args.args[0], load.return_value)
            self.assertIs(run_remote.call_args.args[1], transport.return_value)
            preview.assert_not_called()

    def test_dry_run_never_constructs_transport_or_reads_cookies(self):
        with patch("chaoxing_client.CourseContext.load") as load, \
                patch("chaoxing_client.Transport") as transport, \
                patch("chaoxing_client.cookie_jar") as cookies, \
                patch("chaoxing_client.run_remote") as run_remote, \
                patch("chaoxing_client.preview", return_value={"mode": "preview"}) as preview:
            self.assertEqual(self.invoke(["--dry-run"]), 0)
            preview.assert_called_once_with(load.return_value)
            transport.assert_not_called()
            cookies.assert_not_called()
            run_remote.assert_not_called()

    def test_send_remains_a_compatible_explicit_real_mode(self):
        with patch("chaoxing_client.CourseContext.load"), \
                patch("chaoxing_client.Transport") as transport, \
                patch("chaoxing_client.run_remote", return_value={}) as run_remote:
            self.assertEqual(self.invoke(["--send"]), 0)
            transport.assert_called_once()
            run_remote.assert_called_once()

    def test_send_and_dry_run_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.invoke(["--send", "--dry-run"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
