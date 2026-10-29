"""Sequential, student-scoped homework listing, answer resolution and one-shot submission."""
import hashlib
import html as html_lib
import json
import re
import time
import urllib.parse
from pathlib import Path

from playback_simulator.simulator import atomic_json
from .deepseek import AnswerError
from .platform import ChapterUnavailable, PlatformVerificationRequired, add_query, scoped_link
from .work_form import Document, TaskForm, QuestionParseError
from .font_decoder import FontDecodeError


def identity_matches(values, course):
    return all(str(values.get(k, '')) == str(course[v]) for k, v in
               (('courseId', 'courseid'), ('classId', 'clazzid'), ('cpi', 'cpi')))


def parse_list(html, url, course):
    doc = Document(html)
    rows = []
    for node in doc.root.walk():
        if node.tag != 'li' or not re.fullmatch(r'\s*goTask\(this\)\s*;?\s*', node.attrs.get('onclick', '')):
            continue
        target = scoped_link(url, node.attrs.get('data', ''))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(target).query))
        if (urllib.parse.urlsplit(target).path != '/mooc-ans/mooc2/work/task'
                or not identity_matches(query, course) or not query.get('workId', '').isdigit()
                or not query.get('answerId', '').isdigit() or not query.get('enc')):
            raise AnswerError('作业列表中的课程身份或入口参数不匹配')
        title = next((n.text().strip() for n in node.walk() if n.has_class('overHidden2')), '')
        state = next((n.text().strip() for n in node.walk() if n.has_class('status')), '')
        if not title:
            raise AnswerError('作业标题结构暂不支持')
        completed = any(s in state for s in ('已完成', '已提交', '待批阅', '已批阅', '待互评'))
        unavailable = any(s in state for s in ('已截止', '已过期', '未开始', '未开放', '已结束'))
        pending = any(s in state for s in ('未完成', '未交', '未提交', '待完成', '待提交', '待做'))
        labels = ('已完成', '已提交', '待批阅', '已批阅', '待互评', '已截止', '已过期',
                  '未开始', '未开放', '已结束', '未完成', '未交', '未提交', '待完成', '待提交', '待做')
        status_text = next((label for label in labels if label in state), '状态未知')
        work_id = query['workId']
        key = hashlib.sha256((':'.join(str(course[k]) for k in ('courseid', 'clazzid', 'cpi'))+':'+work_id).encode()).hexdigest()[:24]
        rows.append({'id': key, 'name': title[:300], 'url': target, 'workId': work_id,
                     'completed': completed, 'selectable': not completed and not unavailable,
                     'isCompleted': True if completed else False if pending or unavailable else None,
                     'statusText': status_text})
    match = re.search(r'\bpageNum\s*:\s*(\d+)', html)
    if not match and re.search(r'\.paging\s*\(', html):
        raise AnswerError('作业分页结构暂不支持，未截断列表')
    pages = int(match[1]) if match else 1
    if not 0 <= pages <= 50:
        raise AnswerError('作业列表页数超出支持范围，未截断列表')
    if not rows and not any(s in doc.root.text() for s in ('暂无', '没有作业', '还没有作业', '暂无作业')):
        raise AnswerError('作业列表格式暂不支持或需要重新登录')
    return rows, max(1, pages), doc


