"""Bounded public-data adapters. Website JavaScript is parsed, never executed."""
from __future__ import annotations

import calendar
import datetime as dt
import io
import json
import logging
import math
import re
import time
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

HOLDINGS_TOPLINE = 100  # Verified API value; 10000 silently falls back to ten rows.


class DataError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def stock_identifiers(code, market):
    """Venue-specific candidates; every provider must confirm the returned identity."""
    code = str(code)
    if market == "HK" and re.fullmatch(r"\d{4,5}", code):
        code = code.zfill(5)
        return code, "116." + code, f"{int(code):04d}.HK", "hk" + code, "HKD", "HK"
    if market not in ("CN", "BJ") or not re.fullmatch(r"\d{6}", code):
        raise DataError("UNSUPPORTED_MARKET")
    if code.startswith("6"):
        return code, "1." + code, code + ".SS", "sh" + code, "CNY", "SH"
    if code.startswith(("0", "3")):
        return code, "0." + code, code + ".SZ", "sz" + code, "CNY", "SZ"
    if market == "BJ" or code.startswith(("4", "8", "92")):
        # Candidate only: Yahoo must explicitly confirm Beijing exchange metadata.
        return code, "0." + code, code + ".BJ", "bj" + code, "CNY", "BJ"
    raise DataError("UNSUPPORTED_MARKET")


def checked_price_rows(values, start, end):
    rows = {}
    for value in values:
        date = iso_date(value.get("date"))
        low, high, close = (number(value.get(key)) for key in ("low", "high", "close"))
        if date and start <= date <= end and all(x is not None and x > 0 for x in (low, high, close)) and low <= close <= high:
            rows[date] = {"date": date, "low": low, "high": high, "close": close}
    if not rows:
        raise DataError("NO_PRICE_DATA")
    return [rows[date] for date in sorted(rows)]


def parse_eastmoney_prices(payload, code, start, end):
    data = payload.get("data")
    if not isinstance(data, dict) or str(data.get("code", "")) != code:
        raise DataError("PRICE_IDENTITY_MISMATCH")
    values = data.get("klines")
    if not isinstance(values, list):
        raise DataError("INVALID_PRICE_FORMAT")
    rows = []
    for value in values:
        cells = value.split(",")
        if len(cells) >= 5:
            rows.append({"date": cells[0], "close": cells[2], "high": cells[3], "low": cells[4]})
    return checked_price_rows(rows, start, end)


def parse_yahoo_prices(payload, symbol, currency, venue, start, end):
    result = payload.get("chart", {}).get("result")
    if not isinstance(result, list) or len(result) != 1:
        raise DataError("INVALID_PRICE_FORMAT")
    result = result[0]
    meta = result.get("meta", {})
    if meta.get("symbol") != symbol:
        raise DataError("PRICE_IDENTITY_MISMATCH")
    if meta.get("currency") != currency:
        raise DataError("PRICE_CURRENCY_MISMATCH")
    expected_exchanges = {"SH": {"SHH"}, "SZ": {"SHZ"}, "HK": {"HKG"}}
    if venue in expected_exchanges and meta.get("exchangeName") not in expected_exchanges[venue]:
        raise DataError("PRICE_EXCHANGE_MISMATCH")
    if venue == "BJ" and "beijing" not in str(meta.get("fullExchangeName", "")).lower():
        raise DataError("PRICE_EXCHANGE_MISMATCH")
    # Yahoo's quote arrays are not dividend-adjusted, but past quotes can be
    # split-adjusted. Request split events through retrieval day and decline this
    # source when any are present, leaving raw Tencent/Eastmoney as alternatives.
    if result.get("events", {}).get("splits"):
        raise DataError("PRICE_CORPORATE_ACTION_UNRESOLVED")
    indicators = result.get("indicators", {})
    quote = indicators.get("quote")
    timestamps = result.get("timestamp")
    if not isinstance(quote, list) or len(quote) != 1 or not isinstance(timestamps, list):
        raise DataError("INVALID_PRICE_FORMAT")
    quote = quote[0]
    if any(not isinstance(quote.get(k), list) or len(quote[k]) != len(timestamps) for k in ("low", "high", "close")):
        raise DataError("INVALID_PRICE_FORMAT")
    timezone = ZoneInfo("Asia/Hong_Kong" if venue == "HK" else "Asia/Shanghai")
    rows = []
    for index, timestamp in enumerate(timestamps):
        if not isinstance(timestamp, (int, float)):
            continue
        date = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).astimezone(timezone).date().isoformat()
        rows.append({"date": date, "low": quote["low"][index], "high": quote["high"][index], "close": quote["close"][index]})
    # Never read indicators.adjclose or apply an automatic adjustment.
    return checked_price_rows(rows, start, end)


