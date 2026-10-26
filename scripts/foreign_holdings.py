"""Conservative extraction of foreign equity tables with explicit PDF cells.

The adapter requires identified table headers, original clipping rectangles,
explicit market/country columns and a bounded stock section.  It never infers a
security's exchange from its ticker.  PDF bytes and cell text remain private;
callers receive normalized holdings and a small completeness proof only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import math
import re


@dataclass(frozen=True)
class Cell:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str = ""


@dataclass
class GridPage:
    number: int
    cells: list[Cell]
    lines: list[tuple[float, str]]
    spans: list[tuple[float, float, str]] = field(default_factory=list)


def compact(text):
    return re.sub(r"\s+", "", text)


def _point(x, y, matrix):
    return (float(x) * matrix[0] + float(y) * matrix[2] + matrix[4],
            float(x) * matrix[1] + float(y) * matrix[3] + matrix[5])


def extract_grid_page(page, number):
    """Read rectangular clipping cells through pypdf's public visitor API.

    Unsupported PDFs without such geometry return no table proof.  Rotated,
    overlapping or nonrectangular clipping regions are deliberately not guessed.
    """
    paths, current, rectangles, chunks = [], [], set(), []

    def operand(operator, values, cm, _tm):
        nonlocal paths, current
        if operator == b"m":
            if current:
                paths.append(current)
            current = [_point(values[0], values[1], cm)]
        elif operator == b"l":
            current.append(_point(values[0], values[1], cm))
        elif operator == b"re":
            x, y, width, height = map(float, values)
            paths.append([_point(x, y, cm), _point(x + width, y, cm),
                          _point(x + width, y + height, cm), _point(x, y + height, cm)])
        elif operator in (b"W", b"W*"):
            for points in paths + [current]:
                if len(points) != 4:
                    continue
                xs = sorted(set(round(x, 2) for x, _ in points))
                ys = sorted(set(round(y, 2) for _, y in points))
                if len(xs) == len(ys) == 2 and xs[1] - xs[0] > 2 and ys[1] - ys[0] > 2:
                    if set(points) and len({(round(x, 2), round(y, 2)) for x, y in points}) == 4:
                        rectangles.add((*xs, *ys))
        elif operator in (b"n", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
            current, paths = [], []

    def text(value, cm, tm, _font, _size):
        if value.strip():
            chunks.append((*_point(tm[4], tm[5], cm), value.strip()))

    page.extract_text(visitor_operand_before=operand, visitor_text=text)
    grouped = {rectangle: [] for rectangle in rectangles}
    for x, y, value in chunks:
        candidates = [r for r in grouped if r[0] - .1 <= x <= r[1] + .1 and r[2] - .1 <= y <= r[3] + .1]
        if candidates:
            smallest = min(candidates, key=lambda r: (r[1] - r[0]) * (r[3] - r[2]))
            grouped[smallest].append(value)
    cells = [Cell(*bounds, " ".join(values)) for bounds, values in grouped.items()]
    lines = []
    for x, y, value in sorted(chunks, key=lambda chunk: (-round(chunk[1]), chunk[0])):
        if lines and abs(lines[-1][0] - y) < 1.5:
            lines[-1] = (lines[-1][0], lines[-1][1] + value)
        else:
            lines.append((y, value))
    return GridPage(number, cells, lines, chunks)


_HEADERS = {
    "rank": {"序号"}, "nameEn": {"公司名称（英文）", "公司名称(英文)"},
    "nameCn": {"公司名称（中文）", "公司名称(中文)"}, "code": {"证券代码", "股票代码"},
    "exchange": {"所在证券市场"}, "country": {"所属国家（地区）", "所属国家(地区)"},
    "shares": {"数量（股）", "数量(股)"}, "value": {"公允价值"},
    "weight": {"占基金资产净值比例（%）", "占基金资产净值比例(%)"},
}
_START = re.compile(r"^(\d+(?:\.\d+){1,3})期末按公允价值占基金资产净值比例大小排序的(所有|前十名)(?:股票|权益投资)明细")
_NEXT = re.compile(r"^(\d+(?:\.\d+){1,3})(?:报告期|期末|基金|投资组合|债券|资产)")


def _number(value):
    value = compact(value)
    if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
        return None
    result = float(value.replace(",", ""))
    return result if math.isfinite(result) else None


def _section(number):
    return tuple(int(part) for part in number.split("."))


def _table_header(page):
    for anchor in page.cells:
        if compact(anchor.text) not in _HEADERS["code"]:
            continue
        row = [cell for cell in page.cells if abs(cell.y0 - anchor.y0) < 1 and abs(cell.y1 - anchor.y1) < 1]
        result = {}
        for field, labels in _HEADERS.items():
            matches = [cell for cell in row if compact(cell.text) in labels]
            if len(matches) != 1:
                break
            result[field] = matches[0]
        if len(result) == len(_HEADERS):
            return result
    return None


def _same_column(cell, header):
    return abs(cell.x0 - header.x0) <= 1 and abs(cell.x1 - header.x1) <= 1


def _row_cells(cells, columns, code_cell):
    result = {}
    middle = (code_cell.y0 + code_cell.y1) / 2
    for field, header in columns.items():
        candidates = [cell for cell in cells if _same_column(cell, header) and cell.y0 <= middle <= cell.y1]
        if len(candidates) != 1:
            return None
        cell = candidates[0]
        # Only identity columns may span several security rows.
        if field not in {"rank", "nameEn", "nameCn"} and (abs(cell.y0 - code_cell.y0) > 1 or abs(cell.y1 - code_cell.y1) > 1):
            return None
        result[field] = cell
    return result


def _security(code, exchange, country):
    code, exchange, country = map(compact, (code, exchange, country))
    if country == "美国" and exchange in {"纳斯达克证券交易所", "美国纽约证券交易所", "纽约证券交易所", "美国证券交易所"}:
        if re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:[./-][A-Z0-9]{1,3})?", code):
            return {"stockCode": code.replace("/", ".").replace("-", "."),
                    "sourceStockCode": code, "market": "US", "currency": "USD"}
    if country in {"中国香港", "香港"} and exchange == "香港联合交易所":
        if re.fullmatch(r"\d{4,5}", code):
            return {"stockCode": code.zfill(5), "market": "HK", "currency": "HKD"}
    if country in {"中国", "中国内地", "中国大陆"} and exchange in {"上海证券交易所", "深圳证券交易所", "北京证券交易所"}:
        if re.fullmatch(r"\d{6}", code):
            return {"stockCode": code, "market": "CN", "currency": "CNY"}
    return None


def parse_foreign_equity_grids(pages):
    """Parse ordered physical pages; merged ranks are proven by shared cells."""
    empty = {"holdings": [], "metadata": {"complete": False, "excludedRows": 0}}
    pages = sorted(pages, key=lambda page: page.number)
    start_index = next((i for i, page in enumerate(pages) if _table_header(page)), None)
    if start_index is None:
        return empty
    start_page = pages[start_index]
    columns = _table_header(start_page)
    header_top, header_bottom = columns["code"].y1, columns["code"].y0
    headings = [(y, _START.match(compact(line))) for y, line in start_page.lines if y > header_top]
    headings = [(y, match) for y, match in headings if match]
    if not headings:
        return empty
    _y, heading = min(headings, key=lambda item: item[0])
    section_number, scope = heading.groups()
    units = any("金额单位：人民币元" in compact(line).replace(":", "：")
                for y, line in start_page.lines if y > header_top and y < _y)
    records, excluded, continuation_count = [], 0, 0
    end_number, end_page = None, None
    seen_pages = []
    for index, page in enumerate(pages[start_index:]):
        seen_pages.append(page.number)
        ends = [(y, match.group(1)) for y, line in page.lines
                if (index > 0 or y < header_bottom)
                and (match := _NEXT.match(compact(line)))
                and _section(match.group(1)) > _section(section_number)]
        end_y, current_end = max(ends, default=(-math.inf, None), key=lambda item: item[0])
        top_y = header_bottom if index == 0 else math.inf
        cells = [cell for cell in page.cells if cell.y1 <= top_y + 1 and cell.y0 >= end_y]
        # Anchor rows to the union of security and numeric cells.  A missing
        # ticker cell must not silently hide a second security in a merged rank.
        code_cells = [cell for cell in cells if _same_column(cell, columns["code"])]
        numeric_cells = [cell for cell in cells if any(_same_column(cell, columns[field]) for field in ("shares", "value", "weight"))]
        for numeric in numeric_cells:
            if not any(abs(numeric.y0 - cell.y0) <= 1 and abs(numeric.y1 - cell.y1) <= 1 for cell in code_cells):
                code_cells.append(Cell(columns["code"].x0, columns["code"].x1, numeric.y0, numeric.y1))
        code_cells.sort(key=lambda cell: -cell.y1)
        if code_cells:
            bottom, top = min(cell.y0 for cell in code_cells), max(cell.y1 for cell in code_cells)
            unmatched = []
            for x, y, value in page.spans:
                if not bottom <= y <= top:
                    continue
                for field in ("code", "shares", "value", "weight"):
                    column = columns[field]
                    if column.x0 - .1 <= x <= column.x1 + .1 and not any(
                            _same_column(cell, column) and cell.y0 - .1 <= y <= cell.y1 + .1 for cell in cells):
                        unmatched.append(value)
            # Text outside identifiable cells cannot disappear from the proof.
            excluded += bool(unmatched)
        for row_index, code_cell in enumerate(code_cells):
            values = _row_cells(cells, columns, code_cell)
            if values is None:
                excluded += 1
                continue
            row = {field: cell.text for field, cell in values.items()}
            if compact(row["code"]) in _HEADERS["code"]:
                continue  # A repeated, complete header on a later page.
            if not compact(row["code"]):
                # A PDF page break can cut a cell, e.g. the final character of
                # an exchange name.  Attach only the leading continuation row
                # with empty rank, code and all numeric columns.
                content = any(compact(value) for value in row.values())
                can_continue = (content and row_index == 0 and records
                                and records[-1]["endPage"] == page.number - 1
                                and not any(compact(row[field]) for field in ("rank", "shares", "value", "weight")))
                if can_continue:
                    for field in ("nameEn", "nameCn", "exchange", "country"):
                        records[-1][field] += " " + row[field]
                    records[-1]["endPage"] = page.number
                    continuation_count += 1
                elif content:
                    excluded += 1
                continue
            row.update({"sourcePage": page.number, "endPage": page.number,
                        "rankCell": (page.number, values["rank"].x0, values["rank"].x1, values["rank"].y0, values["rank"].y1)})
            records.append(row)
        if current_end:
            end_number, end_page = current_end, page.number
            break
    structural_excluded = excluded
    holdings, rank_groups, previous_rank_cell, keys = [], [], None, set()
    merged_rows = 0
    for record in records:
        rank = int(compact(record["rank"])) if re.fullmatch(r"\d+", compact(record["rank"])) else None
        if record["rankCell"] != previous_rank_cell:
            rank_groups.append(rank)
        else:
            merged_rows += 1
        previous_rank_cell = record["rankCell"]
        security = _security(record["code"], record["exchange"], record["country"])
        shares, value, weight = (_number(record[field]) for field in ("shares", "value", "weight"))
        name = compact(record["nameCn"])
        if name in {"", "-", "—"}:
            name = re.sub(r"\s+", " ", record["nameEn"]).strip()
        key = (security["market"], security["stockCode"]) if security else None
        if (not rank or not security or key in keys or not name or shares is None or shares <= 0
                or value is None or value <= 0 or weight is None or not 0 <= weight <= 100):
            excluded += 1
            continue
        keys.add(key)
        holdings.append({**security, "stockName": name, "shares": shares, "marketValueYuan": value,
                         "weightPct": weight, "marketValueCurrency": "CNY", "sourceRank": rank,
                         "sourceRankCell": list(record["rankCell"]),
                         "sourcePage": record["sourcePage"], "sourceEndPage": record["endPage"],
                         "sourceExchange": compact(record["exchange"]), "sourceCountry": compact(record["country"])})
    contiguous = bool(rank_groups) and rank_groups == list(range(1, len(rank_groups) + 1))
    complete = bool(end_number and units and contiguous and holdings and not excluded
                    and len(holdings) == len(records) and sum(row["weightPct"] for row in holdings) <= 100.1
                    and seen_pages == list(range(start_page.number, end_page + 1))
                    and (scope == "所有" or len(rank_groups) <= 10))
    return {"holdings": holdings, "metadata": {
        "coverage": "full" if scope == "所有" else "top10", "sectionNumber": section_number,
        "nextSectionNumber": end_number, "sectionBounded": end_number is not None,
        "startPage": start_page.number, "endPage": end_page, "rowsSeen": len(records) + structural_excluded,
        "parsedRows": len(holdings), "excludedRows": excluded, "contiguousRanks": contiguous,
        "unitsVerified": units, "complete": complete, "rankMode": "pdf-cells",
        "rankGroups": len(rank_groups), "mergedSecurityRows": merged_rows,
        "continuedRows": continuation_count, "marketValueCurrency": "CNY",
    }}


def parse_foreign_equity_pdf(pdf_bytes, max_pages=300):
    """Bounded geometry pass; callers still validate report identity/hash/pages."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    limit = max(0, min(300, max_pages))
    text_pages = [(page.extract_text() or "") for page in reader.pages[:limit]]
    grids, section_number = [], None
    for index, text_page in enumerate(text_pages):
        page = reader.pages[index]
        text = compact(text_page)
        if not grids and not ("所在证券市场" in text and "证券代码" in text and "所属国家" in text):
            continue
        grid = extract_grid_page(page, index + 1)
        if not grids:
            header = _table_header(grid)
            starts = [(y, match.group(1)) for y, line in grid.lines
                      if header and y > header["code"].y1 and (match := _START.match(compact(line)))]
            if not starts:
                continue
            section_number = min(starts, key=lambda item: item[0])[1]
        grids.append(grid)
        if any(_section(match.group(1)) > _section(section_number)
               for y, line in grid.lines if (len(grids) > 1 or y < header["code"].y0)
               and (match := _NEXT.match(compact(line)))):
            break
    result = parse_foreign_equity_grids(grids)
    normalized = "\n\n".join(page.strip() for page in text_pages if page.strip())
    result["extraction"] = {
        "parseMethod": "pypdf", "sourceTextHash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "pageStats": {"totalPages": len(reader.pages), "processedPages": len(text_pages), "pageCount": len(text_pages),
                      "nonEmptyPages": sum(bool(page.strip()) for page in text_pages), "charCount": len(normalized),
                      "truncated": len(text_pages) < len(reader.pages), "pageNumbersVerified": True},
    }
    return result
