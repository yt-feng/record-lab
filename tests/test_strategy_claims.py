"""Small synthetic page fixtures; no PDF downloads or external inference."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import strategy_claims
from strategy_claims import parse_strategy_claims
from strategy_sections import parse_strategy_sections

REPORT = {'sourceUrl': 'https://example.org/quarterly.pdf', 'kind': 'quarterly',
          'periodEnd': '2026-06-30', 'publishedAt': '2026-07-20', 'retrievedAt': '2026-10-26T11:00:00Z'}
NOW = '2026-10-26T12:00:00Z'
START = '合成基金2026年第2季度报告\n4.4 报告期内基金的投资策略和运作分析\n'
END = '\n4.5 报告期内基金的业绩表现\n本基金净值增长。'


def scan(body=None, **options):
    pages = options.pop('pages', [START + (body or '本基金增配科技行业。') + END])
    return parse_strategy_claims(pages, options.pop('report', REPORT), now=options.pop('now', NOW), **options)['strategyClaims']


def classified(result):
    return [item for item in result['claims'] if item['classificationStatus'] == 'classified']


class StrategyClaimTests(unittest.TestCase):
    def test_later_explicit_reason_survives_first_two_generic_operations(self):
        body = '本基金增加银行行业配置。本基金增加科技行业配置。基于估值偏高，本基金降低消费行业配置。'
        result = scan(body)
        self.assertTrue(result['sourceSectionComplete'])
        self.assertTrue(result['ruleScanComplete'])
        self.assertFalse(result['semanticAnalysisComplete'])
        claim = next(item for item in result['claims'] if item['reasonStatus'] == 'explicit-same-sentence')
        self.assertEqual(claim['object'], {'type': 'industry', 'label': '消费行业'})
        self.assertEqual(claim['action'], 'decrease')
        self.assertEqual(claim['direction'], 'decrease')
        self.assertEqual(claim['reason']['text'], '估值偏高')
        self.assertFalse(claim['reason']['provesIndividualStockReason'])
        evidence = next(item for item in result['evidence'] if item['id'] == claim['evidenceId'])
        self.assertIn('基于估值偏高', evidence['text'])
        self.assertEqual(result['coverage']['sentenceCount'], 3)
        self.assertEqual(result['coverage']['clauseCount'], 4)
        self.assertEqual(result['coverage']['unknownClauseCount'], 1)

    def test_reuses_private_sections_without_repeating_section_extraction(self):
        pages = [START + '本基金增配科技行业。' + END]
        sections = parse_strategy_sections(pages, REPORT)
        with patch.object(strategy_claims, 'parse_strategy_sections', side_effect=AssertionError('must reuse')):
            result = scan(pages=pages, sections_result=sections)
        self.assertTrue(result['ruleScanComplete'])
        self.assertEqual(result['source']['sectionContentHash'], sections.sections[0]['contentHash'])
        self.assertEqual(result['source']['sourceTextHash'], hashlib.sha256('\n\f\n'.join(pages).encode()).hexdigest())

    def test_claims_use_physical_sentence_pages_not_the_section_start(self):
        pages = [START + '本基金维持现金仓位。', '本基金增加科', '技行业配置。' + END]
        result = scan(pages=pages, page_numbers=[7, 8, 9])
        claim = next(item for item in classified(result) if item['action'] == 'increase')
        evidence = next(item for item in result['evidence'] if item['id'] == claim['evidenceId'])
        self.assertEqual(evidence['text'], '本基金增加科技行业配置。')
        self.assertEqual(evidence['page'], 8)
        self.assertEqual(evidence['pages'], [8, 9])
        self.assertEqual(result['source']['pages'], [7, 8, 9])
        for span in claim['matchedSpans']:
            self.assertEqual(evidence['text'][span['start']:span['end']], span['text'])

    def test_denial_is_not_positive_action_or_causal_explanation(self):
        result = scan('基于估值偏高，本基金没有增加科技行业配置。')
        claim = classified(result)[0]
        self.assertEqual(claim['action'], 'increase')
        self.assertEqual(claim['negation'], 'negated')
        self.assertEqual(claim['direction'], 'unknown')
        self.assertEqual(claim['reasonStatus'], 'unknown')
        self.assertFalse(claim['provesTrade'])

    def test_mixed_sentence_is_split_into_current_denial_and_future_view(self):
        result = scan('本基金本期未增配权益资产，我们预计未来看好科技行业。')
        claims = classified(result)
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0]['kind'], 'portfolio-operation')
        self.assertEqual(claims[0]['time'], 'report-period')
        self.assertEqual(claims[0]['negation'], 'negated')
        self.assertEqual(claims[1]['kind'], 'outlook')
        self.assertEqual(claims[1]['time'], 'forward-looking')
        self.assertEqual(claims[1]['stance'], 'positive')
        self.assertEqual(len(result['evidence']), 1)
        self.assertEqual(claims[0]['evidenceId'], claims[1]['evidenceId'])

    def test_conditional_future_and_comparative_operations_remain_distinct(self):
        for body, time, condition, comparison in [
            ('如果估值回落，本基金将增配科技行业。', 'conditional', True, False),
            ('本基金未来将增配科技行业。', 'forward-looking', False, False),
            ('本基金较上期增加股票配置。', 'report-period', False, True),
        ]:
            with self.subTest(body=body):
                claim = next(item for item in classified(scan(body)) if item['action'])
                self.assertEqual(claim['time'], time)
                self.assertEqual(claim['conditional'], condition)
                self.assertEqual(claim['comparison']['present'], comparison)
                self.assertEqual(claim['direction'], 'unknown')
                self.assertEqual(claim['reasonStatus'], 'unknown')

    def test_viewpoint_is_not_an_allocation_verb(self):
        result = scan('本基金对科技行业保持谨慎。我们看好消费行业。')
        claims = classified(result)
        self.assertEqual([item['kind'] for item in claims], ['viewpoint', 'viewpoint'])
        self.assertEqual([item['stance'] for item in claims], ['cautious', 'positive'])
        self.assertTrue(all(item['action'] is None for item in claims))
        self.assertEqual(claims[1]['subject']['scope'], 'report-author')
        self.assertFalse(claims[1]['subject']['isIndividualManager'])

    def test_ambiguous_negation_predicates_subjects_objects_and_times_are_unknown(self):
        cases = ['本基金并非没有增配科技行业。', '本基金不排除增配科技行业。',
                 '本基金未大幅增加科技行业配置。', '本基金增配或减配科技行业。',
                 '本基金增配科技行业和消费行业。', '本基金认为其他基金增配科技行业。',
                 '其他基金增配科技行业。', '预计本基金本期增配科技行业。',
                 '本基金增加盈利预测。', '本基金增配非科技行业。', '本基金可能增配股票。',
                 '上季度本基金增配股票。', '本基金增配股票取决于市场情况。',
                 '本基金增配可选行业并关注科技行业。', '本基金降低对科技行业的谨慎程度。']
        for body in cases:
            with self.subTest(body=body):
                result = scan(body)
                self.assertTrue(result['ruleScanComplete'])
                self.assertFalse(classified(result))
                self.assertEqual(result['coverage']['unknownClauseCount'], 1)
                self.assertTrue(result['unknownReasonCounts'])
                self.assertFalse(result['semanticAnalysisComplete'])

    def test_unknown_text_is_accounted_for_without_forcing_a_direction(self):
        result = scan('基本面存在多种变化。')
        self.assertEqual(result['status'], 'scanned-with-unknown')
        self.assertEqual(result['coverage']['scannedClauseCount'], 1)
        self.assertEqual(result['coverage']['classifiedClauseCount'], 0)
        self.assertEqual(result['claims'][0]['direction'], 'unknown')
        self.assertEqual(result['claims'][0]['reasonStatus'], 'unknown')

    def test_sector_explanation_does_not_become_a_security_trade_reason(self):
        securities = [{'market': 'CN', 'stockCode': '601318', 'stockName': '中国平安'}]
        result = scan('基于估值偏高，本基金减配保险行业。我们关注中国平安，但没有买入。', securities=securities)
        mentions = [mention for evidence in result['evidence'] for mention in evidence['securityMentions']]
        self.assertEqual([item['stockCode'] for item in mentions], ['601318'])
        self.assertFalse(result['explainsIndividualStockTrades'])
        self.assertTrue(all(not item['provesTrade'] and not item['provesTradeReason'] for item in mentions))
        self.assertTrue(all(item['object']['type'] != 'security' for item in result['claims']))

    def test_reasons_do_not_cross_sentences_or_choose_between_multiple_actions(self):
        for body in ['估值偏高。因此本基金降低消费行业配置。',
                     '基于估值偏高，本基金降低消费行业配置，本基金增加现金配置。',
                     '并非由于估值偏高，本基金降低消费行业配置。',
                     '由于估值上升，市场波动加剧，本基金降低消费行业配置。',
                     '由于估值上升，因为盈利下滑，本基金降低消费行业配置。',
                     '由于估值偏高和盈利下滑，本基金降低消费行业配置。',
                     '由于公司声称估值偏高，本基金降低消费行业配置。',
                     '由于“估值偏高”的说法，本基金降低消费行业配置。']:
            self.assertTrue(all(item['reasonStatus'] == 'unknown' for item in scan(body)['claims']))

    def test_source_and_observation_times_are_preserved_and_not_digest_refreshes(self):
        report = {**REPORT, 'retrievedAt': '2026-10-26T18:00:00+08:00'}
        first = scan(report=report)
        self.assertEqual(first['source']['retrievedAt'], report['retrievedAt'])
        second = scan(report={**report, 'retrievedAt': '2026-10-26T11:30:00Z'})
        self.assertEqual(first['source']['digest'], second['source']['digest'])
        for key in ('sourceUrl', 'publishedAt', 'kind', 'periodEnd'):
            self.assertEqual(first['source'][key], REPORT[key])
        self.assertEqual(first['source']['periodStart'], '2026-04-01')
        for value in (None, '2026-10-26T11:00:00', '2026-10-27T00:00:00Z', '2026-07-19T10:00:00Z', '2026-10-26T11:00:00+00:60'):
            with self.subTest(stamp=value):
                result = scan(report={**REPORT, 'retrievedAt': value})
                self.assertFalse(result['ruleScanComplete'])
                self.assertEqual(result['claims'], [])
                self.assertIn('SOURCE_OBSERVATION_UNVERIFIED', result['reasonCodes'])

    def test_wrong_period_kind_and_incomplete_sections_never_promote_claims(self):
        cases = [dict(pages=[START.replace('第2季度', '第3季度') + '本基金增配股票。' + END]),
                 dict(report={**REPORT, 'kind': 'semiannual'}),
                 dict(pages=[START + '本基金增配股票。']),
                 dict(pages=[START + '本基金增配', '股票。' + END], page_numbers=[7, None]),
                 dict(pages=[START + '本基金增配', '股票。' + END], page_numbers=[7, 9])]
        for options in cases:
            with self.subTest(options=options):
                result = scan(**options)
                self.assertFalse(result['ruleScanComplete'])
                self.assertEqual(result['claims'], [])

    def test_reused_proof_requires_matching_source_text_and_private_section(self):
        pages = [START + '本基金增配股票。' + END]
        sections = parse_strategy_sections(pages, REPORT)
        changed = copy.deepcopy(sections); changed.sections[0]['text'] += '不同文本'
        for options in [dict(sections_result=sections, report={**REPORT, 'sourceUrl': 'https://example.org/other.pdf'}),
                        dict(sections_result=sections, pages=[pages[0] + 'extra']),
                        dict(sections_result=changed), dict(sections_result=dict(sections))]:
            result = scan(**{'pages': pages, **options})
            self.assertFalse(result['ruleScanComplete'])
            self.assertEqual(result['claims'], [])

    def test_full_scan_and_selected_public_quotes_have_separate_accounting(self):
        body = ''.join(f'本基金第{index}阶段增加科技行业配置。' for index in range(20))
        result = scan(body)
        count = result['coverage']
        self.assertTrue(result['ruleScanComplete'])
        self.assertEqual(count['clauseCount'], 20)
        self.assertEqual(count['scannedClauseCount'], 20)
        self.assertEqual(count['classifiedClauseCount'] + count['unknownClauseCount'], 20)
        self.assertEqual(count['selectedClaimCount'], 8)
        self.assertEqual(count['omittedClaimCount'], 12)
        self.assertLessEqual(result['publicQuoteBudget']['usedQuoteChars'], 1280)
        self.assertTrue(result['publicQuoteBudget']['sharedAcrossReport'])
        self.assertTrue(all(len(item['text']) <= 160 for item in result['evidence']))
        self.assertNotIn(body, json.dumps(result, ensure_ascii=False))
        self.assertFalse(result['semanticAnalysisComplete'])

    def test_long_sentences_are_not_cut_past_negation_or_causal_context(self):
        body = '本基金增配科技行业，' + '相关说明' * 50 + '。'
        result = scan(body)
        self.assertTrue(result['ruleScanComplete'])
        self.assertGreater(result['coverage']['scannedClauseCount'], 0)
        self.assertEqual(result['claims'], [])
        self.assertEqual(result['evidence'], [])
        self.assertEqual(result['coverage']['omittedSentenceCount'], 1)

    def test_clause_bound_stays_incomplete(self):
        result = scan('情况未知。' * (strategy_claims.MAX_CLAUSES + 1))
        self.assertFalse(result['ruleScanComplete'])
        self.assertIn('CLAUSE_LIMIT_REACHED', result['reasonCodes'])
        self.assertEqual(result['claims'], [])


if __name__ == '__main__':
    unittest.main()
