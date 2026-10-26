"""Tiny retained reports and request receipts; no network or real directory."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).parents[1] / 'scripts'), str(Path(__file__).parent)]
import traversal
import campaign
import test_traversal as traversal_fixtures
from test_traversal import event
from test_strategy_evidence_pool import extracted
from strategy_claim_patches import binding_for

NOW = '2026-10-27T02:00:00Z'
PLANNED = '2026-10-27T01:00:00Z'
STARTED = '2026-10-27T01:10:00.123456+00:00'
PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
CONFIG = {**PHASE, 'requireReportQuality': True, 'strategyClaimsQuota': 20}
CATALOG = {'companies': [{'code': 'co1', 'name': '合成公司', 'fundCount': 1, 'directoryStatus': 'complete'}],
           'funds': [{'code': '000001', 'companyCode': 'co1'}]}


def fund(scanned=False, body=None):
    value = traversal_fixtures.TraversalTests().retained_fund()
    value['asOf'] = '2026-10-27'
    value['selectedReport']['strategyEvidenceRetrievedAt'] = '2026-09-29T10:00:00Z'
    if scanned:
        result = extracted(**({'body': body} if body else {}))
        for key in (*traversal.ClaimsStateValidator.FIELDS, 'strategyThemes'):
            if key in result: value['selectedReport'][key] = result[key]
    return value


def progress(value=None, catalog=CATALOG):
    return traversal.merge_progress(catalog, None, CONFIG, [event()], NOW, retained=[value or fund()])


def planned(previous=None, value=None):
    result = traversal.plan_batches(CATALOG, previous or progress(value), CONFIG,
        shards=1, batch_size=1, now=PLANNED, retained=[value or fund()])
    return next(iter(result['batches'].values()), None)


def receipt(task, at=STARTED, outcome='started'):
    return {key: task[key] for key in ('schemaVersion', 'code', 'targetPeriodEnd', 'taskDigest', 'analysisDigest')} | {
        'sourceUrl': task['source']['sourceUrl'], 'attemptedAt': at, 'outcome': outcome,
        'errorCode': 'STRATEGY_CLAIMS_INTERRUPTED' if outcome == 'unknown' else None}


def status(batch, at=STARTED, outcome='started', deferred=False, started=True):
    task = batch['funds'][0]['strategyClaimsOnly']
    snapshot = {'schemaVersion': 1, 'sourceMode': 'actions', **PHASE, 'retrievedAt': NOW, 'funds': [],
                'strategyClaimsReceipts': [receipt(task, at, outcome)] if started else [],
                'batchResults': [{'code': '000001', 'status': 'deferred-budget', 'attempted': False,
                                  'errorCode': 'BATCH_DEADLINE_REACHED'}] if deferred else []}
    return traversal.build_status(batch, snapshot, NOW)


class StrategyClaimQueueTests(unittest.TestCase):
    def test_complete_legacy_enters_independent_lane_without_other_budgets(self):
        old = progress(); batch = planned(old)
        self.assertEqual(old['research']['completeFunds'], 1)
        self.assertTrue(old['continueRun'])
        self.assertEqual(old['completionAudit']['work']['strategyClaimsTasks'], 1)
        self.assertEqual(batch['traversal']['strategyClaimsCodes'], ['000001'])
        for key in ('qualityCodes', 'managerCodes', 'strategyCodes', 'enrichmentCodes', 'reportLocatorCodes'):
            self.assertEqual(batch['traversal'][key], [])
        row = status(batch)
        self.assertTrue(traversal.valid_status(row, batch, NOW))
        new = traversal.merge_progress(CATALOG, old, CONFIG, [row], NOW, retained=[fund()])
        self.assertEqual(new['strategyClaims']['attempts'], 1)
        for key in ('status', 'attempts', 'totalAttempts', 'lastAttemptAt', 'quality', 'enrichment', 'reportLocatorAttempts'):
            self.assertEqual(new['funds'][0][key], old['funds'][0][key], key)

    def test_started_unknown_replay_and_out_of_order_each_count_once(self):
        old = progress(); batch = planned(old)
        first = status(batch); second = status(batch, '2026-10-27T01:20:00Z', 'unknown')
        merged = traversal.merge_progress(CATALOG, old, CONFIG, [second, first, first, second], NOW, retained=[fund()])
        self.assertEqual(merged['strategyClaims']['attempts'], 2)
        self.assertEqual(merged['strategyClaims']['exhaustedFunds'], 1)
        self.assertFalse(merged['continueRun'])
        replay = traversal.merge_progress(CATALOG, merged, CONFIG, [first, second], NOW, retained=[fund()])
        self.assertEqual(replay['funds'][0]['strategyClaimsAttempts'], merged['funds'][0]['strategyClaimsAttempts'])
        self.assertIsNone(planned(replay))

    def test_deferred_or_no_receipt_consumes_zero_and_reconcile_never_relabels(self):
        old = progress(); batch = planned(old)
        for deferred in (False, True):
            rows = status(batch, deferred=deferred, started=False)
            self.assertTrue(traversal.valid_status(rows, batch, NOW))
            merger = {'schemaVersion': 1, 'phase': PHASE, 'generatedAt': NOW, 'funds': []}
            reconciled = traversal.reconcile_observations([rows], merger, CONFIG, NOW)
            self.assertEqual(reconciled, [rows])
            merged = traversal.merge_progress(CATALOG, old, CONFIG, reconciled, NOW, retained=[fund()])
            self.assertEqual(merged['strategyClaims']['attempts'], 0)
            self.assertTrue(merged['continueRun'])

    def test_binding_tampering_wrong_quarter_and_missing_source_do_not_start(self):
        batch = planned()
        bad = copy.deepcopy(batch); bad['funds'][0]['strategyClaimsOnly']['source']['sourceUrl'] = 'https://example.org/other.pdf'
        row = status(bad)
        self.assertFalse(row['funds'][0]['attempted'])
        self.assertEqual(row['funds'][0]['errorCode'], 'STRATEGY_CLAIMS_BINDING_INVALID')
        wrong = copy.deepcopy(status(batch)); wrong['funds'][0]['strategyClaimsReceipt']['targetPeriodEnd'] = '2026-09-30'
        self.assertFalse(traversal.valid_status(wrong, batch, NOW))
        wrong = copy.deepcopy(status(batch)); wrong['funds'][0]['strategyClaimsReceipt']['attemptedAt'] = '2026-10-27T00:59:00Z'
        self.assertFalse(traversal.valid_status(wrong, batch, NOW))
        no_source = fund(); no_source['selectedReport'].pop('strategyEvidenceRetrievedAt')
        self.assertEqual(progress(no_source)['strategyClaims']['unavailableFunds'], 1)
        self.assertIsNone(planned(progress(no_source), no_source))

    def test_observation_asof_and_collector_changes_do_not_reset_digest_history(self):
        old = progress(); batch = planned(old)
        exhausted = traversal.merge_progress(CATALOG, old, CONFIG,
            [status(batch), status(batch, '2026-10-27T01:20:00Z', 'unknown')], NOW, retained=[fund()])
        changed = fund(); changed['selectedReport']['strategyEvidenceRetrievedAt'] = '2026-10-27T00:00:00Z'
        self.assertEqual(binding_for(changed, PHASE, NOW)['taskDigest'], batch['funds'][0]['strategyClaimsOnly']['taskDigest'])
        result = traversal.merge_progress(CATALOG, exhausted, {**CONFIG, 'collectorVersion': 99, 'asOf': '2026-10-27'}, [], NOW, retained=[changed])
        self.assertEqual(result['strategyClaims']['attempts'], 2)
        self.assertEqual(result['strategyClaims']['exhaustedFunds'], 1)
        changed['selectedReport']['strategySectionCoverage']['sourceTextHash'] = 'b' * 64
        refreshed = traversal.merge_progress(CATALOG, result, CONFIG, [], NOW, retained=[changed])
        self.assertEqual(refreshed['strategyClaims']['pendingFunds'], 1)
        self.assertEqual(refreshed['strategyClaims']['attempts'], 2)

    def test_strict_hydration_scanned_all_unknown_and_invalid_flags(self):
        for value in (fund(True), fund(True, '市场表现尚待观察。')):
            valid = progress(value)
            self.assertEqual(valid['strategyClaims']['scannedFunds'], 1)
            self.assertFalse(valid['strategyClaims']['semanticAnalysisComplete'])
            self.assertIsNone(planned(valid, value))
        invalid = fund(True); invalid['selectedReport']['strategyClaims']['coverage']['scannedClauseCount'] += 1
        self.assertEqual(progress(invalid)['strategyClaims']['pendingFunds'], 1)
        future_publication = fund(True); future_publication['asOf'] = '2026-07-20'
        self.assertEqual(progress(future_publication)['strategyClaims']['scannedFunds'], 0)

    def test_mutually_exclusive_lanes_and_small_reservation_protect_first_pass(self):
        catalog = copy.deepcopy(CATALOG)
        catalog['companies'][0]['fundCount'] = 41
        catalog['funds'] += [{'code': f'{code:06d}', 'companyCode': 'co1'} for code in range(2, 42)]
        before = progress(catalog=catalog)
        plan = traversal.plan_batches(catalog, before, CONFIG, shards=2, batch_size=10, now=PLANNED, retained=[fund()])
        self.assertEqual(plan['meta']['strategyClaimsCount'], 1)
        self.assertEqual(plan['meta']['freshCount'], 19)
        self.assertEqual(plan['meta']['selectedCount'], 20)
        batch = planned()
        batch['traversal']['qualityCodes'] = ['000001']
        self.assertFalse(traversal.valid_status(status(batch), batch, NOW))

    def test_corrupt_history_blocks_instead_of_rearming(self):
        old = progress(); old['funds'][0]['strategyClaimsAttempts'] = {'bad': {'attempts': 1}}
        result = traversal.merge_progress(CATALOG, old, CONFIG, [], NOW, retained=[fund()])
        self.assertTrue(result['funds'][0]['strategyClaimsHistoryBlocked'])
        self.assertEqual(result['strategyClaims']['exhaustedFunds'], 1)
        self.assertIsNone(planned(result))

    def test_sixteen_source_histories_are_retained_and_seventeenth_is_blocked(self):
        old = progress(); histories = {}
        for index in range(16):
            value = fund(); value['selectedReport']['strategySectionCoverage']['sourceTextHash'] = f'{index:064x}'
            task = binding_for(value, PHASE, NOW)
            received = receipt(task)
            histories[task['taskDigest']] = {'attempts': 1, 'receipts': [received], 'lastAttemptAt': STARTED}
        old['funds'][0]['strategyClaimsAttempts'] = histories
        seventeenth = fund(); seventeenth['selectedReport']['strategySectionCoverage']['sourceTextHash'] = 'c' * 64
        blocked = traversal.merge_progress(CATALOG, old, CONFIG, [], NOW, retained=[seventeenth])
        self.assertEqual(blocked['funds'][0]['strategyClaimsAttempts'], histories)
        self.assertEqual(blocked['strategyClaims']['exhaustedFunds'], 1)
        prior = fund(); prior['selectedReport']['strategySectionCoverage']['sourceTextHash'] = f'{0:064x}'
        reopened = traversal.merge_progress(CATALOG, blocked, CONFIG, [], NOW, retained=[prior])
        self.assertEqual(reopened['strategyClaims']['pendingFunds'], 1)
        self.assertEqual(reopened['strategyClaims']['attempts'], 16)
        oversized = copy.deepcopy(old)
        oversized['funds'][0]['strategyClaimsAttempts']['f' * 64] = next(iter(histories.values()))
        with self.assertRaises(traversal.TraversalError):
            traversal.merge_progress(CATALOG, oversized, CONFIG, [], NOW, retained=[seventeenth])

    def test_existing_scans_use_one_validator_process_and_no_original_fields_change(self):
        value = fund(True); before = copy.deepcopy(value)
        now = traversal.utc_now(NOW)
        _, _, states = traversal.base_states(CATALOG, progress(), PHASE, now, CONFIG)
        original = traversal.subprocess.Popen
        with patch.object(traversal.subprocess, 'Popen', wraps=original) as popen:
            traversal.hydrate_quality(states, [value, value], PHASE, now)
            self.assertEqual(popen.call_count, 1)
        self.assertEqual(value, before)
        self.assertEqual(states['000001']['strategyClaimsStatus'], 'scanned')

    def test_report_local_aliases_share_one_task_without_copying_receipts(self):
        catalog = copy.deepcopy(CATALOG); catalog['companies'][0]['fundCount'] = 2
        catalog['funds'].append({'code': '000002', 'companyCode': 'co1'})
        value = fund(True)
        value['selectedReport']['portfolioIdentityEvidence'] = copy.deepcopy(value['selectedReport']['strategyClaimsIdentityEvidence'])
        for key in ('strategyClaims', 'strategyClaimsIdentityEvidence', 'strategyEvidencePool', 'strategyEvidenceBudget'):
            value['selectedReport'].pop(key)
        old = traversal.merge_progress(catalog, None, CONFIG, [event(), event('000002')], NOW, retained=[value])
        plan = traversal.plan_batches(catalog, old, CONFIG, shards=1, batch_size=10, now=PLANNED, retained=[value])
        self.assertEqual(plan['meta']['strategyClaimsCount'], 1)
        self.assertEqual(old['strategyClaims']['pendingFunds'], 2)
        self.assertEqual(old['strategyClaims']['readyFunds'], 1)
        batch = next(iter(plan['batches'].values()))
        self.assertEqual(batch['traversal']['strategyClaimsCodes'], ['000001'])
        merged = traversal.merge_progress(catalog, old, CONFIG, [status(batch)], NOW, retained=[value])
        self.assertEqual(merged['strategyClaims']['attempts'], 1)
        self.assertEqual(merged['funds'][1]['strategyClaimsAttempts'], {})
        self.assertEqual(merged['funds'][1]['strategyClaimsAttribution'], 'quarter-report-identity')

    def test_alias_wrong_url_or_interim_identity_does_not_expand(self):
        catalog = copy.deepcopy(CATALOG); catalog['companies'][0]['fundCount'] = 2
        catalog['funds'].append({'code': '000002', 'companyCode': 'co1'})
        for field, mismatch in [('kind', 'semiannual'), ('sourceUrl', 'https://example.org/interim.pdf')]:
            value = fund(True)
            value['selectedReport']['strategyClaimsIdentityEvidence'][field] = mismatch
            value['shareClassCodes'] = ['000001', '000002']; value['sourcePortfolioCode'] = '000001'
            result = traversal.merge_progress(catalog, None, CONFIG, [event(), event('000002')], NOW, retained=[value])
            self.assertEqual(result['funds'][1]['strategyClaimsStatus'], 'unavailable')

    def test_unknown_finite_scan_is_shared_only_with_quarter_identity(self):
        catalog = copy.deepcopy(CATALOG); catalog['companies'][0]['fundCount'] = 2
        catalog['funds'].append({'code': '000002', 'companyCode': 'co1'})
        value = fund(True, '市场表现尚待观察。')
        result = traversal.merge_progress(catalog, None, CONFIG, [event(), event('000002')], NOW, retained=[value])
        self.assertEqual(result['strategyClaims']['scannedFunds'], 2)
        self.assertFalse(result['strategyClaims']['semanticAnalysisComplete'])
        self.assertEqual(result['strategyClaims']['readyFunds'], 0)
        self.assertEqual(result['funds'][1]['strategyClaimsAttribution'], 'quarter-report-identity')

    def test_changed_share_representative_cannot_rearm_same_source_budget(self):
        catalog = copy.deepcopy(CATALOG); catalog['companies'][0]['fundCount'] = 2
        catalog['funds'].append({'code': '000002', 'companyCode': 'co1'})
        value = fund(True); value['code'] = '000002'; value['selectedReport']['fundCode'] = '000002'
        value['selectedReport']['portfolioIdentityEvidence'] = copy.deepcopy(value['selectedReport']['strategyClaimsIdentityEvidence'])
        for key in ('strategyClaims', 'strategyClaimsIdentityEvidence', 'strategyEvidencePool', 'strategyEvidenceBudget'):
            value['selectedReport'].pop(key)
        previous = traversal.merge_progress(catalog, None, CONFIG, [event('000002')], NOW, retained=[value])
        config = {**CONFIG, 'funds': [{'code': '000002', 'companyCode': 'co1',
                  'strategyClaimsOnly': binding_for(value, PHASE, NOW)}],
                  'traversal': {'plannedAt': PLANNED, 'strategyClaimsCodes': ['000002']}}
        used = traversal.merge_progress(catalog, previous, CONFIG,
            [status(config), status(config, '2026-10-27T01:20:00Z', 'unknown')], NOW, retained=[value])
        self.assertEqual(used['funds'][1]['strategyClaimsGroupAttempts'], 2)
        original_ledger = copy.deepcopy(used['funds'][1]['strategyClaimsAttempts'])
        # A becomes a valid source observation and now wins canonical selection.
        changed = traversal.merge_progress(catalog, used, CONFIG, [event('000001')], NOW, retained=[value])
        self.assertEqual(changed['funds'][0]['strategyClaimsRepresentativeCode'], '000001')
        self.assertEqual(changed['funds'][0]['strategyClaimsStatus'], 'exhausted')
        self.assertEqual(changed['funds'][0]['strategyClaimsGroupAttempts'], 2)
        self.assertEqual(changed['funds'][0]['strategyClaimsAttempts'], {})
        self.assertEqual(changed['funds'][1]['strategyClaimsAttempts'], original_ledger)
        self.assertEqual(changed['strategyClaims']['attempts'], 2)
        plan = traversal.plan_batches(catalog, changed, CONFIG, shards=1, batch_size=10, now=NOW, retained=[value])
        self.assertEqual(plan['meta']['strategyClaimsCount'], 0)
        self.assertEqual(changed['strategyClaims']['exhaustedFunds'], 2)

    def test_alias_rotation_cannot_expand_sixteen_source_history_limit(self):
        catalog = copy.deepcopy(CATALOG); catalog['companies'][0]['fundCount'] = 2
        catalog['funds'].append({'code': '000002', 'companyCode': 'co1'})
        value = fund(True)
        value['selectedReport']['portfolioIdentityEvidence'] = copy.deepcopy(value['selectedReport']['strategyClaimsIdentityEvidence'])
        for key in ('strategyClaims', 'strategyClaimsIdentityEvidence', 'strategyEvidencePool', 'strategyEvidenceBudget'):
            value['selectedReport'].pop(key)
        previous = traversal.merge_progress(catalog, None, CONFIG, [event(), event('000002')], NOW, retained=[value])
        for index in range(16):
            version = copy.deepcopy(value); version['selectedReport']['strategySectionCoverage']['sourceTextHash'] = f'{index:064x}'
            code = '000001' if index % 2 == 0 else '000002'
            task = binding_for(version, PHASE, NOW, code=code)
            previous['funds'][index % 2]['strategyClaimsAttempts'][task['taskDigest']] = {
                'attempts': 1, 'receipts': [receipt(task)], 'lastAttemptAt': STARTED}
        ledgers = [copy.deepcopy(row['strategyClaimsAttempts']) for row in previous['funds']]
        result = traversal.merge_progress(catalog, previous, CONFIG, [], NOW, retained=[value])
        self.assertEqual(result['strategyClaims']['exhaustedFunds'], 2)
        self.assertEqual(result['strategyClaims']['attempts'], 16)
        self.assertEqual([row['strategyClaimsAttempts'] for row in result['funds']], ledgers)
        self.assertEqual([row['strategyClaimsGroupDigestCount'] for row in result['funds']], [16, 16])

    def test_quarter_quota_is_frozen_and_legacy_plan_remains_recoverable(self):
        self.assertEqual(campaign.quarter_config({}, '2026Q2', '2026-10-27')['strategyClaimsQuota'], 20)
        for value in (-1, 21, True, '20'):
            with self.assertRaises(ValueError): campaign.quarter_config({'strategyClaimsQuota': value}, '2026Q3', '2026-10-27')
        batch = planned()
        selection = {'quarters': ['2026Q2'], 'strategyClaimsQuotas': {'2026Q2': 20}}
        matrix = [{'config': 'batch-0.json', 'quarter': '2026Q2'}]
        def read(path): return {'strategyClaimsQuota': 20} if 'campaign' in str(path) else batch
        with patch.object(campaign, 'read_json', side_effect=read):
            campaign.frozen_claims_quotas_for_publish(selection, matrix)
            with self.assertRaises(ValueError): campaign.frozen_claims_quotas_for_publish({'quarters': ['2026Q2']}, matrix)
            batch['strategyClaimsQuota'] = 19
            with self.assertRaises(ValueError): campaign.frozen_claims_quotas_for_publish(selection, matrix)


if __name__ == '__main__': unittest.main()
