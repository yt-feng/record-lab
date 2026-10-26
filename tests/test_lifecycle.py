"""Small synthetic regressions; no bulk requests or PDF mirrors in tests."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lifecycle import (lookup_lifecycle, parse_lifecycle_announcements,
                       parse_lifecycle_pages, should_check_lifecycle, normalize_lifecycle_evidence)


TITLE = "示例证券投资基金终止基金合同及基金财产清算的公告"
NOTICE = {"title": TITLE, "publishedAt": "2026-05-08", "sourceUrl": "https://example.org/fund-exit.pdf"}
PAGES = [TITLE + "\nA类基金份额代码：123450，C类基金份额代码：123451。\n"
         "2026年5月7日日终本基金资产净值触发约定情形，本基金基金合同自动终止。\n"
         "本基金的最后运作日为2026年5月7日。自2026年5月8日起，本基金进入基金财产清算程序。"]


def parse(pages=None, notice=None, code="123451", period="2026-06-30", as_of="2026-10-26"):
    return parse_lifecycle_pages(pages or PAGES, notice or NOTICE, code, period, as_of)


def row(article="AN20260508000001", title=TITLE, published="2026-05-08", code="123451"):
    return {"ID": article, "TITLE": title, "PUBLISHDATEDESC": published, "FCODE": code}


class LifecycleTests(unittest.TestCase):
    def test_persisted_shared_proof_is_revalidated_without_trusting_flags(self):
        evidence = json.loads((Path(__file__).parent / 'fixtures/lifecycle.json').read_text())
        self.assertEqual(normalize_lifecycle_evidence(evidence, '000001', '2026-06-30', '2026-09-29'), evidence)
        self.assertIsNone(normalize_lifecycle_evidence(evidence, '000001', '2026-09-30', '2026-09-29'))
        evidence['quote'] = '若未达到标准，本基金基金合同自动终止。'
        evidence['evidence'][0]['quote'] = evidence['quote']
        self.assertIsNone(normalize_lifecycle_evidence(evidence, '000001', '2026-06-30', '2026-09-29'))

    def test_collector_uses_lifecycle_only_after_verified_target_absence(self):
        from collect import collect_fund
        from providers import DataError
        proof = json.loads((Path(__file__).parent / 'fixtures/lifecycle.json').read_text())
        class Provider:
            def __init__(self, outcome=proof, timeout=False): self.outcome, self.timeout, self.calls = outcome, timeout, 0
            def overview(self, code): return {'name': '合成基金', 'company': '合成公司', 'managers': [], 'foundationDate': '2023-01-01'}
            def announcements(self, *args):
                if self.timeout: raise DataError('TIMEOUT')
                return []
            def lifecycle_evidence(self, *args, **kwargs):
                self.calls += 1
                return {'notApplicableEvidence': self.outcome, 'diagnostics': []}
        source = Provider()
        fund, errors = collect_fund({'code': '000001'}, '2026-10-26', provider=source, target_period='2026-06-30')
        self.assertEqual(fund['notApplicableEvidence'], proof)
        self.assertEqual(fund['inceptionDate'], '2023-01-01')
        self.assertEqual(errors, [])
        self.assertEqual(source.calls, 1)
        for source, period in [(Provider(None), '2026-06-30'), (Provider(timeout=True), '2026-06-30'), (Provider(), '2026-09-30')]:
            fund, errors = collect_fund({'code': '000001'}, '2026-10-26', provider=source, target_period=period)
            self.assertNotIn('notApplicableEvidence', fund)
            self.assertIn(errors[-1]['errorCode'], {'NO_REPORTS', 'TIMEOUT'})
            self.assertEqual(source.calls, 1 if source.outcome is None else 0)

    def test_final_liquidation_end_is_only_the_explicit_report_period_end(self):
        title = '示例证券投资基金清算报告'
        pages = [PAGES[0].replace(TITLE, title), '自2026年5月8日起至2026年6月15日止为本基金清算报告期。']
        evidence = parse(pages, {**NOTICE, 'title': title, 'publishedAt': '2026-06-22'})
        self.assertEqual(evidence['finalLiquidationEnd'], '2026-06-15')
        self.assertEqual(evidence['finalLiquidationEndBasis'], 'liquidation-report-period-end')
        self.assertEqual(normalize_lifecycle_evidence(evidence, '123451', '2026-06-30', '2026-10-26'), evidence)

    def test_age_gate_is_strictly_older_than_45_days_and_requires_missing_quarter(self):
        self.assertFalse(should_check_lifecycle("2026-09-30", "2026-11-14"))
        self.assertTrue(should_check_lifecycle("2026-09-30", "2026-11-15"))
        self.assertFalse(should_check_lifecycle("2026-09-30", "2026-10-26"))
        self.assertFalse(should_check_lifecycle("2026-06-30", "2026-10-26", False))
        self.assertFalse(should_check_lifecycle("2026-06-29", "2026-10-26"))
        self.assertFalse(should_check_lifecycle("2026-06-31", "2026-10-26"))

    def test_actual_event_preserves_quarter_history_and_page_cited_identity(self):
        evidence = parse()
        self.assertEqual(evidence["reason"], "TERMINATED_BEFORE_PERIOD")
        self.assertEqual(evidence["date"], "2026-05-07")
        self.assertEqual(evidence["dateBasis"], "lastOperationDate")
        self.assertEqual(evidence["identityFundCodes"], ["123450", "123451"])
        self.assertEqual(evidence["lastOperationDate"], "2026-05-07")
        self.assertEqual(evidence["liquidationStartDate"], "2026-05-08")
        self.assertEqual(evidence["scope"], "quarter-end-applicability-only")
        self.assertNotIn("noEquities", evidence)
        self.assertEqual(evidence["publishedAt"], NOTICE["publishedAt"])
        self.assertEqual(evidence["sourceUrl"], NOTICE["sourceUrl"])
        for item in evidence["evidence"]:
            self.assertIn(item["quote"], PAGES[item["page"] - 1])
        self.assertEqual({item["field"] for item in evidence["evidence"]},
                         {"terminationConfirmed", "lastOperationDate", "liquidationStartDate", "identityFundCodes"})

    def test_pdf_wrapping_and_liquidation_report_table_codes(self):
        title = "示例证券投资基金清算报告"
        pages = [title, "目录", "基金主代\n码123450\n各份额类别简称及基金交易代码 123450 123451\n"
                 "本基金基金合同自动终止。本基金的最后运作日\n为2026年5月7日。"]
        evidence = parse(pages, {**NOTICE, "title": title, "publishedAt": "2026-06-20"})
        self.assertEqual(evidence["eventType"], "final-liquidation")
        self.assertEqual(evidence["page"], 3)
        self.assertEqual(evidence["identityFundCodes"], ["123450", "123451"])

    def test_explicit_termination_date_can_stand_without_last_operation_date(self):
        pages = [TITLE + "\n基金代码123451。本基金基金合同已于2026年5月7日终止。"]
        evidence = parse(pages)
        self.assertEqual(evidence["terminationDate"], "2026-05-07")
        self.assertEqual(evidence["dateBasis"], "terminationDate")
        self.assertIsNone(evidence["lastOperationDate"])

    def test_rejects_conditional_titles_and_statements(self):
        for prefix in ("可能触发", "拟终止"):
            self.assertIsNone(parse(notice={**NOTICE, "title": prefix + TITLE}))
        for statement in ("若资产净值低于约定值，本基金基金合同自动终止。",
                          "本基金基金合同可能终止。", "本基金基金合同将于2026年5月7日终止。",
                          "本基金基金合同终止的情形尚未发生。", "本基金基金合同终止条件尚未满足。",
                          "本基金基金合同自动终止，具体情形尚待确认。"):
            with self.subTest(statement=statement):
                pages = [TITLE + "\n基金代码123451。" + statement + "本基金最后运作日为2026年5月7日。"]
                self.assertIsNone(parse(pages))

    def test_generic_contract_rule_never_confirms_an_actual_exit(self):
        pages = [TITLE + "\n基金代码123451。若基金资产不足，基金合同自动终止。"
                 "本基金最后运作日为2026年5月7日。"]
        self.assertIsNone(parse(pages))

    def test_distributor_termination_and_wrong_pdf_heading_are_rejected(self):
        title = "关于终止某基金销售有限公司销售业务的公告"
        self.assertIsNone(parse([title, *PAGES], {**NOTICE, "title": title}))
        self.assertIsNone(parse(["示例证券投资基金年度报告", *PAGES]))

    def test_identity_is_labeled_not_an_unrelated_six_digit_security(self):
        self.assertIsNone(parse(code="654321"))
        pages = [PAGES[0].replace("C类基金份额代码：123451", "股票证券代码：123451")]
        self.assertIsNone(parse(pages))
        self.assertIsNone(parse(notice={**NOTICE, "sourceFundCode": "654321"}))

    def test_future_dates_target_boundary_and_unknown_actual_date_are_rejected(self):
        self.assertIsNone(parse(period="2026-05-07"))
        self.assertIsNone(parse(period="2026-03-31"))
        self.assertIsNone(parse(as_of="2026-05-07"))
        self.assertIsNone(parse(notice={**NOTICE, "publishedAt": "2026-05-06"}))
        pages = [TITLE + "\n基金代码123451。本基金基金合同自动终止。报告截止日2026年5月7日。"]
        self.assertIsNone(parse(pages))

    def test_conflicting_event_dates_remain_unresolved(self):
        self.assertIsNone(parse(PAGES + ["本基金最后运作日为2026年5月6日。"] ))
        self.assertIsNone(parse(PAGES + ["本基金基金合同于2026年7月1日终止。"],
                                {**NOTICE, "publishedAt": "2026-07-02"}))

    def test_discovery_filters_source_identity_future_and_conditional_notices(self):
        payload = {"Data": [row(), row(title="关于可能触发终止基金合同的提示公告"),
                            row(title="关于终止某基金销售有限公司销售业务的公告"),
                            row(article="AN20260508000002", code="654321"),
                            row(article="AN20260508000003", published="2026-11-01"),
                            row(article="bad-id")]}
        found = parse_lifecycle_announcements(payload, "123451", "2026-10-26")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["sourceUrl"], "https://pdf.dfcfw.com/pdf/H2_AN20260508000001_1.pdf")

    def test_source_dates_accept_timestamp_and_chinese_publication_date(self):
        for published in ("2026-05-08 00:00:00", "2026年5月8日"):
            found = parse_lifecycle_announcements({"Data": [row(published=published)]}, "123451", "2026-10-26")
            self.assertEqual(found[0]["publishedAt"], "2026-05-08")

    def test_lookup_gate_never_requests_q3_while_recent_or_target_available(self):
        class Provider:
            def get(self, *args, **kwargs):
                raise AssertionError("No request expected")
        self.assertFalse(lookup_lifecycle(Provider(), "123451", "2026-09-30", "2026-10-26")["checked"])
        self.assertFalse(lookup_lifecycle(Provider(), "123451", "2026-06-30", "2026-10-26", missing_target=False)["checked"])

    def test_lookup_maximum_pages_and_stalled_pagination(self):
        class Provider:
            def __init__(self): self.calls = []
            def get(self, url, **kwargs):
                self.calls.append(kwargs)
                return json.dumps({"Data": [row(title="季度报告")] * 100})
        provider = Provider()
        result = lookup_lifecycle(provider, "123451", "2026-06-30", "2026-10-26", max_announcement_pages=100)
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(all(call["params"]["type"] == 0 and call["params"]["pageSize"] == 100 for call in provider.calls))
        self.assertEqual(result["diagnostics"], [{"errorCode": "LIFECYCLE_PAGINATION_STALLED"}])
        self.assertIsNone(result["notApplicableEvidence"])

    def test_lookup_preserves_absence_as_unresolved(self):
        class Provider:
            def get(self, *args, **kwargs): return '{"Data": []}'
        result = lookup_lifecycle(Provider(), "123451", "2026-06-30", "2026-10-26")
        self.assertTrue(result["checked"])
        self.assertIsNone(result["notApplicableEvidence"])
        self.assertEqual(result["documentsChecked"], 0)

    def test_lookup_deadline_and_two_document_limit(self):
        class Provider:
            def __init__(self): self.calls = []
            def get(self, url, **kwargs):
                self.calls.append(url)
                if kwargs.get("binary"): return b"not a PDF"
                return json.dumps({"Data": [row(article=f"AN2026050800000{i}") for i in range(4)]})
        provider = Provider()
        result = lookup_lifecycle(provider, "123451", "2026-06-30", "2026-10-26", max_documents=100)
        self.assertEqual(result["documentsChecked"], 2)
        self.assertEqual(len(provider.calls), 3)
        provider = Provider()
        result = lookup_lifecycle(provider, "123451", "2026-06-30", "2026-10-26", deadline=time.monotonic() - 1)
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["diagnostics"], [{"errorCode": "LIFECYCLE_DEADLINE_REACHED"}])

    def test_lookup_uses_physical_pdf_pages_and_preserves_evidence(self):
        class Provider:
            def get(self, url, **kwargs):
                return b"%PDF-stub" if kwargs.get("binary") else json.dumps({"Data": [row()]})
        class Page:
            def extract_text(self): return PAGES[0]
        class Reader:
            pages = [Page()]
        with patch("pypdf.PdfReader", return_value=Reader()):
            result = lookup_lifecycle(Provider(), "123451", "2026-06-30", "2026-10-26")
        self.assertEqual(result["notApplicableEvidence"]["date"], "2026-05-07")
        self.assertEqual(result["notApplicableEvidence"]["totalPages"], 1)


if __name__ == "__main__":
    unittest.main()
