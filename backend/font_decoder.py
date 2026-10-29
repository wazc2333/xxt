"""Recover cxsecret text offline; reference license and provenance: FONT_DECODING.md."""
import base64
import hashlib
import io
import json
import re
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont

REFERENCE_SHA256 = '307a19af6b5ed128db0a3cbfcacb54264f3a428fdcf644ada638cd0f895d3374'
MAX_FONT_BYTES = 2_000_000


class FontDecodeError(ValueError):
    pass


@lru_cache(maxsize=1)
def reference_glyphs():
    try:
        raw = (Path(__file__).parent/'resources/cxsecret-glyphs.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != REFERENCE_SHA256:
            raise ValueError()
        data = json.loads(raw)
        if len(data) != 29048 or any(not re.fullmatch('[0-9a-f]{32}', key)
                                   or not isinstance(value, str) or len(value) != 1 for key,value in data.items()):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError):
        raise FontDecodeError('本地字体映射文件缺失或校验失败，请更新完整部署包') from None


@lru_cache(maxsize=16)
def font_mapping(raw):
    if not 12 <= len(raw) <= MAX_FONT_BYTES or raw[:4] not in (b'\x00\x01\x00\x00', b'true'):
        raise FontDecodeError('答题页字体格式暂不支持，未将乱码发送给 DeepSeek')
    reference = reference_glyphs()
    try:
        with TTFont(io.BytesIO(raw), lazy=True, recalcBBoxes=False, recalcTimestamp=False) as font:
            if not {'glyf','cmap','maxp'}.issubset(font.keys()) or not 1 <= font['maxp'].numGlyphs <= 4096:
                raise ValueError()
            cmap = font.getBestCmap()
            if not cmap or len(cmap) > 4096:
                raise ValueError()
            known, unknown = {}, set()
            for code, name in cmap.items():
                glyph = font['glyf'][name]
                if glyph.numberOfContours <= 0 or len(glyph.coordinates) > 20000:
                    unknown.add(code)
                    continue
                # Coordinates and on-curve bits in contour order match the reference data.
                points = ''.join(f'{x}{y}{flag & 1}' for (x,y),flag in zip(glyph.coordinates,glyph.flags))
                character = reference.get(hashlib.md5(points.encode()).hexdigest())
                if character is None:
                    unknown.add(code)
                else:
                    known[code] = character
            return known, frozenset(unknown)
    except FontDecodeError:
        raise
    except Exception:
        raise FontDecodeError('答题页内嵌字体无法解析，未将乱码发送给 DeepSeek') from None


def embedded_fonts(document):
    fonts = set()
    for node in document.root.walk():
        if node.tag != 'style':
            continue
        css = ''.join(child for child in node.children if isinstance(child,str))
        for block in re.findall(r'@font-face\s*\{([^{}]*)\}',css,re.I):
            if not re.search(r'font-family\s*:\s*[\'"]?font-cxsecret[\'"]?\s*(?:;|$)',block,re.I):
                continue
            for uri in re.findall(r'url\(\s*[\'"]?(data:[^\'"\)]+)[\'"]?\s*\)',block,re.I):
                header, separator, payload = uri.partition(',')
                if not separator or ';base64' not in header.lower():
                    continue
                if len(payload) > MAX_FONT_BYTES*4//3+100:
                    raise FontDecodeError('答题页内嵌字体过大，未解析')
                try:
                    raw = base64.b64decode(''.join(payload.split()),validate=True)
                except (ValueError, TypeError):
                    raise FontDecodeError('答题页内嵌字体编码无效，未解析') from None
                fonts.add(raw)
                if len(fonts) > 4:
                    raise FontDecodeError('答题页存在过多字体定义，无法唯一确认文字映射')
    if not fonts:
        raise FontDecodeError('题干使用加密字体，但答题页没有提供受支持的内嵌字体')
    return fonts


def decode_document(document, form):
    mapping, unresolved = {}, set()
    fonts = embedded_fonts(document)
    for raw in fonts:
        known, unknown = font_mapping(raw)
        unresolved.update(unknown)
        for code, character in known.items():
            if code in mapping and mapping[code] != character:
                unresolved.add(code)
            else:
                mapping[code] = character
    changes, missing = [], set()
    def visit(node, encrypted=False):
        if node.tag in ('script','style','textarea','input'):
            return
        encrypted = encrypted or node.has_class('font-cxsecret')
        for index,child in enumerate(node.children):
            if isinstance(child,str):
                if encrypted:
                    missing.update(ord(c) for c in child if ord(c) in unresolved)
                    changes.append((node,index,child.translate(mapping)))
            else:
                visit(child,encrypted)
    inherited = any(n.has_class('font-cxsecret') for n in _parents(form.parent))
    visit(form,inherited)
    if missing:
        raise FontDecodeError(f'加密字体中有 {len(missing)} 个使用中的字形无法唯一识别，未将乱码发送给 DeepSeek')
    for node,index,text in changes:
        node.children[index] = text
    return {'embeddedFonts':len(fonts), 'mappedGlyphs':len(mapping), 'decodedTextNodes':len(changes)}


def _parents(node):
    while node is not None:
        yield node
        node = node.parent
