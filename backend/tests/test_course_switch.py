import copy
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from backend.platform import Platform, PlatformVerificationRequired, RequestPacer, ScopedRedirect
from backend.service import Manager, ServiceError
from backend.tests.test_service import FakePlatform


class LazyPlatform(FakePlatform):
    def __init__(self, cookie):
        super().__init__(cookie)
        self.homes, self.cards, self.shells = [], [], []
        self.block = False

    def chapters(self, course):
        self.homes.append(course['id'])
        return [{'id': str(i), 'name': f'第{i + 1}章', 'enc': 'private-access-token',
                 'referer': 'https://mooc1.chaoxing.com/private?token=private-access-token'} for i in range(133)]

    def prepare_catalog(self, course, chapters):
        self.shells.append((course['id'], [ch['id'] for ch in chapters]))

    def chapter_cards(self, course, chapter):
        if self.block:
            raise PlatformVerificationRequired()
        self.cards.append((course['id'], chapter['id']))
        card = copy.deepcopy(self.setting)
        card['attachments'][0]['objectId'] = 'video-' + chapter['id']
        yield card, {'cards_url': 'https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?num=0'}


class CourseSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name), platform_factory=LazyPlatform)
        _, self.s, _ = self.manager.login('memory-only-test')

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def scan(self, course='course-one', **options):
        info = self.manager.scan(self.s, course, **options)
        job = self.manager.job(self.s, info['id'])
        job.thread.join(3)
        self.assertFalse(job.thread.is_alive())
        return job.snapshot()

    def test_switch_course_loads_only_one_chapter_and_keeps_course_caches_isolated(self):
        for course in ('course-one', 'course-two'):
            self.assertEqual(self.scan(course, scope='initial')['status'], 'completed')
            catalog = self.manager.catalog(self.s, course)
            self.assertEqual(len(catalog['chapters']), 133)
            self.assertEqual(len(catalog['tasks']), 1)
            self.assertEqual(sum(ch['status'] == 'pending' for ch in catalog['chapters']), 132)
            self.assertNotIn('private-access-token', str(catalog))
        self.assertEqual(self.s.platform.cards, [('course-one', '0'), ('course-two', '0')])
        self.assertEqual(len(self.s.platform.shells), 2)
        before = list(self.s.platform.homes)
        self.assertEqual(self.scan(scope='chapters', chapter_ids=['2'])['status'], 'completed')
        catalog = self.manager.catalog(self.s, 'course-one')
        self.assertEqual([t['chapterId'] for t in catalog['tasks']], ['0', '2'])
        self.assertEqual(len(self.manager.catalog(self.s, 'course-two')['tasks']), 1)
        self.assertEqual(self.s.platform.homes, before)
        self.assertEqual(len(self.s.platform.shells), 2)

    def test_chapter_refresh_replaces_only_requested_chapter_and_keeps_task_order(self):
        self.scan(scope='initial')
        self.scan(scope='chapters', chapter_ids=['5', '2'])
        catalog = self.manager.catalog(self.s, 'course-one')
        self.assertEqual([t['chapterId'] for t in catalog['tasks']], ['0', '2', '5'])
        old_ids = [t['id'] for t in catalog['tasks']]
        self.scan(scope='chapters', chapter_ids=['2'])
        self.assertEqual([t['id'] for t in self.manager.catalog(self.s, 'course-one')['tasks']], old_ids)

    def test_verification_keeps_old_catalog_and_marks_specific_action(self):
        self.scan(scope='initial')
        old = self.manager.catalog(self.s, 'course-one')
        self.s.platform.block = True
        failed = self.scan(scope='chapters', chapter_ids=['1'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['actionRequired'], 'platform_verification')
        self.assertIn('访问验证', failed['message'])
        self.assertEqual(self.manager.catalog(self.s, 'course-one'), old)

    def test_invalid_or_expired_chapter_selection_does_not_send_requests(self):
        with self.assertRaises(ServiceError):
            self.manager.scan(self.s, 'course-one', scope='chapters', chapter_ids=['0'])
        self.scan(scope='initial')
        for ids in (None, [], ['unknown'], ['0', '0'], [None], '0'):
            with self.subTest(ids=ids), self.assertRaises(ServiceError):
                self.manager.scan(self.s, 'course-one', scope='chapters', chapter_ids=ids)
        self.manager.refresh_courses(self.s)
        with self.assertRaises(ServiceError):
            self.manager.scan(self.s, 'course-one', scope='chapters', chapter_ids=['0'])
        self.assertEqual(self.s.platform.cards, [('course-one', '0')])


class VerificationTests(unittest.TestCase):
    def test_redirect_is_identified_before_requesting_challenge_page(self):
        req = urllib.request.Request('https://mooc2-ans.chaoxing.com/visit/stucoursemiddle')
        with self.assertRaises(PlatformVerificationRequired):
            ScopedRedirect().redirect_request(req, None, 302, '', {},
                'https://mooc2-ans.chaoxing.com/antispiderShowVerify.ac?private=not-exposed')

    def test_challenge_stops_later_requests_in_same_session(self):
        p = Platform('_uid=test')
        p.pacer = RequestPacer(interval=0)
        try:
            with patch.object(p.local.opener, 'open', side_effect=PlatformVerificationRequired()) as opened:
                for _ in range(2):
                    with self.assertRaises(PlatformVerificationRequired):
                        p.request('https://mooc2-ans.chaoxing.com/visit/stucoursemiddle')
                self.assertEqual(opened.call_count, 1)
        finally:
            p.close()

    def test_verification_html_without_redirect_is_not_reported_as_login_failure(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return 'https://mooc2-ans.chaoxing.com/visit/stucoursemiddle'
            def read(self, limit): return "<title>提示页面</title><input name='ucode'>请输入验证码".encode()
        p = Platform('_uid=test')
        try:
            with patch.object(p.local.opener, 'open', return_value=Response()):
                with self.assertRaises(PlatformVerificationRequired):
                    p.request('https://mooc2-ans.chaoxing.com/visit/stucoursemiddle')
            self.assertTrue(p.stop_requests.is_set())
        finally:
            p.close()

    def test_pacer_shares_budget_and_cancellation_interrupts_wait(self):
        now, waits = [0.0], []
        class Stop:
            stopped = False
            def wait(self, delay):
                waits.append(delay)
                now[0] += delay
                return self.stopped
        stop = Stop()
        p = RequestPacer(clock=lambda: now[0])
        self.assertTrue(p.acquire(stop))
        self.assertTrue(p.acquire(stop))
        self.assertEqual(waits, [0, .5])
        stop.stopped = True
        self.assertFalse(p.acquire(stop))


if __name__ == '__main__':
    unittest.main()
