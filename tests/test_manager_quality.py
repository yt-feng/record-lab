"""Report-manager attribution and bounded migration of old six-field progress."""
import copy
import unittest
import test_traversal as support
from test_traversal import traversal, catalog, CONFIG, PHASE, NOW, event


def retained():
    return support.TraversalTests().retained_fund()


def previous(quality, at='2026-09-29T10:00:00Z'):
    return {'schemaVersion': 1, 'phase': PHASE, 'collectorVersion': 4, 'funds': [
        {'code': '000001', 'companyCode': 'co1', 'status': 'complete', 'attempts': 1,
         'lastAttemptAt': at, 'quality': {'priceCoverageVersion': 2, 'strategySectionCoverageVersion': traversal.STRATEGY_SECTION_COVERAGE_VERSION, **quality}}]}


class ManagerQualityTests(unittest.TestCase):
    def test_only_page_cited_report_managers_qualify(self):
        fund = retained()
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))['fields']['managers'], 'complete')
        earlier = copy.deepcopy(fund)
        earlier['reportManagers'][0]['startDate'] = '1998-01-01'
        earlier['reportManagers'][0]['evidence']['text'] = '张甲 基金经理 1998.1.1 -'
        self.assertTrue(traversal.valid_manager_rows(earlier, earlier['selectedReport'], PHASE))
        self.assertFalse(traversal.valid_report_managers(earlier, earlier['selectedReport'], PHASE))
        mutations = [lambda x: x.update(managerAttribution='current-profile'),
                     lambda x: x.update(managerAttribution='tenure-overlap'),
                     lambda x: x['reportManagers'][0]['evidence'].update(page=0),
                     lambda x: x['reportManagers'][0]['evidence'].update(pages=[3, 5]),
                     lambda x: x['reportManagers'][0]['evidence'].update(sourceUrl='https://example.org/unrelated'),
                     lambda x: x['reportManagers'][0]['evidence'].update(periodEnd='2026-03-31'),
                     lambda x: x['reportManagers'][0]['evidence'].update(text='张甲 基金经理 未提供日期'),
                     lambda x: x['reportManagers'][0].update(role='基金经理助理'),
                     lambda x: x['reportManagers'][0].update(startDate='2026-07-01'),
                     lambda x: x['reportManagers'][0].update(endDate='2026-03-31')]
        for change in mutations:
            with self.subTest(change=change):
                invalid = copy.deepcopy(fund); change(invalid)
                self.assertNotEqual(traversal.report_quality(invalid, PHASE, traversal.utc_now(NOW))['fields']['managers'], 'complete')

    def test_manager_who_departed_during_quarter_is_retained_but_prequarter_departure_is_not(self):
        fund = retained()
        manager = fund['reportManagers'][0]
        manager.update(endDate='2026-05-20', periodStart='2026-01-01')
        manager['evidence']['text'] = '张甲 基金经理 2020年1月1日 2026年5月20日'
        self.assertTrue(traversal.valid_manager_rows(fund, fund['selectedReport'], PHASE))
        fund['selectedReport']['managers'] = [copy.deepcopy(manager)]
        departed = copy.deepcopy(manager)
        departed['name'], departed['endDate'] = '李乙', '2026-03-31'
        departed['evidence']['text'] = '李乙 基金经理 2020年1月1日 2026年3月31日'
        fund['selectedReport']['managers'].append(departed)
        del fund['reportManagers']
        self.assertTrue(traversal.valid_manager_rows(fund, fund['selectedReport'], PHASE))

    def test_legacy_six_field_complete_and_exhausted_states_get_independent_manager_budget(self):
        for attempts in (0, 3):
            prior = {'fields': {key: 'complete' for key in traversal.SIX_QUALITY_FIELDS}, 'attempts': attempts,
                     'totalAttempts': attempts, 'status': 'complete', 'priceCoverageVersion': 2, 'strategySectionCoverageVersion': traversal.STRATEGY_SECTION_COVERAGE_VERSION}
            normalized = traversal.normalize_quality(prior, 'complete', traversal.utc_now(NOW))
            self.assertEqual(normalized['missingFields'], ['managers'])
            self.assertEqual(normalized['status'], 'pending')
            self.assertEqual(normalized['attempts'], attempts)
            self.assertEqual(normalized['managerAttempts'], 0)
            config = {**CONFIG, 'collectorVersion': 4, 'requireReportQuality': True}
            progress = traversal.merge_progress(catalog(), previous(prior), config, now=NOW)
            self.assertEqual(progress['research']['sixFieldsCompleteFunds'], 1)
            self.assertEqual(progress['research']['completeFunds'], 0)
            plan = traversal.plan_batches(catalog(), progress, config, shards=1, batch_size=3, now=NOW)
            batch = plan['batches']['batch-0.json']
            self.assertIn('000001', batch['traversal']['managerCodes'])
            self.assertIn('000001', batch['traversal']['qualityCodes'])

    def test_manager_only_backfill_is_bounded_and_does_not_reopen_old_quality_budget(self):
        config = {**CONFIG, 'collectorVersion': 4, 'requireReportQuality': True}
        fields = {key: 'complete' for key in traversal.SIX_QUALITY_FIELDS}
        progress = previous({'fields': fields, 'attempts': 3, 'totalAttempts': 9})
        for index in range(3):
            stamp = f'2026-09-29T1{index + 1}:00:00Z'
            plan = traversal.plan_batches(catalog(), progress, config, shards=1, batch_size=3, now=stamp)
            batch = plan['batches']['batch-0.json']
            status = traversal.build_status(batch, None, stamp)
            self.assertTrue(traversal.valid_status(status, batch, stamp))
            progress = traversal.merge_progress(catalog(), progress, config, [status], stamp)
            state = next(row for row in progress['funds'] if row['code'] == '000001')
            self.assertEqual(state['status'], 'complete')
            self.assertEqual(state['quality']['attempts'], 3)
            self.assertEqual(state['quality']['managerAttempts'], index + 1)
            self.assertEqual(state['quality']['managerTotalAttempts'], index + 1)
            replay = traversal.merge_progress(catalog(), progress, config, [status], stamp)
            self.assertEqual(replay['funds'][0]['quality'], state['quality'])
        self.assertEqual(state['quality']['status'], 'exhausted')
        self.assertNotIn('000001', traversal.quality_eligible({row['code']: row for row in progress['funds']}))

    def test_repaired_manager_field_does_not_clear_unrelated_exhausted_nav_gap(self):
        config = {**CONFIG, 'collectorVersion': 4, 'requireReportQuality': True}
        fields = {key: 'complete' for key in traversal.SIX_QUALITY_FIELDS}; fields['nav'] = 'missing'
        progress = previous({'fields': fields, 'attempts': 3, 'totalAttempts': 3})
        attempt = event(at='2026-09-29T11:00:00Z')
        attempt['funds'][0].update(qualityAttempt=True, managerAttempt=True,
                                  managerTableCoverageVersion=traversal.MANAGER_TABLE_COVERAGE_VERSION,
                                  reportQuality={'fields': {**fields, 'managers': 'complete'}, 'managerTableCoverageVersion': traversal.MANAGER_TABLE_COVERAGE_VERSION, 'priceCoverageVersion': 2})
        progress = traversal.merge_progress(catalog(), progress, config, [attempt], NOW)
        quality = progress['funds'][0]['quality']
        self.assertEqual(quality['missingFields'], ['nav'])
        self.assertEqual(quality['status'], 'exhausted')
        self.assertEqual(quality['attempts'], 3)
        self.assertEqual(quality['managerAttempts'], 1)

    def test_inflight_legacy_event_remains_valid_but_cannot_consume_new_manager_budget(self):
        fields = {key: 'complete' for key in traversal.SIX_QUALITY_FIELDS}
        config = {**CONFIG, 'collectorVersion': 4, 'requireReportQuality': True,
                  'funds': [{'code': '000001', 'companyCode': 'co1'}], 'traversal': {'qualityCodes': ['000001']}}
        attempt = event(at='2026-09-29T11:00:00Z')
        attempt['funds'][0].update(qualityAttempt=True, reportQuality={'fields': fields})
        self.assertTrue(traversal.valid_status(attempt, config, NOW))
        progress = traversal.merge_progress(catalog(), previous({'fields': fields, 'attempts': 1}), config, [attempt], NOW)
        self.assertEqual(progress['funds'][0]['quality']['managerAttempts'], 0)
        self.assertEqual(progress['funds'][0]['quality']['fields']['managers'], 'missing')

    def test_legacy_observation_cannot_erase_already_verified_report_manager(self):
        fields = {key: 'complete' for key in traversal.SIX_QUALITY_FIELDS}
        config = {**CONFIG, 'collectorVersion': 4, 'requireReportQuality': True}
        attempt = event(at='2026-09-29T11:00:00Z')
        attempt['funds'][0].update(qualityAttempt=True, reportQuality={'fields': fields})
        progress = traversal.merge_progress(catalog(), previous({'fields': {**fields, 'managers': 'complete'}, 'attempts': 1, 'managerTableCoverageVersion': traversal.MANAGER_TABLE_COVERAGE_VERSION}),
                                            config, [attempt], NOW)
        self.assertEqual(progress['funds'][0]['quality']['fields']['managers'], 'complete')
        self.assertEqual(progress['research']['completeFunds'], 1)
        self.assertEqual(progress['funds'][0]['quality']['managerAttempts'], 0)

    def test_new_parser_version_reopens_only_manager_budget_and_legacy_receipt_cannot_reconsume_it(self):
        fields = {key: 'complete' for key in traversal.QUALITY_FIELDS}
        prior = {'fields': fields, 'attempts': 3, 'totalAttempts': 8, 'managerAttempts': 3, 'managerTotalAttempts': 6,
                 'managerTableCoverageVersion': 1, 'priceCoverageVersion': 2, 'strategySectionCoverageVersion': traversal.STRATEGY_SECTION_COVERAGE_VERSION,
                 'strategyAttempts': 2, 'strategyTotalAttempts': 4}
        normalized = traversal.normalize_quality(prior, 'complete', traversal.utc_now(NOW))
        self.assertEqual(normalized['fields']['managers'], 'partial')
        self.assertEqual(normalized['managerAttempts'], 0)
        self.assertEqual(normalized['managerTotalAttempts'], 6)
        self.assertEqual(normalized['attempts'], 3)
        self.assertEqual(normalized['strategyAttempts'], 2)
        self.assertEqual(normalized['missingFields'], ['managers'])
        attempt = event(at='2026-09-29T11:00:00Z')
        attempt['funds'][0].update(qualityAttempt=True, managerAttempt=True, managerTableCoverageVersion=1,
            reportQuality={'fields': fields, 'managerTableCoverageVersion': 1, 'priceCoverageVersion': 2, 'strategySectionCoverageVersion': traversal.STRATEGY_SECTION_COVERAGE_VERSION})
        config = {**CONFIG, 'collectorVersion': 4, 'requireReportQuality': True}
        progress = traversal.merge_progress(catalog(), previous(prior), config, [attempt], NOW)
        quality = progress['funds'][0]['quality']
        self.assertEqual(quality['managerAttempts'], 0)
        self.assertEqual(quality['managerTotalAttempts'], 6)
        self.assertNotEqual(quality['fields']['managers'], 'complete')
        self.assertEqual(quality['attempts'], 3)


if __name__ == '__main__':
    unittest.main()
