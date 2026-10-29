"""Read course pages and resolve fresh video contexts; never evaluate page JS."""
from __future__ import annotations

import copy
import hashlib
import http.cookiejar
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from playback_simulator.chaoxing_client import (
    CourseContext, RemoteError, Transport, UA, authorized_url, card_setting,
    cookie_jar_from_text, validate_setting,
)

BASE = "https://mooc2-ans.chaoxing.com"
MOOC = "https://mooc1.chaoxing.com"


class ChapterUnavailable(RemoteError):
    """A chapter requiring an unsupported interactive flow, visible in the catalog."""


class PlatformVerificationRequired(RemoteError):
    def __init__(self):
        super().__init__("平台要求访问验证。请先在超星原网页完成验证，再重新扫码或使用更新后的 Cookie 登录；这不是课程主页格式变化。")


def verification_url(url):
    return urllib.parse.urlsplit(url).path.lower().startswith("/antispider")


class RequestPacer:
    """One read-request budget shared by all course workers in this session."""
    def __init__(self, interval=.5, clock=time.monotonic):
        self.interval, self.clock, self.next_allowed = interval, clock, 0
        self.lock = threading.Lock()

    def acquire(self, stopped):
        with self.lock:
            delay = max(0, self.next_allowed - self.clock())
            if stopped.wait(delay):
                return False
            self.next_allowed = self.clock() + self.interval
            return True


def task_id(course_id, chapter_id, study, attachment, index):
    card = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(study["cards_url"]).query)).get("num", "0")
    prop = attachment.get("property", {})
    identity = [course_id, chapter_id, card, attachment.get("type"),
                attachment.get("objectId") or prop.get("objectid"),
                attachment.get("jobid") or prop.get("jobid")]
    if not identity[-1] and not identity[-2]:
        identity.append(index)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


def describe_task(course, chapter, setting, study, attachment, index):
    prop = attachment.get("property", {})
    kind = attachment.get("type", "unknown")
    passed = attachment.get("isPassed") is True
    reason = ""
    if kind in ("workid", "work"):
        reason = "章节测验，可在按章节学习时按所选策略处理"
    elif kind != "video":
        reason = "文档或其他类型任务，请在课程页面完成"
    elif passed:
        reason = "服务器已标记完成"
    else:
        try:
            validate_setting(setting)
            if attachment.get("randomFaceCaptureTimeList"):
                reason = "此视频需要互动验证，请使用正常播放器"
        except RemoteError:
            reason = "此视频需要正常播放器中的验证或特殊流程"
    duration = attachment.get("attDuration") or prop.get("duration")
    try:
        seconds = float(duration)
        duration = int(seconds) if 0 < seconds < 86400 * 7 else None
    except (TypeError, ValueError, OverflowError):
        duration = None
    return {"id": task_id(course["id"], chapter["id"], study, attachment, index),
            "courseId": course["id"], "chapterId": chapter["id"], "chapterName": chapter["name"],
            "name": str(prop.get("name") or prop.get("title") or attachment.get("title") or
                        ("视频任务" if kind == "video" else "课程任务"))[:200],
            "type": kind, "isPassed": passed, "selectable": not bool(reason),
            "reason": reason, "duration": duration}


class Node:
    def __init__(self, tag="root", attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return " ".join("".join(c.text() if isinstance(c, Node) else c for c in self.children).split())


class Document(HTMLParser):
    VOID = {"input", "img", "br", "hr", "meta", "link", "source", "area", "base", "embed", "wbr", "param", "col", "track"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, text):
        self.current.children.append(text)

    def inputs(self):
        return {n.attrs.get("id") or n.attrs.get("name"): n.attrs.get("value", "")
                for n in self.root.walk() if n.tag == "input"}


def add_query(url, values):
    p = urllib.parse.urlsplit(authorized_url(url))
    query = dict(urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    query.update(values)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(query), ""))


def scoped_link(base, link):
    url = urllib.parse.urljoin(base, link)
    # Observed legacy redirects use HTTP for the same platform; upgrade first.
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return authorized_url(url)