def parse_tencent_prices(payload, symbol, code, start, end):
    data = payload.get("data", {}).get(symbol)
    if not isinstance(data, dict):
        raise DataError("PRICE_IDENTITY_MISMATCH")
    quote = data.get("qt", {}).get(symbol)
    if not isinstance(quote, list) or len(quote) < 3 or str(quote[2]).zfill(len(code)) != code:
        raise DataError("PRICE_IDENTITY_MISMATCH")
    values = data.get("day")
    if not isinstance(values, list):
        if "qfqday" in data or "hfqday" in data:
            raise DataError("ADJUSTED_PRICE_REJECTED")
        raise DataError("INVALID_PRICE_FORMAT")
    rows = [{"date": row[0], "close": row[2], "high": row[3], "low": row[4]} for row in values if isinstance(row, list) and len(row) >= 5]
    return checked_price_rows(rows, start, end)


def number(value):
    try:
        n = float(str(value).replace(",", "").replace("%", "").strip())
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def iso_date(value):
    match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", str(value))
    if not match:
        return None
    try:
        return dt.date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def report_period(title):
    compact = re.sub(r"\s+", "", title)
    year = re.search(r"(20\d{2})年", compact)
    if not year or any(x in compact for x in ("提示", "摘要", "更正")):
        return None
    year = int(year[1])
    q = re.search(r"第?([1-4一二三四])季度", compact)
    if q:
        quarter = int(q[1]) if q[1].isdigit() else "一二三四".index(q[1]) + 1
        month = quarter * 3
        end = dt.date(year, month, calendar.monthrange(year, month)[1])
        return end.isoformat(), "quarterly"
    if "中期报告" in compact or "半年度报告" in compact:
        return f"{year}-06-30", "semiannual"
    if "年度报告" in compact:
        return f"{year}-12-31", "annual"
    return None


def js_literal(text, key):
    """Decode the quoted content property without eval or running a JS engine."""
    m = re.search(r"(?:\"?" + re.escape(key) + r"\"?)\s*:\s*([\"'])", text)
    if not m:
        raise DataError("INVALID_FORMAT")
    quote, chars, escaped = m[1], [], False
    i = m.end()
    while i < len(text):
        ch = text[i]
        if not escaped and ch == quote:
            raw = "".join(chars)
            if quote == '"':
                try:
                    return json.loads('"' + raw + '"')
                except json.JSONDecodeError:
                    raise DataError("INVALID_FORMAT") from None
            raw = raw.replace("\\'", "'").replace('"', '\\"')
            try:
                return json.loads('"' + raw + '"')
            except json.JSONDecodeError:
                raise DataError("INVALID_FORMAT") from None
        chars.append(ch)
        escaped = not escaped if ch == "\\" else False
        i += 1
    raise DataError("TRUNCATED_RESPONSE")


class HoldingRows(list):
    """List-compatible rows with parser coverage that the collector publishes."""
    def __init__(self, rows, excluded_rows=0, metadata=None):
        super().__init__(rows)
        self.excluded_rows = excluded_rows
        self.metadata = metadata or {}


