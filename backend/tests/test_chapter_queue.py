import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from backend.platform import ChapterUnavailable, PlatformVerificationRequired
from backend.service import Manager, ServiceError
from backend.tests.test_course_switch import LazyPlatform
from backend.tests.test_service import finished_runner


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name), platform_factory=LazyPlatform, runner=finished_runner)
        _, self.s, _ = self.manager.login('memory-only-test')
        self.outline()

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def wait(self, info):
        job = self.manager.job(self.s, info['id'])
        job.thread.join(3)
        self.assertFalse(job.thread.is_alive())
        return job.snapshot()

    def outline(self, course='course-one'):
        return self.wait(self.manager.scan(self.s, course, scope='outline'))

    def start(self, ids, **options):
        return self.manager.start(self.s, ['course-one'], chapter_ids=ids, **options)

    def test_directory_has_all_titles_but_zero_task_or_shell_reads_and_private_fields(self):
        self.outline('course-two')
        self.assertEqual(self.s.platform.cards, [])
        self.assertEqual(self.s.platform.shells, [])
        for course in ('course-one', 'course-two'):
            catalog = self.manager.catalog(self.s, course)
            self.assertEqual(len(catalog['chapters']), 133)
            self.assertEqual(catalog['tasks'], [])
            self.assertNotIn('private-access-token', json.dumps(catalog))

    def test_only_selected_chapters_in_original_order(self):
        result = self.wait(self.start(['7', '2']))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['selectedChapterIds'], ['2', '7'])
        self.assertEqual(self.s.platform.cards, [('course-one', '2'), ('course-one', '7')])
        self.assertEqual(result['passedVideos'], 2)
        self.assertEqual(result['completedChapters'], 2)

    def test_next_chapter_is_not_read_while_current_video_is_running_then_cancel(self):
        entered = threading.Event()
        def gated(ctx, transport, folder, **options):
            entered.set()
            options['stop_event'].wait(3)
            return {'stopped': True}
        self.manager.runner = gated
        info = self.start(['1', '4'])
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.s.platform.cards, [('course-one', '1')])
        self.manager.job(self.s, info['id']).cancel.set()
        result = self.wait(info)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual([ch['status'] for ch in result['chapterQueue']], ['stopped', 'queued'])
        self.assertEqual(self.s.platform.cards, [('course-one', '1')])

    def test_stale_chapter_selection_never_falls_back_to_other_chapters(self):
        self.s.platform.chapters = lambda course: [{'id': '1', 'name': 'changed'}]
        result = self.wait(self.start(['2']))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.s.platform.cards, [])

    def test_invalid_selection_and_policy_rejected_without_card_reads(self):
        for ids in ([], ['1', '1'], ['foreign'], [None], '1'):
            with self.subTest(ids=ids), self.assertRaises(ServiceError):
                self.start(ids)
        with self.assertRaises(ServiceError):
            self.start(['1'], quiz_policy='unknown')
        with self.assertRaises(ServiceError):
            self.manager.start(self.s, ['course-one'], ['task'], chapter_ids=['1'])
        self.manager.refresh_courses(self.s)
        with self.assertRaises(ServiceError):
            self.start(['1'])
        self.assertEqual(self.s.platform.cards, [])

    def add_quiz(self):
        self.s.platform.setting['attachments'].insert(0, {'type': 'workid', 'job': True, 'jobid': 'quiz-job'})

    def test_random_submit_counts_acceptance_separately_from_pass(self):
        self.add_quiz()
        calls = []
        self.s.platform.run_quiz = lambda *args, **kw: calls.append(args) or {'submitted': True, 'isPassed': False}
        result = self.wait(self.start(['1'], quiz_policy='random_submit'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['submittedQuizzes'], 1)
        self.assertEqual(result['passedQuizzes'], 0)
        self.assertEqual(result['unconfirmedQuizzes'], 1)
        self.assertEqual(result['passedVideos'], 1)
        self.assertEqual(result['status'], 'partial')

    def test_unsupported_quiz_keeps_later_video_running(self):
        self.add_quiz()
        def unsupported(*args, **kw):
            raise ChapterUnavailable('test unsupported structure')
        self.s.platform.run_quiz = unsupported
        result = self.wait(self.start(['1'], quiz_policy='random_submit'))
        self.assertEqual(result['unsupportedTasks'], 1)
        self.assertEqual(result['passedVideos'], 1)
        self.assertEqual(result['status'], 'partial')

    def test_skip_quiz_policy_does_not_call_submission(self):
        self.add_quiz()
        self.s.platform.run_quiz = lambda *a, **kw: self.fail('skip must not submit')
        result = self.wait(self.start(['1']))
        self.assertEqual(result['submittedQuizzes'], 0)
        self.assertEqual(result['unsupportedTasks'], 1)
        self.assertEqual(result['passedVideos'], 1)

    def test_verification_stops_entire_queue(self):
        self.s.platform.block = True
        result = self.wait(self.start(['1', '2']))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['actionRequired'], 'platform_verification')
        self.assertEqual(result['chapterQueue'][1]['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
