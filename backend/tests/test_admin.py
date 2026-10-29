import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.admin import AdminAuth, provision
from backend.platform import Platform
from backend.server import Server
from backend.service import Job, Manager, ServiceError
from backend.tests.test_service import FakePlatform
from backend.user_records import UserRecords, identity

PASSWORD = 'local-test-password-2468'


class ProfilePlatform(FakePlatform):
    def account_profile(self):
        return {'uid': '123456', 'displayName': '测试用户', 'cookie': 'never-store-this'}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [100.0]
        self.wall = [1800000000]
        self.manager = self.new_manager()

    def new_manager(self):
        return Manager(Path(self.temp.name), platform_factory=ProfilePlatform,
                       clock=lambda: self.now[0], wall_clock=lambda: self.wall[0], session_ttl=60)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_cookie_qr_logout_and_restart_retain_metadata_only(self):
        token, session, _ = self.manager.login('never-store-this')
        self.manager.login_platform(ProfilePlatform('inert'))
        self.assertEqual(self.manager.admin_users()['stats']['knownUsers'], 1)
        self.assertEqual(self.manager.admin_users()['stats']['activeSessions'], 2)
        self.assertNotEqual(session.record_id, token)
        self.manager.logout(token)
        self.manager.close()
        self.manager = self.new_manager()
        result = self.manager.admin_users()
        self.assertEqual(result['stats']['activeSessions'], 0)
        self.assertEqual({r['status'] for r in result['users']}, {'logged_out', 'interrupted'})
        self.assertEqual({r['loginMethod'] for r in result['users']}, {'cookie', 'qr'})
        self.assertTrue(all(r['endedAt'] for r in result['users']))
        with self.manager.user_records.connect() as db:
            raw = '\n'.join(db.iterdump())
        for secret in ('never-store-this', token, session.csrf):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, json.dumps(result))
        self.assertTrue(session.platform.closed)

    def test_admin_read_never_keeps_user_alive_and_persists_expiration(self):
        _, session, _ = self.manager.login('inert')
        self.now[0] += 59
        self.manager.admin_users()
        self.assertEqual(session.touched, 100)
        self.now[0] += 2
        row = self.manager.admin_users()['users'][0]
        self.assertEqual(row['status'], 'expired')
        self.assertIsNone(row['current'])
        self.assertTrue(session.platform.closed)

    def test_activity_and_job_snapshot_are_sanitized(self):
        token, session, _ = self.manager.login('inert')
        job = Job(list(session.platform.courses.values()))
        job.update(status='running', currentCourse='测试课程', signedUrl='secret-url', message='secret-message')
        session.jobs[job.id] = job
        self.now[0] += 16
        self.wall[0] += 16
        self.manager.session(token)
        row = self.manager.admin_users()['users'][0]
        self.assertEqual(row['lastActiveAt'], self.wall[0])
        self.assertEqual(row['current']['job']['status'], 'running')
        self.assertNotIn('secret-', json.dumps(row))
        with self.manager.user_records.connect() as db:
            self.assertEqual(db.execute('SELECT last_active_at FROM logins').fetchone()[0], self.wall[0])

    def test_filters_pagination_literal_search_and_invalid_queries(self):
        records = self.manager.user_records
        for number in range(26):
            records.start(str(number), {'uid': str(number+1), 'displayName': '特殊_%' if number == 0 else '甲'},
                          'cookie', self.wall[0]+number)
        first = records.list()
        self.assertEqual((first['total'], len(first['users'])), (26, 25))
        self.assertEqual(len(records.list(page=2)['users']), 1)
        self.assertEqual(records.list(search='_%')['total'], 1)
        self.assertEqual(records.list(search="' OR 1=1 --")['total'], 0)
        self.assertEqual(records.list(method='qr')['total'], 0)
        for arguments in ({'page': 0}, {'status': 'bad'}, {'method': 'bad'}, {'search': 'x'*101}):
            with self.subTest(arguments=arguments), self.assertRaises(ServiceError):
                self.manager.admin_users(**arguments)

    def test_startup_recovers_abandoned_active_records(self):
        self.manager.user_records.start('orphan', {}, 'cookie', self.wall[0])
        other = UserRecords(self.manager.user_records.path, self.wall[0]+5)
        row = other.list()['users'][0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['endedAt'], self.wall[0]+5)

    def test_failed_login_is_not_recorded(self):
        with self.assertRaises(ValueError):
            self.manager.login('invalid')
        self.assertEqual(self.manager.admin_users()['total'], 0)

    def test_logout_clears_credentials_even_when_storage_fails(self):
        token, session, _ = self.manager.login('inert')
        with patch.object(self.manager.user_records, 'finish', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.manager.logout(token)
        self.assertTrue(session.closed)
        self.assertTrue(session.platform.closed)
        self.assertNotIn(token, self.manager.sessions)

    def test_identity_and_profile_parsing(self):
        self.assertEqual(identity({'uid': 'secret=abc'})['uid'], '')
        self.assertEqual(identity({'uid': '１２３'})['uid'], '')
        self.assertEqual(identity({'displayName': '甲\n乙'})['displayName'], '甲乙')
        platform = object.__new__(Platform)
        platform.jar = []
        platform.request = lambda *_: ('<p class="user-name">测试姓名</p>', '')
        self.assertEqual(platform.account_profile()['displayName'], '测试姓名')
        platform.profile_hint = {'uid': '123', 'displayName': '扫码昵称'}
        platform.request = lambda *_: self.fail('QR profile should not fetch another page')
        self.assertEqual(platform.account_profile()['uid'], '123')


class AdminAuthTests(unittest.TestCase):
    def test_initialization_hashing_no_overwrite_and_session_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            provision(folder, password=PASSWORD)
            self.assertNotIn(PASSWORD, (Path(folder)/'admin-auth.json').read_text())
            with self.assertRaises(ValueError):
                provision(folder, password='another-valid-password')
            clock = [0]
            auth = AdminAuth(Path(folder)/'admin-auth.json', clock=lambda: clock[0], idle=30, ttl=60)
            token, result = auth.login('admin', PASSWORD, 'loopback')
            self.assertNotEqual(token, result['csrf'])
            self.assertEqual(auth.session(token)['username'], 'admin')
            clock[0] = 31
            with self.assertRaises(ServiceError):
                auth.session(token)
            token, _ = auth.login('admin', PASSWORD, 'loopback')
            clock[0] = 50
            auth.session(token)
            clock[0] = 70
            auth.session(token)
            clock[0] = 92
            with self.assertRaises(ServiceError):
                auth.session(token)

    def test_generated_password_stays_in_local_file_and_missing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = AdminAuth(Path(folder)/'admin-auth.json')
            with self.assertRaises(ServiceError) as error:
                auth.login('admin', PASSWORD, 'test')
            self.assertEqual(error.exception.status, 503)
            path = provision(folder)
            password = path.read_text().split('password: ', 1)[1].strip()
            self.assertGreaterEqual(len(password), 24)
            self.assertNotIn(password, (Path(folder)/'admin-auth.json').read_text())
            self.assertTrue(AdminAuth(Path(folder)/'admin-auth.json').login('admin', password, 'test')[1]['authenticated'])

    def test_rate_limit_and_logout(self):
        with tempfile.TemporaryDirectory() as folder:
            provision(folder, password=PASSWORD)
            auth = AdminAuth(Path(folder)/'admin-auth.json')
            token, _ = auth.login('admin', PASSWORD, 'good')
            auth.logout(token)
            with self.assertRaises(ServiceError):
                auth.session(token)
            for _ in range(5):
                with self.assertRaises(ServiceError) as error:
                    auth.login('wrong', PASSWORD, 'bad')
                self.assertEqual(error.exception.status, 401)
            with self.assertRaises(ServiceError) as error:
                auth.login('admin', PASSWORD, 'bad')
            self.assertEqual(error.exception.status, 429)


class AdminHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        provision(self.temp.name, password=PASSWORD)
        self.manager = Manager(Path(self.temp.name), platform_factory=ProfilePlatform)
        self.server = Server(('127.0.0.1', 0), self.manager)
        self.port = self.server.server_address[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.temp.cleanup()

    def request(self, path, body=None, cookie='', csrf='', origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Origin': origin or self.origin, 'X-Requested-With': 'learning-web',
                   'Cookie': cookie, 'X-CSRF-Token': csrf}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn.request('GET' if body is None else 'POST', path,
                     body=None if body is None else json.dumps(body), headers=headers)
        response = conn.getresponse()
        raw = response.read()
        info = dict(response.getheaders())
        value = json.loads(raw) if 'application/json' in info.get('Content-Type', '') else raw.decode()
        conn.close()
        return response.status, value, info

    def admin_login(self):
        status, data, headers = self.request('/api/admin/login', {'username': 'admin', 'password': PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly; SameSite=Strict; Path=/api/admin', headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';', 1)[0], data['csrf']

    def test_isolation_authorization_history_and_logout(self):
        self.assertEqual(self.request('/api/admin/users')[0], 401)
        user, session, _ = self.manager.login('inert')
        self.assertEqual(self.request('/api/admin/users', cookie='learning_session='+user)[0], 401)
        cookie, csrf = self.admin_login()
        self.assertEqual(self.request('/api/session', cookie=cookie)[0], 401)
        data = self.request('/api/admin/users', cookie=cookie)[1]
        self.assertEqual(data['users'][0]['uid'], '123456')
        self.assertNotIn(user, json.dumps(data))
        self.assertNotIn(session.csrf, json.dumps(data))
        self.manager.logout(user)
        self.assertEqual(self.request('/api/admin/users?status=logged_out', cookie=cookie)[1]['total'], 1)
        self.assertEqual(self.request('/api/admin/logout', {}, cookie=cookie)[0], 403)
        self.assertEqual(self.request('/api/admin/logout', {}, cookie=cookie, csrf=csrf)[0], 200)
        self.assertEqual(self.request('/api/admin/users', cookie=cookie)[0], 401)

    def test_admin_files_validation_and_same_origin(self):
        self.assertEqual(self.request('/admin')[0], 200)
        for path in ('/data/admin-auth.json', '/admin-initial-password.txt', '/data/user-records.sqlite3'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/api/admin/login', {'username': 'admin', 'password': PASSWORD},
                                      origin='https://other.example')[0], 403)
        cookie, _ = self.admin_login()
        for query in ('page=0', 'page=bad', 'status=bad', 'method=bad', 'page=1&page=2', 'unknown=value'):
            self.assertEqual(self.request('/api/admin/users?'+query, cookie=cookie)[0], 400)
        self.assertEqual(self.request('/api/admin/users?search=%25', cookie=cookie)[0], 200)


if __name__ == '__main__':
    unittest.main()