def parse_holdings_html(text):
    content = js_literal(text, "content") if "apidata" in text else text
    soup = BeautifulSoup(content, "html.parser")
    result = {}
    for section in soup.select(".box") or [soup]:
        heading = section.select_one("h4")
        if heading is None:
            continue
        label = heading.get_text(" ", strip=True)
        period = iso_date(section.get_text(" ", strip=True))
        if period is None:
            year_q = re.search(r"(20\d{2})年\s*([1-4])\s*季度", label)
            if year_q:
                month = int(year_q[2]) * 3
                period = dt.date(int(year_q[1]), month, calendar.monthrange(int(year_q[1]), month)[1]).isoformat()
        if period is None:
            continue
        table = section.find("table")
        if table is None:
            continue
        headers = [re.sub(r"\s+", "", x.get_text()) for x in table.select("thead th")]
        if not headers:
            headers = [re.sub(r"\s+", "", x.get_text()) for x in table.select("tr")[0].select("th,td")]
        def index(prefix):
            return next((i for i, h in enumerate(headers) if prefix in h), None)
        ix = {"code": index("股票代码"), "name": index("股票名称"), "weight": index("占净值"), "shares": index("持股数"), "value": index("持仓市值")}
        if any(ix[k] is None for k in ("code", "name", "weight", "shares", "value")):
            continue
        holdings, excluded_rows = [], 0
        for row in table.select("tbody tr") or table.select("tr")[1:]:
            cells = [x.get_text(" ", strip=True) for x in row.select("td")]
            if len(cells) <= max(ix.values()):
                if cells:
                    excluded_rows += 1
                continue
            code = cells[ix["code"]].strip()
            if not re.fullmatch(r"\d{5,6}", code):
                excluded_rows += 1
                continue
            market = "HK" if len(code) == 5 else "CN"
            shares, value, weight = (number(cells[ix[k]]) for k in ("shares", "value", "weight"))
            shares_factor = 10000 if "万股" in headers[ix["shares"]] else 1
            value_factor = 10000 if "万元" in headers[ix["value"]] else 1
            if weight is None or weight < 0 or weight > 100:
                excluded_rows += 1
                continue
            holdings.append({"stockCode": code, "stockName": cells[ix["name"]], "market": market,
                             "currency": "HKD" if market == "HK" else "CNY", "shares": shares * shares_factor if shares is not None else None,
                             "weightPct": weight, "marketValueYuan": value * value_factor if value is not None else None})
        if holdings or excluded_rows:
            # Even a large requested topline does not prove the upstream omitted
            # nothing, so an interim/annual table never automatically means full.
            result[period] = HoldingRows(holdings, excluded_rows)
    if not result:
        raise DataError("NO_HOLDINGS")
    return result


def parse_overview(text):
    soup = BeautifulSoup(text, "html.parser")
    fields = {}
    for row in soup.select("table tr"):
        cells = row.select("th,td")
        for i in range(0, len(cells) - 1, 2):
            fields[cells[i].get_text("", strip=True)] = cells[i + 1].get_text(" ", strip=True)
    nav_text = fields.get("资产规模", "")
    nav_match = re.search(r"([\d.,]+)\s*亿元", nav_text)
    name = fields.get("基金简称") or fields.get("基金全称")
    if not name:
        raise DataError("INVALID_FORMAT")
    manager_names = re.split(r"[、,，\s]+", fields.get("基金经理人", ""))
    return {"name": name, "company": fields.get("基金管理人", ""),
            "foundationDate": next((iso_date(value) for key, value in fields.items()
                                     if key.startswith("成立日期") and iso_date(value)), None),
            "managers": [{"name": n, "startDate": None, "endDate": None} for n in manager_names if n],
            "overviewNavYuan": number(nav_match[1]) * 1e8 if nav_match else None,
            "overviewNavDate": iso_date(nav_text)}


def parse_announcements(data, as_of, expected_code=None):
    rows = data.get("Data")
    if not isinstance(rows, list):
        raise DataError("INVALID_FORMAT")
    reports = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_code = next((str(row[k]).zfill(6) for k in ("FCODE", "FUND_CODE", "FUNDCODE", "fundcode") if row.get(k) is not None), None)
        if expected_code and row_code and row_code != expected_code:
            continue
        title = next((str(row[k]) for k in ("TITLE", "title", "NOTICE_TITLE") if row.get(k)), "")
        # The endpoint has used multiple names; never rely on insertion order.
        published = next((iso_date(row[k]) for k in ("PUBLISHDATEDESC", "PUBLISHDATE", "ADATE", "NOTICE_DATE", "publishDate", "notice_date") if row.get(k)), None)
        if published is None:
            published = next((iso_date(v) for v in row.values() if iso_date(v)), None)
        article = next((str(row[k]) for k in ("ART_CODE", "art_code", "ID", "IDNO", "REPORTID") if str(row.get(k, "")).startswith("AN")), None)
        if article is None:
            article = next((str(v) for v in row.values() if re.fullmatch(r"AN\d+", str(v))), None)
        period = report_period(title)
        if period and published and article and published <= as_of and period[0] <= as_of:
            reports.append({"title": title, "periodEnd": period[0], "kind": period[1], "publishedAt": published,
                            "articleCode": article, "sourceFundCode": row_code,
                            "sourceUrl": f"https://pdf.dfcfw.com/pdf/H2_{article}_1.pdf"})
    return sorted(reports, key=lambda r: (r["periodEnd"], r["publishedAt"]), reverse=True)


