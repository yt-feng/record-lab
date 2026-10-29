"""Bounded strategy-section extraction and limited, page-cited evidence.

Whole-section text stays on the result object's ``sections`` attribute, never
in its JSON fields. Complete extraction does not claim semantic understanding,
an individual security trade, or a manager's unstated motivation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import ipaddress
import re
from urllib.parse import urlsplit, parse_qsl
from report_period import document_periods, period_token

METHOD = "bounded-strategy-sections-v1"
CATEGORIES = ("market-review", "portfolio-operations", "outlook", "unclassified")
MAX_PAGES = 8
MAX_CHARS = 60000
MAX_QUOTES = 8
MAX_QUOTE_CHARS = 160
_SECTION = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?(?P<number>[1-9]\s*\.\s*\d+(?:\s*\.\s*\d+){0,2}|§\s*[1-9])[ \t]*(?P<title>[^\n]{1,180})$")
_STRATEGY = re.compile(r"(?:报告期内)?(?:本)?基金(?:的)?投资策略和运作分析")
_PERFORMANCE = re.compile(r"业绩表现|投资业绩|基金净值表现|净值增长率")
_REVIEW = re.compile(r"宏观|经济|市场|行情|政策|利率|债市|股市|季度|报告期|行业|产业")
_SUBJECT = re.compile(r"本基金|本产品|本组合|组合|我们")
_ACTION = re.compile(r"配置|持有|增持|减持|增配|减配|买入|卖出|加仓|减仓|降低|增加|调整|布局|维持|仓位")
_OUTLOOK = re.compile(r"展望|未来|下一阶段|后续|后市|将继续|继续将|预计|我们将|本基金将|计划|有望")
_CAUSE = re.compile(r"因为|由于|基于|考虑到|因此|从而|以期|旨在|以博取|为了")


class StrategySections(dict):
    def __init__(self, *args, sections=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.sections = tuple(sections)


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return value if dt.date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _source(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"https", "http"} or not host or "." not in host or parsed.username or parsed.password
                or parsed.fragment or host.endswith((".local", ".localhost"))):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return not any(re.fullmatch(r"token|key|secret|password|signature|authorization|access_token|api_key", key, re.I)
                       for key, _ in parse_qsl(parsed.query))
    except ValueError:
        return False


def _interval(report):
    end, kind = report.get("periodEnd"), report.get("kind")
    if not _date(end) or kind not in {"quarterly", "semiannual", "interim", "annual"}:
        return None
    day = dt.date.fromisoformat(end)
    if (day.month, day.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        return None
    if kind in {"semiannual", "interim"} and (day.month, day.day) != (6, 30) or kind == "annual" and (day.month, day.day) != (12, 31):
        return None
    return f"{day.year}-{'%02d' % (day.month - 2) if kind == 'quarterly' else '01'}-01", end


def _clean_page(raw):
    value = html.unescape(str(raw or ""))
    value = re.sub(r"(?i)</?(?:p|div|tr)\b[^>]*>|<br\s*/?>", "\n", value)
    value = re.sub(r"<[^>\n]{0,600}>", " ", value).replace("\r\n", "\n").replace("\r", "\n")
    lines = value.splitlines()
    result = []
    for index, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line)
        if re.fullmatch(r"第\d+页(?:共\d+页)?|\d+\s*/\s*\d+", compact):
            continue
        if (index < 3 or index >= len(lines) - 3) and re.fullmatch(r".{0,100}基金20\d{2}年第?[一二三四1-4]季度报告", compact):
            continue
        result.append(line)
    joined, cursor = [], 0
    while cursor < len(result):
        line = result[cursor]
        match = re.match(r"^\s*(?:#{1,6}\s*)?(4\s*\.\s*4(?:\s*\.\s*1)?)\s*(.*)$", line)
        if match and not _STRATEGY.fullmatch(re.sub(r"\s+", "", match[2])):
            for following in range(cursor + 1, min(len(result), cursor + 3)):
                candidate = "".join(result[cursor:following + 1])
                title = re.sub(r"^\s*(?:#{1,6}\s*)?4\s*\.\s*4(?:\s*\.\s*1)?\s*", "", candidate)
                if _STRATEGY.fullmatch(re.sub(r"\s+", "", title)):
                    line, cursor = candidate, following
                    break
        joined.append(line); cursor += 1
    return "\n".join(joined)


def _number(value):
    return re.sub(r"[§\s]+", "", value)


def _parts(value):
    return tuple(int(number) for number in value.split("."))


def _heading(match):
    return {"section": _number(match["number"]), "title": re.sub(r"\s+", "", match["title"]),
            "text": re.sub(r"\s+", " ", match.group()).strip()[:220], "start": match.start(), "end": match.end()}


def _boundary(start, heading):
    here, other = _parts(start), _parts(heading["section"])
    if other <= here:
        return False
    return _PERFORMANCE.search(heading["title"]) is not None or len(other) <= len(here) or other[:len(here)] != here


def _categories(sentence):
    result = []
    if _REVIEW.search(sentence):
        result.append("market-review")
    if _SUBJECT.search(sentence) and _ACTION.search(sentence) and not _OUTLOOK.search(sentence):
        result.append("portfolio-operations")
    if _OUTLOOK.search(sentence):
        result.append("outlook")
    return result or ["unclassified"]


def _document_periods(pages, numbers):
    return document_periods(pages, numbers)


def _valid_document_period(evidence, report):
    kind = "semiannual" if report.get("kind") == "interim" else report.get("kind")
    return bool(isinstance(evidence, list) and len(evidence) == 1 and isinstance(evidence[0], dict)
                and evidence[0].get("periodEnd") == report.get("periodEnd") and evidence[0].get("kind") == kind
                and type(evidence[0].get("page")) is int and 1 <= evidence[0]["page"] <= 10000
                and isinstance(evidence[0].get("text"), str) and len(evidence[0]["text"]) <= 100
                and period_token(evidence[0]["text"]) == (kind, report.get("periodEnd")))


def _sentences(chunks):
    characters, positions = [], []
    for page, chunk in chunks:
        for character in chunk:
            if character.isspace():
                # PDF line wrapping does not insert spaces into Chinese words.
                if characters and characters[-1].isascii() and characters[-1].isalnum():
                    characters.append(" "); positions.append(page)
                continue
            characters.append(character); positions.append(page)
    text = "".join(characters).strip()
    result = []
    for match in re.finditer(r"[^。！？；]+[。！？；]?", text):
        sentence = match.group().strip()
        if sentence:
            result.append({"text": sentence, "positions": positions[match.start():match.end()], "categories": _categories(sentence)})
    return text, result


def _security_mentions(text, securities):
    # Only exact supplied security names (>=4 characters) or full market codes.
    # No short aliases such as 平安, and no inference from an industry mention.
    result = []
    names = {}
    for security in securities:
        if isinstance(security, dict) and isinstance(security.get("stockName"), str):
            name = re.sub(r"\s+", "", security["stockName"])
            names[name] = names.get(name, 0) + 1
    for security in securities[:2000]:
        if not isinstance(security, dict):
            continue
        code, market, name = security.get("stockCode"), security.get("market"), security.get("stockName")
        if (not isinstance(code, str) or not isinstance(name, str) or not
                ((market == "CN" and re.fullmatch(r"\d{6}", code)) or (market == "HK" and re.fullmatch(r"\d{5}", code))
                 or (market == "US" and re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?", code)))):
            continue
        exact_name = re.sub(r"\s+", "", name)
        generic = exact_name in {"银行行业", "保险行业", "科技行业", "金融行业", "人工智能", "中国经济"}
        name_match = not generic and len(exact_name) >= 4 and names.get(exact_name) == 1 and exact_name in text
        code_match = market in {"CN", "HK"} and re.search(rf"(?<!\d){re.escape(code)}(?!\d)", text)
        # Short English tickers are common words; require an explicit ticker label.
        if market == "US":
            code_match = re.search(rf"(?:股票代码|证券代码|ticker)\s*[:：]?\s*{re.escape(code)}(?![A-Z0-9.])", text, re.I)
        if name_match or code_match:
            result.append({"stockCode": code, "market": market, "stockName": name[:100], "matchType": "exact-name" if name_match else "explicit-code",
                           "provesTrade": False, "provesTradeReason": False})
    return result[:20]


def parse_strategy_sections(pages, report, *, page_numbers=None, securities=None, max_pages=MAX_PAGES, max_chars=MAX_CHARS):
    """Extract a full bounded section; publish only diverse short evidence."""
    pages = [str(page or "") for page in pages]
    report = report if isinstance(report, dict) else {}
    numbers = list(page_numbers) if page_numbers is not None else list(range(1, len(pages) + 1))
    interval = _interval(report)
    proof = {"schemaVersion": 1, "method": METHOD, "sourceUrl": report.get("sourceUrl"), "publishedAt": report.get("publishedAt"),
             "periodStart": interval[0] if interval else None, "periodEnd": report.get("periodEnd"), "kind": report.get("kind"),
             "sourceTextHash": hashlib.sha256("\n\f\n".join(pages).encode()).hexdigest(), "sections": [],
             "documentPeriodEvidence": _document_periods(pages, numbers),
             "sectionCount": 0, "contentChars": 0, "sentenceCount": 0, "complete": False, "status": "missing", "reasonCodes": []}
    private, statements = [], []
    max_pages, max_chars = max(1, min(16, max_pages)), max(100, min(MAX_CHARS, max_chars))
    clean = [_clean_page(page) for page in pages]
    starts = []
    for index, page in enumerate(clean):
        for match in _SECTION.finditer(page):
            heading = _heading(match)
            if heading["section"] not in {"4.4", "4.4.1"} or not _STRATEGY.fullmatch(heading["title"]):
                continue
            # TOC rows have leaders or a trailing page number, so their titles
            # cannot exactly match the actual section title above.
            starts.append((index, heading))
    for index, heading in starts[:4]:
        chunks, physical, end_heading, end_index, used_chars = [], [], None, None, 0
        text_limit = False
        for cursor in range(index, min(len(clean), index + max_pages)):
            page = clean[cursor]
            begin = heading["end"] if cursor == index else 0
            body = page[begin:]
            ending = next((_heading(item) for item in _SECTION.finditer(body) if _boundary(heading["section"], _heading(item))), None)
            if ending:
                body = body[:ending["start"]]
                end_heading, end_index = ending, cursor
            page_number = numbers[cursor] if cursor < len(numbers) else None
            physical.append(page_number)
            if used_chars + len(body) > max_chars:
                body = body[:max(0, max_chars - used_chars)]; text_limit = True
            used_chars += len(body)
            chunks.append((page_number, body))
            if ending or text_limit:
                break
        page_limit = end_heading is None and index + max_pages < len(clean)
        verified = bool(physical and all(type(value) is int and 1 <= value <= 10000 for value in physical)
                        and physical == list(range(physical[0], physical[0] + len(physical))))
        text, sentences = _sentences(chunks)
        metadata = {"section": heading["section"], "heading": heading["text"], "startPage": physical[0] if physical else None,
                    "endPage": physical[-1] if physical else None, "pages": physical, "endSection": end_heading["section"] if end_heading else None,
                    "endHeading": end_heading["text"] if end_heading else None, "sectionBounded": end_heading is not None,
                    "pageNumbersVerified": verified, "pageLimitReached": page_limit, "textLimitReached": text_limit,
                    "contentChars": len(text), "sentenceCount": len(sentences), "contentHash": hashlib.sha256(text.encode()).hexdigest()}
        proof["sections"].append(metadata)
        private.append({**metadata, "text": text})
        for sentence in sentences:
            statements.append({**sentence, "section": heading["section"], "sectionIndex": len(private) - 1})
    proof["sectionCount"] = len(proof["sections"])
    proof["contentChars"] = sum(item["contentChars"] for item in proof["sections"])
    proof["sentenceCount"] = len(statements)
    if len(starts) > 1:
        proof["reasonCodes"].append("MULTIPLE_STRATEGY_SECTIONS")
    for section in proof["sections"]:
        for valid, reason in ((section["sectionBounded"], "MISSING_SECTION_END"), (section["pageNumbersVerified"], "UNKNOWN_OR_NONCONTIGUOUS_PAGES"),
                              (not section["pageLimitReached"], "SECTION_PAGE_LIMIT"), (not section["textLimitReached"], "SECTION_TEXT_LIMIT"),
                              (section["contentChars"] > 0, "EMPTY_SECTION")):
            if not valid:
                proof["reasonCodes"].append(reason)
    if not interval or not _source(report.get("sourceUrl")) or not _date(report.get("publishedAt")) or report.get("publishedAt", "") < report.get("periodEnd", ""):
        proof["reasonCodes"].append("INVALID_SOURCE_OR_REPORT_PERIOD")
    if not _valid_document_period(proof["documentPeriodEvidence"], report):
        proof["reasonCodes"].append("MISSING_OR_MISMATCHED_DOCUMENT_PERIOD")
    proof["reasonCodes"] = sorted(set(proof["reasonCodes"]))
    proof["complete"] = bool(len(starts) == 1 and statements and not proof["reasonCodes"])
    proof["status"] = "complete" if proof["complete"] else "partial" if starts else "missing"
    # Choose evidence across categories rather than publishing every source sentence.
    selected = []
    for category in CATEGORIES:
        indices = [index for index, item in enumerate(statements) if category in item["categories"]]
        for index in indices[:2]:
            if index not in selected and len(selected) < MAX_QUOTES:
                selected.append(index)
    period_conflict = bool(proof["documentPeriodEvidence"] and any(
        item["periodEnd"] != report.get("periodEnd") or item["kind"] != ("semiannual" if report.get("kind") == "interim" else report.get("kind"))
        for item in proof["documentPeriodEvidence"]))
    if period_conflict:
        # Keep the extracted text privately for re-keying, but never publish a
        # prior-quarter or interim statement under this requested quarter.
        selected = []
    evidence = []
    securities = securities if isinstance(securities, list) else []
    for index in sorted(selected):
        statement = statements[index]
        signal = next((pattern.search(statement["text"]) for pattern in (_ACTION, _OUTLOOK, _REVIEW) if pattern.search(statement["text"])), None)
        start = max(0, signal.start() - 45) if signal and len(statement["text"]) > MAX_QUOTE_CHARS else 0
        quote = statement["text"][start:start + MAX_QUOTE_CHARS]
        cited = list(dict.fromkeys(statement["positions"][start:start + len(quote)]))
        valid_pages = bool(cited and all(type(value) is int and 1 <= value <= 10000 for value in cited)
                           and cited == list(range(cited[0], cited[0] + len(cited))))
        if not valid_pages:
            continue
        evidence.append({"id": f"s{statement['sectionIndex']}-{index}", "text": quote, "page": cited[0], "pages": cited,
                         "section": statement["section"], "sentenceIndex": index, "categories": statement["categories"],
                         "sourceUrl": report.get("sourceUrl"), "periodStart": proof["periodStart"], "periodEnd": proof["periodEnd"],
                         "quoteTruncated": start > 0 or len(quote) < len(statement["text"]), "isInference": False,
                         "securityMentions": _security_mentions(quote, securities),
                         "languageSignals": {"operation": bool(_SUBJECT.search(quote) and _ACTION.search(quote)),
                                             "causalConnective": bool(_CAUSE.search(quote)), "outlook": bool(_OUTLOOK.search(quote))}})
    analysis = {"schemaVersion": 1, "scope": "selected-evidence", "method": "explicit-language-rules-v1",
                "status": "wrong-period" if period_conflict else "available" if evidence and proof["complete"] else "partial-source" if evidence else "missing",
                "analysisCoverage": "selected-evidence", "semanticAnalysisComplete": False,
                "sourceSectionComplete": proof["complete"], "sentenceCount": len(statements),
                "categorySentenceCounts": {category: sum(category in item["categories"] for item in statements) for category in CATEGORIES},
                "categoryCountsOverlap": True, "selectedSentenceCount": len(evidence), "omittedSentenceCount": len(statements) - len(evidence),
                "maxQuotes": MAX_QUOTES, "maxQuoteChars": MAX_QUOTE_CHARS, "evidence": evidence,
                "explainsIndividualStockTrades": False, "securityMentionsProveTrades": False}
    return StrategySections({"strategySectionCoverage": proof, "strategyAnalysis": analysis}, sections=private)


def normalize_strategy_section_coverage(raw, period_end=None):
    if (not isinstance(raw, dict) or raw.get("schemaVersion") != 1 or raw.get("method") != METHOD
            or not _source(raw.get("sourceUrl")) or not re.fullmatch(r"[a-f0-9]{64}", str(raw.get("sourceTextHash", "")))
            or period_end is not None and raw.get("periodEnd") != period_end):
        return None
    interval = _interval(raw)
    if not interval or raw.get("periodStart") != interval[0]:
        return None
    integer = lambda value: type(value) is int and 0 <= value <= MAX_CHARS * 4
    if (any(not integer(raw.get(key)) for key in ("sectionCount", "contentChars", "sentenceCount"))
            or not isinstance(raw.get("sections"), list) or len(raw["sections"]) > 4):
        return None
    sections, checks = [], []
    for item in raw["sections"]:
        if (not isinstance(item, dict) or item.get("section") not in {"4.4", "4.4.1"}
                or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("contentHash", "")))
                or any(not integer(item.get(key)) for key in ("contentChars", "sentenceCount"))):
            return None
        heading = item.get("heading")
        match = _SECTION.fullmatch(heading) if isinstance(heading, str) and len(heading) <= 220 else None
        start = _heading(match) if match else None
        ending = item.get("endHeading")
        match = _SECTION.fullmatch(ending) if isinstance(ending, str) and len(ending) <= 220 else None
        end = _heading(match) if match else None
        pages = item.get("pages")
        valid_pages = (isinstance(pages, list) and 1 <= len(pages) <= 16 and all(type(value) is int and 1 <= value <= 10000 for value in pages)
                       and pages == list(range(pages[0], pages[0] + len(pages))) and item.get("startPage") == pages[0] and item.get("endPage") == pages[-1])
        bounded = bool(start and end and start["section"] == item["section"] and _STRATEGY.fullmatch(start["title"])
                       and end["section"] == item.get("endSection") and _boundary(start["section"], end))
        checks.append(bool(valid_pages and bounded and item.get("sectionBounded") is True and item.get("pageNumbersVerified") is True
                           and item.get("pageLimitReached") is False and item.get("textLimitReached") is False
                           and item["contentChars"] > 0 and item["sentenceCount"] > 0))
        sections.append({key: item.get(key) for key in ("section", "heading", "startPage", "endPage", "pages", "endSection", "endHeading", "sectionBounded",
                         "pageNumbersVerified", "pageLimitReached", "textLimitReached", "contentChars", "sentenceCount", "contentHash")})
    complete = bool(raw.get("complete") is True and raw["sectionCount"] == len(sections) == 1 and all(checks)
                    and _valid_document_period(raw.get("documentPeriodEvidence"), raw)
                    and _date(raw.get("publishedAt")) and raw["publishedAt"] >= raw["periodEnd"]
                    and all(raw[key] == sum(item[key] for item in sections) for key in ("contentChars", "sentenceCount")))
    return {"schemaVersion": 1, "method": METHOD, **{key: raw.get(key) for key in ("sourceUrl", "publishedAt", "periodStart", "periodEnd", "kind", "sourceTextHash")},
            "documentPeriodEvidence": [{key: item.get(key) for key in ("periodEnd", "kind", "page", "text")} for item in (raw.get("documentPeriodEvidence") if isinstance(raw.get("documentPeriodEvidence"), list) else []) if isinstance(item, dict)][:8],
            "sections": sections, "sectionCount": len(sections), "contentChars": raw["contentChars"], "sentenceCount": raw["sentenceCount"],
            "complete": complete, "status": "complete" if complete else "partial" if sections else "missing",
            "reasonCodes": sorted(set(reason for reason in (raw.get("reasonCodes") if isinstance(raw.get("reasonCodes"), list) else []) if isinstance(reason, str) and re.fullmatch(r"[A-Z_]{1,60}", reason)))}


__all__ = ["parse_strategy_sections", "normalize_strategy_section_coverage", "StrategySections"]
