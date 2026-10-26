"""Whole-table coverage must survive partial rows, page loss and parser caps."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from report_managers import parse_report_managers, normalize_manager_table_coverage
import test_traversal as support

HEADER = '4.1 基金经理（或基金经理小组）简介\n姓名 职务 任本基金的基金经理期限 证券从业年限 说明 任职日期 离任日期\n'
REPORT = {'sourceUrl': 'https://example.org/manager-report.pdf', 'periodEnd': '2026-06-30', 'kind': 'quarterly', 'publishedAt': '2026-07-20'}
FIRST = '张明 本基金基金经理 2020年1月1日 - 20年\n'
SECOND = '李华 本基金基金经理 2026年4月1日 - 10年\n4.2 运作说明'


def quality(rows):
    fund = support.TraversalTests().retained_fund()
    fund.update(reportManagers=rows, managerTableCoverage=rows.coverage)
    return support.traversal.report_quality(fund, support.PHASE, support.traversal.utc_now(support.NOW))


class ManagerTableCoverageTests(unittest.TestCase):
    def test_six_partial_tables_never_turn_one_valid_row_into_complete(self):
        cases = {
            'invalid-date': ([HEADER + FIRST + '李华 本基金基金经理 2026年2月30日 - 10年\n4.2 运作说明'], None),
            'missing-date': ([HEADER + FIRST + '李华 本基金基金经理 日期缺失 - 10年\n4.2 运作说明'], None),
            'unknown-page': ([HEADER + FIRST, SECOND], [4, None]),
            'no-end-boundary': ([HEADER + FIRST], None),
            'internal-six-page-cap': ([HEADER + FIRST] + ['续表履历文字'] * 5 + [SECOND], None),
            'unsupported-name': ([HEADER + FIRST + 'Jane Doe 本基金基金经理 2026年4月1日 - 10年\n4.2 运作说明'], None),
        }
        for label, (pages, mapping) in cases.items():
            with self.subTest(label=label):
                rows = parse_report_managers(pages, REPORT, page_numbers=mapping)
                self.assertEqual([item['name'] for item in rows], ['张明'])
                self.assertFalse(rows.coverage['complete'])
                self.assertFalse(normalize_manager_table_coverage(rows.coverage, rows, REPORT['periodEnd'])['complete'])
                self.assertEqual(quality(rows)['fields']['managers'], 'partial')
                self.assertNotEqual(support.traversal.normalize_quality(quality(rows), 'complete', support.traversal.utc_now(support.NOW))['status'], 'complete')

    def test_complete_comanagers_account_for_every_candidate_across_pages(self):
        rows = parse_report_managers([HEADER + FIRST, SECOND], REPORT, page_numbers=[5, 6])
        self.assertTrue(rows.coverage['complete'])
        self.assertEqual((rows.coverage['candidateRows'], rows.coverage['parsedRows'], rows.coverage['excludedRows']), (2, 2, 0))
        self.assertEqual(quality(rows)['fields']['managers'], 'complete')
        self.assertTrue(normalize_manager_table_coverage(rows.coverage, rows, REPORT['periodEnd'])['complete'])

    def test_known_assistants_have_separate_cited_role_exclusions(self):
        rows = parse_report_managers([HEADER + FIRST + '王明 本基金基金经理助理 2025年1月1日 - 5年\n4.2 运作说明'], REPORT)
        self.assertEqual(rows.coverage['ignoredKnownRoleRows'], 1)
        self.assertEqual(rows.coverage['tables'][0]['ignoredRoles'][0]['name'], '王明')
        self.assertTrue(normalize_manager_table_coverage(rows.coverage, rows, REPORT['periodEnd'])['complete'])
        forged = copy.deepcopy(rows.coverage); forged['tables'][0]['ignoredRoles'] = []
        self.assertFalse(normalize_manager_table_coverage(forged, rows, REPORT['periodEnd'])['complete'])

    def test_biography_name_or_ambiguous_assistant_role_never_bypasses_whole_table_counting(self):
        for extra in ['纯债债券型证券投资基金 基金经理助理、研究员。\n',
                      '王明 本基金基金经理兼基金经理助理 2025年1月1日 - 5年\n']:
            rows = parse_report_managers([HEADER + FIRST + extra + '4.2 运作说明'], REPORT)
            self.assertFalse(rows.coverage['complete'])
            self.assertEqual(rows.coverage['ignoredKnownRoleRows'], 0)
            self.assertGreater(rows.coverage['excludedRows'], 0)

    def test_forged_counts_keys_hash_or_boundary_cannot_make_complete(self):
        rows = parse_report_managers([HEADER + FIRST + SECOND], REPORT)
        for alter in [lambda value: value.update(sourceTextHash='bad'), lambda value: value.update(outputManagerCount=1),
                      lambda value: value['tables'][0].update(pageNumbersVerified=False),
                      lambda value: value['tables'][0].update(endSection='4.1'),
                      lambda value: value['managerKeys'].pop()]:
            invalid = copy.deepcopy(rows.coverage); alter(invalid)
            result = normalize_manager_table_coverage(invalid, rows, REPORT['periodEnd'])
            self.assertFalse(result and result['complete'])

    def test_old_row_only_quality_is_partial_and_gets_new_bounded_table_budget(self):
        old = {'fields': {key: 'complete' for key in support.traversal.QUALITY_FIELDS}, 'attempts': 3,
               'managerAttempts': 3, 'managerTotalAttempts': 3, 'priceCoverageVersion': 2}
        migrated = support.traversal.normalize_quality(old, 'complete', support.traversal.utc_now(support.NOW))
        self.assertEqual(migrated['fields']['managers'], 'partial')
        self.assertEqual(migrated['managerAttempts'], 0)
        self.assertEqual(migrated['managerTotalAttempts'], 3)
        self.assertEqual(migrated['attempts'], 3)
        self.assertEqual(migrated['status'], 'pending')
        for legacy in [{'fields': {key: 'complete' for key in support.traversal.QUALITY_FIELDS}},
                       {'fields': {key: 'complete' for key in support.traversal.SIX_QUALITY_FIELDS}}]:
            merged = support.traversal.merge_quality_observation(migrated, legacy)
            self.assertNotEqual(merged['fields']['managers'], 'complete')


if __name__ == '__main__':
    unittest.main()
