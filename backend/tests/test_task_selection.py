import copy
import tempfile
import threading
import unittest
from pathlib import Path

from backend.platform import ChapterUnavailable, task_id
from backend.service import Manager, ServiceError
from backend.tests.test_service import FakePlatform, finished_runner, setting


class SelectionPlatform(FakePlatform):
    def __init__(self, cookie):
        super().__init__(cookie)
        self.card_reads, self.context_reads = [], []
        self.setting["attachments"].extend([
            {"type":"video", "objectId":"video-two", "jobid":"job-two", "property":{"name":"第二个视频"}},
            {"type":"video", "objectId":"video-passed", "jobid":"job-passed", "isPassed":True, "property":{"name":"已完成视频"}},
            {"type":"workid", "jobid":"work-one", "job":True, "property":{"name":"练习题"}},
        ])
        self.block_second = False

    def chapters(self, course):
        return [{"id":"chapter-one", "name":"第一章"}, {"id":"chapter-two", "name":"第二章"}]

    def chapter_cards(self, course, chapter):
        self.card_reads.append(chapter["id"])
        if chapter["id"] == "chapter-two":
            if self.block_second:
                raise ChapterUnavailable("需要人工验证")
            card = setting()
            card["attachments"][0].update(objectId="video-three", jobid="job-three")
        else:
            card = self.setting
        yield copy.deepcopy(card), {"cards_url":"https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?num=0"}

    def context(self, setting, study, att):
        self.context_reads.append(att["objectId"])
        return super().context(setting, study, att)


class TaskSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ran = []
        def runner(context, *args, **kwargs):
            self.ran.append(context.object_id)
            return finished_runner(context, *args, **kwargs)
        self.manager = Manager(Path(self.temp.name), platform_factory=SelectionPlatform, runner=runner)
        self.token, self.s, _ = self.manager.login("fake-test-cookie")

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def wait(self, job):
        worker = self.manager.job(self.s, job["id"])
        worker.thread.join(3)
        self.assertFalse(worker.thread.is_alive())
        return worker.snapshot()

    def scan(self):
        result = self.wait(self.manager.scan(self.s, "course-one"))
        self.assertIn(result["status"], ("completed", "partial"))
        return self.manager.catalog(self.s, "course-one")

    def test_scan_is_read_only_and_lists_completed_and_non_video_tasks(self):
        self.assertFalse(self.manager.catalog(self.s, "course-one")["ready"])
        catalog = self.scan()
        self.assertEqual(len(catalog["tasks"]), 5)
        self.assertEqual(sum(t["selectable"] for t in catalog["tasks"]), 3)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.s.platform.context_reads, [])
        self.assertNotIn("cards_url", str(catalog))

    def test_only_one_selected_video_executes_and_other_chapter_is_not_opened(self):
        catalog = self.scan()
        chosen = next(t for t in catalog["tasks"] if t["name"] == "第二个视频")
        self.s.platform.card_reads.clear()
        # A newly inaccessible chapter must not interfere with an unrelated selection.
        self.s.platform.block_second = True
        result = self.wait(self.manager.start(self.s, ["course-one"], [chosen["id"]]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selectedTaskCount"], 1)
        self.assertEqual(result["videoCount"], 1)
        self.assertEqual(result["unsupportedTasks"], 0)
        self.assertEqual(self.ran, ["video-two"])
        self.assertEqual(self.s.platform.card_reads, ["chapter-one"])
        self.assertEqual(result["confirmedTaskIds"], [chosen["id"]])
        refreshed = self.manager.catalog(self.s, "course-one")
        row = next(t for t in refreshed["tasks"] if t["id"] == chosen["id"])
        self.assertTrue(row["isPassed"])
        self.assertFalse(row["selectable"])

    def test_invalid_empty_duplicate_and_other_course_selection_rejected(self):
        catalog = self.scan()
        video = next(t for t in catalog["tasks"] if t["selectable"])
        for ids in ([], ["unknown"], [video["id"], video["id"]], [None], "bad"):
            with self.subTest(ids=ids), self.assertRaises(ServiceError):
                self.manager.start(self.s, ["course-one"], ids)
        with self.assertRaises(ServiceError):
            self.manager.start(self.s, ["course-two"], [video["id"]])
        for task in catalog["tasks"]:
            if not task["selectable"]:
                with self.assertRaises(ServiceError):
                    self.manager.start(self.s, ["course-one"], [task["id"]])
        _, other, _ = self.manager.login("another-test-cookie")
        with self.assertRaises(ServiceError):
            self.manager.start(other, ["course-one"], [video["id"]])

    def test_changed_task_never_falls_back_to_whole_course(self):
        catalog = self.scan()
        chosen = next(t for t in catalog["tasks"] if t["name"] == "第二个视频")
        self.s.platform.setting["attachments"][1]["jobid"] = "new-job-after-scan"
        result = self.wait(self.manager.start(self.s, ["course-one"], [chosen["id"]]))
        self.assertEqual(result["status"], "failed")
        self.assertIn("发生变化", result["message"])
        self.assertEqual(self.ran, [])

    def test_task_completed_after_scan_is_skipped_using_fresh_state(self):
        catalog = self.scan()
        chosen = next(t for t in catalog["tasks"] if t["name"] == "第二个视频")
        self.s.platform.setting["attachments"][1]["isPassed"] = True
        result = self.wait(self.manager.start(self.s, ["course-one"], [chosen["id"]]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["alreadyPassedVideos"], 1)
        self.assertEqual(self.ran, [])

    def test_blocked_chapter_retains_partial_catalog_and_notes(self):
        self.s.platform.block_second = True
        catalog = self.scan()
        self.assertTrue(catalog["incomplete"])
        self.assertEqual(len(catalog["tasks"]), 4)
        self.assertIn("需要人工验证", catalog["notes"][0])

    def test_refresh_courses_invalidates_old_task_selection(self):
        catalog = self.scan()
        video = next(t for t in catalog["tasks"] if t["selectable"])
        self.manager.refresh_courses(self.s)
        self.assertFalse(self.manager.catalog(self.s, "course-one")["ready"])
        with self.assertRaises(ServiceError):
            self.manager.start(self.s, ["course-one"], [video["id"]])

    def test_stopping_scan_does_not_publish_partial_selection(self):
        entered, release = threading.Event(), threading.Event()
        original = self.s.platform.chapter_cards
        def block(course, chapter):
            entered.set()
            release.wait(3)
            yield from original(course, chapter)
        self.s.platform.chapter_cards = block
        job = self.manager.scan(self.s, "course-one")
        self.assertTrue(entered.wait(2))
        self.manager.job(self.s, job["id"]).cancel.set()
        release.set()
        result = self.wait(job)
        self.assertEqual(result["status"], "stopped")
        self.assertFalse(self.manager.catalog(self.s, "course-one")["ready"])
        self.assertEqual(self.ran, [])

    def test_task_identity_separates_cards_and_jobs_without_auth_tokens(self):
        attachment = setting()["attachments"][0]
        study = {"cards_url":"https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?num=0&enc=old-token"}
        first = task_id("c", "ch", study, attachment, 0)
        study["cards_url"] = study["cards_url"].replace("old-token", "new-token")
        self.assertEqual(first, task_id("c", "ch", study, attachment, 0))
        study["cards_url"] = study["cards_url"].replace("num=0", "num=1")
        self.assertNotEqual(first, task_id("c", "ch", study, attachment, 0))


if __name__ == "__main__":
    unittest.main()