def chapter_completion(node):
    """Read the server-rendered directory marker, independently of local task processing."""
    scopes = [n for n in node.walk() if 'catalog_task' in n.attrs.get('class', '').split()]
    result = {'isCompleted': None, 'remainingTasks': None}
    if len(scopes) != 1:
        return result
    markers = list(scopes[0].walk())
    has_class = lambda n, cls: cls in n.attrs.get('class', '').split()
    complete = any(has_class(n, 'icon_yiwanc') for n in markers)
    unfinished = any(has_class(n, 'catalog_jindu') for n in markers)
    remaining = set()
    for n in markers:
        if has_class(n, 'bntHoverTips'):
            match = re.fullmatch(r'\s*(\d+)\s*个待完成任务点\s*', n.text())
            if match and 0 < int(match[1]) <= 10000:
                remaining.add(int(match[1]))
    if complete:
        if not remaining and not unfinished:
            result.update(isCompleted=True, remainingTasks=0)
    elif len(remaining) == 1 and unfinished:
        count = next(iter(remaining))
        hidden = [n.attrs.get('value', '') for n in markers if has_class(n, 'knowledgeJobCount')]
        if not hidden or all(v.isascii() and v.isdigit() and int(v) == count for v in hidden):
            result.update(isCompleted=False, remainingTasks=count)
    return result


def directory_task_progress(document):
    values = set()
    for n in document.root.walk():
        if n.tag == 'h2' and 'xs_head_name' in n.attrs.get('class', '').split():
            match = re.fullmatch(r'\s*已完成任务点\s*[:：]\s*(\d+)\s*/\s*(\d+)\s*', n.text())
            if match and 0 <= int(match[1]) <= int(match[2]) <= 1000000:
                values.add((int(match[1]), int(match[2])))
    if len(values) == 1:
        complete, total = next(iter(values))
        return {'completed': complete, 'total': total}
    return None


class ScopedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = scoped_link(req.full_url, newurl)
        if verification_url(newurl):
            raise PlatformVerificationRequired()
        if urllib.parse.urlsplit(newurl).hostname in ("passport2.chaoxing.com", "passport.chaoxing.com"):
            raise RemoteError("Cookie 已失效，请重新登录")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WriteRedirect(ScopedRedirect):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = scoped_link(req.full_url, newurl)
        if verification_url(target):
            raise PlatformVerificationRequired()
        raise RemoteError("提交响应发生跳转，结果待核对；未跟随跳转或重试")


def parse_courses(html):
    doc = Document(html)
    courses = {}
    for node in doc.root.walk():
        href = node.attrs.get("href", "")
        if node.tag != "a" or "/visit/stucoursemiddle?" not in href:
            continue
        url = scoped_link(BASE, href)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if not all(q.get(k, "").isdigit() for k in ("courseid", "clazzid", "cpi")):
            continue
        key = hashlib.sha256((q["courseid"] + ":" + q["clazzid"] + ":" + q["cpi"]).encode()).hexdigest()[:24]
        name = node.text()
        courses.setdefault(key, {"id": key, "name": "课程 " + q["courseid"], "url": url, **q})
        if name:
            courses[key]["name"] = name[:200]
    if not courses and not ("暂无课程" in html and 'id="courseList"' in html):
        raise RemoteError("未识别到课程列表，请检查 Cookie 或页面是否需要验证")
    return list(courses.values())


