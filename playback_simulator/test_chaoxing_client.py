import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from chaoxing_client import (CourseContext, RemoteError, Transport, authorized_url,
                             card_setting, checked_response, cookie_jar, preview, run_remote)


def fixture():
    setting = {"control": True, "defaults": {"clazzId": "test-class", "userid": "test-user",
        "courseid": "test-course", "knowledgeid": "test-chapter", "fid": "test-fid", "cpi": "test-cpi",
        "state": 0, "isFiled": 0, "chapterCapture": 0, "reportTimeInterval": 2,
        "reportUrl": "https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/test-user"},
        "attachments": [{"type": "video", "objectId": "test-object", "jobid": "test-job",
            "otherInfo": "nodeId_test&courseId=test-course", "attDuration": 6, "attDurationEnc": "test-att-enc",
            "property": {"rt": "1", "jobid": "test-job"}}]}
    media = {"status": "success", "duration": 6, "dtoken": "test-token", "objectid": "test-object"}
    study = {"url": "https://mooc1.chaoxing.com/mycourse/studentstudy?test=1", "cpi": "test-cpi"}
    return CourseContext(setting, media, study)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeTransport:
    """Intercept every route in memory. Never creates a socket or HTTP server."""
    def __init__(self, context, fail_event=None, pass_at_end=True):
        self.context = context
        self.calls = []
        self.fail_event = fail_event
        self.pass_at_end = pass_at_end

    def get(self, url, referer, *, mutating=False, as_json=True):
        self.calls.append((url, mutating))
        parsed = urllib.parse.urlsplit(url)
        if parsed.path.endswith("/knowledge/cards"):
            return "<script>var mArg=" + json.dumps(self.context.setting) + ";</script>"
        if parsed.path.startswith("/ananas/status/"):
            return {**self.context.media, "dtoken": "fresh-test-token"}
        if "/multimedia/log/" in parsed.path:
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if params["isdrag"] == self.fail_event:
                raise RemoteError("injected uncertain transport failure")
            return {"isPassed": params["isdrag"] == "4" and self.pass_at_end}
        raise AssertionError("unexpected route")


class RequestContractTests(unittest.TestCase):
    def test_real_url_query_signature_and_encoding(self):
        context = fixture()
        url, params = context.build("playing", 3.9, timestamp_ms=1234)
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.hostname, "mooc1.chaoxing.com")
        self.assertTrue(parsed.path.endswith("/test-user/test-token"))
        self.assertNotIn("/mock/", parsed.path)
        self.assertNotIn("payload=", parsed.query)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(query["playingTime"], "3")
        self.assertEqual(query["otherInfo"], "nodeId_test")
        self.assertEqual(query["courseId"], "test-course")
        self.assertEqual(query["courseEngineInfo"], "false")
        self.assertEqual(query["_t"], "1234")
        expected = "[test-class][test-user][test-job][test-object][3000][d_yHJ!$pdA~5][6000][0_6]"
        self.assertEqual(params["enc"], hashlib.md5(expected.encode()).hexdigest())
        self.assertEqual(context.build("pause", 3)[1]["enc"], params["enc"])

    def test_preview_is_redacted_and_has_no_transport(self):
        result = preview(fixture())
        rendered = json.dumps(result)
        self.assertNotIn("test-token", rendered)
        self.assertNotIn("test-user", rendered)
        self.assertEqual(result["mode"], "remote-preview-no-network")
        self.assertFalse(result["readsCookie"])

    def test_host_and_report_path_validation(self):
        for url in ("http://mooc1.chaoxing.com/", "https://chaoxing.com.evil.example/",
                    "https://127.0.0.1/", "https://user:pass@mooc1.chaoxing.com/",
                    "https://mooc1.chaoxing.com:8443/"):
            with self.subTest(url=url), self.assertRaises(RemoteError):
                authorized_url(url)
        context = fixture()
        context.setting["defaults"]["reportUrl"] = "https://mooc1.chaoxing.com/not-a-report"
        with self.assertRaises(RemoteError):
            context.build("play", 0)

    def test_json_extraction_never_evaluates_javascript(self):
        context = fixture()
        self.assertEqual(card_setting("var mArg=" + json.dumps(context.setting) + ";"), context.setting)
        with self.assertRaises(RemoteError):
            card_setting("var mArg=evilFunction();")

    def test_response_requires_real_boolean_and_preserves_false(self):
        self.assertFalse(checked_response({"isPassed": False})["isPassed"])
        self.assertTrue(checked_response({"isPassed": True})["isPassed"])
        for body in ({}, {"success": True}, {"isPassed": "false"}, {"isPassed": 1}):
            with self.subTest(body=body), self.assertRaises(RemoteError):
                checked_response(body)

    def test_disabled_and_interactive_cards_are_rejected(self):
        for key in ("chapterCapture", "supportHeartbeat", "supportVideoPluginV1", "isFiled"):
            context = fixture()
            context.setting["defaults"][key] = 1
            with self.subTest(key=key), self.assertRaises(RemoteError):
                context.validate()
        with self.assertRaises(RemoteError):
            fixture().build("pause", 6)


