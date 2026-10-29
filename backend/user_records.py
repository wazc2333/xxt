"""Persistent login metadata. Authentication material never enters this store."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

STATES = {'active', 'logged_out', 'expired', 'interrupted', 'cancelled'}


def identity(values):
    values = values if isinstance(values, dict) else {}
    uid = str(values.get('uid') or '')
    if not uid.isascii() or not uid.isdigit() or len(uid) > 32:
        uid = ''
    name = str(values.get('displayName') or '')
    name = ''.join(ch for ch in name if ch.isprintable()).strip()[:100]
    return {'uid': uid, 'displayName': name or ('超星用户 ' + uid if uid else '未获取姓名')}


class UserRecords:
    def __init__(self, path, now):
        self.path = Path(path)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS logins (
                id TEXT PRIMARY KEY, uid TEXT NOT NULL, display_name TEXT NOT NULL,
                login_method TEXT NOT NULL, login_at INTEGER NOT NULL,
                last_active_at INTEGER NOT NULL, ended_at INTEGER, status TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS logins_time ON logins(login_at DESC, id)')
            db.execute("UPDATE logins SET status='interrupted', ended_at=? WHERE status='active'", (now,))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self, record_id, profile, method, now):
        profile = identity(profile)
        with self.connect() as db:
            db.execute('INSERT INTO logins VALUES (?,?,?,?,?,?,?,?)',
                       (record_id, profile['uid'], profile['displayName'], method, now, now, None, 'active'))

    def touch(self, record_id, now):
        with self.connect() as db:
            db.execute("UPDATE logins SET last_active_at=? WHERE id=? AND status='active'", (now, record_id))

    def resume(self, record_id):
        with self.connect() as db:
            return db.execute("UPDATE logins SET status='active',ended_at=NULL WHERE id=? AND status='interrupted'",
                              (record_id,)).rowcount == 1

    def finish(self, record_id, status, now, last_active):
        if status not in STATES - {'active'}:
            raise ValueError('Unknown login end state')
        with self.connect() as db:
            db.execute("UPDATE logins SET status=?,ended_at=?,last_active_at=? WHERE id=? AND status='active'",
                       (status, now, last_active, record_id))

    def list(self, *, search='', status='', method='', page=1, page_size=25):
        conditions, args = [], []
        if search:
            pattern = '%' + search.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            conditions.append("(uid LIKE ? ESCAPE '!' OR display_name LIKE ? ESCAPE '!')")
            args.extend((pattern, pattern))
        if status:
            conditions.append('status=?')
            args.append(status)
        if method:
            conditions.append('login_method=?')
            args.append(method)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM logins' + where, args).fetchone()[0]
            rows = db.execute('SELECT * FROM logins' + where + ' ORDER BY login_at DESC, id LIMIT ? OFFSET ?',
                              [*args, page_size, (page - 1) * page_size]).fetchall()
            stats = db.execute('''SELECT COUNT(*) AS records,
                COUNT(DISTINCT CASE WHEN uid<>'' THEN 'uid:'||uid ELSE 'session:'||id END) AS accounts
                FROM logins''').fetchone()
        users = [{'id': r['id'], 'uid': r['uid'], 'displayName': r['display_name'], 'loginMethod': r['login_method'],
                  'loginAt': r['login_at'], 'lastActiveAt': r['last_active_at'], 'endedAt': r['ended_at'],
                  'status': r['status']} for r in rows]
        return {'users': users, 'total': total, 'page': page, 'pageSize': page_size,
                'stats': {'loginRecords': stats['records'], 'knownUsers': stats['accounts']}}

    def account_records(self, record_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM logins WHERE id=?', (record_id,)).fetchone()
            if row is None:
                return None
            rows = db.execute('SELECT id FROM logins WHERE uid=?' if row['uid'] else
                              'SELECT id FROM logins WHERE id=?', (row['uid'] or record_id,)).fetchall()
            return {'id':row['id'], 'uid':row['uid'], 'displayName':row['display_name'],
                    'recordIds':[r['id'] for r in rows]}

    def delete_records(self, record_ids):
        with self.connect() as db:
            db.executemany('DELETE FROM logins WHERE id=?', [(value,) for value in record_ids])
