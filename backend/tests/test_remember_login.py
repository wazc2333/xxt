import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from backend.credentials import SessionStore
from backend.platform import Platform
from backend.server import Server
from backend.service import Manager, ServiceError

SECRET = 'upstream-private-cookie-for-local-testing'
COOKIE = '_uid=123456; auth_token=' + SECRET


class RememberedPlatform(Platform):
    reads = 0

    def list_courses(self):
        type(self).reads += 1
        self.courses = {'demo': {'id': 'demo', 'name': '本地测试课程', 'url': 'https://mooc1.chaoxing.com/?enc=test-private'}}
        return [{'id': 'demo', 'name': '本地测试课程'}]

    def account_profile(self):
        return {'uid': '123456', 'displayName': '测试用户'}


class RememberLoginTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.wall, self.clock = [1800000000], [100.0]
        self.manager = self.create()

    def create(self):
        return Manager(Path(self.folder.name), platform_factory=RememberedPlatform,
                       clock=lambda: self.clock[0], wall_clock=lambda: self.wall[0])

    def restart(self):
        self.manager.close()
        self.manager = self.create()

    def tearDown(self):
        self.manager.close()
        self.folder.cleanup()

    def test_clean_restart_restores_same_login_history_and_cached_courses_without_network(self):
        token, before, _ = self.manager.login(COOKIE)
        self.clock[0] += 20
        self.wall[0] += 20
        self.manager.session(token)
        reads = RememberedPlatform.reads
        self.restart()
        restored = self.manager.session(token)
        self.assertEqual((restored.record_id, restored.csrf), (before.record_id, before.csrf))
        self.assertEqual(restored.platform.jar._cookies['.chaoxing.com']['/']['auth_token'].value, SECRET)
        self.assertEqual(self.manager.session_snapshot(restored)['courses'][0]['id'], 'demo')
        self.assertEqual(RememberedPlatform.reads, reads)
        self.assertEqual(restored.jobs, {})
        self.assertEqual(restored.catalogs, {})
        result = self.manager.admin_users()
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['users'][0]['status'], 'active')
        self.assertIsNone(result['users'][0]['endedAt'])
        self.assertEqual(result['stats']['activeSessions'], 1)

    def test_abrupt_stop_recovers_last_committed_credentials(self):
        token, session, _ = self.manager.login(COOKIE)
        # Simulate a killed process: no shutdown save or user logout.
        self.manager.sessions.clear()
        session.platform.close()
        self.restart()
        self.assertEqual(self.manager.session(token).profile['uid'], '123456')

    def test_logout_removes_saved_login_but_keeps_history(self):
        token, _, _ = self.manager.login(COOKIE)
        self.manager.logout(token)
        self.assertEqual(list(self.manager.session_store.load()), [])
        self.restart()
        with self.assertRaises(ServiceError):
            self.manager.session(token)
        self.assertEqual(self.manager.admin_users()['users'][0]['status'], 'logged_out')

    def test_idle_expiration_is_enforced_across_restart(self):
        token, _, _ = self.manager.login(COOKIE)
        self.manager.close()
        self.wall[0] += self.manager.session_ttl + 1
        self.clock[0] += self.manager.session_ttl + 1
        self.manager = self.create()
        with self.assertRaises(ServiceError):
            self.manager.session(token)
        self.assertEqual(list(self.manager.session_store.load()), [])
        self.assertEqual(self.manager.admin_users()['users'][0]['status'], 'expired')

    def test_active_touch_is_persisted_and_not_extended_by_restart(self):
        token, _, _ = self.manager.login(COOKIE)
        self.wall[0] += 100
        self.clock[0] += 100
        self.manager.session(token)
        self.manager.close()
        self.wall[0] += 200
        self.clock[0] += 200
        self.manager = self.create()
        self.assertEqual(self.clock[0]-self.manager.sessions[token].touched, 200)

    def test_qr_adopted_jar_is_also_remembered(self):
        token, _, _ = self.manager.login_platform(RememberedPlatform(COOKIE))
        self.restart()
        self.assertEqual(self.manager.session(token).login_method, 'qr')
        self.assertEqual(self.manager.admin_users()['total'], 1)

    def test_deepseek_key_is_encrypted_restored_and_removed_on_logout(self):
        key = 'sk-only-a-synthetic-persistence-test'
        token, session, _ = self.manager.login(COOKIE)
        self.manager.homework_settings(session, key)
        for path in Path(self.folder.name).iterdir():
            if path.is_file():
                self.assertNotIn(key.encode(), path.read_bytes())
        self.restart()
        restored = self.manager.session(token)
        self.assertEqual(restored.deepseek_key, key)
        self.assertNotIn(key, json.dumps(self.manager.session_snapshot(restored)))
        self.assertNotIn(key, json.dumps(self.manager.admin_users()))
        self.manager.logout(token)
        self.assertEqual(restored.deepseek_key, '')
        self.assertEqual(list(self.manager.session_store.load()), [])

    def test_stored_credentials_are_encrypted_and_history_stays_metadata_only(self):
        token, session, _ = self.manager.login(COOKIE)
        for path in Path(self.folder.name).iterdir():
            if path.is_file():
                raw = path.read_bytes()
                for secret in (SECRET, token, session.csrf, 'test-private'):
                    self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(SECRET, json.dumps(self.manager.admin_users()))
        self.assertNotIn(SECRET, json.dumps(self.manager.session_snapshot(session)))

    def test_tampered_ciphertext_cannot_restore_a_login(self):
        token, session, _ = self.manager.login(COOKIE)
        self.manager.sessions.clear()
        session.platform.close()
        with self.manager.session_store.connect() as db:
            db.execute('UPDATE sessions SET payload=?', (b'invalid-authenticated-encryption',))
        self.restart()
        self.assertEqual(self.manager.sessions, {})
        self.assertEqual(list(self.manager.session_store.load()), [])
        self.assertEqual(self.manager.admin_users()['users'][0]['status'], 'interrupted')

    def test_missing_key_never_generates_a_replacement_for_existing_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(folder)
            key = Path(folder)/'session-encryption.key'
            key.unlink()
            with self.assertRaises(ValueError):
                SessionStore(folder)
            self.assertFalse(key.exists())

    def test_platform_cookie_roundtrip_preserves_domain_path_flags_and_expiry(self):
        platform = Platform(COOKIE)
        cookie = next(iter(platform.jar))
        cookie.domain, cookie.domain_specified, cookie.domain_initial_dot = 'i.chaoxing.com', False, False
        cookie.path, cookie.secure, cookie.expires, cookie.discard = '/base', True, 2000000000, False
        cookie.set_nonstandard_attr('HttpOnly', None)
        with patch.object(Platform, 'request', side_effect=AssertionError('must not call upstream')):
            restored = Platform.restore_login(platform.export_login())
        value = next(c for c in restored.jar if c.name == cookie.name)
        self.assertEqual((value.domain, value.domain_specified, value.path, value.secure, value.expires, value.discard),
                         ('i.chaoxing.com', False, '/base', True, 2000000000, False))
        self.assertTrue(value.has_nonstandard_attr('HttpOnly'))
        platform.close()
        restored.close()


