"""Synthetic metadata/cache/PDF identity integration. No network calls."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import report_locators as locators
from collect import collect_fund, collect, write_checkpoint
from providers import DataError
from test_cninfo_discovery import FakeSource, announcement
from test_portfolio_identity import proof, PAGES

NOW = '2026-10-26T12:00:00Z'
PERIOD = '2026-06-30'


def result(quarter='2026Q2', aid='1001', codes='000001,000002'):
    day = '2026-07-21' if quarter == '2026Q2' else '2026-10-22'
    title = f'合成基金2026年第{quarter[-1]}季度报告'
    fetched = locators.discover_period(quarter, as_of='2026-10-26', observed_at=NOW,
        window_end=day, page_size=30, fetch_page=FakeSource([announcement(aid, codes, day, title)],
            edit=lambda _p, r: {**r, 'retrievedAt': NOW}))
    fetched['paginationVerification'] = {'status': 'verified-pagination', 'pageSize': 30,
        'observedAt': NOW, 'requiredProfiles': ['q2-empty-30', 'q3-empty-30']}
    return fetched


def candidate(aid='1001'):
    return locators.candidates_from_result(result(aid=aid), now=NOW)[0]


def locator(code='000001', candidates=None):
    return locators.locator_for(code, PERIOD, candidates or [candidate()])


class LocatorTests(unittest.TestCase):
    def test_real_discovery_parser_records_become_candidates_without_universe_or_pdf_claim(self):
        value = candidate(); normalized = locators.normalize_locator(locator(), '000001', PERIOD, NOW)
        self.assertEqual(normalized['candidates'], [value])
        self.assertFalse(value['pdfVerified'])
        self.assertIsNone(locators.normalize_locator(locator(), '000003', PERIOD, NOW))
        self.assertIsNone(locators.normalize_locator(locator(), '000001', '2026-09-30', NOW))
        changed = copy.deepcopy(result()); changed['announcements'][0]['sourcePage']['contentHash'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'INVALID_LOCATOR_DISCOVERY'):
            locators.candidates_from_result(changed, now=NOW)
        changed = result(); changed['announcements'][0]['sourceUrl'] = changed['announcements'][0]['sourceUrl'][:-4] + '.pdf'
        self.assertTrue(locators.candidates_from_result(changed, now=NOW)[0]['sourceUrl'].endswith('.PDF'))

    def test_stable_digest_ignores_retrieval_page_hash_order_but_changes_for_real_new_candidate(self):
        a, b = candidate(), candidate('1002'); initial = locator(candidates=[a,b])
        changed = copy.deepcopy(a); changed['retrievedAt'] = '2026-10-26T12:00:01Z'; changed['sourcePage']['contentHash'] = 'f' * 64
        self.assertEqual(locators.candidate_id(changed), a['candidateId'])
        self.assertEqual(locator(candidates=[b,changed])['digest'], initial['digest'])
        self.assertNotEqual(locator(candidates=[b])['digest'], initial['digest'])
        for field, value in [('sourceUrl', 'https://example.org/private.pdf'), ('retrievedAt', '2026-10-27T12:00:00Z'),
                             ('title', '合成基金2026年第2季度报告摘要'), ('retrievedAt', '2026-10-26T12:00:00+00:60')]:
            invalid = copy.deepcopy(a); invalid[field] = value
            self.assertIsNone(locators.normalize_candidate(invalid, PERIOD, now=NOW))

    def test_cache_empty_or_failed_refresh_keeps_original_candidates_and_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = locators.refresh_registries(root, ['2026Q2'], '2026-10-26', now=NOW,
                fetcher=lambda *args, **kwargs: {'2026Q2': result()})['2026Q2']
            file = root / 'periods/2026Q2/report-locators.json'; file.parent.mkdir(parents=True)
            file.write_bytes(locators.canonical(registry))
            def fail(*args, **kwargs): raise locators.DiscoveryError('NETWORK_ERROR')
            failed = locators.refresh_registries(root, ['2026Q2'], '2026-10-27', now='2026-10-27T12:00:00Z', fetcher=fail)['2026Q2']
            self.assertEqual(failed['records'], registry['records'])
            self.assertEqual(failed['records'][0]['retrievedAt'], NOW)
            self.assertEqual(failed['refreshStatus'], 'cached')
            empty = result(); empty['announcements'] = []
            refreshed = locators.refresh_registries(root, ['2026Q2'], '2026-10-27', now='2026-10-27T12:00:00Z',
                fetcher=lambda *args, **kwargs: {'2026Q2': empty})['2026Q2']
            self.assertEqual(refreshed['records'], registry['records'])
            self.assertFalse(refreshed['historicalUniverseVerified'])
            mapping = locators.registry_locators(registry, ['000001'])
            self.assertEqual(set(mapping), {'000001'})  # Unknown C-class remains only a candidate.
            binding = locators.freeze_registry(registry, '2026Q2', root / 'frozen', now=NOW)
            frozen, saved = locators.validate_registry_lock(binding, '2026Q2', root / 'frozen', now=NOW)
            self.assertEqual(saved['records'], registry['records'])
            frozen.write_text('{}')
            with self.assertRaises(ValueError): locators.validate_registry_lock(binding, '2026Q2', root / 'frozen', now=NOW)


class Provider:
    def __init__(self, mode='valid', reports=None): self.mode, self.reports, self.requests = mode, reports, []
    def overview(self, code): return {'name': '合成基金', 'company': '合成公司', 'managers': []}
    def announcements(self, code, as_of):
        if self.reports is not None: return self.reports
        raise DataError('NETWORK_ERROR')
    def holdings(self, code, year): return {PERIOD: [{'stockCode': '600001', 'stockName': '合成股票', 'market': 'CN', 'currency': 'CNY', 'shares': 100, 'weightPct': 5, 'marketValueYuan': 500}]}
    def report_details(self, report):
        self.requests.append(report['sourceUrl'])
        if self.mode == 'error': raise DataError('PDF_PARSE_FAILED')
        value = {'navYuan': 10000, 'narrative': None, 'parseMethod': 'pypdf', 'sourceTextHash': 'a' * 64,
            'pageStats': {'charCount': 500, 'nonEmptyPages': 3, 'totalPages': 3, 'processedPages': 3, 'truncated': False, 'textStatus': 'usable'}}
        if self.mode == 'missing' or self.mode == 'first-invalid' and len(self.requests) == 1: return value
        pages = PAGES if self.mode != 'main-only' else [PAGES[0], '§2 基金产品概况\n基金主代码 000001\n', PAGES[2]]
        value['portfolioIdentityEvidence'] = proof(pages, {**report, 'fundCode': '000001'})
        if self.mode == 'wrong-period': value['portfolioIdentityEvidence']['periodEnd'] = '2026-09-30'
        return value


class LocatorCollectTests(unittest.TestCase):
    def collect(self, provider, code='000001', value=None):
        return collect_fund({'code': code, 'reportLocator': value or locator(code), 'reportLocatorOnly': True},
            '2026-10-26', provider=provider, target_period=PERIOD, baseline_period='2026-03-31')

    def test_em_failure_can_use_identity_verified_pdf_and_keeps_both_original_receipts(self):
        provider = Provider(); fund, errors = self.collect(provider)
        self.assertEqual(len(fund['reports']), 1)
        report = fund['reports'][0]
        self.assertEqual(report['sourceUrl'], candidate()['sourceUrl'])
        self.assertEqual(report['sourceLocatorEvidence']['retrievedAt'], NOW)
        self.assertEqual(report['portfolioIdentityEvidence']['retrievedAt'], '2026-07-21T10:00:00+00:00')
        self.assertEqual(report['evidenceRetrievedAt'], report['portfolioIdentityEvidence']['retrievedAt'])
        self.assertFalse(report['portfolioIdentityEvidence']['shareClassUniverseComplete'])
        self.assertTrue(any(e['errorCode'] == 'NETWORK_ERROR' for e in errors))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(fund['reportLocatorReceipt']['attemptedCandidateIds'], [candidate()['candidateId']])

    def test_missing_wrong_identity_or_pdf_failure_cannot_borrow_em_holdings(self):
        for mode, code in [('missing','000001'), ('error','000001'), ('main-only','000002'), ('wrong-period','000001')]:
            fund, _ = self.collect(Provider(mode), code)
            self.assertEqual(fund['reports'], [], mode)
            self.assertEqual(len(fund['reportLocatorReceipt']['attemptedCandidateIds']), 1)

    def test_first_invalid_candidate_allows_second_without_repeating_pdf_download(self):
        provider = Provider('first-invalid'); value = locator(candidates=[candidate('1002'), candidate('1001')])
        fund, _ = self.collect(provider, value=value)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(fund['reports']), 1)
        self.assertEqual(fund['reports'][0]['sourceUrl'], candidate('1001')['sourceUrl'])
        self.assertEqual(len(fund['reportLocatorReceipt']['attemptedCandidateIds']), 2)

    def test_interim_identity_is_not_replaced_by_its_separate_quarterly_source(self):
        interim = {'title': '合成基金2026年中期报告', 'periodEnd': PERIOD, 'kind': 'semiannual',
                   'publishedAt': '2026-08-30', 'sourceUrl': 'https://example.org/interim.pdf'}
        provider = Provider(reports=[interim])
        fund, _ = collect_fund({'code':'000001', 'reportLocator':locator()}, '2026-10-26', provider=provider,
            target_period=PERIOD, baseline_period='2026-03-31')
        self.assertEqual(len(fund['reports']), 2)
        current = next(r for r in fund['reports'] if r['kind'] == 'semiannual')
        quarter = next(r for r in fund['reports'] if r['kind'] == 'quarterly')
        self.assertNotIn('sourceLocatorEvidence', current)
        self.assertNotEqual(current.get('portfolioIdentityEvidence'), quarter['portfolioIdentityEvidence'])
        self.assertEqual(quarter['sourceLocatorEvidence']['sourceUrl'], quarter['sourceUrl'])

    def test_parallel_request_entry_receipts_are_durable_before_pdf_returns_and_never_duplicate_funds(self):
        barrier = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'input.json'; snapshots = []
            def checkpoint(payload):
                write_checkpoint(file, payload)
                saved = json.loads(file.read_text()); snapshots.append(saved)
                self.assertEqual(len({v['code'] for v in saved['funds']}), len(saved['funds']))
            owner = self
            class ConcurrentProvider(Provider):
                def report_details(self, report):
                    # Both workers have entered the PDF call but neither has
                    # returned a report. The last atomic file already records
                    # both attempts, including the one that will fail.
                    barrier.wait(timeout=3)
                    saved = json.loads(file.read_text())
                    pending = {v['code']:v for v in saved['funds']}
                    owner.assertEqual(set(pending), {'000001','000002'})
                    owner.assertTrue(all(v.get('reportLocatorReceipt') for v in pending.values()))
                    if report['fundCode'] == '000002': raise DataError('PDF_PARSE_FAILED')
                    return super().report_details(report)
            config = {'funds':[{'code':code,'reportLocator':locator(code),'reportLocatorOnly':True} for code in ('000001','000002')],
                'targetPeriodEnd':PERIOD,'baselinePeriodEnd':'2026-03-31','maxPriceSymbols':0,'maxWorkers':2}
            with patch('collect.PublicProvider', side_effect=ConcurrentProvider):
                completed = collect(config, '2026-10-26', checkpoint)
            rows = {v['code']:v for v in completed['funds']}
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows['000001']['reports']), 1)
            self.assertEqual(rows['000002']['reports'], [])
            self.assertTrue(all(len(v['reportLocatorReceipt']['attemptedCandidateIds']) == 1 for v in rows.values()))
            self.assertTrue(any(len(s['funds']) == 2 and all(not f['reports'] for f in s['funds']) for s in snapshots))
            self.assertEqual(rows['000001']['reports'][0]['portfolioIdentityEvidence']['retrievedAt'], '2026-07-21T10:00:00+00:00')


if __name__ == '__main__': unittest.main()
