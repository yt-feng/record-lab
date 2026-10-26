"""Synthetic complete sections only; no provider, credentials or PDF requests."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import strategy_semantics as semantic
from strategy_sections import parse_strategy_sections

REPORT = {'sourceUrl': 'https://example.org/quarterly.pdf', 'kind': 'quarterly', 'periodEnd': '2026-06-30',
          'publishedAt': '2026-07-20', 'strategyEvidenceRetrievedAt': '2026-10-26T19:00:00.123456+08:00',
          'retrievedAt': '2026-10-27T00:00:00Z'}
NOW = '2026-10-26T12:00:00Z'
START = '合成基金2026年第2季度报告\n4.4 报告期内基金的投资策略和运作分析\n'
END = '\n4.5 报告期内基金的业绩表现\n本基金净值增长。'


def packet(body='本基金看好科技行业。', **options):
    pages = options.pop('pages', [START + body + END])
    return semantic.build_semantic_packet(pages, options.pop('report', REPORT),
        existing_evidence=options.pop('existing_evidence', []), now=options.pop('now', NOW), **options)


def span(text, literal):
    start = text.index(literal)
    return {'start': start, 'end': start + len(literal), 'text': literal}


def claim(text, *, subject='本基金', target='科技行业', predicate='看好'):
    return {'kind': 'opinion', 'subject': {'scope': 'fund-report', 'span': span(text, subject)},
            'object': {'type': 'industry', 'span': span(text, target)}, 'predicate': span(text, predicate),
            'polarity': 'positive', 'time': 'report-period', 'negation': 'affirmed', 'conditional': False,
            'negation_spans': [], 'condition_spans': [], 'comparison_spans': [], 'attribution_spans': [],
            'reason': {'status': 'unknown', 'cause': None, 'connective': None, 'operation': None}}


def response(value, selected=None):
    selected = selected or {}
    return {'schemaVersion': 1, 'inputDigest': value['inputDigest'], 'sentences': [
        {'sentence_id': row['sentence_id'], 'status': 'interpreted' if row['index'] in selected else 'unknown',
         'reason': None if row['index'] in selected else 'not-classified', 'claims': selected.get(row['index'], [])}
        for row in value.model_input()['sentences']]}


def evidence(text, pages=None):
    return {'text': text, 'sourceUrl': REPORT['sourceUrl'], 'periodStart': '2026-04-01', 'periodEnd': REPORT['periodEnd'],
            'pages': pages or [1]}


class SemanticPacketTests(unittest.TestCase):
    def test_full_private_section_is_available_only_through_explicit_memory_input(self):
        body = '本基金看好科技行业。市场变化仍需观察。'
        value = packet(body)
        self.assertEqual((value['status'], value['attempted']), ('pending-provider-credentials', False))
        self.assertTrue(value['sourceSectionComplete'])
        self.assertEqual(value.model_input()['section_text'], body)
        self.assertEqual(len(value.model_input()['sentences']), 2)
        self.assertNotIn(body, json.dumps(value, ensure_ascii=False))
        self.assertNotIn('市场变化仍需观察', repr(value))
        self.assertEqual(value['source']['retrievedAt'], REPORT['strategyEvidenceRetrievedAt'])
        self.assertEqual(packet(body, credentials_available=True)['status'], 'ready')

    def test_transport_schema_is_closed_and_bound_but_does_not_replace_literal_validator(self):
        value = packet(); schema = semantic.semantic_response_schema(value)
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['properties']['inputDigest']['const'], value['inputDigest'])
        self.assertEqual(schema['properties']['sentences']['minItems'], 1)
        def walk(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    self.assertFalse(node['additionalProperties'])
                    self.assertEqual(set(node['required']), set(node['properties']))
                for child in node.values(): walk(child)
            elif isinstance(node, list):
                for child in node: walk(child)
        walk(schema)
        self.assertNotIn('本基金看好科技行业', json.dumps(schema, ensure_ascii=False))

    def test_no_environment_or_network_access_when_credentials_are_missing(self):
        with patch('os.getenv', side_effect=AssertionError('no credentials')), \
             patch('socket.create_connection', side_effect=AssertionError('no network')):
            value = packet()
        self.assertEqual(value['reasonCodes'], ['PROVIDER_CREDENTIALS_MISSING'])
        self.assertFalse(value['attempted'])

    def test_strategy_page_hash_is_not_report_normalized_text_hash(self):
        pages = [START + '本基金看好科技行业。', '市场保持平稳。' + END]
        sections = parse_strategy_sections(pages, REPORT)
        ordinary_hash = hashlib.sha256('\n\n'.join(page.strip() for page in pages).encode()).hexdigest()
        expected = hashlib.sha256('\n\f\n'.join(pages).encode()).hexdigest()
        self.assertNotEqual(ordinary_hash, expected)
        with patch.object(semantic, 'parse_strategy_sections', side_effect=AssertionError('reuse private section')):
            value = packet(pages=pages, sections_result=sections,
                           report={**REPORT, 'sourceTextHash': ordinary_hash, 'strategySectionCoverage': sections['strategySectionCoverage']})
        self.assertEqual(value['source']['sourceTextHash'], expected)
        self.assertEqual(value['source']['sectionContentHash'], sections.sections[0]['contentHash'])
        mutated = copy.deepcopy(sections)
        mutated['strategySectionCoverage']['sourceTextHash'] = ordinary_hash
        bad = packet(pages=pages, sections_result=mutated)
        self.assertEqual(bad['reasonCodes'], ['SOURCE_TEXT_HASH_MISMATCH'])

    def test_digest_and_sentence_ids_are_stable_without_replacing_original_time(self):
        first = packet()
        changed_time = '2026-10-26T11:02:00Z'
        second = packet(report={**REPORT, 'strategyEvidenceRetrievedAt': changed_time})
        self.assertEqual(first['inputDigest'], second['inputDigest'])
        self.assertEqual(first.model_input()['sentences'], second.model_input()['sentences'])
        self.assertEqual(second['source']['retrievedAt'], changed_time)
        self.assertNotEqual(first['inputDigest'], packet('本基金谨慎看待科技行业。')['inputDigest'])

    def test_wrong_quarter_interim_unknown_pages_private_text_mutation_and_source_conflict(self):
        pages = [START + '本基金看好科技行业。' + END]
        sections = parse_strategy_sections(pages, REPORT)
        cases = [
            {'report': {**REPORT, 'kind': 'interim'}},
            {'report': {**REPORT, 'periodEnd': '2026-09-30'}},
            {'page_numbers': [None]},
            {'report': {**REPORT, 'sourceUrl': 'https://example.org/other.pdf'}, 'sections_result': sections},
            {'sections_result': dict(sections)},
        ]
        broken = copy.deepcopy(sections); broken.sections[0]['text'] += '新增文字'
        cases.append({'sections_result': broken})
        for case in cases:
            with self.subTest(case=list(case)):
                value = packet(pages=pages, **case)
                self.assertFalse(value['sourceSectionComplete'])
                self.assertFalse(value['attempted'])
                with self.assertRaises(semantic.SemanticError): value.model_input()

    def test_source_time_checks_strict_clock_and_china_publication_day(self):
        for stamp in ('2026-10-26T24:00:00Z', '2026-10-26T11:00:00+00:60', '2026-10-27T11:00:00Z', '2026-07-18T23:59:00Z'):
            with self.subTest(stamp=stamp):
                self.assertFalse(packet(report={**REPORT, 'strategyEvidenceRetrievedAt': stamp})['sourceSectionComplete'])
        value = packet(report={**REPORT, 'strategyEvidenceRetrievedAt': '2026-07-19T16:01:00Z'}, now='2026-07-19T16:02:00Z')
        self.assertTrue(value['sourceSectionComplete'])

    def test_physical_page_and_unicode_codepoint_offsets_survive_split_sentence(self):
        pages = [START + '本基金看好科', '技行业。😀市场仍需观察。' + END]
        value = packet(pages=pages, page_numbers=[7, 8])
        private = value.model_input()
        self.assertEqual(private['offsetUnit'], 'unicode-code-point')
        self.assertEqual(private['sentences'][0]['pages'], [7, 8])
        for row in private['sentences']:
            self.assertEqual(private['section_text'][row['start']:row['end']], row['text'])
        self.assertEqual(private['sentences'][1]['pages'], [8])


class SemanticResponseTests(unittest.TestCase):
    def test_valid_structured_response_stays_separate_from_semantic_completeness(self):
        value = packet('本基金看好科技行业。市场情况难以判断。')
        raw = response(value, {0: [claim(value.model_input()['sentences'][0]['text'])]})
        out = semantic.validate_semantic_response(value, json.dumps(raw, ensure_ascii=False))
        self.assertEqual(out['status'], 'structure-and-evidence-validated')
        self.assertTrue(out['inputCoverageComplete'])
        self.assertFalse(out['semanticAnalysisComplete'])
        self.assertFalse(out['semanticCorrectnessVerified'])
        self.assertEqual(out['coverage']['unknown'], 1)
        self.assertEqual([row['status'] for row in out['sentenceStatuses']], ['interpreted', 'unknown'])
        self.assertTrue(all('text' not in row for row in out['sentenceStatuses']))
        self.assertEqual(out['source'], value['source'])
        self.assertFalse(out['claims'][0]['provesTradeReason'])

    def test_missing_duplicate_unknown_sentence_ids_and_wrong_digest_are_rejected(self):
        value = packet('本基金看好科技行业。市场情况难以判断。')
        cases = []
        bad = response(value); bad['sentences'].pop(); cases.append(bad)
        bad = response(value); bad['sentences'][1] = copy.deepcopy(bad['sentences'][0]); cases.append(bad)
        bad = response(value); bad['sentences'][0]['sentence_id'] = 'invented'; cases.append(bad)
        bad = response(value); bad['inputDigest'] = '0' * 64; cases.append(bad)
        bad = response(value); bad['source'] = REPORT; cases.append(bad)
        for bad in cases:
            self.assertEqual(semantic.validate_semantic_response(value, bad)['status'], 'rejected')

    def test_nonliteral_span_unknown_fields_duplicate_json_keys_and_bools_are_rejected(self):
        value = packet(); text = value.model_input()['sentences'][0]['text']
        good = response(value, {0: [claim(text)]})
        cases = []
        bad = copy.deepcopy(good); bad['sentences'][0]['claims'][0]['predicate']['text'] = '减持'; cases.append(bad)
        bad = copy.deepcopy(good); bad['sentences'][0]['claims'][0]['predicate']['start'] = True; cases.append(bad)
        bad = copy.deepcopy(good); bad['sentences'][0]['claims'][0]['provesTrade'] = True; cases.append(bad)
        cases.extend(['{"schemaVersion":1,"schemaVersion":1}', '{"inputDigest":NaN}', 'x' * 400001])
        for bad in cases:
            out = semantic.validate_semantic_response(value, bad)
            self.assertEqual((out['status'], out['claims'], out['evidence']), ('rejected', [], []))

    def test_negation_condition_comparison_and_future_cannot_be_erased(self):
        variants = [
            ('本基金不看好科技行业。', {'negation': 'negated', 'polarity': 'negative', 'negation_spans': ['不']}),
            ('如果需求回升，本基金未来看好科技行业。', {'conditional': True, 'time': 'conditional', 'condition_spans': ['如果需求回升']}),
            ('相比上季度，本基金更看好科技行业。', {'comparison_spans': ['相比上季度', '更看好']}),
            ('本基金预计未来看好科技行业。', {'time': 'future'}),
        ]
        for text, changes in variants:
            value = packet(text); raw_claim = claim(text)
            self.assertEqual(semantic.validate_semantic_response(value, response(value, {0: [raw_claim]}))['status'], 'rejected')
            for key, item in changes.items(): raw_claim[key] = [span(text, word) for word in item] if key.endswith('_spans') else item
            out = semantic.validate_semantic_response(value, response(value, {0: [raw_claim]}))
            self.assertEqual(out['status'], 'structure-and-evidence-validated', out['reasonCodes'])

    def test_cross_sentence_cross_page_quoted_view_is_not_fund_author_view(self):
        pages = [START + '报告引用：“市场平稳。', '在本报告期我们持续看好人工智能方向。后续观察”。' + END]
        value = packet(pages=pages, page_numbers=[6, 7])
        middle = next(row for row in value.model_input()['sentences'] if '人工智能' in row['text'])
        self.assertEqual(middle['pages'], [7])
        self.assertTrue(middle['quoted_context'])
        self.assertEqual(middle['quoted_context'][0]['attribution'], 'quoted-other')
        raw_claim = claim(middle['text'], subject='我们', target='人工智能方向')
        raw_claim['object']['type'] = 'theme'
        wrong = semantic.validate_semantic_response(value, response(value, {middle['index']: [raw_claim]}))
        self.assertEqual(wrong['reasonCodes'], ['QUOTED_ATTRIBUTION_CHANGED'])
        raw_claim['subject']['scope'] = 'quoted-other'
        accepted = semantic.validate_semantic_response(value, response(value, {middle['index']: [raw_claim]}))
        self.assertEqual(accepted['status'], 'structure-and-evidence-validated')
        self.assertTrue(accepted['claims'][0]['quotedContext'])
        self.assertEqual(accepted['managerAttribution'], 'fund-report-not-individual-speaker')

    def test_unclosed_quote_and_unbounded_relay_require_unknown_attribution(self):
        for text in ('报告引用：“市场平稳。我们看好科技行业。', '分析师表示：市场平稳。我们看好科技行业。'):
            value = packet(text)
            row = next(item for item in value.model_input()['sentences'] if '我们看好' in item['text'])
            item = claim(row['text'], subject='我们')
            item['subject']['scope'] = 'quoted-other'
            self.assertEqual(semantic.validate_semantic_response(value, response(value, {row['index']: [item]}))['status'], 'rejected')
            item['subject']['scope'] = 'unknown'
            self.assertEqual(semantic.validate_semantic_response(value, response(value, {row['index']: [item]}))['status'], 'structure-and-evidence-validated')

    def test_explicit_adjacent_reason_preserved_but_market_bridge_and_security_inference_rejected(self):
        text = '基于估值偏高，本基金降低消费行业配置。'
        value = packet(text)
        item = claim(text, target='消费行业', predicate='降低')
        item.update(kind='operation', polarity='neutral')
        item['reason'] = {'status': 'explicit', 'cause': span(text, '估值偏高'), 'connective': span(text, '基于'),
                          'operation': span(text, '本基金降低消费行业配置')}
        self.assertEqual(semantic.validate_semantic_response(value, response(value, {0: [item]}))['status'], 'structure-and-evidence-validated')
        bridged = '由于估值上升，市场波动加剧，本基金降低消费行业配置。'
        value = packet(bridged)
        item = claim(bridged, target='消费行业', predicate='降低'); item.update(kind='operation', polarity='neutral')
        item['reason'] = {'status': 'explicit', 'cause': span(bridged, '估值上升'), 'connective': span(bridged, '由于'),
                          'operation': span(bridged, '本基金降低消费行业配置')}
        self.assertEqual(semantic.validate_semantic_response(value, response(value, {0: [item]}))['reasonCodes'], ['CAUSAL_SCOPE_NOT_PROVEN'])
        item['reason'] = {'status': 'unknown', 'cause': None, 'connective': None, 'operation': None}
        self.assertEqual(semantic.validate_semantic_response(value, response(value, {0: [item]}))['status'], 'structure-and-evidence-validated')
        single = '基于估值偏高，本基金降低中国平安配置。'
        value = packet(single); item = claim(single, target='中国平安', predicate='降低'); item.update(kind='operation', polarity='neutral')
        item['object']['type'] = 'security-mention'
        item['reason'] = {'status': 'explicit', 'cause': span(single, '估值偏高'), 'connective': span(single, '基于'),
                          'operation': span(single, '本基金降低中国平安配置')}
        self.assertEqual(semantic.validate_semantic_response(value, response(value, {0: [item]}))['status'], 'rejected')

    def test_shared_budget_counts_existing_quotes_and_reuses_identical_evidence(self):
        text = '本基金看好科技行业。'
        value = packet(text, existing_evidence=[evidence(text), evidence(text)])
        out = semantic.validate_semantic_response(value, response(value, {0: [claim(text)]}))
        self.assertEqual(out['evidence'], [])
        self.assertEqual(len(out['reusedEvidenceIds']), 1)
        self.assertEqual(out['reusedEvidenceRefs'][0]['textHash'], hashlib.sha256(text.encode()).hexdigest())
        self.assertNotIn('text', out['reusedEvidenceRefs'][0])
        self.assertEqual(out['publicQuoteBudget']['combinedQuoteChars'], len(text))
        old = '甲' * 1279 + '。'
        value = packet(old + text, existing_evidence=[evidence(old)])
        out = semantic.validate_semantic_response(value, response(value, {1: [claim(text)]}))
        self.assertEqual(out['status'], 'structure-and-evidence-validated')
        self.assertEqual(out['claims'], [])
        self.assertEqual(out['coverage']['quoteBudgetClaims'], 1)
        self.assertEqual(out['publicQuoteBudget']['combinedQuoteChars'], 1280)
        self.assertNotIn(old, json.dumps(out, ensure_ascii=False))

    def test_existing_wrong_source_over_budget_or_nonliteral_quotes_do_not_create_fresh_budget(self):
        text = '本基金看好科技行业。'
        for old in ([{**evidence(text), 'sourceUrl': 'https://example.org/other.pdf'}], [evidence('不存在原句')], [evidence('甲' * 1281)],
                    [{**evidence(text), 'pages': [True]}], [{**evidence(text), 'textHash': '0' * 64}]):
            self.assertFalse(packet(text, existing_evidence=old)['sourceSectionComplete'])

    def test_long_sentence_is_accounted_for_without_clipping_away_qualifiers(self):
        text = '本基金看好科技行业，' + '市场变化仍需持续观察' * 20 + '。'
        value = packet(text)
        out = semantic.validate_semantic_response(value, response(value, {0: [claim(text)]}))
        self.assertTrue(out['inputCoverageComplete'])
        self.assertEqual(out['coverage']['validatedClaimCount'], 1)
        self.assertEqual(out['coverage']['longSentenceClaims'], 1)
        self.assertEqual((out['claims'], out['evidence']), ([], []))

    def test_unknown_sentence_cannot_hide_claims_and_packet_manifest_cannot_be_rebound(self):
        value = packet(); raw = response(value)
        raw['sentences'][0]['claims'] = [claim(value.model_input()['sentences'][0]['text'])]
        self.assertEqual(semantic.validate_semantic_response(value, raw)['status'], 'rejected')
        value['source']['sourceUrl'] = 'https://example.org/other.pdf'
        self.assertEqual(semantic.validate_semantic_response(value, raw)['reasonCodes'], ['INVALID_PRIVATE_PACKET'])

    def test_response_cannot_smuggle_free_text_into_public_sentence_statuses(self):
        value = packet(); raw = response(value)
        raw['sentences'][0]['reason'] = '本基金看好科技行业。'
        out = semantic.validate_semantic_response(value, raw)
        self.assertEqual(out['status'], 'rejected')
        self.assertNotIn('本基金看好科技行业', json.dumps(out, ensure_ascii=False))
        self.assertEqual(semantic.validate_semantic_response(value, '[' * 2000 + ']' * 2000)['status'], 'rejected')

    def test_chinese_and_ascii_single_quotes_protect_cross_sentence_and_page_attribution(self):
        for opening, closing in (('‘', '’'), ("'", "'")):
            pages = [START + f'某人说：{opening}市场平稳。', f'本基金看好股票。继续观察{closing}。' + END]
            value = packet(pages=pages, page_numbers=[4, 5])
            row = next(item for item in value.model_input()['sentences'] if '本基金看好' in item['text'])
            self.assertTrue(row['quoted_context'])
            item = claim(row['text'], target='股票')
            out = semantic.validate_semantic_response(value, response(value, {row['index']: [item]}))
            self.assertEqual(out['reasonCodes'], ['QUOTED_ATTRIBUTION_CHANGED'])
            item['subject']['scope'] = 'unknown'
            accepted = semantic.validate_semantic_response(value, response(value, {row['index']: [item]}))
            self.assertEqual(accepted['status'], 'structure-and-evidence-validated')
            self.assertEqual(accepted['claims'][0]['subject']['scope'], 'unknown')

    def test_free_summary_is_not_in_schema_and_is_rejected_instead_of_published(self):
        value = packet(); item = claim(value.model_input()['sentences'][0]['text'])
        schema = semantic.semantic_response_schema(value)
        claim_properties = schema['properties']['sentences']['items']['properties']['claims']['items']['properties']
        self.assertNotIn('summary', claim_properties)
        item['summary'] = '本基金因某原因卖出某证券。'
        out = semantic.validate_semantic_response(value, response(value, {0: [item]}))
        self.assertEqual((out['status'], out['claims']), ('rejected', []))
        self.assertNotIn('某原因', json.dumps(out, ensure_ascii=False))

    def test_repeated_literal_spans_share_one_quote_pool_but_enums_remain_unverified(self):
        text = '本基金看好科技行业。'; value = packet(text)
        item = claim(text)
        out = semantic.validate_semantic_response(value, response(value, {0: [item, copy.deepcopy(item)]}))
        self.assertEqual(len(out['claims']), 2)
        self.assertEqual(len(out['evidence']), 1)
        self.assertEqual(out['publicQuoteBudget']['combinedQuoteChars'], len(text))
        self.assertEqual(out['publicQuoteBudget']['accounting'], 'unique-source-quote-pool-not-serialized-bytes')
        self.assertTrue(all(row['semanticVerification'] == 'pending' for row in out['claims']))


if __name__ == '__main__':
    unittest.main()
