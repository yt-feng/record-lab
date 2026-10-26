"""Actions-only two-PDF locator verification with a cached negative replay.

Only the compact result is written. No production catalog, report, status or
retry ledger is modified; original PDF bytes and extracted pages stay in RAM.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from unittest.mock import patch

from collect import collect_fund
from portfolio_identity import normalize_portfolio_identity
from providers import DataError, PublicProvider
from publication_guard import inspect_content
from report_extract import mineru_tokens_from_env
from report_locators import ENDPOINT, PERIODS, candidate_id, locator_for, normalize_candidate
from source_dates import china_today
from traversal import build_status, valid_status


# The original announcement observations came from the bounded CNinfo Actions
# discovery. They are not restamped at verification time.
SAMPLES = (
    {'announcementId': '1225416373', 'codes': ['168401'], 'label': '红土创新精选LOF',
     'title': '红土创新转型精选灵活配置混合型证券投资基金（LOF）2026年第2季度报告',
     'publishedAt': '2026-07-10', 'sourceUrl': 'https://static.cninfo.com.cn/finalpage/2026-07-10/1225416373.PDF',
     'targetPeriod': '2026Q2', 'correctionLabel': False, 'pdfVerified': False,
     'retrievedAt': '2026-10-26T15:22:35.404133Z',
     'sourcePage': {'start': '2026-07-08', 'end': '2026-07-14', 'page': 1,
         'contentHash': '1b80452070e0fe26f75eb6ad5374de0cfe41a8b23aea35e63a25cebffe4236e5', 'splitProbe': False}},
    {'announcementId': '1225614564', 'codes': ['159366'], 'label': '港股医疗ETF永赢',
     'title': '永赢中证港股通医疗主题交易型开放式指数证券投资基金2026年第3季度报告',
     'publishedAt': '2026-10-22', 'sourceUrl': 'https://static.cninfo.com.cn/finalpage/2026-10-22/1225614564.PDF',
     'targetPeriod': '2026Q3', 'correctionLabel': False, 'pdfVerified': False,
     'retrievedAt': '2026-10-26T15:24:17.948206Z',
     'sourcePage': {'start': '2026-10-22', 'end': '2026-10-26', 'page': 7,
         'contentHash': '38fdbd05fb13628c16c313716948d3a0b80c17046157e1324cac430c0ebdd245', 'splitProbe': False}},
)
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('.cache/locator-verification/result.json')
MAX_OUTPUT_BYTES = 32_000
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_IDENTITY_CONTEXT_CHARACTERS = 300
ERRORS = {'ACTIONS_REQUIRED', 'INVALID_FIXED_SAMPLE', 'UNEXPECTED_REQUEST', 'RESPONSE_TOO_LARGE',
          'INVALID_PDF', 'PDF_PARSE_FAILED', 'PDF_TEXT_EMPTY', 'HTTP_REJECTED', 'HTTP_ERROR',
          'TLS_FAILED', 'TIMEOUT', 'NETWORK_ERROR', 'EMPTY_RESPONSE', 'REPORT_IDENTITY_MISMATCH',
          'PARSER_CONFIGURATION_UNSUPPORTED', 'SNAPSHOT_BRIDGE_FAILED', 'INVALID_STATUS',
          'LOCATOR_VERIFICATION_FAILED', 'PUBLICATION_REJECTED', 'VERIFICATION_TOO_LARGE'}
NODE_BRIDGE = """const fs=require('node:fs');
try {const {buildSnapshot}=require('./scripts/build-data');const x=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(buildSnapshot(x.raw,{config:x.config,now:x.now})));}
catch(_){process.stdout.write('{}');process.exitCode=1;}
"""


class VerificationError(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else 'LOCATOR_VERIFICATION_FAILED'
        super().__init__(self.code)


def identity_diagnostic(pages, document, value, branch, *, page_numbers=None, retrieved_at=None, pdf_sha256=None):
    """A bounded view of the same parser input, with no second PDF extraction."""
    from portfolio_identity import HEADINGS, LABELS, PERIOD, _period, _spaced
    numbers = list(range(1, len(pages) + 1)) if page_numbers is None else page_numbers
    mapped = [(text, number if type(number) is int and number > 0 else None)
              for text, number in zip(pages[:12], numbers[:12])]
    contexts, seen, remaining = [], set(), MAX_IDENTITY_CONTEXT_CHARACTERS
    patterns = [('code-label', re.compile(r'(?:' + '|'.join(_spaced(label) for labels in LABELS.values() for label in labels)
                + r'|交\s*易\s*代\s*码|基\s*金\s*代\s*码)'), 100),
                ('identity-heading', re.compile(r'(?:§\s*2\s*基\s*金\s*产\s*品\s*概\s*况|2\.1\s*基\s*金\s*基\s*本\s*情\s*况)'), 70),
                ('period-title', PERIOD, 55),
                ('end-heading', re.compile(r'(?:§\s*3|2\.2)\s*[^\n|]{2,70}'), 60)]
    for label, pattern, limit in patterns:
        for text, page in mapped:
            for match in pattern.finditer(text):
                key = (label, page, match.start())
                if key in seen or remaining <= 0 or len(contexts) >= 6: continue
                seen.add(key)
                start = max(0, match.start() - 10)
                quote = text[start:start + min(limit, remaining)]
                if not quote: continue
                contexts.append({'label': label, 'page': page, 'text': quote})
                remaining -= len(quote)
    period_hits = []
    code_hits = []
    for text, page in mapped:
        lines = text.splitlines()
        for line in [*lines[:8], *lines[-8:]]:
            if len(line) > 180: continue
            for match in PERIOD.finditer(line):
                parsed = _period(match[0])
                hit = {'page': page, 'kind': parsed[0], 'periodEnd': parsed[1]} if parsed else None
                if hit and hit not in period_hits and len(period_hits) < 16: period_hits.append(hit)
        for match in re.finditer(r'(?<![0-9])[0-9]{6}(?![0-9])', text):
            if match[0] != document.get('fundCode'): continue
            hit = {'code': match[0], 'page': page}
            if hit not in code_hits and len(code_hits) < 12: code_hits.append(hit)
    combined = ''.join(str(text or '') + '\n' for text, _ in mapped)
    headings = list(HEADINGS.finditer(combined))
    return {'schemaVersion': 1, 'branch': branch.get('branch', 'DIAGNOSTIC_NOT_RECORDED'), 'field': branch.get('field'),
        'sourceUrl': document['sourceUrl'], 'targetPeriodEnd': document['periodEnd'], 'requestedCode': document.get('fundCode'),
        'retrievedAt': retrieved_at, 'pdfSha256': pdf_sha256,
        'sourceTextHash': hashlib.sha256('\n\n'.join(str(page or '').strip() for page in pages if str(page or '').strip()).encode()).hexdigest(),
        'inspectedPhysicalPages': [page for _, page in mapped], 'providedPageCount': len(pages),
        'periodCandidates': period_hits, 'requestedCodeOccurrences': code_hits,
        'strictHeadingCount': len(headings), 'identityParsed': bool(value),
        'parsedCodes': [value['sourcePortfolioCode'], *value['shareClassCodes']] if value else [],
        'requestedCodeMatched': bool(value and document.get('fundCode') in [value['sourcePortfolioCode'], *value['shareClassCodes']]),
        'contextCharacters': MAX_IDENTITY_CONTEXT_CHARACTERS - remaining, 'contexts': contexts}


class FixedPdfProvider(PublicProvider):
    """No profile, holdings, lifecycle or price HTTP requests in this verifier."""
    def __init__(self, sample):
        if os.environ.get('GITHUB_ACTIONS') != 'true': raise VerificationError('ACTIONS_REQUIRED')
        if sample not in SAMPLES: raise VerificationError('INVALID_FIXED_SAMPLE')
        super().__init__(timeout=18, attempts=1)
        self.sample = sample
        self.pdf_requests = 0
        self.pdf_bytes = 0
        self.pdf_sha256 = None
        self.details = None
        self.identity_diagnostic = None

    def overview(self, _code):
        return {'name': self.sample['label'], 'company': '', 'managers': []}

    def announcements(self, *_args): return []
    def holdings(self, *_args): return {}
    def lifecycle_evidence(self, *_args, **_kwargs): return {'notApplicableEvidence': None, 'diagnostics': []}

    def get(self, url, **kwargs):
        if os.environ.get('GITHUB_ACTIONS') != 'true': raise VerificationError('ACTIONS_REQUIRED')
        if url != self.sample['sourceUrl'] or kwargs.get('binary') is not True or kwargs.get('params') or self.pdf_requests:
            raise DataError('UNEXPECTED_REQUEST')
        self.pdf_requests += 1
        data = super().get(url, **kwargs)
        if len(data) > MAX_PDF_BYTES: raise DataError('RESPONSE_TOO_LARGE')
        if not data.startswith(b'%PDF'): raise DataError('INVALID_PDF')
        self.pdf_bytes, self.pdf_sha256 = len(data), hashlib.sha256(data).hexdigest()
        return data

    def report_details(self, report, max_pages=80):
        import portfolio_identity
        parser = portfolio_identity.parse_portfolio_identity
        def observed_parser(pages, document, **kwargs):
            branch = {}
            value = parser(pages, document, **kwargs, diagnostics=branch)
            self.identity_diagnostic = identity_diagnostic(pages, {**document, 'fundCode': report['fundCode']}, value, branch,
                **{key: kwargs.get(key) for key in ('page_numbers', 'retrieved_at', 'pdf_sha256')})
            return value
        with patch.object(portfolio_identity, 'parse_portfolio_identity', observed_parser):
            self.details = super().report_details(report, max_pages=min(max_pages, 80))
        return copy.deepcopy(self.details)

    def price_bars(self, *_args): raise VerificationError('UNEXPECTED_REQUEST')


class MissingIdentityReplay(FixedPdfProvider):
    def __init__(self, sample, details):
        super().__init__(sample)
        self.details = copy.deepcopy(details)
        self.details.pop('portfolioIdentityEvidence', None)

    def get(self, *_args, **_kwargs): raise VerificationError('UNEXPECTED_REQUEST')
    def report_details(self, *_args, **_kwargs): return copy.deepcopy(self.details)


def batch_config(sample, planned_at):
    period = PERIODS[sample['targetPeriod']]
    candidate = {**copy.deepcopy(sample), 'discoverySourceUrl': ENDPOINT}
    candidate['candidateId'] = candidate_id(candidate)
    clean = normalize_candidate(candidate, period, now=planned_at, code=sample['codes'][0])
    if not clean: raise VerificationError('INVALID_FIXED_SAMPLE')
    locator = locator_for(sample['codes'][0], period, [clean])
    baseline = {'2026Q2': '2026-03-31', '2026Q3': '2026-06-30'}[sample['targetPeriod']]
    return {'targetPeriodEnd': period, 'baselinePeriodEnd': baseline, 'requireReportQuality': True,
            'funds': [{'code': sample['codes'][0], 'reportLocator': locator, 'reportLocatorOnly': True}],
            'traversal': {'plannedAt': planned_at.isoformat(), 'reportLocatorCodes': [sample['codes'][0]]}}


def build_snapshot(raw, config, now):
    try:
        process = subprocess.run(['node', '-e', NODE_BRIDGE], cwd=ROOT, text=True,
            input=json.dumps({'raw': raw, 'config': config, 'now': now.isoformat()}, ensure_ascii=False),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
        if process.returncode or len(process.stdout) > 4_000_000: raise VerificationError('SNAPSHOT_BRIDGE_FAILED')
        value = json.loads(process.stdout)
        if not isinstance(value, dict) or value.get('sourceMode') != 'actions': raise VerificationError('SNAPSHOT_BRIDGE_FAILED')
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise VerificationError('SNAPSHOT_BRIDGE_FAILED') from None


def collect_build_status(sample, source):
    planned = dt.datetime.now(dt.timezone.utc)
    config = batch_config(sample, planned)
    fund, errors = collect_fund(config['funds'][0], china_today(planned), provider=source,
        target_period=config['targetPeriodEnd'], baseline_period=config['baselinePeriodEnd'],
        deadline=time.monotonic() + 180)
    received = dt.datetime.now(dt.timezone.utc)
    raw = {'asOf': china_today(received), 'retrievedAt': received.isoformat(), 'funds': [fund], 'errors': errors, 'barsByStock': {}}
    snapshot = build_snapshot(raw, config, received)
    completed = dt.datetime.now(dt.timezone.utc)
    status = build_status(config, snapshot, completed)
    if not valid_status(status, config, completed): raise VerificationError('INVALID_STATUS')
    return config, fund, errors, snapshot, status


def summarize(config, raw_fund, errors, snapshot, status):
    fund = next((item for item in snapshot.get('funds', []) if item.get('code') == config['funds'][0]['code']), {})
    report = fund.get('selectedReport') or {}
    identity = normalize_portfolio_identity(report.get('portfolioIdentityEvidence'), report)
    locator = config['funds'][0]['reportLocator']
    candidate = locator['candidates'][0]
    receipts = snapshot.get('reportLocatorReceipts') or []
    actual_receipt = receipts[0] if len(receipts) == 1 else {}
    row = status['funds'][0]
    return {'fundCount': len(snapshot.get('funds', [])), 'freshFundCount': snapshot.get('coverage', {}).get('batchFreshFundCount', 0),
        'holdingCount': len(report.get('holdings') or []), 'identityVerified': bool(identity),
        'identityCodeMatched': bool(identity and config['funds'][0]['code'] in [identity['sourcePortfolioCode'], *identity['shareClassCodes']]),
        'identityPeriodMatched': bool(identity and identity['periodEnd'] == config['targetPeriodEnd']),
        'identitySourceMatched': bool(identity and identity['sourceUrl'] == candidate['sourceUrl']),
        'pdfIdentityRetrievedAt': identity['retrievedAt'] if identity else None,
        'pdfSha256': identity['pdfSha256'] if identity else None,
        'sourceLocatorPreserved': report.get('sourceLocatorEvidence') == candidate,
        'discoveryRetrievedAt': candidate['retrievedAt'], 'plannedAt': config['traversal']['plannedAt'],
        'reportRetrievedAt': fund.get('retrievedAt'), 'receiptCount': len(receipts),
        'attemptedAt': actual_receipt.get('attemptedAt'),
        'attemptedCandidateIds': actual_receipt.get('attemptedCandidateIds', []), 'locatorDigest': locator['digest'],
        'statusValid': True, 'status': row['status'], 'locatorAttempt': row.get('reportLocatorAttempt') is True,
        'sourceAttemptIsSeparate': all(row.get(key) is False for key in ('qualityAttempt', 'managerAttempt', 'strategyAttempt', 'enrichmentAttempt')),
        'identityMismatchReported': any(error.get('errorCode') == 'REPORT_IDENTITY_MISMATCH' for error in errors),
        'errorCodes': sorted({error.get('errorCode') for error in errors if error.get('errorCode') in ERRORS}),
        'unverifiedRawReportCount': len(raw_fund.get('reports') or [])}


def run_sample(sample, provider_factory=None):
    if os.environ.get('GITHUB_ACTIONS') != 'true': raise VerificationError('ACTIONS_REQUIRED')
    if sample not in SAMPLES: raise VerificationError('INVALID_FIXED_SAMPLE')
    if mineru_tokens_from_env(): raise VerificationError('PARSER_CONFIGURATION_UNSUPPORTED')
    source = (provider_factory or FixedPdfProvider)(sample)
    try:
        positive_args = collect_build_status(sample, source)
        positive = summarize(*positive_args)
        negative = None
        if isinstance(source.details, dict):
            replay = MissingIdentityReplay(sample, source.details)
            try:
                negative = summarize(*collect_build_status(sample, replay))
                negative.update(mode='cached-details-replay-no-network', pdfRequestCount=replay.pdf_requests)
            finally: replay.session.close()
        checks = {'oneRealPdfRequest': source.pdf_requests == 1, 'originalPdfHashMatches': source.pdf_sha256 is not None and source.pdf_sha256 == positive['pdfSha256'],
            'originalPdfIdentity': all(positive[key] for key in ('identityVerified', 'identityCodeMatched', 'identityPeriodMatched', 'identitySourceMatched')),
            'sourceObservationPreserved': positive['sourceLocatorPreserved'] and positive['discoveryRetrievedAt'] == sample['retrievedAt'],
            'receiptAndStatus': positive['receiptCount'] == 1 and positive['locatorAttempt'] and positive['statusValid'] and positive['sourceAttemptIsSeparate'],
            'formalReportAccepted': positive['fundCount'] == 1 and positive['freshFundCount'] == 1 and positive['status'] in {'complete', 'no-equities'},
            'missingIdentityRejected': bool(negative and negative['fundCount'] == 0 and negative['freshFundCount'] == 0
                and negative['unverifiedRawReportCount'] == 0 and negative['status'] == 'failed' and negative['identityMismatchReported']),
            'negativeReceiptOnly': bool(negative and negative['receiptCount'] == 1 and negative['locatorAttempt']
                and negative['statusValid'] and negative['sourceAttemptIsSeparate'] and negative['pdfRequestCount'] == 0)}
        return {'code': sample['codes'][0], 'targetPeriod': sample['targetPeriod'], 'publishedAt': sample['publishedAt'],
            'sourceUrl': sample['sourceUrl'], 'discoverySourceUrl': ENDPOINT, 'pdfRequestCount': source.pdf_requests,
            'pdfBytes': source.pdf_bytes, 'pdfSha256': source.pdf_sha256, 'checks': checks,
            'identityDiagnostic': source.identity_diagnostic,
            'positive': positive, 'negative': negative, 'passed': all(checks.values())}
    finally: source.session.close()


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'ACTIONS_REQUIRED'})); return 2
    results = []
    for sample in SAMPLES:
        try: result = run_sample(sample)
        except Exception as error:
            result = {'code': sample['codes'][0], 'targetPeriod': sample['targetPeriod'], 'sourceUrl': sample['sourceUrl'],
                'passed': False, 'errorCode': VerificationError(getattr(error, 'code', '')).code}
        results.append(result)
    passed = len(results) == 2 and all(result['passed'] for result in results)
    summary = {'schemaVersion': 1, 'status': 'verified' if passed else 'partial', 'observedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
        'scope': 'two-fixed-quarterly-pdfs-and-cached-identity-negative-replays', 'pdfRequestLimit': 2,
        'productionDataModified': False, 'allQuarterReportsVerified': False, 'historicalUniverseVerified': False, 'results': results}
    raw = json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    error = 'VERIFICATION_TOO_LARGE' if len(raw) > MAX_OUTPUT_BYTES else 'PUBLICATION_REJECTED' if inspect_content(raw) else None
    if error:
        raw = json.dumps({'status': 'failed', 'errorCode': error, 'productionDataModified': False}).encode(); passed = False
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp'); temporary.write_bytes(raw); temporary.replace(OUTPUT)
    print(json.dumps({'status': 'verified' if passed else 'partial', 'productionDataModified': False}))
    return 0 if passed else 1


if __name__ == '__main__': raise SystemExit(main())
