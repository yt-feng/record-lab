"""Read-only June/August product-code reconciliation, executed on Actions.

Names and inception dates are compared in memory. The artifact exposes only
counts, public code sets, fixed sample membership and attributable sources.
Neither a matching aggregate nor a successfully parsed workbook is a census.
"""
from collections import Counter
import datetime as dt
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, unquote, urljoin
from urllib.request import Request

import historical_universe as history
import verify_universe as verify
from publication_guard import content_limit, inspect_content

INDEX_URL = 'https://www.csrc.gov.cn/csrc/c101900/c1029655/content.shtml'
AUGUST_URL = history.SOURCE_URL.replace('20260630', '20260831')
SOURCES = (
    {'asOf': '2026-06-30', 'sourceUrl': history.SOURCE_URL, 'aggregateProducts': 14353,
     'aggregateSourceUrl': 'https://www.amac.org.cn/sjtj/tjbg/gmjj/202607/P020260723642575271747.pdf'},
    {'asOf': '2026-08-31', 'sourceUrl': AUGUST_URL, 'aggregateProducts': 14585,
     'aggregateSourceUrl': 'https://www.amac.org.cn/sjtj/tjbg/gmjj/202609/P020260918624509873296.pdf'},
)
SAMPLES = (
    {'code': '508001', 'category': 'infrastructure-REIT',
     'sourceUrl': 'https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-11/508001_20260611_OMHL.pdf'},
    {'code': '508080', 'category': 'infrastructure-REIT',
     'sourceUrl': 'https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-04-02/508080_20260402_0BCD.pdf'},
    {'code': '508603', 'category': 'commercial-property-REIT',
     'sourceUrl': 'https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-12/508603_20260612_538L.pdf'},
    {'code': '890017', 'category': 'former-large-collective-now-public-fund',
     'sourceUrl': 'https://www.cjzcgl.com/changjiangzg-web/product/detail/890017?type=0'},
)
MAX_PRODUCTS = 50000
MAX_SCHEMA_SHEETS = 6
MAX_SCHEMA_ROWS = 12
MAX_SCHEMA_CELLS = 12
CATALOG_PATHS = ('catalog.json', 'periods/2026Q2/catalog.json', 'periods/2026Q3/catalog.json')


def download_source(url):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise verify.VerificationError('ACTIONS_REQUIRED')
    if url not in {INDEX_URL, history.SOURCE_URL, AUGUST_URL}:
        raise verify.VerificationError('HTTP_REJECTED')
    request = Request(quote(url, safe=':/'), headers={
        'User-Agent': 'record-lab-universe-verifier/1.0', 'Accept-Encoding': 'identity'})
    deadline = time.monotonic() + verify.DOWNLOAD_DEADLINE_SECONDS
    with verify.download_deadline(deadline):
        for attempt in range(verify.DOWNLOAD_ATTEMPTS):
            try:
                return verify._download_attempt(request, deadline)
            except verify.VerificationError as error:
                if error.code not in {'NETWORK_ERROR', 'TIMEOUT'} or attempt + 1 == verify.DOWNLOAD_ATTEMPTS:
                    raise
                delay = attempt + 1
                if deadline - time.monotonic() <= delay:
                    raise verify.VerificationError('TIMEOUT') from None
                time.sleep(delay)
    raise verify.VerificationError('TIMEOUT')


class IndexPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.hidden = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}: self.hidden += 1
        if tag == 'a':
            for key, value in attrs:
                if key == 'href' and value: self.links.append(unquote(urljoin(INDEX_URL, value)))

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}: self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden: self.text.append(data)


def landing_evidence(raw, dates):
    if not isinstance(raw, bytes) or len(raw) > 512000:
        raise verify.VerificationError('DOWNLOAD_TOO_LARGE')
    page = IndexPage(); page.feed(raw.decode('utf-8'))
    found = dates(' '.join(page.text))
    return {'sourceUrl': INDEX_URL, 'contentSha256': hashlib.sha256(raw).hexdigest(),
            'bodyExplicitDates': found,
            'linkedProductPeriods': [source['asOf'] for source in SOURCES if source['sourceUrl'] in page.links],
            'fetchStatus': 'observed'}


