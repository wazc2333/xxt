import copy
import json
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from backend.platform import ChapterUnavailable, Platform, RequestPacer, WriteRedirect, PlatformVerificationRequired
from backend.quiz import checked_form, choose_answers, entry_url, run_quiz
from backend.work_form import TaskForm


def fixture():
    att = {'type': 'workid', 'job': True, 'jobid': 'job-quiz', 'enc': 'issued-private-enc',
           'property': {'mid': 'm1', 'workid': '55', 'schoolid': '0'}}
    setting = {'control': True, 'mooc2': 1, 'defaults': {'courseid': '10', 'clazzId': '20', 'knowledgeid': '30', 'cpi': '40'}, 'attachments': [att]}
    study = {'url': 'https://mooc1.chaoxing.com/mycourse/studentstudy',
             'cards_url': 'https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?courseid=10&clazzid=20&knowledgeid=30&num=0',
             'utEnc': 'issued-private-ut', 'workModules': [{'mid': 'm1', 'workid': '55', 'schoolid': '0', 'jobid': 'job-quiz', '_jobid': 'origin-job', 'courseid': '10'}]}
    fields = {'courseId': '10', 'classId': '20', 'knowledgeid': '30', 'cpi': '40', 'jobid': 'job-quiz',
              'workRelationId': '50', 'workAnswerId': '60', 'pyFlag': '', 'answerwqbid': '', 'opaque': 'issued-form-token'}
    html = '<form id="submitForm" method="post" action="/mooc-ans/work/addStudentWorkNewWeb?token=issued-query-token">'
    html += ''.join(f'<input name="{k}" value="{v}">' for k, v in fields.items())
    for qid, typ, value, options in [('1', '0', 'B', ['A', 'B']), ('2', '1', '', ['A', 'C']), ('3', '3', '', ['true', 'false'])]:
        html += f'<div class="questionLi"><input name="answertype{qid}" value="{typ}"><input name="answer{qid}" value="{value}">'
        html += ''.join(f'<span class="num_option" data="{option}">{option}</span>' for option in options) + '</div>'
    return setting, study, att, html + '</form>'


class QuizPlatform:
    def __init__(self):
        self.setting, self.study, self.att, self.html = fixture()
        self.calls, self.posts = [], []
        self.validation = {'status': 3}
        self.response = {'status': True, 'stuStatus': 4}
        self.passed = False
        self.failure = None

    def request(self, url, referer):
        self.calls.append(url)
        if '/api/work?' in url:
            return self.html, 'https://mooc1.chaoxing.com/mooc-ans/work/dowork'
        if '/work/validate?' in url:
            return json.dumps(self.validation), url
        if '/knowledge/cards?' in url:
            setting = copy.deepcopy(self.setting)
            setting['attachments'][0]['isPassed'] = self.passed
            return 'var mArg=' + json.dumps(setting), url
        raise AssertionError('Unexpected request')

    def submit_form(self, request):
        self.posts.append(request)
        if self.failure:
            raise self.failure
        return json.dumps(self.response), request.full_url


class QuizTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.p = QuizPlatform()
        self.cancel = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def run_quiz(self):
        return run_quiz(self.p, self.p.setting, self.p.study, self.p.att, Path(self.temp.name), cancel_event=self.cancel)

    def test_actual_module_fields_match_source_student_entry(self):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(entry_url(self.p.setting, self.p.study, self.p.att)).query, keep_blank_values=True))
        self.assertEqual(query['workId'], '0-55')
        self.assertEqual(query['originJobId'], 'origin-job')
        self.assertEqual(query['ut'], 's')
        self.assertEqual(query['enc'], self.p.att['enc'])
        self.assertNotIn('teacherPreview', query)

    def test_keep_existing_actual_choices_and_form_tokens_and_distinguish_completion(self):
        with patch('backend.quiz.secrets.choice', side_effect=lambda labels: labels[-1]):
            result = self.run_quiz()
        self.assertTrue(result['submitted'])
        self.assertFalse(result['isPassed'])
        self.assertEqual(result['randomizedCount'], 2)
        self.assertEqual(len(self.p.posts), 1)
        request = self.p.posts[0]
        body = dict(urllib.parse.parse_qsl(request.data.decode(), keep_blank_values=True))
        self.assertEqual(body['answer1'], 'B')
        self.assertEqual(body['answer2'], 'C')
        self.assertEqual(body['answer3'], 'false')
        self.assertEqual(body['pyFlag'], '')
        self.assertEqual(body['opaque'], 'issued-form-token')
        self.assertEqual(body['answerwqbid'], '1,2,3,')
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))
        self.assertEqual(query['token'], 'issued-query-token')
        self.assertEqual(query['formType'], 'post')
        journal = ''.join(p.read_text() for p in Path(self.temp.name).glob('*.json'))
        for secret in ('issued-form-token', 'issued-query-token', self.p.att['enc'], self.p.study['utEnc'], 'job-quiz'):
            self.assertNotIn(secret, journal)

    def test_confirmed_readback_and_repeated_job_do_not_submit_twice(self):
        self.p.passed = True
        self.assertTrue(self.run_quiz()['isPassed'])
        result = self.run_quiz()
        self.assertTrue(result['alreadySubmitted'])
        self.assertFalse(result['submitted'])
        self.assertEqual(len(self.p.posts), 1)

    def test_unknown_post_outcome_survives_restart_without_repeated_write(self):
        self.p.failure = TimeoutError('unknown')
        with self.assertRaises(ChapterUnavailable):
            self.run_quiz()
        self.p = QuizPlatform()
        with self.assertRaises(ChapterUnavailable):
            self.run_quiz()
        self.assertEqual(self.p.posts, [])

    def test_server_rejection_is_not_retried(self):
        self.p.response = {'status': False}
        for _ in range(2):
            with self.assertRaises(ChapterUnavailable):
                self.run_quiz()
        self.assertEqual(len(self.p.posts), 1)

    def test_validation_or_captcha_limit_prevents_submission(self):
        for status in (1, 2, None, '3'):
            self.p.validation = {'status': status}
            with self.subTest(status=status), self.assertRaises(ChapterUnavailable):
                self.run_quiz()
        self.p.validation = {'status': 3}
        self.p.html += '<input id="addTimes" value="101"><input id="limitWorkSubmitTimes" value="100">'
        with self.assertRaises(ChapterUnavailable):
            self.run_quiz()
        self.assertEqual(self.p.posts, [])

    def test_unsupported_type_and_nonexistent_saved_option_not_fabricated(self):
        original = self.p.html
        for html in (original.replace('name="answertype2" value="1"', 'name="answertype2" value="4"'),
                     original.replace('name="answer1" value="B"', 'name="answer1" value="D"'),
                     original.replace('class="num_option"', 'class="unrecognized"')):
            self.p.html = html
            with self.assertRaises(ChapterUnavailable):
                self.run_quiz()
        self.assertEqual(self.p.posts, [])

    def test_cross_chapter_identity_or_non_work_destination_rejected(self):
        original = self.p.html
        for html in (original.replace('name="knowledgeid" value="30"', 'name="knowledgeid" value="0"'),
                     original.replace('/mooc-ans/work/addStudentWorkNewWeb', '/exam/submit'),
                     original.replace('name="jobid" value="job-quiz"', 'name="jobid" value="other"')):
            self.p.html = html
            with self.assertRaises(ChapterUnavailable):
                self.run_quiz()
        self.assertEqual(self.p.posts, [])

    def test_cancel_does_not_open_or_submit_quiz(self):
        self.cancel.set()
        self.assertTrue(self.run_quiz()['stopped'])
        self.assertEqual(self.p.calls, [])
        self.assertEqual(self.p.posts, [])

    def test_mutating_transport_does_not_follow_redirect(self):
        import urllib.request
        from playback_simulator.chaoxing_client import RemoteError
        req = urllib.request.Request('https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNewWeb', data=b'x=1')
        with self.assertRaises(RemoteError):
            WriteRedirect().redirect_request(req, None, 302, '', {}, '/work/result')
        with self.assertRaises(PlatformVerificationRequired):
            WriteRedirect().redirect_request(req, None, 302, '', {}, '/antispiderShowVerify.ac')

    def test_accepted_submission_is_counted_even_when_readback_fails(self):
        from playback_simulator.chaoxing_client import RemoteError
        with patch('backend.quiz.confirmed', side_effect=RemoteError('temporary read error')):
            result = self.run_quiz()
        self.assertTrue(result['submitted'])
        self.assertFalse(result['isPassed'])

    def test_captcha_after_acceptance_preserves_acceptance_and_stops_session_flow(self):
        with patch('backend.quiz.confirmed', side_effect=PlatformVerificationRequired()):
            result = self.run_quiz()
        self.assertTrue(result['submitted'])
        self.assertEqual(result['actionRequired'], 'platform_verification')

    def test_submit_transport_sends_once_on_unknown_network_outcome(self):
        import urllib.error
        import urllib.request
        from unittest.mock import Mock
        from playback_simulator.chaoxing_client import RemoteError
        platform = Platform('_uid=unit-test')
        platform.pacer = RequestPacer(interval=0)
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError('private upstream URL must not leak')
        request = urllib.request.Request('https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNewWeb', data=b'answer1=A')
        try:
            with patch('backend.platform.urllib.request.build_opener', return_value=opener):
                with self.assertRaises(RemoteError) as caught:
                    platform.submit_form(request)
            self.assertEqual(opener.open.call_count, 1)
            self.assertNotIn('private upstream', str(caught.exception))
        finally:
            platform.close()

    def test_card_reader_carries_only_actual_work_module_and_literal_shell_token(self):
        import html
        platform = Platform('_uid=unit-test')
        module = html.escape(json.dumps(self.p.study['workModules'][0]), quote=True)
        def request(url, referer):
            if 'studentstudyAjax' in url:
                return '<input id="cardcount" value="1"><iframe src="/mooc-ans/knowledge/cards?courseid=10&amp;clazzid=20&amp;knowledgeid=30&amp;num=0"></iframe>', url
            if '/knowledge/cards?' in url:
                return 'var mArg=' + json.dumps(self.p.setting) + f';<iframe src="/ananas/modules/work/index.html" data="{module}"></iframe>', url
            return '<script>var utEnc="issued-private-ut";</script>', url
        platform.request = request
        try:
            cards = list(platform.chapter_cards({'courseid': '10', 'clazzid': '20', 'cpi': '40'},
                         {'id': '30', 'enc': 'unit-test', 'hidden': '0', 'referer': self.p.study['url']}))
            self.assertEqual(cards[0][1]['workModules'], self.p.study['workModules'])
            self.assertEqual(cards[0][1]['utEnc'], self.p.study['utEnc'])
        finally:
            platform.close()

    def test_lazy_work_iframe_is_read_and_unrelated_or_malformed_data_is_ignored(self):
        import html
        platform = Platform('_uid=unit-test')
        module = html.escape(json.dumps(self.p.study['workModules'][0]), quote=True)
        frames = (f'<iframe module="work" data="{module}"></iframe>'
                  f'<iframe module="video" data="{module}"></iframe>'
                  f'<div module="work" data="{module}"></div>'
                  '<iframe module="work" data="not-json"></iframe>')
        def request(url, referer):
            if 'studentstudyAjax' in url:
                return '<input id="cardcount" value="1"><iframe src="/mooc-ans/knowledge/cards?courseid=10&amp;clazzid=20&amp;knowledgeid=30&amp;num=0"></iframe>', url
            if '/knowledge/cards?' in url:
                return 'var mArg=' + json.dumps(self.p.setting) + ';' + frames, url
            return '<script>var utEnc="issued-private-ut";</script>', url
        platform.request = request
        try:
            cards = list(platform.chapter_cards({'courseid': '10', 'clazzid': '20', 'cpi': '40'},
                         {'id': '30', 'enc': 'unit-test', 'hidden': '0', 'referer': self.p.study['url']}))
            study = cards[0][1]
            self.assertEqual(study['workModules'], self.p.study['workModules'])
            self.assertIn('/api/work?', entry_url(self.p.setting, study, self.p.att))
        finally:
            platform.close()

    def legacy_form(self):
        return (self.p.html.replace('id="submitForm"', 'id="form1" name="form1"')
                .replace('class="questionLi"', 'class="TiMu newTiMu"')
                .replace('/mooc-ans/work/addStudentWorkNewWeb?', 'addStudentWorkNewWeb?')
                .replace('<span class="num_option" data="C">', '<span class="num_option_dx" data="C">'))

    def test_chapter_form1_supports_actual_single_multi_and_judgment_options(self):
        self.p.html = self.legacy_form()
        with patch('backend.quiz.secrets.choice', side_effect=lambda labels: labels[-1]):
            result = self.run_quiz()
        self.assertTrue(result['submitted'])
        self.assertFalse(result['isPassed'])
        self.assertEqual(len(self.p.posts), 1)
        request = self.p.posts[0]
        body = dict(urllib.parse.parse_qsl(request.data.decode(), keep_blank_values=True))
        self.assertEqual((body['answer1'], body['answer2'], body['answer3']), ('B', 'C', 'false'))
        self.assertEqual(body['opaque'], 'issued-form-token')
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query, keep_blank_values=True))
        self.assertEqual(query['pos'], '')
        self.assertEqual(query['token'], 'issued-query-token')
        self.assertEqual(urllib.parse.urlsplit(request.full_url).path, '/mooc-ans/work/addStudentWorkNewWeb')

    def test_chapter_form1_is_opt_in_and_draft_save_is_not_assumed(self):
        page = 'https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew'
        html = self.legacy_form()
        with self.assertRaises(ValueError):
            TaskForm(html, page)
        form = checked_form(html, page, self.p.setting, self.p.study, self.p.att)
        answers, _ = choose_answers(form)
        with self.assertRaises(ValueError):
            form.prepare(answers, mode='save')
        self.assertEqual(len(form.questions()), 3)

    def test_chapter_form1_existing_multi_choice_kept_and_normalized(self):
        html = self.legacy_form().replace('name="answer2" value=""', 'name="answer2" value="CA"')
        form = checked_form(html, 'https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew',
                            self.p.setting, self.p.study, self.p.att)
        answers, count = choose_answers(form)
        self.assertEqual(answers['2'], 'CA')
        self.assertEqual(count, 1)
        body = dict(urllib.parse.parse_qsl(form.prepare(answers).data.decode(), keep_blank_values=True))
        self.assertEqual(body['answer2'], 'AC')

    def test_chapter_form1_rejects_ambiguous_forms_and_missing_question_blocks(self):
        original = self.legacy_form()
        for html in (original + '<form id="submitForm"></form>',
                     original.replace('class="TiMu newTiMu"', 'class="unknown"'),
                     original.replace('name="knowledgeid" value="30"', 'name="knowledgeid" value="31"'),
                     original.replace('addStudentWorkNewWeb?', '/exam/submit?')):
            self.p.html = html
            with self.subTest(html=html[:40]), self.assertRaises(ChapterUnavailable):
                self.run_quiz()
        self.assertEqual(self.p.posts, [])

    def test_chapter_form1_unsupported_question_does_not_submit_partial_answers(self):
        self.p.html = self.legacy_form().replace('name="answertype2" value="1"', 'name="answertype2" value="4"')
        with self.assertRaises(ChapterUnavailable):
            self.run_quiz()
        self.assertEqual(self.p.posts, [])

    def test_chapter_module_still_requires_unique_identity_match(self):
        self.p.study['workModules'].append(copy.deepcopy(self.p.study['workModules'][0]))
        with self.assertRaises(ChapterUnavailable):
            entry_url(self.p.setting, self.p.study, self.p.att)


if __name__ == '__main__':
    unittest.main()
