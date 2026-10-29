"""Official QR sign-in, with browser-owned, expiring, memory-only challenges."""
from __future__ import annotations

import base64
import http.cookiejar
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from playback_simulator.chaoxing_client import RemoteError, UA, authorized_url
from .platform import Document, Platform, PlatformVerificationRequired, scoped_link, verification_url
from .service import ServiceError

PASSPORT = 'https://passport2.chaoxing.com'
LOGIN_URL = PASSPORT + '/login?newversion=true&refer=https%3A%2F%2Fi.chaoxing.com%2F'
MESSAGES = {'waiting': '请使用学习通 App 扫描二维码', 'scanned': '已扫码，请在手机上确认登录',
            'expired': '二维码已过期，请刷新二维码', 'cancelled': '已取消扫码登录，请刷新二维码重试',
            'failed': '扫码登录未完成，请刷新重试或使用 Cookie 登录',
            'verification_required': '此账号需要额外验证，请在超星官方网页完成登录后使用 Cookie 登录',
            'authenticated': '登录成功，正在获取课程'}


class AuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = scoped_link(req.full_url, newurl)
        if verification_url(url):
            raise PlatformVerificationRequired()
        return super().redirect_request(req, fp, code, msg, headers, url)


class PassportQR:
    """One isolated passport session; never exports cookies to the browser."""
    def __init__(self):
        self.jar = http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(
            strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), AuthRedirect(),
                                                 urllib.request.HTTPCookieProcessor(self.jar))
        self.fields, self.image, self.transferred = {}, '', False
        self.profile = {}

    def request(self, url, *, form=None, image=False):
        authorized_url(url)
        headers = {'User-Agent': UA, 'Referer': LOGIN_URL, 'Accept': 'image/*' if image else 'text/html,application/json'}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers.update({'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'Origin': PASSPORT, 'X-Requested-With': 'XMLHttpRequest'})
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=20) as response:
                authorized_url(response.geturl())
                if verification_url(response.geturl()):
                    raise PlatformVerificationRequired()
                limit = 500_000 if image else 2_000_000
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise RemoteError('扫码登录响应过大，请稍后重试')
                return body
        except PlatformVerificationRequired:
            raise
        except (urllib.error.URLError, OSError, ValueError):
            raise RemoteError('无法连接扫码登录服务，请稍后重试或使用 Cookie 登录') from None

    def create(self):
        html = self.request(LOGIN_URL).decode('utf-8-sig', 'replace')
        document = Document(html)
        inputs = document.inputs()
        self.fields = {k: inputs.get(k, '') for k in ('uuid', 'enc', 'doubleFactorLogin', 'forbidotherlogin')}
        if not all(isinstance(self.fields[k], str) and 0 < len(self.fields[k]) <= 2048 for k in ('uuid', 'enc')):
            raise RemoteError('扫码登录页面格式已变化，请使用 Cookie 登录')
        frame = next((n for n in document.root.walk() if n.attrs.get('id') == 'quickCode'), None)
        if frame is None:
            raise RemoteError('官方页面未提供扫码登录入口')
        url = scoped_link(LOGIN_URL, frame.attrs.get('src', ''))
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if (parsed.hostname != 'passport2.chaoxing.com' or parsed.path != '/createqr'
                or query.get('uuid') != self.fields['uuid']):
            raise RemoteError('二维码与本次登录会话不匹配')
        body = self.request(url, image=True)
        # The official endpoint currently labels PNG bytes as image/jpeg.
        if body.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif body.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        else:
            raise RemoteError('官方服务未返回有效二维码图片，请刷新重试')
        self.image = 'data:' + mime + ';base64,' + base64.b64encode(body).decode('ascii')
        return self.image

    def poll(self):
        try:
            data = json.loads(self.request(PASSPORT + '/getauthstatus/v2', form=self.fields))
        except (ValueError, TypeError):
            raise RemoteError('扫码状态响应无法识别，请刷新二维码重试') from None
        if not isinstance(data, dict) or not isinstance(data.get('status'), bool):
            raise RemoteError('扫码状态响应格式已变化，请使用 Cookie 登录')
        uid = str(data.get('uid') or '')
        nickname = data.get('nickname')
        if uid.isascii() and uid.isdigit() and len(uid) <= 32:
            self.profile['uid'] = uid
        if isinstance(nickname, str) and nickname.strip():
            self.profile['displayName'] = nickname.strip()[:100]
        if data['status']:
            if data.get('containTwoFactorLogin') not in (None, False, 0, '0', ''):
                return 'verification_required'
            return 'authorized'
        return {'4': 'scanned', '6': 'cancelled', '7': 'failed', '2': 'expired'}.get(str(data.get('type')), 'waiting')

    def take_platform(self):
        platform = Platform(jar=self.jar)
        platform.profile_hint = dict(self.profile)
        self.transferred = True
        return platform

    def close(self):
        self.fields.clear()
        self.image = ''
        self.profile.clear()
        if not self.transferred:
            self.jar.clear()


@dataclass
class Attempt:
    provider: object
    expires: float
    lock: object = field(default_factory=threading.RLock)
    last_poll: float = float('-inf')
    state: str = 'waiting'
    result: dict | None = None
    session_token: str | None = None
    cancelled: object = field(default_factory=threading.Event)


class QRLogins:
    def __init__(self, adopt, *, revoke=None, factory=PassportQR, clock=time.monotonic, ttl=150, poll_interval=3):
        self.adopt, self.factory, self.clock = adopt, factory, clock
        self.revoke = revoke
        self.ttl, self.poll_interval = ttl, poll_interval
        self.lock, self.attempts, self.creating, self.closed = threading.RLock(), {}, 0, False

    def snapshot(self, attempt):
        return {'status': attempt.state, 'message': MESSAGES[attempt.state],
                'expiresIn': max(0, int(attempt.expires - self.clock())), 'pollAfter': self.poll_interval}

    def start(self, previous=''):
        self.cancel(previous)
        self.reap()
        with self.lock:
            if self.closed or len(self.attempts) + self.creating >= 128 or self.creating >= 4:
                raise ServiceError('扫码登录会话已满，请稍后重试', 429)
            self.creating += 1
        provider = None
        registered = False
        try:
            provider = self.factory()
            image = provider.create()
            attempt = Attempt(provider, self.clock() + self.ttl)
            token = secrets.token_urlsafe(32)
            with self.lock:
                if self.closed:
                    raise ServiceError('服务正在停止', 503)
                self.attempts[token] = attempt
                registered = True
            return token, {**self.snapshot(attempt), 'image': image}
        finally:
            with self.lock:
                self.creating -= 1
            if provider and not registered:
                provider.close()

    def poll(self, token):
        with self.lock:
            attempt = self.attempts.get(token)
        if not attempt:
            return {'status': 'expired', 'message': MESSAGES['expired'], 'expiresIn': 0}, None
        with attempt.lock:
            if attempt.cancelled.is_set():
                attempt.state = 'cancelled'
            elif self.clock() >= attempt.expires:
                attempt.state = 'expired'
                attempt.provider.close()
            elif attempt.state in ('waiting', 'scanned') and self.clock() - attempt.last_poll >= self.poll_interval:
                attempt.last_poll = self.clock()
                try:
                    state = attempt.provider.poll()
                    if attempt.cancelled.is_set():
                        state = 'cancelled'
                    if state == 'authorized':
                        platform = attempt.provider.take_platform()
                        session_token, session, courses = self.adopt(platform)
                        attempt.session_token = session_token
                        attempt.result = {'authenticated': True, 'csrf': session.csrf, 'courses': courses, 'jobs': []}
                        if attempt.cancelled.is_set():
                            if self.revoke:
                                self.revoke(session_token)
                            state = 'cancelled'
                        else:
                            state = 'authenticated'
                    attempt.state = state
                except PlatformVerificationRequired:
                    attempt.state = 'verification_required'
                except RemoteError:
                    attempt.state = 'failed'
                except ServiceError:
                    attempt.state = 'failed'
                except Exception:
                    attempt.state = 'failed'
                finally:
                    if attempt.state not in ('waiting', 'scanned'):
                        attempt.provider.close()
            result = self.snapshot(attempt)
            if attempt.state == 'authenticated':
                result.update(attempt.result)
                return result, attempt.session_token
            return result, None

    def cancel(self, token):
        with self.lock:
            attempt = self.attempts.pop(token, None)
            if attempt:
                attempt.cancelled.set()
        if attempt:
            with attempt.lock:
                attempt.state = 'cancelled'
                attempt.provider.close()

    def reap(self):
        with self.lock:
            expired = [token for token, a in self.attempts.items() if self.clock() >= a.expires]
        for token in expired:
            self.cancel(token)

    def close(self):
        with self.lock:
            self.closed = True
            tokens = list(self.attempts)
        for token in tokens:
            self.cancel(token)