def _inception(cell, as_of):
    if (cell['type'] != 'n' or cell['hasFormula'] or not history.known_date_format(cell)
            or not re.fullmatch(r'[1-9][0-9]{0,5}', cell['rawValue'])):
        return None, False
    serial = int(cell['rawValue'])
    if serial == 60: return None, False
    try: value = (dt.date(1899, 12, 31) + dt.timedelta(days=serial - int(serial > 60))).isoformat()
    except (OverflowError, ValueError): return None, False
    return (value, value > as_of)


def parse_product_table(data, source, dates):
    """Inspect exact four-column tables without changing the pinned adapter."""
    book = verify.Workbook(data)
    try:
        relationships = book.relationship_targets(); shared, formats = book.strings_and_styles(relationships)
        workbook = book.xml('xl/workbook.xml', 'workbook')
        properties = [n for n in workbook if verify.local(n.tag) == 'workbookPr']
        if len(properties) > 1 or any(n.get('date1904', '0') not in ('0', 'false') for n in properties):
            raise verify.VerificationError('UNSUPPORTED_SCHEMA')
        sheets = [n for group in workbook if verify.local(group.tag) == 'sheets' for n in group if verify.local(n.tag) == 'sheet']
        if len(sheets) != 1: raise verify.VerificationError('UNSUPPORTED_SCHEMA')
        relation = next((v for k, v in sheets[0].attrib.items() if verify.local(k) == 'id'), None)
        target = relationships.get(relation)
        if not target or target[0] != 'worksheet': raise verify.VerificationError('UNSUPPORTED_SCHEMA')
        groups = [g for g in book.xml(target[1], 'worksheet') if verify.local(g.tag) == 'sheetData']
        if len(groups) != 1: raise verify.VerificationError('UNSUPPORTED_SCHEMA')
        rows = list(groups[0])
        if not 3 <= len(rows) <= MAX_PRODUCTS + 2: raise verify.VerificationError('XML_LIMIT')
        parsed, observed_codes, rejections, headers, serials, physical = [], [], Counter(), [], [], []
        for index, row in enumerate(rows):
            if verify.local(row.tag) != 'row' or not re.fullmatch(r'[1-9][0-9]{0,6}', row.get('r', '')):
                raise verify.VerificationError('UNSUPPORTED_SCHEMA')
            number = int(row.get('r')); physical.append(number)
            cells = [book.cell(c, number, shared, formats) for c in row if verify.local(c.tag) == 'c']
            if index < 2:
                if number != index + 1 or [c['column'] for c in cells] != list('ABCD') or any(c['hasFormula'] for c in cells):
                    raise verify.VerificationError('UNSUPPORTED_SCHEMA')
                headers.append([c['value'] for c in cells]); continue
            code_cells = [c for c in cells if c['column'] == 'B']
            code = history.product_code(code_cells[0]) if len(code_cells) == 1 else None
            if code: observed_codes.append(code)
            if [c['column'] for c in cells] != list('ABCD'):
                rejections['COLUMN_MISMATCH'] += 1; continue
            ordinal, _, name_cell, date_cell = cells
            if ordinal['type'] != 'n' or ordinal['hasFormula'] or not re.fullmatch(r'[1-9][0-9]{0,5}', ordinal['rawValue']):
                rejections['SERIAL_INVALID'] += 1; continue
            serials.append(int(ordinal['rawValue']))
            name = name_cell['value'].strip()
            if (code is None or name_cell['type'] not in {'s', 'inlineStr', 'str'} or not 0 < len(name) <= 200
                    or re.search(r'[\x00-\x1f\x7f]', name) or not re.search(r'[A-Za-z\u4e00-\u9fff]', name)):
                rejections['CODE_OR_NAME_INVALID'] += 1; continue
            inception, future = _inception(date_cell, source['asOf'])
            if future: rejections['FOUNDATION_AFTER_INDEX_DATE'] += 1; continue
            parsed.append({'code': code, 'name': name, 'nameFromCachedFormula': name_cell['hasFormula'],
                           'inceptionDate': inception, 'row': number})
        if (headers[1] != ['序号', '基金代码', '基金简称', '设立日期'] or headers[0][1:] != ['', '', '']
                or not headers[0][0].startswith('基金产品索引')):
            raise verify.VerificationError('UNSUPPORTED_SCHEMA')
        duplicates = {code for code, count in Counter(observed_codes).items() if count > 1}
        records = {r['code']: r for r in parsed if r['code'] not in duplicates}
        sequence = physical == list(range(1, len(rows) + 1)) and serials == list(range(1, len(rows) - 1))
        codes_complete = not rejections and not duplicates and sequence and len(records) == len(rows) - 2
        header_dates = dates(' '.join(headers[0] + headers[1]))
        summary = {**source, 'sourceSha256': hashlib.sha256(data).hexdigest(), 'downloadedBytes': len(data),
            'recordUnit': 'product-code-not-share-class-census', 'observedProductRows': len(rows) - 2,
            'parsedProductCodes': len(records), 'codeRowsComplete': codes_complete,
            'namesParsed': len(records), 'cachedFormulaNamesParsed': sum(r['nameFromCachedFormula'] for r in records.values()),
            'duplicateCodeCount': len(duplicates), 'rowRejections': dict(rejections),
            'serialAndPhysicalSequenceComplete': sequence,
            'inceptionDatesParsed': sum(r['inceptionDate'] is not None for r in records.values()),
            'inceptionDatesUnresolved': sum(r['inceptionDate'] is None for r in records.values()),
            'headerExplicitDates': header_dates, 'historicalUniverseVerified': False}
        return summary, records
    finally:
        book.archive.close()


