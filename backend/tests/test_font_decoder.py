"""Synthetic glyphs verify decoding without embedding user pages or fonts."""
import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from backend.answer_bank import AnswerBank
from backend.deepseek import AnswerError
from backend.font_decoder import FontDecodeError, decode_document, font_mapping, reference_glyphs
from backend.homework import resolve_answers
from backend.work_form import TaskForm
from backend.tests.test_quiz_answers import readable_form


def fixture():
    builder = FontBuilder(1000, isTTF=True)
    order = ['.notdef','encoded1','encoded2']
    builder.setupGlyphOrder(order)
    glyphs, reference = {}, {}
    for index,name in enumerate(order):
        pen = TTGlyphPen(None)
        pen.moveTo((100,100)); pen.lineTo((200+index*50,100)); pen.lineTo((180,500)); pen.closePath()
        glyph = pen.glyph()
        glyphs[name] = glyph
        points = ''.join(f'{x}{y}{flag & 1}' for (x,y),flag in zip(glyph.coordinates,glyph.flags))
        if index:
            reference[hashlib.md5(points.encode()).hexdigest()] = '二' if index==1 else '三'
    builder.setupCharacterMap({0xe001:'encoded1',0xe002:'encoded2'})
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name:(1000,0) for name in order})
    builder.setupHorizontalHeader(ascent=800,descent=-200)
    builder.setupNameTable({'familyName':'Synthetic Decoder Test','styleName':'Regular',
                            'uniqueFontIdentifier':'SyntheticDecoderTest','fullName':'Synthetic Decoder Test',
                            'psName':'SyntheticDecoderTest'})
    builder.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=200)
    builder.setupPost()
    buffer=io.BytesIO();builder.save(buffer)
    raw=buffer.getvalue()
    style="<style>@font-face{font-family:'font-cxsecret';src:url('data:application/font-ttf;charset=utf-8;base64,"+base64.b64encode(raw).decode()+"')}</style>"
    body=readable_form(short=False).replace('一加一是多少？','一加一等于\ue001？').replace('>二<','>\ue001<').replace('>三<','>\ue002<')
    body=body.replace('class="questionLi"','class="questionLi font-cxsecret"')
    return raw,reference,style+body


class FontTests(unittest.TestCase):
    def setUp(self):
        font_mapping.cache_clear()
        self.raw,self.reference,self.html=fixture()

    def tearDown(self):
        font_mapping.cache_clear()

    def form(self,html=None):
        return TaskForm(self.html if html is None else html,
                        'https://mooc1.chaoxing.com/mooc-ans/work/dowork',allow_chapter_form=True)

    def test_decode_stem_options_once_and_preserve_fields_and_option_values(self):
        form=self.form()
        before=list(form.fields)
        with patch('backend.font_decoder.reference_glyphs',return_value=self.reference):
            questions=form.answer_questions()
            self.assertEqual(form.answer_questions(),questions)
        self.assertEqual(questions[0]['text'],'一加一等于二？')
        self.assertEqual(questions[0]['options'],[{'label':'A','text':'二'},{'label':'B','text':'三'}])
        self.assertEqual(form.fields,before)
        self.assertEqual(form.options,{'1':['A','B']})
        self.assertEqual(form.font_decoding['mappedGlyphs'],2)
        self.assertNotIn('private-form-token',json.dumps(questions))

    def test_only_font_scopes_are_decoded_and_unrelated_text_remains(self):
        form=self.form(self.html.replace('</form>','<div>outside \ue001</div></form>'))
        with patch('backend.font_decoder.reference_glyphs',return_value=self.reference):
            form.answer_questions()
        self.assertIn('outside \ue001',form.form.text())

    def test_missing_or_malformed_embedded_font_reports_specific_reason(self):
        for html,reason in ((readable_form(False).replace('class="questionLi"','class="questionLi font-cxsecret"'),'没有提供'),
                            (self.html.replace('base64,','base64,!'),'编码无效')):
            with self.subTest(reason=reason),self.assertRaisesRegex(FontDecodeError,reason):
                self.form(html).answer_questions()

    def test_unmatched_used_glyph_leaves_document_unchanged_and_blocks_provider(self):
        form=self.form()
        before=form.form.text()
        ai=Mock()
        with tempfile.TemporaryDirectory() as directory:
            bank=AnswerBank(Path(directory)/'bank.sqlite3')
            with patch('backend.font_decoder.reference_glyphs',return_value={}):
                with self.assertRaisesRegex(AnswerError,'无法唯一识别'):
                    resolve_answers(form,bank,'owner',ai,'synthetic-key',threading.Event(),lambda *a:None)
        self.assertEqual(form.form.text(),before)
        ai.answer.assert_not_called()

    def test_unknown_glyph_not_used_in_text_does_not_block_known_glyph(self):
        known={key:value for key,value in self.reference.items() if value=='二'}
        form=self.form(self.html.replace('\ue002','正常选项'))
        with patch('backend.font_decoder.reference_glyphs',return_value=known):
            self.assertEqual(form.answer_questions()[0]['options'][0]['text'],'二')

    def test_shared_resolver_uses_decoded_bank_without_sk(self):
        form=self.form()
        ai=Mock()
        with tempfile.TemporaryDirectory() as directory:
            bank=AnswerBank(Path(directory)/'bank.sqlite3')
            with patch('backend.font_decoder.reference_glyphs',return_value=self.reference):
                bank.put('owner',form.answer_questions()[0],'B','imported')
                answers=resolve_answers(form,bank,'owner',ai,'',threading.Event(),lambda *a:None)
        self.assertEqual(answers,{'1':'B'})
        ai.answer.assert_not_called()

    def test_invalid_font_has_bounded_specific_failure(self):
        for raw in (b'bad',b'\x00\x01\x00\x00'+b'x'*100):
            with self.subTest(size=len(raw)),self.assertRaises(FontDecodeError):
                font_mapping(raw)

    def test_bundled_reference_and_corruption_check(self):
        reference_glyphs.cache_clear()
        self.assertEqual(len(reference_glyphs()),29048)
        reference_glyphs.cache_clear()
        try:
            with patch('backend.font_decoder.Path.read_bytes',return_value=b'{}'):
                with self.assertRaisesRegex(FontDecodeError,'校验失败'):
                    reference_glyphs()
        finally:
            reference_glyphs.cache_clear()


if __name__=='__main__':
    unittest.main()
