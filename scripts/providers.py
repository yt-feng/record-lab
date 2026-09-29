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

import requests
from bs4 import BeautifulSoup


class DataError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
        holdings = []
        for row in table.select("tbody tr") or table.select("tr")[1:]:
            cells = [x.get_text(" ", strip=True) for x in row.select("td")]
            if len(cells) <= max(ix.values()):
                continue
            code = cells[ix["code"]].strip()
            if not re.fullmatch(r"\d{5,6}", code):
                continue
            market = "HK" if len(code) == 5 else "CN"
            shares, value, weight = (number(cells[ix[k]]) for k in ("shares", "value", "weight"))
            shares_factor = 10000 if "万股" in headers[ix["shares"]] else 1
            value_factor = 10000 if "万元" in headers[ix["value"]] else 1
            if weight is None or weight < 0 or weight > 100:
                continue
            holdings.append({"stockCode": code, "stockName": cells[ix["name"]], "market": market,
                             "currency": "HKD" if market == "HK" else "CNY", "shares": shares * shares_factor if shares is not None else None,
                             "weightPct": weight, "marketValueYuan": value * value_factor if value is not None else None})
        if holdings:
            # Topline=100 may truncate an interim/annual list, so it never proves full coverage.
            result[period] = holdings
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


def parse_pdf_pages(pages, source_url, period_end, kind):
    """Conservative extraction of one NAV cell and one bounded strategy excerpt."""
    nav = None
    narrative = None
    joined_intro = "\n".join(pages[:5])
    multi_class = bool(re.search(r"下属(?:分级|分类)基金|各类基金份额|[AC]类基金份额", joined_intro))
    main_code = re.search(r"基金主代码\s*(\d{6})", joined_intro)
    classes = re.search(r"下属(?:分级|分类)基金的交易代码([^\n]*(?:\n[^\n]*)?)", joined_intro)
    class_codes = re.findall(r"\b\d{6}\b", classes[1]) if classes else []
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
            m = re.search(r"期末基金资产净值\s*([\d,]+\.\d{2})([^\n]*)", compact)
            if m and not re.search(r"\d[\d,]*\.\d{2}", m[2]):
                nav = number(m[1])
        # Skip the contents page. Sections differ between report templates.
        for m in re.finditer(r"(?:\d+\.){1,2}\d*\s*(?:报告期内基金的投资策略和运作分析|报告期内基金投资策略和运作分析|管理人对报告期内本基金的投资策略和业绩表现的说明)", compact):
            tail = compact[m.end():]
            if re.match(r"[ .·…\d\n]{0,40}(?:\d+\.)", tail) or re.match(r"[ .·…]{3,}", tail):
                continue
            section_pages = []
            for index in range(page_num - 1, min(len(pages), page_num + 2)):
                page_text = tail if index == page_num - 1 else pages[index]
                chunks = re.split(r"\n\s*(?:\d+\.){1,2}\d*\s*(?:报告期内基金|管理人对|基金的业绩|宏观经济)", page_text, maxsplit=1)
                clean = re.sub(r"第\s*\d+\s*页\s*共\s*\d+\s*页", "", chunks[0])
                clean = re.sub(r"\s+", "", clean).strip()
                section_pages.append((index + 1, clean))
                if len(chunks) > 1:
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
            if len(excerpt) >= 40:
                narrative = {"text": excerpt, "sourceUrl": source_url, "page": excerpt_page,
                             "periodStart": start, "periodEnd": period_end, "excerptSelection": selection}
                break
        if narrative and (nav is not None or kind != "quarterly"):
            break
    return {"navYuan": nav, "narrative": narrative,
            "portfolioCode": main_code[1] if main_code else None,
            "shareClassCodes": class_codes}


def parse_no_equities(pages, report):
    pattern = re.compile(r"(?:本基金)?(?:本报告期末|报告期末|截至(?:本)?报告期末)[，,:：]?(?:本基金)?(?:未持有|未投资于|未投资)股票(?=[。；，]|$)")
    for page_num, page in enumerate(pages, 1):
        compact = re.sub(r"\s+", "", page)
        match = pattern.search(compact)
        if match:
            return {"confirmed": True, "sourceUrl": report["sourceUrl"], "periodEnd": report["periodEnd"],
                    "publishedAt": report["publishedAt"], "kind": report["kind"], "title": report["title"],
                    "page": page_num, "text": match[0][:200]}
    return None