def schema_diagnostic(data):
    """Bounded shape evidence on Actions; no guessed mapping or broad names.

    The first twelve physical rows expose bounded public text so a header
    after a title or blank rows is still observable. Fixed public sample codes
    expose matching references only. Formula expressions and rows outside the
    bounded preview never enter this diagnostic.
    """
    book = verify.Workbook(data)
    try:
        relationships = book.relationship_targets(); shared, formats = book.strings_and_styles(relationships)
        workbook = book.xml('xl/workbook.xml', 'workbook')
        properties = [node for node in workbook if verify.local(node.tag) == 'workbookPr']
        sheets = [node for group in workbook if verify.local(group.tag) == 'sheets'
                  for node in group if verify.local(node.tag) == 'sheet']
        if not sheets or len(sheets) > verify.MAX_SHEETS: raise verify.VerificationError('UNSUPPORTED_SCHEMA')
        result = {'schemaVersion': 1, 'status': 'observed', 'schemaMapping': 'not-inferred',
                  'sourceSha256': hashlib.sha256(data).hexdigest(), 'downloadedBytes': len(data),
                  'sheetCount': len(sheets), 'omittedSheetCount': max(0, len(sheets) - MAX_SCHEMA_SHEETS),
                  'workbookPropertyCount': len(properties),
                  'date1904Values': [node.get('date1904', '0')[:20] for node in properties[:6]],
                  'sheets': [], 'fixedSamplesProveProductMembership': False,
                  'historicalUniverseVerified': False, 'catalogCompletenessVerified': False}
        for sheet in sheets[:MAX_SCHEMA_SHEETS]:
            name = sheet.get('name', '')
            relation = next((v for k, v in sheet.attrib.items() if verify.local(k) == 'id'), None)
            target = relationships.get(relation)
            if not name or len(name) > 100 or not target or target[0] != 'worksheet':
                raise verify.VerificationError('UNSUPPORTED_SCHEMA')
            groups = [node for node in book.xml(target[1], 'worksheet') if verify.local(node.tag) == 'sheetData']
            if len(groups) != 1: raise verify.VerificationError('UNSUPPORTED_SCHEMA')
            summary = {'name': name, 'state': sheet.get('state', 'visible'), 'rowCount': 0,
                       'cellCount': 0, 'firstRows': [],
                       'fixedSampleMatches': {sample['code']: {'cellCount': 0, 'cells': []} for sample in SAMPLES}}
            for index, row in enumerate(groups[0]):
                if verify.local(row.tag) != 'row' or not re.fullmatch(r'[1-9][0-9]{0,6}', row.get('r', '')):
                    raise verify.VerificationError('UNSUPPORTED_SCHEMA')
                number = int(row.get('r'))
                cells = [book.cell(c, number, shared, formats) for c in row if verify.local(c.tag) == 'c']
                summary['rowCount'] += 1; summary['cellCount'] += len(cells)
                if index < MAX_SCHEMA_ROWS:
                    shown = []
                    for cell in cells[:MAX_SCHEMA_CELLS]:
                        value = {key: cell[key] for key in ('reference', 'column', 'type', 'hasFormula')}
                        value['numberFormat'] = cell['numberFormat'][:80] if cell['numberFormat'] is not None else None
                        value['headerCandidateValue'] = cell['value'][:80]
                        value['headerCandidateTruncated'] = len(cell['value']) > 80
                        shown.append(value)
                    summary['firstRows'].append({'row': number, 'cellCount': len(cells),
                        'omittedCellCount': max(0, len(cells) - MAX_SCHEMA_CELLS), 'cells': shown})
                for cell in cells:
                    value = cell['value'].strip()
                    for code, matches in summary['fixedSampleMatches'].items():
                        exact = value == code
                        candidate = cell['type'] == 'n' and value == str(int(code))
                        if not exact and not candidate: continue
                        matches['cellCount'] += 1
                        if len(matches['cells']) < 8:
                            matches['cells'].append({'reference': cell['reference'], 'row': number,
                                'column': cell['column'], 'type': cell['type'],
                                'method': 'exact-six-digit-cell' if exact else 'numeric-equivalent-unverified'})
            result['sheets'].append(summary)
        return result
    finally:
        book.archive.close()


