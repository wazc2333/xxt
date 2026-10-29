"""Administrative mutations operate only on the selected user's session, never real services."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.service import Manager, ServiceError
from backend.tests import test_admin as admin_http
from backend.tests.test_homework import HomeworkPlatform, FakeAI
from backend.tests.test_remember_login import RememberedPlatform, COOKIE
from backend.tests.test_service import finished_runner

KEY='sk-synthetic-admin-write-only'
QUESTION={'type':'0','text':'一加一','options':[{'label':'A','text':'二'},{'label':'B','text':'三'}]}


class ControlPlatform(HomeworkPlatform):
    def __init__(self,cookie):
        super().__init__(cookie)
        self.uid=cookie if cookie.isdigit() else ''

    def account_profile(self):
        return {'uid':self.uid,'displayName':'合成管理测试用户'}

    def chapters(self,course):
        return [{'id':'one','name':'第一章','isCompleted':False,'remainingTasks':1}]


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.manager=Manager(Path(self.temp.name),platform_factory=ControlPlatform,runner=finished_runner,answerer=FakeAI())
        self.token,self.s,_=self.manager.login('10001')

    def tearDown(self):
        self.manager.close();self.temp.cleanup()

    def action(self,action,body=None,session=None):
        return self.manager.admin_user_action((session or self.s).record_id,action,body or {})

    def wait(self,result,session=None):
        job=self.manager.job(session or self.s,result['id']);job.thread.join(3)
        self.assertFalse(job.thread.is_alive());return job.snapshot()

    def scan(self,kind='chapters'):
        self.wait(self.action('scan',{'kind':kind,'courseId':'course-one'}))

    def test_write_only_key_uses_shared_setting_without_leaking_or_touching_activity(self):
        before=self.s.touched
        self.assertEqual(self.action('sk',{'key':KEY}),{'ok':True,'configured':True})
        self.assertTrue(self.manager.ai_settings(self.s)['configured'])
        data=self.manager.admin_control(self.s.record_id)
        self.assertEqual(self.s.touched,before)
        self.assertTrue(data['configured'])
        for secret in (KEY,self.token,self.s.csrf,'issued-token','course-home'):
            self.assertNotIn(secret,json.dumps(data))
        self.assertFalse(self.action('sk',{'key':''})['configured'])
        for key in (None,123,'bad'):
            with self.subTest(key=key),self.assertRaises(ServiceError):self.action('sk',{'key':key})

    def test_chapter_start_stop_and_restart_are_scoped_and_busy_is_enforced(self):
        self.scan()
        entered=threading.Event()
        def gated(ctx,transport,folder,**options):
            entered.set();options['stop_event'].wait(3);return {'stopped':True}
        self.manager.runner=gated
        info=self.action('start',{'kind':'chapters','courseId':'course-one','chapterIds':['one'],'quizPolicy':'skip'})
        self.assertTrue(entered.wait(2))
        with self.assertRaises(ServiceError):self.action('sk',{'key':KEY})
        with self.assertRaises(ServiceError):self.action('start',{'kind':'chapters','courseId':'course-one','chapterIds':['one']})
        _,other,_=self.manager.login('20002')
        with self.assertRaises(ServiceError):self.action('stop',{'jobId':info['id']},session=other)
        self.assertTrue(self.action('stop',{'jobId':info['id']})['stopRequested'])
        self.assertEqual(self.wait(info)['status'],'stopped')
        self.manager.runner=finished_runner
        again=self.wait(self.action('start',{'kind':'chapters','courseId':'course-one','chapterIds':['one'],'quizPolicy':'skip'}))
        self.assertEqual(again['status'],'completed')
        self.assertEqual(again['selectedChapterIds'],['one'])

    def test_no_selection_or_foreign_course_never_falls_back_to_full_course(self):
        self.scan()
        for body in ({'kind':'chapters','courseId':'course-one'},
                     {'kind':'chapters','courseId':'course-one','chapterIds':[]},
                     {'kind':'chapters','courseId':'course-one','chapterIds':['foreign']},
                     {'kind':'chapters','courseId':'foreign','chapterIds':['one']}):
            with self.subTest(body=body),self.assertRaises(ServiceError):self.action('start',body)
        self.assertFalse(self.s.busy)
        self.assertEqual(len(self.s.jobs),1)

    def test_homework_scan_and_start_reuse_normal_lazy_workflow(self):
        self.scan('homework')
        info=self.manager.admin_control(self.s.record_id,course_id='course-one',kind='homework')
        self.assertEqual(len(info['catalog']['homework']),2)
        self.assertFalse(any(e[0]=='detail' for e in self.s.platform.events))
        self.action('sk',{'key':KEY})
        work_id=info['catalog']['homework'][0]['id']
        result=self.wait(self.action('start',{'kind':'homework','courseId':'course-one','workIds':[work_id]}))
        self.assertEqual(result['submittedHomework'],1)
        self.assertEqual([e for e in self.s.platform.events if e[0]=='detail'],[('detail','50')])

    def test_delete_uid_removes_all_logins_bank_and_live_sessions_but_not_other_users(self):
        second,other_session,_=self.manager.login('10001')
        third,different,_=self.manager.login('20002')
        historical,_,_=self.manager.login('10001');self.manager.logout(historical)
        for session in (self.s,other_session,different):self.manager.answer_bank.put(session.record_id,QUESTION,'A','imported')
        result=self.action('delete')
        self.assertEqual(result['deletedRecords'],3)
        for token in (self.token,second):
            with self.assertRaises(ServiceError):self.manager.session(token)
        self.assertIs(self.manager.session(third),different)
        self.assertEqual(self.manager.admin_users()['total'],1)
        for session in (self.s,other_session):
            self.assertTrue(session.closed);self.assertEqual(session.deepseek_key,'')
            self.assertEqual(self.manager.answer_bank.count(session.record_id),0)
            self.manager.answer_bank.put(session.record_id,QUESTION,'A')
            self.assertEqual(self.manager.answer_bank.count(session.record_id),0)
        self.assertEqual(self.manager.answer_bank.count(different.record_id),1)

    def test_delete_unknown_uid_only_removes_selected_record(self):
        a,one,_=self.manager.login('unknown-one');b,two,_=self.manager.login('unknown-two')
        self.assertEqual(self.action('delete',session=one)['deletedRecords'],1)
        with self.assertRaises(ServiceError):self.manager.session(a)
        self.assertIs(self.manager.session(b),two)

    def test_deleted_user_running_job_stops_and_cannot_start_again(self):
        self.scan();entered=threading.Event()
        def gated(ctx,transport,folder,**options):
            entered.set();options['stop_event'].wait(3);return {'stopped':True}
        self.manager.runner=gated
        info=self.action('start',{'kind':'chapters','courseId':'course-one','chapterIds':['one'],'quizPolicy':'skip'})
        self.assertTrue(entered.wait(2));self.action('delete')
        self.assertEqual(self.wait(info)['status'],'stopped')
        with self.assertRaises(ServiceError):self.action('start',{'kind':'chapters','courseId':'course-one','chapterIds':['one']})
        self.assertTrue(self.s.platform.closed)

    def test_expired_user_controls_do_not_revive_login(self):
        self.s.touched=self.manager.clock()-self.manager.session_ttl-1
        self.assertFalse(self.manager.admin_control(self.s.record_id)['active'])
        for action,body in (('sk',{'key':KEY}),('stop',{'jobId':'bad'}),('scan',{'courseId':'course-one','kind':'chapters'})):
            with self.subTest(action=action),self.assertRaises(ServiceError):self.action(action,body)


class PersistentControlTests(unittest.TestCase):
    def test_key_is_encrypted_restored_and_account_deletion_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=Manager(Path(directory),platform_factory=RememberedPlatform)
            token,s,_=manager.login(COOKIE)
            manager.admin_user_action(s.record_id,'sk',{'key':KEY})
            for path in Path(directory).iterdir():
                if path.is_file():self.assertNotIn(KEY.encode(),path.read_bytes())
            manager.close();manager=Manager(Path(directory),platform_factory=RememberedPlatform)
            self.assertEqual(manager.session(token).deepseek_key,KEY)
            manager.admin_user_action(s.record_id,'delete',{})
            self.assertEqual(list(manager.session_store.load()),[])
            manager.close();manager=Manager(Path(directory),platform_factory=RememberedPlatform)
            try:
                self.assertFalse(manager.sessions);self.assertEqual(manager.admin_users()['total'],0)
            finally:manager.close()

    def test_deleted_history_blocks_restore_even_if_credential_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=Manager(Path(directory),platform_factory=RememberedPlatform)
            token,s,_=manager.login(COOKIE)
            with patch.object(manager.session_store,'delete',side_effect=OSError('disk failed')),patch.object(manager.session_store,'delete_records',side_effect=OSError('disk failed')):
                with self.assertRaises(ServiceError):manager.admin_user_action(s.record_id,'delete',{})
            self.assertTrue(s.closed);manager.close()
            manager=Manager(Path(directory),platform_factory=RememberedPlatform)
            try:
                self.assertFalse(manager.sessions);self.assertEqual(list(manager.session_store.load()),[])
            finally:manager.close()


class ControlHTTPTests(unittest.TestCase):
    setUp=admin_http.AdminHTTPTests.setUp
    tearDown=admin_http.AdminHTTPTests.tearDown
    request=admin_http.AdminHTTPTests.request
    admin_login=admin_http.AdminHTTPTests.admin_login

    def test_admin_mutations_require_admin_same_origin_and_csrf_and_never_return_key(self):
        token,s,_=self.manager.login('synthetic')
        root='/api/admin/users/'+s.record_id
        for action in ('sk','start','stop','scan','delete'):
            self.assertEqual(self.request(root+'/'+action,{},cookie='learning_session='+token)[0],401)
        cookie,csrf=self.admin_login()
        for action in ('sk','start','stop','scan','delete'):
            self.assertEqual(self.request(root+'/'+action,{},cookie=cookie,csrf='wrong')[0],403)
            self.assertEqual(self.request(root+'/'+action,{},cookie=cookie,csrf=csrf,origin='https://other.example')[0],403)
        status,result,_=self.request(root+'/sk',{'key':KEY},cookie=cookie,csrf=csrf)
        self.assertEqual(status,200);self.assertEqual(result,{'ok':True,'configured':True})
        control=self.request(root+'/control',cookie=cookie)[1]
        self.assertNotIn(KEY,json.dumps(control));self.assertNotIn(token,json.dumps(control))
        for query in ('kind=other','kind=chapters&kind=homework','a=1&b=2&c=3&d=4&e=5'):
            self.assertEqual(self.request(root+'/control?'+query,cookie=cookie)[0],400)
        self.assertEqual(self.request(root+'/delete',{},cookie=cookie,csrf=csrf)[0],200)
        self.assertEqual(self.request('/api/session',cookie='learning_session='+token)[0],401)
        self.assertEqual(self.request(root+'/control',cookie=cookie)[0],404)


if __name__=='__main__':unittest.main()
