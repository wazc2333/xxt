"""Remembered user sessions and bounded sequential learning jobs."""
from __future__ import annotations

import copy
import re
import secrets
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from playback_simulator.chaoxing_client import RemoteError, run_remote
from playback_simulator.simulator import atomic_json
from .platform import ChapterUnavailable, Platform, PlatformVerificationRequired, describe_task, task_id
from .user_records import UserRecords, identity, STATES

TERMINAL = {"completed", "partial", "failed", "stopped", "interrupted"}


def public_chapter(chapter):
    completed = chapter.get('isCompleted')
    if type(completed) is not bool:
        completed = None
    remaining = chapter.get('remainingTasks')
    if type(remaining) is not int or not 0 <= remaining <= 10000 or completed is None:
        remaining = None
    return {'id': chapter['id'], 'name': chapter['name'], 'status': 'pending',
            'isCompleted': completed, 'remainingTasks': remaining,
            'completionSource': 'server_directory' if completed is not None else None}


def public_directory_progress(platform, course_id):
    summaries = getattr(platform, 'chapter_progress', {})
    values = summaries.get(course_id) if isinstance(summaries, dict) else None
    if (not isinstance(values, dict) or type(values.get('completed')) is not int or type(values.get('total')) is not int
            or not 0 <= values['completed'] <= values['total'] <= 1000000):
        return None
    return {'completed': values['completed'], 'total': values['total']}


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass
class Session:
    platform: object
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    touched: float = field(default_factory=time.monotonic)
    jobs: dict = field(default_factory=dict)
    catalogs: dict = field(default_factory=dict)
    chapter_sources: dict = field(default_factory=dict)
    busy: bool = False
    closed: bool = False
    record_id: str = field(default_factory=lambda: secrets.token_hex(16))
    profile: dict = field(default_factory=dict)
    login_method: str = 'cookie'
    login_at: int = 0
    last_active_at: int = 0
    last_persisted: float = 0
    homework_catalogs: dict = field(default_factory=dict)
    deepseek_key: str = ''


class Job:
    def __init__(self, courses, *, kind="playback", task_ids=None):
        self.id = secrets.token_hex(16)
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.catalog_preview = None
        self.data = {"id": self.id, "kind": kind, "status": "queued", "createdAt": int(time.time()),
                     "selectedTaskIds": list(task_ids or []), "selectedTaskCount": len(task_ids or []),
                     "currentTaskId": None, "confirmedTaskIds": [],
                     "scannedChapters": 0, "totalChapters": 0, "taskCount": 0,
                     "courses": [{"id": c["id"], "name": c["name"], "status": "queued"} for c in courses],
                     "currentCourse": None, "currentChapter": None, "currentVideo": None,
                     "videoCount": 0, "passedVideos": 0, "alreadyPassedVideos": 0,
                     "unconfirmedVideos": 0, "unsupportedTasks": 0, "localPosition": 0,
                     "selectedChapterIds": [], "selectedChapterCount": 0, "chapterQueue": [],
                     "processedChapters": 0, "completedChapters": 0, "currentChapterId": None,
                     "submittedQuizzes": 0, "passedQuizzes": 0, "alreadyPassedQuizzes": 0, "unconfirmedQuizzes": 0,
                     "bankHits": 0, "deepseekQuestions": 0, "shortAnswersGenerated": 0,
                     "duration": 0, "lastResponsePosition": 0, "serverIsPassed": None,
                     "serverCreditedSeconds": None, "reportsWithValidResponse": 0,
                     "message": "等待处理", "notes": [], "actionRequired": None}

    def update(self, **values):
        with self.lock:
            self.data.update(values)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def add(self, field, amount=1):
        with self.lock:
            self.data[field] += amount

    def note(self, text):
        with self.lock:
            self.data["notes"] = (self.data["notes"] + [text])[-50:]

    def course_status(self, index, status):
        with self.lock:
            self.data["courses"][index]["status"] = status