def pdf_portfolio_identity(pages):
    """Read only explicit identity labels and their adjacent code-only rows.

    PDF table labels routinely wrap inside words. Keep the code rows bounded
    and stop at the next text label so security codes and share totals elsewhere
    on the page cannot become share-class aliases.
    """
    spaced = lambda label: r"\s*".join(label)
    intro = "\n".join(pages[:12])
    main = re.search(spaced("基金主代码") + r"\s*[:：]?\s*(\d{6})(?!\d)", intro)
    main_code = main[1] if main else None
    labels = ["下属分级基金的交易代码", "下属分类基金的交易代码"]
    classes = re.search("(?:" + "|".join(spaced(label) for label in labels) + ")", intro)
    codes = []
    if classes:
        tail = intro[classes.end():].lstrip(" \t:：")
        for line in tail.splitlines()[:8]:
            line = line.strip()
            if not line and not codes:
                continue
            if not re.fullmatch(r"\d{6}(?:[ \t]+\d{6})*", line):
                break
            codes.extend(line.split())
    codes = list(dict.fromkeys(codes))
    if main_code and codes and main_code not in codes:
        codes = []
    multi_class = bool(classes or re.search(
        "|".join(spaced(label) for label in ("下属分级基金", "下属分类基金", "各类基金份额"))
        + r"|[AC]\s*类\s*基\s*金\s*份\s*额", intro))
    return main_code, codes, multi_class