class Platform:
    def __init__(self, cookie: str | None = None, *, jar=None):
        self.jar = jar if jar is not None else cookie_jar_from_text(cookie or '')
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), ScopedRedirect(), urllib.request.HTTPCookieProcessor(self.jar))
        self.transport = Transport(jar=self.jar)
        self.lock = threading.RLock()
        self.local = threading.local()
        self.local.opener = self.opener
        self.closed = False
        self.pacer = RequestPacer()
        self.stop_requests = threading.Event()
        self.courses = {}
        self.chapter_progress = {}

    def request(self, url, referer=BASE + "/", form=None):
        authorized_url(url)
        authorized_url(referer)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"User-Agent": UA, "Referer": referer, "Accept": "text/html,application/json"}
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            if not self.pacer.acquire(self.stop_requests):
                if self.closed:
                    raise RemoteError("会话已关闭")
                raise PlatformVerificationRequired()
            with self.lock:
                if self.closed:
                    raise RemoteError("会话已关闭")
                if not hasattr(self.local, "opener"):
                    # Handlers belong to one worker; CookieJar internally locks cookie access.
                    self.local.opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({}), ScopedRedirect(), urllib.request.HTTPCookieProcessor(self.jar))
                opener = self.local.opener
            with opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=20) as response:
                authorized_url(response.geturl())
                if verification_url(response.geturl()):
                    raise PlatformVerificationRequired()
                body = response.read(4_000_001)
                if len(body) > 4_000_000:
                    raise RemoteError("平台响应过大")
                html = body.decode("utf-8-sig", "replace")
                # The observed verification HTML can also be returned without a redirect.
                if re.search(r"<input\b[^>]*\b(?:name|id)\s*=\s*(['\"])ucode\1", html, re.I) and "验证码" in html:
                    raise PlatformVerificationRequired()
                return html, response.geturl()
        except PlatformVerificationRequired:
            self.stop_requests.set()
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 429:
                raise RemoteError("平台限制了访问频率，请稍后重试；无需反复重新登录") from None
            raise RemoteError(f"平台课程请求返回 HTTP {code}，请检查访问权限或稍后重试") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if isinstance(exc, RemoteError):
                raise
            raise RemoteError("课程请求失败，Cookie 可能过期或网络不可用") from None

    def list_courses(self):
        html, _ = self.request(BASE + "/mooc2-ans/visit/courselistdata", form={
            "courseType": "1", "courseFolderId": "0", "query": "", "pageHeader": "-1",
            "single": "0", "superstarClass": "0", "isFirefly": "0", "courseLevelLabelId": "0", "fid": "", "from": ""})
        rows = parse_courses(html)
        self.courses = {c["id"]: c for c in rows}
        return [{"id": c["id"], "name": c["name"]} for c in rows]

    def export_login(self):
        """Called only by the encrypted local session store; never exposed through HTTP."""
        fields = ('version', 'name', 'value', 'port', 'port_specified', 'domain', 'domain_specified',
                  'domain_initial_dot', 'path', 'path_specified', 'secure', 'expires', 'discard',
                  'comment', 'comment_url', 'rfc2109')
        with self.lock:
            if self.closed:
                raise RemoteError('登录会话已关闭')
            cookies = [{**{key: getattr(c, key) for key in fields}, 'rest': dict(c._rest)} for c in self.jar]
            return {'cookies': cookies, 'courses': copy.deepcopy(self.courses),
                    'verificationRequired': self.stop_requests.is_set()}

    @classmethod
    def restore_login(cls, saved):
        """Restore scoped cookies and cached course context without contacting the platform."""
        jar = http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(
            strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain))
        for values in saved['cookies']:
            domain = values['domain'].lstrip('.').lower()
            if domain != 'chaoxing.com' and not domain.endswith('.chaoxing.com'):
                raise ValueError('Invalid saved cookie scope')
            jar.set_cookie(http.cookiejar.Cookie(**values))
        jar.clear_expired_cookies()
        if not len(jar) or not isinstance(saved['courses'], dict):
            raise ValueError('Saved login expired')
        platform = cls(jar=jar)
        platform.courses = saved['courses']
        if saved.get('verificationRequired') is True:
            platform.stop_requests.set()
        return platform

    def account_profile(self):
        from .user_records import identity
        hints = dict(getattr(self, 'profile_hint', {}))
        uid = next((c.value for c in self.jar if c.name == '_uid' and c.value.isascii() and c.value.isdigit()), '')
        if uid:
            if hints.get('uid') and str(hints['uid']) != uid:
                hints.pop('displayName', None)
            hints['uid'] = uid
        if not hints.get('displayName'):
            try:
                html, _ = self.request('https://i.chaoxing.com/base')
                name = next((n.text().strip() for n in Document(html).root.walk()
                             if n.tag == 'p' and 'user-name' in n.attrs.get('class', '').split()), '')
                if name and len(name) <= 100:
                    hints['displayName'] = name
            except RemoteError:
                pass
        return identity(hints)

    def submit_form(self, request):
        """Send one observed work form; never follow redirects or retry a write."""
        target = urllib.parse.urlsplit(authorized_url(request.full_url))
        if request.get_method() != "POST" or target.path != "/mooc-ans/work/addStudentWorkNewWeb":
            raise RemoteError("不支持的提交目标")
        try:
            if not self.pacer.acquire(self.stop_requests):
                if self.closed:
                    raise RemoteError("会话已关闭")
                raise PlatformVerificationRequired()
            with self.lock:
                if self.closed:
                    raise RemoteError("会话已关闭")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), WriteRedirect(),
                                                     urllib.request.HTTPCookieProcessor(self.jar))
            with opener.open(request, timeout=30) as response:
                authorized_url(response.geturl())
                body = response.read(4_000_001)
                if len(body) > 4_000_000:
                    raise RemoteError("提交响应过大，结果待核对")
                html = body.decode("utf-8-sig", "replace")
                if verification_url(response.geturl()) or ("验证码" in html and re.search(r'<input\b[^>]*\b(?:name|id)\s*=\s*([\'\"])ucode\1', html, re.I)):
                    raise PlatformVerificationRequired()
                return html, response.geturl()
        except PlatformVerificationRequired:
            self.stop_requests.set()
            raise
        except (urllib.error.URLError, OSError, ValueError):
            raise RemoteError("提交请求结果待核对，未自动重试") from None

    def run_quiz(self, setting, study, attachment, directory, *, cancel_event, answer_resolver=None):
        from .quiz import run_quiz
        return run_quiz(self, setting, study, attachment, directory, cancel_event=cancel_event,
                        answer_resolver=answer_resolver)

    def chapters(self, course):
        home, home_url = self.request(course["url"])
        doc = Document(home)
        d = doc.inputs()
        if any(not d.get(k) for k in ("courseid", "clazzid", "cpi", "enc", "t")):
            raise RemoteError("课程主页缺少必要的课程参数，未判定为登录失效；请检查原网页是否存在验证或权限提示")
        if any(d[k] != course[k] for k in ("courseid", "clazzid", "cpi")):
            raise RemoteError("课程身份不匹配")
        menu = None
        for n in doc.root.walk():
            if n.tag == "li" and n.attrs.get("dataname") == "zj":
                menu = next((x.attrs["data-url"] for x in n.walk() if x.attrs.get("data-url")), None)
                break
        if not menu:
            raise RemoteError("此课程没有可识别的章节入口")
        url = add_query(scoped_link(home_url, menu), {"courseid": d["courseid"], "clazzid": d["clazzid"],
                        "cpi": d["cpi"], "ut": "s", "t": d["t"], "stuenc": d["enc"]})
        html, url = self.request(url, home_url)
        # Extract literal data only, never execute inline handlers or scripts.
        enc = re.search(r'\bvar\s+enc\s*=\s*[\'\"]([a-fA-F0-9]+)[\'\"]', html)
        if not enc:
            raise RemoteError("章节访问凭证缺失，可能需要人工验证")
        directory = Document(html)
        rows, seen = [], set()
        for n in directory.root.walk():
            m = re.fullmatch(r"\s*toOld\(\s*['\"](\d+)['\"]\s*,\s*['\"](\d+)['\"]\s*,\s*['\"](\d+)['\"]\s*,\s*(\d+)\s*\)\s*;?\s*", n.attrs.get("onclick", ""))
            if m and m[1] == d["courseid"] and m[3] == d["clazzid"] and m[2] not in seen:
                seen.add(m[2])
                rows.append({"id": m[2], "name": n.attrs.get("title") or ("章节 " + str(len(rows) + 1)),
                             "hidden": m[4], "enc": enc[1], "referer": url, **chapter_completion(n)})
        if not rows:
            raise RemoteError("没有可识别的学习章节")
        if len(rows) > 2000:
            raise RemoteError("章节数量超过单课程限制")
        with self.lock:
            self.chapter_progress[course['id']] = directory_task_progress(directory)
        return rows

    def study_url(self, course, chapter):
        values = {"chapterId": chapter["id"], "courseId": course["courseid"], "clazzid": course["clazzid"],
                  "cpi": course["cpi"], "enc": chapter["enc"], "mooc2": "1", "hidetype": chapter["hidden"], "fanyaVersion": "0"}
        return add_query(MOOC + "/mycourse/studentstudy", values)

    def prepare_catalog(self, course, chapters):
        # The website opens the learning shell once, then switches chapters by AJAX.
        if chapters:
            self.request(self.study_url(course, chapters[0]), chapters[0]["referer"])

    def catalog_cards(self, course, chapter, *, cancel_event=None):
        yield from self.chapter_cards(course, chapter, open_study=False, cancel_event=cancel_event)

    def learning_cards(self, course, chapter, *, cancel_event):
        yield from self.chapter_cards(course, chapter, cancel_event=cancel_event)

    def chapter_cards(self, course, chapter, *, open_study=True, cancel_event=None):
        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        if cancelled():
            return
        study_url = self.study_url(course, chapter)
        ut_enc = ""
        if open_study:
            shell, _ = self.request(study_url, chapter["referer"])
            match = re.search(r'\butEnc\s*=\s*([\'\"])([A-Za-z0-9_-]+)\1', shell)
            ut_enc = match[2] if match else ""
        if cancelled():
            return
        ajax_url = add_query(MOOC + "/mycourse/studentstudyAjax", {
            "courseId": course["courseid"], "clazzid": course["clazzid"], "chapterId": chapter["id"],
            "cpi": course["cpi"], "verificationcode": "", "mooc2": "1", "toComputer": "false",
            "microTopicId": "0", "editorPreview": "0", "isPreviewVideo": "false", "cardIndex": "0"})
        html, _ = self.request(ajax_url, study_url)
        if cancelled():
            return
        doc = Document(html)
        if doc.inputs().get("chapterFaceState") not in (None, "", "0", "false"):
            raise ChapterUnavailable("章节需要人脸验证，请在正常播放器中完成")
        frame = next((n for n in doc.root.walk() if n.tag == "iframe" and "/knowledge/cards?" in n.attrs.get("src", "")), None)
        if not frame:
            raise ChapterUnavailable("未找到章节卡片，可能需要验证码或权限验证")
        try:
            count = int(doc.inputs()["cardcount"])
        except (KeyError, ValueError):
            raise RemoteError("无法确认章节卡片数量") from None
        if not 1 <= count <= 100:
            raise RemoteError("章节卡片数量超出支持范围")
        base = scoped_link(study_url, frame.attrs["src"])
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(base).query))
        if any(q.get(k) != v for k, v in {"courseid": course["courseid"], "clazzid": course["clazzid"], "knowledgeid": chapter["id"]}.items()):
            raise RemoteError("卡片课程身份不匹配")
        for index in range(count):
            if cancelled():
                return
            url = add_query(base, {"num": str(index)})
            card, _ = self.request(url, study_url)
            try:
                setting = card_setting(card)
            except RemoteError:
                raise ChapterUnavailable("本章卡片格式暂不支持，请在原网页查看；其他章节仍可读取") from None
            modules = []
            for node in Document(card).root.walk():
                if node.tag != "iframe" or not (node.attrs.get('module') == 'work'
                        or "/ananas/modules/work/" in node.attrs.get("src", "")):
                    continue
                try:
                    value = json.loads(node.attrs.get("data", ""))
                    if isinstance(value, dict):
                        modules.append(value)
                except (ValueError, TypeError):
                    pass
            yield setting, {"url": study_url, "cpi": course["cpi"], "cards_url": url,
                            "utEnc": ut_enc, "workModules": modules}

    def context(self, setting, study, attachment):
        object_id = attachment["objectId"]
        base = urllib.parse.urlsplit(study["cards_url"])
        origin = base.scheme + "://" + base.netloc
        media_url = add_query(origin + "/ananas/status/" + urllib.parse.quote(object_id, safe=""),
                              {"k": setting["defaults"]["fid"], "flag": "normal", "ro": "0"})
        media = self.transport.get(media_url, origin + "/ananas/modules/video/index.html")
        return CourseContext(copy.deepcopy(setting), media, dict(study), object_id)

    def close(self):
        with self.lock:
            self.closed = True
            self.stop_requests.set()
            self.jar.clear()
            self.courses.clear()
            self.chapter_progress.clear()
