import copy
import html
import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from backend.answer_bank import AnswerBank
from backend.deepseek import AnswerError, DeepSeek, ENDPOINT, MODEL, NoRedirect, ProviderError
from backend.homework import checked_form, fetch_list, parse_list, public_list, resolve_answers
from backend.server import Server
from backend.service import Manager, ServiceError
from backend.tests.test_service import FakePlatform
from backend.work_form import TaskForm

KEY = 'sk-synthetic-local-test-only'
COURSE = {'id': 'course-one', 'name': '作业测试课程', 'courseid': '10', 'clazzid': '20', 'cpi': '40',
          'url': 'https://mooc1.chaoxing.com/course-home'}


def form_html(work_id='50', short=True):
    fields = {'courseId': '10', 'classId': '20', 'cpi': '40', 'workRelationId': work_id,
              'workAnswerId': str(int(work_id)+100), 'pyFlag': '', 'answerwqbid': '', 'enc_work': 'private-form-token'}
    text = '<form id="submitForm" method="post" action="/mooc-ans/work/addStudentWorkNewWeb?token=private-query-token">'
    text += ''.join(f'<input name="{k}" value="{v}">' for k, v in fields.items())
    text += '<div class="questionLi"><h3 class="mark_name">1. 一加一是多少？<span>（单选题）</span></h3>'
    text += '<input name="answertype1" value="0"><input name="answer1" value="">'
    text += '<div><span class="num_option" data="A">A</span><div class="answer_p">二</div></div>'
    text += '<div><span class="num_option" data="B">B</span><div class="answer_p">三</div></div></div>'
    if short:
        text += '<div class="questionLi"><h3 class="mark_name">2. 简述学习的方法。</h3>'
        text += '<input name="answertype2" value="4"><textarea name="answer2"></textarea></div>'
    return text+'</form>'


def list_html(done=(), page_count=1, work_ids=('50', '51')):
    body = '<input id="enc" value="list-issued-token"><input id="status" value="0"><input id="topicId" value="">'
    for work_id in work_ids:
        query = urllib.parse.urlencode({'courseId': '10', 'classId': '20', 'cpi': '40', 'workId': work_id,
                                       'answerId': '0', 'enc': 'issued-token'})
        link = html.escape('/mooc-ans/mooc2/work/task?'+query, quote=True)
        state = '已完成' if work_id in done else '未完成'
        body += f'<li onclick="goTask(this)" data="{link}"><p class="overHidden2 fl">测试作业 {work_id}</p><p class="status fl">{state}</p></li>'
    return body+f'<script>pageNum : {page_count}</script>'


class HomeworkPlatform(FakePlatform):
    def __init__(self, cookie):
        super().__init__(cookie)
        self.courses = {'course-one': copy.deepcopy(COURSE), 'course-two': {'id': 'course-two', 'name': '其他课程'}}
        self.events, self.posts, self.completed = [], [], set()
        self.short = True
        self.post_error = None
        self.validation = {'status': 3}

    def request(self, url, referer=None):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path == '/course-home':
            self.events.append(('home', ''))
            fields = {'courseid': '10', 'clazzid': '20', 'cpi': '40', 'workEnc': 'issued-work-token', 't': '1', 'enc': 'home-token'}
            return ''.join(f'<input id="{k}" value="{v}">' for k,v in fields.items())+'<li dataname="zy"><a data-url="/mooc2/work/list"></a></li>', url
        if parts.path.endswith('/work/list'):
            self.events.append(('list', ''))
            return list_html(self.completed), url
        if parts.path.endswith('/work/task'):
            self.events.append(('detail', query['workId']))
            return form_html(query['workId'], self.short), 'https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork'
        if parts.path.endswith('/work/validate'):
            self.events.append(('validate', ''))
            return json.dumps(self.validation), url
        raise AssertionError('Unexpected platform endpoint')

    def submit_form(self, request):
        self.posts.append(request)
        if self.post_error:
            raise self.post_error
        fields = dict(urllib.parse.parse_qsl(request.data.decode(), keep_blank_values=True))
        self.completed.add(fields['workRelationId'])
        return json.dumps({'status': True, 'stuStatus': 4}), request.full_url


