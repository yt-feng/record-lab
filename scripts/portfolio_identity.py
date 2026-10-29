"""Report-local proof of explicitly listed fund codes, never class inference."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from urllib.parse import urlsplit, parse_qsl
from source_dates import china_date
from report_period import document_periods

METHOD = 'pdf-explicit-fund-code-rows-v1'
REIT_METHOD = 'pdf-explicit-reit-code-rows-v1'
PERIOD = re.compile(r'(20\d{2})\s*年\s*(?:第\s*([1-4一二三四])\s*季度|(中期|半年度|年度))\s*报告')
HEADINGS = re.compile(r'(?m)^[ \t|]*(?P<text>(?:§\s*2\s*(?:基金产品概况|不动产\s*基金产品概况|不动产\s*基金简介)|2\.1\s*(?:基金基本情况|不动产\s*基金产品基本情况)|§\s*3\s*[^\n|]+|2\.2\s*[^\n|]+))[ \t|]*$')
LABELS = {'mainCode': ['基金主代码'], 'shareClassCodes': ['下属分级基金的交易代码', '下属分类基金的交易代码']}
REIT_LABELS = {'mainCode': ['基金代码'], 'tradingCode': ['交易代码']}
REIT_PARENT = '§2不动产基金产品概况'
REIT_PARENTS = {'quarterly': REIT_PARENT, 'semiannual': '§2不动产基金简介'}
REIT_CHILD = '2.1不动产基金产品基本情况'
REIT_END = '2.2不动产项目基本情况说明'


def _date(value):
    try: return isinstance(value, str) and dt.date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError): return False


def _instant(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if 'T' in value and parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError): return None


def _url(value):
    if not isinstance(value, str) or len(value) > 2048: return False
    try:
        url = urlsplit(value); host = url.hostname or ''
        return (url.scheme in {'http', 'https'} and '.' in host and ':' not in host and not re.fullmatch(r'\d+(?:\.\d+){3}', host)
                and not host.endswith(('.local', '.localhost')) and not url.username and not url.password and not url.fragment
                and not any(re.search(r'token|secret|password|signature|authorization|credential|key', key, re.I) for key, _ in parse_qsl(url.query)))
    except ValueError: return False


def _hash(value): return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None
def _page(value): return type(value) is int and 1 <= value <= 10000
def _spaced(value): return r'\s*'.join(re.escape(char) for char in value)


def _period(text):
    match = PERIOD.fullmatch(re.sub(r'\s+', '', str(text or '')))
    if not match: return None
    year, quarter, annual = match.groups()
    if quarter:
        quarter = int(quarter) if quarter.isdigit() else '一二三四'.index(quarter) + 1
        return ('quarterly', f'{year}-{("03-31", "06-30", "09-30", "12-31")[quarter-1]}')
    return ('annual', year + '-12-31') if annual == '年度' else ('semiannual', year + '-06-30')


def _row_values(text, field, labels=LABELS):
    if not isinstance(text, str) or not 1 <= len(text) <= 350: return None
    patterns = '|'.join(_spaced(label) for label in labels[field])
    match = re.fullmatch(r'[ \t|]*(?:' + patterns + r')[ \t|：:]*\s*(\d{6}(?:[\s|]+\d{6})*)[\s|]*', text)
    if not match: return None
    codes = re.findall(r'\d{6}', match[1])
    if not codes or len(codes) > 20 or len(codes) != len(set(codes)) or field in {'mainCode', 'tradingCode'} and len(codes) != 1: return None
    return codes


def normalize_portfolio_identity(raw, report=None, *, now=None):
    current = _instant(now) if isinstance(now, str) else now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(current, dt.datetime) or current.tzinfo is None: return None
    if (not isinstance(raw, dict) or type(raw.get('schemaVersion')) is not int or raw.get('schemaVersion') != 1
            or raw.get('method') not in (METHOD, REIT_METHOD) or raw.get('scope') != 'explicitly-listed-fund-codes'
            or raw.get('shareClassUniverseComplete') is not False or raw.get('shareClassSpecificMetricsProvided') is not False
            or not _url(raw.get('sourceUrl')) or not _date(raw.get('periodEnd')) or not _date(raw.get('publishedAt'))
            or raw['publishedAt'] < raw['periodEnd'] or not _hash(raw.get('pdfSha256')) or not _hash(raw.get('sourceTextHash'))
            or raw.get('textHashScope') != 'provided-extracted-pages'):
        return None
    observed = _instant(raw.get('retrievedAt'))
    if not observed or observed > current + dt.timedelta(minutes=5) or china_date(observed).isoformat() < raw['publishedAt']: return None
    if report is not None and any(raw.get(key) != report.get(key) for key in ('sourceUrl', 'periodEnd', 'publishedAt', 'kind')): return None
    title = raw.get('reportPeriodEvidence')
    if (not isinstance(title, dict) or not _page(title.get('page')) or not isinstance(title.get('text'), str)
            or len(title['text']) > 80 or _period(title['text']) != (raw.get('kind'), raw['periodEnd'])): return None
    table = raw.get('identitySection')
    if not isinstance(table, dict): return None
    pages = table.get('pages')
    if (not isinstance(pages, list) or not 1 <= len(pages) <= 5 or not all(_page(page) for page in pages)
            or pages != list(range(pages[0], pages[-1] + 1)) or table.get('startPage') != pages[0] or table.get('endPage') != pages[-1]
            or table.get('pageNumbersVerified') is not True or table.get('sectionBounded') is not True): return None
    heading, end = table.get('heading'), table.get('endHeading')
    if not isinstance(heading, str) or len(heading) > 160 or not isinstance(end, str) or len(end) > 160: return None
    compact = lambda value: re.sub(r'\s+', '', value) if isinstance(value, str) else ''
    is_reit = raw['method'] == REIT_METHOD
    if is_reit:
        if (raw.get('kind') not in ('quarterly', 'semiannual') or compact(heading) != REIT_PARENTS[raw['kind']] or compact(end) != REIT_END
                or not isinstance(table.get('subheading'), str) or len(table['subheading']) > 160
                or compact(table['subheading']) != REIT_CHILD or not _page(table.get('subheadingPage'))
                or table['subheadingPage'] != pages[0]
                or table.get('subheadingAdjacent') is not True): return None
    elif not ((compact(heading) == '§2基金产品概况' and re.fullmatch(r'§3[^\d]{2,100}', compact(end)))
              or (compact(heading) == '2.1基金基本情况' and re.fullmatch(r'2\.2[^\d]{2,100}', compact(end)))): return None
    labels = REIT_LABELS if is_reit else LABELS
    rows = raw.get('rows')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2: return None
    parsed, cleaned = {}, []
    for row in rows:
        if not isinstance(row, dict) or row.get('field') not in labels or row['field'] in parsed: return None
        codes = _row_values(row.get('text'), row['field'], labels)
        rp = row.get('pages')
        if (not codes or codes != row.get('codes') or not isinstance(rp, list) or not rp or not all(_page(p) and p in pages for p in rp)
                or rp != list(range(rp[0], rp[-1] + 1)) or row.get('page') != rp[0]): return None
        parsed[row['field']] = codes
        cleaned.append({'field': row['field'], 'text': row['text'], 'codes': codes, 'page': rp[0], 'pages': rp})
    if 'mainCode' not in parsed: return None
    main = parsed['mainCode'][0]; classes = parsed.get('shareClassCodes', [])
    if is_reit and parsed.get('tradingCode') != [main]: return None
    if classes and main not in classes: return None
    if main != raw.get('sourcePortfolioCode') or classes != raw.get('shareClassCodes'): return None
    if report is not None and any(report.get(key) is not None and report[key] not in [main, *classes] for key in ('fundCode', 'sourceFundCode')): return None
    table_fields = ('heading', 'endHeading', 'startPage', 'endPage', 'pages', 'sectionBounded', 'pageNumbersVerified')
    if is_reit: table_fields += ('subheading', 'subheadingPage', 'subheadingAdjacent')
    return {'schemaVersion': 1, 'method': raw['method'], 'scope': 'explicitly-listed-fund-codes',
            **{key: raw[key] for key in ('sourceUrl', 'periodEnd', 'publishedAt', 'kind', 'retrievedAt', 'pdfSha256', 'sourceTextHash', 'textHashScope')},
            'reportPeriodEvidence': {'text': title['text'], 'page': title['page']},
            'identitySection': {key: table[key] for key in table_fields},
            'rows': cleaned, 'sourcePortfolioCode': main, 'shareClassCodes': classes,
            'eligibleForShareClassExpansion': bool(classes), 'shareClassUniverseComplete': False, 'shareClassSpecificMetricsProvided': False}


def parse_portfolio_identity(pages, report, *, page_numbers=None, retrieved_at=None, pdf_sha256=None, now=None, diagnostics=None):
    """Only bounded identity-section rows from known physical PDF pages."""
    def reject(reason, field=None):
        # Optional verifier-only sink: fixed branch identifiers, never raw text.
        # Recording a rejection must not alter the existing acceptance rules.
        if isinstance(diagnostics, dict):
            diagnostics.update(branch=reason, field=field)
        return None
    if not isinstance(pages, list) or not isinstance(report, dict): return reject('INVALID_INPUT')
    numbers = list(range(1, len(pages)+1)) if page_numbers is None else page_numbers
    if not isinstance(numbers, list) or len(numbers) != len(pages): return reject('PAGE_MAPPING_LENGTH_MISMATCH')
    chunks, positions = [], []
    for text, number in zip(pages[:12], numbers[:12]):
        value = str(text or '') + '\n'; chunks.append(value); positions.extend([number] * len(value))
    text = ''.join(chunks)
    periods = [(item['text'], item['page']) for item in document_periods(pages[:12], numbers[:12], preserve_spacing=True)]
    expected = (report.get('kind'), report.get('periodEnd'))
    if not periods: return reject('PERIOD_TITLE_NOT_FOUND')
    if any(_period(value) != expected for value, _ in periods): return reject('PERIOD_TITLE_MISMATCH')
    title = next(({'text': value, 'page': page} for value, page in periods if _page(page)), None)
    if not title: return reject('PERIOD_TITLE_PAGE_UNKNOWN')
    headings = list(HEADINGS.finditer(text))
    compact = lambda value: re.sub(r'\s+', '', value)
    starts = [item for item in headings if compact(item['text']) in {'§2基金产品概况', '2.1基金基本情况', *REIT_PARENTS.values(), REIT_CHILD}]
    is_reit = any(compact(item['text']) in {*REIT_PARENTS.values(), REIT_CHILD} for item in starts)
    subheading = None
    # A quarterly product overview may contain an immediately nested 2.1
    # heading. This pair describes one section, not two independent tables.
    # Only whitespace/table separators may intervene; repeated tables, a TOC
    # followed by a real section, or identity data before the child stay rejected.
    if len(starts) == 2:
        parent, child = starts
        if ((report.get('kind') not in REIT_PARENTS if is_reit else report.get('kind') != 'quarterly')
                or compact(parent['text']) != (REIT_PARENTS.get(report.get('kind')) if is_reit else '§2基金产品概况')
                or compact(child['text']) != (REIT_CHILD if is_reit else '2.1基金基本情况')
                or text[parent.end():child.start()].strip(' \t\r\n|')):
            return reject('IDENTITY_SECTION_START_COUNT')
        start = parent
        if is_reit:
            if positions[parent.start()] != positions[child.start()]: return reject('IDENTITY_SECTION_PAGE_MAPPING_INVALID')
            subheading = child
    elif len(starts) == 1 and not is_reit:
        start = starts[0]
    else:
        return reject('IDENTITY_SECTION_START_COUNT')
    is_quarter = compact(start['text']) == '§2基金产品概况'
    closing = next((item for item in headings if item.start() > start.end()
                    and compact(item['text']).startswith('§3' if is_quarter else '2.2')), None)
    if not closing: return reject('IDENTITY_SECTION_END_NOT_FOUND')
    if is_reit and compact(closing['text']) != REIT_END: return reject('IDENTITY_SECTION_END_NOT_FOUND')
    section_pages = list(dict.fromkeys(positions[start.start():closing.end()]))
    if (not section_pages or not all(_page(page) for page in section_pages) or len(section_pages) > 5
            or section_pages != list(range(section_pages[0], section_pages[-1]+1))): return reject('IDENTITY_SECTION_PAGE_MAPPING_INVALID')
    body = text[start.end():closing.start()]; rows = []
    labels_by_field = REIT_LABELS if is_reit else LABELS
    if is_reit and re.search(r'(?m)^[ \t|]*(?:' + '|'.join(_spaced(label) for labels in LABELS.values() for label in labels) + r')[ \t|：:]*', body):
        return reject('REIT_UNEXPECTED_CODE_LABEL')
    for field, labels in labels_by_field.items():
        pattern = re.compile(r'(?m)^[ \t|]*(?:' + '|'.join(_spaced(label) for label in labels) + r')[ \t|：:]*')
        matches = list(pattern.finditer(body))
        if len(matches) > 1: return reject('DUPLICATE_CODE_LABEL', field)
        if not matches:
            if field == 'mainCode': return reject('MAIN_CODE_LABEL_NOT_FOUND', field)
            if field == 'tradingCode': return reject('TRADING_CODE_LABEL_NOT_FOUND', field)
            continue
        match = matches[0]; cursor = match.end(); end = cursor
        candidate_lines = body[cursor:].splitlines(keepends=True)
        for line in candidate_lines[:8]:
            if not line.strip(' \t|\r\n'):
                end += len(line); continue
            if not re.fullmatch(r'[ \t|]*\d{6}(?:[ \t|]+\d{6})*[ \t|]*(?:\r?\n)?', line): break
            end += len(line)
        if end >= cursor + sum(len(line) for line in candidate_lines[:8]) and len(candidate_lines) > 8:
            if re.fullmatch(r'[\s|]*\d{6}(?:[\s|]+\d{6})*[\s|]*', candidate_lines[8]): return reject('CODE_ROWS_EXCEED_BOUND', field)
        quote = body[match.start():end].strip(); codes = _row_values(quote, field, labels_by_field)
        if not codes: return reject('CODE_ROW_INVALID', field)
        offset = start.end() + match.start(); last = start.end() + end
        row_pages = list(dict.fromkeys(positions[offset:last]))
        rows.append({'field': field, 'text': quote, 'codes': codes, 'page': row_pages[0], 'pages': row_pages})
    main = next(row['codes'][0] for row in rows if row['field'] == 'mainCode')
    classes = next((row['codes'] for row in rows if row['field'] == 'shareClassCodes'), [])
    if is_reit and next(row['codes'][0] for row in rows if row['field'] == 'tradingCode') != main:
        return reject('REIT_CODE_MISMATCH')
    proof = {'schemaVersion': 1, 'method': REIT_METHOD if is_reit else METHOD, 'scope': 'explicitly-listed-fund-codes',
             **{key: report.get(key) for key in ('sourceUrl', 'periodEnd', 'publishedAt', 'kind')},
             'retrievedAt': retrieved_at, 'pdfSha256': pdf_sha256,
             'sourceTextHash': hashlib.sha256('\n\n'.join(str(page or '').strip() for page in pages if str(page or '').strip()).encode()).hexdigest(),
             'textHashScope': 'provided-extracted-pages', 'reportPeriodEvidence': title,
             'identitySection': {'heading': start['text'].strip(), 'endHeading': closing['text'].strip(),
                 'startPage': section_pages[0], 'endPage': section_pages[-1], 'pages': section_pages,
                 'sectionBounded': True, 'pageNumbersVerified': True},
             'rows': rows, 'sourcePortfolioCode': main, 'shareClassCodes': classes,
             'shareClassUniverseComplete': False, 'shareClassSpecificMetricsProvided': False}
    if is_reit:
        proof['identitySection'].update(subheading=subheading['text'].strip(), subheadingPage=positions[subheading.start()],
                                        subheadingAdjacent=True)
    clean = normalize_portfolio_identity(proof, report, now=now)
    if clean is None: return reject('PROOF_VALIDATION_REJECTED')
    if isinstance(diagnostics, dict): diagnostics.update(branch='VERIFIED', field=None)
    return clean
