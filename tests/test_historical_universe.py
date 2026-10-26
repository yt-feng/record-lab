"""Synthetic pinned-workbook fixtures, never a local official-index fetch."""
import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import historical_universe as history

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NOW = '2026-10-26T12:00:00+00:00'


def product(row, code, name='合成基金', serial=None, date='45118', cache=True):
    serial = row - 2 if serial is None else serial
    cached = f'<v>{escape(name)}</v>' if cache else ''
    return (f'<row r="{row}"><c r="A{row}"><v>{serial}</v></c><c r="B{row}" s="1"><v>{int(code)}</v></c>'
            f'<c r="C{row}" t="str"><f>CONCATENATE("合成","基金")</f>{cached}</c>'
            f'<c r="D{row}" s="2"><v>{date}</v></c></row>')


def files(rows=None):
    rows = rows if rows is not None else [product(3, '000001'), product(4, '018774', '汇添富合成A')]
    headers = '<row r="1"><c r="A1" t="inlineStr"><is><t>基金产品索引</t></is></c><c r="B1"/><c r="C1"/><c r="D1" s="2"/></row>'
    headers += '<row r="2">' + ''.join(f'<c r="{column}2" t="inlineStr"><is><t>{name}</t></is></c>' for column, name in
               zip('ABCD', ['序号', '基金代码', '基金简称', '设立日期'])) + '</row>'
    return {'xl/workbook.xml': f'<workbook xmlns="{NS}" xmlns:r="{REL}"><workbookPr date1904="0"/>'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="r1"/></sheets></workbook>',
            'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="r1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>'
                f'<Relationship Id="r2" Type="{REL}/styles" Target="styles.xml"/></Relationships>',
            'xl/styles.xml': f'<styleSheet xmlns="{NS}"><numFmts><numFmt numFmtId="164" formatCode="000000"/>'
                '<numFmt numFmtId="165" formatCode="yyyy/m/d;@"/></numFmts><cellXfs><xf numFmtId="0"/>'
                '<xf numFmtId="164"/><xf numFmtId="165"/></cellXfs></styleSheet>',
            'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{NS}"><sheetData>{headers}{"".join(rows)}</sheetData></worksheet>'}