class FakeAI:
    def __init__(self):
        self.calls = []
        self.entered, self.release = threading.Event(), None
        self.bad = False

    def answer(self, questions, key, cancel):
        if not key:
            raise AnswerError('请先填写 DeepSeek SK')
        self.calls.append(copy.deepcopy(questions))
        self.entered.set()
        if self.release:
            self.release.wait(3)
        if self.bad:
            return {q['id']: 'Z' for q in questions}
        return {q['id']: '认真阅读，结合练习与复盘。' if q['type']=='4' else q['options'][0]['label'] for q in questions}


class HomeworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ai = FakeAI()
        self.manager = Manager(Path(self.temp.name), platform_factory=HomeworkPlatform, answerer=self.ai)
        self.token, self.s, _ = self.manager.login('inert-cookie')

    def tearDown(self):
        if self.ai.release:
            self.ai.release.set()
        self.manager.close()
        self.temp.cleanup()

    def wait(self, job):
        thread = self.manager.job(self.s, job['id']).thread
        thread.join(5)
        self.assertFalse(thread.is_alive())
        return self.manager.job(self.s, job['id']).snapshot()

    def scan(self):
        result = self.wait(self.manager.scan_homework(self.s, 'course-one'))
        self.assertEqual(result['status'], 'completed')
        return self.manager.homework_catalog(self.s, 'course-one')['homework']

    def test_title_scan_never_opens_details_or_leaks_upstream_links(self):
        rows = self.scan()
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(event[0] == 'detail' for event in self.s.platform.events))
        self.assertEqual(set(rows[0]), {'id', 'name', 'selectable', 'isCompleted', 'statusText'})
        self.assertIs(rows[0]['isCompleted'], False)
        self.assertEqual(rows[0]['statusText'], '未完成')
        self.assertNotIn('issued-token', json.dumps(rows))

    def test_choice_bank_hit_and_short_answer_always_deepseek(self):
        rows = self.scan()
        form = checked_form(form_html(), 'https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork', COURSE, {'workId':'50'})
        question = form.answer_questions()[0]
        self.manager.answer_bank.put(self.s.record_id, question, 'B', 'imported')
        self.manager.homework_settings(self.s, KEY)
        result = self.wait(self.manager.start_homework(self.s, 'course-one', [rows[0]['id']]))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['bankHits'], result['deepseekQuestions'], result['shortAnswersGenerated']), (1,1,1))
        self.assertEqual(len(self.ai.calls), 1)
        self.assertEqual([q['type'] for q in self.ai.calls[0]], ['4'])
        body = dict(urllib.parse.parse_qsl(self.s.platform.posts[0].data.decode(), keep_blank_values=True))
        self.assertEqual(body['answer1'], 'B')
        self.assertIn('<p>', body['answer2'])
        self.assertEqual((result['submittedHomework'], result['confirmedHomework']), (1,1))
        self.assertFalse(self.manager.homework_catalog(self.s,'course-one')['homework'][0]['selectable'])

    def test_details_are_lazy_and_cancellation_prevents_second_detail_or_submission(self):
        rows = self.scan()
        self.manager.homework_settings(self.s, KEY)
        self.ai.release = threading.Event()
        job = self.manager.start_homework(self.s, 'course-one', [r['id'] for r in rows])
        self.assertTrue(self.ai.entered.wait(2))
        self.assertEqual([value for event,value in self.s.platform.events if event=='detail'], ['50'])
        self.manager.job(self.s, job['id']).cancel.set()
        self.ai.release.set()
        result = self.wait(job)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(self.s.platform.posts, [])
        self.assertEqual([value for event,value in self.s.platform.events if event=='detail'], ['50'])

    def test_local_choices_need_no_key_and_generated_choices_are_cached(self):
        self.s.platform.short = False
        rows = self.scan()
        self.manager.homework_settings(self.s, KEY)
        result = self.wait(self.manager.start_homework(self.s,'course-one',[rows[0]['id']]))
        self.assertEqual(result['deepseekQuestions'], 1)
        self.manager.homework_settings(self.s, '')
        result = self.wait(self.manager.start_homework(self.s,'course-one',[rows[1]['id']]))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['bankHits'], result['deepseekQuestions']), (1,0))
        self.assertEqual(len(self.ai.calls), 1)

    def test_unknown_post_outcome_is_not_retried_on_next_job(self):
        rows = self.scan()
        self.manager.homework_settings(self.s, KEY)
        self.s.platform.post_error = TimeoutError('unknown')
        for _ in range(2):
            self.wait(self.manager.start_homework(self.s,'course-one',[rows[0]['id']]))
        self.assertEqual(len(self.s.platform.posts), 1)
        self.assertNotIn(KEY, ''.join(p.read_text(encoding='utf-8') for p in Path(self.temp.name).rglob('*.json')))

    def test_missing_key_invalid_ai_and_validation_all_prevent_submit(self):
        rows = self.scan()
        self.assertEqual(self.wait(self.manager.start_homework(self.s,'course-one',[rows[0]['id']]))['status'], 'partial')
        self.manager.homework_settings(self.s, KEY)
        self.ai.bad = True
        self.wait(self.manager.start_homework(self.s,'course-one',[rows[0]['id']]))
        self.ai.bad = False
        self.s.platform.validation = {'status':2}
        self.wait(self.manager.start_homework(self.s,'course-one',[rows[0]['id']]))
        self.assertEqual(self.s.platform.posts, [])

    def test_course_ownership_and_active_job_limits(self):
        rows = self.scan()
        with self.assertRaises(ServiceError):
            self.manager.start_homework(self.s,'course-two',[rows[0]['id']])
        for ids in ([],None,[rows[0]['id']]*2,['foreign']):
            with self.subTest(ids=ids), self.assertRaises(ServiceError):
                self.manager.start_homework(self.s,'course-one',ids)
        self.manager.homework_settings(self.s, KEY)
        self.ai.release=threading.Event()
        job=self.manager.start_homework(self.s,'course-one',[rows[0]['id']])
        self.assertTrue(self.ai.entered.wait(2))
        with self.assertRaises(ServiceError): self.manager.scan_homework(self.s,'course-one')
        with self.assertRaises(ServiceError): self.manager.homework_settings(self.s,'other-valid-secret')
        self.ai.release.set()
        self.wait(job)

    def test_settings_and_question_bank_are_isolated_and_no_key_in_snapshots(self):
        self.manager.homework_settings(self.s,KEY)
        _,other,_=self.manager.login('other-inert-cookie')
        self.assertFalse(self.manager.homework_settings(other)['configured'])
        question=checked_form(form_html(),'https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork',COURSE,{'workId':'50'}).answer_questions()[0]
        self.manager.answer_bank.put(self.s.record_id,question,'A')
        self.assertEqual(self.manager.answer_bank.count(other.record_id),0)
        self.assertNotIn(KEY,json.dumps(self.manager.session_snapshot(self.s)))
        self.assertNotIn(KEY,json.dumps(self.manager.admin_users()))
        self.manager.logout(self.token)
        self.assertEqual(self.s.deepseek_key,'')

    def test_provider_failure_stops_before_later_homework_details(self):
        rows=self.scan()
        self.manager.homework_settings(self.s,KEY)
        with patch.object(self.ai,'answer',side_effect=ProviderError('DeepSeek 余额不足')):
            result=self.wait(self.manager.start_homework(self.s,'course-one',[r['id'] for r in rows]))
        self.assertEqual(result['status'],'failed')
        self.assertEqual([value for event,value in self.s.platform.events if event=='detail'],['50'])
        self.assertEqual(self.s.platform.posts,[])


