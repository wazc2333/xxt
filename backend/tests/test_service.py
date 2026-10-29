import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.platform import Document, Platform, parse_courses
from backend.service import Manager, ServiceError


def setting():
    return {"control": True, "defaults": {}, "attachments": [{"type": "video", "objectId": "video-one", "jobid": "job-one",
            "job": True, "property": {"name": "授权测试视频"}}]}


class FakePlatform:
    """Deterministic in-memory adapter; no socket or real credential is used."""
    def __init__(self, cookie):
        if cookie == "invalid":
            raise ValueError("invalid test cookie")
        self.closed = False
        self.courses = {"course-one": {"id": "course-one", "name": "测试课程"},
                        "course-two": {"id": "course-two", "name": "第二门课程"}}
        self.transport = object()
        self.setting = setting()

    def list_courses(self):
        return list(self.courses.values())

    def chapters(self, course):
        return [{"id": "chapter-one", "name": "测试章节"}]

    def chapter_cards(self, course, chapter):
        yield copy.deepcopy(self.setting), {"cards_url": "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?num=0"}

    def context(self, setting, study, att):
        return SimpleNamespace(duration=6, fingerprint="test-fingerprint", object_id=att["objectId"])

    def close(self):
        self.closed = True


def finished_runner(ctx, transport, folder, **kwargs):
    kwargs["on_update"]({"localPosition": 6, "lastResponsePosition": 6, "serverIsPassed": True,
                         "reportsWithValidResponse": 2})
    return {"serverIsPassed": True}


def blocking_runner(ctx, transport, folder, **kwargs):
    kwargs["stop_event"].wait(3)
    return {"stopped": True, "serverIsPassed": False}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name), platform_factory=FakePlatform, runner=finished_runner)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def login(self):
        return self.manager.login("memory-only-secret")

    def wait_job(self, session, job_id):
        job = self.manager.job(session, job_id)
        job.thread.join(3)
        self.assertFalse(job.thread.is_alive())
        return job.snapshot()

    def test_login_course_selection_progress_and_private_disk_state(self):
        token, s, courses = self.login()
        self.assertEqual(len(courses), 2)
        job = self.manager.start(s, ["course-one", "course-two"])
        result = self.wait_job(s, job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["passedVideos"], 2)
        self.assertEqual(result["localPosition"], 6)
        self.assertIsNone(result["serverCreditedSeconds"])
        disk = "".join(p.read_text(encoding="utf-8") for p in Path(self.temp.name).rglob("*.json"))
        for secret in ("memory-only-secret", token, s.csrf):
            self.assertNotIn(secret, disk)

    def test_two_sessions_cannot_read_each_others_jobs(self):
        _, a, _ = self.login()
        _, b, _ = self.login()
        job = self.manager.start(a, ["course-one"])
        with self.assertRaises(ServiceError) as caught:
            self.manager.job(b, job["id"])
        self.assertEqual(caught.exception.status, 404)

    def test_invalid_and_duplicate_course_ids_are_rejected(self):
        _, s, _ = self.login()
        for value in ([], ["course-one", "course-one"], ["foreign-course"], "course-one", [None]):
            with self.subTest(value=value), self.assertRaises(ServiceError):
                self.manager.start(s, value)

    def test_cancel_and_global_job_limit(self):
        self.manager.runner = blocking_runner
        self.manager.max_jobs = 1
        token, s, _ = self.login()
        _, other, _ = self.login()
        started = self.manager.start(s, ["course-one"])
        with self.assertRaises(ServiceError) as caught:
            self.manager.start(other, ["course-one"])
        self.assertEqual(caught.exception.status, 429)
        with self.assertRaises(ServiceError):
            self.manager.refresh_courses(s)
        self.manager.logout(token)
        result = self.wait_job(s, started["id"])
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(s.platform.closed)
        self.assertEqual(self.manager.active, 0)
        with self.assertRaises(ServiceError):
            self.manager.session(token)

    def test_server_non_confirmation_and_non_video_tasks_mean_partial(self):
        self.manager.runner = lambda *args, **kwargs: {"serverIsPassed": False}
        _, s, _ = self.login()
        s.platform.setting["attachments"].append({"type": "workid", "job": True})
        job = self.manager.start(s, ["course-one"])
        result = self.wait_job(s, job["id"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["passedVideos"], 0)
        self.assertEqual(result["unsupportedTasks"], 1)
        self.assertEqual(result["unconfirmedVideos"], 1)

    def test_already_passed_video_never_runs_again(self):
        _, s, _ = self.login()
        s.platform.setting["attachments"][0]["isPassed"] = True
        self.manager.runner = lambda *args, **kwargs: self.fail("must not report passed video")
        job = self.manager.start(s, ["course-one"])
        result = self.wait_job(s, job["id"])
        self.assertEqual(result["alreadyPassedVideos"], 1)
        self.assertEqual(result["status"], "completed")

    def test_session_expiry_clears_credentials(self):
        token, s, _ = self.login()
        self.manager.clock = lambda: s.touched + self.manager.session_ttl + 1
        with self.assertRaises(ServiceError) as caught:
            self.manager.session(token)
        self.assertEqual(caught.exception.status, 401)
        self.assertTrue(s.platform.closed)

    def test_upstream_failure_does_not_complete_course(self):
        from playback_simulator.chaoxing_client import RemoteError
        def fail(*args, **kwargs):
            raise RemoteError("test: login required")
        self.manager.runner = fail
        _, s, _ = self.login()
        job = self.manager.start(s, ["course-one"])
        result = self.wait_job(s, job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["passedVideos"], 0)


class ParserTests(unittest.TestCase):
    def test_courses_deduplicate_use_text_and_scope_hosts(self):
        html = '<div id="courseList"><a href="/visit/stucoursemiddle?courseid=1&amp;clazzid=2&amp;cpi=3"><img src="x"></a>'
        html += '<a href="/visit/stucoursemiddle?courseid=1&amp;clazzid=2&amp;cpi=3"><span title="课程">测试课程</span></a></div>'
        rows = parse_courses(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "测试课程")
        from playback_simulator.chaoxing_client import RemoteError
        with self.assertRaises(RemoteError):
            parse_courses(html.replace('/visit/stucoursemiddle?', 'https://evil.example/visit/stucoursemiddle?'))
        with self.assertRaises(RemoteError):
            parse_courses("<form>登录</form>")

    def test_multiple_cards_use_server_count_and_actual_indexes(self):
        p = Platform("_uid=test")
        requests = []
        def request(url, referer=None):
            requests.append(url)
            if "studentstudyAjax" in url:
                return '<iframe src="/mooc-ans/knowledge/cards?courseid=1&amp;clazzid=2&amp;knowledgeid=4&amp;num=0"></iframe><input id="cardcount" value="2">', url
            if "/knowledge/cards" in url:
                return 'var mArg=' + json.dumps({"attachments": [], "defaults": {}}), url
            return "<html></html>", url
        p.request = request
        cards = list(p.chapter_cards({"courseid":"1","clazzid":"2","cpi":"3"},
                    {"id":"4","enc":"test","hidden":"0","referer":"https://mooc1.chaoxing.com/"}))
        self.assertEqual(len(cards), 2)
        self.assertIn("num=0", cards[0][1]["cards_url"])
        self.assertIn("num=1", cards[1][1]["cards_url"])
        p.close()


if __name__ == "__main__":
    unittest.main()
