"""Synthetic in-memory XLSX checks. No official workbook is fetched locally."""
import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import verify_universe as verify

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NOW = dt.datetime(2026, 10, 26, 12, tzinfo=dt.timezone.utc)


def fixture_files():
    return {
        'xl/workbook.xml': f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets>'
            '<sheet name="产品表" sheetId="1" r:id="r1"/><sheet name="说明" sheetId="2" r:id="r2"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="r1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="r2" Type="{REL}/worksheet" Target="worksheets/sheet2.xml"/>'
            f'<Relationship Id="rs" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/>'
            f'<Relationship Id="rt" Type="{REL}/styles" Target="styles.xml"/></Relationships>',
        'xl/sharedStrings.xml': f'<sst xmlns="{NS}">' + ''.join(f'<si><t>{text}</t></si>' for text in
            ['基金代码', '基金简称', '018774', '合成基金A']) + '</sst>',
        'xl/styles.xml': f'<styleSheet xmlns="{NS}"><numFmts count="1"><numFmt numFmtId="164" formatCode="000000"/></numFmts>'
            '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>',
        'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{NS}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row>'
            '<row r="3"><c r="A3" s="1"><v>18775</v></c><c r="B3" t="inlineStr"><is><t>合成基金C</t></is></c></row>'
            '<row r="4"><c r="A4"><v>18774</v></c></row>'
            '<row r="5"><c r="A5" t="inlineStr"><is><r><t>018</t></r><r><t>775</t></r></is></c></row>'
            '<row r="6"><c r="A6" t="inlineStr"><is><t>不得出现在有限样例中的整表后续记录</t></is></c></row>'
            '</sheetData></worksheet>',
        'xl/worksheets/sheet2.xml': f'<worksheet xmlns="{NS}"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>未经推断的原始说明</t></is></c></row></sheetData></worksheet>',
    }


