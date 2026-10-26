import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from foreign_holdings import Cell, GridPage, extract_grid_page, parse_foreign_equity_grids


FIELDS = ["rank", "nameEn", "nameCn", "code", "exchange", "country", "shares", "value", "weight"]
LABELS = ["序号", "公司名称（英文）", "公司名称（中文）", "证券代码", "所在证券市场",
          "所属国家（地区）", "数量（股）", "公允价值", "占基金资产净值比例（%）"]


def cells(bottom, top, values):
    return [Cell(index * 50, index * 50 + 49, bottom, top, values.get(field, ""))
            for index, field in enumerate(FIELDS)]


def row(rank, ticker, bottom, top, **changes):
    values = dict(rank=str(rank), nameEn="EXAMPLE CORP", nameCn="示例公司", code=ticker,
                  exchange="纳斯达克证券交易所", country="美国", shares="1,000.00", value="5,000.00", weight="1.20")
    values.update(changes)
    return cells(bottom, top, values)


def fixture():
    header = cells(700, 730, dict(zip(FIELDS, LABELS)))
    first = row(1, "GOOGL", 620, 690)
    second = row(1, "GOOG", 550, 619)
    merged = [Cell(cell.x0, cell.x1, 550, 690, cell.text) for cell in first[:3]]
    third = row(2, "NVDA", 480, 549)
    return [GridPage(38, header + merged + first[3:] + second[3:] + third,
                     [(760, "7.4.1期末按公允价值占基金资产净值比例大小排序的所有权益投资明细"),
                      (745, "金额单位：人民币元"), (450, "7.5报告期内权益投资组合的重大变动")])]


class ForeignHoldingsTests(unittest.TestCase):
    def test_shared_rank_cell_proves_two_distinct_securities(self):
        result = parse_foreign_equity_grids(fixture())
        self.assertTrue(result["metadata"]["complete"])
        self.assertEqual([row["stockCode"] for row in result["holdings"]], ["GOOGL", "GOOG", "NVDA"])
        self.assertEqual([row["sourceRank"] for row in result["holdings"]], [1, 1, 2])
        self.assertEqual(result["metadata"]["rankGroups"], 2)
        self.assertEqual(result["metadata"]["mergedSecurityRows"], 1)
        self.assertEqual(result["metadata"]["rowsSeen"], 3)
        self.assertTrue(all(row["currency"] == "USD" and row["marketValueCurrency"] == "CNY" for row in result["holdings"]))

    def test_explicit_country_exchange_and_share_class_normalization(self):
        for ticker, exchange, country, expected in [
            ("BRK/B", "美国纽约证券交易所", "美国", ("BRK.B", "US", "USD")),
            ("MOG/A", "纽约证券交易所", "美国", ("MOG.A", "US", "USD")),
            ("700", "香港联合交易所", "中国香港", None),
            ("0700", "香港联合交易所", "中国香港", ("00700", "HK", "HKD")),
            ("AAPL", "伦敦证券交易所", "英国", None),
            ("AAPL", "纳斯达克证券交易所", "中国香港", None),
        ]:
            with self.subTest(ticker=ticker, country=country):
                pages = fixture()
                pages[0].cells = pages[0].cells[:9] + row(1, ticker, 480, 690, exchange=exchange, country=country)
                result = parse_foreign_equity_grids(pages)
                self.assertEqual(result["metadata"]["complete"], expected is not None)
                if expected:
                    holding = result["holdings"][0]
                    self.assertEqual((holding["stockCode"], holding["market"], holding["currency"]), expected)
                else:
                    self.assertEqual(result["metadata"]["excludedRows"], 1)

    def test_page_break_continuation_requires_empty_numeric_and_rank_cells(self):
        pages = fixture()
        pages[0].lines = pages[0].lines[:2]
        pages[0].cells = pages[0].cells[:9] + row(1, "MU", 80, 140, exchange="纳斯达克证券")
        continued = cells(740, 780, {"exchange": "交易所"})
        pages.append(GridPage(39, continued + row(2, "AMZN", 660, 730), [(600, "7.5报告期内权益投资组合的重大变动")]))
        result = parse_foreign_equity_grids(pages)
        self.assertTrue(result["metadata"]["complete"])
        self.assertEqual(result["metadata"]["continuedRows"], 1)
        self.assertEqual(result["holdings"][0]["sourceExchange"], "纳斯达克证券交易所")
        self.assertEqual(result["holdings"][0]["sourceEndPage"], 39)
        broken = copy.deepcopy(pages)
        broken[1].cells = cells(740, 780, {"exchange": "交易所", "shares": "10"}) + broken[1].cells[9:]
        self.assertFalse(parse_foreign_equity_grids(broken)["metadata"]["complete"])
        missing_page = copy.deepcopy(pages)
        missing_page[1].number = 40
        self.assertFalse(parse_foreign_equity_grids(missing_page)["metadata"]["complete"])

    def test_missing_ticker_cell_inside_merged_rank_cannot_claim_complete(self):
        pages = fixture()
        pages[0].cells = [cell for cell in pages[0].cells if cell.text != "GOOG"]
        result = parse_foreign_equity_grids(pages)
        self.assertFalse(result["metadata"]["complete"])
        self.assertEqual(result["metadata"]["rowsSeen"], 3)
        self.assertEqual(result["metadata"]["excludedRows"], 1)

    def test_text_without_cell_geometry_cannot_disappear_inside_table(self):
        pages = fixture()
        # A text fragment in the gap between row cells has no matching cell;
        # treating recognized cells as the entire source would miss it.
        pages[0].spans.append((155, 619.5, "OTHER"))
        result = parse_foreign_equity_grids(pages)
        self.assertFalse(result["metadata"]["complete"])
        self.assertEqual(result["metadata"]["excludedRows"], 1)

    def test_unbounded_units_missing_duplicate_or_unmerged_rank_fail_closed(self):
        for scenario in ("end", "units", "duplicate", "unmerged", "numeric", "rank-gap"):
            with self.subTest(scenario=scenario):
                pages = fixture()
                if scenario == "end":
                    pages[0].lines.pop()
                elif scenario == "units":
                    pages[0].lines.pop(1)
                elif scenario == "unmerged":
                    pages[0].cells = pages[0].cells[:9] + row(1, "GOOGL", 620, 690) + row(1, "GOOG", 550, 619)
                else:
                    changes = {"duplicate": ("GOOG", "GOOGL"), "numeric": ("1,000.00", "1,broken"), "rank-gap": ("2", "3")}
                    before, after = changes[scenario]
                    pages[0].cells = [Cell(c.x0, c.x1, c.y0, c.y1, after if c.text == before else c.text) for c in pages[0].cells]
                self.assertFalse(parse_foreign_equity_grids(pages)["metadata"]["complete"])

    def test_original_pdf_clip_geometry_and_transformed_coordinates(self):
        from pypdf import PageObject
        from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject

        page = PageObject.create_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = DecodedStreamObject()
        content.set_data(b"q 1 0 0 -1 100 200 cm 0 0 70 30 re W n BT /F1 10 Tf 1 0 0 -1 2 12 Tm (GOOGL) Tj ET Q")
        page[NameObject("/Contents")] = content
        result = extract_grid_page(page, 38)
        self.assertEqual(result.cells, [Cell(100.0, 170.0, 170.0, 200.0, "GOOGL")])


if __name__ == "__main__":
    unittest.main()
