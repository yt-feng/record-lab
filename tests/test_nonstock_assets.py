"""Synthetic exhaustive-table evidence; no network or real report seeds."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from nonstock_assets import normalize_nonstock_assets, parse_nonstock_assets, valid_nonstock_assets

FIXTURE = Path(__file__).parent / "fixtures/nonstock-assets.json"


class NonstockAssetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())

    def parse(self, fixture=None):
        data = fixture or self.fixture
        return parse_nonstock_assets(data["pages"], data["report"], page_numbers=data["pageNumbers"])

    def test_two_closed_tables_explain_every_cent_including_rounded_zero_other_weight(self):
        result = self.parse()
        self.assertIsNotNone(result)
        self.assertTrue(valid_nonstock_assets(result))
        self.assertEqual(result["pages"], [8, 12, 13])
        self.assertEqual(result["assetTable"]["rows"][-1]["amount"], "0.10")
        self.assertEqual(result["assetTable"]["rows"][-1]["weightPct"], "0.00")
        self.assertEqual(result["otherAssetsTable"]["total"]["amount"], "0.10")
        self.assertEqual(result["stockBookValue"], "0.00")
        self.assertEqual(result["scope"], "period-end-stock-book-value")
        self.assertLess(len(result["assetTable"]["text"]), 800)
        self.assertLess(len(result["otherAssetsTable"]["text"]), 600)

    def test_fund_name_or_type_has_no_role_in_the_zero_conclusion(self):
        first = self.parse()
        self.fixture["pages"] = [page.replace("合成基金", "合成股票投资基金") for page in self.fixture["pages"]]
        second = self.parse()
        self.assertIsNotNone(second)
        self.assertEqual(first["assetTable"], second["assetTable"])

    def test_missing_or_unclassified_rows_and_nonzero_other_block_proof(self):
        cases = [(0, "1 固定收益投资", "1 权益投资"), (0, "4 其他资产 0.10 0.00\n", ""),
                 (0, "固定收益投资", "其他投资"), (1, "7 其他 -", "7 其他 0.01"),
                 (1, "应收申购款", "其他证券投资"), (0, "资产支持证\n券 - -\n", "")]
        for index, before, after in cases:
            with self.subTest(before=before):
                fixture = copy.deepcopy(self.fixture)
                fixture["pages"][index] = fixture["pages"][index].replace(before, after)
                self.assertIsNone(self.parse(fixture))

    def test_one_cent_mismatch_and_material_percentage_error_are_rejected(self):
        for before, after in [("2,999.90", "2,999.91"), ("3,000.00 30.00", "3,000.00 29.99"),
                              ("100.00", "99.99"), ("0.10 0.00", "0.10 -")]:
            fixture = copy.deepcopy(self.fixture)
            fixture["pages"][0] = fixture["pages"][0].replace(before, after)
            self.assertIsNone(self.parse(fixture))
        fixture = copy.deepcopy(self.fixture)
        fixture["pages"][1] = fixture["pages"][1].replace("0.06", "0.05")
        self.assertIsNone(self.parse(fixture))

    def test_rounded_percentages_need_exact_rowwise_half_up_explanation(self):
        fixture = copy.deepcopy(self.fixture)
        fixture["pages"][0] = fixture["pages"][0].replace("4,000.00 40.00", "3,333.33 33.33").replace("3,000.00 30.00", "3,333.33 33.33").replace("2,999.90 30.00", "3,333.24 33.33")
        result = self.parse(fixture)
        self.assertIsNotNone(result)  # 99.99% is fully explained by displayed rounding.
        fixture["pages"][0] = fixture["pages"][0].replace("3,333.24 33.33", "3,333.24 33.34")
        self.assertIsNone(self.parse(fixture))

    def test_missing_table_end_unknown_page_gap_or_other_table_is_not_complete(self):
        for index, end in [(0, "5.2 报告期债券回购融资情况"), (2, "§6 开放式基金份额变动")]:
            fixture = copy.deepcopy(self.fixture)
            fixture["pages"][index] = fixture["pages"][index].replace(end, "")
            self.assertIsNone(self.parse(fixture))
        for numbers in [[8, None, 13], [8, 12, 14], [8, 12], [True, 12, 13]]:
            fixture = copy.deepcopy(self.fixture)
            fixture["pageNumbers"] = numbers
            self.assertIsNone(self.parse(fixture))

    def test_period_source_and_unit_must_be_explicit_and_consistent(self):
        for change in ["period", "source", "unit", "ratio", "title"]:
            fixture = copy.deepcopy(self.fixture)
            if change == "period": fixture["report"]["periodEnd"] = "2026-09-30"
            if change == "source": fixture["report"]["sourceUrl"] = "https://localhost/report.pdf"
            if change == "unit": fixture["pages"][0] = fixture["pages"][0].replace("金额（元）", "金额（万元）")
            if change == "ratio": fixture["pages"][0] = fixture["pages"][0].replace("基金总资产", "基金资产净值")
            if change == "title": fixture["pages"][2] = fixture["pages"][2].replace("第2季度", "第3季度")
            self.assertIsNone(self.parse(fixture))

    def test_structured_numbers_cannot_override_short_source_tables_and_extra_fields_are_removed(self):
        proof = self.parse()
        modified = copy.deepcopy(proof)
        modified["assetTable"]["rows"][0]["amount"] = "5000.00"
        self.assertFalse(valid_nonstock_assets(modified))
        modified = copy.deepcopy(proof)
        modified["otherAssetsTable"]["text"] = modified["otherAssetsTable"]["text"].replace("7 其他 -", "7 其他 1.00")
        self.assertFalse(valid_nonstock_assets(modified))
        modified = copy.deepcopy(proof)
        modified["diagnostic"] = "not part of public proof"
        self.assertNotIn("diagnostic", normalize_nonstock_assets(modified))
        for edit in (lambda item: item.update(schemaVersion=True),
                     lambda item: item["assetTable"]["rows"][0].update(rank=True)):
            modified = copy.deepcopy(proof)
            edit(modified)
            self.assertFalse(valid_nonstock_assets(modified))

    def test_duplicate_table_pairs_are_ambiguous_not_arbitrarily_selected(self):
        fixture = copy.deepcopy(self.fixture)
        fixture["pages"] += fixture["pages"]
        fixture["pageNumbers"] += [18, 22, 23]
        self.assertIsNone(self.parse(fixture))


if __name__ == "__main__":
    unittest.main()
