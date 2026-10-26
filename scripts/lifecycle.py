"""Bounded, evidence-only fund lifecycle fallback for missing old reports.

The 45-day gate is an operational request budget, not a disclosure deadline.
Termination evidence affects only target-period applicability. It never implies
that a fund held no securities during that quarter or removes its catalog code.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
import time
from urllib.parse import urlparse


DATE = r"20\d{2}(?:年\d{1,2}月\d{1,2}日|[-/]\d{1,2}[-/]\d{1,2})"
API_URL = "https://api.fund.eastmoney.com/f10/JJGG"
MAX_PDF_PAGES = 20
MAX_PDF_BYTES = 4 * 1024 * 1024
MAX_TEXT_CHARS = 250000


def _date(value):
    match = re.fullmatch(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", str(value))
    try:
        return dt.date(*map(int, match.groups())).isoformat() if match else None
    except ValueError:
        return None


def _iso(value):
    return isinstance(value, str) and _date(value) == value


def _publication_date(value):
    match = re.match(DATE, str(value))
    return _date(match[0]) if match else None


def _public_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def should_check_lifecycle(target_period, as_of, missing_target=True):
    """Only an actually missing target older than 45 days enables the fallback."""
    if missing_target is not True or not _iso(target_period) or not _iso(as_of):
        return False
    target = dt.date.fromisoformat(target_period)
    if (target.month, target.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        return False
    return (dt.date.fromisoformat(as_of) - target).days > 45


def _notice_kind(title):
    title = re.sub(r"\s+", "", str(title))
    # A distributor closing service is unrelated to a fund contract ending.
    if re.search(r"可能|拟终止|拟清算|基金销售|销售机构|销售业务|销售服务|代销|服务协议", title):
        return None
    if re.search(r"基金(?:财产)?清算报告", title):
        return "final-liquidation"
    if re.search(r"终止(?:《)?基金合同|基金合同(?:》)?终止|终止基金运作", title):
        return "termination"
    return None


def parse_lifecycle_announcements(payload, fund_code, as_of):
    """Discover candidates only; titles cannot establish termination."""
    if not isinstance(payload, dict) or not isinstance(payload.get("Data"), list):
        raise ValueError("LIFECYCLE_ANNOUNCEMENT_FORMAT")
    if not re.fullmatch(r"\d{6}", str(fund_code)) or not _iso(as_of):
        raise ValueError("LIFECYCLE_ARGUMENTS")
    notices = {}
    for row in payload["Data"][:100]:
        if not isinstance(row, dict):
            continue
        source_code = next((str(row[key]).zfill(6) for key in ("FCODE", "FUND_CODE", "FUNDCODE", "fundcode")
                            if row.get(key) is not None), None)
        if source_code is not None and source_code != fund_code:
            continue
        title = next((str(row[key]) for key in ("TITLE", "title", "NOTICE_TITLE") if row.get(key)), "")
        kind = _notice_kind(title)
        published = next((_publication_date(row[key]) for key in
                          ("PUBLISHDATEDESC", "PUBLISHDATE", "ADATE", "NOTICE_DATE", "publishDate", "notice_date")
                          if row.get(key) and _publication_date(row[key])), None)
        article = next((str(row[key]) for key in ("ART_CODE", "art_code", "ID", "IDNO", "REPORTID")
                        if re.fullmatch(r"AN\d{8,32}", str(row.get(key, "")))), None)
        if kind and published and article and published <= as_of:
            url = f"https://pdf.dfcfw.com/pdf/H2_{article}_1.pdf"
            notices[url] = {"title": title[:300], "kind": kind, "publishedAt": published,
                            "sourceFundCode": source_code, "sourceUrl": url}
    return sorted(notices.values(), key=lambda item: item["publishedAt"], reverse=True)


def _page_text(raw):
    positions = [index for index, char in enumerate(raw) if not char.isspace()]
    return "".join(raw[index] for index in positions), positions


def _quote(raw, positions, start, end, field, page):
    value = raw[positions[start]:positions[end - 1] + 1].strip()
    # Every excerpt remains an original contiguous PDF-text span.
    return {"field": field, "page": page, "quote": value[:420]}


def parse_lifecycle_pages(pages, notice, fund_code, target_period, as_of):
    """Return validated applicability evidence or None, never an inferred exit.

    Pages must be ordered physical PDF pages, not arbitrary markdown chunks.
    The caller must not supply a truncated document under those page numbers.
    """
    if (not isinstance(pages, (list, tuple)) or not 1 <= len(pages) <= MAX_PDF_PAGES
            or any(not isinstance(page, str) for page in pages)
            or sum(map(len, pages)) > MAX_TEXT_CHARS or not isinstance(notice, dict)
            or not re.fullmatch(r"\d{6}", str(fund_code))
            or not _iso(target_period) or not _iso(as_of)
            or not _iso(notice.get("publishedAt")) or notice["publishedAt"] > as_of
            or notice.get("sourceFundCode") not in (None, fund_code)
            or not _public_url(notice.get("sourceUrl"))):
        return None
    kind = _notice_kind(notice.get("title", ""))
    header = re.sub(r"\s+", "", pages[0])[:500]
    # Only inspect the heading, before the body starts discussing service fees.
    heading = re.split(r"重要提示|本基金管理人|以下简称", header, maxsplit=1)[0][:260]
    if not kind or _notice_kind(heading) != kind:
        return None
    identities, identity_quotes, confirmations, dates = set(), [], [], {}
    date_patterns = {
        "lastOperationDate": [rf"(?:本基金(?:的)?)?(?:最后|最终)运作日(?:为|是|[:：])?({DATE})",
                              rf"({DATE})(?:为|是)本基金(?:的)?(?:最后|最终)运作日"],
        "terminationDate": [rf"(?:本基金(?:的)?)?(?:《基金合同》|基金合同)(?:已)?(?:自|于)({DATE})(?:起)?(?:已|正式|自动)?终止",
                            rf"(?:本基金(?:的)?)?(?:《基金合同》|基金合同)终止日(?:为|是|[:：])({DATE})"],
        "liquidationStartDate": [rf"自({DATE})起[，,]?(?:本基金)?(?:已)?进入(?:基金财产)?清算(?:程序|期)"],
        "finalLiquidationEnd": [rf"自{DATE}起?至({DATE})止?为(?:本基金)?清算报告期",
                                rf"清算报告期(?:结束|截止)日[（(:：]?({DATE})"]}
    confirmation = re.compile(r"(?:本基金(?:的)?(?:《基金合同》|基金合同)|《基金合同》)"
                              rf"(?:已)?(?:(?:自|于){DATE}(?:起)?)?(?:正式|自动)?终止(?=[。；;，,]|$)")
    for page_number, raw in enumerate(pages, 1):
        text, positions = _page_text(raw)
        # Code-only table cells after explicit fund identity labels. Never use
        # arbitrary six-digit numbers elsewhere in the financial statements.
        for match in re.finditer(r"(?:基金主代码|基金(?:份额|交易)?代码|基金份额(?:交易)?代码|下属(?:分级|分类)基金的交易代码)[:：]?"
                                 r"((?:\d{6}){1,8})(?!\d)", text):
            codes = re.findall(r"\d{6}", match[1])
            identities.update(codes)
            identity_quotes.append(_quote(raw, positions, match.start(), match.end(), "identityFundCodes", page_number))
        for match in confirmation.finditer(text):
            left = max(text.rfind(mark, 0, match.start()) for mark in "。；;！？!?") + 1
            prefix = text[left:match.start()]
            if len(prefix) > 260 or re.search(r"若|如果|如届时|一旦|可能|拟|计划|将|应当|需满足|未终止|不终止", prefix):
                continue
            right = re.search(r"[。；;！？!?]", text[match.end():])
            end = match.end() + (right.start() + 1 if right else 0)
            if re.search(r"可能|拟|计划|将|尚未|尚待|未发生|待确认", text[match.end():end]):
                continue
            confirmations.append(_quote(raw, positions, max(left, match.start() - 180), end,
                                        "terminationConfirmed", page_number))
        for field, patterns in date_patterns.items():
            for pattern in patterns:
                for match in re.finditer(pattern, text):
                    value = _date(match[1])
                    left = max(text.rfind(mark, 0, match.start()) for mark in "。；;！？!?") + 1
                    context = text[left:match.start()]
                    if value and not re.search(r"若|如果|可能|拟|计划|将", context[-100:]):
                        dates.setdefault(field, []).append((value, _quote(raw, positions, match.start(), match.end(), field, page_number)))
    if fund_code not in identities or not confirmations:
        return None
    # Conflicting explicit dates are unresolved, even if one date would exempt.
    values = {}
    date_quotes = []
    for field, matches in dates.items():
        unique = {value for value, _quote_value in matches}
        if len(unique) != 1:
            return None
        values[field] = matches[0][0]
        date_quotes.append(matches[0][1])
    date_basis = "terminationDate" if values.get("terminationDate") else "lastOperationDate"
    effective = values.get(date_basis)
    if not effective or effective >= target_period or effective > notice["publishedAt"]:
        return None
    if any(value > notice["publishedAt"] for value in values.values()):
        return None
    if values.get("lastOperationDate") and values["lastOperationDate"] >= target_period:
        return None
    if values.get("liquidationStartDate") and values["liquidationStartDate"] < effective:
        return None
    if values.get("finalLiquidationEnd") and (kind != "final-liquidation"
            or values["finalLiquidationEnd"] < (values.get("liquidationStartDate") or effective)):
        return None
    identity_quotes.sort(key=lambda item: fund_code not in re.sub(r"\s+", "", item["quote"]))
    cited = [confirmations[0], *date_quotes, *identity_quotes[:8]]
    return {"reason": "TERMINATED_BEFORE_PERIOD", "fundCode": fund_code, "periodEnd": target_period,
            "date": effective, "dateBasis": date_basis, "sourceUrl": notice["sourceUrl"],
            "publishedAt": notice["publishedAt"], "eventType": kind, "terminationConfirmed": True,
            "identityFundCodes": sorted(identities), "lastOperationDate": values.get("lastOperationDate"),
            "terminationDate": values.get("terminationDate"), "liquidationStartDate": values.get("liquidationStartDate"),
            "finalLiquidationEnd": values.get("finalLiquidationEnd"),
            "finalLiquidationEndBasis": "liquidation-report-period-end" if values.get("finalLiquidationEnd") else None,
            "page": confirmations[0]["page"], "quote": confirmations[0]["quote"], "evidence": cited,
            "sourceTextHash": hashlib.sha256("\n\f\n".join(pages).encode()).hexdigest(),
            "parseMethod": "pypdf", "pageNumbersVerified": True, "totalPages": len(pages),
            "scope": "quarter-end-applicability-only"}


def normalize_lifecycle_evidence(evidence, fund_code, target_period, as_of):
    """Revalidate persisted short proof without trusting a reason string alone."""
    if (not isinstance(evidence, dict) or evidence.get("reason") != "TERMINATED_BEFORE_PERIOD"
            or evidence.get("fundCode") != fund_code or evidence.get("periodEnd") != target_period
            or evidence.get("terminationConfirmed") is not True
            or evidence.get("scope") != "quarter-end-applicability-only"
            or evidence.get("eventType") not in {"termination", "final-liquidation"}
            or evidence.get("dateBasis") not in {"terminationDate", "lastOperationDate"}
            or evidence.get("parseMethod") != "pypdf" or evidence.get("pageNumbersVerified") is not True
            or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("sourceTextHash", "")))
            or type(evidence.get("totalPages")) is not int or not 1 <= evidence["totalPages"] <= MAX_PDF_PAGES
            or not isinstance(evidence.get("identityFundCodes"), list)
            or not 1 <= len(evidence["identityFundCodes"]) <= 64
            or any(not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) for code in evidence["identityFundCodes"])
            or len(set(evidence["identityFundCodes"])) != len(evidence["identityFundCodes"])
            or not isinstance(evidence.get("evidence"), list) or not 3 <= len(evidence["evidence"]) <= 16):
        return None
    fields = {"terminationConfirmed", "identityFundCodes", "terminationDate", "lastOperationDate",
              "liquidationStartDate", "finalLiquidationEnd"}
    pages = ["" for _ in range(evidence["totalPages"])]
    title = "示例基金清算报告" if evidence["eventType"] == "final-liquidation" else "终止基金合同的公告"
    pages[0] = title + "。\n"
    for item in evidence["evidence"]:
        if (not isinstance(item, dict) or item.get("field") not in fields or type(item.get("page")) is not int
                or not 1 <= item["page"] <= len(pages) or not isinstance(item.get("quote"), str)
                or not 1 <= len(item["quote"]) <= 420):
            return None
        pages[item["page"] - 1] += item["quote"] + "。\n"
    required = {"terminationConfirmed", "identityFundCodes", evidence["dateBasis"]}
    if not required.issubset({item["field"] for item in evidence["evidence"]}):
        return None
    if not any(item["field"] == "terminationConfirmed" and item["page"] == evidence.get("page")
               and item["quote"] == evidence.get("quote") for item in evidence["evidence"]):
        return None
    proof = parse_lifecycle_pages(pages, {"title": title, "sourceUrl": evidence.get("sourceUrl"),
                                        "publishedAt": evidence.get("publishedAt")}, fund_code, target_period, as_of)
    keys = ("date", "dateBasis", "lastOperationDate", "terminationDate", "liquidationStartDate",
            "finalLiquidationEnd", "finalLiquidationEndBasis")
    if not proof or any(proof.get(key) != evidence.get(key) for key in keys):
        return None
    if sorted(evidence["identityFundCodes"]) != proof["identityFundCodes"]:
        return None
    result = {key: evidence.get(key) for key in proof}
    result["evidence"] = [{key: item[key] for key in ("field", "page", "quote")} for item in evidence["evidence"]]
    result["identityFundCodes"] = sorted(evidence["identityFundCodes"])
    return result


def lookup_lifecycle(provider, fund_code, target_period, as_of, *, missing_target=True,
                     deadline=None, max_announcement_pages=2, max_documents=2):
    """Use the existing bounded HTTPS provider; no requests before the age gate.

    A missing or unsupported result remains unresolved. At most two notice-list
    requests and two small PDFs are read, with a 30-second total request budget.
    """
    result = {"checked": False, "notApplicableEvidence": None, "documentsChecked": 0, "diagnostics": []}
    if not should_check_lifecycle(target_period, as_of, missing_target) or not re.fullmatch(r"\d{6}", str(fund_code)):
        return result
    limit = min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30
    def get(url, **kwargs):
        remaining = limit - time.monotonic()
        if remaining < 1.25:
            raise ValueError("LIFECYCLE_DEADLINE_REACHED")
        # PublicProvider has separate connect/read budgets; reserve both.
        return provider.get(url, timeout_seconds=min(8, (remaining - .25) / 2), attempts_override=1, **kwargs)
    def error(exc):
        code = str(getattr(exc, "code", str(exc)))
        result["diagnostics"].append({"errorCode": code if re.fullmatch(r"[A-Z_]+", code) else "LIFECYCLE_PARSE_FAILED"})
    result["checked"] = True
    notices, seen = {}, set()
    page_limit = max(1, min(2, int(max_announcement_pages)))
    document_limit = max(1, min(2, int(max_documents)))
    for page in range(1, page_limit + 1):
        try:
            payload = json.loads(get(API_URL, params={"fundcode": fund_code, "pageIndex": page, "pageSize": 100, "type": 0},
                                     referer=f"https://fundf10.eastmoney.com/jjgg_{fund_code}_0.html"))
            candidates = parse_lifecycle_announcements(payload, fund_code, as_of)
            fingerprint = hashlib.sha256(json.dumps(payload["Data"], sort_keys=True).encode()).hexdigest()
            if fingerprint in seen:
                raise ValueError("LIFECYCLE_PAGINATION_STALLED")
            seen.add(fingerprint)
            notices.update({notice["sourceUrl"]: notice for notice in candidates})
            if len(payload["Data"]) < 100 or len(notices) >= document_limit:
                break
            if page == page_limit:
                result["diagnostics"].append({"errorCode": "LIFECYCLE_ANNOUNCEMENT_PAGE_LIMIT"})
        except Exception as exc:
            error(exc)
            break
    for notice in sorted(notices.values(), key=lambda item: item["publishedAt"], reverse=True)[:document_limit]:
        try:
            content = get(notice["sourceUrl"], binary=True)
            result["documentsChecked"] += 1
            if not isinstance(content, bytes) or not content.startswith(b"%PDF"):
                raise ValueError("LIFECYCLE_INVALID_PDF")
            if len(content) > MAX_PDF_BYTES:
                raise ValueError("LIFECYCLE_PDF_TOO_LARGE")
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
                raise ValueError("LIFECYCLE_PDF_PAGE_LIMIT")
            pages = []
            for pdf_page in reader.pages:
                if time.monotonic() >= limit:
                    raise ValueError("LIFECYCLE_DEADLINE_REACHED")
                pages.append(pdf_page.extract_text() or "")
            evidence = parse_lifecycle_pages(pages, notice, fund_code, target_period, as_of)
            if evidence:
                result["notApplicableEvidence"] = evidence
                break
            result["diagnostics"].append({"errorCode": "LIFECYCLE_NOT_CONFIRMED"})
        except Exception as exc:
            error(exc)
    return result
