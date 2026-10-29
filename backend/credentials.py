"""Authenticated encryption for remembered user sessions, separate from login history."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SessionStore:
    def __init__(self, directory):
        directory = Path(directory)
        self.path = directory / 'saved-sessions.sqlite3'
        key_path = directory / 'session-encryption.key'
        if not key_path.exists():
            if self.path.exists():
                raise ValueError('本地登录密钥缺失，请恢复 session-encryption.key；未尝试读取已保存凭证')
            descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        try:
            self.cipher = Fernet(key_path.read_bytes())
        except (TypeError, ValueError):
            raise ValueError('本地登录密钥无效，请恢复原 session-encryption.key') from None
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, payload BLOB NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def save(self, token, payload):
        payload = {'version': 1, 'token': token, **payload}
        encrypted = self.cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode())
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (self.key(token), encrypted))

    def delete(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE id=?', (self.key(token),))

    def delete_records(self, record_ids):
        wanted = set(record_ids)
        with self.connect() as db:
            keys = []
            for key, encrypted in db.execute('SELECT id,payload FROM sessions').fetchall():
                try:
                    payload = json.loads(self.cipher.decrypt(encrypted))
                    if payload.get('recordId') in wanted:
                        keys.append((key,))
                except (InvalidToken, ValueError, TypeError, AttributeError):
                    continue
            db.executemany('DELETE FROM sessions WHERE id=?', keys)

    def load(self):
        with self.connect() as db:
            rows = db.execute('SELECT id,payload FROM sessions').fetchall()
        for key, encrypted in rows:
            try:
                payload = json.loads(self.cipher.decrypt(encrypted))
                if (payload.get('version') != 1 or not isinstance(payload.get('token'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{43}', payload['token'])
                        or self.key(payload['token']) != key
                        or not re.fullmatch(r'[0-9a-f]{32}', payload.get('recordId', ''))
                        or not re.fullmatch(r'[A-Za-z0-9_-]{43}', payload.get('csrf', ''))
                        or payload.get('loginMethod') not in ('cookie', 'qr')
                        or not isinstance(payload.get('lastActiveAt'), int)
                        or not isinstance(payload.get('loginAt'), int)
                        or not isinstance(payload.get('platform'), dict)):
                    raise ValueError('Invalid saved session')
            except (InvalidToken, ValueError, TypeError, AttributeError):
                # A damaged or unauthenticated blob cannot create a session.
                with self.connect() as db:
                    db.execute('DELETE FROM sessions WHERE id=?', (key,))
                continue
            yield payload
