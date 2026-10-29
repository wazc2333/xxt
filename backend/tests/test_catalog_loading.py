import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.platform import Platform
from backend.service import Manager, ServiceError
from backend.tests.test_service import FakePlatform


class GatedPlatform(FakePlatform):
    def __init__(self, cookie):
        super().__init__(cookie)
        self.entered = [threading.Event() for _ in range(8)]
        self.release = [threading.Event() for _ in range(8)]
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.reads = []

    def chapters(self, course):
        return [{"id": str(i), "name": f"第{i + 1}章"} for i in range(8)]

    def chapter_cards(self, course, chapter):
        i = int(chapter["id"])
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.reads.append(i)
        self.entered[i].set()
        try:
            if not self.release[i].wait(4):
                raise RuntimeError("Test gate was not released")
            card = copy.deepcopy(self.setting)
            card["attachments"][0]["objectId"] = f"video-{i}"
            yield card, {"cards_url": "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?num=0"}
        finally:
            with self.lock:
                self.active -= 1


class CatalogLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name), platform_factory=GatedPlatform)
        self.token, self.session, _ = self.manager.login("test-only")
        self.platform = self.session.platform

    def tearDown(self):
        for s in list(self.manager.sessions.values()):
            for event in s.platform.release:
                event.set()
        self.manager.close()
        self.temp.cleanup()

    def until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Expected catalog transition did not occur")
            time.sleep(.005)

    def start_scan(self):
        started = self.manager.scan(self.session, "course-one")
        job = self.manager.job(self.session, started["id"])
        self.assertTrue(all(e.wait(2) for e in self.platform.entered[:4]))
        return job

    def finish(self, job):
        for event in self.platform.release:
            event.set()
        job.thread.join(3)
        self.assertFalse(job.thread.is_alive())
        return job.snapshot()

    def test_bounded_parallel_preview_and_stable_chapter_order(self):
        job = self.start_scan()
        self.assertEqual(self.platform.peak, 4)
        self.assertEqual(sorted(self.platform.reads), [0, 1, 2, 3])
        initial = self.manager.catalog(self.session, "course-one")
        self.assertEqual(len(initial["chapters"]), 8)
        self.assertTrue(initial["loading"])
        self.assertFalse(initial["ready"])
        self.platform.release[2].set()
        self.until(lambda: len(self.manager.catalog(self.session, "course-one")["tasks"]) == 1)
        preview = self.manager.catalog(self.session, "course-one")
        self.assertEqual(preview["tasks"][0]["chapterId"], "2")
        self.assertFalse(preview["ready"])
        with self.assertRaises(ServiceError):
            self.manager.start(self.session, ["course-one"], [preview["tasks"][0]["id"]])
        self.platform.release[0].set()
        self.until(lambda: len(self.manager.catalog(self.session, "course-one")["tasks"]) == 2)
        self.assertEqual([t["chapterId"] for t in self.manager.catalog(self.session, "course-one")["tasks"]], ["0", "2"])
        self.assertEqual(self.finish(job)["status"], "completed")
        final = self.manager.catalog(self.session, "course-one")
        self.assertTrue(final["ready"])
        self.assertFalse(final["loading"])
        self.assertEqual([t["chapterId"] for t in final["tasks"]], list(map(str, range(8))))
        self.assertEqual(self.platform.peak, 4)

    def test_cancelled_rescan_keeps_previous_complete_catalog(self):
        first = self.start_scan()
        self.finish(first)
        old = self.manager.catalog(self.session, "course-one")
        for event in [*self.platform.release, *self.platform.entered]:
            event.clear()
        job = self.start_scan()
        self.platform.release[2].set()
        self.until(lambda: len(self.manager.catalog(self.session, "course-one")["tasks"]) == 1)
        job.cancel.set()
        self.assertEqual(self.finish(job)["status"], "stopped")
        self.assertEqual(self.manager.catalog(self.session, "course-one"), old)
        self.assertEqual(self.manager.active, 0)

    def test_partial_preview_is_session_scoped_and_logout_drains_workers(self):
        job = self.start_scan()
        self.platform.release[0].set()
        self.until(lambda: len(self.manager.catalog(self.session, "course-one")["tasks"]) == 1)
        _, other, _ = self.manager.login("other-test-only")
        self.assertFalse(self.manager.catalog(other, "course-one")["ready"])
        self.assertEqual(self.manager.catalog(other, "course-one")["tasks"], [])
        self.manager.logout(self.token)
        self.assertEqual(self.finish(job)["status"], "stopped")
        self.assertTrue(self.platform.closed)
        self.assertEqual(self.platform.active, 0)


class CatalogPlatformTests(unittest.TestCase):
    def test_catalog_opens_shell_once_and_still_reads_all_actual_cards(self):
        platform = Platform("_uid=test")
        calls = []
        course = {"courseid": "1", "clazzid": "2", "cpi": "3"}
        chapters = [{"id": str(i), "enc": "test", "hidden": "0", "referer": "https://mooc1.chaoxing.com/"} for i in (4, 5)]

        def request(url, referer=None):
            from urllib.parse import parse_qs, urlsplit
            calls.append(urlsplit(url).path)
            if "studentstudyAjax" in url:
                i = parse_qs(urlsplit(url).query)["chapterId"][0]
                return f'<iframe src="/mooc-ans/knowledge/cards?courseid=1&amp;clazzid=2&amp;knowledgeid={i}"></iframe><input id="cardcount" value="2">', url
            return "empty", url

        platform.request = request
        try:
            platform.prepare_catalog(course, chapters)
            with patch("backend.platform.card_setting", return_value={"attachments": []}):
                for chapter in chapters:
                    self.assertEqual(len(list(platform.catalog_cards(course, chapter))), 2)
            self.assertEqual(calls.count("/mycourse/studentstudy"), 1)
            self.assertEqual(calls.count("/mycourse/studentstudyAjax"), 2)
            self.assertEqual(calls.count("/mooc-ans/knowledge/cards"), 4)
            cancelled = threading.Event()
            cancelled.set()
            before = len(calls)
            self.assertEqual(list(platform.catalog_cards(course, chapters[0], cancel_event=cancelled)), [])
            self.assertEqual(len(calls), before)
        finally:
            platform.close()


if __name__ == "__main__":
    unittest.main()
