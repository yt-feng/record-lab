"""Small synthetic archives only; official data parsing runs on Actions."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import verify_universe_sources as sources
from test_historical_universe import files, workbook, synthetic_pin, NOW


def with_core(content):
    entries = files()
    entries['docProps/core.xml'] = (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
        + content + '</cp:coreProperties>')
    return workbook(entries)


def diagnose(data):
    with synthetic_pin(data):
        return sources.run_diagnostic(fetch=lambda: data, now=NOW)


class UniverseSourceDiagnosticTests(unittest.TestCase):
    def test_missing_period_date_and_mismatched_total_remain_partial(self):
        result = diagnose(workbook())
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['diagnosticComplete'])
        self.assertEqual(result['pinnedParser']['parsedRows'], 2)
        self.assertEqual(result['periodEvidence']['status'], 'filename-only')
        self.assertEqual(result['comparison']['aggregateMinusParsed'], 14351)
        self.assertIn('PRODUCT_COUNT_DISCREPANCY', result['unresolvedReasons'])
        self.assertFalse(result['historicalUniverseVerified'])
        self.assertFalse(result['catalogCompletenessVerified'])
        self.assertFalse(result['productionCatalogModified'])
        self.assertEqual(sources.compare_aggregate(14170, 14170)['aggregateMinusParsed'], 183)

    def test_matching_aggregate_never_certifies_scope_or_product_set(self):
        result = sources.compare_aggregate(14353, 14353)
        self.assertTrue(result['countsMatch'])
        self.assertEqual(result['status'], 'unresolved')
        self.assertFalse(result['sameScopeVerified'])
        self.assertFalse(result['productSetReconciled'])

    def test_created_modified_dates_and_author_text_are_not_period_evidence(self):
        secret = 'SYNTHETIC_AUTHOR_VALUE_DO_NOT_EXPORT'
        data = with_core(f'<dc:creator>{secret}</dc:creator><cp:lastModifiedBy>{secret}</cp:lastModifiedBy>'
                         '<dcterms:created>2026-06-30T08:00:00+08:00</dcterms:created>'
                         '<dcterms:modified>invalid timestamp</dcterms:modified>')
        result = diagnose(data)
        core = result['workbook']['coreProperties']
        self.assertEqual(core['createdAt'], '2026-06-30T00:00:00+00:00')
        self.assertEqual(core['invalidTimestampProperties'], 1)
        self.assertEqual(core['authorPropertyCount'], 2)
        self.assertEqual(result['periodEvidence']['status'], 'filename-only')
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn('invalid timestamp', json.dumps(result))

    def test_explicit_dates_corroborate_or_conflict_but_never_complete(self):
        for date, status in [('20260630', 'explicit-date-found'), ('20260531', 'contradictory')]:
            result = diagnose(with_core(f'<dc:title>基金产品索引（截至{date}）</dc:title>'))
            self.assertEqual(result['periodEvidence']['status'], status)
            self.assertEqual(result['status'], 'partial')
            self.assertFalse(result['historicalUniverseVerified'])
        self.assertEqual(sources.explicit_period_dates('设立日期2026-06-30; created2026-06-30'), [])
        self.assertEqual(sources.explicit_period_dates('截至20260230'), [])

    def test_unknown_hash_does_not_apply_pinned_product_schema(self):
        result = sources.run_diagnostic(fetch=workbook, now=NOW)
        self.assertFalse(result['sourcePinMatches'])
        self.assertEqual(result['pinnedParser']['parseStatus'], 'not-run')
        self.assertIsNone(result['comparison']['parsedProductRows'])
        self.assertIn('SOURCE_HASH_MISMATCH', result['unresolvedReasons'])

    def test_metadata_xml_rejects_entities_and_oversized_properties(self):
        entries = files()
        for content in ['<!DOCTYPE root [<!ENTITY e "bad">]><root/>', 'x' * (sources.MAX_CORE_BYTES + 1)]:
            entries['docProps/core.xml'] = content
            result = diagnose(workbook(entries))
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(result['diagnosticComplete'])
            self.assertIn(result['errorCode'], {'XML_REJECTED', 'XML_LIMIT'})

    def test_fixed_benchmarks_are_cited_aggregate_observations_not_live_fetches(self):
        result = diagnose(workbook())
        for row in result['aggregateBenchmarks']:
            self.assertTrue(row['sourceUrl'].startswith('https://www.amac.org.cn/'))
            self.assertEqual(row['scope'], 'aggregate-only-not-product-list')
            self.assertFalse(row['fetchedByThisRun'])
            self.assertTrue(row['excludesFundsReportedForLiquidation'])

    def test_publication_guard_and_actions_gate_apply_before_output(self):
        result, data = sources.encode_result(diagnose(workbook()))
        self.assertEqual(result['status'], 'partial')
        self.assertLess(len(data), sources.MAX_OUTPUT_BYTES)
        with patch.object(sources, 'inspect_content', return_value=['synthetic violation']):
            guarded, _ = sources.encode_result(result)
        self.assertEqual(guarded['errorCode'], 'PUBLICATION_REJECTED')
        with tempfile.TemporaryDirectory() as directory, patch.dict(sources.os.environ, {}, clear=True), \
                patch.object(sources, 'OUTPUT', Path(directory) / 'result.json'), \
                patch.object(sources, 'run_diagnostic') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sources.main(), 2)
            run.assert_not_called()
            self.assertFalse(sources.OUTPUT.exists())

    def test_partial_diagnostic_writes_only_guarded_artifact_and_returns_success(self):
        result = diagnose(workbook())
        with tempfile.TemporaryDirectory() as directory, patch.dict(sources.os.environ, {'GITHUB_ACTIONS': 'true'}), \
                patch.object(sources, 'OUTPUT', Path(directory) / 'diagnostic' / 'result.json'), \
                patch.object(sources, 'run_diagnostic', return_value=result), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sources.main(), 0)
            stored = json.loads(sources.OUTPUT.read_text())
            self.assertEqual(stored['status'], 'partial')
            self.assertFalse(stored['catalogCompletenessVerified'])
            self.assertEqual([path.relative_to(directory).as_posix() for path in Path(directory).rglob('*') if path.is_file()],
                             ['diagnostic/result.json'])


if __name__ == '__main__':
    unittest.main()