def parse_pdf_pages(pages, source_url, period_end, kind):
    """Conservative extraction of one NAV cell and one bounded strategy excerpt."""
    from report_extract import strategy_section_end
    nav = None
    narrative = None
    main_code, class_codes, multi_class = pdf_portfolio_identity(pages)
    nav_heading = r"\s*".join("期末基金资产净值")
    unit_nav_heading = r"\s*".join("期末基金份额净值")
    if kind in ("semiannual", "annual"):
        all_text = re.sub(r"[ \t]+", "", "\n".join(pages))
        # The first column of the balance sheet is this report's period end.
        # Confirm that column heading before accepting its fund-wide NAV.
        balance_sections = re.split(r"(?:\d+\.)+1\s*资产负债表", all_text)
        for balance in balance_sections[1:]:
            balance = re.split(r"(?:\d+\.)+2\s*利润表", balance, maxsplit=1)[0]
            date = dt.date.fromisoformat(period_end)
            required_date = f"{date.year}年{date.month}月{date.day}日"
            if "本期末" not in balance or required_date not in balance:
                continue
            nav_match = re.search(r"净资产合计\s*([\d,]+\.\d{2})", balance)
            if nav_match:
                nav = number(nav_match[1])
                break
    start = f"{period_end[:4]}-01-01"
    if kind == "quarterly":
        start = f"{period_end[:4]}-{((int(period_end[5:7]) - 1) // 3) * 3 + 1:02d}-01"
    for page_num, text in enumerate(pages, 1):
        compact = re.sub(r"[ \t]+", " ", text)
        # Multiple share-class columns must be summed, while a historical comparison
        # column must not be. Only accept a single quarterly NAV cell here.
        if nav is None and kind == "quarterly" and not multi_class:
            m = re.search(nav_heading + r"\s*(?:[（(]\s*(?:人民币)?元\s*[）)]\s*)?([\d,]+\.\d{2})([^\n]*)", compact)
            if m and not re.search(r"\d[\d,]*\.\d{2}", m[2]):
                nav = number(m[1])
        elif nav is None and kind == "quarterly" and len(set(class_codes)) >= 2:
            # Quarterly metric columns are share classes, not different dates.
            # Sum only when the report itself identifies exactly those columns.
            metric = re.search(nav_heading + r"([^\n]*(?:\n(?!\s*(?:\d+[.、]|" + unit_nav_heading + r"))[^\n]*){0,5})", compact)
            if metric:
                values = re.findall(r"(?<![\d.])[\d,]+\.\d{2}(?!\d)", re.split(unit_nav_heading, metric[1], maxsplit=1)[0])
                if len(values) == len(set(class_codes)):
                    parsed = [number(value) for value in values]
                    if all(value is not None and value >= 0 for value in parsed):
                        nav = sum(parsed)
        # Skip the contents page. Sections differ between report templates.
        for m in re.finditer(r"(?:\d+\.){1,2}\d*\s*(?:报告期内基金的投资策略和运作分析|报告期内基金投资策略和运作分析|管理人对报告期内本基金的投资策略和业绩表现的说明)", compact):
            tail = compact[m.end():]
            if re.match(r"[ .·…\d\n]{0,40}(?:\d+\.)", tail) or re.match(r"[ .·…]{3,}", tail):
                continue
            section_pages = []
            for index in range(page_num - 1, min(len(pages), page_num + 2)):
                page_text = tail if index == page_num - 1 else pages[index]
                boundary = strategy_section_end(page_text)
                clean = re.sub(r"第\s*\d+\s*页\s*共\s*\d+\s*页", "", page_text if boundary is None else page_text[:boundary])
                clean = re.sub(r"\s+", "", clean).strip()
                section_pages.append((index + 1, clean))
                if boundary is not None:
                    break
            candidates = []
            for number_, page_text in section_pages:
                sentences = re.split(r"(?<=[。！？；])", page_text)
                matching = [s for s in sentences if len(s) >= 12 and re.search(r"本基金|组合|配置|增持|减持|增配|减配", s)]
                if matching:
                    # All chosen sentences come from the same cited PDF page.
                    # Selection changes no wording and asserts no inferred motive.
                    score = sum(2 if re.search(r"调整|增加|降低|增持|减持|增配|减配|买入|卖出", s) else 1 for s in matching)
                    candidates.append((score, number_, matching))
            if candidates:
                _, excerpt_page, sentences = max(candidates, key=lambda item: item[0])
                excerpt = "……".join(sentences[:3])[:260]
                selection = "operation-sentences"
            elif section_pages:
                excerpt_page, excerpt = section_pages[0]
                excerpt = excerpt[:260]
                selection = "section-opening"
            else:
                excerpt, excerpt_page, selection = "", page_num, "section-opening"
            # A concise, explicitly selected operation sentence remains useful
            # evidence. Keep the longer threshold for an unfiltered opening.
            if len(excerpt) >= (20 if selection == "operation-sentences" else 40):
                narrative = {"text": excerpt, "sourceUrl": source_url, "page": excerpt_page,
                             "periodStart": start, "periodEnd": period_end, "excerptSelection": selection}
                break
        if narrative and (nav is not None or kind != "quarterly"):
            break
    return {"navYuan": nav, "narrative": narrative,
            "portfolioCode": main_code,
            "shareClassCodes": class_codes}


def parse_no_equities(pages, report):
    pattern = re.compile(r"(?:本基金)?(?:本报告期末|报告期末|截至(?:本)?报告期末)[，,:：]?(?:本基金)?(?:未持有|未投资于|未投资)股票(?:资产|投资|组合)?(?=[。；，]|$)")
    zero = r"(?:-|0(?:\.0+)?)"
    zero_rows = re.compile(r"(?m)^[ \t]*(?:[1-9]\d*[ \t]+)?权益投资[ \t]+" + zero + r"[ \t]+" + zero
                           + r"[ \t]*\r?\n[ \t]*其中[：:][ \t]*股票[ \t]+" + zero + r"[ \t]+" + zero + r"[ \t]*(?=\r?$)")
    heading = r"\s*".join("期末按公允价值占基金资产净值比例大小排序的所有股票投资明细")
    empty_detail = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+){1,2}[ \t]*" + heading
                              + r"\s*(?:注\s*[:：]\s*)?无[。.]?)(?=[ \t]*(?:\r?\n|$))")
    for page_num, page in enumerate(pages, 1):
        compact = re.sub(r"\s+", "", page)
        match = pattern.search(compact)
        if match:
            return {"confirmed": True, "sourceUrl": report["sourceUrl"], "periodEnd": report["periodEnd"],
                    "publishedAt": report["publishedAt"], "kind": report["kind"], "title": report["title"],
                    "page": page_num, "text": match[0][:200]}
        # Explicit empty full-stock detail plus a corroborating all-zero equity
        # allocation row on this same physical page. A fund name or a missing
        # stock table is never evidence of no holdings.
        detail = empty_detail.search(page)
        allocation = re.search(r"\d+(?:\.\d+){1,2}\s*(?:报告)?期末基金资产组合情况", page)
        if not detail or not allocation or allocation.end() >= detail.start():
            continue
        section = re.split(r"\n\s*\d+(?:\.\d+){1,2}\s*[\u4e00-\u9fff]",
                           page[allocation.end():detail.start()], maxsplit=1)[0]
        header = re.sub(r"\s+", "", section[:400])
        rows = zero_rows.search(section)
        if rows and "金额" in header and "占基金总资产的比例" in header:
            return {"confirmed": True, "evidenceType": "empty-stock-detail-section",
                    "sourceUrl": report["sourceUrl"], "periodEnd": report["periodEnd"],
                    "publishedAt": report["publishedAt"], "kind": report["kind"], "title": report["title"],
                    "page": page_num, "text": detail.group(1).strip(), "assetAllocationText": rows[0].strip()}
    return None


