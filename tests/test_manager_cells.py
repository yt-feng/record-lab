"""Synthetic original-cell geometry; real PDFs run only in Actions verification."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from foreign_holdings import Cell, GridPage
import manager_cells as cells
from report_managers import normalize_manager_table_coverage, parse_report_managers

REPORT = {'sourceUrl': 'https://example.org/quarter.pdf', 'periodEnd': '2026-06-30', 'kind': 'quarterly', 'publishedAt': '2026-07-20'}
HEADER = '4.1 基金经理（或基金经理小组）简介\n姓名 职务 任本基金的基金经理期限 证券从业年限 说明 任职日期 离任日期\n'
END = '4.2 管理人对报告期内本基金运作遵规守信情况的说明'
COLUMNS = {'name': (10, 50), 'role': (50, 170), 'start': (170, 240), 'end': (240, 310), 'experience': (310, 360), 'description': (360, 580)}
LABELS = dict(zip(cells.FIELDS, ('姓名', '职务', '任职日期', '离任日期', '证券从业年限', '说明')))


def row(y0, y1, name='张明', role='本基金基金经理', start='2020年1月2日', end='-', description='曾任其他基金基金经理。', experience='8年'):
    values = locals()
    return [Cell(*COLUMNS[key], y0, y1, values[key]) for key in cells.FIELDS]


def page(number, body, *, header=False, end_y=None, shift=0):
    grid = GridPage(number, body.copy(), [])
    if header:
        grid.cells += [Cell(*COLUMNS[key], 700, 740, LABELS[key]) for key in cells.FIELDS]
        grid.lines.append((760, '4.1 基金经理（或基金经理小组）简介'))
    if end_y is not None: grid.lines.append((end_y, END))
    if shift: grid.cells = [Cell(c.x0 + shift, c.x1 + shift, c.y0, c.y1, c.text) for c in grid.cells]
    grid.page_box = [0, 0, 595, 842]
    return grid


def parse(grids, pages):
    return cells.parse_manager_grids(grids, pages, REPORT, pdf_sha256='a' * 64)


class ManagerCellTests(unittest.TestCase):
    def test_description_biographies_never_create_manager_candidates_or_ignored_roles(self):
        text = HEADER + '张明 本基金基金经理 2020年1月2日 - 8年\n其他基金经理 以前任职。\n' + END
        result = parse([page(1, row(300, 700), header=True, end_y=250)], [text])
        self.assertTrue(result.coverage['complete'])
        self.assertEqual([m['name'] for m in result], ['张明'])
        self.assertEqual(result.coverage['candidateRows'], 1)
        self.assertEqual(result.coverage['ignoredKnownRoleRows'], 0)
        proof = normalize_manager_table_coverage(result.coverage, result, '2026-06-30')
        self.assertTrue(proof['complete'])
        self.assertEqual(proof['extractionMethod'], cells.METHOD)
        self.assertNotIn('曾任其他基金', json.dumps(proof, ensure_ascii=False))

    def test_department_prefixed_manager_role_is_evidenced_by_its_role_cell(self):
        text = HEADER + '林茂政 指数及量化业务管理部-基金经理 2020年1月2日 - 8年\n' + END
        result = parse([page(1, row(300, 700, name='林茂政', role='指数及量化业务管理部-基金经理'), header=True, end_y=250)], [text])
        self.assertTrue(result.coverage['complete'])
        self.assertEqual(result[0]['role'], '指数及量化业务管理部-基金经理')

    def test_role_continues_only_through_matching_cells_at_adjacent_page_edges(self):
        prefix = '总经理助理兼权益投资部总经理、本基'
        suffix = '金基金经理、投资经理'
        pages = [HEADER + f'徐超 {prefix} 2020年1月2日 - 8年\n', f'{suffix}\n' + END]
        grids = [page(1, row(60, 700, name='徐超', role=prefix), header=True),
                 page(2, row(600, 780, name='', role=suffix, start='', end='', experience=''), end_y=550)]
        result = parse(grids, pages)
        self.assertTrue(result.coverage['complete'])
        self.assertEqual(result[0]['role'], prefix + suffix)
        self.assertEqual(result[0]['evidence']['pages'], [1, 2])
        self.assertEqual(result.coverage['geometryEvidence']['continuationCount'], 1)
        self.assertTrue(normalize_manager_table_coverage(result.coverage, result, '2026-06-30')['complete'])
        # A blank row away from the next page edge cannot be silently appended.
        grids[1] = page(2, row(400, 650, name='', role=suffix, start='', end='', experience=''), end_y=350)
        result = parse(grids, pages)
        self.assertFalse(result.coverage['complete'])
        self.assertGreater(result.coverage['excludedRows'], 0)

    def test_true_assistant_and_departed_before_quarter_have_separate_accounting(self):
        body = row(550, 700) + row(400, 550, name='李明', role='本基金基金经理助理') + row(250, 400, name='王明', end='2026年3月31日')
        text = HEADER + '张明 本基金基金经理 2020年1月2日 -\n李明 本基金基金经理助理 2020年1月2日 -\n王明 本基金基金经理 2020年1月2日 2026年3月31日\n' + END
        result = parse([page(1, body, header=True, end_y=200)], [text])
        self.assertTrue(result.coverage['complete'])
        self.assertEqual(result.coverage['candidateRows'], 3)
        self.assertEqual(result.coverage['parsedRows'], 2)
        self.assertEqual(result.coverage['outsidePeriodRows'], 1)
        self.assertEqual(result.coverage['ignoredKnownRoleRows'], 1)
        self.assertEqual(len(result), 1)

    def test_missing_identity_cell_invalid_date_and_unknown_name_keep_known_manager_partial(self):
        for change in ('missing', 'bad-date', 'unknown-name'):
            second = row(300, 500, name='李明', start='2026年2月31日' if change == 'bad-date' else '2020年1月2日')
            if change == 'missing': second = [cell for cell in second if cell.x0 != COLUMNS['role'][0]]
            if change == 'unknown-name': second[0] = Cell(*COLUMNS['name'], 300, 500, '未识别的复杂姓名字段')
            result = parse([page(1, row(500, 700) + second, header=True, end_y=250)], [HEADER + '张明 本基金基金经理 2020年1月2日 -\n' + END])
            self.assertFalse(result.coverage['complete'])
            self.assertEqual([m['name'] for m in result], ['张明'])
            self.assertEqual(result.coverage['excludedRows'], 1)

    def test_column_drift_or_missing_page_never_hides_second_manager(self):
        pages = [HEADER + '张明 本基金基金经理 2020年1月2日 -\n', '李明 本基金基金经理 2020年1月2日 -\n' + END]
        first = page(1, row(60, 700), header=True)
        for grids in ([first], [first, page(2, row(600, 780, name='李明'), end_y=550, shift=4)]):
            result = parse(grids, pages)
            self.assertFalse(result.coverage['complete'])

    def test_bad_geometry_and_source_bindings_cannot_be_normalized_complete(self):
        result = parse([page(1, row(300, 700), header=True, end_y=250)], [HEADER + '张明 本基金基金经理 2020年1月2日 -\n' + END])
        for mutation in ('hash', 'count', 'source', 'row-key', 'blank-end'):
            proof = copy.deepcopy(result.coverage)
            geometry = proof['geometryEvidence']; fragment = geometry['tables'][0]['rows'][0]['fragments'][0]
            if mutation == 'hash': fragment['cellTextHashes']['role'] = 'b' * 64
            if mutation == 'count': geometry['physicalRowCount'] += 1
            if mutation == 'source': geometry['sourceUrl'] = 'https://example.org/other.pdf'
            if mutation == 'row-key': geometry['tables'][0]['rows'][0]['rowKey'] = '["另一经理",null,null,"基金经理"]'
            if mutation == 'blank-end':
                fragment['cellText']['end'] = ''; fragment['cellTextHashes']['end'] = cells.EMPTY_HASH
            self.assertFalse(normalize_manager_table_coverage(proof, result, '2026-06-30')['complete'])

    def test_notes_and_month_fragments_are_not_table_rows_or_section_endings(self):
        text = HEADER + '张明 本基金基金经理 2020年1月2日 -\n5 月至今任合成基金的基金经理。\n注：首任基金经理的任职日期为成立日。\n' + END
        baseline = parse_report_managers([text], REPORT)
        self.assertEqual(baseline.coverage['tables'][0]['endSection'], '4.2')
        self.assertFalse(any(item.get('name') == '首任基金经理' for item in baseline))
        legacy = copy.deepcopy(baseline.coverage); legacy.pop('parserVersion')
        self.assertFalse(normalize_manager_table_coverage(legacy, baseline, '2026-06-30')['complete'])

    def test_cell_fallback_retains_valid_extra_text_manager_and_marks_whole_roster_partial(self):
        text = HEADER + '张明 本基金基金经理 2020年1月2日 -\n李明 本基金基金经理 2020年1月2日 -\n' + END
        baseline = parse_report_managers([text], REPORT)
        geometry = parse([page(1, row(300, 700), header=True, end_y=250)], [text])
        selected = cells.select_manager_rows(baseline, geometry)
        self.assertEqual({row['name'] for row in selected}, {'张明', '李明'})
        self.assertFalse(selected.coverage['complete'])
        self.assertTrue(cells.select_manager_rows(baseline, None) is baseline)

    def test_duplicate_clipping_paths_with_empty_text_are_same_cell_but_conflicts_are_not(self):
        original = row(300, 700)
        duplicates = [Cell(c.x0 + .01, c.x1 + .01, c.y0 - .01, c.y1, '') for c in original]
        pages = [HEADER + '张明 本基金基金经理 2020年1月2日 -\n' + END]
        result = parse([page(1, original + duplicates, header=True, end_y=250)], pages)
        self.assertTrue(result.coverage['complete'])
        self.assertEqual(result.coverage['candidateRows'], 1)
        duplicates[0] = Cell(*COLUMNS['name'], 300, 700, '李明')
        result = parse([page(1, original + duplicates, header=True, end_y=250)], pages)
        self.assertFalse(result.coverage['complete'])
        self.assertEqual(result.coverage['excludedRows'], 1)

    def test_description_only_page_continuation_requires_empty_identity_region_evidence(self):
        pages = [HEADER + '张明 本基金基金经理 2020年1月2日 -\n', '续写履历\n李明 本基金基金经理 2020年1月2日 -\n' + END]
        second = page(2, [Cell(*COLUMNS['description'], 600, 780, '这是已知经理的履历续写。')] + row(300, 600, name='李明'), end_y=250)
        result = parse([page(1, row(60, 700), header=True), second], pages)
        self.assertTrue(result.coverage['complete'])
        self.assertEqual(result.coverage['candidateRows'], 2)
        fragment = result.coverage['geometryEvidence']['tables'][0]['rows'][0]['fragments'][1]
        self.assertEqual(fragment['continuationType'], 'description-only')
        self.assertEqual(fragment['emptyIdentityRegion']['spanCount'], 0)
        self.assertTrue(normalize_manager_table_coverage(result.coverage, result, '2026-06-30')['complete'])
        second.spans = [(20, 700, '未知姓名')]
        result = parse([page(1, row(60, 700), header=True), second], pages)
        self.assertFalse(result.coverage['complete'])
        self.assertEqual(result.coverage['excludedRows'], 1)

    def test_ambiguous_manager_and_assistant_role_is_unresolved_not_ignored(self):
        body = row(500, 700) + row(300, 500, name='李明', role='本基金基金经理兼基金经理助理')
        result = parse([page(1, body, header=True, end_y=250)], [HEADER + '张明 本基金基金经理 2020年1月2日 -\n' + END])
        self.assertFalse(result.coverage['complete'])
        self.assertEqual(result.coverage['ignoredKnownRoleRows'], 0)
        self.assertEqual(result.coverage['excludedRows'], 1)

    def test_geometry_cannot_complete_without_the_original_publication_date(self):
        grids = [page(1, row(300, 700), header=True, end_y=250)]
        pages = [HEADER + '张明 本基金基金经理 2020年1月2日 -\n' + END]
        for published in (None, '2026-06-29', 'invalid'):
            result = cells.parse_manager_grids(grids, pages, REPORT | {'publishedAt': published}, pdf_sha256='a' * 64)
            self.assertFalse(result.coverage['complete'])
            self.assertEqual(len(result), 1)


if __name__ == '__main__': unittest.main()
