"""Actions-only verification of explicit claims against two fixed report PDFs.

PDF bytes and the full extracted section remain in memory. This command never
changes production observations or the existing report retry budgets.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from portfolio_identity import normalize_portfolio_identity
from publication_guard import inspect_content
from report_extract import mineru_tokens_from_env
from verify_locators import FixedPdfProvider, SAMPLES

OUTPUT = Path('.cache/strategy-claims-verification/result.json')
MAX_OUTPUT_BYTES = 40_000
KNOWN_Q2_VIEWPOINT = '在本报告期我们持续看好人工智能方向'
KNOWN_Q2_SENTENCE = KNOWN_Q2_VIEWPOINT + '，包括算力基础建设和AI 应用方面的发展。'


def known_viewpoint_control(claims, sample, original_section_text=None):
    """One independently read Q2 sentence is a semantic positive control.

    Q3 has no required viewpoint. A market/index description need not become
    a portfolio statement to make the source verification succeed.
    """
    if sample['targetPeriod'] != '2026Q2' or sample['codes'] != ['168401']:
        return True
    if not isinstance(original_section_text, str) or KNOWN_Q2_SENTENCE not in original_section_text:
        return False
    source = claims.get('source') or {}
    if (source.get('sourceUrl') != sample['sourceUrl'] or source.get('periodStart') != '2026-04-01'
            or source.get('periodEnd') != '2026-06-30' or source.get('section') != '4.4'
            or source.get('pages') != [6]):
        return False
    evidence = {row.get('id'): row for row in claims.get('evidence', []) if isinstance(row, dict)}
    for claim in claims.get('claims', []):
        if not isinstance(claim, dict): continue
        quote = evidence.get(claim.get('evidenceId'))
        if not isinstance(quote, dict) or not isinstance(quote.get('text'), str): continue
        start, end = claim.get('clauseStart'), claim.get('clauseEnd')
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(quote['text'])
                or quote['text'][start:end] != KNOWN_Q2_VIEWPOINT
                or quote['text'] != KNOWN_Q2_SENTENCE or start != 0 or end != len(KNOWN_Q2_VIEWPOINT)
                or quote.get('page') != 6 or quote.get('pages') != [6]
                or quote.get('sourceUrl') != sample['sourceUrl'] or quote.get('periodStart') != '2026-04-01'
                or quote.get('periodEnd') != '2026-06-30'):
            continue
        expected = {'classificationStatus': 'classified', 'kind': 'viewpoint',
                    'subject': {'scope': 'report-author', 'text': '我们', 'isIndividualManager': False},
                    'object': {'type': 'theme', 'label': '人工智能方向'}, 'action': None, 'stance': 'positive',
                    'time': 'report-period', 'negation': 'affirmed', 'conditional': False, 'conditionText': None,
                    'comparison': {'present': False, 'text': None}, 'direction': 'unknown',
                    'reasonStatus': 'unknown', 'reason': None, 'unknownReasons': [],
                    'provesTrade': False, 'provesTradeReason': False}
        if all(claim.get(key) == value for key, value in expected.items()):
            return True
    return False


def bounded_evidence(claims, proof):
    evidence, statements = claims.get('evidence'), claims.get('claims')
    if not isinstance(evidence, list) or not isinstance(statements, list) or len(evidence) > 8 or len(statements) > 8:
        return False
    ids, total = set(), 0
    section = next(iter(proof.get('sections') or []), {})
    pages = set(section.get('pages', []))
    for item in evidence:
        if not isinstance(item, dict): return False
        quote, cited, key = item.get('text'), item.get('pages'), item.get('id')
        if (not isinstance(key, str) or key in ids or not isinstance(quote, str) or not 1 <= len(quote) <= 160
                or not isinstance(cited, list) or not cited or any(type(page) is not int or page not in pages for page in cited)
                or cited != list(range(cited[0], cited[-1] + 1)) or item.get('page') != cited[0]
                or item.get('textHash') != hashlib.sha256(quote.encode()).hexdigest()
                or any(item.get(field) != proof.get(field) for field in ('sourceUrl', 'periodStart', 'periodEnd'))):
            return False
        ids.add(key); total += len(quote)
    return total <= 1280 and all(isinstance(item, dict) and item.get('evidenceId') in ids for item in statements)


def scan_accounting(claims, proof):
    coverage = claims.get('coverage') or {}
    fields = ('sentenceCount', 'clauseCount', 'scannedClauseCount', 'classifiedClauseCount', 'unknownClauseCount',
              'selectedClaimCount', 'selectedSentenceCount', 'omittedClaimCount', 'omittedSentenceCount')
    if any(type(coverage.get(key)) is not int or coverage[key] < 0 for key in fields): return False
    return (claims.get('reasonCodes') == []
            and claims.get('status') == ('scanned-with-unknown' if coverage['unknownClauseCount'] else 'scanned')
            and coverage['sentenceCount'] == proof.get('sentenceCount')
            and coverage['clauseCount'] == coverage['scannedClauseCount']
            and coverage['scannedClauseCount'] == coverage['classifiedClauseCount'] + coverage['unknownClauseCount']
            and coverage['selectedClaimCount'] == len(claims.get('claims') or [])
            and coverage['selectedSentenceCount'] == len(claims.get('evidence') or [])
            and coverage['omittedClaimCount'] + coverage['selectedClaimCount'] == coverage['clauseCount']
            and coverage['omittedSentenceCount'] + coverage['selectedSentenceCount'] == coverage['sentenceCount'])


def run_sample(sample, *, provider_factory=None):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('ACTIONS_REQUIRED')
    if sample not in SAMPLES or mineru_tokens_from_env():
        raise ValueError('FIXED_SAMPLE_CONFIGURATION_INVALID')
    from strategy_claims import parse_strategy_claims, METHOD
    import strategy_sections

    source = (provider_factory or FixedPdfProvider)(sample)
    captured = []
    parser = strategy_sections.parse_strategy_sections

    def capture(pages, document, **kwargs):
        sections = parser(pages, document, **kwargs)
        captured.append((pages, document, kwargs, sections))
        return sections

    document = {'fundCode': sample['codes'][0], 'sourceUrl': sample['sourceUrl'],
                'periodEnd': {'2026Q2': '2026-06-30', '2026Q3': '2026-09-30'}[sample['targetPeriod']],
                'kind': 'quarterly', 'publishedAt': sample['publishedAt']}
    try:
        with patch.object(strategy_sections, 'parse_strategy_sections', capture):
            details = source.report_details(document, max_pages=80)
        identity = normalize_portfolio_identity(details.get('portfolioIdentityEvidence'), document)
        if len(captured) != 1 or not identity:
            raise ValueError('SOURCE_IDENTITY_OR_SECTION_CAPTURE_FAILED')
        pages, parsed_document, options, sections = captured[0]
        claims = parse_strategy_claims(pages,
            {**parsed_document, 'retrievedAt': identity['retrievedAt']},
            page_numbers=options.get('page_numbers'), securities=options.get('securities'),
            sections_result=sections)['strategyClaims']
        proof = sections['strategySectionCoverage']
        section = next(iter(proof.get('sections') or []), {})
        claim_source = claims.get('source') or {}
        original_codes = [identity['sourcePortfolioCode'], *identity['shareClassCodes']]
        expected_rule_digest = hashlib.sha256(json.dumps(
            {**{key: value for key, value in claim_source.items() if key not in ('digest', 'retrievedAt')}, 'method': METHOD},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        checks = {
            'oneRealPdfRequest': source.pdf_requests == 1,
            'pdfIdentityMatched': document['fundCode'] in original_codes and identity['periodEnd'] == document['periodEnd']
                and identity['sourceUrl'] == sample['sourceUrl'],
            'pdfHashPreserved': bool(source.pdf_sha256) and identity['pdfSha256'] == source.pdf_sha256,
            'sourceTimePreserved': claim_source.get('retrievedAt') == identity['retrievedAt'],
            'sourceTextHashPreserved': claim_source.get('sourceTextHash') == proof['sourceTextHash'],
            'sourceDocumentMatched': all(parsed_document.get(key) == document.get(key)
                and claim_source.get(key) == document.get(key)
                for key in ('sourceUrl', 'publishedAt', 'kind', 'periodEnd')),
            'sectionHashPreserved': bool(section.get('contentHash')) and claim_source.get('sectionContentHash') == section['contentHash'],
            'sectionCoordinatesMatched': claim_source.get('periodStart') == proof.get('periodStart')
                and all(claim_source.get(key) == section.get(key) for key in ('pages', 'section')),
            'sectionSourceComplete': proof.get('complete') is True and claims.get('sourceSectionComplete') is True,
            'fullRuleScanComplete': claims.get('ruleScanComplete') is True,
            'scanAccountingMatched': scan_accounting(claims, proof),
            'limitedSemanticScope': claims.get('semanticAnalysisComplete') is False,
            'boundedPublicClaims': bounded_evidence(claims, proof),
            'currentRuleIdentity': claims.get('method') == METHOD and claim_source.get('digest') == expected_rule_digest,
        }
        known_required = sample['targetPeriod'] == '2026Q2' and sample['codes'] == ['168401']
        if known_required:
            private_sections = getattr(sections, 'sections', ())
            original_text = private_sections[0].get('text') if len(private_sections) == 1 else None
            checks['knownViewpointControl'] = known_viewpoint_control(claims, sample, original_text)
        return {'code': document['fundCode'], 'quarter': sample['targetPeriod'], 'sourceUrl': sample['sourceUrl'],
                'pdfSha256': source.pdf_sha256, 'pdfRequestCount': source.pdf_requests,
                'knownViewpointRequired': known_required,
                'checks': checks, 'strategyClaims': claims, 'passed': all(checks.values())}
    finally:
        source.session.close()


def save_result(results, output=OUTPUT):
    expected = {(row['codes'][0], row['targetPeriod'], row['sourceUrl']) for row in SAMPLES}
    observed = [(row.get('code'), row.get('quarter'), row.get('sourceUrl')) for row in results]
    passed = (len(results) == len(SAMPLES) and set(observed) == expected
              and all(row.get('passed') is True and isinstance(row.get('checks'), dict)
                      and row['checks'] and all(value is True for value in row['checks'].values()) for row in results))
    summary = {'schemaVersion': 1, 'status': 'verified' if passed else 'partial',
               'observedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
               'scope': 'two-fixed-pdfs-explicit-language-rules', 'productionDataModified': False,
               'semanticAnalysisComplete': False, 'pdfRequestLimit': 2, 'results': results}
    raw = json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_OUTPUT_BYTES or inspect_content(raw):
        raw = json.dumps({'status': 'failed', 'errorCode': 'VERIFICATION_PUBLICATION_REJECTED',
                          'productionDataModified': False}).encode()
        passed = False
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.tmp')
    temp.write_bytes(raw)
    temp.replace(output)
    return passed


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'ACTIONS_REQUIRED'}))
        return 2
    results = []
    for sample in SAMPLES:
        try:
            results.append(run_sample(sample))
        except Exception:
            # No provider response, local path or raw exception is public output.
            results.append({'code': sample['codes'][0], 'quarter': sample['targetPeriod'],
                            'passed': False, 'errorCode': 'CLAIM_VERIFICATION_FAILED'})
    passed = save_result(results)
    print(json.dumps({'status': 'verified' if passed else 'partial', 'productionDataModified': False}))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