def parse_pdf_holdings(pages):
    """Recover only explicit numeric stock rows inside a stock-detail section.

    Text-layer extraction can leave table rows broken. Unrecognized rows remain
    missing and the recovered subset is never declared the complete portfolio.
    """
    # Preserve physical page offsets and line breaks for bounded row checks.
    normalized_pages = [re.sub(r"[ \t]+", " ", page) for page in pages]
    normalized = "\n".join(normalized_pages)
    offsets, position = [], 0
    for page in normalized_pages:
        offsets.append(position)
        position += len(page) + 1
    def page_at(offset):
        return sum(start <= offset for start in offsets)
    section = None
    metadata = {}
    for heading in re.finditer(r"按公允价值占基金资产净值比例大小\s*(?:排序|排名)的\s*(所有|前十名|前十|前五名)?\s*股票投资明细", normalized):
        candidate = normalized[heading.end():]
        prefix = re.search(r"(\d+(?:\.\d+){1,2})\s*(?:报告期末|期末)?\s*$", normalized[max(0, heading.start() - 100):heading.start()])
        section_number = tuple(map(int, prefix[1].split('.'))) if prefix else None
        end = None
        for boundary in re.finditer(r"\n\s*(\d+(?:\.\d+){1,2})\s*(?:报告期|期末|期货|投资组合|重大|基金|债券)", candidate):
            if section_number is None or tuple(map(int, boundary[1].split('.'))) > section_number:
                end = boundary
                break
        if end:
            candidate = candidate[:end.start()]
        if all(word in candidate[:1200] for word in ("股票代码", "股票名称", "数量", "公允价值")):
            section = candidate
            metadata = {"coverage": "full" if heading[1] == "所有" else "top10" if heading[1] in ("前十名", "前十") else "unknown",
                        "sectionNumber": prefix[1] if prefix else None,
                        "nextSectionNumber": end[1] if end else None,
                        "sectionBounded": bool(prefix and end),
                        "startPage": page_at(heading.start()),
                        "endPage": page_at(heading.end() + end.start(1)) if end else None}
            break
    if section is None:
        return HoldingRows([])
    if re.search(r"数量\s*[（(]\s*万股|公允价值\s*[（(]\s*万元", section[:1200]):
        return HoldingRows([])
    header = re.sub(r"\s+", "", section[:1200])
    units_verified = bool(re.search(r"数量[（(]股[）)]", header)
                          and re.search(r"公允价值[（(](?:人民币)?元[）)]", header))
    amount = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    pattern = re.compile(r"^\s*(\d{1,4})\s+(\d{5,6})\s+([^\n]+?)\s+(" + amount + r")\s+(" + amount + r")\s+(\d+(?:\.\d+)?)\s*%?\s*$", re.M)
    rows, ranks, seen = [], [], set()
    candidate_ranks = [int(match[1]) for match in re.finditer(r"(?m)^[ \t]*(\d{1,4})(?=[ \t]+[^ \t\r\n]|\r?$)", section)]
    for match in pattern.finditer(section):
        rank, code, name, shares, value, weight = match.groups()
        shares, value, weight = map(number, (shares, value, weight))
        if code in seen or shares is None or value is None or weight is None or not 0 <= weight <= 100 or shares < 0 or value < 0:
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", name) or len(name) > 80:
            continue
        seen.add(code)
        ranks.append(int(rank))
        market = "HK" if len(code) == 5 else "CN"
        rows.append({"stockCode": code, "stockName": name.strip(), "market": market,
                     "currency": "HKD" if market == "HK" else "CNY", "shares": shares,
                     "marketValueYuan": value, "weightPct": weight})
    if sum(row["weightPct"] for row in rows) > 100.1:
        return HoldingRows([])
    excluded = max(0, len(candidate_ranks) - len(rows))
    contiguous = bool(rows and ranks == list(range(1, len(rows) + 1)) and candidate_ranks == ranks)
    metadata.update({"rowsSeen": len(candidate_ranks), "parsedRows": len(rows), "excludedRows": excluded,
                     "contiguousRanks": contiguous, "unitsVerified": units_verified,
                     "complete": bool(metadata["sectionBounded"] and units_verified and contiguous and excluded == 0
                                      and metadata["coverage"] in {"full", "top10"}
                                      and (metadata["coverage"] != "top10" or len(rows) <= 10))})
    return HoldingRows(rows, excluded, metadata)


