"""Prove zero period-end stock book value from two exhaustive asset tables.

This recognizer does not use fund names, investment mandates, missing stock
sections, or rounded zero percentages. Unknown rows and unclosed tables fail
closed. Monetary checks use integer cents; source PDFs stay with the caller.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import re
from urllib.parse import urlsplit, parse_qsl

TYPE = "exhaustive-nonstock-assets"
METHOD = "two-closed-nonstock-asset-tables"
MAX_ASSET_TEXT = 800
MAX_OTHER_TEXT = 600
MAX_TABLE_PAGES = 3
MONEY = r"(?:[-—－–]|(?:0|[1-9]\d{0,17}|[1-9]\d{0,2}(?:,\d{3}){1,5})(?:\.\d{1,2})?)"
RATIO = r"(?:[-—－–]|(?:0|[1-9]\d{0,2})(?:\.\d{1,2})?)"
PERIOD = re.compile(r"(\d{4})年(?:第([1-4一二三四])季度|(中期|半年度|年度))报告")
SECTION = re.compile(r"(?m)^\s*(?P<heading>(?P<number>\d{1,2}\.\d{1,2}(?:\.\d{1,2})?|§\s*\d{1,2})\s+[^\n]{2,120})\s*$")
ASSET_TITLE = re.compile(r"^(\d{1,2}\.1)(?:报告)?期末基金资产组合情况$")
OTHER_TITLE = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{1,2})其他(?:各项)?资产构成$")
OTHER_NAMES = ("存出保证金", "应收证券清算款", "应收利息", "应收申购款", "其他应收款", "待摊费用", "其他")
ZERO = {"-", "—", "－", "–"}


def _phrase(value):
    return r"\s*".join(re.escape(char) for char in value)


def _field(name, pattern=MONEY):
    return rf"(?P<{name}>{pattern})"


def _date(value):
    try:
        return isinstance(value, str) and dt.date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError):
        return False


def _source(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        return (url.scheme in {"https", "http"} and "." in host and not url.username and not url.password and not url.fragment
                and ":" not in host and not re.fullmatch(r"\d+(?:\.\d+){3}", host)
                and not host.endswith((".local", ".localhost"))
                and not any(re.fullmatch(r"token|key|secret|password|signature|authorization|access_token|api_key", key, re.I)
                            for key, _ in parse_qsl(url.query)))
    except ValueError:
        return False


def _period(value):
    match = PERIOD.fullmatch(re.sub(r"\s+", "", value)) if isinstance(value, str) else None
    if not match:
        return None
    year = int(match[1])
    if year < 1900:
        return None
    if match[2]:
        quarter = "一二三四".find(match[2]) + 1 if match[2] in "一二三四" else int(match[2])
        ending = ("03-31", "06-30", "09-30", "12-31")[quarter - 1]
        return "quarterly", f"{year:04d}-{ending}"
    return ("annual", f"{year:04d}-12-31") if match[3] == "年度" else ("semiannual", f"{year:04d}-06-30")


def _cents(value, ratio=False):
    if not isinstance(value, str) or not re.fullmatch(RATIO if ratio else MONEY, value):
        return None
    if value in ZERO:
        return 0
    cents = int(Decimal(value.replace(",", "")) * 100)
    return cents if 0 <= cents <= (10000 if ratio else 10**20) else None


def _decimal(value):
    return f"{value // 100}.{value % 100:02d}"


def _rounded_weight(amount, total):
    return (2 * amount * 10000 + total) // (2 * total)


def _body_match(table, body):
    return re.fullmatch(r"\s*" + re.escape(table["heading"]) + r"\s*" + body + r"\s*"
                        + re.escape(table["endHeading"]) + r"\s*", table["text"])


def _asset_values(table):
    unit = r"(?:" + _phrase("金额单位：人民币元") + r"\s*)?"
    header = (_phrase("序号") + r"\s*" + _phrase("项目") + r"\s*" + _phrase("金额")
              + r"\s*(?:[（(]\s*元\s*[）)])?\s*" + _phrase("占基金总资产的比例") + r"\s*[（(]\s*[%％]\s*[）)]\s*")
    def row(prefix, label, key):
        return prefix + r"\s*" + _phrase(label) + r"\s+" + _field("a" + key) + r"\s+" + _field("w" + key, RATIO) + r"\s*"
    body = unit + header + row("1", "固定收益投资", "1")
    body += row(r"其中\s*[：:]", "债券", "bond")
    body += row(r"(?:其中\s*[：:]\s*)?", "资产支持证券", "abs")
    body += row("2", "买入返售金融资产", "2")
    body += row(r"其中\s*[：:]", "买断式回购的买入返售金融资产", "repo")
    body += row("3", "银行存款和结算备付金合计", "3")
    body += r"4\s*(?:" + _phrase("其他资产") + "|" + _phrase("其他各项资产") + r")\s+" + _field("a4") + r"\s+" + _field("w4", RATIO) + r"\s*"
    body += row("5", "合计", "total")
    body += r"(?:" + _phrase("注：由于四舍五入的原因，分项之和与合计项可能存在尾差。") + r")?"
    match = _body_match(table, body)
    compact = re.sub(r"\s+", "", table["text"])
    if not match or not (re.search(r"金额[（(]元[）)]", compact) or "金额单位：人民币元" in compact):
        return None
    keys = ("1", "2", "3", "4", "bond", "abs", "repo", "total")
    amounts = {key: _cents(match["a" + key]) for key in keys}
    weights = {key: _cents(match["w" + key], True) for key in keys}
    if any(value is None for value in [*amounts.values(), *weights.values()]) or amounts["total"] <= 0:
        return None
    if sum(amounts[key] for key in ("1", "2", "3", "4")) != amounts["total"] or weights["total"] != 10000:
        return None
    if amounts["bond"] + amounts["abs"] != amounts["1"] or amounts["repo"] > amounts["2"]:
        return None
    if any(weights[key] != _rounded_weight(amounts[key], amounts["total"])
           or match["w" + key] in ZERO and amounts[key] != 0 for key in keys):
        return None
    if abs(sum(weights[key] for key in ("1", "2", "3", "4")) - 10000) * 2 > 4:
        return None
    names = ("固定收益投资", "买入返售金融资产", "银行存款和结算备付金合计", "其他资产")
    rows = [{"rank": rank, "name": name, "amount": _decimal(amounts[str(rank)]), "weightPct": _decimal(weights[str(rank)])}
            for rank, name in enumerate(names, 1)]
    subs = [{"parentRank": parent, "name": name, "amount": _decimal(amounts[key]), "weightPct": _decimal(weights[key])}
            for parent, name, key in ((1, "债券", "bond"), (1, "资产支持证券", "abs"), (2, "买断式回购的买入返售金融资产", "repo"))]
    return {"rows": rows, "subrows": subs, "total": {"rank": 5, "name": "合计", "amount": _decimal(amounts["total"]), "weightPct": "100.00"}}


def _other_values(table):
    body = _phrase("序号") + r"\s*" + _phrase("名称") + r"\s*" + _phrase("金额") + r"\s*[（(]\s*元\s*[）)]\s*"
    for rank, name in enumerate((*OTHER_NAMES, "合计"), 1):
        body += str(rank) + r"\s*" + _phrase(name) + r"\s+" + _field("a" + str(rank)) + r"\s*"
    match = _body_match(table, body)
    if not match:
        return None
    amounts = [_cents(match["a" + str(rank)]) for rank in range(1, 9)]
    if any(value is None for value in amounts) or amounts[6] != 0 or sum(amounts[:7]) != amounts[7]:
        return None
    return {"rows": [{"rank": rank, "name": name, "amount": _decimal(amounts[rank - 1])} for rank, name in enumerate(OTHER_NAMES, 1)],
            "total": {"rank": 8, "name": "合计", "amount": _decimal(amounts[7])}}


def _number(value):
    return tuple(map(int, value.split("."))) if isinstance(value, str) and re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){0,2}", value) else None


def _same(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[key], value) for key, value in right.items())
    if isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _table(raw, other=False):
    if not isinstance(raw, dict) or raw.get("sectionBounded") is not True or raw.get("pageNumbersVerified") is not True:
        return None
    start, end, numbers = raw.get("startPage"), raw.get("endPage"), raw.get("pageNumbers")
    if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= 10000
            or end - start >= MAX_TABLE_PAGES or not _same(numbers, list(range(start, end + 1)))):
        return None
    text, heading, ending = raw.get("text"), raw.get("heading"), raw.get("endHeading")
    if not all(isinstance(value, str) and value.strip() for value in (text, heading, ending)) or len(heading) > 120 or len(ending) > 120:
        return None
    if len(text) > (MAX_OTHER_TEXT if other else MAX_ASSET_TEXT):
        return None
    title = (OTHER_TITLE if other else ASSET_TITLE).fullmatch(re.sub(r"\s+", "", heading))
    closing = SECTION.fullmatch(ending)
    if not title or not closing:
        return None
    section = title[1]
    end_section = re.sub(r"[§\s]", "", closing["number"])
    current, following = _number(section), _number(end_section)
    if raw.get("section") != section or raw.get("endSection") != end_section or not following:
        return None
    adjacent = following == current[:-1] + (current[-1] + 1,)
    chapter = other and following == (current[0] + 1,) and closing["number"].startswith("§")
    if not (adjacent or chapter):
        return None
    result = {"section": section, "endSection": end_section, "heading": heading, "endHeading": ending,
              "startPage": start, "endPage": end, "pageNumbers": list(numbers), "sectionBounded": True,
              "pageNumbersVerified": True, "text": text, "amountUnit": "元"}
    values = _other_values(result) if other else _asset_values(result)
    if values is None:
        return None
    for key, value in values.items():
        if not _same(raw.get(key), value):
            return None
    result.update(values)
    if not other:
        result["ratioBasis"] = "fund-total-assets"
        if raw.get("ratioBasis") != result["ratioBasis"]:
            return None
    return result if raw.get("amountUnit") == "元" else None


def normalize_nonstock_assets(evidence):
    """Validate all short source tables and return only the public schema."""
    if not isinstance(evidence, dict) or type(evidence.get("schemaVersion")) is not int or evidence.get("schemaVersion") != 1 or evidence.get("evidenceType") != TYPE:
        return None
    if (evidence.get("confirmed") is not True or evidence.get("method") != METHOD
            or evidence.get("scope") != "period-end-stock-book-value" or evidence.get("stockBookValue") != "0.00"
            or evidence.get("amountUnit") != "元" or not _source(evidence.get("sourceUrl"))
            or not _date(evidence.get("periodEnd")) or not _date(evidence.get("publishedAt"))
            or evidence["publishedAt"] < evidence["periodEnd"]
            or _period(evidence.get("reportPeriodText")) != (evidence.get("kind"), evidence["periodEnd"])
            or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("sourceTextHash", "")))):
        return None
    asset = _table(evidence.get("assetTable"))
    other = _table(evidence.get("otherAssetsTable"), True)
    if (not asset or not other or other["startPage"] < asset["endPage"]
            or _number(other["section"])[0] != _number(asset["section"])[0]
            or _number(other["section"]) <= _number(asset["endSection"])
            or asset["rows"][-1]["amount"] != other["total"]["amount"]):
        return None
    pages = sorted(set(asset["pageNumbers"] + other["pageNumbers"]))
    if type(evidence.get("page")) is not int or evidence.get("page") != asset["startPage"] or not _same(evidence.get("pages"), pages):
        return None
    return {"schemaVersion": 1, "evidenceType": TYPE, "confirmed": True, "method": METHOD,
            "scope": "period-end-stock-book-value", "stockBookValue": "0.00", "amountUnit": "元",
            **{key: evidence[key] for key in ("sourceUrl", "periodEnd", "publishedAt", "kind", "reportPeriodText", "sourceTextHash")},
            "page": asset["startPage"], "pages": pages, "assetTable": asset, "otherAssetsTable": other}


def valid_nonstock_assets(evidence):
    return normalize_nonstock_assets(evidence) is not None


def _clean_page(page):
    lines = []
    for line in str(page or "").replace("\r", "\n").splitlines():
        line = line.strip()
        compact = re.sub(r"\s+", "", line)
        if re.fullmatch(r"第\d+页(?:共\d+页)?", compact) or PERIOD.search(compact) and compact.endswith("报告"):
            continue
        if line:
            lines.append(line)
    return "\n".join(lines)


def _regions(pages, numbers, other=False):
    for index, page in enumerate(pages):
        for heading in SECTION.finditer(page):
            if not (OTHER_TITLE if other else ASSET_TITLE).fullmatch(re.sub(r"\s+", "", heading["heading"])):
                continue
            fragments = [page[heading.start():]] + pages[index + 1:index + MAX_TABLE_PAGES]
            joined = "\n".join(fragments)
            sections = list(SECTION.finditer(joined))
            if len(sections) < 2:
                continue
            closing = sections[1]
            offset = 0
            last = index
            for relative, fragment in enumerate(fragments):
                if offset <= closing.start():
                    last = index + relative
                offset += len(fragment) + 1
            mapped = numbers[index:last + 1]
            if not mapped or any(type(value) is not int or value < 1 or value != mapped[0] + i for i, value in enumerate(mapped)):
                continue
            raw = {"section": re.sub(r"\s+", "", heading["number"]), "endSection": re.sub(r"[§\s]", "", closing["number"]),
                   "heading": heading["heading"].strip(), "endHeading": closing["heading"].strip(),
                   "startPage": mapped[0], "endPage": mapped[-1], "pageNumbers": mapped, "sectionBounded": True,
                   "pageNumbersVerified": True, "amountUnit": "元", "text": joined[:closing.end()].strip()}
            if not other:
                raw["ratioBasis"] = "fund-total-assets"
            if len(raw["text"]) > (MAX_OTHER_TEXT if other else MAX_ASSET_TEXT):
                continue
            values = _other_values(raw) if other else _asset_values(raw)
            if values:
                raw.update(values)
                result = _table(raw, other)
                if result:
                    yield result


def parse_nonstock_assets(pages, report, *, page_numbers=None):
    """Recognize one unambiguous pair of closed, exhaustive source tables.

    Without ``page_numbers``, input must be physical PDF pages in order. For
    MinerU pass the verified mapping; unknown or missing table pages reject the
    proof. The caller must not remap just the top-level ``page`` afterwards.
    """
    if not isinstance(pages, (list, tuple)) or not pages or not isinstance(report, dict):
        return None
    if page_numbers is not None and len(page_numbers) != len(pages):
        return None
    numbers = list(page_numbers) if page_numbers is not None else list(range(1, len(pages) + 1))
    period_texts = {match[0] for page in pages for match in PERIOD.finditer(re.sub(r"\s+", "", str(page or "")[:500]))}
    expected = (report.get("kind"), report.get("periodEnd"))
    if not period_texts or any(_period(value) != expected for value in period_texts):
        return None
    cleaned = [_clean_page(page) for page in pages]
    assets, others = list(_regions(cleaned, numbers)), list(_regions(cleaned, numbers, True))
    results = []
    for asset in assets:
        for other in others:
            proof = {"schemaVersion": 1, "evidenceType": TYPE, "confirmed": True, "method": METHOD,
                     "scope": "period-end-stock-book-value", "stockBookValue": "0.00", "amountUnit": "元",
                     **{key: report.get(key) for key in ("sourceUrl", "periodEnd", "publishedAt", "kind")},
                     "reportPeriodText": sorted(period_texts)[0],
                     "sourceTextHash": hashlib.sha256("\n\n".join(str(page or "") for page in pages).encode()).hexdigest(),
                     "page": asset["startPage"], "pages": sorted(set(asset["pageNumbers"] + other["pageNumbers"])),
                     "assetTable": asset, "otherAssetsTable": other}
            result = normalize_nonstock_assets(proof)
            if result:
                results.append(result)
    return results[0] if len(results) == 1 else None
