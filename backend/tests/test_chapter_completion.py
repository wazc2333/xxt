import tempfile
import unittest
from pathlib import Path

from backend.platform import Document, Platform, chapter_completion, directory_task_progress
from backend.service import Manager
from backend.tests.test_course_switch import LazyPlatform
from backend.tests.test_service import finished_runner


DONE = '<div class="catalog_task"><div class="catalog_state icon_yiwanc"><span class="bntHoverTips">已完成</span></div></div>'
PENDING = '<div class="catalog_task"><input class="knowledgeJobCount" value="2"><div class="catalog_jindu"><span class="catalog_points_yi">2</span><span class="bntHoverTips">2个待完成任务点</span></div></div>'


def row(identity, title, marker, course='10', clazz='20'):
    return f'''<div class="chapter_item" title="{title}" onclick="toOld('{course}','{identity}','{clazz}',0)">{marker}</div>'''


class DirectoryPlatform(Platform):
    def __init__(self, cookie=None, *, jar=None):
        super().__init__(cookie, jar=jar)
        self.calls = []
        self.changed = False

    def list_courses(self):
        self.courses = {
            'one': {'id':'one', 'name':'第一课程', 'url':'https://mooc2-ans.chaoxing.com/home-one', 'courseid':'10','clazzid':'20','cpi':'40'},
            'two': {'id':'two', 'name':'第二课程', 'url':'https://mooc2-ans.chaoxing.com/home-two', 'courseid':'11','clazzid':'21','cpi':'41'}}
        return [{'id': c['id'], 'name': c['name']} for c in self.courses.values()]

    def account_profile(self):
        return {}

    def request(self, url, referer=None):
        self.calls.append(url)
        if '/home-' in url:
            second = url.endswith('two')
            course, clazz, cpi = ('11','21','41') if second else ('10','20','40')
            fields = {'courseid':course,'clazzid':clazz,'cpi':cpi,'enc':'private-home-token','t':'123'}
            page = ''.join(f'<input id="{k}" value="{v}">' for k,v in fields.items())
            return page+'<li dataname="zj"><a data-url="/mooc2-ans/mycourse/studentcourse"></a></li>', url
        if '/studentcourse?' in url:
            if 'courseid=11' in url:
                body = '<h2 class="xs_head_name">已完成任务点: 0/14</h2>'+row('31','第二课程小节',PENDING,'11','21')
            else:
                header = '<h2 class="xs_head_name">已完成任务点: 184/262</h2>'
                body = header+row('30','已完成小节',DONE)+row('32','待完成小节', DONE if self.changed else PENDING)+row('33','未知状态小节','')
            return '<script>var enc="abcdef";</script>'+body, url
        raise AssertionError('Outline must not open shells or task cards')

    def prepare_catalog(self, *_args):
        raise AssertionError('Outline must not initialize a study shell')

    def chapter_cards(self, *_args, **_kwargs):
        raise AssertionError('Outline must not read task details')


class CompletionMarkerTests(unittest.TestCase):
    def parse(self, marker):
        return chapter_completion(Document(marker).root)

    def test_server_tick_and_remaining_count_are_distinct(self):
        self.assertEqual(self.parse(DONE), {'isCompleted':True,'remainingTasks':0})
        self.assertEqual(self.parse(PENDING), {'isCompleted':False,'remainingTasks':2})

    def test_unknown_contradictory_or_invalid_marker_is_not_completed(self):
        variants = ('<div class="catalog_task"></div>',
                    PENDING.replace('value="2"','value="3"'),
                    PENDING.replace('2个待完成任务点','0个待完成任务点'),
                    PENDING.replace('class="catalog_jindu"','class="catalog_jindu icon_yiwanc"'),
                    '<div class="catalog_task"><span class="catalog_points_yi">0</span></div>')
        for value in variants:
            with self.subTest(marker=value):
                self.assertEqual(self.parse(value), {'isCompleted':None,'remainingTasks':None})

    def test_course_task_totals_come_from_header_not_chapter_counts_or_scripts(self):
        html = '<script>var text="已完成任务点:999/999";</script><h2 class="xs_head_name">已完成任务点：184 / 262</h2>'
        self.assertEqual(directory_task_progress(Document(html)), {'completed':184,'total':262})
        self.assertIsNone(directory_task_progress(Document('<h2 class="xs_head_name">已完成任务点:3/2</h2>')))
        self.assertIsNone(directory_task_progress(Document(html+'<h2 class="xs_head_name">已完成任务点:1/5</h2>')))
        self.assertEqual(directory_task_progress(Document('<h2 class="xs_head_name">已完成任务点:0/0</h2>')), {'completed':0,'total':0})


class OutlineCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name), platform_factory=DirectoryPlatform, persist_sessions=False)
        _, self.session, _ = self.manager.login('_uid=local-test')

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def outline(self, course):
        info = self.manager.scan(self.session, course, scope='outline')
        job = self.manager.job(self.session,info['id'])
        job.thread.join(3)
        self.assertEqual(job.snapshot()['status'],'completed')
        return self.manager.catalog(self.session,course)

    def test_all_completion_states_and_totals_need_only_two_directory_reads(self):
        data = self.outline('one')
        self.assertEqual(len(self.session.platform.calls), 2)
        self.assertEqual(data['tasks'], [])
        self.assertEqual([c['isCompleted'] for c in data['chapters']], [True,False,None])
        self.assertEqual(data['chapters'][1]['remainingTasks'], 2)
        self.assertEqual(data['taskProgress'], {'completed':184,'total':262})
        self.assertEqual(data['chapters'][0]['status'],'pending')
        self.assertNotIn('private-home-token', str(data))
        self.assertNotIn('abcdef', str(data))

    def test_course_caches_are_isolated_and_reread_updates_server_markers(self):
        first = self.outline('one')
        second = self.outline('two')
        self.assertEqual(second['taskProgress'], {'completed':0,'total':14})
        self.assertEqual(self.manager.catalog(self.session,'one')['taskProgress'],first['taskProgress'])
        self.session.platform.changed = True
        self.assertTrue(self.outline('one')['chapters'][1]['isCompleted'])

    def test_unrecognized_header_clears_previous_summary(self):
        self.outline('one')
        original = self.session.platform.request
        def request(url, referer=None):
            html, final = original(url, referer)
            return html.replace('xs_head_name','unknown-header'), final
        self.session.platform.request = request
        self.assertIsNone(self.outline('one')['taskProgress'])


class LocalProcessingTests(unittest.TestCase):
    def test_processing_success_does_not_invent_a_server_directory_tick(self):
        class PendingPlatform(LazyPlatform):
            def chapters(self,course):
                return [{**row,'isCompleted':False,'remainingTasks':2} for row in super().chapters(course)]
        with tempfile.TemporaryDirectory() as folder:
            manager = Manager(Path(folder),platform_factory=PendingPlatform,runner=finished_runner)
            try:
                _,session,_=manager.login('inert')
                scan=manager.scan(session,'course-one',scope='outline')
                manager.job(session,scan['id']).thread.join(3)
                job=manager.start(session,['course-one'],chapter_ids=['1'])
                manager.job(session,job['id']).thread.join(3)
                self.assertEqual(manager.job(session,job['id']).snapshot()['completedChapters'],1)
                chapter=next(c for c in manager.catalog(session,'course-one')['chapters'] if c['id']=='1')
                self.assertFalse(chapter['isCompleted'])
                self.assertEqual(chapter['remainingTasks'],2)
            finally:
                manager.close()


if __name__=='__main__':unittest.main()
