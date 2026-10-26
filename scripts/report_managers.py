"""Conservative, source-cited manager attribution from a report's own table.

Input is already extracted page text, not PDF bytes. No requests, OCR, current
profile lookups, or inference from a manager's biography occur here. Empty
output means the explicit table could not establish an appointment interval;
it must not be interpreted as a fund having no manager.
"""
from __future__ import annotations

import calendar
import datetime as dt
import html
import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit


_CJK = r"\u3400-\u9fff"
_DATE = r"(?:\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})"
_TITLE = re.compile(
    r"(?m)^\s*(?P<section>4\s*\.\s*1(?:\s*\.\s*2)?)\s*"
    r"基金经理\s*(?:[（(]\s*或\s*基金经理小组\s*[）)])?\s*"
    r"(?:及\s*基金经理助理\s*)?简\s*介[^\n]*"
)
_STOP = re.compile(r"(?m)^\s*(?:4\s*\.\s*1\s*\.\s*[1-9]\b|4\s*\.\s*[2-9]\b|§\s*[5-9]\b|第\s*[五六七八九5-9]\s*[章节]|注\s*[:：])")
_ROW = re.compile(
    rf"(?m)^[ \t]*(?!姓名\s|任职日期\s|离任日期\s|证券从业)(?P<name>[{_CJK}]{{2,6}}(?:[·•][{_CJK}]{{1,6}})?)\s+"
    rf"(?P<role>[^\d\n]{{1,45}}(?:\n(?!\s*[{_CJK}]{{2,6}}\s+(?:本基金|基金经理))[^\d\n]{{1,45}}){{0,3}}?)\s*"
    rf"(?P<start>{_DATE})\s*(?P<end>{_DATE}|[-—－–]|至今|在任)"
    r"(?=\s|$)"
)
_HEADERS = ("姓名", "职务", "任职日期", "离任日期", "证券从业")
_BIOGRAPHY_WORDS = ("曾", "历任", "担任", "毕业", "加入", "学历", "学位", "硕士", "博士", "学士", "就职", "从事")
_METHOD = "explicit-report-manager-table"
_MAX_TABLE_PAGES = 6
_MAX_MANAGERS = 30


