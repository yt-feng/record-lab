"""Bounded immutable-data and two-quarter backfill verification, no network."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).parents[1] / 'scripts'), str(Path(__file__).parent)]
import report_extract
import verify_strategy_backfill as verifier
from providers import DataError
from source_dates import china_today
from test_strategy_evidence_pool import RECEIVED

COMMIT = 'a' * 40
BODY = '在本报告期我们持续看好人工智能方向。'
ROOT = Path(__file__).parents[1]


def details(quarter='2026Q2', body=BODY):
    end, _ = verifier.PERIODS[quarter]
    number = quarter[-1]
    start = '4月1日' if number == '2' else '7月1日'
    ending = '6月30日' if number == '2' else '9月30日'
    report = {'sourceUrl': 'https://example.org/' + quarter + '.pdf', 'periodEnd': end, 'kind': 'quarterly',
              'publishedAt': '2026-07-21' if number == '2' else '2026-10-21', 'retrievedAt': RECEIVED}
    pages = [f'合成基金2026年第{number}季度报告\n报告期2026年{start}至2026年{ending}',
             '§2 基金产品概况\n基金主代码 000001\n', '§3 主要财务指标和基金净值表现\n',
             '4.4 报告期内基金的投资策略和运作分析\n' + body + '\n4.5 报告期内基金的业绩表现\n净值表现。']
    with patch.object(report_extract, '_pypdf_pages', return_value=pages):
        value = report_extract.extract_report(b'%PDF synthetic', report['sourceUrl'], end, 'quarterly', env={},
            parse_pages=lambda *_: {'navYuan': 1000}, published_at=report['publishedAt'], retrieved_at=RECEIVED)
    return report, value


def old_data(quarter='2026Q2', body=BODY, scanned=False):
    report, template = details(quarter, body)
    source = {**report, **copy.deepcopy(template), 'navYuan': 1000, 'coverage': 'top10', 'holdings': [
        {'market': 'CN', 'stockCode': '600001', 'stockName': '合成证券', 'currency': 'CNY', 'shares': 10, 'marketValueYuan': 100, 'weightPct': 10}]}
    if not scanned:
        for key in ('strategyClaims', 'strategyClaimsIdentityEvidence', 'strategyEvidencePool', 'strategyEvidenceBudget'):
            source.pop(key, None)
    end, baseline = verifier.PERIODS[quarter]
    phase = {'targetPeriodEnd': end, 'baselinePeriodEnd': baseline, 'funds': [{'code': '000001'}]}
    raw = {'asOf': china_today(), 'retrievedAt': RECEIVED, 'funds': [{'code': '000001', 'name': '合成基金',
           'companyId': 'co1', 'company': '合成公司', 'reports': [source]}], 'barsByStock': {}, 'errors': []}
    node = """const fs=require('fs'),{buildSnapshot}=require('./scripts/build-data'),{mergeSnapshots}=require('./scripts/merge-parts');