class AnswerBankTests(unittest.TestCase):
    def test_content_match_remaps_shuffled_option_labels_and_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bank.sqlite3'
            bank=AnswerBank(path)
            q={'id':'1','type':'0','text':'问题正文','options':[{'label':'A','text':'甲'},{'label':'B','text':'乙'}]}
            bank.put('owner',q,'A')
            changed=copy.deepcopy(q)
            changed['id']='99'
            changed['options']=[{'label':'A','text':'乙'},{'label':'B','text':'甲'}]
            self.assertEqual(AnswerBank(path).get('owner',changed),'B')
            self.assertIsNone(bank.get('other',changed))
            exported=bank.export('owner')
            self.assertEqual(bank.import_questions('second',exported['questions']),1)
            self.assertEqual(bank.get('second',changed),'B')

    def test_short_answers_never_looked_up_and_duplicate_options_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            bank=AnswerBank(Path(folder)/'bank.sqlite3')
            self.assertIsNone(bank.get('owner',{'type':'4'}))
            q={'type':'0','text':'问题','options':[{'label':'A','text':'相同'},{'label':'B','text':'相同'}]}
            with self.assertRaises(ValueError): bank.put('owner',q,'A')


class ProtocolTests(unittest.TestCase):
    def test_list_completion_closed_and_unknown_states_are_distinct(self):
        cases = [('已完成', True, False), ('已提交', True, False), ('待批阅', True, False),
                 ('已批阅', True, False), ('待互评', True, False), ('未交', False, True),
                 ('未完成', False, True), ('已截止', False, False), ('未开放', False, False),
                 ('未识别 private-token', None, True), ('', None, True)]
        for state, completed, selectable in cases:
            with self.subTest(state=state):
                rows, _, _ = parse_list(list_html(work_ids=('50',)).replace('>未完成<', '>'+state+'<'),
                                        'https://mooc1.chaoxing.com/mooc2/work/list', COURSE)
                row = public_list(rows)[0]
                self.assertIs(row['isCompleted'], completed)
                self.assertIs(row['selectable'], selectable)
                self.assertEqual(row['statusText'], state if completed is not None else '状态未知')
                self.assertNotIn('private-token', json.dumps(row))
                self.assertNotIn('url', row)

    def test_completed_list_state_is_exposed_without_reading_details(self):
        platform = HomeworkPlatform('inert')
        platform.completed.add('50')
        rows = public_list(fetch_list(platform, COURSE, threading.Event()))
        self.assertIs(rows[0]['isCompleted'], True)
        self.assertEqual(rows[0]['statusText'], '已完成')
        self.assertIs(rows[1]['isCompleted'], False)
        self.assertEqual([event[0] for event in platform.events], ['home', 'list'])

    def test_list_paging_never_fetches_details(self):
        platform=HomeworkPlatform('inert')
        original=platform.request
        paths=[]
        def request(url,referer=None):
            paths.append(url)
            if 'pageNum=2' in url: return list_html(work_ids=('51',)),url
            if '/work/list?' in url: return list_html(page_count=2,work_ids=('50',)),url
            return original(url,referer)
        platform.request=request
        rows=fetch_list(platform,COURSE,threading.Event())
        self.assertEqual(len(rows),2)
        self.assertTrue(any('pageNum=2' in u for u in paths))
        self.assertFalse(any('/work/task?' in u for u in paths))

    def test_mismatched_course_or_work_form_and_unknown_type_rejected(self):
        page='https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork'
        with self.assertRaises(AnswerError): checked_form(form_html('51'),page,COURSE,{'workId':'50'})
        with self.assertRaises(AnswerError): parse_list(list_html().replace('courseId=10','courseId=11'),'https://mooc1.chaoxing.com/mooc2/work/list',COURSE)
        form=TaskForm(form_html().replace('name="answertype2" value="4"','name="answertype2" value="11"'),page,allow_short_answer=True)
        with self.assertRaises(ValueError):form.answer_questions()

    def test_short_answer_is_opt_in_and_form_credentials_not_in_questions(self):
        page='https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork'
        form=TaskForm(form_html(),page)
        self.assertFalse(form.valid_answer('2','正文'))
        form=TaskForm(form_html(),page,allow_short_answer=True)
        questions=form.answer_questions()
        raw=json.dumps(questions)
        self.assertNotIn('private-form-token',raw)
        self.assertNotIn('private-query-token',raw)
        self.assertEqual(questions[0]['text'],'一加一是多少？')

    def test_rich_text_stem_and_decimal_prefix_are_preserved(self):
        page='https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork'
        source=form_html(short=False).replace('1. 一加一是多少？','<span style="color:red">1.5 倍等于多少？</span>')
        form=TaskForm(source,page)
        self.assertEqual(form.answer_questions()[0]['text'],'1.5 倍等于多少？')