def _clean_page(text: str) -> str:
    """Keep row boundaries when given plain text, markdown, or MinerU HTML."""
    text = html.unescape(str(text or ""))
    text = re.sub(r"(?i)</?tr\b[^>]*>|</?(?:p|div)\b[^>]*>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>\n]{0,600}>", " ", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("|", " ").replace("\u3000", " ")


def _date(value: str) -> str | None:
    numbers = re.findall(r"\d+", value)
    if len(numbers) != 3:
        return None
    try:
        result = dt.date(*(int(part) for part in numbers))
        return result.isoformat() if result.year >= 1900 else None
    except (TypeError, ValueError):
        return None


def _report_interval(report: Mapping) -> tuple[str, str] | None:
    period_end = report.get("periodEnd")
    if not isinstance(period_end, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period_end):
        return None
    try:
        day = dt.date.fromisoformat(period_end)
    except ValueError:
        return None
    kind = report.get("kind", "quarterly")
    if kind == "quarterly":
        if day.month not in (3, 6, 9, 12) or day.day != calendar.monthrange(day.year, day.month)[1]:
            return None
        start = dt.date(day.year, day.month - 2, 1)
    elif kind in ("interim", "semiannual", "annual"):
        expected = "12-31" if kind == "annual" else "06-30"
        if period_end[5:] != expected:
            return None
        start = dt.date(day.year, 1, 1)
    else:
        return None
    return start.isoformat(), period_end


def _title_matches_period(pages: Sequence[str], period_end: str) -> bool:
    """If a document identifies its period, never relabel that document."""
    observed = set()
    for page in pages:
        compact = re.sub(r"\s+", "", page[:600])
        for match in re.finditer(r"(\d{4})年第([1-4一二三四])季度报告", compact):
            quarter = "一二三四".find(match[2]) + 1 if match[2] in "一二三四" else int(match[2])
            month = quarter * 3
            year = int(match[1])
            if 1900 <= year <= 9999:
                observed.add(f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}")
        for match in re.finditer(r"(\d{4})年(中期|半年度|年度)报告", compact):
            observed.add(match[1] + ("-12-31" if match[2] == "年度" else "-06-30"))
    return not observed or observed == {period_end}


def _role_allowed(role: str) -> bool:
    role = re.sub(r"\s+", "", role)
    # A job title containing 基金经理助理 is not the manager appointment table
    # role requested here. Biographical prose must never create another row.
    if role.count("基金经理") != 1 or "基金经理助理" in role or any(word in role for word in _BIOGRAPHY_WORDS):
        return False
    if "证券投资基金" in role or len(role) > 60:
        return False
    return bool(re.search(r"本基金(?:的)?基金经理", role) or role.startswith("基金经理"))


def parse_report_managers(
    pages: Sequence[str], report: Mapping, *, page_numbers: Sequence[int | None] | None = None,
) -> list[dict]:
    """Return only appointments established by explicit report table rows.

    ``pages`` must be physical PDF pages in original order when ``page_numbers``
    is omitted. For MinerU, pass its verified PDF page mapping explicitly;
    unknown mappings (``None``) are never converted to invented PDF citations.
    ``report`` supplies ``sourceUrl``, ``periodEnd``, and ``kind``. The result is
    intended for that exact report's ``managers`` field, not fund-level current
    managers. Appointments overlapping any part of the report are retained.
    """
    if not isinstance(report, Mapping) or isinstance(pages, (str, bytes)) or not pages:
        return []
    interval = _report_interval(report)
    source_url = report.get("sourceUrl")
    if not interval or not isinstance(source_url, str) or len(source_url) > 2048:
        return []
    try:
        parsed_url = urlsplit(source_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            return []
    except ValueError:
        return []
    if page_numbers is not None and len(page_numbers) != len(pages):
        return []
    numbers = list(page_numbers) if page_numbers is not None else list(range(1, len(pages) + 1))
    cleaned = [_clean_page(page) for page in pages]
    start, end = interval
    if not _title_matches_period(cleaned, end):
        return []
    managers: dict[tuple[str, str, str | None], dict] = {}
    for first_page, text in enumerate(cleaned):
        for heading in _TITLE.finditer(text):
            # Ignore the table of contents: headers have to occur directly
            # after this heading, before actual rows, in a bounded prefix.
            fragments = [text[heading.end():]] + cleaned[first_page + 1:first_page + _MAX_TABLE_PAGES]
            joined = "\n".join(fragments)
            stop = _STOP.search(joined)
            body = joined[:stop.start()] if stop else joined
            header_matches = [re.search(r"\s*".join(re.escape(char) for char in header), body[:1200])
                              for header in _HEADERS]
            tenure_header = re.search(r"任\s*本\s*基\s*金\s*的\s*基\s*金\s*经\s*理\s*(?:[（(]\s*助\s*理\s*[）)]\s*)?期\s*限", body[:1200])
            if not all(header_matches) or not tenure_header:
                continue
            header_end = max(match.end() for match in [*header_matches, tenure_header])
            offsets = []
            offset = 0
            for index, fragment in enumerate(fragments):
                offsets.append((offset, first_page + index))
                offset += len(fragment) + 1
            for match in _ROW.finditer(body, header_end):
                role = re.sub(r"\s+", "", match["role"])
                if not _role_allowed(role):
                    continue
                appointment = _date(match["start"])
                departure = _date(match["end"])
                if not appointment or (match["end"] not in ("-", "—", "－", "–", "至今", "在任") and not departure):
                    continue
                if appointment > end or departure and (departure < start or departure < appointment):
                    continue
                physical_indices = [index for offset, index in offsets if offset <= match.start()]
                index = physical_indices[-1]
                page = numbers[index]
                if type(page) is not int or page < 1:
                    continue
                table_page = numbers[first_page]
                if type(table_page) is not int or table_page < 1 or any(
                    type(numbers[i]) is not int or numbers[i] != table_page + i - first_page
                    for i in range(first_page, index + 1)
                ):
                    continue
                # Do not cite one page for a row whose appointment/date fields
                # straddle unknown or nonconsecutive PDF pages.
                row_pages = [numbers[i] for offset, i in offsets if match.start() < offset < match.end()]
                if any(type(value) is not int or value != page + step for step, value in enumerate(row_pages, 1)):
                    continue
                name = match["name"]
                key = (name, appointment, departure)
                managers.setdefault(key, {
                    "name": name, "startDate": appointment, "endDate": departure,
                    "role": role, "periodStart": start, "periodEnd": end,
                    "activeAtPeriodEnd": departure is None or departure >= end,
                    "evidence": {
                        "sourceUrl": source_url, "page": page, "pages": [page, *row_pages], "tablePage": table_page,
                        "section": re.sub(r"\s+", "", heading["section"]),
                        "text": re.sub(r"\s+", " ", match.group()).strip()[:240],
                        "method": _METHOD, "periodEnd": end,
                    },
                })
                if len(managers) > _MAX_MANAGERS:
                    return []
    return list(managers.values())
