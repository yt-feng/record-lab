"""Anchor a report period to its opening title, not its disclosure history.

The public evidence remains a short period token and physical page. This
module does not infer a period from filenames, report metadata, financial
comparison columns, or incidental references to earlier reports.
"""
from __future__ import annotations

import html
import re

PERIOD = re.compile(r'(2\s*0\s*\d\s*\d)\s*年\s*(?:第?\s*([1-4一二三四])\s*季\s*度|(中\s*期|半\s*年\s*度)|(年\s*度))\s*报\s*告')
_COUNTER = re.compile(r'^(?:第\d+页(?:共\d+页)?|\d+/\d+)$')
_REFERENCE = re.compile(r'参见|详见|查阅|根据|关于|公告|披露|目录|历史|发布|更新|附录|附件|以下简称|重要提示')
_SECTION = re.compile(r'^(?:§\s*\d|\d{1,2}\s*[.．]\s*\d|第[一二三四五六七八九十\d]+[章节])')
_NAME = re.compile(r'^[\u3400-\u9fffA-Za-z0-9（）()·－—\-]{1,160}$')
_TITLE_SUFFIXES = tuple(f'年{ordinal}{quarter}季度报告'
                        for ordinal in ('', '第') for quarter in '1234一二三四') + (
                            '年中期报告', '年半年度报告', '年年度报告')


def period_token(text):
    match = PERIOD.fullmatch(re.sub(r'\s+', '', str(text or '')))
    if not match:
        return None
    year, quarter, half, annual = match.groups()
    if quarter:
        index = int(quarter) - 1 if quarter.isdigit() else '一二三四'.index(quarter)
        return 'quarterly', year + '-' + ('03-31', '06-30', '09-30', '12-31')[index]
    return ('semiannual', year + '-06-30') if half else ('annual', year + '-12-31')


def _lines(raw):
    text = html.unescape(str(raw or ''))
    text = re.sub(r'(?i)<br\s*/?>|</?(?:p|div|tr)\b[^>]*>', '\n', text)
    text = re.sub(r'<[^>\n]{0,600}>', ' ', text)
    lines = [re.sub(r'^\s*#{1,6}\s*', '', line).strip(' \t|') for line in text.splitlines()]
    return [line for line in lines if line and not _COUNTER.fullmatch(re.sub(r'\s+', '', line))]


def _partial_title(raw):
    """Only an unfinished report-period token may consume another title line."""
    compact = re.sub(r'\s+', '', raw)
    for match in re.finditer(r'20\d{2}', compact):
        lead, tail = compact[:match.start()], compact[match.end():]
        if (not lead or _NAME.fullmatch(lead) and '基金' in lead) and any(
                suffix.startswith(tail) for suffix in _TITLE_SUFFIXES):
            return True
    return False


def _opening_titles(raw, *, header_only=False):
    prefix, result = [], []
    lines = _lines(raw)[:6 if header_only else 12]
    index = 0
    while index < len(lines):
        line = lines[index]
        compact = re.sub(r'\s+', '', line)
        if len(line) > 240 or _REFERENCE.search(compact) or _SECTION.match(compact):
            break
        match = PERIOD.search(line)
        consumed = index
        if not match and _partial_title(line):
            candidate = line
            # PDF covers can wrap a token, but section/history boundaries
            # still terminate the opening block before any lines are joined.
            for following in range(index + 1, min(len(lines), index + 6)):
                next_line = lines[following]
                next_compact = re.sub(r'\s+', '', next_line)
                if (len(candidate) + len(next_line) + 1 > 240
                        or _REFERENCE.search(next_compact) or _SECTION.match(next_compact)):
                    break
                candidate += '\n' + next_line
                match = PERIOD.search(candidate)
                if match:
                    line, consumed = candidate, following
                    break
                if not _partial_title(candidate):
                    break
        if match:
            lead = re.sub(r'\s+', '', line[:match.start()])
            if line[match.end():].strip() or lead and (not _NAME.fullmatch(lead) or '基金' not in lead):
                break
            # A full fund title is also a valid running header. A bare period
            # token is accepted only in the opening block, not on late pages.
            if header_only and not lead and (not prefix or '基金' not in ''.join(prefix)):
                break
            if not lead and prefix and '基金' not in ''.join(prefix):
                break
            if not lead and not prefix and index > 0 and not result:
                break
            result.append(match[0])
            if header_only:
                break
        elif not result and _NAME.fullmatch(compact):
            prefix.append(compact)
        else:
            break
        index = consumed + 1
    return result


def document_periods(pages, numbers=None, *, preserve_spacing=False):
    """Read opening titles and strict page-opening fund-title headers.

    History rows following a section/table heading or a running header cannot
    add competing periods. A genuinely different later cover/header remains
    conflicting evidence, so concatenated reports do not become one report.
    """
    pages = list(pages)
    numbers = list(range(1, len(pages) + 1)) if numbers is None else list(numbers)
    candidates = [(index, title) for index, raw in enumerate(pages[:2]) for title in _opening_titles(raw)]
    candidates.extend((index, title) for index, raw in enumerate(pages[2:], 2)
                      for title in _opening_titles(raw, header_only=True))
    observed = {}
    for index, title in candidates:
        kind, period = period_token(title)
        page = numbers[index] if index < len(numbers) else None
        key = kind, period
        if key not in observed or type(observed[key]['page']) is not int:
            observed[key] = {'periodEnd': period, 'kind': kind, 'page': page,
                'text': title if preserve_spacing else re.sub(r'\s+', '', title)}
    return list(observed.values())[:8]


def has_period_reference(pages):
    return any(PERIOD.search(str(page or '')) for page in pages)