class DeepSeekTests(unittest.TestCase):
    def test_official_model_low_effort_json_and_no_credential_forwarding(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,_):return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"answers":{"1":"A"}}','reasoning_content':'do not retain'}}]}).encode()
        class Opener:
            def open(self,request,timeout):self.request=request;return Response()
        opener=Opener()
        questions=[{'id':'1','type':'0','text':'题干','options':[{'label':'A','text':'甲'},{'label':'B','text':'乙'}]}]
        result=DeepSeek(opener).answer(questions,KEY,threading.Event())
        self.assertEqual(result,{'1':'A'})
        self.assertEqual(opener.request.full_url,ENDPOINT)
        body=json.loads(opener.request.data)
        self.assertEqual(body['model'],MODEL)
        self.assertEqual(body['reasoning_effort'],'low')
        self.assertEqual(body['thinking'],{'type':'enabled'})
        self.assertNotIn(KEY,opener.request.data.decode())
        self.assertNotIn('cookie',opener.request.data.decode().lower())

    def test_redirect_and_missing_key_are_rejected(self):
        with self.assertRaises(AnswerError):NoRedirect().redirect_request(None,None,302,'',{},'https://other.example')
        with self.assertRaises(AnswerError):DeepSeek().answer([], '', threading.Event())

    def test_truncated_or_wrong_question_output_is_not_accepted(self):
        class Response:
            def __init__(self,raw):self.raw=raw
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,_):return json.dumps(self.raw).encode()
        class Opener:
            def __init__(self,raw):self.raw=raw
            def open(self,*args,**kwargs):return Response(self.raw)
        q=[{'id':'1','type':'0','text':'题目','options':[{'label':'A','text':'甲'},{'label':'B','text':'乙'}]}]
        for finish,content in [('length','{"answers":{"1":"A"}}'),('stop','{"answers":{"2":"A"}}'),('stop','')]:
            raw={'choices':[{'finish_reason':finish,'message':{'content':content}}]}
            with self.subTest(finish=finish,content=content),self.assertRaises(AnswerError):
                DeepSeek(Opener(raw)).answer(q,KEY,threading.Event())


