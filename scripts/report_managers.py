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
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit
from report_period import document_periods, has_period_reference


_CJK = r"\u3400-\u9fff"
_DATE = r"(?:\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})"
_TITLE = re.compile(
    r"(?m)^\s*(?P<section>(?:4\s*\.\s*1(?:\s*\.\s*2)?|7\s*\.\s*1\s*\.\s*2))\s*"
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
_REIT_HEADERS = ("姓名", "职务", "任职日期", "离任日期")
_REIT_EXPERIENCE = re.compile(r"不\s*动\s*产\s*项\s*目\s*运\s*营\s*或\s*投\s*资\s*管\s*理\s*(?:年限\s*/\s*经验|年限|经验)(?!年限)")
_BIOGRAPHY_WORDS = ("曾", "历任", "担任", "毕业", "加入", "学历", "学位", "硕士", "博士", "学士", "就职", "从事")
_METHOD = "explicit-report-manager-table"
_MAX_TABLE_PAGES = 6
_MAX_MANAGERS = 30
PARSER_VERSION = 2
_CANDIDATE = re.compile(
    rf"(?m)^[ \t]*(?!姓名\s|任职日期\s|离任日期\s|证券从业)(?P<name>[{_CJK}]{{1,16}}(?:[·•][{_CJK}]{{1,16}})?|[A-Za-z][A-Za-z .'-]{{1,60}})\s+"
    rf"(?P<role>[^\d\n]{{1,90}}(?:\n(?!\s*(?:[{_CJK}]{{1,16}}|[A-Za-z][A-Za-z .'-]{{1,60}})\s+(?:本基金|基金经理))[^\d\n]{{1,90}}){{0,3}})"
)
_SECTION_STOP = re.compile(r"(?m)^\s*(?P<section>4\s*\.\s*[1-9](?:\s*\.\s*[1-9])?|[5-9](?:\s*\.\s*\d+){0,2}(?=\s+(?:投资组合|报告期|基金|期末|备查|资产|管理人)))\s+[^\n]*")
_COUNT_FIELDS = ("candidateRows", "parsedRows", "ignoredKnownRoleRows", "excludedRows", "outsidePeriodRows", "duplicateRows")


class ManagerRows(list):
    """Valid individual appointments plus a separate whole-table observation."""
    def __init__(self, rows=(), coverage=None):
        super().__init__(rows)
        self.coverage = coverage or {}


def manager_key(manager):
    return json.dumps([manager["name"], manager["startDate"], manager["endDate"], manager["role"]],
                      ensure_ascii=False, separators=(",", ":"))


def empty_coverage(pages, report):
    return {"schemaVersion": 1, "parserVersion": PARSER_VERSION, "extractionMethod": "plain-text-manager-table",
            "sourceUrl": report.get("sourceUrl"), "periodEnd": report.get("periodEnd"),
            "kind": report.get("kind", "quarterly"), "periodStart": None, "publishedAt": report.get("publishedAt"),
            "sourceTextHash": hashlib.sha256("\n\f\n".join(map(str, pages)).encode()).hexdigest(),
            "tables": [], **dict.fromkeys(_COUNT_FIELDS, 0), "outputManagerCount": 0, "managerKeys": [],
            "complete": False, "status": "missing", "method": "bounded-report-manager-tables"}


def normalize_manager_table_coverage(raw, managers=None, period_end=None):
    """Validate bounded table coverage independently of valid individual rows."""
    if (not isinstance(raw, Mapping) or raw.get("schemaVersion") != 1
            or raw.get("method") != "bounded-report-manager-tables"
            or period_end is not None and raw.get("periodEnd") != period_end
            or not re.fullmatch(r"[a-f0-9]{64}", str(raw.get("sourceTextHash", "")))):
        return None
    interval = _report_interval(raw)
    if not interval or raw.get("periodStart") != interval[0]:
        return None
    try:
        source = urlsplit(raw.get("sourceUrl", ""))
        if source.scheme not in ("http", "https") or not source.hostname or source.username or source.password:
            return None
    except ValueError:
        return None
    counts = (*_COUNT_FIELDS, "outputManagerCount")
    if any(type(raw.get(key)) is not int or not 0 <= raw[key] <= 10000 for key in counts):
        return None
    keys = raw.get("managerKeys")
    tables = raw.get("tables")
    if (not isinstance(keys, list) or len(keys) > _MAX_MANAGERS or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)
            or not isinstance(tables, list) or len(tables) > 30):
        return None
    try:
        for key in keys:
            value = json.loads(key)
            if (not isinstance(value, list) or len(value) != 4 or not isinstance(value[0], str) or not 1 <= len(value[0]) <= 100
                    or not _date(value[1]) or value[2] is not None and not _date(value[2])
                    or not isinstance(value[3], str) or not _role_allowed(value[3])
                    or value[1] > interval[1] or value[2] is not None and (value[2] < value[1] or value[2] < interval[0])):
                return None
    except (ValueError, TypeError):
        return None
    cleaned, table_complete = [], []
    for table in tables:
        if (not isinstance(table, Mapping) or not re.fullmatch(r"(?:4\.1(?:\.2)?|7\.1\.2)", str(table.get("section", "")))
                or any(type(table.get(key)) is not int or not 0 <= table[key] <= 10000 for key in _COUNT_FIELDS)):
            return None
        section, following = table["section"], table.get("endSection")
        forward = isinstance(following, str) and re.fullmatch(r"\d+(?:\.\d+){0,2}", following) and tuple(map(int, following.split('.'))) > tuple(map(int, section.split('.')))
        start_page, end_page = table.get("startPage"), table.get("endPage")
        pages = type(start_page) is int and type(end_page) is int and 1 <= start_page <= end_page <= 10000
        heading, ending = table.get("heading"), table.get("endHeading")
        headings = (isinstance(heading, str) and len(heading) <= 240 and "基金经理" in re.sub(r"\s+", "", heading)
                    and re.sub(r"\s+", "", heading).startswith(section)
                    and isinstance(ending, str) and len(ending) <= 240 and forward and re.sub(r"\s+", "", ending).startswith(following))
        ignored = table.get("ignoredRoles", [])
        ignored_valid = isinstance(ignored, list) and len(ignored) == table["ignoredKnownRoleRows"]
        if ignored_valid:
            for item in ignored:
                ignored_valid = (isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("role"), str)
                    and _ignored_role_allowed(item["name"], item["role"])
                    and type(item.get("page")) is int and pages and start_page <= item["page"] <= end_page
                    and isinstance(item.get("text"), str) and 1 <= len(item["text"]) <= 240
                    and re.sub(r"\s+", "", item["text"]).startswith(item["name"]) and item["role"] in re.sub(r"\s+", "", item["text"]))
                if not ignored_valid:
                    break
        balanced = table["candidateRows"] == table["parsedRows"] + table["ignoredKnownRoleRows"] + table["excludedRows"]
        table_complete.append(bool(forward and pages and headings and ignored_valid and balanced and table["excludedRows"] == 0
                                   and table.get("sectionBounded") is True and table.get("pageNumbersVerified") is True
                                   and table.get("pageLimitReached") is False))
        clean_table = {key: table.get(key) for key in ("section", "startPage", "endPage", "endSection", "heading", "endHeading",
                         "sectionBounded", "pageNumbersVerified", "pageLimitReached", *_COUNT_FIELDS)}
        clean_table["ignoredRoles"] = [{key: item.get(key) for key in ("name", "role", "page", "text")} for item in ignored if isinstance(item, dict)] if isinstance(ignored, list) else []
        cleaned.append(clean_table)
    aligned = raw["outputManagerCount"] == len(keys)
    if managers is not None:
        try:
            aligned = aligned and isinstance(managers, (list, tuple)) and sorted(manager_key(item) for item in managers) == sorted(keys)
            aligned = aligned and all(item.get("evidence", {}).get("sourceUrl") == raw["sourceUrl"] and item.get("periodEnd") == raw["periodEnd"] for item in managers)
        except (TypeError, KeyError, AttributeError):
            aligned = False
    published = _date(raw.get("publishedAt"))
    parser_version = raw.get("parserVersion", 1)
    extraction = raw.get("extractionMethod", "plain-text-manager-table")
    geometry = None
    if extraction == "pdf-clipping-manager-table":
        from manager_cells import normalize_geometry_evidence
        geometry = normalize_geometry_evidence(raw.get("geometryEvidence"), raw)
    method_valid = extraction == "plain-text-manager-table" or extraction == "pdf-clipping-manager-table" and geometry is not None
    complete = bool(parser_version == PARSER_VERSION and method_valid and published and published >= raw["periodEnd"] and raw.get("complete") is True and aligned and keys and tables and all(table_complete)
                    and all(raw[key] == sum(table[key] for table in tables) for key in _COUNT_FIELDS)
                    and raw["candidateRows"] == raw["parsedRows"] + raw["ignoredKnownRoleRows"]
                    and raw["parsedRows"] == raw["outputManagerCount"] + raw["outsidePeriodRows"] + raw["duplicateRows"])
    return {"schemaVersion": 1, "parserVersion": parser_version if type(parser_version) is int else 1,
            "extractionMethod": extraction, **({"geometryEvidence": geometry} if geometry is not None else {}),
            "sourceUrl": raw["sourceUrl"], "periodStart": raw["periodStart"], "periodEnd": raw["periodEnd"],
            "kind": raw["kind"], "publishedAt": published, "sourceTextHash": raw["sourceTextHash"], "method": raw["method"],
            "tables": cleaned, **{key: raw[key] for key in counts}, "managerKeys": sorted(keys),
            "complete": complete, "status": "complete" if complete else "partial" if keys or tables else "missing"}


