"""Inspect one official XLSX on Actions without changing the production catalog.

Only bounded headers and two requested code lookups leave memory. Column labels
are never interpreted as a product schema, and formulas are never executed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET
import zipfile

from publication_guard import inspect_content

SOURCE_URL = 'https://www.csrc.gov.cn/csrc/c101900/c1029655/1029655/files/公募基金产品索引（截至20260630）.xlsx'
SOURCE_AS_OF = '2026-06-30'
TARGET_CODES = ('018774', '018775')
OUTPUT = Path('.cache/universe-verification/result.json')
MAX_DOWNLOAD = 5 * 1024 * 1024
MAX_EXPANDED = 40 * 1024 * 1024
MAX_ENTRIES = 512
MAX_SHEETS = 32
MAX_CELLS = 500_000
MAX_STRINGS = 200_000
MAX_TEXT_CHARS = 32767
MAX_SUMMARY = 128 * 1024
MAX_MATCH_ROWS = 8
MAIN_NAMESPACES = {'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
                   'http://purl.oclc.org/ooxml/spreadsheetml/main'}
ERRORS = {'ACTIONS_REQUIRED', 'HTTP_REJECTED', 'NETWORK_ERROR', 'TIMEOUT', 'DOWNLOAD_TOO_LARGE',
          'EMPTY_RESPONSE', 'INVALID_ZIP', 'ZIP_LIMIT', 'ZIP_PATH_INVALID', 'XML_REJECTED', 'XML_LIMIT',
          'UNSUPPORTED_SCHEMA', 'SUMMARY_TOO_LARGE', 'PUBLICATION_REJECTED', 'UNIVERSE_VERIFICATION_FAILED'}


class VerificationError(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else 'UNIVERSE_VERIFICATION_FAILED'
        super().__init__(self.code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise VerificationError('HTTP_REJECTED')


def download_official():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise VerificationError('ACTIONS_REQUIRED')
    request = Request(quote(SOURCE_URL, safe=':/'), headers={
        'User-Agent': 'record-lab-universe-verifier/1.0', 'Accept-Encoding': 'identity'})
    deadline = time.monotonic() + 60
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise VerificationError('HTTP_REJECTED')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_DOWNLOAD):
                raise VerificationError('DOWNLOAD_TOO_LARGE')
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise VerificationError('TIMEOUT')
                chunk = response.read(min(64 * 1024, MAX_DOWNLOAD + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise VerificationError('DOWNLOAD_TOO_LARGE')
                chunks.append(chunk)
            if not size:
                raise VerificationError('EMPTY_RESPONSE')
            return b''.join(chunks)
    except VerificationError:
        raise
    except HTTPError:
        raise VerificationError('HTTP_REJECTED') from None
    except (TimeoutError, socket.timeout):
        raise VerificationError('TIMEOUT') from None
    except (URLError, OSError):
        raise VerificationError('NETWORK_ERROR') from None


def local(tag):
    return tag.rsplit('}', 1)[-1]


def safe_member(name):
    path = PurePosixPath(name)
    if (not name or len(name) > 240 or '\\' in name or ':' in name or '\0' in name
            or name.startswith('/') or any(part in ('', '.', '..') for part in name.rstrip('/').split('/'))):
        raise VerificationError('ZIP_PATH_INVALID')
    return str(path)


class Workbook:
    def __init__(self, data):
        if not isinstance(data, bytes) or not data or len(data) > MAX_DOWNLOAD:
            raise VerificationError('DOWNLOAD_TOO_LARGE' if data else 'EMPTY_RESPONSE')
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(data))
            entries = self.archive.infolist()
        except (zipfile.BadZipFile, OSError):
            raise VerificationError('INVALID_ZIP') from None
        if len(entries) > MAX_ENTRIES or sum(item.file_size for item in entries) > MAX_EXPANDED:
            raise VerificationError('ZIP_LIMIT')
        self.entries, self.expanded_bytes = {}, sum(item.file_size for item in entries)
        for item in entries:
            name = safe_member(item.filename)
            if (name in self.entries or item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16)
                    or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise VerificationError('INVALID_ZIP')
            self.entries[name] = item
        self.cells = 0

    def xml(self, name, expected):
        if name not in self.entries:
            raise VerificationError('UNSUPPORTED_SCHEMA')
        try:
            with self.archive.open(self.entries[name]) as stream:
                data = stream.read(MAX_EXPANDED + 1)
            if len(data) > MAX_EXPANDED or len(data) != self.entries[name].file_size:
                raise VerificationError('ZIP_LIMIT')
            if b'\0' in data or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
                raise VerificationError('XML_REJECTED')
            declaration = 3 if data.startswith(b'\xef\xbb\xbf') else 0
            encoding = re.search(br'<\?xml\s[^?]*encoding=["\']([^"\']+)', data[declaration:256], re.I)
            if encoding and encoding[1].lower() not in (b'utf-8', b'utf8', b'us-ascii'):
                raise VerificationError('XML_REJECTED')
            depth = 0
            parser = ET.iterparse(io.BytesIO(data), events=('start', 'end'))
            for event, _element in parser:
                depth += 1 if event == 'start' else -1
                if depth > 64:
                    raise VerificationError('XML_LIMIT')
            root = parser.root
            if local(root.tag) != expected:
                raise VerificationError('UNSUPPORTED_SCHEMA')
            if expected != 'Relationships' and root.tag.split('}')[0].lstrip('{') not in MAIN_NAMESPACES:
                raise VerificationError('UNSUPPORTED_SCHEMA')
            return root
        except VerificationError:
            raise
        except (ET.ParseError, zipfile.BadZipFile, RuntimeError, OSError, ValueError):
            raise VerificationError('XML_REJECTED') from None

    def relationship_targets(self):
        result = {}
        for item in self.xml('xl/_rels/workbook.xml.rels', 'Relationships'):
            if local(item.tag) != 'Relationship':
                continue
            identity, target = item.get('Id'), item.get('Target', '')
            if not identity or identity in result or item.get('TargetMode', 'Internal') != 'Internal':
                raise VerificationError('UNSUPPORTED_SCHEMA')
            name = safe_member(target[1:] if target.startswith('/') else 'xl/' + target)
            if not name.startswith('xl/') or name not in self.entries:
                raise VerificationError('UNSUPPORTED_SCHEMA')
            result[identity] = (item.get('Type', '').rsplit('/', 1)[-1], name)
        return result

    def text(self, element):
        container = next((node for node in element if local(node.tag) == 'is'), None) if local(element.tag) == 'c' else element
        if container is None:
            raise VerificationError('UNSUPPORTED_SCHEMA')
        # rPh is a pronunciation annotation, not part of the displayed cell.
        parts = []
        for node in container:
            if local(node.tag) == 't':
                parts.append(node.text or '')
            elif local(node.tag) == 'r':
                parts.extend(child.text or '' for child in node if local(child.tag) == 't')
        value = ''.join(parts)
        if len(value) > MAX_TEXT_CHARS:
            raise VerificationError('XML_LIMIT')
        return value

    def strings_and_styles(self, relationships):
        shared, formats = [], []
        for kind, name in relationships.values():
            if kind == 'sharedStrings':
                if shared:
                    raise VerificationError('UNSUPPORTED_SCHEMA')
                root = self.xml(name, 'sst')
                shared = [self.text(node) for node in root if local(node.tag) == 'si']
                if len(shared) > MAX_STRINGS:
                    raise VerificationError('XML_LIMIT')
            elif kind == 'styles':
                if formats:
                    raise VerificationError('UNSUPPORTED_SCHEMA')
                root = self.xml(name, 'styleSheet')
                custom = {node.get('numFmtId'): node.get('formatCode') for group in root if local(group.tag) == 'numFmts'
                          for node in group if local(node.tag) == 'numFmt'}
                formats = [custom.get(node.get('numFmtId')) for group in root if local(group.tag) == 'cellXfs'
                           for node in group if local(node.tag) == 'xf']
                self.number_format_ids = [node.get('numFmtId') for group in root if local(group.tag) == 'cellXfs'
                                          for node in group if local(node.tag) == 'xf']
                if any(value is not None and len(value) > 512 for value in formats):
                    raise VerificationError('XML_LIMIT')
        return shared, formats

    def cell(self, cell, row, shared, formats):
        reference = cell.get('r', '')
        match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', reference)
        if not match or int(match[2]) != row:
            raise VerificationError('UNSUPPORTED_SCHEMA')
        column = 0
        for char in match[1]:
            column = column * 26 + ord(char) - 64
        if column > 16384:
            raise VerificationError('UNSUPPORTED_SCHEMA')
        self.cells += 1
        if self.cells > MAX_CELLS:
            raise VerificationError('XML_LIMIT')
        kind = cell.get('t', 'n')
        raw = next((node.text or '' for node in cell if local(node.tag) == 'v'), '')
        if len(raw) > MAX_TEXT_CHARS:
            raise VerificationError('XML_LIMIT')
        if kind == 's':
            if not raw.isdigit() or int(raw) >= len(shared):
                raise VerificationError('UNSUPPORTED_SCHEMA')
            value = shared[int(raw)]
        elif kind == 'inlineStr':
            value = self.text(cell)
        elif kind in ('n', 'str', 'b', 'e', 'd'):
            value = raw
        else:
            raise VerificationError('UNSUPPORTED_SCHEMA')
        style = cell.get('s')
        if style is not None and (not style.isdigit() or int(style) >= len(formats)):
            raise VerificationError('UNSUPPORTED_SCHEMA')
        number_format = formats[int(style)] if style is not None else None
        return {'reference': reference, 'column': match[1], 'type': kind, 'rawValue': raw, 'value': value,
                'numberFormatId': self.number_format_ids[int(style)] if style is not None else None,
                'numberFormat': number_format, 'hasFormula': any(local(node.tag) == 'f' for node in cell)}


def shorten(value, limit):
    encoded = value.encode('utf-8')
    return encoded[:limit].decode('utf-8', errors='ignore'), len(encoded) > limit


def row_summary(number, cells, header=False):
    cap, chars = (8, 96) if header else (32, 192)
    shown = []
    for cell in cells[:cap]:
        value = dict(cell)
        truncated = False
        for key in ('rawValue', 'value', 'numberFormat'):
            if value[key] is not None:
                value[key], limited = shorten(value[key], chars)
                truncated = truncated or limited
        value['truncated'] = truncated
        shown.append(value)
    return {'row': number, 'cellCount': len(cells), 'omittedCellCount': max(0, len(cells) - cap), 'cells': shown}


def inspect_workbook(data):
    book = Workbook(data)
    try:
        relationships = book.relationship_targets()
        shared, formats = book.strings_and_styles(relationships)
        sheets = [node for group in book.xml('xl/workbook.xml', 'workbook') if local(group.tag) == 'sheets'
                  for node in group if local(node.tag) == 'sheet']
        if not sheets or len(sheets) > MAX_SHEETS:
            raise VerificationError('UNSUPPORTED_SCHEMA')
        result = {'sheetCount': len(sheets), 'sheetNames': [], 'sheets': [], 'lookup': {
            code: {'exactMatchCount': 0, 'numericCandidateCount': 0, 'matches': [], 'numericCandidates': []} for code in TARGET_CODES},
            'zipEntryCount': len(book.entries), 'expandedBytes': book.expanded_bytes, 'parsedCellCount': 0,
            'schemaMapping': 'not-inferred', 'scope': 'bounded-headers-and-requested-code-lookups',
            'numericCandidateMeaning': 'Numeric values without an explicit six-digit format are candidates, not confirmed fund codes.'}
        seen_sheets, seen_targets = set(), set()
        for sheet in sheets:
            name = sheet.get('name')
            relation = next((value for key, value in sheet.attrib.items() if local(key) == 'id'), None)
            target = relationships.get(relation)
            if (not name or len(name) > 100 or name in seen_sheets or not target or target[0] != 'worksheet'
                    or target[1] in seen_targets):
                raise VerificationError('UNSUPPORTED_SCHEMA')
            seen_sheets.add(name)
            seen_targets.add(target[1])
            summary = {'name': name, 'state': sheet.get('state', 'visible'), 'rowCount': 0, 'nonemptyRowCount': 0,
                       'maxRowNumber': 0, 'cellCount': 0, 'firstFiveRows': []}
            root = book.xml(target[1], 'worksheet')
            if sum(local(group.tag) == 'sheetData' for group in root) != 1:
                raise VerificationError('UNSUPPORTED_SCHEMA')
            for group in root:
                if local(group.tag) != 'sheetData':
                    continue
                for row in group:
                    if local(row.tag) != 'row' or not (row.get('r') or '').isdigit():
                        raise VerificationError('UNSUPPORTED_SCHEMA')
                    number = int(row.get('r'))
                    if not summary['maxRowNumber'] < number <= 1048576:
                        raise VerificationError('UNSUPPORTED_SCHEMA')
                    cells = [book.cell(cell, number, shared, formats) for cell in row if local(cell.tag) == 'c']
                    if len({cell['reference'] for cell in cells}) != len(cells):
                        raise VerificationError('UNSUPPORTED_SCHEMA')
                    summary['rowCount'] += 1
                    summary['nonemptyRowCount'] += int(any(cell['value'] for cell in cells))
                    summary['maxRowNumber'] = number
                    summary['cellCount'] += len(cells)
                    if len(summary['firstFiveRows']) < 5:
                        summary['firstFiveRows'].append(row_summary(number, cells, header=True))
                    for code in TARGET_CODES:
                        exact, candidates = [], []
                        for cell in cells:
                            value = cell['value'].strip()
                            if value == code:
                                exact.append({'reference': cell['reference'], 'method': 'exact-cell-value'})
                            elif cell['type'] == 'n' and value == str(int(code)):
                                if cell['numberFormat'] == '000000':
                                    exact.append({'reference': cell['reference'], 'method': 'explicit-six-digit-number-format'})
                                else:
                                    candidates.append({'reference': cell['reference'], 'method': 'numeric-equivalent-unverified'})
                        for hits, count_key, rows_key in [(exact, 'exactMatchCount', 'matches'),
                                                         (candidates, 'numericCandidateCount', 'numericCandidates')]:
                            if hits:
                                lookup = result['lookup'][code]
                                lookup[count_key] += 1
                                if len(lookup[rows_key]) < MAX_MATCH_ROWS:
                                    lookup[rows_key].append({'sheet': name, 'hits': hits[:32], **row_summary(number, cells)})
            result['sheetNames'].append(name)
            result['sheets'].append(summary)
        for lookup in result['lookup'].values():
            lookup['omittedMatchRows'] = lookup['exactMatchCount'] - len(lookup['matches'])
            lookup['omittedCandidateRows'] = lookup['numericCandidateCount'] - len(lookup['numericCandidates'])
        result['parsedCellCount'] = book.cells
        return result
    finally:
        book.archive.close()


def run_verification(fetch=download_official, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    result = {'schemaVersion': 1, 'sourceUrl': SOURCE_URL, 'sourceAsOf': SOURCE_AS_OF,
              'sourceAsOfBasis': 'official-download-filename', 'catalogCompletenessVerified': False,
              'retrievedAt': now.isoformat(), 'status': 'failed', 'errorCode': None,
              'requestedCodes': list(TARGET_CODES), 'productionCatalogModified': False}
    try:
        data = fetch()
        result.update(sourceSha256=hashlib.sha256(data).hexdigest(), downloadedBytes=len(data))
        result.update(inspect_workbook(data), status='verified')
    except VerificationError as error:
        result['errorCode'] = error.code
    except Exception:
        result['errorCode'] = 'UNIVERSE_VERIFICATION_FAILED'
    return result


def encoded_result(result):
    data = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    error = 'SUMMARY_TOO_LARGE' if len(data) > MAX_SUMMARY else 'PUBLICATION_REJECTED' if inspect_content(data) else None
    if error:
        result = {'schemaVersion': 1, 'sourceUrl': SOURCE_URL, 'sourceAsOf': SOURCE_AS_OF,
                  'status': 'failed', 'errorCode': error, 'productionCatalogModified': False,
                  'catalogCompletenessVerified': False}
        data = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return result, data


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'ACTIONS_REQUIRED'}))
        return 2
    result, data = encoded_result(run_verification())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_bytes(data)
    temporary.replace(OUTPUT)
    print(json.dumps({key: result.get(key) for key in ('status', 'errorCode', 'downloadedBytes', 'sheetCount')}))
    return 0 if result['status'] == 'verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