const x=JSON.parse(fs.readFileSync(0,'utf8')),snapshot=buildSnapshot(x.raw,{config:x.phase,now:x.now});
const r=mergeSnapshots({snapshots:[snapshot],catalog:{companies:[{code:'co1',name:'合成公司',fundCount:1}],
funds:[{code:'000001',companyCode:'co1',companyName:'合成公司'}]},config:x.phase,now:x.now,reusePrices:false});
process.stdout.write(JSON.stringify({manifest:r.manifest,envelope:r.companySnapshots.co1}));"""
    result = subprocess.run(['node', '-e', node], input=json.dumps({'raw': raw, 'phase': phase,
        'now': dt.datetime.now(dt.timezone.utc).isoformat()}), text=True, capture_output=True, timeout=30, cwd=ROOT, check=True)
    return json.loads(result.stdout), (report, template)


class Reader:
    commit = COMMIT
    last_blob = 'b' * 40
    def __init__(self, data): self.data, self.reads = data, []
    def read(self, path, maximum=verifier.MAX_PART_BYTES):
        self.reads.append(path)
        if path not in self.data: raise verifier.VerificationError('DATA_READ_FAILED')
        encoded = json.dumps(self.data[path], ensure_ascii=False).encode()
        self.last_blob = hashlib.sha1(b'blob ' + str(len(encoded)).encode() + b'\0' + encoded).hexdigest()
        return copy.deepcopy(self.data[path])
    def unchanged(self): return True


def setup_reader(body=BODY, scanned=False):
    records, templates = {}, {}
    for quarter in verifier.PERIODS:
        value, (report, template) = old_data(quarter, body, scanned)
        prefix = 'web/data/periods/' + quarter + '/'
        records[prefix + 'latest.json'] = value['manifest']
        records[prefix + 'companies/co1.json'] = value['envelope']
        records[prefix + 'catalog.json'] = {'schemaVersion': 1, 'targetPeriodEnd': verifier.PERIODS[quarter][0],
            'companies': [{'code': 'co1', 'name': '合成公司'}],
            'funds': [{'code': '000001', 'companyCode': 'co1', 'companyName': '合成公司'}]}
        templates[report['sourceUrl']] = template
    return Reader(records), templates


def source_factory(templates, calls, *, changed=False, failure=False):
    class Source:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if failure: raise DataError('TIMEOUT')
            return b'%PDF synthetic'
        def report_details(self, document, max_pages=300):
            self.get(document['sourceUrl'], binary=True)
            value = copy.deepcopy(templates[document['sourceUrl']])
            at = dt.datetime.now(dt.timezone.utc).isoformat()
            value['strategyEvidenceRetrievedAt'] = at
            for key in ('strategyClaims', 'strategyEvidencePool'): value[key]['source']['retrievedAt'] = at
            for key in ('strategyClaimsIdentityEvidence', 'portfolioIdentityEvidence'): value[key]['retrievedAt'] = at
            if changed: value['strategySectionCoverage']['sourceTextHash'] = '0' * 64
            return value
    return Source


class StrategyBackfillVerificationTests(unittest.TestCase):
    def diagnostic_setup(self):
        reader, _ = setup_reader()
        sample = verifier.select_sample(reader, '2026Q2', dt.datetime.now(dt.timezone.utc).isoformat())
        def control(fund):
            return verifier.bridge({'mode': 'normalize-only', 'fund': fund, 'envelope': sample['envelope'],
                'config': sample['phase'], 'now': dt.datetime.now(dt.timezone.utc).isoformat()})
        value = control(sample['fund'])
        expected = {'quarter': '2026Q2', 'dataPath': sample['dataPath'], 'dataBlobSha': sample['dataBlobSha'],
                    'beforeHash': value['protectedBeforeHash'], 'observedAfterHash': value['normalizedHash']}
        return reader, sample, expected, control

    def test_zero_request_diagnosis_binds_original_blob_and_observed_hashes(self):
        reader, _, expected, _ = self.diagnostic_setup()
        with tempfile.TemporaryDirectory() as directory, patch.object(verifier, 'DIAGNOSTIC_COMMIT', reader.commit), \
             patch.object(verifier, 'DIAGNOSTIC_SAMPLES', {'000001': expected}), \
             patch.object(verifier, 'collect', side_effect=AssertionError('no source requests')), \
             patch.object(verifier, 'PublicProvider', side_effect=AssertionError('no provider')):
            value = verifier.diagnose_normalization(reader, '000001', Path(directory) / 'result.json')
        self.assertEqual(value['status'], 'diagnosed')
        self.assertEqual(value['originalPdfRequestsStarted'], 0)
        self.assertTrue(value['control']['matchesPreviouslyObservedBefore'])
        self.assertTrue(value['control']['matchesPreviouslyObservedAfter'])
        self.assertTrue(value['control']['nonStrategyFieldsUnchanged'])
        self.assertEqual(value['control']['differences'], [])

    def test_zero_request_diagnosis_rejects_wrong_commit_blob_and_sample(self):
        reader, _, expected, _ = self.diagnostic_setup()
        with tempfile.TemporaryDirectory() as directory:
            for change in ('commit', 'blob', 'code'):
                altered = copy.deepcopy(expected)
                if change == 'blob': altered['dataBlobSha'] = '0' * 40
                with patch.object(verifier, 'DIAGNOSTIC_COMMIT', '0' * 40 if change == 'commit' else reader.commit), \
                     patch.object(verifier, 'DIAGNOSTIC_SAMPLES', {'000001': altered}):
                    with self.assertRaisesRegex(verifier.VerificationError, 'DIAGNOSTIC_BINDING_INVALID'):
                        verifier.diagnose_normalization(reader, '000002' if change == 'code' else '000001', Path(directory) / 'result.json')

    def test_mismatched_observed_before_hash_stays_partial_in_diagnostic_mode(self):
        reader, _, expected, _ = self.diagnostic_setup(); expected['beforeHash'] = '0' * 64
        with tempfile.TemporaryDirectory() as directory, patch.object(verifier, 'DIAGNOSTIC_COMMIT', reader.commit), \
             patch.object(verifier, 'DIAGNOSTIC_SAMPLES', {'000001': expected}):
            value = verifier.diagnose_normalization(reader, '000001', Path(directory) / 'result.json')
        self.assertEqual(value['status'], 'partial')
        self.assertFalse(value['control']['matchesPreviouslyObservedBefore'])

    def test_safe_differences_report_added_defaults_but_never_values_or_unknown_keys(self):
        _, sample, _, control = self.diagnostic_setup()
        fund = copy.deepcopy(sample['fund']); fund['selectedReport'].pop('fundCode', None)
        fund['UNAPPROVED_PRIVATE_KEY'] = 'UNAPPROVED_PRIVATE_VALUE'
        value = control(fund)
        self.assertTrue(verifier.valid_differences(value))
        self.assertIn({'path': 'fund.selectedReport.fundCode', 'beforeType': 'missing', 'afterType': 'string', 'change': 'added'}, value['differences'])
        self.assertGreaterEqual(value['unmappedDifferenceCount'], 1)
        self.assertNotIn('UNAPPROVED', json.dumps(value))
        self.assertFalse(value['nonStrategyFieldsUnchanged'])
        bad = copy.deepcopy(value); bad['differences'][0]['value'] = 'UNAPPROVED_PRIVATE_VALUE'
        self.assertFalse(verifier.valid_differences(bad))
        bad = copy.deepcopy(value); bad['differences'][0]['path'] = 'fund.UNAPPROVED_PRIVATE_KEY'
        self.assertFalse(verifier.valid_differences(bad))
        bad = copy.deepcopy(value); bad['differences'][0]['beforeType'] = []
        self.assertFalse(verifier.valid_differences(bad))

    def test_empty_patch_diagnosis_exposes_sample_catalog_missing_proven_alias(self):
        from test_strategy_evidence_pool import REPORT, extracted
        template = extracted()
        report = {**copy.deepcopy(REPORT), **copy.deepcopy(template), 'navYuan': 1000, 'coverage': 'top10', 'holdings': [
            {'market': 'CN', 'stockCode': '600001', 'stockName': '合成证券', 'currency': 'CNY',
             'shares': 10, 'marketValueYuan': 100, 'weightPct': 10}]}
        for key in ('strategyClaims', 'strategyClaimsIdentityEvidence', 'strategyEvidencePool', 'strategyEvidenceBudget'):
            report.pop(key, None)
        identity = report['portfolioIdentityEvidence']
        phase = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31', 'funds': [{'code': '000001'}]}
        raw = {'asOf': china_today(), 'retrievedAt': RECEIVED, 'funds': [{'code': '000001', 'name': '合成基金',
               'companyId': 'co1', 'company': '合成公司', 'sourcePortfolioCode': identity['sourcePortfolioCode'],
               'shareClassCodes': identity['shareClassCodes'], 'portfolioSourceUrl': identity['sourceUrl'],
               'reports': [report]}], 'barsByStock': {}, 'errors': []}
        node = """const fs=require('fs'),{buildSnapshot}=require('./scripts/build-data'),{mergeSnapshots}=require('./scripts/merge-parts');