def workbook(files=None, extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in (files or fixture_files()).items():
            archive.writestr(name, data)
        for name, data in extra:
            archive.writestr(name, data)
    return output.getvalue()


class UniverseVerificationTests(unittest.TestCase):
    def assert_error(self, data, code):
        with self.assertRaises(verify.VerificationError) as raised:
            verify.inspect_workbook(data)
        self.assertEqual(raised.exception.code, code)

    def test_headers_rows_and_original_code_columns_are_bounded_and_not_schema_mapped(self):
        data = workbook()
        result = verify.run_verification(fetch=lambda: data, now=NOW)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['sourceSha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['sourceAsOf'], '2026-06-30')
        self.assertEqual(result['downloadedBytes'], len(data))
        self.assertEqual(result['sheetNames'], ['产品表', '说明'])
        self.assertEqual([item['rowCount'] for item in result['sheets']], [6, 1])
        self.assertEqual(len(result['sheets'][0]['firstFiveRows']), 5)
        self.assertEqual(result['schemaMapping'], 'not-inferred')
        self.assertFalse(result['productionCatalogModified'])
        self.assertEqual(result['lookup']['018774']['exactMatchCount'], 1)
        self.assertEqual(result['lookup']['018774']['numericCandidateCount'], 1)
        self.assertEqual(result['lookup']['018775']['exactMatchCount'], 2)
        cell = result['lookup']['018774']['matches'][0]['cells'][0]
        self.assertEqual((cell['reference'], cell['rawValue'], cell['value']), ('A2', '2', '018774'))
        formatted = result['lookup']['018775']['matches'][0]
        self.assertEqual(formatted['cells'][0]['rawValue'], '18775')
        self.assertEqual(formatted['cells'][0]['numberFormat'], '000000')
        self.assertEqual(formatted['hits'][0]['method'], 'explicit-six-digit-number-format')
        _result, encoded = verify.encoded_result(result)
        self.assertLess(len(encoded), verify.MAX_SUMMARY)
        self.assertNotIn('不得出现在有限样例中的整表后续记录', encoded.decode())

    def test_plain_numeric_candidates_never_become_confirmed_codes(self):
        files = fixture_files()
        files['xl/styles.xml'] = files['xl/styles.xml'].replace('formatCode="000000"', 'formatCode="0.00"')
        result = verify.inspect_workbook(workbook(files))
        self.assertEqual(result['lookup']['018775']['exactMatchCount'], 1)
        self.assertEqual(result['lookup']['018775']['numericCandidateCount'], 1)

    def test_shared_and_inline_phonetic_annotations_cannot_create_or_hide_codes(self):
        for displayed, annotation, expected in [('018774', '注音', 1), ('018', '774', 0)]:
            for inline in (False, True):
                with self.subTest(displayed=displayed, inline=inline):
                    files = fixture_files()
                    body = f'<t>{displayed}</t><rPh sb="0" eb="3"><t>{annotation}</t></rPh>'
                    if inline:
                        files['xl/worksheets/sheet1.xml'] = files['xl/worksheets/sheet1.xml'].replace(
                            '<c r="A2" t="s"><v>2</v></c>', f'<c r="A2" t="inlineStr"><is>{body}</is></c>')
                    else:
                        files['xl/sharedStrings.xml'] = files['xl/sharedStrings.xml'].replace('<si><t>018774</t></si>', f'<si>{body}</si>')
                    result = verify.inspect_workbook(workbook(files))
                    self.assertEqual(result['lookup']['018774']['exactMatchCount'], expected)

    def test_match_rows_and_cell_text_are_bounded_with_explicit_omission_counts(self):
        files = fixture_files()
        rows = ''.join(f'<row r="{row}"><c r="A{row}" t="inlineStr"><is><t>018774</t></is></c>'
                       f'<c r="B{row}" t="inlineStr"><is><t>{"长字段" * 100}</t></is></c></row>' for row in range(1, 16))
        files['xl/worksheets/sheet1.xml'] = f'<worksheet xmlns="{NS}"><sheetData>{rows}</sheetData></worksheet>'
        result = verify.inspect_workbook(workbook(files))
        lookup = result['lookup']['018774']
        self.assertEqual(lookup['exactMatchCount'], 15)
        self.assertEqual(len(lookup['matches']), verify.MAX_MATCH_ROWS)
        self.assertEqual(lookup['omittedMatchRows'], 7)
        self.assertTrue(lookup['matches'][0]['cells'][1]['truncated'])
        self.assertLessEqual(len(lookup['matches'][0]['cells'][1]['value'].encode()), 192)

    def test_zip_paths_entry_count_and_expanded_total_are_checked_before_xml(self):
        for name in ['../escape.xml', '/xl/escape.xml', 'xl/../escape.xml', 'xl\\escape.xml']:
            self.assert_error(workbook(extra=[(name, 'x')]), 'ZIP_PATH_INVALID')
        with patch.object(verify, 'MAX_ENTRIES', 2):
            self.assert_error(workbook(), 'ZIP_LIMIT')
        with patch.object(verify, 'MAX_EXPANDED', 200):
            self.assert_error(workbook(), 'ZIP_LIMIT')
        self.assert_error(b'not a ZIP archive', 'INVALID_ZIP')

    def test_duplicate_zip_members_and_symlinks_cannot_be_parsed_as_workbook_parts(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            self.assert_error(workbook(extra=[('xl/workbook.xml', 'duplicate')]), 'INVALID_ZIP')
        info = zipfile.ZipInfo('xl/link.xml')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        self.assert_error(workbook(extra=[(info, 'sharedStrings.xml')]), 'INVALID_ZIP')

    def test_dtd_entity_utf16_and_excessive_xml_depth_are_rejected(self):
        for xml in [f'<!DOCTYPE worksheet [<!ENTITY a "hidden">]><worksheet xmlns="{NS}"><sheetData/></worksheet>',
                    f'<worksheet xmlns="{NS}"><sheetData>{"<x>" * 65}{"</x>" * 65}</sheetData></worksheet>',
                    f'<?xml version="1.0" encoding="UTF-16"?><worksheet xmlns="{NS}"><sheetData/></worksheet>'.encode('utf-16')]:
            files = fixture_files(); files['xl/worksheets/sheet1.xml'] = xml
            self.assert_error(workbook(files), 'XML_LIMIT' if isinstance(xml, str) and '<x>' in xml else 'XML_REJECTED')

    def test_unknown_shared_string_row_cell_or_relationship_schema_fails_closed(self):
        for path, old, new in [
                ('xl/worksheets/sheet1.xml', '<v>2</v>', '<v>999</v>'),
                ('xl/worksheets/sheet1.xml', 'r="A2"', 'r="A3"'),
                ('xl/worksheets/sheet1.xml', 'r="A2"', 'r="XFE2"'),
                ('xl/worksheets/sheet1.xml', '<sheetData>', '<notSheetData>'),
                ('xl/_rels/workbook.xml.rels', 'Target="worksheets/sheet1.xml"', 'Target="https://example.org/book.xml" TargetMode="External"')]:
            files = fixture_files(); files[path] = files[path].replace(old, new)
            if old == '<sheetData>': files[path] = files[path].replace('</sheetData>', '</notSheetData>')
            self.assert_error(workbook(files), 'UNSUPPORTED_SCHEMA')

    def test_xml_cell_and_shared_string_counts_are_bounded(self):
        with patch.object(verify, 'MAX_CELLS', 2): self.assert_error(workbook(), 'XML_LIMIT')
        with patch.object(verify, 'MAX_STRINGS', 2): self.assert_error(workbook(), 'XML_LIMIT')

    def test_local_download_and_main_refuse_before_network_or_file_output(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(verify, 'build_opener') as opener:
            with self.assertRaises(verify.VerificationError) as error:
                verify.download_official()
            self.assertEqual(error.exception.code, 'ACTIONS_REQUIRED')
            with patch.object(verify, 'run_verification') as run, contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(verify.main(), 2)
            run.assert_not_called(); opener.assert_not_called()
            self.assertEqual(json.loads(log.getvalue())['errorCode'], 'ACTIONS_REQUIRED')

    def test_cloud_download_is_fixed_host_single_request_and_stream_size_limited(self):
        class Response(io.BytesIO):
            status = 200
            headers = {}
        response = Response(b'x' * 129)
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), patch.object(verify, 'MAX_DOWNLOAD', 128), \
                patch.object(verify, 'build_opener') as factory:
            factory.return_value.open.return_value = response
            with self.assertRaises(verify.VerificationError) as raised:
                verify.download_official()
            self.assertEqual(raised.exception.code, 'DOWNLOAD_TOO_LARGE')
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, verify.quote(verify.SOURCE_URL, safe=':/'))
            self.assertEqual(factory.return_value.open.call_count, 1)
        with self.assertRaises(verify.VerificationError) as raised:
            verify.NoRedirect().redirect_request(None, None, 302, None, None, 'https://example.org/')
        self.assertEqual(raised.exception.code, 'HTTP_REJECTED')

    def test_cloud_main_writes_only_guarded_summary_and_fixed_error_categories(self):
        success = verify.run_verification(fetch=workbook, now=NOW)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'result.json'
            with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), patch.object(verify, 'OUTPUT', output), \
                    patch.object(verify, 'run_verification', return_value=success), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verify.main(), 0)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['result.json'])
            self.assertEqual(json.loads(output.read_bytes())['status'], 'verified')
        def fail(): raise RuntimeError('private debug text must never be serialized')
        failed = verify.run_verification(fetch=fail, now=NOW)
        self.assertEqual(failed['errorCode'], 'UNIVERSE_VERIFICATION_FAILED')
        self.assertNotIn('private debug', json.dumps(failed))

    def test_output_guard_and_size_failures_replace_untrusted_content(self):
        original = verify.run_verification(fetch=workbook, now=NOW)
        with patch.object(verify, 'inspect_content', return_value=['synthetic-rejection']):
            result, encoded = verify.encoded_result(original)
        self.assertEqual(result['errorCode'], 'PUBLICATION_REJECTED')
        self.assertNotIn('sheets', json.loads(encoded))
        with patch.object(verify, 'MAX_SUMMARY', 100):
            result, _ = verify.encoded_result(original)
        self.assertEqual(result['errorCode'], 'SUMMARY_TOO_LARGE')

    def test_workflow_is_manual_read_only_and_uploads_no_source_workbook(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/verify-universe.yml').read_text()
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('timeout-minutes: 15', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('path: .cache/universe-verification/result.json', workflow)
        for forbidden in ['contents: write', 'git push', 'web/data/', 'secrets.', 'pip install', 'schedule:']:
            self.assertNotIn(forbidden, workflow)


if __name__ == '__main__':
    unittest.main()