def read_catalogs(root):
    result = {}
    for relative in CATALOG_PATHS:
        path = Path(root) / relative
        if not path.exists(): result[relative] = {'status': 'missing', 'membershipKnown': False}; continue
        try:
            bound = content_limit('web/data/' + relative)
            if path.is_symlink() or path.stat().st_size > bound: raise ValueError()
            raw = path.read_bytes(); value = json.loads(raw)
            funds = value['funds']
            if not isinstance(funds, list) or len(funds) > 150000 or inspect_content(raw, bound): raise ValueError()
            codes = [row['code'] for row in funds]
            if any(not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code) for code in codes): raise ValueError()
            result[relative] = {'status': 'observed', 'membershipKnown': True, 'sourcePath': 'web/data/' + relative,
                'sourceSha256': hashlib.sha256(raw).hexdigest(), 'asOf': value.get('asOf'),
                'recordUnit': 'catalog-codes-not-independent-portfolios', 'rowCount': len(codes),
                'uniqueCodeCount': len(set(codes)), 'duplicateCodeRows': len(codes) - len(set(codes)),
                'sampleMembership': {sample['code']: sample['code'] in codes for sample in SAMPLES}}
        except (KeyError, TypeError, ValueError, OSError):
            result[relative] = {'status': 'invalid', 'membershipKnown': False}
    return result


