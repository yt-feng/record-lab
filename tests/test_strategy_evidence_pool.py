"""Small source-bound extraction/collection fixtures, without PDF requests."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from strategy_sections import parse_strategy_sections
from strategy_claims import parse_strategy_claims
from strategy_evidence_pool import (apply_strategy_evidence_budget, normalize_strategy_evidence_pool,
                                    strategy_evidence_budget_for)
import report_extract
from collect import collect_fund

NOW = '2026-10-27T02:00:00Z'
RECEIVED = '2026-10-26T19:00:00.123456+00:00'
REPORT = {'sourceUrl': 'https://example.org/quarter.pdf', 'publishedAt': '2026-07-21', 'kind': 'quarterly',
          'periodEnd': '2026-06-30', 'retrievedAt': RECEIVED, 'title': '合成基金2026年第2季度报告', 'fundCode': '000001'}
START = '合成基金2026年第2季度报告\n4.4 报告期内基金的投资策略和运作分析\n'
END = '\n4.5 报告期内基金的业绩表现\n本期净值表现。'
BODY = '在本报告期我们持续看好人工智能方向。由于估值偏高，本基金减配消费行业。未来本基金将关注科技方向。'


def fixture(body=BODY, *, report=None, pages=None, page_numbers=None):
    report = copy.deepcopy(report or REPORT)
    pages = pages or [START + body + END]
    sections = parse_strategy_sections(pages, report, page_numbers=page_numbers)
    claims = parse_strategy_claims(pages, report, page_numbers=page_numbers, sections_result=sections, now=NOW)
    details = {**copy.deepcopy(dict(sections)), **claims,
               'narrative': {'text': body * 20, 'page': 1}, 'strategyExcerpts': [{'text': body * 5, 'page': 1}]}
    details.update(apply_strategy_evidence_budget(details, pages, report, sections_result=sections,
                                                page_numbers=page_numbers, now=NOW))
    return details


def extracted(report=None, body=BODY):
    report = report or REPORT
    pages = ['合成基金2026年第2季度报告\n报告期2026年4月1日至2026年6月30日',
             '§2 基金产品概况\n基金主代码 000001\n下属分级基金的交易代码\n000001 000002\n',
             '§3 主要财务指标和基金净值表现\n3.1 主要财务指标\n',
             '4.4 报告期内基金的投资策略和运作分析\n' + body + END]
    if report['kind'] == 'semiannual':
        pages[0] = '合成基金2026年中期报告'
        pages[1] = pages[1].replace('§2 基金产品概况', '2.1 基金基本情况')
        pages[2] = '2.2 基金产品说明'
    parser = lambda *_: {'navYuan': 10000, 'narrative': {'text': body * 20, 'page': 4},
                         'portfolioCode': '000001', 'shareClassCodes': ['000001', '000002']}
    with patch.object(report_extract, '_pypdf_pages', return_value=pages):
        return report_extract.extract_report(b'%PDF synthetic source', report['sourceUrl'], report['periodEnd'], report['kind'],
            parse_pages=parser, env={}, published_at=report['publishedAt'], retrieved_at=report['retrievedAt'])


class StrategyEvidencePoolTests(unittest.TestCase):
    def test_complete_source_stays_private_and_all_four_projections_share_one_pool(self):
        value = fixture()
        self.assertEqual(value['strategyEvidenceBudget']['status'], 'verified')
        self.assertTrue(value['strategySectionCoverage']['complete'])
        self.assertTrue(value['strategyClaims']['ruleScanComplete'])
        self.assertFalse(value['strategyClaims']['semanticAnalysisComplete'])
        pool = value['strategyEvidencePool']
        unique = {quote['text'] for quote in pool['quotes']}
        texts = [value['narrative']['text']] + [row['text'] for field in ('strategyExcerpts',) for row in value[field]]
        texts += [row['text'] for key in ('strategyAnalysis', 'strategyClaims') for row in value[key]['evidence']]
        self.assertTrue(set(texts).issubset(unique)); self.assertLessEqual(len(unique), 8)
        self.assertLessEqual(sum(len(text) for text in unique), 1280)
        self.assertEqual(pool['source']['retrievedAt'], RECEIVED)
        self.assertNotIn(BODY * 5, json.dumps(value, ensure_ascii=False))

    def test_union_budget_prioritizes_claims_then_limits_old_analysis_without_changing_full_scan(self):
        body = ''.join(f'本季度市场第{i}阶段回暖。' for i in range(10)) + BODY
        value = fixture(body)
        self.assertEqual(value['strategyEvidenceBudget']['status'], 'verified')
        self.assertLessEqual(value['strategyEvidencePool']['usedQuotes'], 8)
        self.assertEqual(value['strategyClaims']['coverage']['sentenceCount'], value['strategySectionCoverage']['sentenceCount'])
        self.assertEqual(value['strategyAnalysis']['omittedSentenceCount'],
                         value['strategyAnalysis']['sentenceCount'] - len(value['strategyAnalysis']['evidence']))

    def test_emoji_codepoints_and_cross_page_literal_positions_are_preserved(self):
        value = fixture(pages=[START + '本基金增配科技行业😀，', '并保持审慎。' + END], page_numbers=[6, 7])
        self.assertEqual(value['strategyEvidenceBudget']['status'], 'verified')
        quote = value['strategyEvidencePool']['quotes'][0]
        self.assertEqual(quote['pages'], [6, 7]); self.assertIn('😀', quote['text'])
        self.assertEqual(quote['textHash'], hashlib.sha256(quote['text'].encode()).hexdigest())
        self.assertEqual(value['strategyEvidencePool']['usedQuoteChars'], sum(len(item['text']) for item in value['strategyEvidencePool']['quotes']))

    def test_unknown_pages_or_wrong_title_do_not_receive_verified_pool(self):
        for pages, numbers in [([START + BODY + END], [None]), ([START.replace('第2', '第3') + BODY + END], None)]:
            value = fixture(pages=pages, page_numbers=numbers)
            self.assertNotIn('strategyEvidencePool', value)
            self.assertEqual(value['strategyEvidenceBudget']['status'], 'partial')

    def test_hashes_are_distinct_source_roles_and_pool_digest_does_not_refresh_with_observation(self):
        value = fixture(pages=[START + '本基金增配科技行业。', '本季度市场回暖。' + END])
        pool = value['strategyEvidencePool']; proof = value['strategySectionCoverage']
        self.assertNotEqual(pool['source']['sourceTextHash'], hashlib.sha256('\n\n'.join([START + '本基金增配科技行业。', '本季度市场回暖。' + END]).encode()).hexdigest())
        refreshed = copy.deepcopy(value); refreshed['strategyEvidencePool']['source']['retrievedAt'] = NOW
        refreshed['strategyClaims']['source']['retrievedAt'] = NOW
        refreshed['strategyEvidenceRetrievedAt'] = NOW
        self.assertEqual(strategy_evidence_budget_for(refreshed, NOW)['status'], 'verified')
        self.assertEqual(normalize_strategy_evidence_pool(refreshed['strategyEvidencePool'], proof, now=NOW)['digest'], pool['digest'])

    def test_no_silent_legacy_migration_and_tampered_projection_or_provenance_stays_partial(self):
        legacy = {'narrative': {'text': BODY * 30}, 'strategySectionCoverage': fixture()['strategySectionCoverage']}
        before = copy.deepcopy(legacy)
        self.assertEqual(strategy_evidence_budget_for(legacy, NOW)['status'], 'legacy-unbudgeted')
        self.assertEqual(legacy, before)
        mutations = [lambda x: x['narrative'].update(text='虚构原因'),
                     lambda x: x['strategyExcerpts'][0].update(evidencePoolId='q' + '0' * 64),
                     lambda x: x['strategyAnalysis']['evidence'][0].update(sourceUrl='https://example.org/other.pdf'),
                     lambda x: x['strategyEvidencePool']['source'].update(sourceTextHash='0' * 64),
                     lambda x: x.update(strategyEvidenceRetrievedAt='2026-10-26T19:00:01Z')]
        for mutation in mutations:
            value = fixture(); mutation(value)
            self.assertEqual(strategy_evidence_budget_for(value, NOW)['status'], 'partial')

    def test_derived_retrieval_keeps_old_observation_with_explicit_monotonic_binding(self):
        value = fixture(); original = value['strategyEvidenceRetrievedAt']
        value['strategyClaimsDerivedFromObservedAt'] = original
        value['strategyEvidencePool']['source']['retrievedAt'] = NOW
        value['strategyClaims']['source']['retrievedAt'] = NOW
        self.assertEqual(strategy_evidence_budget_for(value, NOW)['status'], 'verified')
        self.assertEqual(value['strategyEvidenceRetrievedAt'], original)
        for derived in (None, '2026-10-26T20:00:00Z'):
            bad = copy.deepcopy(value); bad['strategyClaimsDerivedFromObservedAt'] = derived
            self.assertEqual(strategy_evidence_budget_for(bad, NOW)['status'], 'partial')

    def test_rejected_projection_stays_partial_after_text_and_bad_pool_are_removed(self):
        value = fixture(); value['narrative']['text'] = '未计费正文'
        value['strategyEvidenceBudget'] = strategy_evidence_budget_for(value, NOW)
        value.update(narrative=None, strategyExcerpts=[], strategyAnalysis=None, strategyClaims=None)
        self.assertEqual(strategy_evidence_budget_for(value, NOW)['reasonCodes'], ['PRIOR_QUOTE_BUDGET_REJECTED'])
        value['strategyEvidencePool'] = None
        self.assertEqual(strategy_evidence_budget_for(value, NOW)['status'], 'partial')
        self.assertEqual(strategy_evidence_budget_for(fixture(), NOW)['status'], 'verified')

    def test_real_extraction_entry_reuses_sections_and_returns_pool_claims_and_pdf_identity(self):
        with patch('strategy_claims.parse_strategy_sections', side_effect=AssertionError('must reuse private section')):
            value = extracted()
        self.assertEqual(value['strategyEvidenceBudget']['status'], 'verified')
        self.assertEqual(value['strategyClaimsIdentityEvidence']['retrievedAt'], RECEIVED)
        self.assertEqual(value['strategyClaimsIdentityEvidence']['sourceUrl'], REPORT['sourceUrl'])
        self.assertNotEqual(value['strategyClaimsIdentityEvidence']['sourceTextHash'], value['strategyEvidencePool']['source']['sourceTextHash'])

    def test_collect_report_only_and_interim_supplement_keep_original_quarter_pdf_time(self):
        interim = {**REPORT, 'kind': 'semiannual', 'sourceUrl': 'https://example.org/interim.pdf', 'publishedAt': '2026-08-31',
                   'retrievedAt': '2026-09-01T10:00:00Z'}
        class Source:
            def __init__(self, with_interim): self.with_interim = with_interim
            def overview(self, _): return {'name': '合成基金', 'company': '合成机构', 'managers': []}
            def announcements(self, *_): return [interim, REPORT] if self.with_interim else [REPORT]
            def holdings(self, *_): return {}
            def report_details(self, report): return extracted(report)
        for with_interim in (False, True):
            fund, _ = collect_fund({'code': '000001'}, '2026-10-27', provider=Source(with_interim), target_period='2026-06-30')
            report = fund['reports'][0]
            self.assertTrue(report['reportOnly'])
            self.assertEqual(report['strategyEvidenceRetrievedAt'], RECEIVED)
            self.assertEqual(report['strategyClaimsIdentityEvidence']['sourceUrl'], REPORT['sourceUrl'])
            self.assertEqual(report['portfolioIdentityEvidence']['sourceUrl'], interim['sourceUrl'] if with_interim else REPORT['sourceUrl'])
            self.assertEqual(strategy_evidence_budget_for(report, NOW)['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
