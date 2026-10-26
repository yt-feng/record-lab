"""Chinese publication dates across UTC midnight; synthetic inputs only."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from source_dates import china_date, china_today
from providers import parse_announcements
import campaign
import collect
import traversal
from portfolio_identity import normalize_portfolio_identity
from historical_inception import normalize_profile_inception, normalize_inception
from historical_universe import SOURCE_ID, SOURCE_URL, SOURCE_AS_OF, SOURCE_HASH, SHEET, observation_time

NOW = traversal.instant('2026-10-26T16:15:00Z')
OBSERVATIONS = ('2026-10-26T16:10:00Z', '2026-10-27T00:10:00+08:00', '2026-10-26T12:10:00-04:00')
PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}


def fund(observed):
    sample = json.loads((ROOT / 'tests/fixtures/strategy-sections.json').read_text())['complete']
    sample['strategySectionCoverage']['publishedAt'] = '2026-10-27'
    return {'code': '000001', 'status': 'ok', 'dataStatus': 'fresh', 'retrievedAt': observed,
            'selectedReport': {**sample, 'periodEnd': PHASE['targetPeriodEnd'], 'publishedAt': '2026-10-27',
                'sourceUrl': 'https://example.org/quarter.pdf', 'strategyEvidenceRetrievedAt': observed,
                'holdings': [{'stockCode': '600001', 'market': 'CN'}]},
            'previousReport': {'periodEnd': PHASE['baselinePeriodEnd'], 'publishedAt': '2026-10-27',
                'sourceUrl': 'https://example.org/prior.pdf', 'holdings': [{'stockCode': '600001', 'market': 'CN'}]},
            'rows': [{'stockCode': '600001', 'market': 'CN'}]}


class SourceDateTests(unittest.TestCase):
    def test_midnight_offsets_and_invalid_instants(self):
        for value in OBSERVATIONS:
            self.assertEqual(china_today(value), '2026-10-27')
        self.assertEqual(china_today('2026-10-26T15:59:59Z'), '2026-10-26')
        self.assertEqual(china_today('2026-10-26T16:00:00Z'), '2026-10-27')
        for value in (None, '', '2026-10-27', '2026-10-27T00:10:00', '2026-02-31T00:00:00Z', dt.datetime(2026, 10, 27)):
            self.assertIsNone(china_date(value))

    def test_announcement_cutoff_uses_china_day_but_explicit_history_and_future_stay_strict(self):
        data = {'Data': [{'FUNDCODE': '000001', 'TITLE': '合成基金2026年第3季度报告',
                         'PUBLISHDATEDESC': '2026-10-27', 'ID': 'AN202610270000001'}]}
        self.assertEqual(len(parse_announcements(data, china_today(NOW), '000001')), 1)
        self.assertEqual(parse_announcements(data, '2026-10-26', '000001'), [])
        data['Data'][0]['PUBLISHDATEDESC'] = '2026-10-28'
        self.assertEqual(parse_announcements(data, china_today(NOW), '000001'), [])

    def test_quality_strategy_baseline_and_receipt_share_instant_semantics_on_replay(self):
        for observed in OBSERVATIONS:
            value = fund(observed)
            for now in (NOW, traversal.instant('2026-10-27T02:00:00Z')):
                quality = traversal.report_quality(value, PHASE, now)
                self.assertEqual(quality['fields']['strategy'], 'complete')
                self.assertEqual(quality['fields']['baseline'], 'complete')
                status, evidence = traversal.report_evidence(value, PHASE, now)
                self.assertEqual(status, 'complete')
                self.assertEqual(evidence['observedAt'], observed)
                self.assertTrue(traversal.valid_event_evidence(evidence, 'complete', PHASE, now))
            self.assertEqual(value['retrievedAt'], observed)
        invalid = fund(OBSERVATIONS[0]); invalid['selectedReport']['publishedAt'] = '2026-10-28'
        self.assertIsNone(traversal.report_quality(invalid, PHASE, NOW))
        self.assertIsNone(traversal.report_evidence(invalid, PHASE, NOW))
        future = fund('2026-10-26T16:21:00Z')
        self.assertIsNone(traversal.report_quality(future, PHASE, NOW))
        self.assertIsNone(traversal.report_evidence(future, PHASE, NOW))

    def test_report_identity_preserves_original_timestamp_and_rejects_future_day(self):
        fixture = json.loads((ROOT / 'tests/fixtures/portfolio-identity.json').read_text())
        fixture['report']['publishedAt'] = fixture['proof']['publishedAt'] = '2026-10-27'
        for observed in OBSERVATIONS:
            fixture['proof']['retrievedAt'] = observed
            result = normalize_portfolio_identity(fixture['proof'], fixture['report'], now=NOW)
            self.assertEqual(result['retrievedAt'], observed)
        fixture['report']['publishedAt'] = fixture['proof']['publishedAt'] = '2026-10-28'
        self.assertIsNone(normalize_portfolio_identity(fixture['proof'], fixture['report'], now=NOW))

    def test_inception_and_historical_snapshot_date_do_not_depend_on_offset_text(self):
        for observed in OBSERVATIONS:
            value = {'schemaVersion': 1, 'fundCode': '000001', 'date': '2026-10-27',
                     'sourceUrl': 'https://fundf10.eastmoney.com/jbgk_000001.html', 'retrievedAt': observed}
            self.assertEqual(normalize_profile_inception(value, '000001', now=NOW), value)
            self.assertIsNone(normalize_profile_inception({**value, 'date': '2026-10-28'}, '000001', now=NOW))
        value = {'schemaVersion': 1, 'fundCode': '000001', 'periodEnd': SOURCE_AS_OF, 'date': '2026-04-01',
                 'sourceId': SOURCE_ID, 'sourceUrl': SOURCE_URL, 'contentHash': SOURCE_HASH,
                 'sheet': SHEET, 'row': 3, 'retrievedAt': '2026-06-29T16:10:00Z'}
        now = traversal.instant('2026-06-29T16:15:00Z')
        self.assertEqual(normalize_inception(value, '000001', SOURCE_AS_OF, now=now), value)
        self.assertEqual(observation_time(value['retrievedAt']), '2026-06-29T16:10:00+00:00')
        self.assertIsNone(normalize_inception({**value, 'retrievedAt': '2026-06-29T15:59:59Z'}, '000001', SOURCE_AS_OF, now=now))

    def test_campaign_and_collector_defaults_use_same_business_day_without_network(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(campaign, 'china_today', return_value=china_today(NOW)), \
                patch.object(sys, 'argv', ['campaign.py', 'plan']), patch.object(campaign, 'plan', return_value={}) as plan, \
                patch('sys.stdout', new_callable=io.StringIO):
            campaign.main()
            self.assertEqual(plan.call_args.args[1], '2026-10-27')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'; config.write_text(json.dumps({'funds': [{'code': '000001'}]}))
            for explicit, expected in (([], '2026-10-27'), (['--as-of', '2026-10-26'], '2026-10-26')):
                with patch.object(collect, 'china_today', return_value=china_today(NOW)), \
                        patch.object(sys, 'argv', ['collect.py', '--config', str(config), *explicit]), \
                        patch.object(collect, 'collect', return_value={'coverage': {'status': 'partial'}, 'errors': []}) as run, \
                        patch('sys.stdout', new_callable=io.StringIO):
                    collect.main()
                    self.assertEqual(run.call_args.args[1], expected)


if __name__ == '__main__':
    unittest.main()