def workbook(entries=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in (entries or files()).items(): archive.writestr(name, value)
    return output.getvalue()


@contextlib.contextmanager
def synthetic_pin(data, expected=2):
    with patch.object(history, 'SOURCE_HASH', hashlib.sha256(data).hexdigest()), patch.object(history, 'EXPECTED_PRODUCTS', expected):
        yield


def parse(data, expected=2):
    with synthetic_pin(data, expected): return history.parse_official_index(data, retrieved_at=NOW)


class HistoricalUniverseTests(unittest.TestCase):
    def test_exact_known_columns_preserve_codes_dates_and_source_rows_without_asserting_share_coverage(self):
        data = workbook(); result = parse(data)
        self.assertEqual(result['parseStatus'], 'complete')
        self.assertFalse(result['historicalUniverseVerified'])
        self.assertEqual(result['recordUnit'], 'fund-product-code')
        self.assertEqual([row['code'] for row in result['records']], ['000001', '018774'])
        record = result['records'][1]
        self.assertEqual(record['name'], '汇添富合成A')
        self.assertEqual(record['inceptionDate'], '2023-07-11')
        self.assertEqual(record['inceptionEvidence']['rawSerial'], '45118')
        self.assertEqual(record['evidence'], [{'sourceId': history.SOURCE_ID, 'sheet': 'Sheet1', 'row': 4}])
        self.assertTrue(record['nameFromCachedFormula'])
        self.assertEqual(result['evidence'][0]['contentHash'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['evidence'][0]['retrievedAt'], NOW)
        self.assertEqual(result['evidence'][0]['unresolvedRows'], 0)
        self.assertNotIn('CONCATENATE', json.dumps(result))
        self.assertNotIn('companyCode', record)

    def test_source_hash_changes_require_new_verification_before_any_schema_guess(self):
        with self.assertRaises(history.HistoricalUniverseError) as error:
            history.parse_official_index(workbook(), retrieved_at=NOW)
        self.assertEqual(error.exception.code, 'SOURCE_HASH_MISMATCH')

    def test_headers_and_excel_date_system_are_explicitly_pinned(self):
        for path, old, new, reason in [('xl/worksheets/sheet1.xml', '设立日期', '终止日期', 'HISTORICAL_SCHEMA_MISMATCH'),
                                     ('xl/workbook.xml', 'date1904="0"', 'date1904="1"', 'UNSUPPORTED_DATE_SYSTEM'),
                                     ('xl/workbook.xml', 'Sheet1', 'Other', 'HISTORICAL_SCHEMA_MISMATCH')]:
            entries = files(); entries[path] = entries[path].replace(old, new); data = workbook(entries)
            with synthetic_pin(data), self.assertRaises(history.HistoricalUniverseError) as error:
                history.parse_official_index(data, retrieved_at=NOW)
            self.assertEqual(error.exception.code, reason)

    def test_code_padding_requires_known_format_and_formula_code_is_not_assumed(self):
        for original, replacement in [('r="B4" s="1"', 'r="B4"'),
                                      ('r="B4" s="1"><v>', 'r="B4" s="1"><f>18774</f><v>')]:
            entries = files(); entries['xl/worksheets/sheet1.xml'] = entries['xl/worksheets/sheet1.xml'].replace(original, replacement)
            result = parse(workbook(entries))
            self.assertEqual(result['parseStatus'], 'partial')
            self.assertEqual(result['diagnostics']['rejectedRows'], 1)
            self.assertEqual(result['diagnostics']['rejectionReasons']['INVALID_FUND_CODE'], 1)

    def test_uncached_formula_and_invalid_name_are_counted_not_silently_dropped(self):
        result = parse(workbook(files([product(3, '000001'), product(4, '018774', cache=False)])))
        self.assertEqual(result['parseStatus'], 'partial')
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['diagnostics']['rejectionReasons']['MISSING_FORMULA_CACHE'], 1)
        self.assertEqual(result['evidence'][0]['unresolvedRows'], 1)

    def test_unresolved_optional_dates_do_not_drop_readable_product_codes(self):
        for date in ['60', '0', '-1', '45118.5']:
            result = parse(workbook(files([product(3, '000001'), product(4, '018774', date=date)])))
            self.assertEqual(result['parseStatus'], 'partial')
            self.assertEqual(result['diagnostics']['rejectedRows'], 0)
            self.assertEqual(result['diagnostics']['unresolvedInceptionRows'], 1)
            self.assertTrue(result['diagnostics']['productRowsComplete'])
            self.assertFalse(result['diagnostics']['inceptionMetadataComplete'])
            self.assertEqual([row['code'] for row in result['records']], ['000001', '018774'])
            self.assertNotIn('inceptionDate', result['records'][1])
        future = str((dt.date(2026, 7, 1) - dt.date(1899, 12, 30)).days)
        result = parse(workbook(files([product(3, '000001'), product(4, '018774', date=future)])))
        self.assertEqual(result['diagnostics']['rejectionReasons']['FOUNDATION_AFTER_INDEX_DATE'], 1)
        self.assertEqual([row['code'] for row in result['records']], ['000001'])
        for serial, expected in [('59', '1900-02-28'), ('61', '1900-03-01')]:
            cell = {'type': 'n', 'hasFormula': False, 'numberFormat': 'yyyy/m/d;@', 'rawValue': serial}
            self.assertEqual(history.excel_1900_date(cell), expected)

    def test_builtin_or_missing_number_formats_remain_inspectable_without_dropping_codes(self):
        for replacement in ['<c r="D4">', '<c r="D4" s="0">']:
            data = files([product(3, '000001'), product(4, '018774').replace('<c r="D4" s="2">', replacement)])
            result = parse(workbook(data))
            self.assertEqual(len(result['records']), 2)
            self.assertEqual(result['parseStatus'], 'partial')
            diagnostic = result['diagnostics']['unresolvedInceptionSamples'][0]
            self.assertIsNone(diagnostic['numberFormat'])
            self.assertEqual(diagnostic['rawValue'], '45118')
            self.assertIsNotNone(history.summary_for(result))

    def test_all_duplicate_code_rows_are_quarantined_with_unique_output_records(self):
        result = parse(workbook(files([product(3, '018774'), product(4, '018774', '冲突名称')])))
        self.assertEqual(result['parseStatus'], 'partial')
        self.assertEqual(result['records'], [])
        self.assertEqual(result['diagnostics']['duplicateCodeCount'], 1)
        self.assertEqual(result['diagnostics']['duplicateCodeRows'], 2)
        self.assertEqual(result['diagnostics']['rejectedRows'], 2)

    def test_a_bad_date_or_name_row_still_quarantines_its_readable_duplicate_code(self):
        for bad in [product(4, '018774', date='60'), product(4, '018774', cache=False),
                    product(4, '018774').replace('<c r="D4" s="2"><v>45118</v></c>', '')]:
            result = parse(workbook(files([product(3, '018774'), bad])))
            self.assertEqual(result['parseStatus'], 'partial')
            self.assertEqual(result['records'], [])
            self.assertEqual(result['diagnostics']['duplicateCodeCount'], 1)
            self.assertEqual(result['diagnostics']['duplicateCodeRows'], 2)
            self.assertEqual(result['diagnostics']['rejectedRows'], 2)
            self.assertEqual(result['evidence'][0]['unresolvedRows'], 2)
            self.assertEqual(result['diagnostics']['rejectionReasons']['DUPLICATE_FUND_CODE'], 2)
        # An unformatted numeric value cannot be guessed to be that code.
        unreadable = product(4, '018774').replace('r="B4" s="1"', 'r="B4"')
        result = parse(workbook(files([product(3, '018774'), unreadable])))
        self.assertEqual(result['diagnostics']['duplicateCodeCount'], 0)
        self.assertEqual([row['code'] for row in result['records']], ['018774'])

    def test_missing_physical_rows_and_noncontiguous_serials_remain_partial(self):
        result = parse(workbook(files([product(3, '000001'), product(5, '018774')])), expected=3)
        self.assertEqual(result['parseStatus'], 'partial')
        self.assertEqual(result['diagnostics']['missingPhysicalRows'], 1)
        self.assertEqual(result['evidence'][0]['unresolvedRows'], 1)
        self.assertFalse(result['diagnostics']['serialSequenceComplete'])
        result = parse(workbook(files([product(3, '000001'), product(4, '018774', serial=1)])))
        self.assertEqual(result['diagnostics']['duplicateSerialCount'], 1)
        self.assertEqual(result['diagnostics']['rejectionReasons']['SERIAL_POSITION_MISMATCH'], 1)

    def test_blank_rows_missing_columns_and_bad_cells_are_explicit_rejections(self):
        bad_rows = ['<row r="4"><c r="A4"/><c r="B4"/><c r="C4"/><c r="D4"/></row>',
                    '<row r="4"><c r="A4"><v>2</v></c></row>',
                    product(4, '018774').replace('r="B4"', 'r="B5"')]
        for row, reason in zip(bad_rows, ['EMPTY_PRODUCT_ROW', 'COLUMN_MISMATCH', 'INVALID_CELL']):
            result = parse(workbook(files([product(3, '000001'), row])))
            self.assertEqual(result['diagnostics']['observedProductRows'], 2)
            self.assertEqual(result['diagnostics']['parsedRows'], 1)
            self.assertEqual(result['diagnostics']['rejectedRows'], 1)
            self.assertEqual(result['diagnostics']['rejectionReasons'][reason], 1)
            self.assertEqual(result['parseStatus'], 'partial')

    def test_observation_requires_real_dated_timezone_aware_timestamp(self):
        for stamp in ['2026-06-29T12:00:00Z', '2026-10-26T12:00:00', '2099-01-01T00:00:00Z']:
            with self.assertRaises(history.HistoricalUniverseError): history.observation_time(stamp)

    def test_fetch_is_actions_only_and_uses_one_download_with_actual_observation(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(history, 'download_official') as download:
            with self.assertRaises(history.HistoricalUniverseError): history.fetch_official_overlay()
            download.assert_not_called()
        data = workbook()
        before = dt.datetime.now(dt.timezone.utc)
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), synthetic_pin(data), \
                patch.object(history, 'download_official', return_value=data) as download:
            result = history.fetch_official_overlay()
        download.assert_called_once_with()
        self.assertGreaterEqual(dt.datetime.fromisoformat(result['evidence'][0]['retrievedAt']), before)

    def test_cli_emits_only_small_summary_and_two_code_samples_without_production_mutation(self):
        overlay = parse(workbook())
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), \
                patch.object(history, 'fetch_official_overlay', return_value=overlay), \
                patch.object(history, 'OUTPUT', Path(folder) / 'result.json'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(history.main(), 0)
            result = json.loads((Path(folder) / 'result.json').read_bytes())
            self.assertLess((Path(folder) / 'result.json').stat().st_size, 96_000)
            self.assertNotIn('records', result)
            self.assertEqual(set(result['codeLookups']), {'018774', '018775'})
            self.assertTrue(result['codeLookups']['018774']['found'])
            self.assertFalse(result['codeLookups']['018775']['found'])
            self.assertFalse(result['historicalUniverseVerified'])
            self.assertFalse(result['productionCatalogModified'])
            self.assertEqual([path.name for path in Path(folder).iterdir()], ['result.json'])

    def test_local_cli_refuses_before_fetch_and_publication_guard_replaces_rejected_output(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(history, 'fetch_official_overlay') as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(history.main(), 2)
        fetch.assert_not_called()
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), \
                patch.object(history, 'fetch_official_overlay', return_value=parse(workbook())), \
                patch.object(history, 'inspect_content', return_value=['synthetic-rejection']), \
                patch.object(history, 'OUTPUT', Path(folder) / 'result.json'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(history.main(), 1)
            result = json.loads((Path(folder) / 'result.json').read_bytes())
            self.assertEqual(result['errorCode'], 'PUBLICATION_REJECTED')
            self.assertNotIn('records', result)
            self.assertNotIn('codeLookups', result)


if __name__ == '__main__': unittest.main()
