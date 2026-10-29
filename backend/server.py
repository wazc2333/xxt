"""Same-origin JSON API and static frontend with encrypted remembered logins."""
from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import signal
import sys
import threading
import time
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playback_simulator.chaoxing_client import RemoteError
from .platform import PlatformVerificationRequired
from .service import Manager, ServiceError
from .admin import AdminAuth

STATIC = Path(__file__).parent / "static"


def source_version():
    root = Path(__file__).resolve().parent.parent
    names = ("requirements.txt", "backend/homework.py", "backend/deepseek.py", "backend/answer_bank.py", "backend/credentials.py", "backend/platform.py", "backend/service.py", "backend/server.py", "backend/auth.py", "backend/admin.py", "backend/user_records.py", "backend/quiz.py", "backend/work_form.py", "backend/static/admin.html", "backend/static/admin.js", "backend/static/admin.css", "backend/static/app.js",
             "backend/static/index.html", "backend/static/tasks.css", "backend/static/style.css",
             "playback_simulator/chaoxing_client.py")
    names += ('backend/font_decoder.py', 'backend/resources/cxsecret-glyphs.json')
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()[:16]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, manager, *, public_origin=None, secure=False, admin_auth=None):
        self.manager, self.secure, self.public_origin = manager, secure, public_origin
        self.admin = admin_auth if admin_auth is not None else AdminAuth(manager.data_dir / 'admin-auth.json')
        self.source_version = source_version()
        self.slots = threading.BoundedSemaphore(32)
        self.rate_lock, self.login_rates = threading.Lock(), {}
        super().__init__(address, Handler)
        port = self.server_address[1]
        if public_origin:
            self.origins = {public_origin}
        else:
            self.origins = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
        self.hosts = {urllib.parse.urlsplit(o).netloc for o in self.origins}

    def process_request(self, request, client_address):
        # Bound thread creation, not just execution inside an unbounded thread pool.
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def login_allowed(self, ip):
        now = time.monotonic()
        with self.rate_lock:
            self.login_rates = {k: v for k, v in self.login_rates.items() if now - v[0] < 60}
            start, count = self.login_rates.get(ip, (now, 0))
            self.login_rates[ip] = (start, count + 1)
            return count < 10


