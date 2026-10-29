"""Strict serializer for the observed PC chapter-work form, without networking."""
from __future__ import annotations
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: object = field(default=None, repr=False)

    def walk(self):
        yield self
        for item in self.children:
            if isinstance(item, Node):
                yield from item.walk()

    def text(self):
        if self.tag in ("script", "style"):
            return ""
        return " ".join(item.text() if isinstance(item, Node) else item for item in self.children)

    def has_class(self, value):
        return value in self.attrs.get("class", "").split()


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class QuestionParseError(ValueError):
    pass


class TaskForm:
    def __init__(self, html: str, page_url: str, *, allow_chapter_form=False, allow_short_answer=False):
        self.document = Document(html)
        self.font_decoding = None
        self.page_url = page_url
        self.allow_short_answer = allow_short_answer
        accepted = {'submitForm', 'form1'} if allow_chapter_form else {'submitForm'}
        forms = [n for n in self.document.root.walk() if n.tag == 'form' and n.attrs.get('id') in accepted]
        if len(forms) != 1:
            raise ValueError("No unique supported work form: login, verification, completed task, or a changed page")
        self.form = forms[0]
        self.chapter_template = self.form.attrs.get('id') == 'form1'
        self.action = urllib.parse.urljoin(page_url, self.form.attrs.get("action", ""))
        parsed = urllib.parse.urlsplit(self.action)
        if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".chaoxing.com")
                or parsed.path != "/mooc-ans/work/addStudentWorkNewWeb" or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            raise ValueError("Unexpected homework form destination")
        if self.form.attrs.get("method", "get").lower() != "post":
            raise ValueError("Unexpected form method")
        self.fields = self.successful_controls()
        self.question_ids = [name[len("answertype"):] for name, value in self.fields if name.startswith("answertype")]
        if not self.question_ids:
            raise ValueError("No question type controls")
        if (len(self.question_ids) > 2000 or len(set(self.question_ids)) != len(self.question_ids)
                or any(not q.isdigit() for q in self.question_ids)):
            raise ValueError("Invalid question identities")
        self.options = {}
        for node in self.form.walk():
            if not self._question_block(node):
                continue
            kinds = [n for n in node.walk() if n.attrs.get("name", "").startswith("answertype")]
            if len(kinds) != 1:
                raise ValueError("Unsupported nested question structure")
            qid = kinds[0].attrs["name"][len("answertype"):]
            labels = [n.attrs.get("data", "") for n in node.walk() if n.has_class("num_option") or
                      (kinds[0].attrs.get('value') == '1' and n.has_class('num_option_dx'))]
            if qid in self.options or len(set(labels)) != len(labels):
                raise ValueError("Ambiguous question options")
            self.options[qid] = labels
        if set(self.options) != set(self.question_ids):
            raise ValueError('Question blocks do not match answer type controls')

    def _question_block(self, node):
        return node.has_class('questionLi') or (self.chapter_template and node.has_class('TiMu'))

    def successful_controls(self):
        fields = []
        for n in self.form.walk():
            a = n.attrs
            if not a.get("name") or "disabled" in a:
                continue
            parent = n.parent
            if any(p.tag == "fieldset" and "disabled" in p.attrs for p in self._ancestors(parent)):
                raise ValueError("Disabled fieldset requires browser serialization")
            if n.tag == "input":
                typ = a.get("type", "text").lower()
                if typ in ("button", "submit", "reset", "file", "image"):
                    continue
                if typ in ("radio", "checkbox") and "checked" not in a:
                    continue
                fields.append((a["name"], a.get("value", "on" if typ in ("radio", "checkbox") else "")))
            elif n.tag == "textarea":
                fields.append((a["name"], "".join(x for x in n.children if isinstance(x, str))))
            elif n.tag == "select":
                raise ValueError("Named selects require an explicit supported serializer")
        return fields

    @staticmethod
    def _ancestors(node):
        while node is not None:
            yield node
            node = node.parent

    def questions(self):
        result = []
        for node in self.document.root.walk():
            if not self._question_block(node):
                continue
            type_node = next((n for n in node.walk() if n.attrs.get("name", "").startswith("answertype")), None)
            if type_node is not None:
                result.append({"id": type_node.attrs["name"][len("answertype"):],
                    "type": type_node.attrs.get("value"),
                    "text": re.sub(r"\s+", " ", node.text()).strip()})
        return result

    def answer_questions(self):
        """Only question text and actual choices; excludes account and form credentials."""
        encrypted = (any(n.has_class('font-cxsecret') for n in self.form.walk()) or
                     any(n.has_class('font-cxsecret') for n in self._ancestors(self.form.parent)))
        if self.font_decoding is None and encrypted:
            from .font_decoder import decode_document
            self.font_decoding = decode_document(self.document,self.form)
        result = {}
        for node in self.form.walk():
            if not self._question_block(node):
                continue
            number = len(result)+1
            control = next(n for n in node.walk() if n.attrs.get('name', '').startswith('answertype'))
            qid, kind = control.attrs['name'][len('answertype'):], control.attrs.get('value')
            stem = next((n for n in node.walk() if n.has_class('mark_name') or n.has_class('Zy_TItle')), None)
            if stem is None:
                raise QuestionParseError(f'第 {number} 题未找到可识别的题干控件')
            # Exclude observed type/score metadata, while preserving rich-text spans in the stem.
            pieces = []
            for child in stem.children:
                if isinstance(child, str):
                    pieces.append(child)
                    continue
                value = child.text().strip()
                if stem.has_class('Zy_TItle') and child.tag == 'i' and value.isdigit():
                    continue
                if child.has_class('colorShallow') or (child.tag == 'span' and
                        re.match(r'^[（(](?:单选题|多选题|判断题|简答题)', value)):
                    continue
                pieces.append(value)
            text = ' '.join(pieces)
            text = re.sub(r'^\s*\d+\s*[.、．](?:\s+|(?=[^\d]))', '', re.sub(r'\s+', ' ', text)).strip()
            text = re.sub(r'^【(?:单选题|多选题|判断题|简答题)】\s*', '', text)
            options = []
            for label in self.options[qid]:
                marker = next(n for n in node.walk() if n.attrs.get('data') == label and
                              (n.has_class('num_option') or n.has_class('num_option_dx')))
                container = marker.parent
                answer_node = next((n for n in container.walk() if n.has_class('answer_p')), None)
                if answer_node is None and container.tag == 'label' and container.parent.tag == 'li':
                    siblings = [n for n in container.parent.children if isinstance(n, Node) and n.has_class('after')]
                    if len(siblings) == 1:
                        answer_node = siblings[0]
                option_text = answer_node.text().strip() if answer_node else container.text().strip()
                if not answer_node:
                    option_text = re.sub(r'^' + re.escape(label) + r'[.、．\s]*', '', option_text).strip()
                options.append({'label': label, 'text': option_text})
            if not text or any(not o['text'] for o in options):
                raise QuestionParseError(f'第 {number} 题的题干或选项正文为空，未提交')
            if kind not in ('0', '1', '3', '4') or (kind == '4' and not self.allow_short_answer):
                raise QuestionParseError(f'第 {number} 题的题型暂不支持，未提交')
            if kind != '4' and not options:
                raise QuestionParseError(f'第 {number} 题缺少可识别选项，未提交')
            if any(n.tag in ('img', 'iframe', 'audio', 'video', 'math') for n in node.walk()):
                raise QuestionParseError(f'第 {number} 题含图片、公式或音视频，需要在原网页处理')
            result[qid] = {'id': qid, 'type': kind, 'text': text, 'options': options}
        return [result[qid] for qid in self.question_ids]

    def prepare(self, answers: dict[str, str], *, mode: str = "submit") -> urllib.request.Request:
        if mode not in ("save", "submit"):
            raise ValueError("Expected save or submit")
        if mode == 'save' and self.chapter_template:
            raise ValueError('This chapter template uses a different save endpoint; draft save is not supported')
        if set(answers) != set(self.question_ids):
            raise ValueError("Provide exactly the question IDs in this page")
        types = {name[len("answertype"):]: value for name, value in self.fields if name.startswith("answertype")}
        normalized = {}
        for qid, answer in answers.items():
            if not self.valid_answer(qid, answer):
                raise ValueError("Unsupported question or answer absent from actual options")
            # The observed page sorts multiple choice option labels.
            normalized[qid] = "".join(sorted(set(answer))) if types[qid] == "1" else answer
        replacements = {"answer" + qid: answer for qid, answer in normalized.items()}
        replacements.update(pyFlag="1" if mode == "save" else "",
                            answerwqbid=",".join(self.question_ids) + ",")
        fields = [(name, replacements.get(name, value)) for name, value in self.fields]
        if not {"pyFlag", "answerwqbid"}.issubset(dict(fields)):
            raise ValueError("Missing required observed control names")
        p = urllib.parse.urlsplit(self.action)
        query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
                 if k not in ("pyFlag", "ua", "formType", "saveStatus")]
        if mode == "save":
            query.append(("pyFlag", "1"))
        query.extend((("ua", "pc"), ("formType", "post"), ("saveStatus", "1")))
        if self.chapter_template and not any(k == 'pos' for k, _ in query):
            # The observed page catches unavailable getEnc() and sends an empty pos.
            query.append(('pos', ''))
        existing = next((v for k, v in query if k == "version"), None)
        if existing is not None:
            query = [(k, str(int(v) + 1) if k == "version" else v) for k, v in query]
        else:
            query.append(("version", "1"))
        url = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(query), ""))
        body = urllib.parse.urlencode([(k, re.sub(r"\r?\n", "\r\n", v)) for k, v in fields]).encode("utf-8")
        return urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest", "Referer": self.page_url,
            "Origin": urllib.parse.urlunsplit((p.scheme, p.netloc, "", "", "")),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/156.0.0.0 Safari/537.36"})

    def valid_answer(self, qid, answer):
        types = dict(self.fields)
        kind = types.get("answertype" + qid)
        labels = self.options.get(qid, [])
        if kind == '4' and self.allow_short_answer:
            return isinstance(answer, str) and 0 < len(answer.strip()) <= 16000 and '\x00' not in answer
        if not isinstance(answer, str) or not labels:
            return False
        if kind == "3":
            return set(labels) == {"true", "false"} and answer in labels
        if kind not in ("0", "1") or any(not re.fullmatch(r"[A-Z]", label) for label in labels):
            return False
        return bool(answer) and set(answer) <= set(labels) and (kind == "1" or len(answer) == 1)