class TransportTests(unittest.TestCase):
    def test_successful_request_is_get_with_referer_and_json_response(self):
        class Response(io.BytesIO):
            status = 200

            def geturl(self):
                return "https://mooc1.chaoxing.com/report"

        class Opener:
            request = None

            def open(self, request, timeout):
                self.request = request
                return Response(b'{"isPassed":false}')

        opener = Opener()
        transport = Transport(opener=opener)
        self.assertEqual(transport.get("https://mooc1.chaoxing.com/report", "https://mooc1.chaoxing.com/player",
                                       mutating=True), {"isPassed": False})
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertEqual(opener.request.get_header("Referer"), "https://mooc1.chaoxing.com/player")
        self.assertIsNone(opener.request.data)

    def test_reporting_has_no_blind_retry_but_reads_retry(self):
        class FailingOpener:
            count = 0

            def open(self, request, timeout):
                self.count += 1
                raise urllib.error.HTTPError(request.full_url, 503, "test", {}, io.BytesIO(b"test"))

        opener = FailingOpener()
        transport = Transport(opener=opener, sleeper=lambda _: None)
        with self.assertRaisesRegex(RemoteError, "503"):
            transport.get("https://mooc1.chaoxing.com/report", "https://mooc1.chaoxing.com/", mutating=True)
        self.assertEqual(opener.count, 1)
        with self.assertRaisesRegex(RemoteError, "503"):
            transport.get("https://mooc1.chaoxing.com/status", "https://mooc1.chaoxing.com/")
        self.assertEqual(opener.count, 4)

    def test_cookie_formats_and_domain_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookie.txt"
            variants = ["Cookie: test=value; another=example",
                json.dumps([{"name": "test", "value": "value", "domain": ".chaoxing.com"},
                            {"name": "bad", "value": "ignored", "domain": ".example.org"}]),
                "# Netscape HTTP Cookie File\n.chaoxing.com\tTRUE\t/\tTRUE\t0\ttest\tvalue"]
            for variant in variants:
                path.write_text(variant, encoding="utf-8")
                jar = cookie_jar(path)
                self.assertGreater(len(jar), 0)
                self.assertNotIn("bad", [cookie.name for cookie in jar])


class PlaybackTests(unittest.TestCase):
    def test_real_scheduler_uses_wall_time_refreshed_token_and_checkpoints(self):
        context = fixture()
        transport, clock = FakeTransport(context), Clock()
        with tempfile.TemporaryDirectory() as temp:
            result = run_remote(context, transport, Path(temp), pause_at=2, pause_for=3,
                                clock=clock, sleeper=clock.sleep, quiet=True)
            self.assertTrue(result["serverIsPassed"])
            self.assertIsNone(result["serverCreditedSeconds"])
            self.assertEqual(result["lastResponsePosition"], 6)
            self.assertGreaterEqual(clock.now, 109)
            reports = [url for url, mutating in transport.calls if mutating]
            self.assertTrue(all("fresh-test-token" in url for url in reports))
            self.assertTrue(all("127.0.0.1" not in url and "/mock/" not in url for url in reports))
            saved = (Path(temp) / "remote-checkpoint.json").read_text()
            self.assertNotIn("test-token", saved)
            self.assertNotIn("test-user", saved)

    def test_restart_uses_last_response_without_crediting_downtime(self):
        context, clock = fixture(), Clock()
        transport = FakeTransport(context)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            first = run_remote(context, transport, folder, stop_at=2,
                               clock=clock, sleeper=clock.sleep, quiet=True)
            self.assertEqual(first["lastResponsePosition"], 2)
            clock.now += 999
            before = clock.now
            second = run_remote(context, transport, folder, resume=True,
                                clock=clock, sleeper=clock.sleep, quiet=True)
            self.assertTrue(second["serverIsPassed"])
            self.assertGreaterEqual(clock.now - before, 4)

    def test_transport_failure_keeps_pending_does_not_retry_or_advance(self):
        context, clock = fixture(), Clock()
        transport = FakeTransport(context, fail_event="0")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with self.assertRaises(RemoteError):
                run_remote(context, transport, folder, clock=clock, sleeper=clock.sleep, quiet=True)
            saved = json.loads((folder / "remote-checkpoint.json").read_text())
            self.assertEqual(saved["lastResponsePosition"], 0)
            self.assertEqual(saved["pendingEvent"], {"event": "playing", "position": 2})
            self.assertEqual(sum(mutating for _, mutating in transport.calls), 2)

    def test_duration_reached_without_ispassed_does_not_claim_completion(self):
        context, clock = fixture(), Clock()
        transport = FakeTransport(context, pass_at_end=False)
        with tempfile.TemporaryDirectory() as temp:
            result = run_remote(context, transport, Path(temp), clock=clock, sleeper=clock.sleep, quiet=True)
            self.assertFalse(result["serverIsPassed"])
            self.assertIsNone(result["serverCreditedSeconds"])

    def test_server_limit_stops_immediately(self):
        context, clock = fixture(), Clock()

        class Limit(FakeTransport):
            def get(self, *args, **kwargs):
                result = super().get(*args, **kwargs)
                return {**result, "hasJobLimit": True} if kwargs.get("mutating") else result

        transport = Limit(context)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RemoteError, "limit"):
                run_remote(context, transport, Path(temp), clock=clock, sleeper=clock.sleep, quiet=True)
            self.assertEqual(sum(mutating for _, mutating in transport.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
