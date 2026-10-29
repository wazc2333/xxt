"""Chapter and homework share a bank, provider and encrypted user setting; no external calls."""
import json
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

from backend.answer_bank import AnswerBank
from backend.deepseek import ProviderError
from backend.homework import resolve_answers
from backend.platform import ChapterUnavailable
from backend.quiz import checked_form, run_quiz
from backend.service import Manager
from backend.tests.test_course_switch import LazyPlatform
from backend.tests.test_homework import FakeAI, form_html
from backend.tests.test_quiz import QuizPlatform
from backend.tests.test_service import finished_runner
from backend.work_form import TaskForm

KEY = 'sk-synthetic-shared-scenario-test'


def readable_form(short=True):
    return form_html(short=short).replace('<div class="questionLi">',
        '<input name="knowledgeid" value="30"><input name="jobid" value="job-quiz"><div class="questionLi">', 1)


class QuizAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.bank = AnswerBank(self.folder/'bank.sqlite3')
        self.ai = FakeAI()
        self.p = QuizPlatform()
        self.p.html = readable_form()
        self.cancel = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def form(self):
        return checked_form(self.p.html, 'https://mooc1.chaoxing.com/mooc-ans/work/dowork',
                            self.p.setting, self.p.study, self.p.att, allow_short_answer=True)

    def submit(self, key=KEY, resolver=None):
        if resolver is None:
            resolver = lambda form: resolve_answers(form, self.bank, 'owner', self.ai, key,
                                                    self.cancel, lambda *args: None)
        return run_quiz(self.p, self.p.setting, self.p.study, self.p.att, self.folder/'attempts',
                        cancel_event=self.cancel, answer_resolver=resolver)

    def test_bank_hit_and_short_answer_always_provider_with_same_key(self):
        question = self.form().answer_questions()[0]
        self.bank.put('owner', question, 'B', 'imported')
        self.bank.get = Mock(wraps=self.bank.get)
        self.ai.answer = Mock(wraps=self.ai.answer)
        result = self.submit()
        self.assertTrue(result['submitted'])
        self.assertEqual(result['randomizedCount'], 0)
        self.assertEqual([c.args[1]['type'] for c in self.bank.get.call_args_list], ['0'])
        self.assertEqual(self.ai.answer.call_args.args[1], KEY)
        self.assertEqual([q['type'] for q in self.ai.calls[0]], ['4'])
        fields = dict(urllib.parse.parse_qsl(self.p.posts[0].data.decode()))
        self.assertEqual(fields['answer1'], 'B')
        self.assertTrue(fields['answer2'].startswith('<p>'))

    def test_all_choice_hits_need_no_key_and_never_call_provider(self):
        self.p.html = readable_form(short=False)
        self.bank.put('owner', self.form().answer_questions()[0], 'B', 'imported')
        self.assertTrue(self.submit(key='')['submitted'])
        self.assertEqual(self.ai.calls, [])

    def test_provider_choices_cached_without_random_fallback(self):
        with patch('backend.quiz.secrets.choice', side_effect=AssertionError('no random fallback')):
            self.assertTrue(self.submit()['submitted'])
        self.assertEqual(self.bank.get('owner', self.form().answer_questions()[0]), 'A')
        self.assertEqual(len(self.ai.calls[0]), 2)
        journal = ''.join(p.read_text() for p in (self.folder/'attempts').glob('*.json'))
        self.assertNotIn(KEY, journal)
        self.assertNotIn('private-form-token', journal)

    def test_prior_submission_does_not_generate_or_charge_again(self):
        self.submit()
        calls = len(self.ai.calls)
        again = self.submit(key='')
        self.assertTrue(again['alreadySubmitted'])
        self.assertEqual(len(self.ai.calls), calls)
        self.assertEqual(len(self.p.posts), 1)

    def test_provider_failure_or_cancel_never_submits_or_claims(self):
        self.ai.answer = Mock(side_effect=ProviderError('SK unavailable'))
        with self.assertRaises(ProviderError):
            self.submit()
        self.assertEqual(self.p.posts, [])
        self.assertEqual(list((self.folder/'attempts').glob('*.json')), [])
        def cancel(questions, key, event):
            event.set()
            return {q['id']: 'A' if q['type']=='0' else '正文' for q in questions}
        self.ai.answer = cancel
        from backend.deepseek import AnswerError
        with self.assertRaises(AnswerError):
            self.submit()
        self.assertEqual(self.p.posts, [])

    def test_plain_legacy_question_text_and_sibling_options_are_readable(self):
        body = readable_form(short=False).replace('id="submitForm"', 'id="form1"')
        body = body.replace('class="questionLi"', 'class="TiMu"').replace(
            '<h3 class="mark_name">1. 一加一是多少？<span>（单选题）</span></h3>',
            '<div class="Zy_TItle"><i class="fl">1</i><div>【单选题】一加一是多少？</div></div>')
        for label, text in [('A','二'),('B','三')]:
            body = body.replace(f'<div><span class="num_option" data="{label}">{label}</span><div class="answer_p">{text}</div></div>',
                                f'<li><label><span class="num_option" data="{label}">{label}</span></label><a class="after">{text}</a></li>')
        form = TaskForm(body, 'https://mooc1.chaoxing.com/mooc-ans/work/dowork', allow_chapter_form=True)
        question = form.answer_questions()[0]
        self.assertEqual(question['text'], '一加一是多少？')
        self.assertEqual([o['text'] for o in question['options']], ['二','三'])
        protected = TaskForm(body.replace('class="Zy_TItle"', 'class="Zy_TItle font-cxsecret"'),
                             form.page_url, allow_chapter_form=True)
        with self.assertRaises(ValueError):
            protected.answer_questions()


class SharedSettingQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ai = FakeAI()
        self.ai.answer = Mock(wraps=self.ai.answer)
        self.manager = Manager(Path(self.temp.name), platform_factory=LazyPlatform,
                               runner=finished_runner, answerer=self.ai)
        _, self.s, _ = self.manager.login('synthetic')
        self.p = QuizPlatform()
        self.p.html = readable_form(short=False)
        self.s.platform.setting['attachments'].insert(0, {'type':'workid','job':True,'jobid':'quiz-job'})
        self.s.platform.run_quiz = lambda *args, **kwargs: run_quiz(
            self.p, self.p.setting, self.p.study, self.p.att, args[3], **kwargs)
        self.wait(self.manager.scan(self.s,'course-one',scope='outline'))

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def wait(self, info):
        job = self.manager.job(self.s, info['id'])
        job.thread.join(3)
        self.assertFalse(job.thread.is_alive())
        return job.snapshot()

    def start(self, policy='bank_submit', ids=None):
        return self.wait(self.manager.start(self.s,['course-one'],chapter_ids=ids or ['1'],quiz_policy=policy))

    def test_homework_setting_used_by_chapter_and_isolated_between_users(self):
        self.manager.homework_settings(self.s,KEY)
        self.assertTrue(self.manager.ai_settings(self.s)['configured'])
        _, other, _ = self.manager.login('second-synthetic')
        self.assertFalse(self.manager.ai_settings(other)['configured'])
        result = self.start()
        self.assertEqual(result['submittedQuizzes'],1)
        self.assertEqual(result['deepseekQuestions'],1)
        self.assertEqual(self.ai.answer.call_args.args[1],KEY)
        self.assertNotIn(KEY,json.dumps(result))
        self.manager.ai_settings(self.s,'')
        self.assertFalse(self.manager.homework_settings(self.s)['configured'])

    def test_provider_error_stops_before_next_chapter_or_video(self):
        self.ai.answer.side_effect = ProviderError('DeepSeek SK invalid')
        result = self.start(ids=['1','2'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['actionRequired'],'deepseek_configuration')
        self.assertEqual(self.s.platform.cards,[('course-one','1')])
        self.assertEqual(result['passedVideos'],0)
        self.assertEqual(self.p.posts,[])

    def test_random_policy_uses_no_bank_provider_or_key(self):
        self.p.html = self.p.html.replace('class="mark_name"', 'class="font-cxsecret mark_name"')
        self.manager.answer_bank.get = Mock(side_effect=AssertionError('random must bypass bank'))
        result = self.start('random_submit')
        self.assertEqual(result['submittedQuizzes'],1)
        self.ai.answer.assert_not_called()
        self.manager.answer_bank.get.assert_not_called()

    def test_unsupported_text_does_not_fall_back_to_random(self):
        self.p.html = self.p.html.replace('class="mark_name"','class="font-cxsecret mark_name"')
        with patch('backend.quiz.secrets.choice', side_effect=AssertionError('no fallback')):
            result = self.start()
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['submittedQuizzes'],0)
        self.assertEqual(result['passedVideos'],1)
        self.ai.answer.assert_not_called()
        self.assertEqual(self.p.posts,[])


if __name__ == '__main__':
    unittest.main()
