import copy
import json
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from playback_simulator.chaoxing_client import CourseContext, run_remote


class Clock:
    def __init__(self, stop=None):
        self.now = 0
        self.stop = stop

    def __call__(self):
        return self.now

    def sleep(self, amount):
        self.now += amount
        if self.stop and self.now >= 1.5:
            self.stop.set()


def context():
    setting = {"control": True, "defaults": {"clazzId":"2", "userid":"3", "courseid":"1", "knowledgeid":"4",
        "cpi":"5", "fid":"6", "reportTimeInterval":2, "reportUrl":"https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/3"},
        "attachments":[{"type":"video", "objectId":"object-first", "jobid":"job-first", "otherInfo":"test", "property":{}},
                       {"type":"video", "objectId":"object-second", "jobid":"job-second", "otherInfo":"test", "property":{}}]}
    media = {"status":"success", "objectid":"object-second", "duration":6, "dtoken":"test-token"}
    study = {"url":"https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=4", "cpi":"5",
             "cards_url":"https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?num=1"}
    return CourseContext(setting, media, study, "object-second")


class FakeTransport:
    def __init__(self, ctx):
        self.ctx, self.calls = ctx, []

    def get(self, url, referer, *, mutating=False, as_json=True):
        self.calls.append((url, mutating))
        if "/knowledge/cards?" in url:
            return "var mArg=" + json.dumps(self.ctx.setting)
        if "/ananas/status/" in url:
            return self.ctx.media
        if "/multimedia/log/" in url:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            return {"isPassed": q["isdrag"] == "4"}
        raise AssertionError("unexpected route")


class PlaybackTests(unittest.TestCase):
    def test_specific_video_on_second_card_and_final_callback(self):
        ctx = context()
        transport = FakeTransport(ctx)
        updates, clock = [], Clock()
        with tempfile.TemporaryDirectory() as folder:
            result = run_remote(ctx, transport, Path(folder), clock=clock, sleeper=clock.sleep,
                                quiet=True, on_update=updates.append)
        self.assertTrue(result["serverIsPassed"])
        self.assertEqual(clock.now, 6)
        self.assertTrue(transport.calls[0][0].endswith("num=1"))
        self.assertTrue(updates[-1]["serverIsPassed"])
        self.assertEqual(updates[-1]["lastResponsePosition"], 6)
        self.assertTrue(all("objectId=object-second" in url for url, mutating in transport.calls if mutating))

    def test_cancel_during_wait_sends_pause_without_end_or_future_time(self):
        ctx, stop = context(), threading.Event()
        transport, clock = FakeTransport(ctx), Clock(stop)
        with tempfile.TemporaryDirectory() as folder:
            result = run_remote(ctx, transport, Path(folder), clock=clock, sleeper=clock.sleep,
                                quiet=True, stop_event=stop)
            saved = json.loads((Path(folder)/"remote-checkpoint.json").read_text(encoding="utf-8"))
        self.assertTrue(result["stopped"])
        self.assertEqual(saved["lastEvent"], "pause")
        self.assertLessEqual(saved["lastResponsePosition"], clock.now)
        queries = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)) for url, m in transport.calls if m]
        self.assertEqual([q["isdrag"] for q in queries], ["3", "2"])

    def test_pre_cancelled_job_never_accesses_network(self):
        ctx, stop = context(), threading.Event()
        stop.set()
        transport = FakeTransport(ctx)
        with tempfile.TemporaryDirectory() as folder:
            result = run_remote(ctx, transport, Path(folder), quiet=True, stop_event=stop)
        self.assertTrue(result["stopped"])
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