class PublicProvider:
    def __init__(self, timeout=18, attempts=2):
        self.timeout = min(max(timeout, 3), 30)
        self.attempts = min(max(attempts, 1), 2)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})

    def get(self, url, *, params=None, referer=None, binary=False):
        if urlparse(url).scheme != "https":
            raise DataError("INVALID_URL")
        headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"} if referer else {}
        for attempt in range(self.attempts):
            try:
                started = time.monotonic()
                response = self.session.get(url, params=params, headers=headers, timeout=(8, self.timeout), stream=True)
                if response.status_code in (403, 404):
                    raise DataError("HTTP_REJECTED")
                response.raise_for_status()
                chunks, size = [], 0
                try:
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > 16 * 1024 * 1024:
                            raise DataError("RESPONSE_TOO_LARGE")
                        if time.monotonic() - started > self.timeout + 8:
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
            if attempt + 1 < self.attempts:
                time.sleep(0.4 * (attempt + 1))
        raise DataError(error)

    def overview(self, code):
        return parse_overview(self.get(f"https://fundf10.eastmoney.com/jbgk_{code}.html"))

    def holdings(self, code, year):
        text = self.get("https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
                        params={"type": "jjcc", "code": code, "topline": "100", "year": str(year), "month": "", "rt": str(time.time())},
                        referer=f"https://fundf10.eastmoney.com/ccmx_{code}.html")
        return parse_holdings_html(text)

    def announcements(self, code, as_of):
        text = self.get("https://api.fund.eastmoney.com/f10/JJGG",
                        params={"fundcode": code, "pageIndex": 1, "pageSize": 100, "type": 3},
                        referer=f"https://fundf10.eastmoney.com/jjgg_{code}_3.html")
        try:
            return parse_announcements(json.loads(text), as_of, expected_code=code)
        except (ValueError, TypeError):
            raise DataError("INVALID_FORMAT") from None

    def report_details(self, report, max_pages=18):
        from pypdf import PdfReader
        logging.getLogger("pypdf").setLevel(logging.CRITICAL)
        data = self.get(report["sourceUrl"], binary=True)
        if not data.startswith(b"%PDF"):
            raise DataError("INVALID_PDF")
        try:
            reader = PdfReader(io.BytesIO(data))
            # Strategy/financial indicator pages are near the front; bounded parsing.
            pages = [p.extract_text() or "" for p in reader.pages[:max(1, min(max_pages, 80))]]
        except Exception:
            raise DataError("PDF_PARSE_FAILED") from None
        result = parse_pdf_pages(pages, report["sourceUrl"], report["periodEnd"], report["kind"])
        result["noEquitiesEvidence"] = parse_no_equities(pages, report)
        return result

    def price_bars(self, code, market, start, end):
        if market == "HK":
            secid = "116." + code.zfill(5)
        elif code.startswith(("6", "9")):
            secid = "1." + code
        elif code.startswith(("0", "3")):
            secid = "0." + code
        else:
            raise DataError("UNSUPPORTED_MARKET")
        params = {"secid": secid, "klt": 101, "fqt": 0, "beg": start.replace("-", ""), "end": end.replace("-", ""),
                  "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"}
        base = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        try:
            payload = json.loads(self.get(base, params=params))
            values = payload["data"]["klines"]
        except (ValueError, TypeError, KeyError):
            raise DataError("INVALID_PRICE_FORMAT") from None
        rows = []
        for value in values:
            cells = value.split(",")
            if len(cells) < 5:
                continue
            date = iso_date(cells[0])
            close, high, low = map(number, (cells[2], cells[3], cells[4]))
            if date and start <= date <= end and all(v is not None and v > 0 for v in (close, high, low)) and low <= close <= high:
                rows.append({"date": date, "low": low, "high": high, "close": close})
        if not rows:
            raise DataError("NO_PRICE_DATA")
        return {"currency": "HKD" if market == "HK" else "CNY", "priceBasis": "unadjusted", "market": market, "stockCode": code,
                "sourceUrl": base + "?" + urlencode(params), "rows": rows}