def _clean_page(text: str) -> str:
    """Keep row boundaries when given plain text, markdown, or MinerU HTML."""
    text = html.unescape(str(text or ""))
    text = re.sub(r"(?i)</?tr\b[^>]*>|</?(?:p|div)\b[^>]*>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>\n]{0,600}>", " ", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("|", " ").replace("\u3000", " ")


def _date(value: str) -> str | None:
    if not isinstance(value, str):
        return None
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


def _title_matches_period(pages: Sequence[str], period_end: str, kind: str) -> bool:
    """Bind the opening report title; incidental disclosure dates do not vote."""
    observed = document_periods(pages)
    expected = 'semiannual' if kind == 'interim' else kind
    if not observed:
        # Preserve support for title-free bounded table extracts, but a list
        # of historical report names cannot serve as the current document.
        return not has_period_reference(pages)
    return len(observed) == 1 and (observed[0]['kind'], observed[0]['periodEnd']) == (expected, period_end)


def _role_allowed(role: str) -> bool:
    role = re.sub(r"\s+", "", role)
    # A job title containing 基金经理助理 is not the manager appointment table
    # role requested here. Biographical prose must never create another row.
    if role.count("基金经理") != 1 or "基金经理助理" in role or any(word in role for word in _BIOGRAPHY_WORDS):
        return False
    if "证券投资基金" in role or len(role) > 60:
        return False
    return bool(re.search(r"本基金(?:的)?基金经理", role) or role.startswith("基金经理")
                or re.search(r"(?:部|中心|团队)[-－—/、兼]基金经理$", role))


def _ignored_role_allowed(name, role):
    if not re.fullmatch(rf'[{_CJK}]{{2,6}}(?:[·•][{_CJK}]{{1,6}})?', name):
        return False
    return (role in {"董事长", "公司总经理", "总经理", "投资总监"}
            or role.count("基金经理") == 1 and "基金经理助理" in role
            and not any(word in role for word in _BIOGRAPHY_WORDS))


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
        return ManagerRows()
    coverage = empty_coverage(pages, report)
    interval = _report_interval(report)
    source_url = report.get("sourceUrl")
    if not interval or not isinstance(source_url, str) or len(source_url) > 2048:
        return ManagerRows(coverage=coverage)
    try:
        parsed_url = urlsplit(source_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            return ManagerRows(coverage=coverage)
    except ValueError:
        return ManagerRows(coverage=coverage)
    if page_numbers is not None and len(page_numbers) != len(pages):
        return ManagerRows(coverage=coverage)
    numbers = list(page_numbers) if page_numbers is not None else list(range(1, len(pages) + 1))
    cleaned = [_clean_page(page) for page in pages]
    start, end = interval
    if not _title_matches_period(cleaned, end, report.get('kind', 'quarterly')):
        return ManagerRows(coverage=coverage)
    coverage["periodStart"] = start
    managers: dict[tuple[str, str, str | None], dict] = {}
    consumed = []
    for first_page, text in enumerate(cleaned):
        for heading in _TITLE.finditer(text):
            section = re.sub(r"\s+", "", heading["section"])
            if any(begin <= first_page <= finish and prior == section for begin, finish, prior in consumed):
                continue
            # Ignore the table of contents: headers have to occur directly
            # after this heading, before actual rows, in a bounded prefix.
            fragments = [text[heading.end():]] + cleaned[first_page + 1:first_page + _MAX_TABLE_PAGES]
            joined = "\n".join(fragments)
            structural_stop = next((item for item in _SECTION_STOP.finditer(joined)
                                    if tuple(map(int, re.sub(r"\s+", "", item["section"]).split('.'))) > tuple(map(int, section.split('.')))), None)
            scope = joined[:structural_stop.start()] if structural_stop else joined
            note = re.search(r"(?m)^\s*注\s*[:：]", scope)
            body = scope[:note.start()] if note else scope
            header_matches = [re.search(r"\s*".join(re.escape(char) for char in header), body[:1200])
                              for header in _HEADERS]
            reit_header_matches = [re.search(r"\s*".join(re.escape(char) for char in header), body[:1200])
                                   for header in _REIT_HEADERS]
            reit_experience = _REIT_EXPERIENCE.search(body[:1600])
            tenure_header = re.search(r"任\s*本\s*基\s*金\s*的\s*基\s*金\s*经\s*理\s*(?:[（(]\s*助\s*理\s*[）)]\s*)?期\s*限", body[:1200])
            standard_headers = all(header_matches) and bool(tenure_header)
            reit_headers = section == '7.1.2' and all(reit_header_matches) and bool(reit_experience)
            if not all(header_matches) and not reit_headers:
                continue
            if not reit_headers and not tenure_header:
                continue
            header_end = max(match.end() for match in ([*header_matches, tenure_header] if standard_headers
                                                        else [*reit_header_matches, reit_experience]))
            offsets = []
            offset = 0
            for index, fragment in enumerate(fragments):
                offsets.append((offset, first_page + index))
                offset += len(fragment) + 1
            end_offset = structural_stop.start() if structural_stop else len(joined)
            end_index = next((i for offset, i in reversed(offsets) if offset <= end_offset), first_page)
            mapping_verified = all(type(numbers[i]) is int and numbers[i] > 0 and numbers[i] == numbers[first_page] + i - first_page
                                   for i in range(first_page, end_index + 1)) if type(numbers[first_page]) is int else False
            table = {"section": section, "startPage": numbers[first_page], "endPage": numbers[end_index],
                     "endSection": re.sub(r"\s+", "", structural_stop["section"]) if structural_stop else None,
                     "heading": heading.group().strip()[:240], "endHeading": structural_stop.group().strip()[:240] if structural_stop else None,
                     "sectionBounded": structural_stop is not None, "pageNumbersVerified": mapping_verified,
                     "pageLimitReached": structural_stop is None and len(fragments) >= _MAX_TABLE_PAGES,
                     "ignoredRoles": [], **dict.fromkeys(_COUNT_FIELDS, 0)}
            consumed.append((first_page, end_index, section))
            matches = {match.start("name"): match for match in _ROW.finditer(body, header_end)}
            candidates = list(_CANDIDATE.finditer(body, header_end))
            spans = []
            for candidate in candidates:
                role_candidate = re.sub(r"\s+", "", candidate["role"])
                if candidate["name"] in {"日", "月", "年", "年限", "任职", "离任"} or any(label in candidate["name"] for label in ("姓名", "任职日期", "离任日期", "证券从业", "说明")):
                    continue
                # Biographies are not table rows. Only explicit biographical
                # prefixes are ignored; any ambiguous manager mention blocks
                # whole-table completeness below.
                if any(role_candidate.startswith(word) for word in _BIOGRAPHY_WORDS):
                    spans.append((candidate.start(), candidate.end()))
                    continue
                table["candidateRows"] += 1
                spans.append((candidate.start(), candidate.end()))
                if _ignored_role_allowed(candidate["name"], role_candidate):
                    table["ignoredKnownRoleRows"] += 1
                    candidate_page = next((i for offset, i in reversed(offsets) if offset <= candidate.start()), first_page)
                    table["ignoredRoles"].append({"name": candidate["name"], "role": role_candidate, "page": numbers[candidate_page],
                                                  "text": re.sub(r"\s+", " ", candidate.group()).strip()[:240]})
                    continue
                match = matches.get(candidate.start("name"))
                if not match:
                    table["excludedRows"] += 1
                    continue
                role = re.sub(r"\s+", "", match["role"])
                if not _role_allowed(role):
                    table["excludedRows"] += 1
                    continue
                appointment = _date(match["start"])
                departure = _date(match["end"])
                if not appointment or (match["end"] not in ("-", "—", "－", "–", "至今", "在任") and not departure):
                    table["excludedRows"] += 1
                    continue
                if departure and departure < appointment:
                    table["excludedRows"] += 1
                    continue
                physical_indices = [index for offset, index in offsets if offset <= match.start()]
                index = physical_indices[-1]
                page = numbers[index]
                if type(page) is not int or page < 1:
                    table["excludedRows"] += 1
                    continue
                table_page = numbers[first_page]
                if type(table_page) is not int or table_page < 1 or any(
                    type(numbers[i]) is not int or numbers[i] != table_page + i - first_page
                    for i in range(first_page, index + 1)
                ):
                    table["excludedRows"] += 1
                    continue
                # Do not cite one page for a row whose appointment/date fields
                # straddle unknown or nonconsecutive PDF pages.
                row_pages = [numbers[i] for offset, i in offsets if match.start() < offset < match.end()]
                if any(type(value) is not int or value != page + step for step, value in enumerate(row_pages, 1)):
                    table["excludedRows"] += 1
                    continue
                table["parsedRows"] += 1
                if appointment > end or departure and departure < start:
                    table["outsidePeriodRows"] += 1
                    continue
                name = match["name"]
                key = (name, appointment, departure)
                if key in managers:
                    table["duplicateRows"] += 1
                managers.setdefault(key, {
                    "name": name, "startDate": appointment, "endDate": departure,
                    "role": role, "periodStart": start, "periodEnd": end,
                    "activeAtPeriodEnd": departure is None or departure >= end,
                    "evidence": {
                        "sourceUrl": source_url, "page": page, "pages": [page, *row_pages], "tablePage": table_page,
                        "section": section,
                        "text": re.sub(r"\s+", " ", match.group()).strip()[:240],
                        "method": _METHOD, "periodEnd": end,
                    },
                })
                if len(managers) > _MAX_MANAGERS:
                    table["excludedRows"] += 1
            # Catch unsupported names/row layouts without requiring valid dates
            # or the strict CJK parser to recognize them as rows first.
            for mention in re.finditer(r"基\s*金\s*经\s*理", body[header_end:]):
                position = header_end + mention.start()
                if any(begin <= position < finish for begin, finish in spans):
                    continue
                line_start = body.rfind('\n', 0, position) + 1
                prefix = re.sub(r"\s+", "", body[line_start:position])
                if any(word in prefix for word in _BIOGRAPHY_WORDS) or "任本基金的" in prefix or re.search(r"4\s*\.\s*1", prefix):
                    continue
                context = re.sub(r"\s+", "", body[max(header_end, position - 180):position])
                possible_name = ("本基金" in prefix or bool(re.fullmatch(r"[A-Za-z][A-Za-z.'-]{1,60}", prefix))
                                 or bool(re.fullmatch(rf"[{_CJK}]{{2,8}}", prefix)) and prefix not in {"基金", "经理"}
                                 and not any(word in prefix for word in ("投资", "证券", "公司", "部门", "工作")))
                if not possible_name and re.search(r"(?:担任|从事)[^。；]{0,100}$", context):
                    continue
                table["candidateRows"] += 1
                table["excludedRows"] += 1
            coverage["tables"].append(table)
    rows = list(managers.values())[:_MAX_MANAGERS]
    for key in _COUNT_FIELDS:
        coverage[key] = sum(table[key] for table in coverage["tables"])
    coverage["outputManagerCount"] = len(rows)
    coverage["managerKeys"] = sorted(manager_key(manager) for manager in rows)
    published = _date(coverage.get("publishedAt"))
    coverage["complete"] = bool(published and published >= end and rows and coverage["tables"] and coverage["excludedRows"] == 0
        and coverage["candidateRows"] == coverage["parsedRows"] + coverage["ignoredKnownRoleRows"]
        and coverage["parsedRows"] == len(rows) + coverage["outsidePeriodRows"] + coverage["duplicateRows"]
        and all(table["sectionBounded"] and table["pageNumbersVerified"] and not table["pageLimitReached"] for table in coverage["tables"]))
    coverage["status"] = "complete" if coverage["complete"] else "partial" if rows or coverage["tables"] else "missing"
    return ManagerRows(rows, coverage)
