"""Synthetic document errors across collection, JS normalization and status."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from collect import collect_fund, safe_error
from providers import DataError
import traversal

NOW = '2026-10-29T12:00:00Z'
PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
CONFIG = {**PHASE, 'collectorVersion': 5, 'funds': [{'code': '000001', 'companyCode': 'co1'}]}
TARGET = {'title': '合成基金2026年第2季度报告', 'periodEnd': PHASE['targetPeriodEnd'],
          'kind': 'quarterly', 'publishedAt': '2026-07-20', 'sourceUrl': 'https://example.org/quarter.pdf'}
BASELINE = {**TARGET, 'periodEnd': PHASE['baselinePeriodEnd'], 'publishedAt': '2026-04-20',
            'sourceUrl': 'https://example.org/baseline.pdf'}
HOLDING = {'stockCode': '600001', 'stockName': '合成证券', 'market': 'CN', 'currency': 'CNY',
           'shares': 100, 'weightPct': 5, 'marketValueYuan': 500}


class Source:
    def __init__(self, reports=None, failures=None, holdings=None):
        self.reports = reports if reports is not None else [TARGET, BASELINE]
        self.failures = failures or {}
        self.rows = holdings or {}
        self.documents = []

    def overview(self, code):
        return {'name': '合成基金', 'company': '合成公司', 'managers': []}

    def announcements(self, code, as_of):
        return copy.deepcopy(self.reports)

    def holdings(self, code, year):
        return copy.deepcopy(self.rows)

    def report_details(self, document):
        self.documents.append(copy.deepcopy(document))
        if document['sourceUrl'] in self.failures:
            raise DataError(self.failures[document['sourceUrl']])
        return {'navYuan': 10000, 'narrative': None}


def collect_source(source, **extra):
    return collect_fund({**CONFIG['funds'][0], **extra}, NOW[:10], provider=source,
                        target_period=PHASE['targetPeriodEnd'], baseline_period=PHASE['baselinePeriodEnd'])


def snapshot(errors, fund=None):
    # The public builder correctly refuses an entirely empty batch. A separate
    # successful synthetic product lets failed-product diagnostics cross the
    # ordinary mixed-batch boundary without weakening that publication guard.
    control = {'code': '000003', 'name': '合成对照基金', 'managers': [], 'reports': [
        {**TARGET, 'fundCode': '000003', 'coverage': 'top10', 'navYuan': 10000, 'holdings': [HOLDING]}]}
    payload = {'asOf': NOW[:10], 'retrievedAt': NOW, 'errors': errors, 'barsByStock': {},
               'funds': [fund or {'code': '000001', 'reports': []}, control]}
    config = {**CONFIG, 'funds': CONFIG['funds'] + [{'code': '000003', 'companyCode': 'co1'}]}
    script = ("const fs=require('node:fs');const{buildSnapshot}=require('./scripts/build-data');"
              "const x=JSON.parse(fs.readFileSync(0,'utf8'));"
              "process.stdout.write(JSON.stringify(buildSnapshot(x.payload,{config:x.config,now:x.now}))); ")
    result = subprocess.run(['node', '-e', script], input=json.dumps({'payload': payload, 'config': config, 'now': NOW}),
                            cwd=ROOT, text=True, capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


def status(errors, fund=None):
    return traversal.build_status(CONFIG, snapshot(errors, fund), NOW)


def issue(error='HTTP_ERROR', document=TARGET, stage='report'):
    return safe_error('000001', stage, DataError(error), document=document)


class ReportAcquisitionErrorTests(unittest.TestCase):
    def test_download_exception_survives_real_collector_without_secondary_attribute_error(self):
        for error in ('HTTP_ERROR', 'TIMEOUT', 'NETWORK_ERROR', 'TLS_FAILED'):
            with self.subTest(error=error):
                source = Source(failures={TARGET['sourceUrl']: error, BASELINE['sourceUrl']: 'TLS_FAILED'})
                fund, errors = collect_source(source)
                self.assertEqual(len(source.documents), 2)
                self.assertEqual(fund['reports'], [])
                report_errors = [row for row in errors if row['stage'] == 'report']
                self.assertEqual(report_errors, [issue(error), issue('TLS_FAILED', BASELINE)])
                result = snapshot(errors, fund)
                self.assertEqual(result['batchResults'][0]['errorCode'], 'NO_VALID_REPORTS')
                self.assertEqual([row for row in result['errors'] if row['stage'] == 'report'], report_errors)
                event = traversal.build_status(CONFIG, result, NOW)
                self.assertTrue(traversal.valid_status(event, CONFIG, NOW))
                self.assertEqual(event['funds'][0]['errorCode'], error)

    def test_secondary_quarter_download_keeps_its_actual_kind_and_does_not_fail_valid_report(self):
        interim = {**TARGET, 'kind': 'semiannual', 'publishedAt': '2026-08-28',
                   'sourceUrl': 'https://example.org/interim.pdf'}
        source = Source([interim, TARGET], {TARGET['sourceUrl']: 'TIMEOUT'},
                        {PHASE['targetPeriodEnd']: [HOLDING]})
        fund, errors = collect_source(source)
        self.assertEqual(fund['reports'][0]['kind'], 'semiannual')
        self.assertEqual([row for row in errors if row['stage'] == 'quarterly-report'],
                         [issue('TIMEOUT', TARGET, 'quarterly-report')])
        result = snapshot(errors, fund)
        self.assertEqual(result['funds'][0]['selectedReport']['kind'], 'semiannual')
        self.assertNotEqual(traversal.build_status(CONFIG, result, NOW)['funds'][0]['status'], 'failed')

    def test_malformed_details_return_keeps_typed_parse_error_without_secondary_attribute_error(self):
        for value in (None, [], 'invalid details', 42):
            with self.subTest(value=value):
                class MalformedSource(Source):
                    def report_details(self, document):
                        return copy.deepcopy(value)
                fund, errors = collect_source(MalformedSource([TARGET]))
                self.assertEqual(fund['reports'], [])
                self.assertEqual([row for row in errors if row['stage'] == 'report'], [issue('PARSE_FAILED')])
                self.assertEqual(status(errors, fund)['funds'][0]['errorCode'], 'PARSE_FAILED')

    def test_actual_locator_acquisition_preserves_stage_and_document_binding(self):
        from test_report_locators import locator
        value = locator()
        candidate_url = value['candidates'][0]['sourceUrl']
        source = Source([TARGET], {candidate_url: 'HTTP_ERROR', TARGET['sourceUrl']: 'HTTP_ERROR'})
        fund, errors = collect_source(source, reportLocator=value, reportLocatorOnly=True)
        locator_errors = [row for row in errors if row['stage'] == 'report-locator']
        self.assertEqual(locator_errors, [issue(stage='report-locator')])
        result = snapshot(errors, fund)
        self.assertEqual([row for row in result['errors'] if row['stage'] == 'report-locator'], locator_errors)
        self.assertEqual(traversal.build_status(CONFIG, result, NOW)['funds'][0]['errorCode'], 'HTTP_ERROR')

    def test_baseline_only_and_legacy_unbound_download_errors_remain_generic(self):
        cases = [[issue(document=BASELINE)], [issue(document=None)],
                 [issue(), issue(document=None)], [issue(document=BASELINE), issue(document=None)]]
        for errors in cases:
            with self.subTest(errors=errors):
                self.assertEqual(status(errors)['funds'][0]['errorCode'], 'NO_VALID_REPORTS')

    def test_conflicting_target_errors_remain_generic(self):
        for other in ('TIMEOUT', 'REPORT_IDENTITY_MISMATCH', 'CONTRADICTORY_NO_EQUITIES'):
            with self.subTest(other=other):
                self.assertEqual(status([issue(), issue(other, stage='report-locator')])['funds'][0]['errorCode'],
                                 'NO_VALID_REPORTS')

    def test_identical_target_causes_deduplicate_and_other_funds_do_not_conflict(self):
        other_fund = {**issue('TIMEOUT'), 'code': '000002'}
        interim = {**TARGET, 'kind': 'semiannual'}
        event = status([issue(), issue(), issue(document=interim), other_fund])
        self.assertEqual(event['funds'][0]['errorCode'], 'HTTP_ERROR')

    def test_existing_definite_parser_and_announcement_reasons_keep_precedence(self):
        for errors, expected in [
            ([issue(), issue('PDF_PARSE_FAILED')], 'PDF_PARSE_FAILED'),
            ([issue(), issue('PDF_PARSE_FAILED', stage='report-locator')], 'PDF_PARSE_FAILED'),
            ([issue(), {'code': '000001', 'stage': 'target-report', 'errorCode': 'TARGET_REPORT_UNAVAILABLE'}],
             'TARGET_REPORT_UNAVAILABLE'),
            ([issue(), {'code': '000001', 'stage': 'announcements', 'errorCode': 'NO_REPORTS'}], 'NO_REPORTS'),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(status(errors)['funds'][0]['errorCode'], expected)

    def test_nonreport_errors_cannot_supply_report_context(self):
        for stage in ('metadata', 'holdings', 'build', 'unknown-stage'):
            with self.subTest(stage=stage):
                raw = {**issue(), 'stage': stage, 'sourceUrl': 'https://example.org/unneeded.pdf', 'message': 'not retained'}
                result = snapshot([raw])
                normalized = next(row for row in result['errors'] if row['errorCode'] == 'HTTP_ERROR')
                self.assertEqual(set(normalized), {'code', 'stage', 'errorCode'})
                self.assertEqual(traversal.build_status(CONFIG, result, NOW)['funds'][0]['errorCode'], 'NO_VALID_REPORTS')

    def test_invalid_or_partial_typed_context_is_dropped_as_a_pair(self):
        variants = [('reportPeriodEnd', '2026-02-30'), ('reportPeriodEnd', '20260630'),
                    ('reportPeriodEnd', None), ('reportKind', None), ('reportKind', []), ('reportKind', 'unknown')]
        for field, value in variants:
            with self.subTest(field=field, value=value):
                raw = {**issue(), field: value}
                result = snapshot([raw])
                normalized = next(row for row in result['errors'] if row['stage'] == 'report')
                self.assertEqual(set(normalized), {'code', 'stage', 'errorCode'})
                self.assertEqual(traversal.build_status(CONFIG, result, NOW)['funds'][0]['errorCode'], 'NO_VALID_REPORTS')
                document = {**TARGET, 'periodEnd' if field == 'reportPeriodEnd' else 'kind': value}
                self.assertEqual(set(issue(document=document)), {'code', 'stage', 'errorCode'})

    def test_valid_binding_drops_unneeded_error_payload_and_keeps_only_public_typed_fields(self):
        raw = {**issue(), 'message': 'discard', 'sourceUrl': 'https://example.org/unneeded.pdf',
               'providerErrors': ['discard'], 'extra': {'discard': True}}
        result = snapshot([raw])
        normalized = next(row for row in result['errors'] if row['stage'] == 'report')
        self.assertEqual(normalized, issue())

    def test_failure_status_replay_preserves_cumulative_attempts_and_original_clocks(self):
        directory = {'schemaVersion': 1, 'retrievedAt': '2026-10-27T10:00:00Z',
                     'companies': [{'code': 'co1', 'name': '合成公司', 'fundCount': 1, 'directoryStatus': 'complete'}],
                     'funds': [{'code': '000001', 'companyCode': 'co1'}]}
        original = '2026-10-27T11:00:00Z'
        previous = {'schemaVersion': 1, 'phase': PHASE, 'collectorVersion': 5,
                    'funds': [{'code': '000001', 'companyCode': 'co1', 'status': 'failed',
                               'errorCode': 'NO_VALID_REPORTS', 'attempts': 1, 'totalAttempts': 7,
                               'lastAttemptAt': original}]}
        untouched = traversal.merge_progress(directory, previous, CONFIG, now=NOW)
        self.assertEqual((untouched['funds'][0]['totalAttempts'], untouched['funds'][0]['lastAttemptAt']), (7, original))
        event = status([issue()])
        merged = traversal.merge_progress(directory, untouched, CONFIG, [event], NOW)
        replay = traversal.merge_progress(directory, merged, CONFIG, [event], NOW)
        self.assertEqual((replay['funds'][0]['attempts'], replay['funds'][0]['totalAttempts']), (2, 8))
        self.assertEqual(replay['funds'][0]['lastAttemptAt'], event['attemptedAt'])
        self.assertEqual(replay['funds'][0]['errorCode'], 'HTTP_ERROR')
        self.assertEqual(previous['funds'][0]['lastAttemptAt'], original)
        self.assertEqual(directory['retrievedAt'], '2026-10-27T10:00:00Z')


if __name__ == '__main__':
    unittest.main()