class Handler(BaseHTTPRequestHandler):
    server_version = "LearningService/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_args):
        pass  # Never log URLs, user credentials, request headers or bodies.

    def send(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
        if cookie is None:
            cookie = getattr(self, 'renew_session_cookie', None)
        data = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
        self.send_response(status)
        for k, v in {"Content-Type": content_type, "Content-Length": str(len(data)), "Cache-Control": "no-store",
                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                     "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                     "Connection": "close"}.items():
            self.send_header(k, v)
        for value in (cookie if isinstance(cookie, (tuple, list)) else [cookie]):
            if value:
                self.send_header("Set-Cookie", value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def token(self, name="learning_session"):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            return jar[name].value if name in jar else ""
        except CookieError:
            return ""

    def session_cookie(self, token, clear=False):
        return ("learning_session=" + token + "; HttpOnly; SameSite=Strict; Path=/" +
                ("; Secure" if self.server.secure else "") + '; Max-Age=' +
                ('0' if clear else str(int(self.server.manager.session_ttl))))

    def qr_cookie(self, token, clear=False):
        return ('learning_qr=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=' + ('0' if clear else '180')
                + ('; Secure' if self.server.secure else ''))

    def admin_cookie(self, token, clear=False):
        return ('learning_admin=' + token + '; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age='
                + ('0' if clear else '28800') + ('; Secure' if self.server.secure else ''))

    def do_GET(self):
        self.handle_route(False)

    def do_POST(self):
        self.handle_route(True)

    def handle_route(self, post):
        try:
            if self.headers.get("Host") not in self.server.hosts:
                raise ServiceError("Host 不在部署地址列表中", 403)
            path = urllib.parse.urlsplit(self.path).path
            if post:
                if self.headers.get("Origin") not in self.server.origins or self.headers.get("X-Requested-With") != "learning-web":
                    raise ServiceError("请求来源校验失败", 403)
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise ServiceError("请求必须是 JSON", 415)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ServiceError("请求长度无效") from None
                limit = 2_000_000 if path == '/api/homework/bank/import' else 24576
                if not 0 < length <= limit:
                    raise ServiceError("请求体大小无效", 413)
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise ServiceError("JSON 格式无效") from None
                if not isinstance(body, dict):
                    raise ServiceError("请求必须是 JSON 对象")
            else:
                body = {}
            manager = self.server.manager
            token = self.token()
            if path == "/api/health" and not post:
                return self.send(200, {"status": "ok", "version": self.server.source_version})
            if path.startswith('/api/admin/'):
                admin_token = self.token('learning_admin')
                if path == '/api/admin/login' and post:
                    new_token, result = self.server.admin.login(body.get('username'), body.get('password'),
                                                               self.client_address[0])
                    self.server.admin.logout(admin_token)
                    return self.send(200, result, cookie=self.admin_cookie(new_token))
                admin_session = self.server.admin.session(admin_token)
                if post and not hmac.compare_digest(self.headers.get('X-CSRF-Token', '').encode(),
                                                    admin_session['csrf'].encode()):
                    raise ServiceError('管理员会话校验失败，请刷新页面', 403)
                if path == '/api/admin/session' and not post:
                    return self.send(200, admin_session)
                if path == '/api/admin/logout' and post:
                    self.server.admin.logout(admin_token)
                    return self.send(200, {'ok': True}, cookie=self.admin_cookie('', clear=True))
                if path == '/api/admin/users' and not post:
                    try:
                        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True,
                                                      max_num_fields=10)
                        if set(query) - {'search', 'status', 'method', 'page'} or any(len(v) != 1 for v in query.values()):
                            raise ValueError('Unexpected query')
                        page = int(query.get('page', ['1'])[0])
                    except ValueError:
                        raise ServiceError('用户查询参数无效') from None
                    return self.send(200, manager.admin_users(search=query.get('search', [''])[0],
                                     status=query.get('status', [''])[0], method=query.get('method', [''])[0], page=page))
                pieces = path.strip('/').split('/')
                if len(pieces)==5 and pieces[:3]==['api','admin','users']:
                    if pieces[4]=='control' and not post:
                        try:
                            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,keep_blank_values=True,max_num_fields=4)
                            if set(query)-{'courseId','kind'} or any(len(v)!=1 for v in query.values()):
                                raise ValueError()
                        except ValueError:
                            raise ServiceError('任务查询参数无效') from None
                        return self.send(200,manager.admin_control(pieces[3],course_id=query.get('courseId',[None])[0],
                                                                  kind=query.get('kind',['chapters'])[0]))
                    if post and pieces[4] in ('sk','stop','scan','start','delete'):
                        result = manager.admin_user_action(pieces[3],pieces[4],body)
                        return self.send(202 if pieces[4] in ('scan','start','stop') else 200,result)
                raise ServiceError('管理员接口不存在', 404)
            if path in ("/api/login/qr", "/api/login/qr/status", "/api/login/qr/cancel") and post:
                owner = self.token('learning_qr')
                if path.endswith('/cancel'):
                    manager.qr.cancel(owner)
                    return self.send(200, {'ok': True}, cookie=self.qr_cookie('', clear=True))
                if token in manager.sessions:
                    if path.endswith('/status'):
                        s = manager.session(token)
                        return self.send(200, {'status': 'authenticated', **manager.session_snapshot(s)},
                                         cookie=self.qr_cookie('', clear=True))
                    raise ServiceError('请先退出当前登录', 409)
                if path == '/api/login/qr':
                    if not self.server.login_allowed(self.client_address[0]):
                        raise ServiceError('登录尝试过于频繁，请稍后重试', 429)
                    owner, result = manager.qr.start(owner)
                    return self.send(200, result, cookie=self.qr_cookie(owner))
                result, new_token = manager.qr.poll(owner)
                cookies = None
                if new_token:
                    manager.session(new_token)
                    cookies = [self.session_cookie(new_token), self.qr_cookie('', clear=True)]
                return self.send(200, result, cookie=cookies)
            if path == "/api/login" and post:
                if not self.server.login_allowed(self.client_address[0]):
                    raise ServiceError("登录尝试过于频繁，请稍后重试", 429)
                if token in manager.sessions:
                    raise ServiceError("请先退出当前登录", 409)
                manager.qr.cancel(self.token('learning_qr'))
                new_token, s, courses = manager.login(body.get("cookie"))
                return self.send(200, {"authenticated": True, "csrf": s.csrf, "courses": courses, "jobs": []},
                                 cookie=[self.qr_cookie('', clear=True), self.session_cookie(new_token)])
            if path.startswith("/api/"):
                s = manager.session(token)
                self.renew_session_cookie = self.session_cookie(token)
                if post and not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode("utf-8"), s.csrf.encode("utf-8")):
                    raise ServiceError("会话校验失败，请刷新页面", 403)
                if path == "/api/session" and not post:
                    return self.send(200, manager.session_snapshot(s))
                if path == "/api/courses" and not post:
                    return self.send(200, {"courses": manager.refresh_courses(s)})
                if path in ('/api/ai/settings', '/api/homework/settings'):
                    if post and ('key' not in body or not isinstance(body['key'], str)):
                        raise ServiceError('需要提供 key 字符串，空字符串表示清除 SK')
                    return self.send(200, manager.ai_settings(s, body['key'] if post else None))
                if path == '/api/homework/bank/import' and post:
                    return self.send(200, manager.import_bank(s, body.get('questions')))
                if path == '/api/homework/bank' and not post:
                    return self.send(200, manager.answer_bank.export(s.record_id))
                if path == '/api/homework/jobs' and post:
                    return self.send(202, manager.start_homework(s, body.get('courseId'), body.get('workIds')))
                if path == "/api/logout" and post:
                    manager.logout(token)
                    return self.send(200, {"ok": True}, cookie=self.session_cookie("", clear=True))
                if path == "/api/jobs" and post:
                    if "taskIds" in body and body["taskIds"] is None:
                        raise ServiceError("taskIds 必须是已选择的视频任务数组")
                    if "chapterIds" in body and body["chapterIds"] is None:
                        raise ServiceError("chapterIds 必须是已选择的章节数组")
                    return self.send(202, manager.start(s, body.get("courseIds"), body.get("taskIds"),
                        chapter_ids=body.get("chapterIds"), quiz_policy=body.get("quizPolicy", "skip")))
                pieces = path.strip("/").split("/")
                if len(pieces) in (4, 5) and pieces[:2] == ['api', 'courses'] and pieces[3] == 'homework':
                    if len(pieces) == 4 and not post:
                        return self.send(200, manager.homework_catalog(s, pieces[2]))
                    if len(pieces) == 5 and pieces[4] == 'scan' and post:
                        return self.send(202, manager.scan_homework(s, pieces[2]))
                if len(pieces) in (4, 5) and pieces[:2] == ["api", "courses"] and pieces[3] == "tasks":
                    if len(pieces) == 4 and not post:
                        return self.send(200, manager.catalog(s, pieces[2]))
                    if len(pieces) == 5 and pieces[4] == "scan" and post:
                        return self.send(202, manager.scan(s, pieces[2], scope=body.get("scope", "all"),
                                                          chapter_ids=body.get("chapterIds")))
                if len(pieces) in (3, 4) and pieces[:2] == ["api", "jobs"]:
                    job = manager.job(s, pieces[2])
                    if len(pieces) == 3 and not post:
                        return self.send(200, job.snapshot())
                    if len(pieces) == 4 and pieces[3] == "stop" and post:
                        job.cancel.set()
                        return self.send(202, job.snapshot())
                raise ServiceError("接口不存在", 404)
            if post:
                raise ServiceError("接口不存在", 404)
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/admin": ("admin.html", "text/html; charset=utf-8"),
                     "/admin/": ("admin.html", "text/html; charset=utf-8"),
                     "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
                     "/admin.css": ("admin.css", "text/css; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/tasks.css": ("tasks.css", "text/css; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in files:
                raise ServiceError("页面不存在", 404)
            name, mime = files[path]
            return self.send(200, (STATIC / name).read_bytes(), mime)
        except ServiceError as exc:
            self.send(exc.status, {"error": str(exc)})
        except RemoteError as exc:
            self.send(502, {"error": str(exc),
                            "actionRequired": "platform_verification" if isinstance(exc, PlatformVerificationRequired) else None})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.send(500, {"error": "服务处理失败，请稍后重试"})


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Chaoxing course learning backend and frontend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--public-origin", help="浏览器访问的完整来源，例如 https://learn.example.com")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--max-jobs", type=int, default=4)
    args = parser.parse_args()
    if args.public_origin:
        p = urllib.parse.urlsplit(args.public_origin)
        if (p.scheme not in ("https", "http") or not p.hostname or p.path or p.query or p.fragment or p.username or p.password):
            parser.error("public-origin 必须是无路径的 HTTP(S) 来源")
    if args.host not in ("127.0.0.1", "localhost") and not args.public_origin:
        parser.error("对外监听时必须设置 --public-origin")
    if not 1 <= args.max_jobs <= 32 or not 0 < args.port < 65536:
        parser.error("max-jobs 范围 1..32；port 范围 1..65535")
    manager = Manager(args.data_dir, max_jobs=args.max_jobs)
    server = Server((args.host, args.port), manager, public_origin=args.public_origin,
                    secure=bool(args.public_origin and args.public_origin.startswith("https://")))

    def stop(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    print("Learning service listening on " + (args.public_origin or f"http://{args.host}:{args.port}"), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        manager.close()


if __name__ == "__main__":
    main()
