"""Synthetic source binding and real finite-proof normalization; no requests."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path[:0] = [str(Path(__file__).parents[1] / 'scripts'), str(Path(__file__).parent)]
from test_strategy_evidence_pool import extracted, REPORT, NOW
from strategy_claim_patches import (METHOD, StrategyClaimPatchError, binding_for, digest,
                                    normalize_task, normalize_receipt, report_for)

PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
PLANNED = '2026-10-27T00:00:00.123456Z'
ATTEMPTED = '2026-10-27T00:30:00.123456Z'
FETCHED = '2026-10-27T01:00:00.123456Z'


def fund(body=None):
    return {'code': '000001', 'selectedReport': {**REPORT, **extracted(**({'body': body} if body else {}))}}


def task(code='000001'):
    return binding_for(fund(), PHASE, NOW, code=code)


def observed_details(at=FETCHED, body=None):
    # The extractor runs at the real test time. Rebind only the synthetic
    # observation clock for deterministic later-instant admission tests.
    value = extracted(**({'body': body} if body else {}))
    value['strategyEvidenceRetrievedAt'] = at
    for field in ('strategyEvidencePool', 'strategyClaims'):
        value[field]['source']['retrievedAt'] = at
    for field in ('strategyClaimsIdentityEvidence', 'portfolioIdentityEvidence'):
        value[field]['retrievedAt'] = at
    return value


def receipt(value=None, outcome='started', error=None):
    value = value or task()
    return {'schemaVersion': 1, **{key: value[key] for key in ('code', 'targetPeriodEnd', 'taskDigest', 'analysisDigest')},
            'sourceUrl': value['source']['sourceUrl'], 'attemptedAt': ATTEMPTED, 'outcome': outcome, 'errorCode': error}


class StrategyClaimPatchTests(unittest.TestCase):
    def test_stable_method_digest_uses_original_quarter_source_and_no_observation_clock(self):
        value = task()
        self.assertEqual(value['analysisDigest'], fund()['selectedReport']['strategyClaims']['source']['digest'])
        newer = {**value, 'retainedStrategyObservedAt': FETCHED}
        self.assertEqual(normalize_task(newer, now=NOW)['taskDigest'], value['taskDigest'])
        bad = copy.deepcopy(value); bad['source']['sectionContentHash'] = '0' * 64
        self.assertIsNone(normalize_task(bad, now=NOW))
        self.assertIsNone(binding_for(fund(), {'targetPeriodEnd': '2026-09-30'}, NOW))

    def test_legacy_source_can_plan_but_alias_requires_quarter_pdf_identity(self):
        value = fund()
        for key in ('strategyClaims', 'strategyEvidencePool', 'strategyEvidenceBudget', 'strategyClaimsIdentityEvidence'):
            value['selectedReport'].pop(key, None)
        self.assertIsNotNone(binding_for(value, PHASE, NOW))
        self.assertIsNotNone(binding_for(value, PHASE, NOW, code='000002'))
        self.assertIsNone(binding_for(value, PHASE, NOW, code='000003'))
        value['selectedReport']['portfolioIdentityEvidence']['sourceUrl'] = 'https://example.org/interim.pdf'
        self.assertIsNone(binding_for(value, PHASE, NOW, code='000002'))

    def test_complete_source_single_contiguous_section_and_safe_exact_url(self):
        for url in ('http://example.org/a.pdf', 'https://example.org/a.pdf#', 'https://example.org/a.pdf?apikey=x',
                    'https://example.local/a.pdf', 'https://example.org:8443/a.pdf', 'https://%65xample.org/a.pdf',
                    'https://example.org/\ud800.pdf', 'https://user@example.test/a.pdf'):
            value = task(); value['source']['sourceUrl'] = url
            self.assertIsNone(normalize_task(value, now=NOW))
        for pages in ([4, 6], [True], [], [10001]):
            value = task(); value['source']['pages'] = pages
            self.assertIsNone(normalize_task(value, now=NOW))
        value = task(); value['code'] = '０００００１'
        self.assertIsNone(normalize_task(value, now=NOW))
        value = task(); value['targetPeriodEnd'] = '9999-12-31'
        self.assertIsNone(normalize_task(value, now=NOW))
        value = task(); value['retainedStrategyObservedAt'] = '0001-01-01T00:00:00+23:00'
        self.assertIsNone(normalize_task(value, now=NOW))

    def test_receipt_microseconds_and_outcome_require_actual_request(self):
        value = task()
        for outcome, error in [('started', None), ('parsed', None), ('failed', 'TIMEOUT'), ('unknown', 'STRATEGY_CLAIMS_INTERRUPTED')]:
            self.assertIsNotNone(normalize_receipt(receipt(value, outcome, error), value, PLANNED, NOW, NOW))
        for changes in ({'attemptedAt': '2026-10-27T00:00:00.123455Z'}, {'attemptedAt': '2026-10-27T02:00:00.000001Z'},
                        {'attemptedAt': '2026-10-27T01:00:00+00:60'}, {'code': '000002'},
                        {'outcome': 'failed', 'errorCode': 'STRATEGY_CLAIMS_BUDGET_REACHED'},
                        {'outcome': 'unknown', 'errorCode': 'TIMEOUT'}, {'outcome': 'parsed', 'errorCode': 'TIMEOUT'}):
            self.assertIsNone(normalize_receipt({**receipt(value), **changes}, value, PLANNED, NOW, NOW))

    def test_retrieved_new_candidate_is_only_strategy_and_preserves_original_time(self):
        details = observed_details()
        value = report_for(details, task(), ATTEMPTED, NOW, NOW)
        self.assertEqual(value['strategyEvidenceRetrievedAt'], FETCHED)
        self.assertEqual(value['strategyClaimsIdentityEvidence']['retrievedAt'], FETCHED)
        self.assertNotIn('navYuan', value); self.assertNotIn('portfolioCode', value)
        self.assertNotIn('strategyClaimsDerivedFromObservedAt', value)
        self.assertFalse(value['strategyClaims']['semanticAnalysisComplete'])

    def test_mismatched_source_identity_clock_or_budget_does_not_make_patch(self):
        cases = [lambda x: x['strategySectionCoverage']['sections'][0].update(contentHash='0' * 64),
                 lambda x: x['strategyClaimsIdentityEvidence'].update(sourceUrl='https://example.org/other.pdf'),
                 lambda x: x['strategyClaimsIdentityEvidence'].update(retrievedAt=ATTEMPTED),
                 lambda x: x.update(strategyEvidenceRetrievedAt=REPORT['retrievedAt']),
                 lambda x: x['narrative'].update(text='新增未计入引用的正文'),
                 lambda x: x['strategyClaims'].update(semanticAnalysisComplete=True)]
        for mutate in cases:
            details = observed_details(); mutate(details)
            with self.assertRaises(StrategyClaimPatchError): report_for(details, task(), ATTEMPTED, NOW, NOW)

    def test_all_unknown_is_full_finite_scan_without_semantic_completion(self):
        body = '本季度市场运行平稳。'
        binding = binding_for(fund(body), PHASE, NOW)
        details = observed_details(body=body)
        value = report_for(details, binding, ATTEMPTED, NOW, NOW)
        self.assertTrue(value['strategyClaims']['ruleScanComplete'])
        self.assertFalse(value['strategyClaims']['semanticAnalysisComplete'])
        self.assertEqual(value['strategyClaims']['coverage']['classifiedClauseCount'], 0)

    def test_python_task_and_candidate_pass_actual_js_admission(self):
        binding = task(); value = report_for(observed_details(), binding, ATTEMPTED, NOW, NOW)
        patch_value = {'schemaVersion': 1, 'code': binding['code'], 'binding': binding,
                       'receipt': receipt(binding, 'parsed'), 'report': value}
        script = """const fs=require('fs');const p=require('./lib/strategy-claim-patches');
const x=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify({task:p.normalizeTask(x.task,x.phase,x.task.code,x.now),
patch:!!p.normalizePatch(x.patch,x.task,x.phase,x.now,x.now,x.planned)}));"""
        result = subprocess.run(['node', '-e', script], cwd=Path(__file__).parents[1],
            input=json.dumps({'task': binding, 'phase': PHASE, 'now': NOW, 'planned': PLANNED, 'patch': patch_value}),
            capture_output=True, text=True, timeout=30, check=True)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed['task'], binding); self.assertTrue(parsed['patch'])


if __name__ == '__main__': unittest.main()
