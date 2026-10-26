"""Finite, source-cited statement rules; never a full semantic summary.

Scan a verified section while its text is still private. Public evidence is
selected and must share the report's existing quote budget when integrated.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from source_dates import china_date
from strategy_sections import (parse_strategy_sections, normalize_strategy_section_coverage,
                               _clean_page, _SECTION, _heading, _boundary, _sentences, _security_mentions)

METHOD = 'explicit-strategy-claims-v2'
MAX_CLAUSES = 4096
MAX_CLAIMS = 8
MAX_QUOTES = 8
MAX_QUOTE_CHARS = 160
MAX_TOTAL_QUOTE_CHARS = 1280
_SUBJECT = re.compile(r'本基金|本产品|本组合|我们')
_ACTION = re.compile(r'增配|加仓|增持|买入|减配|减仓|减持|卖出|增加|提高|降低|减少|维持|保持')
_STANCE = re.compile(r'看好|看多|乐观|谨慎|审慎|看淡|看空|悲观')
_FUTURE = re.compile(r'展望|未来|下一阶段|后续|后市|预计|计划|有望|打算|准备|拟(?:增|减|加|买|卖|调|提|降)|将(?:继续|会|要)?')
_PAST = re.compile(r'本期|报告期内|报告期|本季度|本季|期内|已经|已(?:增|减|加|买|卖|调|提|降|维|保)')
_CONDITION = re.compile(r'(?:如果|倘若|若|一旦|只要|除非)[^，,。；]{1,60}|在[^，,。；]{1,50}(?:前提下|情况下)')
_COMPARISON = re.compile(r'相比|相较|较(?:上期|上季|去年|此前)|相对(?:于|其他|更)|比[^，,。；]{1,20}更|更(?:加)?(?:看好|看多|谨慎|审慎|乐观|看淡|看空|悲观)')
_UNCERTAIN_NEGATION = re.compile(r'并非|并不|不是不|未必|不一定|不排除|不能排除|没有理由不|不再|尚难|难以')
_ACTION_VALUE = {'增配': 'increase', '加仓': 'increase', '增持': 'increase', '买入': 'increase',
                 '增加': 'increase', '提高': 'increase', '减配': 'decrease', '减仓': 'decrease',
                 '减持': 'decrease', '卖出': 'decrease', '降低': 'decrease', '减少': 'decrease',
                 '维持': 'maintain', '保持': 'maintain'}
_STANCE_VALUE = {'看好': 'positive', '看多': 'positive', '乐观': 'positive', '谨慎': 'cautious',
                 '审慎': 'cautious', '看淡': 'negative', '看空': 'negative', '悲观': 'negative'}
# Exact object vocabulary, not company-to-industry inference or ticker aliases.
_OBJECTS = {
    'asset-class': ['权益类资产', '权益资产', '可转换债券', '可转债', '利率债', '信用债', '股票', '债券', '现金', '港股', 'A股', '美股', '黄金'],
    'industry': ['银行行业', '银行板块', '科技行业', '科技板块', '消费行业', '消费板块', '医药行业', '医药板块',
                 '保险行业', '保险板块', '地产行业', '地产板块', '房地产行业', '房地产板块', '新能源行业', '新能源板块',
                 '半导体行业', '半导体板块', '人工智能行业', '人工智能板块', '制造业', '消费电子行业', '消费电子板块'],
    'style': ['高股息策略', '红利策略', '价值风格', '成长风格', '大盘风格', '小盘风格'],
    'theme': ['人工智能方向'],
}
_OBJECT_RE = re.compile('|'.join(re.escape(term) for term in sorted(
    [term for terms in _OBJECTS.values() for term in terms], key=len, reverse=True)))
_OBJECT_TYPE = {term: kind for kind, terms in _OBJECTS.items() for term in terms}


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _instant(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        return None
    try:
        if value[-1] != 'Z' and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            return None
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _source(report, proof):
    value = {key: proof.get(key) for key in ('sourceUrl', 'publishedAt', 'kind', 'periodStart', 'periodEnd', 'sourceTextHash')}
    section = proof.get('sections', [{}])[0] if proof.get('sections') else {}
    original_observation = report.get('strategyEvidenceRetrievedAt') or report.get('retrievedAt')
    value.update({'section': section.get('section'), 'sectionContentHash': section.get('contentHash'),
                  'pages': section.get('pages', []),
                  'retrievedAt': original_observation if _instant(original_observation) else None})
    # Observation instants do not re-arm the same source/rule analysis identity.
    value['digest'] = _hash(json.dumps({**{key: item for key, item in value.items() if key != 'retrievedAt'},
                                      'method': METHOD}, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    return value


def _mapped_sentences(pages, numbers, result, proof):
    """Reuse the existing section, reconstruct only its verified character map."""
    section = proof['sections'][0]
    private = getattr(result, 'sections', ())
    if len(private) != 1 or private[0].get('contentHash') != section['contentHash']:
        return None
    indexes = []
    for physical in section['pages']:
        matches = [index for index, number in enumerate(numbers) if type(number) is int and number == physical]
        if len(matches) != 1:
            return None
        indexes.append(matches[0])
    if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        return None
    chunks = []
    for position, index in enumerate(indexes):
        body = _clean_page(pages[index])
        if position == 0:
            headings = [_heading(match) for match in _SECTION.finditer(body)]
            starts = [item for item in headings if item['text'] == section['heading'] and item['section'] == section['section']]
            if len(starts) != 1:
                return None
            body = body[starts[0]['end']:]
        ending = next((_heading(match) for match in _SECTION.finditer(body) if _boundary(section['section'], _heading(match))), None)
        if ending:
            if position != len(indexes) - 1 or ending['text'] != section['endHeading']:
                return None
            body = body[:ending['start']]
        elif position == len(indexes) - 1:
            return None
        chunks.append((numbers[index], body))
    text, sentences = _sentences(chunks)
    if text != private[0].get('text') or _hash(text) != section['contentHash'] or len(sentences) != proof['sentenceCount']:
        return None
    return sentences


def _quotation_scopes(sentences):
    """Keep explicit quotation scope across sentence and physical-page cuts.

    These are the consecutive sentences reconstructed from the same verified
    private section. No quote text or state is added to the public evidence.
    An unmatched opening quote stays unresolved through the section boundary;
    an unquoted attribution does not extend beyond its original sentence.
    """
    pairs = {'“': '”', '‘': '’', '「': '」', '『': '』', '"': '"', "'": "'"}
    closers = set(pairs.values())
    stack = []
    for sentence in sentences:
        quoted = bool(stack)
        for character in sentence['text']:
            if character not in pairs and character not in closers:
                continue
            quoted = True
            if stack and character == stack[-1]:
                stack.pop()
            elif character in pairs:
                stack.append(pairs[character])
            # A mismatched closer cannot prove that an open quotation ended.
        yield quoted


def _clause_claim(text, sentence, offset, *, quoted_scope=False):
    subject = _SUBJECT.search(text)
    actions, stances = list(_ACTION.finditer(text)), list(_STANCE.finditer(text))
    objects = list(_OBJECT_RE.finditer(text))
    unique_objects = {(item.group(), _OBJECT_TYPE[item.group()]) for item in objects}
    condition = _CONDITION.search(sentence)
    comparison = _COMPARISON.search(text)
    future, past = bool(_FUTURE.search(text)), bool(_PAST.search(text))
    claim = {'classificationStatus': 'unknown', 'kind': 'unknown',
             'subject': {'scope': 'report-author' if subject and subject.group() == '我们' else 'portfolio' if subject else 'unknown',
                         'text': subject.group() if subject else None, 'isIndividualManager': False},
             'object': {'type': 'unknown', 'label': None}, 'action': None, 'stance': None,
             'time': 'conditional' if condition else 'mixed' if future and past else 'forward-looking' if future else 'report-period' if past else 'unspecified',
             'negation': 'affirmed', 'conditional': bool(condition),
             'conditionText': condition.group() if condition else None,
             'comparison': {'present': bool(comparison), 'text': comparison.group() if comparison else None},
             'direction': 'unknown', 'reasonStatus': 'unknown', 'reason': None,
             'unknownReasons': [], 'matchedSpans': [], 'provesTrade': False, 'provesTradeReason': False}
    if subject:
        claim['matchedSpans'].append({'field': 'subject', 'text': subject.group(), 'start': offset + subject.start(), 'end': offset + subject.end()})
    if len(unique_objects) == 1:
        label, kind = next(iter(unique_objects))
        claim['object'] = {'type': kind, 'label': label}
        for item in objects:
            claim['matchedSpans'].append({'field': 'object', 'text': item.group(), 'start': offset + item.start(), 'end': offset + item.end()})
    else:
        claim['unknownReasons'].append('OBJECT_AMBIGUOUS' if unique_objects else 'OBJECT_UNRECOGNIZED')
    # Generic verbs such as "保持" only denote a portfolio operation when an
    # allocation noun is explicit. "保持谨慎" remains a viewpoint.
    operations = [item for item in actions if item.group() not in {'增加', '提高', '降低', '减少', '维持', '保持'}
                  or re.search(r'配置|仓位|比重|权重|持仓|敞口', text)]
    predicates = [('operation', item) for item in operations] + [('stance', item) for item in stances]
    if len(predicates) != 1:
        claim['unknownReasons'].append('PREDICATE_AMBIGUOUS' if predicates else 'PREDICATE_UNRECOGNIZED')
    if not subject:
        claim['unknownReasons'].append('SUBJECT_UNRESOLVED')
    # A comma does not end a quotation or attribution. If the sentence's
    # reported-speaker scope is unclear, none of its clauses becomes our voice.
    if (quoted_scope or len(_SUBJECT.findall(text)) > 1 or re.search(r'其他基金|其他组合|该基金|该组合|他们|他人|业内|分析师', text)
            or re.search(r'引用|转述|据称|听说|声称|表示|指出|提到|认为|[“”「」‘’\"\']', sentence)
            or subject and re.search(r'认为|指出|表示|声称|提到|听说|据称', text[subject.end():])):
        claim['unknownReasons'].append('SUBJECT_SCOPE_UNRESOLVED')
    if _UNCERTAIN_NEGATION.search(text):
        claim['negation'] = 'uncertain'; claim['unknownReasons'].append('NEGATION_SCOPE_UNRESOLVED')
    if re.search(r'可能|或许|也许|假设|是否|取决于|视.{0,20}而定|应当|必须|建议|不宜', text):
        claim['unknownReasons'].append('MODALITY_UNRESOLVED')
    if not comparison and re.search(r'上(?:一)?(?:季度|季|期)|去年|年初以来|今年以来|以往|此前', text):
        claim['unknownReasons'].append('OTHER_PERIOD_REFERENCE')
        claim['time'] = 'unspecified'
    if any(re.search(r'非|不属于|除$', text[max(0, item.start() - 4):item.start()]) for item in objects):
        claim['unknownReasons'].append('OBJECT_NEGATION_UNRESOLVED')
    if future and past:
        claim['unknownReasons'].append('TIME_SCOPE_UNRESOLVED')
    if len(predicates) == 1:
        kind, predicate = predicates[0]
        if len(unique_objects) == 1:
            linked = []
            for item in objects:
                between = text[predicate.end():item.start()] if item.start() >= predicate.end() else text[item.end():predicate.start()]
                allowed = r'(?:了|对|的|在|部分|少量|适度|相关)*' if item.start() >= predicate.end() else (
                    r'(?:的|配置|仓位|比重|权重|持仓|敞口|方面|保持|维持)*' if kind == 'stance' else r'(?:的|配置|仓位|比重|权重|持仓|敞口|方面)*')
                if re.fullmatch(allowed, between): linked.append(item)
            if not linked: claim['unknownReasons'].append('OBJECT_PREDICATE_LINK_UNRESOLVED')
        if kind == 'stance' and any(item.group() in {'增加', '提高', '降低', '减少'} for item in actions):
            claim['unknownReasons'].append('STANCE_SCOPE_UNRESOLVED')
        before = text[max(0, predicate.start() - 8):predicate.start()]
        negated = re.search(r'(?:没有|并未|尚未|未曾|未|不)(?:再)?$', before)
        if negated and claim['negation'] != 'uncertain':
            claim['negation'] = 'negated'
            claim['matchedSpans'].append({'field': 'negation', 'text': negated.group(),
                                          'start': offset + predicate.start() - len(negated.group()), 'end': offset + predicate.start()})
        elif re.search(r'没有|并未|尚未|未曾|未(?!来)|不', before) and not negated:
            claim['negation'] = 'uncertain'; claim['unknownReasons'].append('NEGATION_SCOPE_UNRESOLVED')
        claim['matchedSpans'].append({'field': kind, 'text': predicate.group(), 'start': offset + predicate.start(), 'end': offset + predicate.end()})
        if not claim['unknownReasons']:
            claim['classificationStatus'] = 'classified'
            claim['kind'] = 'outlook' if future or condition else 'portfolio-operation' if kind == 'operation' else 'viewpoint'
            if kind == 'operation':
                claim['action'] = _ACTION_VALUE[predicate.group()]
                # An explicit non-future operation in this report is a reported
                # period operation, not independent proof of an executed trade.
                if claim['time'] == 'unspecified': claim['time'] = 'report-period'
                if claim['negation'] == 'affirmed' and not condition and not comparison and not future:
                    claim['direction'] = claim['action']
            else:
                claim['stance'] = _STANCE_VALUE[predicate.group()]
    return claim


def parse_strategy_claims(pages, report, *, page_numbers=None, securities=None, sections_result=None, now=None):
    """Return selected claims plus whole-section rule-scan accounting.

    ``sections_result`` reuses an existing StrategySections result. Original
    pages are still required to prove physical page positions; the section's
    first page is never guessed as the location of every claim.
    """
    report = report if isinstance(report, dict) else {}
    output = {'schemaVersion': 1, 'method': METHOD, 'status': 'unavailable', 'source': None,
              'sourceSectionComplete': False, 'ruleScanComplete': False, 'semanticAnalysisComplete': False,
              'scanScope': 'all-source-clauses-finite-rules', 'publicEvidenceScope': 'selected-evidence',
              'managerAttribution': 'fund-report-not-individual-speaker', 'explainsIndividualStockTrades': False,
              'coverage': {'sentenceCount': 0, 'clauseCount': 0, 'scannedClauseCount': 0, 'classifiedClauseCount': 0,
                           'unknownClauseCount': 0, 'selectedClaimCount': 0, 'selectedSentenceCount': 0,
                           'omittedClaimCount': 0, 'omittedSentenceCount': 0},
              'unknownReasonCounts': {}, 'reasonCodes': [], 'claims': [], 'evidence': [],
              'publicQuoteBudget': {'sharedAcrossReport': True, 'requiresSharedBudgetAtIntegration': True,
                                    'maxQuotes': MAX_QUOTES, 'maxQuoteChars': MAX_QUOTE_CHARS,
                                    'maxTotalQuoteChars': MAX_TOTAL_QUOTE_CHARS, 'usedQuotes': 0, 'usedQuoteChars': 0}}
    def unavailable(reason):
        output['reasonCodes'].append(reason)
        output['claims'] = []; output['evidence'] = []
        return {'strategyClaims': output}
    if (not isinstance(pages, (list, tuple)) or not pages or len(pages) > 10000
            or any(not isinstance(page, str) or re.search(r'[\ud800-\udfff]', page) for page in pages)
            or report.get('kind') != 'quarterly'):
        return unavailable('INVALID_QUARTER_SOURCE')
    if page_numbers is not None and not isinstance(page_numbers, (list, tuple)):
        return unavailable('INVALID_PAGE_MAP')
    numbers = list(range(1, len(pages) + 1)) if page_numbers is None else list(page_numbers)
    if len(numbers) != len(pages): return unavailable('INVALID_PAGE_MAP')
    result = sections_result if sections_result is not None else parse_strategy_sections(pages, report, page_numbers=numbers, securities=securities)
    proof = normalize_strategy_section_coverage(result.get('strategySectionCoverage') if isinstance(result, dict) else None, report.get('periodEnd'))
    if not proof or any(proof.get(key) != report.get(key) for key in ('sourceUrl', 'publishedAt', 'kind', 'periodEnd')):
        return unavailable('SECTION_SOURCE_MISMATCH')
    if proof['sourceTextHash'] != _hash('\n\f\n'.join(pages)):
        return unavailable('SECTION_TEXT_HASH_MISMATCH')
    output['source'] = _source(report, proof)
    output['sourceSectionComplete'] = proof['complete']
    output['coverage']['sentenceCount'] = proof['sentenceCount']
    if not proof['complete']: return unavailable('SOURCE_SECTION_INCOMPLETE')
    observed = _instant(output['source']['retrievedAt'])
    current = _instant(now) if isinstance(now, str) else now or dt.datetime.now(dt.timezone.utc)
    if (not observed or not isinstance(current, dt.datetime) or current.tzinfo is None
            or observed > current + dt.timedelta(minutes=5) or proof['publishedAt'] > china_date(observed).isoformat()):
        return unavailable('SOURCE_OBSERVATION_UNVERIFIED')
    sentences = _mapped_sentences(pages, numbers, result, proof)
    if sentences is None: return unavailable('SECTION_POSITION_MISMATCH')
    all_claims = []
    for sentence_index, (sentence, quoted_scope) in enumerate(zip(sentences, _quotation_scopes(sentences))):
        text = sentence['text']
        pieces = list(re.finditer(r'[^，,]+', text))
        output['coverage']['clauseCount'] += len(pieces)
        if len(all_claims) + len(pieces) > MAX_CLAUSES:
            return unavailable('CLAUSE_LIMIT_REACHED')
        local = []
        for clause_index, match in enumerate(pieces):
            claim = _clause_claim(match.group(), text, match.start(), quoted_scope=quoted_scope)
            claim.update({'id': f'c{sentence_index}-{clause_index}', 'sentenceIndex': sentence_index, 'clauseIndex': clause_index,
                          'sentenceHash': _hash(text), 'clauseHash': _hash(match.group()),
                          'clauseStart': match.start(), 'clauseEnd': match.end()})
            local.append(claim)
        cause = re.search(r'^(?:由于|因为|基于|考虑到)([^，,。；]{2,60})[，,]', text)
        operations = [claim for claim in local if claim['classificationStatus'] == 'classified' and claim['action']]
        simple_cause = bool(cause and not re.search(r'由于|因为|基于|考虑到|以及|及|和|与|表示|声称|认为|指出|提到|听说|据称|[“”「」‘’"]', cause[1]))
        if (simple_cause and len(local) >= 2 and len(operations) == 1 and operations[0] is local[1]
                and operations[0]['negation'] == 'affirmed'
                and not operations[0]['conditional'] and not operations[0]['comparison']['present']):
            operations[0]['reasonStatus'] = 'explicit-same-sentence'
            operations[0]['reason'] = {'text': cause[1], 'start': cause.start(1), 'end': cause.end(1),
                                       'scope': 'portfolio-statement', 'provesIndividualStockReason': False}
        all_claims.extend(local)
    output['coverage'].update({'scannedClauseCount': len(all_claims),
        'classifiedClauseCount': sum(item['classificationStatus'] == 'classified' for item in all_claims),
        'unknownClauseCount': sum(item['classificationStatus'] == 'unknown' for item in all_claims)})
    for claim in all_claims:
        for reason in set(claim['unknownReasons']):
            output['unknownReasonCounts'][reason] = output['unknownReasonCounts'].get(reason, 0) + 1
    # An explicit reason ranks before generic actions, so later explanations
    # need not disappear behind the first two statements of the same category.
    rank = lambda item: (0 if item['reasonStatus'] == 'explicit-same-sentence' else 1 if item['classificationStatus'] == 'classified' else 2,
                         item['sentenceIndex'], item['clauseIndex'])
    selected = sorted((item for item in all_claims if len(sentences[item['sentenceIndex']]['text']) <= MAX_QUOTE_CHARS), key=rank)[:MAX_CLAIMS]
    evidence = {}
    for claim in sorted(selected, key=lambda item: (item['sentenceIndex'], item['clauseIndex'])):
        index = claim['sentenceIndex']; sentence = sentences[index]
        evidence_id = f's{index}'
        if evidence_id not in evidence:
            cited = list(dict.fromkeys(sentence['positions']))
            if (not cited or any(type(page) is not int or page not in output['source']['pages'] for page in cited)
                    or cited != list(range(cited[0], cited[0] + len(cited)))):
                return unavailable('CLAIM_PAGE_MISMATCH')
            evidence[evidence_id] = {'id': evidence_id, 'text': sentence['text'], 'textHash': _hash(sentence['text']),
                'page': cited[0], 'pages': cited, 'section': output['source']['section'], 'sentenceIndex': index,
                'sourceUrl': proof['sourceUrl'], 'periodStart': proof['periodStart'], 'periodEnd': proof['periodEnd'],
                'quoteTruncated': False, 'securityMentions': _security_mentions(sentence['text'], securities if isinstance(securities, list) else []),
                'provesTrade': False, 'provesTradeReason': False}
        output['claims'].append({**claim, 'evidenceId': evidence_id})
    output['evidence'] = list(evidence.values())
    quote_chars = sum(len(item['text']) for item in output['evidence'])
    if len(output['evidence']) > MAX_QUOTES or quote_chars > MAX_TOTAL_QUOTE_CHARS:
        return unavailable('PUBLIC_QUOTE_LIMIT_REACHED')
    output['coverage'].update({'selectedClaimCount': len(output['claims']), 'selectedSentenceCount': len(evidence),
        'omittedClaimCount': len(all_claims) - len(output['claims']), 'omittedSentenceCount': len(sentences) - len(evidence)})
    output['publicQuoteBudget'].update({'usedQuotes': len(evidence), 'usedQuoteChars': quote_chars})
    output['ruleScanComplete'] = True
    output['status'] = 'scanned-with-unknown' if output['coverage']['unknownClauseCount'] else 'scanned'
    return {'strategyClaims': output}


__all__ = ['parse_strategy_claims', 'METHOD']