class Manager:
    def __init__(self, data_dir: Path, *, platform_factory=Platform, runner=run_remote,
                 max_jobs=4, session_ttl=30*86400, clock=time.monotonic, qr_factory=None, wall_clock=time.time,
                 persist_sessions=True, answerer=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.platform_factory, self.runner, self.clock = platform_factory, runner, clock
        self.max_jobs, self.session_ttl = max_jobs, session_ttl
        self.wall_clock = wall_clock
        self.user_records = UserRecords(self.data_dir / 'user-records.sqlite3', int(wall_clock()))
        self.lock = threading.RLock()
        self.sessions, self.active, self.logins = {}, 0, 0
        self.closing = False
        from .answer_bank import AnswerBank
        from .deepseek import DeepSeek
        self.answer_bank = AnswerBank(self.data_dir/'question-bank.sqlite3')
        self.answerer = answerer or DeepSeek()
        self.session_store = None
        if persist_sessions and hasattr(platform_factory, 'restore_login'):
            from .credentials import SessionStore
            self.session_store = SessionStore(self.data_dir)
            self._restore_sessions()
        from .auth import QRLogins, PassportQR
        self.qr = QRLogins(self.login_platform, revoke=self.logout, factory=qr_factory or PassportQR, clock=clock)
        self.stop_reaper = threading.Event()
        self.reaper = threading.Thread(target=self._reap, daemon=True, name="session-reaper")
        self.reaper.start()

    def _save_session(self, token, session):
        if self.session_store is None:
            return
        self.session_store.save(token, {
            'recordId': session.record_id, 'csrf': session.csrf, 'profile': session.profile,
            'loginMethod': session.login_method, 'loginAt': session.login_at,
            'lastActiveAt': session.last_active_at, 'platform': session.platform.export_login(),
            'deepseekKey': session.deepseek_key,
        })

    def _restore_sessions(self):
        for saved in self.session_store.load():
            token, platform = saved['token'], None
            try:
                age = max(0, int(self.wall_clock()) - saved['lastActiveAt'])
                if age >= self.session_ttl:
                    if self.user_records.resume(saved['recordId']):
                        self.user_records.finish(saved['recordId'], 'expired', int(self.wall_clock()), saved['lastActiveAt'])
                    self.session_store.delete(token)
                    continue
                if len(self.sessions) >= 128:
                    self.session_store.delete(token)
                    continue
                platform = self.platform_factory.restore_login(saved['platform'])
                session = Session(platform=platform, csrf=saved['csrf'], touched=self.clock()-age,
                                  record_id=saved['recordId'], profile=identity(saved.get('profile')),
                                  login_method=saved['loginMethod'], login_at=saved['loginAt'],
                                  last_active_at=saved['lastActiveAt'], last_persisted=self.clock())
                from .deepseek import valid_key
                if valid_key(saved.get('deepseekKey', '')):
                    session.deepseek_key = saved.get('deepseekKey', '')
                if not self.user_records.resume(session.record_id):
                    raise ValueError('Saved login no longer active')
                self.sessions[token] = session
            except (ValueError, TypeError, KeyError, RemoteError):
                if platform:
                    platform.close()
                self.session_store.delete(token)

    def _reap(self):
        while not self.stop_reaper.wait(30):
            self.qr.reap()
            with self.lock:
                expired = [k for k, s in self.sessions.items() if self.clock() - s.touched > self.session_ttl]
                for key in expired:
                    self.logout(key, reason='expired')

    def login(self, cookie):
        if not isinstance(cookie, str) or not 1 <= len(cookie.encode("utf-8")) <= 16384:
            raise ServiceError("请提供不超过 16 KB 的 Cookie")
        return self.login_platform(None, cookie=cookie)

    def login_platform(self, platform, *, cookie=None):
        with self.lock:
            if self.closing or len(self.sessions) + self.logins >= 128 or self.logins >= 4:
                if platform:
                    platform.close()
                raise ServiceError("登录会话已满，请稍后重试", 429)
            self.logins += 1
        try:
            if platform is None:
                platform = self.platform_factory(cookie)
            courses = platform.list_courses()
            read_profile = getattr(platform, 'account_profile', None)
            profile = identity(read_profile() if read_profile else {})
            token = secrets.token_urlsafe(32)
            now = int(self.wall_clock())
            session = Session(platform=platform, touched=self.clock(), profile=profile,
                              login_method='cookie' if cookie is not None else 'qr', login_at=now,
                              last_active_at=now, last_persisted=self.clock())
            with self.lock:
                if self.closing:
                    raise ServiceError("服务正在停止", 503)
                self.user_records.start(session.record_id, profile, session.login_method, now)
                try:
                    self._save_session(token, session)
                except Exception:
                    self.user_records.finish(session.record_id, 'interrupted', now, now)
                    raise
                self.sessions[token] = session
            return token, session, courses
        except Exception:
            if platform:
                platform.close()
            raise
        finally:
            with self.lock:
                self.logins -= 1

    def session(self, token):
        with self.lock:
            s = self.sessions.get(token)
            if not s or s.closed:
                raise ServiceError("请先登录", 401)
            if self.clock() - s.touched > self.session_ttl:
                self.logout(token, reason='expired')
                raise ServiceError("会话已过期，请重新登录", 401)
            s.touched = self.clock()
            s.last_active_at = int(self.wall_clock())
            if s.touched - s.last_persisted >= 15:
                self._save_session(token, s)
                self.user_records.touch(s.record_id, s.last_active_at)
                s.last_persisted = s.touched
            return s

    def admin_users(self, *, search='', status='', method='', page=1):
        if (not isinstance(search, str) or len(search) > 100 or status not in STATES | {''}
                or method not in ('', 'cookie', 'qr') or not isinstance(page, int) or not 1 <= page <= 1000000):
            raise ServiceError('用户查询参数无效')
        with self.lock:
            expired = [k for k, s in self.sessions.items() if self.clock() - s.touched > self.session_ttl]
            for token in expired:
                self.logout(token, reason='expired')
            live = {s.record_id: s for s in self.sessions.values() if not s.closed}
            result = self.user_records.list(search=search.strip(), status=status, method=method, page=page)
            active_users = {s.profile['uid'] or s.record_id for s in live.values()}
            result['stats'].update(activeUsers=len(active_users), activeSessions=len(live), runningJobs=self.active)
            for row in result['users']:
                session = live.get(row['id'])
                row['current'] = None
                if session is None:
                    continue
                row['lastActiveAt'] = session.last_active_at
                jobs = list(session.jobs.values())
                latest = jobs[-1].snapshot() if jobs else None
                row['current'] = {'busy': session.busy, 'courseCount': len(session.platform.courses),
                                  'jobCount': len(jobs), 'job': None}
                if latest:
                    keys = ('id', 'kind', 'status', 'currentCourse', 'currentChapter', 'currentVideo', 'selectedChapterCount',
                            'processedChapters', 'passedVideos', 'alreadyPassedVideos', 'submittedQuizzes', 'passedQuizzes',
                            'selectedHomeworkCount', 'processedHomework', 'submittedHomework', 'unconfirmedHomework')
                    row['current']['job'] = {k: latest.get(k) for k in keys}
            result['updatedAt'] = int(self.wall_clock())
            return result

    def _admin_target(self, record_id, *, require_live=True):
        if not isinstance(record_id,str) or not re.fullmatch('[0-9a-f]{32}',record_id):
            raise ServiceError('用户标识无效')
        account = self.user_records.account_records(record_id)
        if account is None:
            raise ServiceError('用户记录不存在或已删除',404)
        found = next(((token,s) for token,s in self.sessions.items() if s.record_id==record_id and not s.closed),None)
        if found and self.clock()-found[1].touched > self.session_ttl:
            self.logout(found[0],reason='expired')
            found = None
        if require_live and found is None:
            raise ServiceError('用户已退出或会话已过期，不能操作任务或 SK',409)
        return account, found[1] if found else None

    def admin_control(self, record_id, *, course_id=None, kind='chapters'):
        if kind not in ('chapters','homework') or (course_id is not None and not isinstance(course_id,str)):
            raise ServiceError('任务查询参数无效')
        with self.lock:
            account,s = self._admin_target(record_id,require_live=False)
            result = {'user':{k:account[k] for k in ('id','uid','displayName')},
                      'deleteRecordCount':len(account['recordIds']), 'active':s is not None,
                      'busy':bool(s and s.busy),'configured':bool(s and s.deepseek_key),
                      'courses':[], 'job':None, 'catalog':None}
            if s is None:
                return result
            result['courses'] = [{'id':c['id'],'name':c['name']} for c in s.platform.courses.values()]
            latest = next(reversed(s.jobs.values()),None)
            if latest:
                state = latest.snapshot()
                keys = ('id','kind','status','selectedChapterIds','selectedHomeworkIds','quizPolicy',
                        'currentCourse','currentChapter','currentVideo','processedChapters','selectedChapterCount',
                        'processedHomework','selectedHomeworkCount')
                result['job'] = {k:state.get(k) for k in keys}
            if course_id is not None:
                if course_id not in s.platform.courses:
                    raise ServiceError('课程不属于该用户',404)
                if kind=='homework':
                    result['catalog'] = self.homework_catalog(s,course_id)
                else:
                    catalog = self.catalog(s,course_id)
                    result['catalog'] = {k:catalog.get(k) for k in ('courseId','ready','chapters','taskProgress')}
            return result

    def admin_user_action(self, record_id, action, body):
        with self.lock:
            account,s = self._admin_target(record_id,require_live=action!='delete')
            if action=='delete':
                ids = account['recordIds']
                # Missing history prevents restoring a saved login even if later cleanup fails.
                self.user_records.delete_records(ids)
                failed = False
                for token,current in list(self.sessions.items()):
                    if current.record_id in ids:
                        try:
                            self.logout(token)
                        except Exception:
                            failed = True
                if self.session_store is not None:
                    try:
                        self.session_store.delete_records(ids)
                    except Exception:
                        failed = True
                try:
                    self.answer_bank.delete_owners(ids)
                except Exception:
                    failed = True
                if failed:
                    raise ServiceError('用户已退出并移出列表，部分本地数据未能清理，请检查存储状态',500)
                return {'deleted':True,'deletedRecords':len(ids)}
            if action=='sk':
                if not isinstance(body.get('key'),str):
                    raise ServiceError('需要提供 key 字符串，空字符串表示清除 SK')
                self.ai_settings(s,body['key'])
                return {'ok':True,'configured':bool(s.deepseek_key)}
            if action=='stop':
                job_id = body.get('jobId')
                if not isinstance(job_id,str):
                    raise ServiceError('需要指定任务标识')
                job = self.job(s,job_id)
                job.cancel.set()
                return {'id':job.id,'stopRequested':True}
            course_id,kind = body.get('courseId'),body.get('kind')
            if kind not in ('chapters','homework') or not isinstance(course_id,str):
                raise ServiceError('请选择课程和任务类型')
            if action=='scan':
                job = self.scan(s,course_id,scope='outline') if kind=='chapters' else self.scan_homework(s,course_id)
            elif action=='start':
                if kind=='chapters':
                    if not isinstance(body.get('chapterIds'),list):
                        raise ServiceError('请明确选择需要启动的章节')
                    job = self.start(s,[course_id],chapter_ids=body.get('chapterIds'),quiz_policy=body.get('quizPolicy','bank_submit'))
                else:
                    job = self.start_homework(s,course_id,body.get('workIds'))
            else:
                raise ServiceError('管理员操作不存在',404)
            return {k:job[k] for k in ('id','kind','status')}

    def refresh_courses(self, s):
        with self.lock:
            if s.busy or s.closed:
                raise ServiceError("任务运行期间不能刷新课程", 409)
            s.busy = True
        try:
            courses = s.platform.list_courses()
            with self.lock:
                s.catalogs.clear()
                s.chapter_sources.clear()
                s.homework_catalogs.clear()
                for token, current in self.sessions.items():
                    if current is s:
                        self._save_session(token, s)
                        break
            return courses
        finally:
            with self.lock:
                s.busy = False
                if s.closed:
                    s.platform.close()

    def session_snapshot(self, s):
        with self.lock:
            return {"authenticated": True, "csrf": s.csrf,
                    "courses": [{"id": c["id"], "name": c["name"]} for c in s.platform.courses.values()],
                    "jobs": [j.snapshot() for j in s.jobs.values()]}

    def catalog(self, s, course_id):
        with self.lock:
            if course_id not in s.platform.courses:
                raise ServiceError("课程不属于当前会话", 404)
            for job in reversed(list(s.jobs.values())):
                with job.lock:
                    if (job.data["kind"] == "catalog" and job.data["status"] not in TERMINAL
                            and job.data["courses"][0]["id"] == course_id and job.catalog_preview is not None):
                        return copy.deepcopy(job.catalog_preview)
            return copy.deepcopy(s.catalogs.get(course_id, {"courseId": course_id, "ready": False, "tasks": [], "notes": []}))

    def homework_settings(self, s, key=None):
        # Compatibility for existing clients; all scenarios use the same user setting.
        return self.ai_settings(s, key)

    def ai_settings(self, s, key=None):
        from .deepseek import MODEL, MODEL_VERSION, valid_key
        with self.lock:
            if s.closed:
                raise ServiceError('登录已失效', 401)
            if key is not None:
                if s.busy:
                    raise ServiceError('任务运行时不能更改 DeepSeek SK', 409)
                if not valid_key(key):
                    raise ServiceError('DeepSeek SK 格式无效')
                previous = s.deepseek_key
                s.deepseek_key = key
                try:
                    for token, session in self.sessions.items():
                        if session is s:
                            self._save_session(token, s)
                            break
                except Exception:
                    s.deepseek_key = previous
                    raise
            return {'configured': bool(s.deepseek_key), 'model': MODEL, 'modelVersion': MODEL_VERSION,
                    'reasoningEffort': 'low', 'bankQuestions': self.answer_bank.count(s.record_id)}

    def import_bank(self, s, questions):
        with self.lock:
            if s.busy:
                raise ServiceError('任务运行时不能导入题库', 409)
            try:
                count = self.answer_bank.import_questions(s.record_id, questions)
            except (ValueError, KeyError, TypeError):
                raise ServiceError('题库 JSON 格式或答案选项无效，请按示例格式导入') from None
            return {'imported': count, 'bankQuestions': self.answer_bank.count(s.record_id)}

    def homework_catalog(self, s, course_id):
        with self.lock:
            if course_id not in s.platform.courses:
                raise ServiceError('课程不属于当前会话', 404)
            from .homework import public_list
            catalog = s.homework_catalogs.get(course_id)
            return {'courseId': course_id, 'ready': catalog is not None,
                    'homework': public_list(catalog) if catalog is not None else []}

    def scan_homework(self, s, course_id):
        with self.lock:
            if course_id not in s.platform.courses:
                raise ServiceError('课程不属于当前会话', 404)
            course = copy.deepcopy(s.platform.courses[course_id])
            return self._launch(s, Job([course], kind='homework_catalog'), self._scan_homework, course)

    def _scan_homework(self, s, job, course):
        from .homework import fetch_list
        from .deepseek import AnswerError
        job.update(status='running', currentCourse=course['name'], message='正在读取作业标题，未打开作业详情')
        try:
            rows = fetch_list(s.platform, course, job.cancel)
            if job.cancel.is_set():
                job.update(status='stopped', message='作业列表读取已停止')
            else:
                with self.lock:
                    s.homework_catalogs[course['id']] = rows
                job.update(status='completed', message='作业标题已就绪', homeworkCount=len(rows))
        except PlatformVerificationRequired as exc:
            job.update(status='failed', message=str(exc), actionRequired='platform_verification')
        except (RemoteError, AnswerError) as exc:
            job.update(status='failed', message=str(exc))
        except Exception:
            job.update(status='failed', message='作业标题读取失败，请刷新作业列表')
        finally:
            self._finish(s, job)

    def start_homework(self, s, course_id, work_ids):
        with self.lock:
            if (not isinstance(work_ids, list) or not 1 <= len(work_ids) <= 100
                    or any(not isinstance(i, str) for i in work_ids) or len(set(work_ids)) != len(work_ids)):
                raise ServiceError('请选择 1 至 100 份不同作业')
            if course_id not in s.platform.courses:
                raise ServiceError('课程不属于当前会话', 404)
            rows = s.homework_catalogs.get(course_id)
            if rows is None or any(i not in {r['id'] for r in rows if r['selectable']} for i in work_ids):
                raise ServiceError('作业列表已失效、不可提交或不属于当前课程，请刷新作业列表', 409)
            selected = [r for r in rows if r['id'] in work_ids]
            course = copy.deepcopy(s.platform.courses[course_id])
            job = Job([course], kind='homework')
            job.update(selectedHomeworkIds=work_ids[:], selectedHomeworkCount=len(selected),
                       processedHomework=0, submittedHomework=0, confirmedHomework=0,
                       alreadySubmittedHomework=0, failedHomework=0, unconfirmedHomework=0,
                       bankHits=0, deepseekQuestions=0, shortAnswersGenerated=0,
                       homeworkQueue=[{'id': r['id'], 'name': r['name'], 'status': 'queued'} for r in selected])
            return self._launch(s, job, self._run_homework, course, copy.deepcopy(selected))

    def _run_homework(self, s, job, course, selected):
        from .homework import fetch_list, process
        from .deepseek import AnswerError, ProviderError
        job.update(status='running', currentCourse=course['name'], message='核对所选作业列表')
        job.course_status(0, 'running')
        def queue_state(identity, status):
            with job.lock:
                for row in job.data['homeworkQueue']:
                    if row['id'] == identity:
                        row['status'] = status
        try:
            fresh = {r['id']: r for r in fetch_list(s.platform, course, job.cancel)}
            if not job.cancel.is_set():
                with self.lock:
                    s.homework_catalogs[course['id']] = list(fresh.values())
            if any(r['id'] not in fresh for r in selected) and not job.cancel.is_set():
                raise AnswerError('所选作业已变化，请刷新列表后重新选择；未打开作业详情')
            for chosen in selected:
                if job.cancel.is_set():
                    break
                work = fresh[chosen['id']]
                job.update(currentChapter='课程作业', currentVideo=work['name'], currentTaskId=work['id'])
                queue_state(work['id'], 'running')
                if work['completed']:
                    job.add('alreadySubmittedHomework')
                    queue_state(work['id'], 'completed')
                    job.add('processedHomework')
                    continue
                if not work['selectable']:
                    job.add('failedHomework')
                    job.note(work['name']+'：作业当前不可提交')
                    queue_state(work['id'], 'failed')
                    job.add('processedHomework')
                    continue
                before = job.snapshot()
                def progress(hits, generated, short, message):
                    job.update(bankHits=before['bankHits']+hits, deepseekQuestions=before['deepseekQuestions']+generated,
                               shortAnswersGenerated=before['shortAnswersGenerated']+short, message=message)
                try:
                    job.update(message='读取当前作业详情，准备从题库与 DeepSeek 获取答案')
                    outcome = process(s.platform, course, work, self.answer_bank, s.record_id, self.answerer,
                                      s.deepseek_key, self.data_dir/'homework-attempts', job.cancel, progress)
                    if outcome.get('stopped') or job.cancel.is_set():
                        queue_state(work['id'], 'stopped')
                        break
                    job.add('submittedHomework')
                    try:
                        fresh = {r['id']: r for r in fetch_list(s.platform, course, job.cancel)}
                        if not job.cancel.is_set():
                            with self.lock:
                                s.homework_catalogs[course['id']] = list(fresh.values())
                        confirmed = bool(fresh.get(work['id'], {}).get('completed'))
                    except PlatformVerificationRequired:
                        raise
                    except (RemoteError, AnswerError):
                        confirmed = False
                    if confirmed:
                        job.add('confirmedHomework')
                        queue_state(work['id'], 'completed')
                    else:
                        job.add('unconfirmedHomework')
                        job.note(work['name']+'：提交已被接受，列表尚未确认完成；不会重复提交')
                        queue_state(work['id'], 'partial')
                except PlatformVerificationRequired:
                    queue_state(work['id'], 'failed')
                    raise
                except ProviderError as exc:
                    job.add('failedHomework')
                    job.add('processedHomework')
                    job.note(work['name']+'：'+str(exc))
                    queue_state(work['id'], 'failed')
                    raise
                except (AnswerError, RemoteError) as exc:
                    if job.cancel.is_set():
                        queue_state(work['id'], 'stopped')
                        break
                    job.add('failedHomework')
                    job.note(work['name']+'：'+str(exc))
                    queue_state(work['id'], 'failed')
                job.add('processedHomework')
            state = job.snapshot()
            if job.cancel.is_set():
                job.update(status='stopped', message='作业任务已停止，尚未到达的作业没有打开详情')
            else:
                complete = state['failedHomework'] == 0 and state['unconfirmedHomework'] == 0
                job.update(status='completed' if complete else 'partial', message='所选作业处理结束')
        except PlatformVerificationRequired as exc:
            job.update(status='failed', message=str(exc), actionRequired='platform_verification')
        except (AnswerError, RemoteError) as exc:
            job.update(status='failed', message=str(exc))
        except Exception:
            job.update(status='failed', message='作业处理失败，请检查题型或在原网页核对；不会自动重交')
        finally:
            state = job.snapshot()
            for row in state['homeworkQueue']:
                if row['status'] == 'running':
                    queue_state(row['id'], state['status'])
            job.course_status(0, state['status'])
            self._finish(s, job)

    def _launch(self, s, job, target, *args):
        # Caller holds the manager lock; both scans and playback consume a worker slot.
        if s.closed or self.closing:
            raise ServiceError("会话或服务已关闭", 409)
        if s.busy:
            raise ServiceError("当前会话已有运行中的任务", 409)
        if self.active >= self.max_jobs:
            raise ServiceError("运行任务已满，请稍后重试", 429)
        while len(s.jobs) >= 20:
            del s.jobs[next(iter(s.jobs))]
        s.jobs[job.id] = job
        s.busy, self.active = True, self.active + 1
        job.thread = threading.Thread(target=target, args=(s, job, *args), daemon=True, name="learning-" + job.id[:8])
        try:
            job.thread.start()
        except Exception:
            del s.jobs[job.id]
            s.busy, self.active = False, self.active - 1
            raise
        return job.snapshot()

    def scan(self, s, course_id, *, scope="all", chapter_ids=None):
        with self.lock:
            if course_id not in s.platform.courses:
                raise ServiceError("课程不属于当前会话", 404)
            if scope not in ("all", "initial", "chapters", "outline"):
                raise ServiceError("不支持的章节读取范围")
            sources, previous = None, None
            if scope == "chapters":
                sources = copy.deepcopy(s.chapter_sources.get(course_id))
                previous = copy.deepcopy(s.catalogs.get(course_id))
                if (not isinstance(chapter_ids, list) or not 1 <= len(chapter_ids) <= 4 or
                        any(not isinstance(i, str) for i in chapter_ids) or len(set(chapter_ids)) != len(chapter_ids)):
                    raise ServiceError("请选择 1 至 4 个不同章节")
                if not sources or not previous or any(i not in {c["id"] for c in sources} for i in chapter_ids):
                    raise ServiceError("章节列表已失效或不属于当前课程，请重新读取目录", 409)
            elif chapter_ids is not None:
                raise ServiceError("chapterIds 仅用于按章节读取")
            course = copy.deepcopy(s.platform.courses[course_id])
            job = Job([course], kind="catalog")
            job.update(catalogScope=scope, selectedChapterIds=list(chapter_ids or []))
            return self._launch(s, job, self._scan, course, scope, chapter_ids, sources, previous)

    def _scan(self, s, job, course, scope="all", chapter_ids=None, sources=None, previous=None):
        job.update(status="running", currentCourse=course["name"], message="读取章节任务列表")
        job.course_status(0, "running")
        rows, seen = [], set()
        incomplete = False
        try:
            chapters = sources if sources is not None else s.platform.chapters(course)
            if scope == "outline":
                if job.cancel.is_set() or s.closed:
                    job.update(status="stopped", message="目录读取已停止")
                    return
                with self.lock:
                    if s.closed:
                        job.update(status="stopped", message="会话已退出")
                        return
                    if course["id"] not in s.catalogs and len(s.catalogs) >= 10:
                        expired = next(iter(s.catalogs))
                        del s.catalogs[expired]
                        s.chapter_sources.pop(expired, None)
                    s.chapter_sources[course["id"]] = copy.deepcopy(chapters)
                    s.catalogs[course["id"]] = {"courseId": course["id"], "scanId": job.id, "revision": 0,
                        "ready": True, "loading": False, "tasks": [], "notes": [], "incomplete": False,
                        "chapters": [public_chapter(ch) for ch in chapters],
                        'taskProgress': public_directory_progress(s.platform, course['id'])}
                job.update(status="completed", totalChapters=len(chapters), scannedChapters=len(chapters),
                           message="章节目录已就绪，请勾选要学习的章节")
                return
            selected_indexes = [i for i, ch in enumerate(chapters) if scope != "chapters" or ch["id"] in chapter_ids]
            if scope == "initial":
                selected_indexes = selected_indexes[:1]
            job.update(totalChapters=len(selected_indexes))
            grouped = {}
            chapter_rows = [public_chapter(ch) for ch in chapters]
            if previous is not None:
                chapter_rows = copy.deepcopy(previous["chapters"])
                by_id = {ch["id"]: i for i, ch in enumerate(chapters)}
                for row in previous["tasks"]:
                    if row["chapterId"] not in chapter_ids:
                        grouped.setdefault(by_id[row["chapterId"]], []).append(row)
                for i in selected_indexes:
                    chapter_rows[i]["status"] = "pending"
                for note in previous.get("notes", []):
                    job.note(note)

            def publish_preview():
                nonlocal rows, seen
                rows, seen = [], set()
                for index in sorted(grouped):
                    for row in grouped[index]:
                        if row["id"] not in seen:
                            seen.add(row["id"])
                            rows.append(row)
                if len(rows) > 5000:
                    raise RemoteError("单课程任务数超过 5000 项限制")
                with job.lock:
                    job.data["taskCount"] = len(rows)
                    job.catalog_preview = {"courseId": course["id"], "scanId": job.id,
                        "revision": job.data["scannedChapters"], "ready": False, "loading": True,
                        "tasks": rows, "chapters": copy.deepcopy(chapter_rows), "notes": list(job.data["notes"]),
                        'taskProgress': public_directory_progress(s.platform, course['id']),
                        "loadingChapterIds": [chapters[i]["id"] for i in selected_indexes]}

            publish_preview()
            prepare = getattr(s.platform, "prepare_catalog", None)
            if prepare and scope != "chapters" and not job.cancel.is_set():
                prepare(course, [chapters[i] for i in selected_indexes])

            def read_chapter(chapter):
                found = []
                if job.cancel.is_set():
                    return found, None
                try:
                    reader = getattr(s.platform, "catalog_cards", None)
                    cards = reader(course, chapter, cancel_event=job.cancel) if reader else s.platform.chapter_cards(course, chapter)
                    for setting, study in cards:
                        if job.cancel.is_set():
                            break
                        for index, attachment in enumerate(setting.get("attachments", [])):
                            found.append(describe_task(course, chapter, setting, study, attachment, index))
                            if len(found) > 5000:
                                raise RemoteError("单课程任务数超过 5000 项限制")
                    return found, None
                except ChapterUnavailable as exc:
                    return found, str(exc)

            # Keep at most four outstanding chapters, rather than queue the whole course.
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="catalog") as pool:
                pending, next_index = {}, 0
                try:
                    while pending or next_index < len(selected_indexes):
                        while not job.cancel.is_set() and len(pending) < 4 and next_index < len(selected_indexes):
                            i = selected_indexes[next_index]
                            pending[pool.submit(read_chapter, chapters[i])] = i
                            next_index += 1
                        if job.cancel.is_set():
                            break
                        done, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                        for future in sorted(done, key=lambda f: pending[f]):
                            i = pending.pop(future)
                            found, error = future.result()
                            grouped[i] = found
                            chapter_rows[i]["status"] = "unavailable" if error else "loaded"
                            if error:
                                incomplete = True
                                job.note(chapters[i]["name"] + "：" + error)
                            job.update(currentChapter=chapters[i]["name"])
                            job.add("scannedChapters")
                        if done:
                            publish_preview()
                except Exception:
                    job.cancel.set()
                    raise
                finally:
                    for future in pending:
                        future.cancel()
            if job.cancel.is_set():
                job.update(status="stopped", message="任务读取已停止，原有任务列表未替换")
            else:
                with self.lock:
                    if s.closed:
                        job.update(status="stopped", message="会话已退出")
                    else:
                        if course["id"] not in s.catalogs and len(s.catalogs) >= 10:
                            expired = next(iter(s.catalogs))
                            del s.catalogs[expired]
                            s.chapter_sources.pop(expired, None)
                        incomplete = any(ch["status"] == "unavailable" for ch in chapter_rows)
                        s.catalogs[course["id"]] = {"courseId": course["id"], "scanId": job.id, "ready": True,
                            "loading": False, "revision": len(selected_indexes), "chapters": chapter_rows,
                            'taskProgress': public_directory_progress(s.platform, course['id']),
                            "tasks": rows, "incomplete": incomplete, "notes": job.snapshot()["notes"]}
                        s.chapter_sources[course["id"]] = copy.deepcopy(chapters)
                        job.update(status="partial" if incomplete else "completed",
                                   message=("章节目录和首章已读取；点击其他章节读取任务" if scope == "initial" else "任务列表已就绪，请选择要运行的视频")
                                           + ("；部分章节需要人工处理" if incomplete else ""))
        except RemoteError as exc:
            job.update(status="failed", message=str(exc),
                       actionRequired="platform_verification" if isinstance(exc, PlatformVerificationRequired) else None)
        except Exception:
            job.update(status="failed", message="读取任务失败，请检查课程格式或重新登录")
        finally:
            with job.lock:
                job.catalog_preview = None
            job.course_status(0, job.snapshot()["status"])
            self._finish(s, job)

    def start(self, s, ids, task_ids=None, *, chapter_ids=None, quiz_policy="skip"):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 30 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise ServiceError("请选择 1 至 30 门不同课程")
        with self.lock:
            if s.closed or self.closing:
                raise ServiceError("会话或服务已关闭", 409)
            if s.busy:
                raise ServiceError("当前会话已有运行中的任务", 409)
            if self.active >= self.max_jobs:
                raise ServiceError("运行任务已满，请稍后重试", 429)
            if any(i not in s.platform.courses for i in ids):
                raise ServiceError("课程不属于当前会话，请刷新课程", 400)
            if chapter_ids is not None:
                if task_ids is not None or len(ids) != 1:
                    raise ServiceError("按章节学习需要选择一门课程，不能同时传入 taskIds")
                if (not isinstance(chapter_ids, list) or not 1 <= len(chapter_ids) <= 1000 or
                        any(not isinstance(i, str) for i in chapter_ids) or len(set(chapter_ids)) != len(chapter_ids)):
                    raise ServiceError("请选择 1 至 1000 个不同章节")
                if quiz_policy not in ("skip", "random_submit", "bank_submit"):
                    raise ServiceError("不支持的章节测验策略")
                available = s.chapter_sources.get(ids[0], [])
                if any(i not in {ch["id"] for ch in available} for i in chapter_ids):
                    raise ServiceError("章节不属于当前课程或目录已失效，请重新读取目录", 409)
                wanted = set(chapter_ids)
                queue = [{"id": ch["id"], "name": ch["name"], "status": "queued"} for ch in available if ch["id"] in wanted]
                course = copy.deepcopy(s.platform.courses[ids[0]])
                job = Job([course], kind="learning")
                job.update(selectedChapterIds=[ch["id"] for ch in queue], selectedChapterCount=len(queue),
                           chapterQueue=queue, totalChapters=len(queue), quizPolicy=quiz_policy)
                return self._launch(s, job, self._run_chapters, course, queue, quiz_policy)
            selection = None
            if task_ids is not None:
                if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 200 or
                        any(not isinstance(i, str) for i in task_ids) or len(set(task_ids)) != len(task_ids)):
                    raise ServiceError("请选择 1 至 200 个不同的视频任务")
                available = {t["id"]: t for course_id in ids for t in s.catalogs.get(course_id, {}).get("tasks", [])}
                if any(i not in available for i in task_ids):
                    raise ServiceError("任务不属于所选课程或列表已失效，请重新读取任务", 409)
                if any(not available[i]["selectable"] for i in task_ids):
                    raise ServiceError("所选任务已完成或需要正常播放器处理，请重新选择", 409)
                selection = {i: copy.deepcopy(available[i]) for i in task_ids}
                if set(ids) != {t["courseId"] for t in selection.values()}:
                    raise ServiceError("每门所选课程至少需要选择一个视频任务")
            courses = [copy.deepcopy(s.platform.courses[i]) for i in ids]
            job = Job(courses, task_ids=task_ids)
            return self._launch(s, job, self._run, courses, selection)

    def job(self, s, job_id):
        with self.lock:
            job = s.jobs.get(job_id)
            if not job:
                raise ServiceError("任务不存在", 404)
            return job

    def _run(self, s, job, courses, selection=None):
        folder = self.data_dir / job.id
        job.update(status="running", message="读取课程章节")
        try:
            seen = set()
            for index, course in enumerate(courses):
                if job.cancel.is_set():
                    break
                job.course_status(index, "running")
                job.update(currentCourse=course["name"])
                before = job.snapshot()
                chapters = s.platform.chapters(course)
                chosen = {k: t for k, t in selection.items() if t["courseId"] == course["id"]} if selection is not None else None
                wanted_chapters = {t["chapterId"] for t in chosen.values()} if chosen is not None else None
                matched = set()
                for chapter in chapters:
                    if job.cancel.is_set():
                        break
                    if wanted_chapters is not None and chapter["id"] not in wanted_chapters:
                        continue
                    job.update(currentChapter=chapter["name"], message="读取章节视频")
                    for setting, study in s.platform.chapter_cards(course, chapter):
                        if job.cancel.is_set():
                            break
                        for att_index, att in enumerate(setting.get("attachments", [])):
                            if job.cancel.is_set():
                                break
                            identity = task_id(course["id"], chapter["id"], study, att, att_index)
                            if chosen is not None:
                                if identity not in chosen:
                                    continue
                                matched.add(identity)
                            if att.get("type") != "video":
                                if att.get("job") is True and att.get("isPassed") is not True:
                                    job.add("unsupportedTasks")
                                continue
                            # An object can be used by different tasks: deduplicate by task identity.
                            key = (course["id"], chapter["id"], study["cards_url"], att.get("objectId"),
                                   att.get("jobid") or att.get("property", {}).get("jobid"))
                            if key in seen:
                                continue
                            seen.add(key)
                            job.add("videoCount")
                            if att.get("isPassed") is True:
                                job.add("alreadyPassedVideos")
                                self._confirmed(s, job, course["id"], identity)
                                continue
                            context = s.platform.context(setting, study, att)
                            title = att.get("property", {}).get("name") or att.get("property", {}).get("title") or "课程视频"
                            job.update(currentTaskId=identity, currentVideo=str(title)[:200], duration=context.duration, localPosition=0,
                                       lastResponsePosition=0, serverIsPassed=None, reportsWithValidResponse=0,
                                       message="按实际时间运行视频事件与进度请求")

                            def progress(state):
                                job.update(localPosition=state.get("localPosition", state["lastResponsePosition"]),
                                           lastResponsePosition=state["lastResponsePosition"],
                                           serverIsPassed=state["serverIsPassed"],
                                           reportsWithValidResponse=state["reportsWithValidResponse"])

                            result = self.runner(context, s.platform.transport, folder / context.fingerprint,
                                                 quiet=True, stop_event=job.cancel, on_update=progress)
                            if result.get("serverIsPassed") is True:
                                job.add("passedVideos")
                                self._confirmed(s, job, course["id"], identity)
                            elif not result.get("stopped") and not job.cancel.is_set():
                                job.add("unconfirmedVideos")
                                job.note(chapter["name"] + "：视频流程结束，服务器尚未确认完成")
                            if result.get("stopped") or job.cancel.is_set():
                                break
                if not job.cancel.is_set() and chosen is not None and matched != set(chosen):
                    raise RemoteError("所选视频任务已发生变化或不可访问，请重新读取任务列表")
                now = job.snapshot()
                if job.cancel.is_set():
                    job.course_status(index, "stopped")
                    break
                partial = (now["unconfirmedVideos"] > before["unconfirmedVideos"] or
                           now["unsupportedTasks"] > before["unsupportedTasks"] or now["videoCount"] == before["videoCount"])
                job.course_status(index, "partial" if partial else "completed")
            state = job.snapshot()
            if job.cancel.is_set():
                job.update(status="stopped", message="任务已停止，已保存最后一次有效响应")
            elif any(c["status"] != "completed" for c in state["courses"]):
                job.update(status="partial", message="视频流程已结束；部分内容需要在正常课程页面处理")
            else:
                job.update(status="completed", message="所选视频任务均获服务器完成确认" if selection is not None
                           else "所选课程中已识别的视频均获服务器完成确认")
        except RemoteError as exc:
            job.update(status="failed", message=str(exc),
                       actionRequired="platform_verification" if isinstance(exc, PlatformVerificationRequired) else None)
        except Exception:
            # Never send raw upstream responses, URLs or arbitrary exception strings to browsers/logs.
            job.update(status="failed", message="任务处理失败，请检查课程格式或联系部署管理员")
        finally:
            with job.lock:
                for c in job.data["courses"]:
                    if c["status"] == "running":
                        c["status"] = job.data["status"]
            self._finish(s, job)

    def _queue_status(self, job, chapter_id, status):
        with job.lock:
            for chapter in job.data["chapterQueue"]:
                if chapter["id"] == chapter_id:
                    chapter["status"] = status
                    break

    def _chapter_video(self, s, job, course, chapter, setting, study, att, identity, folder):
        job.add("videoCount")
        if att.get("isPassed") is True:
            job.add("alreadyPassedVideos")
            self._confirmed(s, job, course["id"], identity)
            return
        context = s.platform.context(setting, study, att)
        title = att.get("property", {}).get("name") or att.get("property", {}).get("title") or "课程视频"
        job.update(currentTaskId=identity, currentVideo=str(title)[:200], duration=context.duration, localPosition=0,
                   lastResponsePosition=0, serverIsPassed=None, reportsWithValidResponse=0,
                   message="运行当前章节的视频任务")

        def progress(state):
            job.update(localPosition=state.get("localPosition", state["lastResponsePosition"]),
                       lastResponsePosition=state["lastResponsePosition"], serverIsPassed=state["serverIsPassed"],
                       reportsWithValidResponse=state["reportsWithValidResponse"])

        result = self.runner(context, s.platform.transport, folder / context.fingerprint,
                             quiet=True, stop_event=job.cancel, on_update=progress)
        if result.get("serverIsPassed") is True:
            job.add("passedVideos")
            self._confirmed(s, job, course["id"], identity)
        elif not result.get("stopped") and not job.cancel.is_set():
            job.add("unconfirmedVideos")
            job.note(chapter["name"] + "：服务器尚未确认视频完成")

    def _run_chapters(self, s, job, course, queue, quiz_policy):
        from .deepseek import AnswerError, ProviderError
        from .homework import resolve_answers

        def answer_resolver(form):
            counts = [0, 0, 0]
            def progress(hits, generated, short, message):
                for index, (field, value) in enumerate(zip(
                        ('bankHits', 'deepseekQuestions', 'shortAnswersGenerated'), (hits, generated, short))):
                    job.add(field, value-counts[index])
                    counts[index] = value
                job.update(message='章节测验：'+message)
            try:
                return resolve_answers(form, self.answer_bank, s.record_id, self.answerer,
                                       s.deepseek_key, job.cancel, progress)
            except ProviderError:
                raise
            except AnswerError as exc:
                raise ChapterUnavailable(str(exc).replace('作业', '章节测验')) from None

        job.update(status="running", currentCourse=course["name"], message="核对所选章节")
        job.course_status(0, "running")
        try:
            if job.cancel.is_set():
                job.update(status="stopped", message="学习已停止")
                return
            fresh = s.platform.chapters(course)
            by_id = {ch["id"]: ch for ch in fresh}
            if any(ch["id"] not in by_id for ch in queue):
                raise RemoteError("所选章节已变化或不可访问，请重新读取课程目录")
            seen = set()
            for chosen in queue:
                if job.cancel.is_set():
                    break
                chapter = by_id[chosen["id"]]
                self._queue_status(job, chapter["id"], "running")
                job.update(currentChapterId=chapter["id"], currentChapter=chapter["name"], currentVideo=None,
                           duration=0, localPosition=0, serverIsPassed=None, reportsWithValidResponse=0,
                           lastResponsePosition=0, message="到达章节，正在读取本章任务")
                before = job.snapshot()
                try:
                    reader = getattr(s.platform, "learning_cards", None)
                    cards = reader(course, chapter, cancel_event=job.cancel) if reader else s.platform.chapter_cards(course, chapter)
                    for setting, study in cards:
                        if job.cancel.is_set():
                            break
                        for i, att in enumerate(setting.get("attachments", [])):
                            if job.cancel.is_set():
                                break
                            identity = task_id(course["id"], chapter["id"], study, att, i)
                            if identity in seen:
                                continue
                            seen.add(identity)
                            job.add("taskCount")
                            kind = att.get("type")
                            if kind == "video":
                                self._chapter_video(s, job, course, chapter, setting, study, att, identity,
                                                    self.data_dir / job.id)
                            elif kind in ("workid", "work"):
                                if att.get("isPassed") is True:
                                    job.add("alreadyPassedQuizzes")
                                    continue
                                if att.get("job") is not True:
                                    continue
                                if quiz_policy == "skip":
                                    job.add("unsupportedTasks")
                                    job.note(chapter["name"] + "：章节测验已按设置跳过")
                                    continue
                                job.update(currentTaskId=identity, currentVideo="章节测验", duration=0, localPosition=0,
                                           serverIsPassed=None, reportsWithValidResponse=0, lastResponsePosition=0,
                                           message="章节测验：从题库匹配答案，未命中时使用 DeepSeek" if quiz_policy == "bank_submit"
                                           else "处理章节测验；无已有作答时随机选择并正式提交")
                                method = getattr(s.platform, "run_quiz", None)
                                if method is None:
                                    raise ChapterUnavailable("当前适配器未实现章节测验")
                                try:
                                    options = {'answer_resolver': answer_resolver} if quiz_policy == 'bank_submit' else {}
                                    result = method(setting, study, att, self.data_dir / "quiz-attempts",
                                                    cancel_event=job.cancel, **options)
                                except ChapterUnavailable as exc:
                                    job.add("unsupportedTasks")
                                    job.note(chapter["name"] + "：" + str(exc))
                                    continue
                                if result.get("submitted"):
                                    job.add("submittedQuizzes")
                                if result.get("actionRequired") == "platform_verification":
                                    raise PlatformVerificationRequired()
                                if result.get("isPassed") is True:
                                    job.add("passedQuizzes")
                                    self._confirmed(s, job, course["id"], identity)
                                elif not job.cancel.is_set():
                                    job.add("unconfirmedQuizzes")
                                    job.note(chapter["name"] + "：测验提交与任务通过分别核对，尚未确认通过")
                            elif att.get("job") is True and att.get("isPassed") is not True:
                                job.add("unsupportedTasks")
                                job.note(chapter["name"] + "：包含文档或其他需在原网页处理的任务")
                except ChapterUnavailable as exc:
                    job.add("unsupportedTasks")
                    job.note(chapter["name"] + "：" + str(exc))
                if job.cancel.is_set():
                    self._queue_status(job, chapter["id"], "stopped")
                    break
                now = job.snapshot()
                partial = any(now[key] > before[key] for key in ("unsupportedTasks", "unconfirmedVideos", "unconfirmedQuizzes"))
                self._queue_status(job, chapter["id"], "partial" if partial else "completed")
                job.add("processedChapters")
                if not partial:
                    job.add("completedChapters")
            if job.cancel.is_set():
                job.update(status="stopped", message="学习已停止，尚未到达的章节没有读取任务")
            else:
                complete = job.snapshot()["completedChapters"] == len(queue)
                job.update(status="completed" if complete else "partial",
                           message="所选章节处理完成" if complete else "所选章节处理结束，部分任务仍需处理或等待服务器确认")
        except ProviderError as exc:
            job.update(status="failed", message=str(exc), actionRequired="deepseek_configuration")
        except RemoteError as exc:
            job.update(status="failed", message=str(exc),
                       actionRequired="platform_verification" if isinstance(exc, PlatformVerificationRequired) else None)
        except Exception:
            job.update(status="failed", message="章节处理失败，请检查任务格式或重新读取目录")
        finally:
            state = job.snapshot()
            for chapter in state["chapterQueue"]:
                if chapter["status"] == "running":
                    self._queue_status(job, chapter["id"], state["status"])
            job.course_status(0, state["status"])
            self._finish(s, job)

    def _confirmed(self, s, job, course_id, identity):
        with self.lock:
            for task in s.catalogs.get(course_id, {}).get("tasks", []):
                if task["id"] == identity:
                    task.update(isPassed=True, selectable=False, reason="服务器已标记完成")
                    break
        with job.lock:
            if identity not in job.data["confirmedTaskIds"]:
                job.data["confirmedTaskIds"].append(identity)

    def _finish(self, s, job):
        job.update(finishedAt=int(time.time()))
        try:
            atomic_json(self.data_dir / job.id / "summary.json", job.snapshot())
        finally:
            with self.lock:
                s.busy = False
                self.active -= 1
                if s.closed:
                    s.platform.close()

    def logout(self, token, *, reason='logged_out', remember=False):
        with self.lock:
            s = self.sessions.pop(token, None)
            if not s:
                return
            try:
                if self.session_store is not None:
                    if remember:
                        self._save_session(token, s)
                    else:
                        self.session_store.delete(token)
                self.user_records.finish(s.record_id, reason, int(self.wall_clock()), s.last_active_at)
            finally:
                # Persistence failures must never leave credentials usable after logout.
                s.closed = True
                s.deepseek_key = ''
                for job in s.jobs.values():
                    job.cancel.set()
                if not s.busy:
                    s.platform.close()

    def close(self):
        self.stop_reaper.set()
        with self.lock:
            self.closing = True
        self.qr.close()
        with self.lock:
            threads = [j.thread for s in self.sessions.values() for j in s.jobs.values() if j.thread and j.thread.is_alive()]
            for token in list(self.sessions):
                self.logout(token, reason='interrupted', remember=True)
        deadline = time.monotonic() + 45
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