class PersistentBrowserTests(unittest.TestCase):
    def test_browser_close_reopen_backend_restart_and_logout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = Manager(root/'data', platform_factory=RememberedPlatform)
            server = Server(('127.0.0.1', 0), manager)
            port = server.server_address[1]
            origin = f'http://127.0.0.1:{port}'
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            jar_path = root/'test-browser-cookies.txt'
            jar = http.cookiejar.LWPCookieJar(jar_path)
            def opener(cookies):
                return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
            def request(client, path, body=None, csrf=''):
                headers = {'Origin': origin, 'X-Requested-With': 'learning-web', 'X-CSRF-Token': csrf,
                           'Content-Type': 'application/json'}
                with client.open(urllib.request.Request(origin+path, headers=headers,
                                 data=None if body is None else json.dumps(body).encode()), timeout=5) as response:
                    return json.load(response), response.headers
            try:
                first, headers = request(opener(jar), '/api/login', {'cookie': COOKIE})
                cookie = next(c for c in jar if c.name == 'learning_session')
                self.assertFalse(cookie.discard)
                self.assertGreater(cookie.expires, 0)
                self.assertTrue(cookie.has_nonstandard_attr('HttpOnly'))
                self.assertIn('Max-Age=2592000', headers.get_all('Set-Cookie')[-1])
                jar.save()  # Excludes transient cookies, as a closed browser would.
                restored_jar = http.cookiejar.LWPCookieJar(jar_path)
                restored_jar.load()
                client = opener(restored_jar)
                self.assertEqual(request(client, '/api/session')[0]['csrf'], first['csrf'])
                server.shutdown()
                server.server_close()
                manager.close()
                manager = Manager(root/'data', platform_factory=RememberedPlatform)
                server = Server(('127.0.0.1', port), manager)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                result, headers = request(client, '/api/session')
                self.assertTrue(result['authenticated'])
                self.assertIn('Max-Age=2592000', headers['Set-Cookie'])
                request(client, '/api/logout', {}, result['csrf'])
                self.assertFalse(any(c.name == 'learning_session' for c in restored_jar))
                self.assertEqual(list(manager.session_store.load()), [])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request(client, '/api/session')
                self.assertEqual(error.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                manager.close()


if __name__ == '__main__':
    unittest.main()