const x=JSON.parse(fs.readFileSync(0,'utf8')), snapshot=buildSnapshot(x.raw,{config:x.phase,now:x.now});
const catalog={companies:[{code:'co1',name:'合成公司',fundCount:2}],funds:[
{code:'000001',companyCode:'co1',companyName:'合成公司'},{code:'000002',companyCode:'co1',companyName:'合成公司'}]};
const result=mergeSnapshots({snapshots:[snapshot],catalog,config:x.phase,now:x.now,reusePrices:false});
process.stdout.write(JSON.stringify({envelope:result.companySnapshots.co1,manifest:result.manifest}));"""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        response = subprocess.run(['node', '-e', node], input=json.dumps({'raw': raw, 'phase': phase, 'now': now}),
            text=True, capture_output=True, timeout=30, cwd=ROOT, check=True)
        original = json.loads(response.stdout); envelope = original['envelope']; fund = envelope['funds'][0]
        self.assertEqual(fund['companyId'], 'co1')
        control = verifier.bridge({'mode': 'normalize-only', 'fund': fund, 'envelope': envelope, 'config': phase, 'now': now})
        self.assertFalse(control['nonStrategyFieldsUnchanged'])
        self.assertTrue(verifier.valid_differences(control))
        self.assertEqual({row['path'] for row in control['differences']},
                         {'fund.company', 'fund.companyId', 'fund.companyMapping'})
        self.assertTrue(all(row['beforeType'] == row['afterType'] == 'string' for row in control['differences']))
        self.assertEqual(control['unmappedDifferenceCount'], 0)
        catalog_path = 'web/data/periods/2026Q2/catalog.json'
        original_catalog = {'schemaVersion': 1, 'targetPeriodEnd': '2026-06-30',
            'companies': [{'code': 'co1', 'name': '合成公司'}], 'funds': [
                {'code': '000001', 'companyCode': 'co1', 'companyName': '合成公司'},
                {'code': '000002', 'companyCode': 'co1', 'companyName': '合成公司'}]}
        reader = Reader({catalog_path: original_catalog})
        catalog, evidence = verifier.catalog_context(reader, '2026Q2', fund)
        fixed = verifier.bridge({'mode': 'normalize-only', 'fund': fund, 'envelope': envelope,
                                 'catalog': catalog, 'config': phase, 'now': now})
        self.assertTrue(fixed['nonStrategyFieldsUnchanged']); self.assertEqual(fixed['differences'], [])
        self.assertEqual(evidence['requiredCodeCount'], 2); self.assertEqual(evidence['blobSha'], reader.last_blob)
        calls = []
        sample = {'quarter': '2026Q2', 'phase': phase, 'manifest': original['manifest'], 'envelope': envelope,
                  'fund': fund, 'task': verifier.binding_for(fund, phase, now), 'partsRead': 1,
                  'dataPath': 'web/data/periods/2026Q2/companies/co1.json', 'dataBlobSha': 'b' * 40,
                  'catalog': catalog, 'catalogContext': evidence}
        result = verifier.run_sample(sample, provider_factory=source_factory({REPORT['sourceUrl']: template}, calls))
        self.assertEqual(result['status'], 'verified', result); self.assertEqual(len(calls), 1)
        self.assertEqual(result['protectedBeforeHash'], result['protectedAfterHash'])
        self.assertTrue(all(result['catalogContext']['checks'].values()))
        # A real contradictory directory row must never be repaired by copying
        # the representative's owner to its alias, nor cause a PDF request.
        reader.data[catalog_path]['funds'][1]['companyCode'] = 'other'
        with patch.object(verifier, 'PublicProvider', side_effect=AssertionError('no provider')):
            with self.assertRaisesRegex(verifier.VerificationError, 'CATALOG_CONTEXT_INVALID'):
                verifier.catalog_context(reader, '2026Q2', fund)

    def test_catalog_context_missing_duplicate_wrong_quarter_and_conflicting_fields_fail_before_get(self):
        for problem in ('missing', 'duplicate', 'quarter', 'field-conflict', 'company-field-conflict', 'oversize-name'):
            reader, templates = setup_reader(); calls = []
            catalog = reader.data['web/data/periods/2026Q2/catalog.json']
            if problem == 'missing': catalog['funds'] = []
            if problem == 'duplicate': catalog['funds'] *= 2
            if problem == 'quarter': catalog['targetPeriodEnd'] = '2026-09-30'
            if problem == 'field-conflict': catalog['funds'][0]['companyId'] = 'other'
            if problem == 'company-field-conflict': catalog['companies'][0]['id'] = 'other'
            if problem == 'oversize-name': catalog['companies'][0]['name'] = 'x' * 201
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(verifier.VerificationError, 'CATALOG_CONTEXT_INVALID'):
                    verifier.select_sample(reader, '2026Q2', dt.datetime.now(dt.timezone.utc).isoformat())
                self.assertEqual(calls, [])

    def test_real_two_quarter_collect_build_merge_preserves_original_values_and_clocks(self):
        reader, templates = setup_reader(); before = copy.deepcopy(reader.data); calls = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            value = verifier.verify(reader, output, source_factory(templates, calls))
            self.assertEqual(json.loads(output.read_text()), value)
        self.assertEqual(value['status'], 'verified', value)
        self.assertEqual(len(calls), 2); self.assertEqual(value['originalPdfRequestsStarted'], 2)
        self.assertEqual(reader.data, before); self.assertFalse(value['productionDataModified'])
        for sample in value['samples']:
            self.assertTrue(all(sample['checks'].values()))
            self.assertEqual(sample['protectedBeforeHash'], sample['protectedAfterHash'])
            self.assertEqual(sample['retainedStrategyObservedAt'], RECEIVED)
            self.assertNotEqual(sample['newStrategyObservedAt'], RECEIVED)
        encoded = json.dumps(value, ensure_ascii=False)
        self.assertNotIn(BODY, encoded); self.assertNotIn('holdings', encoded)
        self.assertLess(len(encoded.encode()), verifier.MAX_OUTPUT_BYTES)

    def test_all_unknown_scan_can_verify_without_claiming_full_semantics(self):
        reader, templates = setup_reader(body='本季度市场运行平稳。'); calls = []
        with tempfile.TemporaryDirectory() as directory:
            value = verifier.verify(reader, Path(directory) / 'result.json', source_factory(templates, calls))
        self.assertEqual(value['status'], 'verified', value)
        self.assertFalse(value['semanticAnalysisComplete'])
        self.assertTrue(all(sample['classifiedClauseCount'] == 0 for sample in value['samples']))
        self.assertTrue(all(sample['scannedClauseCount'] == sample['unknownClauseCount'] for sample in value['samples']))

    def test_existing_position_mismatch_cannot_be_hidden_by_successful_analysis_admission(self):
        reader, templates = setup_reader(); calls = []
        sample = verifier.select_sample(reader, '2026Q2', dt.datetime.now(dt.timezone.utc).isoformat())
        sample['fund']['selectedReport']['holdings'][0]['shares'] += 1
        result = verifier.run_sample(sample, provider_factory=source_factory(templates, calls))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['checks']['positionsUnchanged'])
        self.assertTrue(result['checks']['patchAdmitted'])

    def test_other_original_metadata_change_is_also_not_reported_as_preserved(self):
        reader, templates = setup_reader(); calls = []
        sample = verifier.select_sample(reader, '2026Q2', dt.datetime.now(dt.timezone.utc).isoformat())
        # Even a corrective normalization cannot be labelled byte-for-byte
        # preservation of the original non-strategy field.
        sample['fund']['inceptionDate'] = '2020-02-31'
        result = verifier.run_sample(sample, provider_factory=source_factory(templates, calls))
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['checks']['nonStrategyFieldsUnchanged'])
        self.assertTrue(result['checks']['patchAdmitted'])

    def test_bridge_extra_text_is_rejected_and_never_copied_into_artifact(self):
        reader, templates = setup_reader(); calls = []
        sample = verifier.select_sample(reader, '2026Q2', dt.datetime.now(dt.timezone.utc).isoformat())
        original = verifier.bridge
        def extra(value):
            result = original(value); result['fullText'] = BODY * 20; return result
        with patch.object(verifier, 'bridge', side_effect=extra):
            result = verifier.run_sample(sample, provider_factory=source_factory(templates, calls))
        self.assertEqual(result['errorCode'], 'BRIDGE_FAILED')
        self.assertNotIn(BODY, json.dumps(result, ensure_ascii=False))

    def test_interrupted_request_has_compact_started_checkpoint_and_no_second_get(self):
        reader, templates = setup_reader(); calls = []
        factory = source_factory(templates, calls)
        def interrupted(self, url, **kwargs): calls.append((url, kwargs)); raise KeyboardInterrupt()
        factory.get = interrupted
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            with self.assertRaises(KeyboardInterrupt): verifier.verify(reader, output, factory)
            value = json.loads(output.read_text())
        self.assertEqual(len(calls), 1); self.assertEqual(value['originalPdfRequestsStarted'], 1)
        self.assertEqual(value['samples'][0]['receipt']['outcome'], 'started')
        self.assertEqual(value['status'], 'partial')

    def test_source_change_or_failure_is_partial_and_retains_real_receipt_without_retry(self):
        for changed in (False, True):
            reader, templates = setup_reader(); calls = []
            with tempfile.TemporaryDirectory() as directory:
                value = verifier.verify(reader, Path(directory) / 'result.json', source_factory(templates, calls, changed=changed, failure=not changed))
            self.assertEqual(value['status'], 'partial'); self.assertEqual(len(calls), 2)
            for sample in value['samples']:
                self.assertEqual(sample['receipt']['outcome'], 'failed')
                self.assertEqual(sample['receipt']['errorCode'], 'STRATEGY_CLAIMS_SOURCE_CHANGED' if changed else 'TIMEOUT')
                self.assertFalse(sample['checks']['patchAdmitted'])
                self.assertTrue(sample['checks']['oldTimesUnchanged'])

    def test_already_scanned_and_missing_samples_do_not_make_any_request(self):
        reader, templates = setup_reader(scanned=True); calls = []
        with tempfile.TemporaryDirectory() as directory:
            value = verifier.verify(reader, Path(directory) / 'result.json', source_factory(templates, calls))
        self.assertEqual(value['status'], 'partial'); self.assertEqual(calls, [])
        self.assertEqual(value['originalPdfRequestsStarted'], 0)
        self.assertTrue(all(sample['errorCode'] == 'SAMPLE_NOT_FOUND_WITHIN_BOUND' for sample in value['samples']))

    def test_selection_path_traversal_and_bounded_part_search_reject_before_requests(self):
        reader, _ = setup_reader(scanned=True)
        manifest = reader.data['web/data/periods/2026Q2/latest.json']
        manifest['companyFiles'][0]['path'] = './data/companies/../private.json'
        with self.assertRaises(verifier.VerificationError): verifier.select_sample(reader, '2026Q2', dt.datetime.now(dt.timezone.utc).isoformat())
        reader, _ = setup_reader(scanned=True)
        manifest = reader.data['web/data/periods/2026Q2/latest.json']
        envelope = reader.data['web/data/periods/2026Q2/companies/co1.json']
        manifest['companyFiles'] = [{'path': f'./data/companies/co{i}.json'} for i in range(20)]
        for i in range(20): reader.data[f'web/data/periods/2026Q2/companies/co{i}.json'] = envelope
        with self.assertRaisesRegex(verifier.VerificationError, 'SAMPLE_NOT_FOUND_WITHIN_BOUND'):
            verifier.select_sample(reader, '2026Q2', dt.datetime.now(dt.timezone.utc).isoformat())
        self.assertEqual(len(reader.reads), verifier.MAX_PARTS + 1)

    def test_frozen_git_reader_checks_exact_commit_regular_blob_and_byte_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'web/data/periods/2026Q2/latest.json'
            path.parent.mkdir(parents=True); path.write_text('{"small":true}')
            def git(*args): return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True).stdout.strip()
            git('init', '-q'); git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'Synthetic data')
            sha = git('rev-parse', 'HEAD'); reader = verifier.GitDataReader(root, sha)
            self.assertEqual(reader.read('web/data/periods/2026Q2/latest.json'), {'small': True})
            self.assertTrue(reader.unchanged())
            with self.assertRaisesRegex(verifier.VerificationError, 'DATA_READ_BOUND'):
                reader.read('web/data/periods/2026Q2/latest.json', maximum=1)
            with self.assertRaises(verifier.VerificationError): verifier.GitDataReader(root, '0' * 40)
            path.write_text('{"changed":true}')
            self.assertFalse(reader.unchanged())

    def test_actions_gate_precedes_reading_data_or_credentials(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(sys, 'argv', ['verify', '--data-repo', '.', '--data-commit', COMMIT]), \
             patch.object(verifier, 'GitDataReader', side_effect=AssertionError('must not inspect data')):
            with self.assertRaisesRegex(verifier.VerificationError, 'ACTIONS_REQUIRED'): verifier.main()


if __name__ == '__main__': unittest.main()
