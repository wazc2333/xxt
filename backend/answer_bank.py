"""Per-login local choice-answer library; answers are matched by content, not question IDs."""
import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path


def normalized(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(text))).strip()


def fingerprint(question):
    texts = [normalized(o['text']) for o in question['options']]
    if len(texts) != len(set(texts)):
        raise ValueError('选项正文重复，无法可靠匹配题库')
    canonical = [question['type'], normalized(question['text']), sorted(texts)]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()


class AnswerBank:
    def __init__(self, path):
        self.path = Path(path)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS answers (
                owner TEXT, fingerprint TEXT, question TEXT, selected TEXT, source TEXT,
                PRIMARY KEY(owner,fingerprint))''')
            db.execute('CREATE TABLE IF NOT EXISTS revoked_owners (owner TEXT PRIMARY KEY)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def count(self, owner):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM answers WHERE owner=?', (owner,)).fetchone()[0]

    def get(self, owner, question):
        if question['type'] == '4':
            return None
        key = fingerprint(question)
        with self.connect() as db:
            row = db.execute('SELECT selected FROM answers WHERE owner=? AND fingerprint=?', (owner, key)).fetchone()
        if row is None:
            return None
        selected = json.loads(row[0])
        current = {normalized(o['text']): o['label'] for o in question['options']}
        try:
            labels = [current[text] for text in selected]
        except KeyError:
            return None
        return labels[0] if question['type'] in ('0', '3') else ''.join(sorted(labels))

    @staticmethod
    def prepare(question, answer, source):
        if question['type'] not in ('0', '1', '3'):
            raise ValueError('题库只接受单选、多选和判断题')
        labels = {o['label']: normalized(o['text']) for o in question['options']}
        picked = [answer] if question['type'] == '3' else list(answer)
        if not picked or any(v not in labels for v in picked) or len(set(picked)) != len(picked):
            raise ValueError('答案不在题目选项中')
        if question['type'] != '1' and len(picked) != 1:
            raise ValueError('单选或判断题只能有一个答案')
        public = {k: question[k] for k in ('type', 'text', 'options')}
        return fingerprint(question), json.dumps(public, ensure_ascii=False), json.dumps([labels[x] for x in picked]), source

    def put(self, owner, question, answer, source='deepseek'):
        row = self.prepare(question, answer, source)
        with self.connect() as db:
            db.execute('''INSERT OR REPLACE INTO answers SELECT ?,?,?,?,?
                       WHERE NOT EXISTS (SELECT 1 FROM revoked_owners WHERE owner=?)''', (owner, *row, owner))

    def import_questions(self, owner, questions):
        if not isinstance(questions, list) or not 1 <= len(questions) <= 1000:
            raise ValueError('导入文件需要 questions 数组，包含 1 至 1000 道选择题')
        rows = []
        for q in questions:
            if (not isinstance(q, dict) or not isinstance(q.get('text'), str) or not 0 < len(q['text']) <= 16000
                    or not isinstance(q.get('options'), list) or not 2 <= len(q['options']) <= 26
                    or any(not isinstance(o, dict) or not isinstance(o.get('text'), str) or not o['text']
                           or len(o['text']) > 16000 or not isinstance(o.get('label'), str) for o in q['options'])
                    or not isinstance(q.get('answer'), str)):
                raise ValueError('题库题目格式无效')
            labels = [o['label'] for o in q['options']]
            if len(labels) != len(set(labels)) or any(not re.fullmatch(r'[A-Z]|true|false', v) for v in labels):
                raise ValueError('题库选项标签无效')
            if q.get('type') not in ('0', '1', '3'):
                raise ValueError('题库只接受选择题')
            if q['type'] == '3' and set(labels) != {'true', 'false'}:
                raise ValueError('判断题需要 true/false 两个实际选项')
            if q['type'] in ('0', '1') and any(not re.fullmatch(r'[A-Z]', v) for v in labels):
                raise ValueError('选择题需要字母选项标签')
            rows.append((owner, *self.prepare(q, q['answer'], 'imported')))
        with self.connect() as db:
            if db.execute('SELECT 1 FROM revoked_owners WHERE owner=?',(owner,)).fetchone():
                raise ValueError('用户已删除，不能写入题库')
            db.executemany('INSERT OR REPLACE INTO answers VALUES (?,?,?,?,?)', rows)
        return len(rows)

    def delete_owners(self, owners):
        values = [(owner,) for owner in owners]
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO revoked_owners VALUES (?)',values)
            db.executemany('DELETE FROM answers WHERE owner=?',values)

    def export(self, owner):
        with self.connect() as db:
            rows = db.execute('SELECT question,selected,source FROM answers WHERE owner=?', (owner,)).fetchall()
        questions = []
        for payload, selected, source in rows:
            q = json.loads(payload)
            wanted = set(json.loads(selected))
            labels = [o['label'] for o in q['options'] if normalized(o['text']) in wanted]
            q.update(answer=labels[0] if q['type'] in ('0', '3') else ''.join(sorted(labels)), source=source)
            questions.append(q)
        return {'questions': questions}
