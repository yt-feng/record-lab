"""Actions-only, one fixed public PDF and at most one model request.

This is a diagnostic artifact, not production admission. Complete PDF text,
private packets, provider bodies and credentials are never written or printed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
from unittest.mock import patch

import requests

from deepseek_strategy import MAX_RESPONSE_BYTES, MODEL, http_transport, run_packet
from portfolio_identity import normalize_portfolio_identity
from providers import parse_pdf_pages
from publication_guard import inspect_content
from report_extract import extract_report
from strategy_evidence_pool import normalize_strategy_evidence_pool
from strategy_semantics import build_semantic_packet
import strategy_sections

OUTPUT = Path('.cache/deepseek-strategy-verification/result.json')
MAX_ARTIFACT_BYTES = 96_000
MAX_PDF_BYTES = 16_000_000
SAMPLE = {'fundCode': '168401', 'sourceUrl': 'https://static.cninfo.com.cn/finalpage/2026-07-10/1225416373.PDF',
          'periodEnd': '2026-06-30', 'kind': 'quarterly', 'publishedAt': '2026-07-10'}
RESULT_FIELDS = {'schemaVersion', 'kind', 'sample', 'generatedAt', 'requestedModel', 'status', 'executionSuccess',
    'dataStatus', 'errorCode', 'pdfRequests', 'modelRequests', 'pdfReceipt', 'modelReceipt', 'checks', 'source',
    'inputDigest', 'inference', 'publicEvidence', 'productionReady', 'productionDataModified',
    'semanticAnalysisComplete', 'productionAdmission', 'quotePoolIntegration'}
INFERENCE_FIELDS = {'schemaVersion', 'provider', 'requestedModel', 'promptVersion', 'endpoint', 'status',
    'attempted', 'requestCount', 'errorCode', 'receipt', 'usage', 'responseModel', 'responseDigest',
    'analysis', 'semanticAnalysisComplete', 'productionReady', 'formatDiagnostics', 'bindingDiagnostics'}
PRIVATE_FIELDS = {'body', 'content', 'section_text', 'sentences', 'messages', 'reasoning_content',
    'raw', 'rawText', 'fullText', 'markdown', 'authorization', 'api_key', 'credential', 'sectionsText'}
FORMAT_FIELDS = {'schemaVersion', 'digestEncoding', 'originalBytes', 'originalCharacters', 'originalDigest',
    'leadingToken', 'fence', 'normalization', 'normalizedBytes', 'normalizedCharacters', 'normalizedDigest',
    'jsonStatus', 'jsonError'}
BINDING_FIELDS = {'schemaVersion', 'method', 'modelSpanFormat', 'status', 'sentenceCount',
    'examinedClaimCount', 'examinedSpanCount', 'boundSpanCount', 'errorCode', 'boundResponseDigest'}
BINDING_ERRORS = ('STRICT_VALIDATOR_PREFLIGHT_REJECTED', 'MODEL_SPAN_SHAPE_INVALID',
    'MODEL_LITERAL_EMPTY', 'MODEL_LITERAL_NOT_FOUND', 'MODEL_LITERAL_AMBIGUOUS', 'MODEL_BINDING_LIMIT')


def valid_binding_diagnostics(value):
    """Expose fixed binding counters and hashes, never a model literal or error body."""
    if value is None:
        return True
    if (type(value) is not dict or set(value) != BINDING_FIELDS
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or value['method'] != 'unique-exact-sentence-span-v1'
            or value['modelSpanFormat'] != 'text-only-v1' or value['status'] not in ('bound', 'rejected')):
        return False
    for key, maximum in (('sentenceCount', 128), ('examinedClaimCount', 512),
                         ('examinedSpanCount', 19456), ('boundSpanCount', 19456)):
        if type(value[key]) is not int or not 0 <= value[key] <= maximum:
            return False
    if value['boundSpanCount'] > value['examinedSpanCount']:
        return False
    if value['status'] == 'bound':
        return (value['sentenceCount'] > 0 and value['boundSpanCount'] == value['examinedSpanCount']
                and value['errorCode'] is None and isinstance(value['boundResponseDigest'], str)
                and re.fullmatch(r'[a-f0-9]{64}', value['boundResponseDigest']) is not None)
    return value['errorCode'] in BINDING_ERRORS and value['boundResponseDigest'] is None


def valid_format_diagnostics(value):
    """Only finite structure metadata may cross the private response boundary."""
    if value is None:
        return True
    if (type(value) is not dict or set(value) != FORMAT_FIELDS
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or value['digestEncoding'] != 'utf8-surrogatepass'
            or value['leadingToken'] not in ('empty', 'object', 'array', 'string', 'number',
                                           'true', 'false', 'null', 'code-fence', 'other')
            or value['fence'] not in ('none', 'complete-json', 'complete-other', 'incomplete', 'multiple', 'mixed-content')
            or value['normalization'] not in ('none', 'removed-single-json-fence')
            or value['jsonStatus'] not in ('valid', 'invalid')):
        return False
    for key in ('originalBytes', 'originalCharacters', 'normalizedBytes', 'normalizedCharacters'):
        if type(value[key]) is not int or not 0 <= value[key] <= MAX_RESPONSE_BYTES:
            return False
    for key in ('originalDigest', 'normalizedDigest'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[a-f0-9]{64}', value[key]):
            return False
    if (value['originalCharacters'] > value['originalBytes']
            or value['normalizedCharacters'] > value['normalizedBytes']):
        return False
    if value['normalization'] == 'none':
        if any(value['original' + suffix] != value['normalized' + suffix]
               for suffix in ('Bytes', 'Characters', 'Digest')):
            return False
    elif (value['fence'] != 'complete-json' or value['leadingToken'] != 'code-fence'
          or value['normalizedBytes'] >= value['originalBytes']
          or value['normalizedCharacters'] >= value['originalCharacters']):
        return False
    error = value['jsonError']
    if value['jsonStatus'] == 'valid':
        return error is None
    if (type(error) is not dict or set(error) != {'category', 'line', 'column', 'position'}
            or error['category'] not in ('syntax', 'duplicate-key', 'nonfinite', 'unicode', 'depth', 'size', 'type')):
        return False
    if error['category'] == 'syntax':
        return (all(type(error[key]) is int and 1 <= error[key] <= MAX_RESPONSE_BYTES
                    for key in ('line', 'column'))
                and type(error['position']) is int and 0 <= error['position'] <= value['normalizedCharacters'])
    return all(error[key] is None for key in ('line', 'column', 'position'))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def pdf_get():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('ACTIONS_REQUIRED')
    with requests.Session() as session:
        session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
        with session.get(SAMPLE['sourceUrl'], timeout=(10, 30), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('PDF_HTTP_REJECTED')
            blocks, size = [], 0
            for block in response.iter_content(chunk_size=65536):
                size += len(block)
                if size > MAX_PDF_BYTES:
                    raise ValueError('PDF_SIZE_LIMIT')
                blocks.append(block)
    value = b''.join(blocks)
    if not value.startswith(b'%PDF'):
        raise ValueError('PDF_INVALID')
    return value


def write_result(result, output=OUTPUT):
    def compact(value, key=None):
        if isinstance(value, dict):
            return len(value) <= 64 and all(isinstance(name, str) and name not in PRIVATE_FIELDS
                and compact(item, name) for name, item in value.items())
        if isinstance(value, list):
            return len(value) <= 128 and all(compact(item, key) for item in value)
        if isinstance(value, str):
            return len(value) <= (160 if key == 'text' else 2048)
        return value is None or type(value) in (bool, int)
    if (not isinstance(result, dict) or set(result) != RESULT_FIELDS or result['sample'] != SAMPLE
            or result['productionReady'] is not False or result['productionDataModified'] is not False
            or result['semanticAnalysisComplete'] is not False or not compact(result)
            or result['inference'] is not None and (not isinstance(result['inference'], dict)
                or set(result['inference']) != INFERENCE_FIELDS
                or not valid_format_diagnostics(result['inference']['formatDiagnostics'])
                or not valid_binding_diagnostics(result['inference']['bindingDiagnostics'])
                or result['inference']['status'] == 'validated' and
                    (result['inference']['formatDiagnostics'] is None
                     or result['inference']['formatDiagnostics']['jsonStatus'] != 'valid'
                     or result['inference']['bindingDiagnostics'] is None
                     or result['inference']['bindingDiagnostics']['status'] != 'bound'))):
        raise ValueError('PUBLICATION_REJECTED')
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_ARTIFACT_BYTES or inspect_content(encoded, max_bytes=MAX_ARTIFACT_BYTES):
        raise ValueError('PUBLICATION_REJECTED')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    temporary.write_bytes(encoded)
    temporary.replace(output)


def verify(credential, *, fetch_pdf=pdf_get, extractor=extract_report,
           transport=http_transport, checkpoint=lambda _: None):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('ACTIONS_REQUIRED')
    result = {'schemaVersion': 1, 'kind': 'deepseek-strategy-verification', 'sample': dict(SAMPLE),
              'generatedAt': now(), 'requestedModel': MODEL, 'status': 'pending', 'executionSuccess': True,
              'dataStatus': 'pending', 'errorCode': None, 'pdfRequests': 0, 'modelRequests': 0,
              'pdfReceipt': None, 'modelReceipt': None, 'checks': {}, 'source': None, 'inputDigest': None,
              'inference': None, 'publicEvidence': [], 'productionReady': False,
              'productionDataModified': False, 'semanticAnalysisComplete': False,
              'productionAdmission': 'not-connected', 'quotePoolIntegration': 'pending-canonical-id-mapping'}
    if not isinstance(credential, str) or not credential.strip():
        result['errorCode'] = 'PROVIDER_CREDENTIALS_MISSING'; checkpoint(result); return result
    checkpoint(result)
    try:
        result['pdfRequests'] = 1
        result['pdfReceipt'] = {'attemptedAt': now(), 'fetchedAt': None, 'outcome': 'started'}
        checkpoint(result)
        raw_pdf = fetch_pdf()
        if not isinstance(raw_pdf, bytes) or not raw_pdf.startswith(b'%PDF') or len(raw_pdf) > MAX_PDF_BYTES:
            raise ValueError('PDF_INVALID')
        fetched_at = now()
        result['pdfReceipt'].update(fetchedAt=fetched_at, outcome='received')
        checkpoint(result)
        captured, parser = [], strategy_sections.parse_strategy_sections

        def capture(pages, document, **options):
            sections = parser(pages, document, **options)
            captured.append((pages, document, options, sections))
            return sections

        with patch.object(strategy_sections, 'parse_strategy_sections', capture):
            details = extractor(raw_pdf, SAMPLE['sourceUrl'], SAMPLE['periodEnd'], SAMPLE['kind'],
                parse_pages=parse_pdf_pages, published_at=SAMPLE['publishedAt'], retrieved_at=fetched_at,
                max_pages=80, env={})
        identity = normalize_portfolio_identity(details.get('portfolioIdentityEvidence'), SAMPLE)
        if len(captured) != 1 or not identity or identity['retrievedAt'] != fetched_at:
            raise ValueError('PDF_SOURCE_IDENTITY_INVALID')
        pages, document, options, sections = captured[0]
        pool = normalize_strategy_evidence_pool(details.get('strategyEvidencePool'),
            sections.get('strategySectionCoverage'), now=now())
        if not pool or pool['source']['retrievedAt'] != fetched_at:
            raise ValueError('SOURCE_QUOTE_POOL_INVALID')
        old_evidence = [{**row, **{key: pool['source'][key] for key in ('sourceUrl', 'periodStart', 'periodEnd')}}
                        for row in pool['quotes']]
        packet = build_semantic_packet(pages, {**document, 'strategyEvidenceRetrievedAt': fetched_at},
            existing_evidence=old_evidence, sections_result=sections,
            page_numbers=options.get('page_numbers'), credentials_available=True, now=now())
        if packet['status'] != 'ready':
            raise ValueError('SEMANTIC_PACKET_NOT_READY')
        proof, source = sections['strategySectionCoverage'], packet['source']
        result.update(source=source, inputDigest=packet['inputDigest'])
        result['checks'] = {
            'oneOriginalPdfRequest': result['pdfRequests'] == 1,
            'oneStrategySectionExtraction': len(captured) == 1,
            'requestedCodeProvenInPdf': SAMPLE['fundCode'] in [identity['sourcePortfolioCode'], *identity['shareClassCodes']],
            'pdfBytesHashMatches': identity['pdfSha256'] == hashlib.sha256(raw_pdf).hexdigest(),
            'sourceTimePreserved': source['retrievedAt'] == identity['retrievedAt'] == pool['source']['retrievedAt'] == fetched_at,
            'wholeSectionHashPreserved': source['sourceTextHash'] == proof['sourceTextHash'],
            'sectionContentHashPreserved': source['sectionContentHash'] == proof['sections'][0]['contentHash'],
            'sourceDocumentMatches': all(source[key] == SAMPLE[key] for key in ('sourceUrl', 'periodEnd', 'kind', 'publishedAt')),
            'sourceSectionComplete': packet['sourceSectionComplete'],
            'oldPublicEvidenceAccounted': packet['publicQuoteBudget']['existingQuoteChars'] == pool['usedQuoteChars'],
        }
        if not all(result['checks'].values()):
            raise ValueError('SOURCE_BINDING_FAILED')

        def model_checkpoint(receipt):
            result.update(modelRequests=1, modelReceipt=receipt)
            checkpoint(result)

        inference = run_packet(packet, credential, checkpoint=model_checkpoint, transport=transport)
        result['inference'] = inference
        result['modelRequests'] = inference['requestCount']
        analysis = inference.get('analysis') or {}
        # Diagnostic evidence IDs remain separate from production pool IDs.
        # Only the already-validated short texts may accompany reused refs.
        for ref in analysis.get('reusedEvidenceRefs', []):
            matches = [quote for quote in pool['quotes'] if quote['textHash'] == ref['textHash'] and quote['pages'] == ref['pages']]
            if len(matches) != 1:
                raise ValueError('SOURCE_QUOTE_POOL_INVALID')
            result['publicEvidence'].append({**matches[0], 'id': ref['id'],
                **{key: source[key] for key in ('sourceUrl', 'periodStart', 'periodEnd')}})
        result['publicEvidence'].extend(analysis.get('evidence', []))
        result['checks'].update({
            'atMostOneModelRequest': inference['requestCount'] <= 1,
            'modelStructureAndEvidenceValidated': inference['status'] == 'validated',
            'everySentenceAccounted': analysis.get('inputCoverageComplete') is True,
            'analysisSourceMatches': analysis.get('source') == source,
            'sharedQuoteBudgetPreserved': analysis.get('publicQuoteBudget', {}).get('combinedQuoteCount', 9) <= 8
                and analysis.get('publicQuoteBudget', {}).get('combinedQuoteChars', 1281) <= 1280,
            'semanticCompletenessNotInferred': analysis.get('semanticAnalysisComplete') is False
                and analysis.get('semanticCorrectnessVerified') is False,
        })
        if inference['status'] == 'validated' and all(result['checks'].values()):
            result.update(status='candidate-validated', dataStatus='candidate', executionSuccess=True)
        else:
            result.update(status=inference['status'], dataStatus='partial', executionSuccess=False,
                          errorCode=inference['errorCode'] or 'MODEL_VALIDATION_FAILED')
    except Exception as error:
        known = {'PDF_INVALID', 'PDF_HTTP_REJECTED', 'PDF_SIZE_LIMIT', 'PDF_SOURCE_IDENTITY_INVALID',
                 'SOURCE_QUOTE_POOL_INVALID', 'SEMANTIC_PACKET_NOT_READY', 'SOURCE_BINDING_FAILED'}
        result.update(status='failed', dataStatus='partial', executionSuccess=False,
                      errorCode=str(error) if type(error) is ValueError and str(error) in known else 'VERIFICATION_FAILED')
    result['generatedAt'] = now()
    checkpoint(result)
    return result


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('ACTIONS_REQUIRED')
    # The only credential name this entry point reads. Its value stays in memory.
    credential = os.environ.get('DEEPSEEK_API_KEY')
    result = verify(credential, checkpoint=write_result)
    raise SystemExit(0 if result['executionSuccess'] else 1)


if __name__ == '__main__':
    main()
