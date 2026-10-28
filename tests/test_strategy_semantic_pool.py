"""Canonical mapper fixtures only: no PDF bytes, provider, secret or inference."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import strategy_semantics as semantic
import strategy_semantic_pool as mapper
import strategy_evidence_pool as pool_api
from strategy_sections import parse_strategy_sections
from portfolio_identity import parse_portfolio_identity
from test_strategy_semantics import claim, response

NOW = '2026-10-27T02:00:00Z'
OLD = '2026-10-26T19:00:00.123456+00:00'
POOL_TIME = '2026-10-26T19:10:00.123456+00:00'
NEW = '2026-10-26T20:00:00.123456+00:00'
START = '4.4 报告期内基金的投资策略和运作分析\n'
END = '\n4.5 报告期内基金的业绩表现\n本期净值表现。'


def case(body='本基金看好科技行业。市场保持平稳。', *, old_indices=(), old_literals=None, chunks=None,
         numbers=None, source_time=NEW):
    pages = ['合成基金2026年第2季度报告\n报告期2026年4月1日至2026年6月30日',
             '§2 基金产品概况\n基金主代码 000001\n下属分级基金的交易代码\n000001 000002\n',
             '§3 主要财务指标和基金净值表现\n3.1 主要财务指标\n']
    strategy_pages = chunks or [START + body + END]
    pages.extend(strategy_pages)
    numbers = numbers or list(range(1, len(pages) + 1))
    metadata = {'sourceUrl': 'https://example.org/quarter.pdf', 'publishedAt': '2026-07-21', 'kind': 'quarterly',
                'periodEnd': '2026-06-30', 'fundCode': '000001', 'retrievedAt': source_time}
    sections = parse_strategy_sections(pages, metadata, page_numbers=numbers)
    proof = sections['strategySectionCoverage']
    empty = semantic.build_semantic_packet(pages, metadata, existing_evidence=[], sections_result=sections,
                                           page_numbers=numbers, credentials_available=True, now=NOW)
    sentences = empty.model_input()['sentences']
    source = {key: proof[key] for key in pool_api.SOURCE_FIELDS if key != 'retrievedAt'}
    source['retrievedAt'] = POOL_TIME
    chosen = [(sentences[index]['text'], sentences[index]['pages']) for index in old_indices]
    chosen += list(old_literals or [])
    quotes = [{'id': pool_api._quote_id(source['sourceTextHash'], semantic._hash(text), used_pages),
               'text': text, 'textHash': semantic._hash(text), 'page': used_pages[0], 'pages': used_pages,
               'section': proof['sections'][0]['section']} for text, used_pages in chosen]
    pool = {'schemaVersion': 1, 'method': pool_api.METHOD, 'status': 'verified', 'source': source, 'quotes': quotes,
            'digest': pool_api._digest(source, quotes), 'usedQuotes': len(quotes), 'usedQuoteChars': sum(len(q['text']) for q in quotes),
            **pool_api.LIMITS, 'accounting': pool_api.ACCOUNTING, 'semanticAnalysisComplete': False}
    projections = [{**q, 'evidencePoolId': q['id'], 'sourceUrl': source['sourceUrl'],
                    'periodStart': source['periodStart'], 'periodEnd': source['periodEnd']} for q in quotes]
    identity = parse_portfolio_identity(pages, metadata, page_numbers=numbers, retrieved_at=source_time,
                                        pdf_sha256='f' * 64, now=NOW)
    old_identity = {**identity, 'retrievedAt': POOL_TIME}
    report = {**metadata, 'retrievedAt': OLD, 'strategyEvidenceRetrievedAt': OLD,
              'strategyClaimsDerivedFromObservedAt': OLD, 'strategySectionCoverage': proof,
              'strategyEvidencePool': pool, 'strategyClaimsIdentityEvidence': old_identity,
              'narrative': projections[0] if projections else None, 'strategyExcerpts': projections,
              'strategyAnalysis': {'evidence': copy.deepcopy(projections)},
              'strategyClaims': {'source': copy.deepcopy(source), 'evidence': copy.deepcopy(projections)}}
    report['strategyEvidenceBudget'] = pool_api.strategy_evidence_budget_for(report, NOW)
    packet = semantic.build_semantic_packet(pages, metadata, existing_evidence=projections,
        sections_result=sections, page_numbers=numbers, credentials_available=True, now=NOW)
    return report, packet, identity


def observations(packet, raw):
    # Six retained request receipts, including failures and unknown; the mapper
    # neither creates a new request nor starts another zero-based counter.
    history = []
    totals = [1000, 5450, 10000, 10000, 16624, 7796]
    for index, total in enumerate(totals):
        history.append({'requestDigest': str(index + 1) * 64, 'inputDigest': packet['inputDigest'],
            'attemptedAt': f'2026-10-26T20:{index + 1:02d}:00.123456+00:00',
            'finishedAt': f'2026-10-26T20:{index + 1:02d}:05.123456+00:00',
            'outcome': 'validated' if index == 5 else 'unknown' if index == 0 else 'rejected',
            'responseDigest': chr(ord('a') + index) * 64,
            'usage': {'prompt_tokens': total // 2, 'completion_tokens': total - total // 2, 'total_tokens': total}})
    model = {'provider': 'deepseek', 'model': 'deepseek-flash', 'responseModel': 'deepseek-flash',
             'promptVersion': 'source-bound-deepseek-v5', 'boundResponseDigest': mapper.canonical_digest(raw),
             'observation': copy.deepcopy(history[-1])}
    return model, history


def mapped(report, packet, identity, selected=None, **overrides):
    raw = response(packet, selected)
    model, history = observations(packet, raw)
    arguments = {'code': '000001', 'source_identity': identity, 'model_observation': model, 'usage_history': history,
                 'expected_baseline_digest': report['strategyEvidencePool']['digest'],
                 'expected_usage_history_digest': mapper.canonical_digest(history), 'now': NOW}
    arguments.update(overrides)
    return mapper.map_strategy_semantic_pool(report, packet, raw, **arguments)


class SemanticPoolTests(unittest.TestCase):
    def test_real_failed_attempt_is_preserved_before_a_validated_candidate(self):
        report, packet, identity = case(old_indices=(1,))
        raw = response(packet)
        model, history = observations(packet, raw)
        history[0]['outcome'] = 'failed'
        history[0]['responseDigest'] = None
        history[0]['usage'] = None
        candidate = mapped(report, packet, identity, usage_history=history, model_observation=model,
            expected_usage_history_digest=mapper.canonical_digest(history))
        self.assertEqual(candidate['usageHistory'][0], history[0])
        self.assertEqual(candidate['usageAccounting']['attemptCount'], 6)
        self.assertEqual(candidate['usageAccounting']['unknownUsageAttempts'], 1)

    def test_new_quotes_use_production_ids_one_pool_and_preserve_all_inputs(self):
        report, packet, identity = case(old_indices=(1,))
        before = copy.deepcopy((report, dict(packet), packet.model_input(), identity))
        row = packet.model_input()['sentences'][0]
        candidate = mapped(report, packet, identity, {0: [claim(row['text'])]})
        self.assertEqual(candidate['pool']['quotes'][:1], report['strategyEvidencePool']['quotes'])
        self.assertEqual(candidate['coverage']['newQuoteCount'], 1)
        self.assertEqual(candidate['claims'][0]['evidencePoolId'], candidate['pool']['quotes'][1]['id'])
        self.assertNotIn('text', candidate['claims'][0]['subject']['span'])
        self.assertNotIn('evidence', candidate)
        self.assertEqual(candidate['claims'][0]['subject']['span']['textHash'], semantic._hash('本基金'))
        self.assertEqual((report, dict(packet), packet.model_input(), identity), before)
        self.assertIsNotNone(pool_api.normalize_strategy_evidence_pool(candidate['pool'], report['strategySectionCoverage'], now=NOW))
        self.assertFalse(candidate['semanticAnalysisComplete']); self.assertFalse(candidate['semanticCorrectnessVerified'])
        self.assertEqual(candidate['productionAdmission'], 'not-connected')

    def test_reused_quote_and_multiple_claims_do_not_duplicate_public_text_or_budget(self):
        report, packet, identity = case(old_indices=(0,))
        raw_claim = claim(packet.model_input()['sentences'][0]['text'])
        candidate = mapped(report, packet, identity, {0: [raw_claim, copy.deepcopy(raw_claim)]})
        self.assertEqual(candidate['pool'], report['strategyEvidencePool'])
        self.assertEqual(candidate['coverage']['newQuoteCount'], 0)
        self.assertEqual(len(candidate['evidenceIdMap']), 1)
        self.assertEqual(len(candidate['claims']), 2)
        self.assertEqual(candidate['pool']['usedQuoteChars'], len('本基金看好科技行业。'))

    def test_partial_overlap_is_a_distinct_quote_and_literal_whitespace_cannot_be_repaired(self):
        report, packet, identity = case(old_literals=[('科技行业', [4])])
        candidate = mapped(report, packet, identity, {0: [claim('本基金看好科技行业。')]})
        self.assertEqual(candidate['pool']['usedQuotes'], 2)
        self.assertEqual(candidate['pool']['quotes'][0]['text'], '科技行业')
        self.assertEqual(candidate['pool']['quotes'][1]['text'], '本基金看好科技行业。')
        bad = copy.deepcopy(report); pool = bad['strategyEvidencePool']
        quote = pool['quotes'][0]; quote['text'] = '科 技行业'; quote['textHash'] = semantic._hash(quote['text'])
        quote['id'] = pool_api._quote_id(pool['source']['sourceTextHash'], quote['textHash'], quote['pages'])
        pool.update(digest=pool_api._digest(pool['source'], pool['quotes']), usedQuoteChars=len(quote['text']))
        with self.assertRaises(mapper.MappingError): mapped(bad, packet, identity)

    def test_three_source_clocks_and_model_time_remain_independent(self):
        report, packet, identity = case(old_indices=(0,))
        candidate = mapped(report, packet, identity, {0: [claim('本基金看好科技行业。')]})
        self.assertEqual(report['strategyEvidenceRetrievedAt'], OLD)
        self.assertEqual(candidate['pool']['source']['retrievedAt'], POOL_TIME)
        self.assertEqual(candidate['sidecar']['derivedFromObservedAt'], POOL_TIME)
        self.assertEqual(candidate['sidecar']['retainedStrategyObservedAt'], OLD)
        self.assertEqual(candidate['sidecar']['source']['retrievedAt'], NEW)
        self.assertEqual(candidate['sidecar']['sourceIdentity']['retrievedAt'], NEW)
        self.assertNotEqual(candidate['sidecar']['modelObservation']['observation']['finishedAt'], NEW)
        self.assertNotEqual(identity['sourceTextHash'], packet['source']['sourceTextHash'])
        bad = case(source_time='2026-10-26T19:10:00.123455+00:00')
        with self.assertRaisesRegex(mapper.MappingError, 'SOURCE_OBSERVATION_MISMATCH'): mapped(*bad)
        same_instant = case(source_time='2026-10-27T03:10:00.123456+08:00')
        self.assertEqual(mapped(*same_instant)['pool']['source']['retrievedAt'], POOL_TIME)

    def test_full_legacy_pool_omits_new_claims_without_evicting_reordering_or_truncation(self):
        body = ''.join(f'市场第{i}阶段保持平稳。' for i in range(8)) + '本基金看好科技行业。'
        report, packet, identity = case(body, old_indices=tuple(range(8)))
        candidate = mapped(report, packet, identity, {8: [claim('本基金看好科技行业。')]})
        self.assertEqual(candidate['pool'], report['strategyEvidencePool'])
        self.assertEqual(candidate['claims'], [])
        self.assertEqual(candidate['coverage']['quoteBudgetClaims'], 1)
        self.assertTrue(candidate['inputCoverageComplete']); self.assertFalse(candidate['semanticAnalysisComplete'])

    def test_same_text_different_pages_is_two_quotes_but_same_page_ambiguity_is_omitted(self):
        text = '本基金看好科技行业。'
        report, packet, identity = case(chunks=[START + text, text + END], numbers=[1, 2, 3, 6, 7])
        candidate = mapped(report, packet, identity, {0: [claim(text)], 1: [claim(text)]})
        self.assertEqual([quote['pages'] for quote in candidate['pool']['quotes']], [[6], [7]])
        self.assertNotEqual(*[quote['id'] for quote in candidate['pool']['quotes']])
        report, packet, identity = case(text * 2)
        ambiguous = mapped(report, packet, identity, {0: [claim(text)], 1: [claim(text)]})
        self.assertEqual(ambiguous['claims'], [])
        self.assertEqual(ambiguous['coverage']['mapperOmittedClaims']['ambiguousLiteralClaims'], 2)
        self.assertFalse(ambiguous['mappingCompleteForSelectedClaims'])

    def test_overlapping_legacy_literal_is_rejected_instead_of_first_match(self):
        report, packet, identity = case('本基金看好哈哈哈行业。', old_literals=[('哈哈', [4])])
        self.assertTrue(packet['sourceSectionComplete'])  # upstream nonoverlap search alone is insufficient
        with self.assertRaisesRegex(mapper.MappingError, 'BASELINE_LITERAL_AMBIGUOUS_OR_MISSING'):
            mapped(report, packet, identity)

    def test_unicode_codepoints_cross_page_and_long_sentences_are_never_clipped(self):
        prefix = '本基金看好科技行业😀'
        for length in (160, 161):
            text = prefix + '甲' * (length - len(prefix) - 1) + '。'
            report, packet, identity = case(chunks=[START + text[:10], text[10:] + END], numbers=[1, 2, 3, 6, 7])
            candidate = mapped(report, packet, identity, {0: [claim(text)]})
            if length == 160:
                self.assertEqual(candidate['pool']['usedQuoteChars'], 160)
                self.assertEqual(candidate['pool']['quotes'][0]['pages'], [6, 7])
                self.assertEqual(candidate['claims'][0]['sentenceRange']['end'], 160)
            else:
                self.assertEqual(candidate['claims'], [])
                self.assertEqual(candidate['coverage']['longSentenceClaims'], 1)

    def test_source_section_hash_identity_code_and_observation_changes_are_rejected(self):
        report, packet, identity = case(old_indices=(0,))
        for change in ('section-hash', 'section-pages', 'source', 'identity-time', 'identity-period', 'identity-code'):
            p, i = copy.deepcopy(packet), copy.deepcopy(identity)
            if change.startswith('section') or change == 'source':
                key = {'section-hash': 'sectionContentHash', 'section-pages': 'pages', 'source': 'sourceUrl'}[change]
                p['source'][key] = {'section-hash': 'a'*64, 'section-pages': [5], 'source': 'https://example.org/other.pdf'}[change]
                p._manifest = copy.deepcopy(dict(p))
            if change == 'identity-time': i['retrievedAt'] = POOL_TIME
            if change == 'identity-period': i['kind'] = 'semiannual'
            if change == 'identity-code': i['sourcePortfolioCode'] = '999999'
            with self.subTest(change=change), self.assertRaises(mapper.MappingError): mapped(report, p, i)
        with self.assertRaises(mapper.MappingError): mapped(report, packet, identity, code='999999')
        self.assertEqual(mapped(report, packet, identity, code='000002')['code'], '000002')
        missing = copy.deepcopy(report); missing.pop('strategyClaimsIdentityEvidence')
        with self.assertRaisesRegex(mapper.MappingError, 'BASELINE_QUARTER_IDENTITY_MIGRATION_REQUIRED'):
            mapped(missing, packet, identity)

    def test_bool_pages_and_nonadjacent_cross_page_proof_fail_closed(self):
        report, packet, identity = case(old_indices=(0,))
        bad = copy.deepcopy(report); quote = bad['strategyEvidencePool']['quotes'][0]
        quote['page'] = True; quote['pages'] = [True]
        with self.assertRaises(mapper.MappingError): mapped(bad, packet, identity)
        bad = copy.deepcopy(packet); bad['source']['pages'] = [6, 8]; bad._manifest = copy.deepcopy(dict(bad))
        with self.assertRaisesRegex(mapper.MappingError, 'COMPLETE_SECTION_BINDING_MISMATCH'): mapped(report, bad, identity)

    def test_baseline_digest_order_and_clock_compare_and_swap_are_idempotent(self):
        report, packet, identity = case(old_indices=(1,))
        candidate = mapped(report, packet, identity, {0: [claim('本基金看好科技行业。')]})
        old = report['strategyEvidencePool']
        applied = mapper.apply_candidate_pool(old, candidate)
        self.assertEqual(applied['status'], 'candidate-applied-in-memory')
        self.assertEqual(mapper.apply_candidate_pool(applied['pool'], candidate)['status'], 'already-applied')
        self.assertEqual(mapper.apply_candidate_pool(old, candidate)['pool'], applied['pool'])
        changed = copy.deepcopy(old); changed['source']['retrievedAt'] = OLD
        with self.assertRaisesRegex(mapper.MappingError, 'BASELINE_POOL_CHANGED'): mapper.apply_candidate_pool(changed, candidate)
        changed = copy.deepcopy(applied['pool']); changed['quotes'].reverse()
        changed['digest'] = pool_api._digest(changed['source'], changed['quotes'])
        with self.assertRaises(mapper.MappingError): mapper.apply_candidate_pool(changed, candidate)
        with self.assertRaisesRegex(mapper.MappingError, 'UNVERIFIED_POOL_CANDIDATE'): mapper.apply_candidate_pool(old, dict(candidate))
        candidate['claims'].append({'injected': True})
        with self.assertRaises(mapper.MappingError): mapper.apply_candidate_pool(old, candidate)

    def test_all_six_attempts_and_50870_tokens_survive_and_unknown_is_not_zero(self):
        report, packet, identity = case()
        raw = response(packet); model, history = observations(packet, raw)
        before = copy.deepcopy(history)
        candidate = mapped(report, packet, identity)
        self.assertEqual(candidate['usageHistory'], history)
        self.assertEqual(candidate['usageAccounting']['attemptCount'], 6)
        self.assertEqual(candidate['usageAccounting']['knownTotalTokens'], 50870)
        self.assertEqual(history, before)
        history[0]['usage'] = None; history[0]['responseDigest'] = None
        unknown = mapped(report, packet, identity, usage_history=history, model_observation=model,
                         expected_usage_history_digest=mapper.canonical_digest(history))
        self.assertEqual(unknown['usageAccounting']['attemptCount'], 6)
        self.assertEqual(unknown['usageAccounting']['unknownUsageAttempts'], 1)
        self.assertEqual(unknown['usageAccounting']['knownTotalTokens'], 49870)
        for changed in (history[1:], [], history + [history[-1]]):
            with self.assertRaises(mapper.MappingError): mapped(report, packet, identity, usage_history=changed)

    def test_digest_rehash_cannot_bypass_core_negation_condition_or_quoted_attribution(self):
        cases = [('本基金并不看好科技行业。', {}), ('若市场回暖，本基金看好科技行业。', {}),
                 ('报告引用：“本基金看好科技行业。”', {})]
        for body, _ in cases:
            report, packet, identity = case(body)
            sentence = packet.model_input()['sentences'][0]
            raw = response(packet, {0: [claim(sentence['text'])]})
            model, history = observations(packet, raw)
            with self.subTest(body=body), self.assertRaisesRegex(mapper.MappingError, 'STRICT_SEMANTIC_RESPONSE_REJECTED'):
                mapper.map_strategy_semantic_pool(report, packet, raw, code='000001', source_identity=identity,
                    model_observation=model, usage_history=history, expected_baseline_digest=report['strategyEvidencePool']['digest'],
                    expected_usage_history_digest=mapper.canonical_digest(history), now=NOW)

    def test_wrong_response_digest_history_binding_and_old_partial_do_not_self_heal(self):
        report, packet, identity = case()
        raw = response(packet); model, history = observations(packet, raw)
        model['boundResponseDigest'] = '0'*64
        with self.assertRaisesRegex(mapper.MappingError, 'BOUND_RESPONSE_DIGEST_MISMATCH'):
            mapped(report, packet, identity, model_observation=model)
        history[-1]['inputDigest'] = '0'*64; model['observation'] = history[-1]
        with self.assertRaisesRegex(mapper.MappingError, 'MODEL_OBSERVATION_MISMATCH'):
            mapped(report, packet, identity, model_observation=model, usage_history=history,
                   expected_usage_history_digest=mapper.canonical_digest(history))
        report['strategyEvidenceBudget']['status'] = 'partial'
        with self.assertRaisesRegex(mapper.MappingError, 'BASELINE_POOL_MIGRATION_REQUIRED'): mapped(report, packet, identity)

    def test_all_unknown_is_accounted_but_never_semantically_complete_or_public_fulltext(self):
        report, packet, identity = case('市场保持平稳。需要更多信息。')
        with patch('socket.create_connection', side_effect=AssertionError('no network')), \
             patch('os.getenv', side_effect=AssertionError('no credential access')):
            candidate = mapped(report, packet, identity)
        self.assertEqual(candidate['coverage']['unknown'], 2)
        self.assertEqual(candidate['claims'], [])
        self.assertTrue(candidate['inputCoverageComplete']); self.assertFalse(candidate['semanticCorrectnessVerified'])
        self.assertFalse(candidate['semanticAnalysisComplete']); self.assertFalse(candidate['productionReady'])
        public = json.dumps(candidate, ensure_ascii=False)
        for text in ('市场保持平稳', '需要更多信息', 'section_text', 'reasoning_content'):
            self.assertNotIn(text, public)


if __name__ == '__main__':
    unittest.main()