class HomeworkHTTPTests(unittest.TestCase):
    def test_authenticated_settings_import_and_jobs_api(self):
        with tempfile.TemporaryDirectory() as folder:
            manager=Manager(Path(folder),platform_factory=HomeworkPlatform,answerer=FakeAI())
            server=Server(('127.0.0.1',0),manager)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            port=server.server_address[1]
            token,session,_=manager.login('inert')
            def call(path,body=None,csrf=None):
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                headers={'Cookie':'learning_session='+token,'Origin':f'http://127.0.0.1:{port}','X-Requested-With':'learning-web',
                         'Content-Type':'application/json','X-CSRF-Token':session.csrf if csrf is None else csrf}
                conn.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),headers)
                response=conn.getresponse();status=response.status;data=json.load(response);conn.close();return status,data
            try:
                self.assertEqual(call('/api/homework/settings',{'key':KEY},csrf='wrong')[0],403)
                self.assertEqual(call('/api/ai/settings',{'key':KEY},csrf='wrong')[0],403)
                status,data=call('/api/ai/settings',{'key':KEY})
                self.assertEqual(status,200);self.assertTrue(data['configured'])
                self.assertNotIn(KEY,json.dumps(data))
                self.assertEqual(call('/api/ai/settings')[1],call('/api/homework/settings')[1])
                self.assertEqual(call('/api/ai/settings',{'key':None})[0],400)
                status,data=call('/api/homework/settings',{'key':KEY})
                self.assertEqual(status,200);self.assertTrue(data['configured']);self.assertNotIn(KEY,json.dumps(data))
                self.assertEqual(call('/api/homework/settings',{'key':None})[0],400)
                _,job=call('/api/courses/course-one/homework/scan',{})
                manager.job(session,job['id']).thread.join(3)
                _,catalog=call('/api/courses/course-one/homework')
                self.assertEqual(len(catalog['homework']),2)
                _,job=call('/api/homework/jobs',{'courseId':'course-one','workIds':[catalog['homework'][0]['id']]})
                manager.job(session,job['id']).thread.join(3)
                self.assertEqual(call('/api/jobs/'+job['id'])[1]['status'],'completed')
                self.assertEqual(call('/api/homework/bank')[1]['questions'][0]['answer'],'A')
            finally:
                server.shutdown();server.server_close();manager.close()


if __name__=='__main__':unittest.main()
