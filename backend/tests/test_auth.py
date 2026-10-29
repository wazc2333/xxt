import base64
import http.client
import json
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

from backend.auth import LOGIN_URL, PASSPORT, PassportQR, QRLogins
from backend.platform import Platform, PlatformVerificationRequired
from backend.server import Server
from backend.service import Manager
from backend.tests.test_service import FakePlatform, finished_runner
from playback_simulator.chaoxing_client import RemoteError

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9lcAAAAASUVORK5CYII=')


class FakeQR:
    def __init__(self):
        self.states = ['scanned', 'authorized']
        self.reads, self.closed, self.transferred = 0, False, False
        self.platform = FakePlatform('qr-test-only')

    def create(self):
        return 'data:image/png;base64,' + base64.b64encode(PNG).decode()

    def poll(self):
        self.reads += 1
        return self.states.pop(0) if self.states else 'waiting'

    def take_platform(self):
        self.transferred = True
        return self.platform

    def close(self):
        self.closed = True
        if not self.transferred:
            self.platform.close()


class PassportTests(unittest.TestCase):
    def test_create_uses_actual_page_uuid_and_image_bytes_instead_of_wrong_mime(self):
        provider = PassportQR()
        calls = []
        html = b'<input id="uuid" value="private-test-uuid"><input id="enc" value="private-test-enc"><input id="doubleFactorLogin" value="0"><input id="forbidotherlogin" value="0"><img id="quickCode" src="/createqr?uuid=private-test-uuid&amp;fid=-1">'
        def request(url, **options):
            calls.append((url, options))
            if options.get('image'):
                return PNG
            return html
        provider.request = request
        try:
            self.assertTrue(provider.create().startswith('data:image/png;base64,'))
            self.assertEqual(provider.fields['enc'], 'private-test-enc')
            self.assertEqual(calls[0][0], LOGIN_URL)
            self.assertIn('/createqr?uuid=private-test-uuid', calls[1][0])
        finally:
            provider.close()

    def test_image_identity_or_html_image_is_rejected(self):
        for src, data in [('/createqr?uuid=other', PNG), ('https://evil.example/createqr?uuid=same', PNG),
                          ('/createqr?uuid=same', b'<html>verification</html>')]:
            provider = PassportQR()
            page = f'<input id="uuid" value="same"><input id="enc" value="enc"><img id="quickCode" src="{src}">'.encode()
            provider.request = lambda url, **kw: data if kw.get('image') else page
            try:
                with self.subTest(src=src), self.assertRaises(RemoteError):
                    provider.create()
            finally:
                provider.close()

    def test_v2_states_and_two_factor_not_bypassed(self):
        provider = PassportQR()
        provider.fields = {'uuid': 'private-uuid', 'enc': 'private-enc', 'doubleFactorLogin': '0', 'forbidotherlogin': '0'}
        samples = [({'status': False, 'type': '3'}, 'waiting'), ({'status': False, 'type': 4}, 'scanned'),
                   ({'status': False, 'type': '2'}, 'expired'), ({'status': False, 'type': 6}, 'cancelled'),
                   ({'status': False, 'type': 7}, 'failed'), ({'status': True}, 'authorized'),
                   ({'status': True, 'containTwoFactorLogin': True, 'twoFactorLoginPCUrl': 'private'}, 'verification_required')]
        try:
            for body, state in samples:
                def request(url, *, form):
                    self.assertEqual(url, PASSPORT + '/getauthstatus/v2')
                    self.assertEqual(form, provider.fields)
                    return json.dumps(body).encode()
                provider.request = request
                self.assertEqual(provider.poll(), state)
        finally:
            provider.close()

    def test_invalid_status_does_not_authenticate(self):
        provider = PassportQR()
        try:
            for body in (b'null', b'{"status":"true"}', b'<html>login</html>'):
                provider.request = lambda *args, **kw: body
                with self.assertRaises(RemoteError):
                    provider.poll()
        finally:
            provider.close()

    def test_platform_adopts_same_jar_without_cookie_export_or_clearing_on_detach(self):
        provider = PassportQR()
        jar = provider.jar
        platform = provider.take_platform()
        self.assertIs(platform.jar, jar)
        with patch.object(jar, 'clear') as clear:
            provider.close()
            clear.assert_not_called()
        platform.close()


class QRManagerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.now = 1000
        self.providers = []
        def factory():
            p = FakeQR()
            self.providers.append(p)
            return p
        self.manager = Manager(Path(self.folder.name), platform_factory=FakePlatform, qr_factory=factory,
                               runner=finished_runner, clock=lambda: self.now)

    def tearDown(self):
        self.manager.close()
        self.folder.cleanup()

    def test_poll_throttling_confirmation_and_single_session_adoption(self):
        owner, image = self.manager.qr.start()
        self.assertIn('image', image)
        self.assertNotIn('uuid', str(image))
        first, token = self.manager.qr.poll(owner)
        self.assertEqual(first['status'], 'scanned')
        self.assertIsNone(token)
        self.manager.qr.poll(owner)
        self.assertEqual(self.providers[0].reads, 1)
        self.now += 3
        result, token = self.manager.qr.poll(owner)
        self.assertEqual(result['status'], 'authenticated')
        self.assertEqual(len(result['courses']), 2)
        self.assertEqual(len(self.manager.sessions), 1)
        self.assertIs(self.manager.session(token).platform, self.providers[0].platform)
        self.assertFalse(self.providers[0].platform.closed)
        again, same = self.manager.qr.poll(owner)
        self.assertEqual(same, token)
        self.assertEqual(len(self.manager.sessions), 1)
        self.assertEqual(list(Path(self.folder.name).rglob('*.json')), [])

    def test_foreign_or_expired_owner_cannot_obtain_image_or_authentication(self):
        owner, _ = self.manager.qr.start()
        value, token = self.manager.qr.poll('foreign')
        self.assertEqual(value['status'], 'expired')
        self.assertNotIn('image', value)
        self.assertIsNone(token)
        self.now += 150
        value, token = self.manager.qr.poll(owner)
        self.assertEqual(value['status'], 'expired')
        self.assertEqual(self.providers[0].reads, 0)
        self.assertTrue(self.providers[0].closed)
        self.manager.qr.reap()
        self.assertEqual(self.manager.qr.attempts, {})

    def test_refresh_cancels_old_challenge_and_server_close_clears_pending(self):
        first, _ = self.manager.qr.start()
        second, _ = self.manager.qr.start(first)
        self.assertNotEqual(first, second)
        self.assertTrue(self.providers[0].closed)
        self.assertEqual(self.manager.qr.poll(first)[0]['status'], 'expired')
        self.manager.close()
        self.assertTrue(self.providers[1].closed)

    def test_two_factor_or_verification_requires_human_without_importing_session(self):
        for value in ('verification_required', PlatformVerificationRequired()):
            owner, _ = self.manager.qr.start()
            provider = self.providers[-1]
            if isinstance(value, Exception):
                def poll():
                    raise value
                provider.poll = poll
            else:
                provider.states = [value]
            data, token = self.manager.qr.poll(owner)
            self.assertEqual(data['status'], 'verification_required')
            self.assertIsNone(token)
            self.assertTrue(provider.closed)
        self.assertEqual(self.manager.sessions, {})

    def test_failure_to_fetch_courses_closes_authenticated_platform(self):
        owner, _ = self.manager.qr.start()
        provider = self.providers[0]
        provider.states = ['authorized']
        def fail():
            raise RemoteError('private upstream detail')
        provider.platform.list_courses = fail
        result, token = self.manager.qr.poll(owner)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('private upstream', str(result))
        self.assertIsNone(token)
        self.assertTrue(provider.platform.closed)

    def test_cancel_during_scan_request_prevents_late_session_creation(self):
        owner, _ = self.manager.qr.start()
        provider = self.providers[0]
        entered, release = threading.Event(), threading.Event()
        def poll():
            entered.set()
            release.wait(2)
            return 'authorized'
        provider.poll = poll
        result = []
        worker = threading.Thread(target=lambda: result.append(self.manager.qr.poll(owner)))
        worker.start()
        self.assertTrue(entered.wait(1))
        attempt = self.manager.qr.attempts[owner]
        cancel = threading.Thread(target=self.manager.qr.cancel, args=(owner,))
        cancel.start()
        self.assertTrue(attempt.cancelled.wait(1))
        release.set()
        worker.join(2)
        cancel.join(2)
        self.assertEqual(result[0][0]['status'], 'cancelled')
        self.assertEqual(self.manager.sessions, {})


class QRHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.folder.name), platform_factory=FakePlatform, qr_factory=FakeQR)
        self.now = 0
        self.manager.qr.clock = lambda: self.now
        self.server = Server(('127.0.0.1', 0), self.manager)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookies = {}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.folder.cleanup()

    def request(self, path, body=None, *, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        headers = {'Origin': origin or f'http://127.0.0.1:{self.port}', 'X-Requested-With': 'learning-web',
                   'Content-Type': 'application/json', 'Cookie': '; '.join(k+'='+v for k, v in self.cookies.items())}
        connection.request('GET' if body is None else 'POST', path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        cookie_headers = response.headers.get_all('Set-Cookie') or []
        for line in cookie_headers:
            for name, morsel in SimpleCookie(line).items():
                if morsel['max-age'] == '0':
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        status = response.status
        connection.close()
        return status, value, cookie_headers

    def test_qr_to_authenticated_http_only_session_and_courses(self):
        status, value, headers = self.request('/api/login/qr', {})
        self.assertEqual(status, 200)
        self.assertTrue(any('HttpOnly' in h and 'SameSite=Strict' in h for h in headers))
        self.assertNotIn('learning_session', self.cookies)
        self.assertEqual(self.request('/api/login/qr/status', {})[1]['status'], 'scanned')
        self.now += 3
        status, value, headers = self.request('/api/login/qr/status', {})
        self.assertEqual(value['status'], 'authenticated')
        self.assertIn('learning_session', self.cookies)
        self.assertNotIn('learning_qr', self.cookies)
        self.assertEqual(self.request('/api/session')[1]['courses'], value['courses'])
        self.assertEqual(self.request('/api/login/qr/status', {})[1]['status'], 'authenticated')
        self.assertNotIn(self.cookies['learning_session'], json.dumps(value))

    def test_prelogin_ownership_cross_origin_and_cookie_fallback(self):
        self.assertEqual(self.request('/api/login/qr', {}, origin='https://evil.example')[0], 403)
        self.assertEqual(self.manager.qr.attempts, {})
        self.assertEqual(self.request('/api/login/qr/status', {})[1]['status'], 'expired')
        self.request('/api/login/qr', {})
        old = self.cookies['learning_qr']
        self.assertEqual(self.request('/api/login', {'cookie': 'controlled-cookie'})[0], 200)
        self.assertNotIn(old, self.manager.qr.attempts)
        self.assertIn('learning_session', self.cookies)
        self.assertNotIn('learning_qr', self.cookies)

    def test_cancel_endpoint_clears_owner_and_no_authentication(self):
        self.request('/api/login/qr', {})
        self.assertEqual(self.request('/api/login/qr/cancel', {})[0], 200)
        self.assertEqual(self.cookies, {})
        self.assertEqual(self.manager.qr.attempts, {})
        self.assertEqual(self.request('/api/session')[0], 401)


if __name__ == '__main__':
    unittest.main()
