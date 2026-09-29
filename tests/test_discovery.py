"""Directory completeness/identity fixtures use synthetic public-style records."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from providers import DataError
from discover import parse_companies, parse_funds, parse_company_funds, build_catalog, select_pending, catalog_attempt, restore_company_records


COMPANY = {"id": "80000001", "code": "80000001", "name": "示例机构", "sourceUrl": "https://example.org/company.html"}


def html_table(rows, total=None):
    attr = f' data-total="{total}"' if total is not None else ""
    return '<table' + attr + '><thead><tr><th>基金名称 代码</th><th>类型</th><th>基金经理</th></tr></thead><tbody>' + "".join(
        f'<tr><td><a href="https://fund.eastmoney.com/{code}.html">{name}</a><span>{code}</span></td><td>{kind}</td><td><a>经理甲</a></td></tr>' for code, name, kind in rows) + '</tbody></table>'


class DiscoveryTests(unittest.TestCase):
    def test_whole_static_arrays_and_no_name_based_canonicalization(self):
        companies = parse_companies('var gs={op:[["80000001","示例机构"]]};')
        self.assertEqual(len(companies), 1)
        funds = parse_funds('var r=' + json.dumps([["000001", "A", "示例基金A", "债券型", "A"], ["000002", "C", "示例基金C", "债券型", "C"]]) + ';')
        self.assertEqual(len(funds), 2)
        self.assertNotIn("canonicalCode", funds["000002"])
        with self.assertRaises(DataError):
            parse_companies('var gs={op:[["80000001","示例机构"]]')
        with self.assertRaises(DataError):
            parse_funds('var r=[["000001","A","示例","货币型"]')

    def test_all_product_tables_and_cross_table_dedup(self):
        body = '<html><script>var gsId="80000001";</script>'
        body += html_table([("000001", "开放式", "混合型"), ("000002", "货币基金", "货币型")], 2)
        body += html_table([("000001", "上市基金同代码", "混合型"), ("000003", "债券基金", "债券型")], 2)
        body += '</html>'
        rows, validation = parse_company_funds(body, COMPANY)
        self.assertEqual(len(rows), 3)
        self.assertEqual(validation["rawRows"], 4)
        self.assertEqual({r["type"] for r in rows}, {"混合型", "债券型", "货币型"})
        self.assertTrue(all(r["companyCode"] == COMPANY["code"] for r in rows))

    def test_total_mismatch_and_pagination_fail_closed(self):
        header = '<html><script>var gsId="80000001";</script>'
        with self.assertRaises(DataError):
            parse_company_funds(header + html_table([("000001", "示例", "股票型")], 2) + '</html>', COMPANY)
        with self.assertRaises(DataError):
            parse_company_funds(header + html_table([("000001", "示例", "股票型")]) + '<a href="?page=2">下一页</a></html>', COMPANY)
        with self.assertRaises(DataError):
            parse_company_funds(header.replace("80000001", "80000002") + html_table([]) + '</html>', COMPANY)

    def test_partial_catalog_keeps_unmapped_and_pending_companies(self):
        other = {**COMPANY, "id": "80000002", "code": "80000002", "name": "第二机构"}
        master = {"000001": {"code": "000001", "name": "股票", "type": "股票型"}, "000002": {"code": "000002", "name": "货币", "type": "货币型"}}
        mappings = {COMPANY["code"]: {"funds": [{**master["000001"], "companyCode": COMPANY["code"], "companyId": COMPANY["code"], "companyName": COMPANY["name"], "company": COMPANY["name"]}], "validation": {}}}
        result = build_catalog([COMPANY, other], master, mappings, "2026-09-29", {c["code"] for c in [COMPANY, other]})
        self.assertEqual(result["fundCount"], 2)
        self.assertEqual(result["coverage"]["unmappedCount"], 1)
        self.assertEqual(result["coverage"]["status"], "partial-directory")
        self.assertEqual(result["companies"][1]["directoryStatus"], "pending")
        self.assertIsNone(result["companies"][1]["fundCount"])
        self.assertNotIn("funds", result["companies"][0])
        restored = restore_company_records(result, result["companies"][0])
        self.assertEqual([fund["code"] for fund in restored], ["000001"])

    def test_permanent_front_failures_do_not_starve_later_companies(self):
        companies = [{**COMPANY, "code": str(80000000 + i), "id": str(80000000 + i)} for i in range(215)]
        attempts = {}
        for pass_number in range(12):
            selected = select_pending(companies, {}, attempts, 30)
            for company in selected:
                previous = attempts.get(company["code"], {"count": 0})
                attempts[company["code"]] = {"count": previous["count"] + 1, "status": "failed",
                                               "lastAttemptAt": f"2020-01-{pass_number + 1:02d}T00:00:00+00:00"}
            if pass_number == 1:
                self.assertEqual(selected[0]["code"], companies[30]["code"])
        self.assertEqual(len(attempts), 215)
        self.assertTrue(all(value["count"] >= 1 for value in attempts.values()))

    def test_catalog_carries_real_attempt_count_and_timestamp_across_runs(self):
        other = {**COMPANY, "id": "80000002", "code": "80000002", "name": "第二机构"}
        attempts = {COMPANY["code"]: {"count": 2, "status": "failed", "lastAttemptAt": "2020-01-01T01:02:03+00:00",
                                      "lastFinishedAt": "2020-01-01T01:02:04+00:00", "lastErrorCode": "TIMEOUT"}}
        catalog = build_catalog([COMPANY, other], {"000001": {"code": "000001", "name": "示例", "type": "货币型"}}, {}, "2026-09-29", {COMPANY["code"], other["code"]}, attempts=attempts)
        persisted = json.loads(json.dumps(catalog))
        restored = {company["code"]: catalog_attempt(company) for company in persisted["companies"]}
        self.assertEqual(restored[COMPANY["code"]]["count"], 2)
        self.assertEqual(restored[COMPANY["code"]]["lastAttemptAt"], "2020-01-01T01:02:03+00:00")
        self.assertEqual(select_pending([COMPANY, other], {}, restored, 1)[0]["code"], other["code"])


if __name__ == "__main__":
    unittest.main()
