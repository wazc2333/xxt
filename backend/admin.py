"""Independent administrator authentication and local CLI provisioning."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from .service import ServiceError

ITERATIONS = 600_000


def provision(directory, *, username='admin', password=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'admin-auth.json'
    if target.exists():
        raise ValueError('管理员已配置，未覆盖现有账号')
    if not isinstance(username, str) or not 1 <= len(username.strip()) <= 64:
        raise ValueError('管理员用户名无效')
    generated = password is None
    if generated and (directory / 'admin-initial-password.txt').exists():
        raise ValueError('本机初始密码文件已存在，未覆盖文件；请改用交互方式初始化')
    password = secrets.token_urlsafe(24) if generated else password
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('管理员密码需要 12 至 256 个字符')
    salt = secrets.token_hex(16)
    config = {'username': username.strip(), 'salt': salt, 'iterations': ITERATIONS,
              'hash': hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()}
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(config, stream)
    if generated:
        initial = directory / 'admin-initial-password.txt'
        descriptor = os.open(initial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write('username: ' + config['username'] + '\npassword: ' + password + '\n')
        return initial
    return target


class AdminAuth:
    def __init__(self, path, *, clock=time.monotonic, ttl=28800, idle=1800):
        self.path, self.clock, self.ttl, self.idle = Path(path), clock, ttl, idle
        self.lock, self.sessions, self.attempts = threading.RLock(), {}, {}
        self.capacity = threading.BoundedSemaphore(2)
        self.config = None
        if self.path.exists():
            try:
                config = json.loads(self.path.read_text(encoding='utf-8'))
                if (not isinstance(config['username'], str) or not 1 <= len(config['username']) <= 64
                        or config['iterations'] != ITERATIONS or len(bytes.fromhex(config['salt'])) != 16
                        or len(bytes.fromhex(config['hash'])) != 32):
                    raise ValueError('Invalid administrator configuration')
                self.config = config
            except (ValueError, KeyError, TypeError):
                raise ValueError('管理员配置损坏，请检查本地 admin-auth.json；未开放管理员接口') from None

    def reap(self):
        now = self.clock()
        with self.lock:
            self.sessions = {k: s for k, s in self.sessions.items()
                             if now < s['expires'] and now - s['touched'] <= self.idle}

    def login(self, username, password, address):
        if self.config is None:
            raise ServiceError('管理员尚未配置，请部署者运行 python -m backend.admin init', 503)
        now = self.clock()
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if now - v[0] < 60}
            start, count = self.attempts.get(address, (now, 0))
            if count >= 5:
                raise ServiceError('管理员登录尝试过于频繁，请稍后重试', 429)
            self.attempts[address] = (start, count + 1)
        if (not isinstance(username, str) or not isinstance(password, str)
                or len(username) > 64 or not 1 <= len(password) <= 256):
            raise ServiceError('管理员账号或密码错误', 401)
        if not self.capacity.acquire(blocking=False):
            raise ServiceError('管理员登录繁忙，请稍后重试', 429)
        try:
            actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(self.config['salt']), ITERATIONS)
            valid = hmac.compare_digest(actual, bytes.fromhex(self.config['hash']))
            valid = hmac.compare_digest(username.encode(), self.config['username'].encode()) and valid
        finally:
            self.capacity.release()
        if not valid:
            raise ServiceError('管理员账号或密码错误', 401)
        self.reap()
        with self.lock:
            if len(self.sessions) >= 32:
                raise ServiceError('管理员会话已满，请稍后重试', 429)
            token = secrets.token_urlsafe(32)
            session = {'username': self.config['username'], 'csrf': secrets.token_urlsafe(32),
                       'expires': now + self.ttl, 'touched': now}
            self.sessions[token] = session
            return token, self.public(session)

    @staticmethod
    def public(session):
        return {'authenticated': True, 'username': session['username'], 'csrf': session['csrf']}

    def session(self, token):
        self.reap()
        with self.lock:
            session = self.sessions.get(token)
            if not session:
                raise ServiceError('请先登录管理员账号', 401)
            session['touched'] = self.clock()
            return self.public(session)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)


def main():
    parser = argparse.ArgumentParser(description='本地初始化管理员账号；不通过网页开放注册')
    parser.add_argument('command', choices=['init'])
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--username', default='admin')
    parser.add_argument('--generate', action='store_true', help='生成随机密码并仅写入本机初始密码文件')
    args = parser.parse_args()
    password = None
    if not args.generate:
        password = getpass.getpass('管理员密码（至少 12 个字符）：')
        if password != getpass.getpass('再次输入管理员密码：'):
            parser.error('两次密码不一致')
    try:
        path = provision(args.data_dir, username=args.username, password=password)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(json.dumps({'status': 'configured', 'username': args.username, 'localFile': str(path.resolve())}))


if __name__ == '__main__':
    main()
