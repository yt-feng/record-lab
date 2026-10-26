"""Recognize an explicit zero stock-investment row in a dated balance sheet."""
import datetime as dt
import re

TYPE = 'zero-stock-balance-sheet'
ZERO = r'(?:[-—－–]|0(?:\.0+)?)'
ROW = re.compile(r'(?m)^[ \t]*其中[：:][ \t]*股票投资[ \t]+' + ZERO + r'[ \t]+' + ZERO + r'[ \t]*$')


def valid_zero_stock(evidence):
    if not isinstance(evidence, dict) or evidence.get('evidenceType') != TYPE:
        return False
    text, period = evidence.get('text'), evidence.get('periodEnd')
    if not isinstance(text, str) or not 1 <= len(text) <= 600 or evidence.get('kind') not in ('semiannual', 'annual'):
        return False
    try:
        end = dt.date.fromisoformat(period)
    except (ValueError, TypeError):
        return False
    if end.isoformat() != period or (end.month, end.day) not in ((6, 30), (12, 31)):
        return False
    if evidence['kind'] != ('semiannual' if end.month == 6 else 'annual'):
        return False
    compact = re.sub(r'\s+', '', text)
    current = rf'{end.year}年0?{end.month}月0?{end.day}日'
    prior = rf'{end.year - 1}年12月31日'
    rows = list(ROW.finditer(text))
    return bool(re.match(r'^\d+\.\d+资产负债表', compact)
                and re.search(r'报告截止日[:：]' + current, compact)
                and '单位：人民币元' in compact
                and re.search('本期末' + current + '上年度末' + prior, compact)
                and len(rows) == 1 and len(re.findall('股票投资', compact)) == 1
                and '交易性金融资产' in compact
                and compact.index('交易性金融资产') < compact.index('股票投资')
                and rows[0].end() == len(text.rstrip()))


def parse_zero_stock(pages, report):
    for page_number, page in enumerate(pages, 1):
        for heading in re.finditer(r'(?m)^\s*\d+\.\d+\s*资\s*产\s*负\s*债\s*表\s*$', page):
            tail = page[heading.start():heading.start() + 601].strip()
            row = ROW.search(tail)
            if not row:
                continue
            evidence = {key: report.get(key) for key in ('sourceUrl', 'periodEnd', 'publishedAt', 'kind', 'title')}
            evidence.update(confirmed=True, evidenceType=TYPE, page=page_number, text=tail[:row.end()].strip())
            if valid_zero_stock(evidence):
                return evidence
    return None


DETAIL_TYPE = 'explicit-no-stock-detail'
_DETAIL_HEADING = '期末按公允价值占基金资产净值比例大小排序的所有股票投资明细'
_DETAIL_NEXT = '报告期内股票投资组合的重大变动'
_SECTION = r'\d+(?:\.\d+){1,2}'
_DETAIL_TEXT = re.compile(r'^(' + _SECTION + ')' + _DETAIL_HEADING
                          + r'本基金不投资于股票。(' + _SECTION + ')' + _DETAIL_NEXT + r'$')


def valid_explicit_no_stock(evidence):
    """A complete empty stock-detail subsection, not an investment mandate."""
    if not isinstance(evidence, dict) or evidence.get('evidenceType') != DETAIL_TYPE:
        return False
    text, period, kind = evidence.get('text'), evidence.get('periodEnd'), evidence.get('kind')
    if not isinstance(text, str) or not 1 <= len(text) <= 600 or kind not in ('quarterly', 'semiannual', 'annual'):
        return False
    try:
        end = dt.date.fromisoformat(period)
    except (ValueError, TypeError):
        return False
    allowed_ends = {'quarterly': {(3, 31), (6, 30), (9, 30), (12, 31)},
                    'semiannual': {(6, 30)}, 'annual': {(12, 31)}}
    if end.isoformat() != period or (end.month, end.day) not in allowed_ends[kind]:
        return False
    match = _DETAIL_TEXT.fullmatch(re.sub(r'\s+', '', text))
    if not match:
        return False
    current, following = (tuple(map(int, number.split('.'))) for number in match.groups())
    return len(current) == len(following) and current[:-1] == following[:-1] and following[-1] == current[-1] + 1


def parse_explicit_no_stock(pages, report):
    """Require the entire statement and immediate next heading on one page."""
    spaced = lambda value: r'\s*'.join(re.escape(char) for char in value)
    pattern = re.compile(r'(?m)^[ \t]*' + _SECTION + r'\s*' + spaced(_DETAIL_HEADING)
                         + r'\s*' + spaced('本基金不投资于股票。') + r'\s*'
                         + _SECTION + r'\s*' + spaced(_DETAIL_NEXT) + r'[ \t]*(?=\r?$)')
    for page_number, page in enumerate(pages, 1):
        for match in pattern.finditer(page):
            evidence = {key: report.get(key) for key in ('sourceUrl', 'periodEnd', 'publishedAt', 'kind', 'title')}
            evidence.update(confirmed=True, evidenceType=DETAIL_TYPE, page=page_number, text=match.group().strip())
            if valid_explicit_no_stock(evidence):
                return evidence
    return None