def fetch_list(platform, course, cancel):
    home, home_url = platform.request(course['url'])
    doc = Document(home)
    values = {n.attrs.get('id') or n.attrs.get('name'): n.attrs.get('value', '') for n in doc.root.walk() if n.tag == 'input'}
    if any(values.get(k) != str(course[k]) for k in ('courseid', 'clazzid', 'cpi')):
        raise AnswerError('作业课程主页身份不匹配')
    menus = [n for n in doc.root.walk() if n.tag == 'li' and n.attrs.get('dataname') == 'zy']
    links = [n.attrs['data-url'] for menu in menus for n in menu.walk() if n.attrs.get('data-url')]
    if len(links) != 1 or not values.get('workEnc'):
        raise AnswerError('未找到受支持的作业菜单或平台下发凭证')
    target = add_query(scoped_link(home_url, links[0]), {
        'courseId': course['courseid'], 'classId': course['clazzid'], 'cpi': course['cpi'],
        'enc': values['workEnc'], 'ut': 's', 't': values.get('t', ''), 'stuenc': values.get('enc', '')})
    if urllib.parse.urlsplit(target).path not in ('/mooc2/work/list', '/mooc-ans/mooc2/work/list'):
        raise AnswerError('作业菜单地址暂不支持')
    if cancel.is_set():
        return []
    body, final = platform.request(target, home_url)
    rows, pages, listing = parse_list(body, final, course)
    fields = {n.attrs.get('id'): n.attrs.get('value', '') for n in listing.root.walk() if n.tag == 'input'}
    for number in range(2, pages+1):
        if cancel.is_set():
            return []
        # Exact route used by the observed paging script; keep its list token and filters.
        page_url = add_query(scoped_link(final, '/mooc-ans/mooc2/work/list'), {
            'courseId': course['courseid'], 'classId': course['clazzid'], 'cpi': course['cpi'],
            'enc': fields.get('enc') or values['workEnc'], 'status': fields.get('status', '0'),
            'topicId': fields.get('topicId', ''), 'pageNum': number})
        page, page_final = platform.request(page_url, final)
        extra, _, _ = parse_list(page, page_final, course)
        rows.extend(extra)
    unique = {}
    for row in rows:
        unique[row['id']] = row
    if len(unique) > 1000:
        raise AnswerError('课程作业数量超出支持范围')
    return list(unique.values())


def public_list(rows):
    return [{k: row[k] for k in ('id', 'name', 'selectable', 'isCompleted', 'statusText')} for row in rows]


def checked_form(html, url, course, work):
    try:
        form = TaskForm(html, url, allow_chapter_form=True, allow_short_answer=True)
    except (ValueError, TypeError):
        raise AnswerError('作业答题页模板暂不支持，未提交') from None
    fields = dict(form.fields)
    if (not identity_matches(fields, course) or fields.get('workRelationId') != work['workId']
            or not fields.get('workAnswerId', '').isdigit() or fields['workAnswerId'] == '0'
            or 'exam' in urllib.parse.urlsplit(url).path.lower()
            or urllib.parse.urlsplit(form.action).hostname != urllib.parse.urlsplit(url).hostname):
        raise AnswerError('作业表单与当前用户所选作业身份不匹配')
    for key in ('courseId', 'classId', 'cpi', 'workRelationId', 'workAnswerId', 'pyFlag', 'answerwqbid'):
        if sum(name == key for name, _ in form.fields) != 1:
            raise AnswerError('作业必要字段缺失或重复')
    for qid in form.question_ids:
        if sum(name == 'answer'+qid for name, _ in form.fields) != 1:
            raise AnswerError('作业答案控件暂不支持')
    controls = {n.attrs.get('id'): n.attrs.get('value', '') for n in form.document.root.walk() if n.tag == 'input'}
    try:
        if int(controls.get('addTimes') or 0) > int(controls.get('limitWorkSubmitTimes') or 100):
            raise AnswerError('作业提交需要验证码，请在原网页完成')
    except ValueError:
        raise AnswerError('无法核对作业验证码状态') from None
    return form