def compare_sources(june_data, *, dates, core_properties, fetch=download_source, catalog_root='web/data'):
    result = {'schemaVersion': 1, 'status': 'partial', 'purpose': 'Find product-code gaps; do not certify a universe.',
              'sources': [], 'catalogs': read_catalogs(catalog_root), 'sampleEvidenceObservedOn': '2026-10-29',
              'productionCatalogModified': False, 'historicalUniverseVerified': False,
              'catalogCompletenessVerified': False, 'catalogCommit': os.environ.get('GITHUB_SHA')}
    try: landing = landing_evidence(fetch(INDEX_URL), dates)
    except Exception: landing = {'sourceUrl': INDEX_URL, 'fetchStatus': 'unavailable', 'bodyExplicitDates': [], 'linkedProductPeriods': []}
    result['landingPage'] = landing
    by_period = {}
    for source in SOURCES:
        data = None
        try:
            data = june_data if source['asOf'] == history.SOURCE_AS_OF else fetch(source['sourceUrl'])
            summary, records = parse_product_table(data, source, dates)
            core = core_properties(data); summary['coreProperties'] = core
            body_dates = summary['headerExplicitDates'] + core['explicitPeriodDates']
            summary['dateEvidence'] = {'filenameAsOf': source['asOf'], 'workbookExplicitDates': sorted(set(body_dates)),
                'landingBodyAndLinkCorroborates': source['asOf'] in landing['bodyExplicitDates'] and source['asOf'] in landing['linkedProductPeriods'],
                'workbookDateContradiction': bool(set(body_dates) - {source['asOf']}),
                'metadataTimestampsArePeriodEvidence': False}
            summary['aggregateMinusParsedCodes'] = source['aggregateProducts'] - summary['parsedProductCodes']
            summary['aggregateIsProductList'] = False; summary['sameScopeVerified'] = False
            summary['aggregateObservedOn'] = '2026-10-29'; summary['aggregateFetchedByThisRun'] = False
            summary['parseStatus'] = 'complete' if summary['codeRowsComplete'] and not summary['dateEvidence']['workbookDateContradiction'] else 'partial'
            if source['asOf'] == history.SOURCE_AS_OF and summary['sourceSha256'] != history.SOURCE_HASH:
                summary['parseStatus'] = 'partial'; summary['errorCode'] = 'SOURCE_HASH_MISMATCH'
            result['sources'].append(summary); by_period[source['asOf']] = records
        except Exception as error:
            failure = {**source, 'parseStatus': 'failed', 'errorCode': getattr(error, 'code', 'SOURCE_INSPECTION_FAILED')}
            if data is not None:
                try: failure['schemaDiagnostic'] = schema_diagnostic(data)
                except Exception as diagnostic_error:
                    failure['schemaDiagnostic'] = {'status': 'failed',
                        'errorCode': getattr(diagnostic_error, 'code', 'SOURCE_INSPECTION_FAILED'), 'schemaMapping': 'not-inferred'}
            result['sources'].append(failure)
    ready = len(by_period) == 2 and all(s['parseStatus'] == 'complete' for s in result['sources'])
    result['codeSetComparisonReady'] = ready
    if ready:
        june, august = by_period['2026-06-30'], by_period['2026-08-31']; common = june.keys() & august.keys()
        renamed = sorted(code for code in common if june[code]['name'] != august[code]['name'])
        dated = sorted(code for code in common if june[code]['inceptionDate'] != august[code]['inceptionDate'])
        added = sorted(august.keys() - june.keys())
        older = [code for code in added if august[code]['inceptionDate'] and august[code]['inceptionDate'] <= '2026-06-30']
        newer = [code for code in added if august[code]['inceptionDate'] and august[code]['inceptionDate'] > '2026-06-30']
        gaps = [s['aggregateMinusParsedCodes'] for s in result['sources']]
        result['comparison'] = {'commonCodeCount': len(common), 'addedCodes': added,
            'removedCodes': sorted(june.keys() - august.keys()), 'nameChangedCodes': renamed, 'inceptionChangedCodes': dated,
            'addedCount': len(august.keys() - june.keys()), 'removedCount': len(june.keys() - august.keys()),
            'addedCodesDatedOnOrBeforeJune': older, 'addedCodesDatedAfterJune': newer,
            'addedCodesWithUnresolvedInceptionCount': len(added) - len(older) - len(newer),
            'nameChangedCount': len(renamed), 'inceptionChangedCount': len(dated),
            'aggregateGapStable': gaps[0] == gaps[1], 'juneAggregateGap': gaps[0], 'augustAggregateGap': gaps[1],
            'equalGapsExplainCause': False, 'removedCodesProveLiquidation': False, 'addedCodesProveNewInception': False,
            'historicalShareUniverseVerified': False}
    result['samples'] = [{**sample, 'evidenceFetchedByThisRun': False,
        'directoryMembership': {s['asOf']: {'found': (sample['code'] in by_period[s['asOf']]
            if s['parseStatus'] == 'complete' or sample['code'] in by_period[s['asOf']] else None),
            'sourceRow': by_period[s['asOf']].get(sample['code'], {}).get('row'),
            'sourceUrl': s['sourceUrl'], 'tableComplete': s['parseStatus'] == 'complete'}
            for s in result['sources'] if s['asOf'] in by_period}}
        for sample in SAMPLES]
    return result
