"""Pinned CSRC June 2026 product-code index adapter; not a share-class census."""
from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re

from publication_guard import inspect_content
from verify_universe import Workbook, VerificationError, SOURCE_URL, SOURCE_AS_OF, TARGET_CODES, download_official, local

SOURCE_HASH = '0f6ac234b792bb6334c0c94ec37fa6c0548363c80017c946d11a5c7e95d3a9e0'
EXPECTED_PRODUCTS = 14170
SOURCE_ID = 'csrc-fund-products-20260630'
PARSER_VERSION = 'csrc-20260630-abcd-v1'
SHEET = 'Sheet1'
OUTPUT = Path('.cache/universe-verification/result.json')
ERRORS = {'ACTIONS_REQUIRED', 'SOURCE_HASH_MISMATCH', 'INVALID_OBSERVATION_TIME', 'HISTORICAL_SCHEMA_MISMATCH',
          'UNSUPPORTED_DATE_SYSTEM', 'PUBLICATION_REJECTED', 'SUMMARY_TOO_LARGE', 'HISTORICAL_VERIFICATION_FAILED'}


class HistoricalUniverseError(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else 'HISTORICAL_VERIFICATION_FAILED'
        super().__init__(self.code)


def observation_time(value):
    try:
        observed = dt.datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if (not isinstance(observed, dt.datetime) or observed.tzinfo is None
                or observed.astimezone(dt.timezone.utc).date() < dt.date.fromisoformat(SOURCE_AS_OF)
                or observed > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)):
            raise ValueError
        return observed.astimezone(dt.timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        raise HistoricalUniverseError('INVALID_OBSERVATION_TIME') from None


def excel_1900_date(cell):
    if (cell['type'] != 'n' or cell['hasFormula'] or cell['numberFormat'] != 'yyyy/m/d;@'
            or not re.fullmatch(r'[1-9][0-9]{0,5}', cell['rawValue'])):
        return None
    serial = int(cell['rawValue'])
    # Excel's fictitious 1900-02-29 is not an admissible founding date.
    if serial == 60:
        return None
    try:
        date = dt.date(1899, 12, 31) + dt.timedelta(days=serial - int(serial > 60))
    except (OverflowError, ValueError):
        return None
    return date.isoformat() if date <= dt.date.fromisoformat(SOURCE_AS_OF) else None


def product_code(cell):
    if cell['hasFormula']:
        return None
    if cell['type'] == 'n' and cell['numberFormat'] == '000000' and re.fullmatch(r'[0-9]{1,6}', cell['rawValue']):
        code = cell['rawValue'].zfill(6)
    elif cell['type'] in ('s', 'inlineStr', 'str') and re.fullmatch(r'[0-9]{6}', cell['value']):
        code = cell['value']
    else:
        return None
    return code if code != '000000' else None


def parse_official_index(data, *, retrieved_at):
    """Return every accepted product plus explicit diagnostics for every gap.

    The exact file hash is pinned to the separate successful cloud schema audit.
    A changed file requires another schema audit, not a guessed column mapping.
    """
    observed = observation_time(retrieved_at)
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != SOURCE_HASH:
        raise HistoricalUniverseError('SOURCE_HASH_MISMATCH')
    book = Workbook(data)
    try:
        relationships = book.relationship_targets()
        shared, formats = book.strings_and_styles(relationships)
        workbook = book.xml('xl/workbook.xml', 'workbook')
        properties = [node for node in workbook if local(node.tag) == 'workbookPr']
        if len(properties) > 1 or any(node.get('date1904', '0') not in ('0', 'false') for node in properties):
            raise HistoricalUniverseError('UNSUPPORTED_DATE_SYSTEM')
        sheets = [node for group in workbook if local(group.tag) == 'sheets' for node in group if local(node.tag) == 'sheet']
        if len(sheets) != 1 or sheets[0].get('name') != SHEET:
            raise HistoricalUniverseError('HISTORICAL_SCHEMA_MISMATCH')
        relation = next((value for key, value in sheets[0].attrib.items() if local(key) == 'id'), None)
        target = relationships.get(relation)
        if not target or target[0] != 'worksheet':
            raise HistoricalUniverseError('HISTORICAL_SCHEMA_MISMATCH')
        sheet = book.xml(target[1], 'worksheet')
        groups = [group for group in sheet if local(group.tag) == 'sheetData']
        if len(groups) != 1:
            raise HistoricalUniverseError('HISTORICAL_SCHEMA_MISMATCH')
        rows = []
        for node in groups[0]:
            if local(node.tag) != 'row' or not re.fullmatch(r'[1-9][0-9]{0,6}', node.get('r', '')) or int(node.get('r')) > 1048576:
                raise HistoricalUniverseError('HISTORICAL_SCHEMA_MISMATCH')
            number = int(node.get('r'))
            try:
                cells = [book.cell(cell, number, shared, formats) for cell in node if local(cell.tag) == 'c']
            except VerificationError:
                rows.append((number, None))
            else:
                rows.append((number, cells))
        if len(rows) < 2 or [row for row, _ in rows[:2]] != [1, 2]:
            raise HistoricalUniverseError('HISTORICAL_SCHEMA_MISMATCH')
        for index, expected in [(0, ['基金产品索引', '', '', '']), (1, ['序号', '基金代码', '基金简称', '设立日期'])]:
            cells = rows[index][1]
            if (cells is None or [cell['column'] for cell in cells] != ['A', 'B', 'C', 'D']
                    or [cell['value'] for cell in cells] != expected or any(cell['hasFormula'] for cell in cells)):
                raise HistoricalUniverseError('HISTORICAL_SCHEMA_MISMATCH')
        products = rows[2:]
        physical = Counter(number for number, _ in rows)
        # A readable B-column code participates in duplicate detection even
        # when its date/name/other columns cannot produce an accepted record.
        code_rows = defaultdict(list)
        for number, cells in products:
            code_cells = [cell for cell in cells or [] if cell['column'] == 'B']
            code = product_code(code_cells[0]) if len(code_cells) == 1 else None
            if code is not None: code_rows[code].append(number)
        duplicates = {code: numbers for code, numbers in code_rows.items() if len(numbers) > 1}
        serials, candidates, rejected, name_cache_count = [], [], [], 0
        for number, cells in products:
            reasons = []
            if physical[number] > 1: reasons.append('DUPLICATE_PHYSICAL_ROW')
            if not 3 <= number <= EXPECTED_PRODUCTS + 2: reasons.append('UNEXPECTED_PHYSICAL_ROW')
            if cells is None:
                reasons.append('INVALID_CELL')
            elif [cell['column'] for cell in cells] != ['A', 'B', 'C', 'D']:
                reasons.append('COLUMN_MISMATCH')
            elif not any(cell['value'] for cell in cells):
                reasons.append('EMPTY_PRODUCT_ROW')
            if reasons:
                rejected.append({'row': number, 'reasons': reasons})
                continue
            ordinal, code_cell, name_cell, date_cell = cells
            if ordinal['hasFormula'] or ordinal['type'] != 'n' or not re.fullmatch(r'[1-9][0-9]{0,5}', ordinal['rawValue']):
                reasons.append('INVALID_SERIAL')
            else:
                serial = int(ordinal['rawValue']); serials.append(serial)
                if serial != number - 2: reasons.append('SERIAL_POSITION_MISMATCH')
            code = product_code(code_cell)
            if code is None: reasons.append('INVALID_FUND_CODE')
            name = name_cell['value']
            if name_cell['hasFormula'] and not name:
                reasons.append('MISSING_FORMULA_CACHE')
            if (name_cell['type'] not in ('s', 'inlineStr', 'str') or not 0 < len(name.strip()) <= 200
                    or re.search(r'[\x00-\x1f\x7f]', name) or not re.search(r'[A-Za-z\u4e00-\u9fff]', name)):
                reasons.append('INVALID_NAME')
            date = excel_1900_date(date_cell)
            if date is None: reasons.append('INVALID_FOUNDATION_DATE')
            if reasons:
                rejected.append({'row': number, 'reasons': reasons})
                continue
            name_cache_count += int(name_cell['hasFormula'])
            candidates.append({'code': code, 'name': name.strip(), 'type': '未分类', 'inceptionDate': date,
                               'inceptionSourceUrl': SOURCE_URL,
                               'inceptionEvidence': {'sourceId': SOURCE_ID, 'sheet': SHEET, 'row': number, 'column': 'D',
                                                     'rawSerial': date_cell['rawValue'], 'numberFormat': date_cell['numberFormat'],
                                                     'dateSystem': '1900'},
                               'nameFromCachedFormula': name_cell['hasFormula'],
                               'evidence': [{'sourceId': SOURCE_ID, 'sheet': SHEET, 'row': number}]})
        for numbers in duplicates.values():
            for number in set(numbers):
                prior = [entry for entry in rejected if entry['row'] == number]
                if prior:
                    for entry in prior:
                        if 'DUPLICATE_FUND_CODE' not in entry['reasons']: entry['reasons'].append('DUPLICATE_FUND_CODE')
                else:
                    rejected.append({'row': number, 'reasons': ['DUPLICATE_FUND_CODE']})
        records = sorted((record for record in candidates if record['code'] not in duplicates), key=lambda row: row['code'])
        missing_rows = set(range(3, EXPECTED_PRODUCTS + 3)) - {number for number, _ in products}
        increasing_rows = [number for number, _ in rows] == sorted(set(number for number, _ in rows))
        sequence_complete = increasing_rows and serials == list(range(1, EXPECTED_PRODUCTS + 1))
        complete = not rejected and not missing_rows and len(records) == EXPECTED_PRODUCTS and sequence_complete
        reason_counts = Counter(reason for row in rejected for reason in row['reasons'])
        diagnostics = {'sheet': SHEET, 'observedRows': len(rows), 'headerRows': 2,
                       'expectedProductRows': EXPECTED_PRODUCTS, 'observedProductRows': len(products),
                       'parsedRows': len(records), 'rejectedRows': len(rejected), 'missingPhysicalRows': len(missing_rows),
                       'blankRows': reason_counts['EMPTY_PRODUCT_ROW'], 'duplicateCodeCount': len(duplicates),
                       'duplicateCodeRows': sum(len(group) for group in duplicates.values()),
                       'duplicateSerialCount': sum(count - 1 for count in Counter(serials).values() if count > 1),
                       'physicalRowsIncreasing': increasing_rows, 'serialSequenceComplete': sequence_complete,
                       'cachedFormulaNameRows': name_cache_count, 'rejectionReasons': dict(sorted(reason_counts.items())),
                       'rejectedRowSamples': sorted(rejected, key=lambda row: row['row'])[:20],
                       'omittedRejectedRowSamples': max(0, len(rejected) - 20),
                       'missingPhysicalRowSamples': sorted(missing_rows)[:20],
                       'parsedCells': book.cells}
        return {'schemaVersion': 1, 'targetPeriodEnd': SOURCE_AS_OF, 'parseStatus': 'complete' if complete else 'partial',
                'recordUnit': 'fund-product-code', 'historicalUniverseVerified': False,
                'records': records, 'evidence': [{'id': SOURCE_ID, 'sourceUrl': SOURCE_URL, 'effectiveDate': SOURCE_AS_OF,
                    'retrievedAt': observed, 'contentHash': SOURCE_HASH, 'format': 'xlsx', 'recordUnit': 'fund-product-code',
                    'title': '公募基金产品索引（截至20260630）', 'authority': '中国证监会', 'parserVersion': PARSER_VERSION,
                    'parsedRows': len(records), 'expectedRows': EXPECTED_PRODUCTS,
                    'unresolvedRows': len(rejected) + len(missing_rows)}],
                'downloadedBytes': len(data), 'diagnostics': diagnostics}
    finally:
        book.archive.close()


def fetch_official_overlay():
    """Download once on Actions; return an in-memory overlay, never a file."""
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise HistoricalUniverseError('ACTIONS_REQUIRED')
    data = download_official()
    return parse_official_index(data, retrieved_at=dt.datetime.now(dt.timezone.utc))


def summary_for(overlay):
    source = overlay['evidence'][0]
    dates = [record['inceptionDate'] for record in overlay['records']]
    by_code = {record['code']: record for record in overlay['records']}
    return {'schemaVersion': 1, 'status': 'verified' if overlay['parseStatus'] == 'complete' else 'partial',
            'parseStatus': overlay['parseStatus'], 'recordUnit': 'fund-product-code', 'historicalUniverseVerified': False,
            'productionCatalogModified': False, 'source': source, 'downloadedBytes': overlay['downloadedBytes'],
            'recordCount': len(overlay['records']), 'diagnostics': overlay['diagnostics'],
            'inceptionDateRange': {'min': min(dates) if dates else None, 'max': max(dates) if dates else None},
            'codeLookups': {code: {'found': code in by_code, 'record': by_code.get(code)} for code in TARGET_CODES},
            'limitations': ['该索引为基金产品主代码，未列出的份额代码仍需报告证据展开。',
                            '全表解析完成不代表历史份额目录完整；未修改正式目录。']}


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'ACTIONS_REQUIRED'}))
        return 2
    try:
        result = summary_for(fetch_official_overlay())
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(encoded) > 96_000: raise HistoricalUniverseError('SUMMARY_TOO_LARGE')
        if inspect_content(encoded): raise HistoricalUniverseError('PUBLICATION_REJECTED')
    except (HistoricalUniverseError, VerificationError) as error:
        result = {'status': 'failed', 'errorCode': error.code, 'sourceUrl': SOURCE_URL,
                  'historicalUniverseVerified': False, 'productionCatalogModified': False}
    except Exception:
        result = {'status': 'failed', 'errorCode': 'HISTORICAL_VERIFICATION_FAILED',
                  'historicalUniverseVerified': False, 'productionCatalogModified': False}
    data = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp'); temporary.write_bytes(data); temporary.replace(OUTPUT)
    print(json.dumps({key: result.get(key) for key in ('status', 'parseStatus', 'recordCount', 'errorCode')}))
    return 0 if result['status'] == 'verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