def resolve_answers(form, bank, owner, ai, key, cancel, progress):
    try:
        questions = form.answer_questions()
    except (QuestionParseError, FontDecodeError) as exc:
        raise AnswerError(str(exc)) from None
    except ValueError:
        raise AnswerError('作业题目结构无法解析，未提交；请核对题干和实际选项控件') from None
    if len(questions) > 200:
        raise AnswerError('单份作业超过 200 道题，未提交')
    answers, pending, bank_hits = {}, [], 0
    for question in questions:
        if cancel.is_set():
            raise AnswerError('任务已停止')
        qid = question['id']
        if question['type'] == '4':
            pending.append(question)
            continue
        try:
            found = bank.get(owner, question)
        except ValueError as exc:
            raise AnswerError(str(exc)) from None
        if found is not None and form.valid_answer(qid, found):
            answers[qid] = found
            bank_hits += 1
        else:
            pending.append(question)
    progress(bank_hits, 0, 0, '本地题库匹配完成，正在获取缺少的答案' if pending else '本地题库匹配完成')
    generated, generated_short = 0, 0
    for index in range(0, len(pending), 10):
        batch = pending[index:index+10]
        result = ai.answer(batch, key, cancel)
        for question in batch:
            value = result.get(question['id'])
            if not form.valid_answer(question['id'], value):
                raise AnswerError('DeepSeek 未给出符合题目选项或简答长度要求的答案，未提交')
            answers[question['id']] = value
        generated += len(batch)
        generated_short += sum(q['type'] == '4' for q in batch)
        progress(bank_hits, generated, generated_short, 'DeepSeek 已生成 '+str(generated)+' 道题的答案')
    if cancel.is_set():
        raise AnswerError('任务已停止')
    for question in pending:
        if question['type'] != '4':
            bank.put(owner, question, answers[question['id']], 'deepseek')
    for question in questions:
        if question['type'] == '4':
            value = answers[question['id']]
            answers[question['id']] = '<p>'+html_lib.escape(value).replace('\n', '<br/>')+'</p>'
    return answers


def process(platform, course, work, bank, owner, ai, key, directory, cancel, progress):
    if cancel.is_set():
        return {'stopped': True}
    html, url = platform.request(work['url'], course['url'])
    form = checked_form(html, url, course, work)
    fields = dict(form.fields)
    identity = [fields[k] for k in ('courseId', 'classId', 'cpi', 'workRelationId', 'workAnswerId')]
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/(hashlib.sha256(json.dumps(identity).encode()).hexdigest()+'.json')
    if path.exists():
        raise AnswerError('此作业已有提交记录，请先在原网页核对，未重复提交')
    answers = resolve_answers(form, bank, owner, ai, key, cancel, progress)
    try:
        request = form.prepare(answers)
    except ValueError:
        raise AnswerError('答案无法按当前作业表单完整序列化，未提交') from None
    if cancel.is_set():
        return {'stopped': True}
    validation = add_query(scoped_link(url, '/mooc-ans/work/validate'),
                           {k: fields[k] for k in ('courseId', 'classId', 'cpi')})
    try:
        check = json.loads(platform.request(validation, url)[0])
    except (ValueError, TypeError):
        raise AnswerError('作业提交前校验响应无法识别，未提交') from None
    if not isinstance(check, dict) or type(check.get('status')) is not int or check['status'] != 3:
        raise AnswerError('作业提交要求登录或额外验证，请在原网页处理')
    if cancel.is_set():
        return {'stopped': True}
    record = {'status': 'pending', 'createdAt': int(time.time()), 'questionCount': len(answers)}
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(record, stream)
    except FileExistsError:
        raise AnswerError('作业已有提交记录，未重复提交') from None
    try:
        raw, _ = platform.submit_form(request)
        response = json.loads(raw)
        if not isinstance(response, dict) or response.get('status') is not True:
            record['status'] = 'rejected'
            atomic_json(path, record)
            raise AnswerError('平台未接受作业提交，未自动重试')
        record.update(status='accepted', acceptedAt=int(time.time()))
        atomic_json(path, record)
    except (AnswerError, PlatformVerificationRequired):
        raise
    except Exception:
        raise AnswerError('作业提交结果不明，已保留记录；请在原网页核对，未自动重试') from None
    return {'submitted': True}
