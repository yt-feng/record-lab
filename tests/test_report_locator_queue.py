"""Synthetic locator metadata and receipts; no network or real PDF parsing."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import traversal
from report_locators import ENDPOINT, candidate_id, locator_for, normalize_locator

NOW = '2026-10-26T12:00:00Z'
PLANNED = '2026-10-26T10:30:00Z'
PHASE = {'targetPeriodEnd': '2026-09-30', 'baselinePeriodEnd': '2026-06-30'}
CATALOG = {'companies': [{'code': 'co1', 'name': '合成公司', 'fundCount': 1, 'directoryStatus': 'complete'}],
           'funds': [{'code': '000001', 'companyCode': 'co1', 'name': '合成基金'}]}


def locator(aid='1001', code='000001'):
    value = {'announcementId': aid, 'codes': [code], 'title': '合成证券投资基金2026年第3季度报告',
             'label': '合成基金', 'publishedAt': '2026-10-21', 'sourceUrl': f'https://static.cninfo.com.cn/finalpage/2026-10-21/{aid}.PDF',
             'targetPeriod': '2026Q3', 'correctionLabel': False, 'pdfVerified': False, 'retrievedAt': '2026-10-26T10:00:00Z',
             'discoverySourceUrl': ENDPOINT,
             'sourcePage': {'start': '2026-10-21', 'end': '2026-10-21', 'page': 1, 'contentHash': 'a' * 64, 'splitProbe': False}}
    value['candidateId'] = candidate_id(value)
    result = locator_for(code, PHASE['targetPeriodEnd'], [value])
    assert normalize_locator(result, code, PHASE['targetPeriodEnd'], NOW) == result
    return result


def config(value=None):
    return {**PHASE, 'requireReportQuality': True, 'reportLocatorCandidates': {'000001': value or locator()}}


def exhausted(cfg):
    events = [{'schemaVersion': 1, 'phase': PHASE, 'attemptedAt': f'2026-10-26T0{hour}:00:00Z', 'portfolioAliases': [],
               'funds': [{'code': '000001', 'companyCode': 'co1', 'status': 'failed', 'errorCode': 'TARGET_REPORT_UNAVAILABLE'}]}
              for hour in (8, 9)]
    result = traversal.merge_progress(CATALOG, None, cfg, events, NOW)
    state = result['funds'][0]
    assert state['attempts'] == 2
    state['quality'].update({'attempts': 3, 'totalAttempts': 3, 'managerAttempts': 2, 'managerTotalAttempts': 2,
                             'strategyAttempts': 2, 'strategyTotalAttempts': 2, 'lastAttemptAt': '2026-10-26T09:00:00Z'})
    state['enrichment'].update({'attempts': 2, 'totalAttempts': 2, 'lastAttemptAt': '2026-10-26T09:00:00Z'})
    return result


def batch(cfg, previous=None):
    at = NOW if previous and any(item.get('reportLocatorAttempts') for item in previous['funds']) else PLANNED
    plan = traversal.plan_batches(CATALOG, previous, cfg, shards=1, batch_size=1, now=at)
    return plan, next(iter(plan['batches'].values()), None)


def status_for(planned, started='2026-10-26T11:00:00Z', *, success=False, deferred=False, no_receipt=False):
    value = planned['funds'][0]['reportLocator']
    receipt = {key: value[key] for key in ('schemaVersion', 'code', 'targetPeriodEnd', 'digest')}
    receipt.update({'attemptedAt': started, 'attemptedCandidateIds': [value['candidates'][0]['candidateId']]})
    row = {'code': '000001', 'status': 'complete' if success else 'deferred-budget' if deferred else 'failed',
           'errorCode': None if success else 'BATCH_DEADLINE_REACHED' if deferred else 'PDF_PARSE_FAILED'}
    if deferred: row['attempted'] = False
    snapshot = {'schemaVersion': 1, 'sourceMode': 'actions', 'targetPeriodEnd': PHASE['targetPeriodEnd'],
                'funds': [], 'errors': [], 'batchResults': [row], 'reportLocatorReceipts': [] if no_receipt else [receipt]}
    if success:
        report = {'fundCode': '000001', 'periodEnd': PHASE['targetPeriodEnd'], 'kind': 'quarterly', 'coverage': 'top10',
                  'publishedAt': '2026-10-21', 'sourceUrl': value['candidates'][0]['sourceUrl'],
                  'holdings': [{'market': 'CN', 'stockCode': '600001', 'currency': 'CNY', 'shares': 10, 'weightPct': 1}]}
        snapshot['funds'] = [{'code': '000001', 'status': 'ok', 'dataStatus': 'fresh', 'retrievedAt': '2026-10-26T11:10:00Z',
                              'selectedReport': report, 'rows': [{**report['holdings'][0], 'current': report['holdings'][0], 'priceRange': {}}]}]
    return traversal.build_status(planned, snapshot, NOW)


class ReportLocatorQueueTests(unittest.TestCase):
    def test_valid_locator_reopens_exhausted_source_without_resetting_existing_budgets(self):
        cfg = config(); previous = exhausted(cfg)
        plan, planned = batch(cfg, previous)
        self.assertEqual(plan['meta']['reportLocatorCount'], 1)
        self.assertEqual(plan['meta']['retryCount'], 0)
        self.assertNotIn('reportLocatorCandidates', planned)
        self.assertEqual(planned['funds'][0]['reportLocator'], cfg['reportLocatorCandidates']['000001'])
        self.assertTrue(planned['funds'][0]['reportLocatorOnly'])
        self.assertEqual(planned['traversal']['reportLocatorCodes'], ['000001'])
        for key in ('qualityCodes', 'managerCodes', 'strategyCodes', 'enrichmentCodes'):
            self.assertEqual(planned['traversal'][key], [])
        event = status_for(planned)
        self.assertTrue(traversal.valid_status(event, planned, NOW))
        after = traversal.merge_progress(CATALOG, previous, cfg, [event], NOW)
        old, new = previous['funds'][0], after['funds'][0]
        for key in ('attempts', 'totalAttempts', 'lastAttemptAt', 'status', 'quality', 'enrichment'):
            self.assertEqual(new[key], old[key], key)
        self.assertEqual(after['reportLocators']['attempts'], 1)
        self.assertEqual(new['reportLocatorAttempts'][planned['funds'][0]['reportLocator']['digest']]['attempts'], 1)

    def test_replay_two_attempt_limit_and_digest_switch_do_not_rearm_old_candidates(self):
        cfg = config(); original = exhausted(cfg); _, planned = batch(cfg, original)
        first, second = status_for(planned), status_for(planned, '2026-10-26T11:20:00Z')
        done = traversal.merge_progress(CATALOG, original, cfg, [first, first, second, second], NOW)
        self.assertEqual(done['reportLocators']['attempts'], 2)
        self.assertFalse(batch(cfg, done)[0]['meta']['hasWork'])
        replay = traversal.merge_progress(CATALOG, done, cfg, [first, second], NOW)
        self.assertEqual(replay['reportLocators']['attempts'], 2)
        other = config(locator('1002')); self.assertTrue(batch(other, done)[0]['meta']['hasWork'])
        carried = traversal.merge_progress(CATALOG, done, other, [], NOW)
        self.assertFalse(batch(cfg, carried)[0]['meta']['hasWork'])
        refreshed = copy.deepcopy(cfg); refreshed['reportLocatorCandidates']['000001']['candidates'][0]['retrievedAt'] = '2026-10-26T10:20:00Z'
        self.assertFalse(batch(refreshed, done)[0]['meta']['hasWork'])

    def test_unstarted_and_capacity_deferred_do_not_consume_any_attempt(self):
        cfg = config(); previous = exhausted(cfg); _, planned = batch(cfg, previous)
        for deferred in (True, False):
            event = status_for(planned, no_receipt=True, deferred=deferred)
            self.assertTrue(traversal.valid_status(event, planned, NOW))
            result = traversal.merge_progress(CATALOG, previous, cfg, [event], NOW)
            self.assertEqual(result['reportLocators']['attempts'], 0)
            self.assertEqual(result['funds'][0]['totalAttempts'], previous['funds'][0]['totalAttempts'])
            self.assertTrue(batch(cfg, result)[0]['meta']['hasWork'])
            empty_receipt = {'schemaVersion': 1, 'phase': PHASE, 'generatedAt': NOW, 'funds': []}
            reconciled = traversal.reconcile_observations([event], empty_receipt, cfg, NOW)
            self.assertFalse(reconciled[0]['funds'][0]['attempted'])
            recovered = traversal.merge_progress(CATALOG, previous, cfg, reconciled, NOW)
            self.assertEqual(recovered['reportLocators']['attempts'], 0)

    def test_success_requires_matching_real_report_and_only_then_creates_observation(self):
        cfg = config(); previous = exhausted(cfg); _, planned = batch(cfg, previous)
        event = status_for(planned, success=True)
        self.assertTrue(traversal.valid_status(event, planned, NOW))
        result = traversal.merge_progress(CATALOG, previous, cfg, [event], NOW)
        state = result['funds'][0]
        self.assertEqual(state['status'], 'complete'); self.assertEqual(state['attempts'], 2)
        self.assertEqual(state['totalAttempts'], previous['funds'][0]['totalAttempts'])
        self.assertEqual(state['lastAttemptAt'], '2026-10-26T11:10:00Z')
        self.assertEqual(state['quality']['attempts'], 3)
        self.assertEqual(state['quality']['managerAttempts'], 2)
        self.assertEqual(state['quality']['strategyAttempts'], 2)
        self.assertEqual(state['enrichment']['attempts'], 2)
        wrong = copy.deepcopy(event); wrong['funds'][0]['reportEvidence']['sourceUrl'] = 'https://example.org/unrelated.pdf'
        self.assertFalse(traversal.valid_status(wrong, planned, NOW))
        self.assertEqual(traversal.merge_progress(CATALOG, previous, cfg, [wrong], NOW)['funds'][0]['status'], 'failed')
        no_receipt = status_for(planned, success=True, no_receipt=True)
        self.assertEqual(no_receipt['funds'][0]['status'], 'failed')
        self.assertNotIn('reportEvidence', no_receipt['funds'][0])

    def test_invalid_period_code_candidate_and_attempt_window_are_rejected(self):
        cfg = config(); previous = exhausted(cfg)
        for mutate in (lambda value: value.update(targetPeriodEnd='2026-06-30'), lambda value: value.update(code='000002'),
                       lambda value: value['candidates'][0].update(sourceUrl='https://example.org/unknown.pdf')):
            changed = copy.deepcopy(cfg); mutate(changed['reportLocatorCandidates']['000001'])
            self.assertFalse(batch(changed, previous)[0]['meta']['hasWork'])
        _, planned = batch(cfg, previous)
        early = status_for(planned, '2026-10-26T10:01:00Z')
        self.assertFalse(early['funds'][0]['attempted'])
        forged = status_for(planned); forged['funds'][0]['reportLocatorReceipt']['attemptedCandidateIds'] = ['0' * 64]
        self.assertFalse(traversal.valid_status(forged, planned, NOW))
        self.assertEqual(traversal.merge_progress(CATALOG, previous, cfg, [forged], NOW)['reportLocators']['attempts'], 0)
        future = status_for(planned); future['funds'][0]['reportLocatorReceipt']['attemptedAt'] = '2026-10-26T12:01:00Z'
        self.assertFalse(traversal.valid_status(future, planned, NOW))
        self.assertEqual(traversal.merge_progress(CATALOG, previous, cfg, [future], NOW)['reportLocators']['attempts'], 0)

    def test_late_old_digest_receipt_stays_valid_but_cannot_refresh_already_collected_report(self):
        cfg = config(); previous = exhausted(cfg); _, planned = batch(cfg, previous)
        event = status_for(planned, success=True)
        changed = config(locator('1002'))
        observed = traversal.merge_progress(CATALOG, previous, changed, [event], NOW)
        self.assertEqual(observed['funds'][0]['status'], 'complete')
        later = status_for(planned, '2026-10-26T11:30:00Z')
        after = traversal.merge_progress(CATALOG, observed, changed, [later], NOW)
        for key in ('status', 'lastAttemptAt', 'quality', 'enrichment', 'totalAttempts'):
            self.assertEqual(after['funds'][0][key], observed['funds'][0][key])
        self.assertEqual(after['reportLocators']['attempts'], 2)

    def test_history_bound_keeps_old_digest_limits_instead_of_forgetting_them(self):
        cfg = config(); previous = exhausted(cfg)
        previous['funds'][0]['reportLocatorAttempts'] = {f'{index:064x}': {'attempts': 2, 'lastAttemptAt': '2026-10-26T10:00:00Z', 'errorCode': 'PDF_PARSE_FAILED'} for index in range(16)}
        self.assertFalse(batch(cfg, previous)[0]['meta']['hasWork'])
        next_state = traversal.merge_progress(CATALOG, previous, cfg, [], NOW)['funds'][0]
        self.assertTrue(next_state['reportLocatorHistoryFull']); self.assertEqual(len(next_state['reportLocatorAttempts']), 16)

    def test_fair_queue_reserves_locator_capacity_without_stalling_new_products(self):
        cfg = config(); previous = exhausted(cfg)
        many = copy.deepcopy(CATALOG)
        many['funds'].extend({'code': f'{index:06d}', 'companyCode': 'co1'} for index in range(2, 22))
        many['companies'][0]['fundCount'] = len(many['funds'])
        plan = traversal.plan_batches(many, previous, cfg, shards=1, batch_size=10, now=PLANNED)
        self.assertEqual(plan['meta']['reportLocatorCount'], 1)
        self.assertEqual(plan['meta']['freshCount'], 9)
        self.assertEqual(plan['meta']['selectedCount'], 10)

    def test_optional_fallback_locator_on_ordinary_lane_does_not_spend_independent_budget(self):
        cfg = config(); previous = exhausted(cfg); _, planned = batch(cfg, previous)
        ordinary = copy.deepcopy(planned); ordinary['funds'][0].pop('reportLocatorOnly')
        ordinary['traversal']['reportLocatorCodes'] = []
        event = status_for(ordinary)
        self.assertTrue(traversal.valid_status(event, ordinary, NOW))
        self.assertNotIn('reportLocatorOnly', event['funds'][0])
        result = traversal.merge_progress(CATALOG, previous, cfg, [event], NOW)
        self.assertEqual(result['reportLocators']['attempts'], 0)
        self.assertEqual(result['funds'][0]['totalAttempts'], previous['funds'][0]['totalAttempts'] + 1)

    def test_quarter_scoped_history_and_mixed_lane_flags_are_guarded(self):
        cfg = config(); previous = exhausted(cfg); _, planned = batch(cfg, previous)
        event = status_for(planned)
        bad = copy.deepcopy(event); bad['funds'][0]['qualityAttempt'] = True
        self.assertFalse(traversal.valid_status(bad, planned, NOW))
        self.assertEqual(traversal.merge_progress(CATALOG, previous, cfg, [bad], NOW)['reportLocators']['attempts'], 0)
        different = copy.deepcopy(previous); different['phase'] = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
        different['funds'][0]['reportLocatorAttempts'] = {locator()['digest']: {'attempts': 2, 'lastAttemptAt': '2026-10-26T10:00:00Z'}}
        result = traversal.merge_progress(CATALOG, different, cfg, [], NOW)
        self.assertEqual(result['funds'][0]['reportLocatorAttempts'], {})
        excessive = copy.deepcopy(previous)
        excessive['funds'][0]['reportLocatorAttempts'] = {locator()['digest']: {'attempts': 3, 'lastAttemptAt': '2026-10-26T10:00:00Z'}}
        self.assertFalse(batch(cfg, excessive)[0]['meta']['hasWork'])

    def test_cli_expands_a_locked_registry_without_copying_it_into_each_batch(self):
        from report_locators import empty_registry, freeze_registry
        cfg = config(); previous = exhausted(cfg)
        registry = empty_registry('2026Q3'); registry['records'] = locator()['candidates']
        with tempfile.TemporaryDirectory() as temporary:
            old = Path.cwd()
            try:
                os.chdir(temporary)
                lock = freeze_registry(registry, '2026Q3', now=NOW)
                locked = {**PHASE, 'asOf': '2026-10-26', 'requireReportQuality': True, 'reportLocatorEnabled': True,
                          'reportLocatorRegistry': lock, 'reportLocatorUniverseCodes': ['000001']}
                for filename, value in [('catalog.json', CATALOG), ('config.json', locked), ('progress.json', previous)]:
                    Path(filename).write_text(json.dumps(value))
                with patch('traversal.utc_now', return_value=traversal.instant(NOW)), patch('report_locators.current', return_value=traversal.instant(NOW)), contextlib.redirect_stdout(io.StringIO()):
                    traversal.main(['plan', '--catalog', 'catalog.json', '--config', 'config.json', '--progress', 'progress.json', '--output', 'plan', '--shards', '1', '--batch-size', '1'])
                planned = json.loads(Path('plan/batch-0.json').read_text())
                self.assertTrue(planned['funds'][0]['reportLocatorOnly'])
                for key in ('reportLocatorRegistry', 'reportLocatorUniverseCodes', 'reportLocatorCandidates'):
                    self.assertNotIn(key, planned)
            finally:
                os.chdir(old)


if __name__ == '__main__': unittest.main()