class PublicProvider:
    def __init__(self, timeout=18, attempts=2):
        self.timeout = min(max(timeout, 3), 30)
        self.attempts = min(max(attempts, 1), 2)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})

    def get(self, url, *, params=None, referer=None, binary=False, timeout_seconds=None, attempts_override=None):
        if urlparse(url).scheme != "https":
            raise DataError("INVALID_URL")
        headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"} if referer else {}
        request_timeout = max(0.5, min(float(timeout_seconds), 30)) if timeout_seconds is not None else self.timeout
        attempts = min(self.attempts, max(1, attempts_override)) if attempts_override is not None else self.attempts
        connect_timeout = min(3, request_timeout) if timeout_seconds is not None else 8
        for attempt in range(attempts):
            try:
                started = time.monotonic()
                response = self.session.get(url, params=params, headers=headers, timeout=(connect_timeout, request_timeout), stream=True)
                if response.status_code in (403, 404):
                    raise DataError("HTTP_REJECTED")
                response.raise_for_status()
                chunks, size = [], 0
                try:
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > 16 * 1024 * 1024:
                            raise DataError("RESPONSE_TOO_LARGE")
                        if time.monotonic() - started > request_timeout + connect_timeout:
                            raise DataError("TIMEOUT")
                        chunks.append(chunk)
                finally:
                    response.close()
                content = b"".join(chunks)
                if not content:
                    raise DataError("EMPTY_RESPONSE")
                if binary:
                    return content
                encoding = response.encoding if response.encoding and response.encoding.lower() != "iso-8859-1" else "utf-8"
                return content.decode(encoding, errors="replace")
            except requests.exceptions.SSLError:
                raise DataError("TLS_FAILED") from None
            except requests.exceptions.Timeout:
                error = "TIMEOUT"
            except requests.exceptions.HTTPError:
                error = "HTTP_ERROR"
            except requests.exceptions.RequestException:
                error = "NETWORK_ERROR"
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
        raise DataError(error)

    def overview(self, code):
        return parse_overview(self.get(f"https://fundf10.eastmoney.com/jbgk_{code}.html"))

    def holdings(self, code, year):
        text = self.get("https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
                        params={"type": "jjcc", "code": code, "topline": str(HOLDINGS_TOPLINE), "year": str(year), "month": "", "rt": str(time.time())},
                        referer=f"https://fundf10.eastmoney.com/ccmx_{code}.html")
        return parse_holdings_html(text)

    def announcements(self, code, as_of):
        reports, seen_pages = {}, set()
        self.announcement_error_code = None
        for page in range(1, 5):
            try:
                text = self.get("https://api.fund.eastmoney.com/f10/JJGG",
                                params={"fundcode": code, "pageIndex": page, "pageSize": 100, "type": 3},
                                referer=f"https://fundf10.eastmoney.com/jjgg_{code}_3.html")
                payload = json.loads(text)
                rows = payload.get("Data")
                if not isinstance(rows, list):
                    raise DataError("INVALID_FORMAT")
                fingerprint = json.dumps(rows, sort_keys=True)
                if fingerprint in seen_pages:
                    self.announcement_error_code = "ANNOUNCEMENT_PAGINATION_STALLED"
                    break
                seen_pages.add(fingerprint)
                parsed = parse_announcements(payload, as_of, expected_code=code)
                reports.update({report["sourceUrl"]: report for report in parsed})
                if len(rows) < 100 or parsed and min(report["periodEnd"] for report in parsed) < f"{int(as_of[:4])-1}-01-01":
                    break
                if page == 4:
                    self.announcement_error_code = "ANNOUNCEMENT_PAGE_LIMIT"
            except (ValueError, TypeError):
                raise DataError("INVALID_FORMAT") from None
            except DataError as error:
                if not reports:
                    raise
                self.announcement_error_code = error.code
                break
        return sorted(reports.values(), key=lambda r: (r["periodEnd"], r["publishedAt"]), reverse=True)

    def report_details(self, report, max_pages=300):
        from report_extract import extract_report

        data = self.get(report["sourceUrl"], binary=True)
        if not data.startswith(b"%PDF"):
            raise DataError("INVALID_PDF")
        def enrich(pages):
            holdings = parse_pdf_holdings(pages)
            return {"noEquitiesEvidence": parse_no_equities(pages, report), "pdfHoldings": holdings,
                    "pdfHoldingsMetadata": holdings.metadata}
        try:
            result = extract_report(data, report["sourceUrl"], report["periodEnd"], report["kind"],
                                    parse_pages=parse_pdf_pages, filename=report.get("title", "report.pdf"),
                                    max_pages=max_pages,
                                    enrich_pages=enrich)
        except Exception as error:
            code = getattr(error, "code", "PDF_PARSE_FAILED")
            raise DataError(code if re.fullmatch(r"[A-Z0-9_]+", str(code)) else "PDF_PARSE_FAILED") from None
        # Preserve the historical field while attaching page-cited MinerU/pypdf
        # metadata. MinerU markdown is consumed in memory and is never published.
        result.update({"reportPdfSourceUrl": report["sourceUrl"], "reportPdfPeriodEnd": report["periodEnd"],
                       "reportPdfKind": report["kind"]})
        return result

    def price_bars(self, code, market, start, end):
        if iso_date(start) != start or iso_date(end) != end or start > end:
            raise DataError("INVALID_PRICE_RANGE")
        code, secid, yahoo_symbol, tencent_symbol, currency, venue = stock_identifiers(code, market)
        params = {"secid": secid, "klt": 101, "fqt": 0, "beg": start.replace("-", ""), "end": end.replace("-", ""),
                  "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"}
        start_time = dt.datetime.combine(dt.date.fromisoformat(start), dt.time.min, tzinfo=dt.timezone.utc)
        retrieval_day = dt.datetime.now(dt.timezone.utc).date()
        events_end = max(dt.date.fromisoformat(end), retrieval_day) + dt.timedelta(days=1)
        yahoo_params = {"period1": int(start_time.timestamp()),
                        "period2": int(dt.datetime.combine(events_end, dt.time.min, tzinfo=dt.timezone.utc).timestamp()),
                        "interval": "1d", "events": "splits", "includeAdjustedClose": "false"}
        sources = [
            ("eastmoney", "https://push2his.eastmoney.com/api/qt/stock/kline/get", params,
             lambda data: parse_eastmoney_prices(data, code, start, end)),
            ("yahoo", "https://query1.finance.yahoo.com/v8/finance/chart/" + yahoo_symbol, yahoo_params,
             lambda data: parse_yahoo_prices(data, yahoo_symbol, currency, venue, start, end)),
            ("tencent", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
             {"param": f"{tencent_symbol},day,{start},{end},1000,"},
             lambda data: parse_tencent_prices(data, tencent_symbol, code, start, end)),
        ]
        deadline = time.monotonic() + 24
        failures = []
        for provider_name, base, query, parse in sources:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                failures.append({"provider": provider_name, "errorCode": "TIMEOUT"})
                break
            try:
                payload = json.loads(self.get(base, params=query, timeout_seconds=min(4, remaining / 2), attempts_override=1))
                rows = parse(payload)
                return {"currency": currency, "priceBasis": "unadjusted", "market": market, "stockCode": code,
                        "provider": provider_name, "sourceUrl": base + "?" + urlencode(query), "rows": rows,
                        "providerErrors": failures}
            except DataError as error:
                failures.append({"provider": provider_name, "errorCode": error.code})
            except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
                failures.append({"provider": provider_name, "errorCode": "INVALID_PRICE_FORMAT"})
        error = DataError("PRICE_SOURCES_UNAVAILABLE")
        error.provider_errors = failures
        raise error
