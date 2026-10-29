"""Read-only Actions diagnosis of two fixed report-retention cases.

Only two quarterly PDFs are fetched. A digest-bound prior part is an observed
mixed-report anchor, not an independently reparsed interim PDF. The replay
never supplies the quarter's zero-stock proof as interim evidence. No campaign,
market-price, model, or production-data writes are performed.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess

from collect import collect_fund
from portfolio_identity import normalize_portfolio_identity, parse_portfolio_identity
from providers import DataError, PublicProvider, parse_no_equities, parse_pdf_holdings, parse_pdf_pages
from publication_guard import inspect_content
from report_extract import extract_report
from source_dates import china_today

ROOT = Path(__file__).resolve().parents[1]
INPUT = Path('.cache/nonstock-retention-input/part.json')
OUTPUT = Path('.cache/nonstock-retention/result.json')
SOURCE_RUN = '40790220286'
SOURCE_PART_SHA256 = '93e417657ef089851bb4fc892a9725130f72fcc46913f19962fade616c2d25be'
MAX_INPUT = 12_000_000
MAX_PDF = 20_000_000
MAX_OUTPUT = 70_000
SAMPLES = (
    {'code': '003589', 'kind': 'quarterly', 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-21',
     'title': '东吴增鑫宝货币市场基金2026年第2季度报告',
     'sourceUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202607211827189276_1.pdf',
     'interimUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202608311828785689_1.pdf'},
    {'code': '508007', 'kind': 'quarterly', 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-21',
     'title': '中金山高集团高速公路封闭式基础设施证券投资基金2026年第2季度报告',
     'sourceUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202607201827145476_1.pdf',
     'interimUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202608271828542519_1.pdf'},
)
ERRORS = {'ANCHOR_DIGEST_MISMATCH', 'ANCHOR_INVALID', 'ANCHOR_PUBLICATION_MISMATCH', 'ANCHOR_PUBLICATION_MISSING',
          'HTTP_REJECTED', 'HTTP_ERROR', 'TLS_FAILED',
          'TIMEOUT', 'NETWORK_ERROR', 'INVALID_PDF', 'EMPTY_RESPONSE', 'RESPONSE_TOO_LARGE',
          'PDF_PARSE_FAILED', 'PDF_TEXT_EMPTY', 'SNAPSHOT_BRIDGE_FAILED', 'DIAGNOSTIC_FAILED'}
NODE_BRIDGE = """const fs=require('node:fs');
const {buildSnapshot}=require('./scripts/build-data');
try{const x=JSON.parse(fs.readFileSync(0,'utf8'));
const s=buildSnapshot(x.payload,{now:x.now,config:x.config});
process.stdout.write(JSON.stringify({fund:s.funds[0]||null,errorCount:s.errors.length,
errorCodes:[...new Set(s.errors.map(e=>e.errorCode).filter(e=>/^[A-Z_]+$/.test(e||'')))]}));
}catch(_){process.stdout.write('{}');process.exitCode=1;}
"""


def document(sample):
    return {**{key: sample[key] for key in ('title', 'kind', 'periodEnd', 'publishedAt', 'sourceUrl')},
            'fundCode': sample['code']}


def quarter_publication_metadata(fund, sample):
    """Bind the date to retained quarter-source metadata, never the URL's date.

    A partial strategy/manager extraction still retains announcement metadata;
    this check does not turn its incomplete contents into a verified proof.
    """
    rows = [('selectedReport', fund.get('selectedReport'))]
    rows.extend((f'reports[{index}]', report) for index, report in enumerate(fund.get('reports') or []))
    bindings = []
    for path, report in rows:
        if not isinstance(report, dict):
            continue
        candidates = [(path, report)]
        for field in ('strategySectionCoverage', 'managerTableCoverage', 'strategyClaimsIdentityEvidence'):
            candidates.append((path + '.' + field, report.get(field)))
        for field in ('strategyEvidencePool', 'strategyClaims'):
            value = report.get(field)
            if isinstance(value, dict):
                candidates.append((path + '.' + field + '.source', value.get('source')))
        if (report.get('reportPdfSourceUrl') == sample['sourceUrl']
                and report.get('reportPdfPeriodEnd') == sample['periodEnd']
                and report.get('reportPdfKind') == sample['kind'] and report.get('reportPdfPublishedAt') is not None):
            candidates.append((path + '.reportPdfPublishedAt', {
                'sourceUrl': sample['sourceUrl'], 'kind': sample['kind'], 'periodEnd': sample['periodEnd'],
                'publishedAt': report['reportPdfPublishedAt']}))
        for field, value in candidates:
            if not isinstance(value, dict) or any(value.get(k) != sample[k] for k in ('sourceUrl', 'kind', 'periodEnd')):
                continue
            if value.get('publishedAt') is None:
                continue
            if value['publishedAt'] != sample['publishedAt']:
                raise DataError('ANCHOR_PUBLICATION_MISMATCH')
            bindings.append({'field': field, 'publishedAt': value['publishedAt']})
    if not bindings:
        raise DataError('ANCHOR_PUBLICATION_MISSING')
    return bindings


def load_anchors(path, *, expected_digest=SOURCE_PART_SHA256):
    """Reject an altered or oversized artifact before provider creation."""
    try:
        if path.stat().st_size > MAX_INPUT:
            raise DataError('ANCHOR_INVALID')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise DataError('ANCHOR_DIGEST_MISMATCH')
        rows = json.loads(data)['funds']
        anchors = {}
        for sample in SAMPLES:
            matches = [fund for fund in rows if fund.get('code') == sample['code']]
            if len(matches) != 1:
                raise DataError('ANCHOR_INVALID')
            fund = matches[0]
            report = fund.get('selectedReport') or {}
            if (report.get('sourceUrl') != sample['interimUrl'] or report.get('kind') != 'semiannual'
                    or report.get('periodEnd') != sample['periodEnd'] or report.get('holdings')
                    or report.get('noEquitiesEvidence') or report.get('reportOnly') is not True
                    or report.get('reportPdfSourceUrl') != sample['sourceUrl']
                    or report.get('reportPdfKind') != 'quarterly'
                    or not re.fullmatch('[a-f0-9]{64}', str(report.get('sourceTextHash', '')))):
                raise DataError('ANCHOR_INVALID')
            quarter_publication_metadata(fund, sample)
            anchors[sample['code']] = copy.deepcopy(fund)
        return anchors
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise DataError('ANCHOR_INVALID') from None


def proof_summary(raw, doc):
    evidence = raw if isinstance(raw, dict) else {}
    keys = ('sourceUrl', 'kind', 'periodEnd', 'publishedAt')
    types = {'explicit-no-stock-detail', 'zero-stock-balance-sheet', 'empty-stock-detail-section',
             'exhaustive-nonstock-assets'}
    return {'present': bool(evidence), 'confirmed': evidence.get('confirmed') is True,
            'evidenceType': evidence.get('evidenceType') if evidence.get('evidenceType') in types else
                ('explicit-no-stock-statement' if evidence else None),
            'sourceMatchesDocument': bool(evidence) and all(evidence.get(k) == doc.get(k) for k in keys),
            'sourceUrl': evidence.get('sourceUrl') if evidence.get('sourceUrl') in
                {s[k] for s in SAMPLES for k in ('sourceUrl', 'interimUrl')} else None,
            'kind': evidence.get('kind') if evidence.get('kind') in {'quarterly', 'semiannual'} else None,
            'periodEnd': evidence.get('periodEnd') if evidence.get('periodEnd') == '2026-06-30' else None,
            'page': evidence.get('page') if type(evidence.get('page')) is int and 1 <= evidence['page'] <= 300 else None,
            'pages': [p for p in (evidence.get('pages') or [])[:6] if type(p) is int and 1 <= p <= 300]}


_HEADING = re.compile(r'^\s*(?:§\s*\d+|\d{1,2}(?:\.\d{1,2}){1,3})\s*[^\d\n]{2,120}$')
_ASSET = re.compile(r'期末基金资产组合|其他(?:各项)?资产构成|资产负债表|股票投资明细|基础设施(?:基金)?投资组合')
_PRIVATE = re.compile(r'基金经理|联系人|联系方式|电话号码|身份证|邮箱|电子邮件|@|https?://')


def bounded_table_windows(pages, numbers):
    """Retain at most six short table regions, never a report or manager section."""
    windows, budget = [], 1800
    for page, number in zip(pages, numbers):
        if type(number) is not int or not 1 <= number <= 300:
            continue
        lines = str(page).splitlines()
        for index, line in enumerate(lines):
            if (_PRIVATE.search(line) or not _ASSET.search(re.sub(r'\s+', '', line))
                    or not _HEADING.fullmatch(line.strip())):
                continue
            region, following = [line.strip()], None
            for item in lines[index + 1:]:
                if _HEADING.fullmatch(item.strip()):
                    following = item.strip()[:120] if not _PRIVATE.search(item) else None
                    break
                if _PRIVATE.search(item):
                    break
                region.append(item.strip())
            raw = '\n'.join(region).strip()
            excerpt = raw[:min(500, budget)]
            if not excerpt or inspect_content(excerpt.encode()):
                continue
            windows.append({'page': number, 'text': excerpt, 'textSha256': hashlib.sha256(excerpt.encode()).hexdigest(),
                            'truncated': len(excerpt) < len(raw), 'followingHeading': following,
                            'boundaryOnSamePage': following is not None})
            budget -= len(excerpt)
            if len(windows) >= 6 or budget <= 0:
                return windows
    return windows


def table_recognition(pages, numbers):
    """Expose fixed recognizer branches as counts, without dumping parser inputs."""
    from nonstock_assets import ASSET_TITLE, OTHER_TITLE, PERIOD, SECTION, _clean_page, _regions
    cleaned = [_clean_page(page) for page in pages]
    headings = [re.sub(r'\s+', '', hit['heading']) for page in cleaned for hit in SECTION.finditer(page)]
    return {'assetHeadingCount': sum(bool(ASSET_TITLE.fullmatch(value)) for value in headings),
            'otherAssetsHeadingCount': sum(bool(OTHER_TITLE.fullmatch(value)) for value in headings),
            'recognizedClosedAssetTables': len(list(_regions(cleaned, list(numbers)))),
            'recognizedClosedOtherAssetsTables': len(list(_regions(cleaned, list(numbers), True))),
            'reportPeriodTitleCount': sum(len(list(PERIOD.finditer(re.sub(r'\s+', '', str(page)[:500])))) for page in pages)}


_IDENTITY_BRANCHES = {'VERIFIED', 'INVALID_INPUT', 'PAGE_MAPPING_LENGTH_MISMATCH', 'PERIOD_TITLE_NOT_FOUND',
    'PERIOD_TITLE_MISMATCH', 'PERIOD_TITLE_PAGE_UNKNOWN', 'IDENTITY_SECTION_START_COUNT',
    'IDENTITY_SECTION_END_NOT_FOUND', 'IDENTITY_SECTION_PAGE_MAPPING_INVALID', 'DUPLICATE_CODE_LABEL',
    'MAIN_CODE_LABEL_NOT_FOUND', 'CODE_ROWS_EXCEED_BOUND', 'CODE_ROW_INVALID', 'PROOF_VALIDATION_REJECTED'}
_STRUCTURE_HEADING = re.compile(r'^\s*(?:§\s*)?\d{1,2}(?:[.．]\d{1,2}){0,3}\s*[^\d\n]{2,100}$')
_STRUCTURE_TOPIC = re.compile(r'基金|基础设施|项目|资产|投资|财务|管理|净值|运营')
_CODE_LABEL = re.compile(r'^(?:基金主代码|基金代码|场内代码|交易代码|下属(?:分级|分类)基金的交易代码)[：:|]*')


def document_structure(pages, numbers, doc, *, pdf_sha256, now):
    """Expose bounded rejection evidence, never treat format signals as proof.

    The normal table diagnosis misses REIT-specific headings entirely. These
    excerpts identify the actual title, code labels and section boundaries so
    a later repair can target the observed format instead of assuming that a
    REIT holds no stocks. The existing identity recognizer remains unchanged.
    """
    from portfolio_identity import PERIOD, _period
    pages, numbers = list(pages), list(numbers)
    branch = {}
    identity = parse_portfolio_identity(pages, {k: v for k, v in doc.items() if k != 'fundCode'},
        page_numbers=numbers, retrieved_at=now.isoformat(), pdf_sha256=pdf_sha256, now=now, diagnostics=branch)
    result = {'identityRecognizer': {
        'branch': branch.get('branch') if branch.get('branch') in _IDENTITY_BRANCHES else 'UNKNOWN_BRANCH',
        'field': branch.get('field') if branch.get('field') in {'mainCode', 'shareClassCodes'} else None,
        'parsedPortfolioCode': (identity or {}).get('sourcePortfolioCode'),
        'requestedCodeMatched': bool(identity and doc['fundCode'] in
            [identity['sourcePortfolioCode'], *identity['shareClassCodes']])},
        'titleWindow': None, 'periodTitles': [], 'headings': [], 'codeRows': [],
        'formatSignals': {'infrastructureFundMentions': 0, 'infrastructureProjectHeadings': 0,
                          'standardProductOverviewHeadings': 0},
        'diagnosticOnly': True}
    heading_budget, title_seen, code_seen = 640, set(), set()
    for page, number in zip(pages, numbers):
        if type(number) is not int or not 1 <= number <= 300:
            continue
        lines = [line.strip() for line in str(page).splitlines()]
        compact_page = re.sub(r'\s+', '', str(page))
        result['formatSignals']['infrastructureFundMentions'] += len(re.findall(r'基础设施(?:证券投资)?基金|基础设施证券投资', compact_page))
        for index, line in enumerate(lines):
            compact = re.sub(r'\s+', '', line)
            if not line or _PRIVATE.search(compact) or inspect_content(line.encode()):
                continue
            for match in PERIOD.finditer(line):
                normalized = _period(match[0])
                if normalized and normalized not in title_seen and len(result['periodTitles']) < 4:
                    title_seen.add(normalized)
                    result['periodTitles'].append({'page': number, 'text': match[0][:80],
                        'kind': normalized[0], 'periodEnd': normalized[1],
                        'matchesExpected': normalized == (doc['kind'], doc['periodEnd'])})
                if result['titleWindow'] is None and number <= 2 and index < 50:
                    preceding = [v for v in lines[max(0, index - 6):index + 1] if v]
                    title = '\n'.join(preceding)
                    if len(title) <= 180 and not _PRIVATE.search(re.sub(r'\s+', '', title)) and not inspect_content(title.encode()):
                        result['titleWindow'] = {'page': number, 'text': title,
                            'textSha256': hashlib.sha256(title.encode()).hexdigest()}
            if len(line) <= 120 and _STRUCTURE_HEADING.fullmatch(line) and _STRUCTURE_TOPIC.search(compact):
                result['formatSignals']['infrastructureProjectHeadings'] += bool(re.search(r'基础设施|项目', compact))
                result['formatSignals']['standardProductOverviewHeadings'] += '基金产品概况' in compact
                if len(result['headings']) < 16 and len(line) <= heading_budget:
                    result['headings'].append({'page': number, 'text': line})
                    heading_budget -= len(line)
            label = _CODE_LABEL.match(compact)
            if label and len(result['codeRows']) < 4:
                # Only exact labels followed solely by six-digit codes are
                # retained; surrounding product or contact prose is omitted.
                pieces = [line]
                for following in lines[index + 1:index + 4]:
                    if not following:
                        continue
                    if not re.fullmatch(r'\d{6}(?:[\s|]+\d{6})*', following):
                        break
                    pieces.append(following)
                excerpt = '\n'.join(pieces)
                compact_tail = re.sub(r'\s+', '', excerpt)[label.end():]
                codes = re.findall(r'\d{6}', excerpt)
                if (len(excerpt) <= 100 and 1 <= len(codes) <= 6 and
                        re.fullmatch(r'(?:\d{6}[|]*)+', compact_tail) and excerpt not in code_seen):
                    code_seen.add(excerpt)
                    result['codeRows'].append({'page': number, 'text': excerpt, 'codes': codes,
                        'requestedCodeMentioned': doc['fundCode'] in codes, 'provesIdentity': False})
    return result


def extract_quarter(provider, sample, now):
    """One source request and the same pypdf extractor/enrichment as collection."""
    doc, private = document(sample), {}
    data = provider.get(doc['sourceUrl'], binary=True, attempts_override=1)
    if not isinstance(data, bytes) or not data.startswith(b'%PDF'):
        raise DataError('INVALID_PDF')
    if len(data) > MAX_PDF:
        raise DataError('RESPONSE_TOO_LARGE')
    def enrich(pages):
        holdings = parse_pdf_holdings(pages)
        return {'noEquitiesEvidence': parse_no_equities(pages, doc, include_nonstock=False),
                'pdfHoldings': holdings, 'pdfHoldingsMetadata': holdings.metadata}
    def capture(**context):
        private.update(windows=bounded_table_windows(context['pages'], context['page_numbers']),
                       tableRecognition=table_recognition(context['pages'], context['page_numbers']),
                       documentStructure=document_structure(context['pages'], context['page_numbers'], doc,
                           pdf_sha256=hashlib.sha256(data).hexdigest(), now=now))
    details = extract_report(data, doc['sourceUrl'], doc['periodEnd'], doc['kind'], parse_pages=parse_pdf_pages,
        filename=doc['title'], max_pages=300, env={}, enrich_pages=enrich, published_at=doc['publishedAt'],
        retrieved_at=now.isoformat(), on_private_sections=capture)
    details.update(reportPdfSourceUrl=doc['sourceUrl'], reportPdfPeriodEnd=doc['periodEnd'], reportPdfKind=doc['kind'])
    return details, {'pdfSha256': hashlib.sha256(data).hexdigest(), 'pdfBytes': len(data), **private}


class ReplayProvider:
    """Offline replay of an existing observation, without asserting a new interim parse."""
    def __init__(self, sample, quarter, anchor, mixed):
        self.sample, self.quarter, self.anchor, self.mixed = sample, quarter, anchor, mixed
        self.calls = []
        self.price_calls = 0

    def overview(self, _code):
        return {'name': self.anchor.get('name', self.sample['code']), 'company': self.anchor.get('company', ''), 'managers': []}

    def announcements(self, _code, _as_of):
        docs = [document(self.sample)]
        if self.mixed:
            prior = self.anchor['selectedReport']
            docs.append({key: prior[key] for key in ('title', 'kind', 'periodEnd', 'publishedAt', 'sourceUrl')})
        return docs

    def holdings(self, _code, _year):
        return {}

    def report_details(self, doc):
        self.calls.append(doc['sourceUrl'])
        if doc['sourceUrl'] == self.sample['sourceUrl']:
            return copy.deepcopy(self.quarter)
        if not self.mixed or doc['sourceUrl'] != self.sample['interimUrl']:
            raise DataError('ANCHOR_INVALID')
        prior = self.anchor['selectedReport']
        # These exact retained fields have their own reportPdfSourceUrl. In
        # this mixed anchor it is the quarter URL. No invented interim hash,
        # parsed pages, fresh retrieval time, or quarter zero-stock proof.
        keys = ('navYuan', 'narrative', 'parseMethod', 'parseErrorCode', 'sourceTextHash', 'pageStats',
                'strategyExcerpts', 'strategyThemes', 'strategySectionCoverage', 'strategyAnalysis',
                'strategyClaims', 'strategyEvidencePool', 'strategyEvidenceBudget', 'strategyEvidenceRetrievedAt',
                'reportPdfSourceUrl', 'reportPdfPeriodEnd', 'reportPdfKind', 'portfolioIdentityEvidence',
                'strategyClaimsIdentityEvidence', 'managerTableCoverage')
        result = {key: copy.deepcopy(prior[key]) for key in keys if key in prior}
        result['reportManagers'] = copy.deepcopy(prior.get('managers') or [])
        return result

    def price_bars(self, *_args, **_kwargs):
        self.price_calls += 1
        raise AssertionError('PRICE_REQUEST_FORBIDDEN')


def build_fund(fund, now):
    payload = {'asOf': china_today(now), 'retrievedAt': now.isoformat(), 'barsByStock': {}, 'errors': [], 'funds': [fund]}
    config = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31', 'funds': [{'code': fund['code']}]}
    try:
        proc = subprocess.run(['node', '-e', NODE_BRIDGE], cwd=ROOT,
            input=json.dumps({'payload': payload, 'config': config, 'now': now.isoformat()}, ensure_ascii=False),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
        if proc.returncode or len(proc.stdout) > 4_000_000:
            raise DataError('SNAPSHOT_BRIDGE_FAILED')
        result = json.loads(proc.stdout)
        if not isinstance(result.get('fund'), dict):
            raise DataError('SNAPSHOT_BRIDGE_FAILED')
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise DataError('SNAPSHOT_BRIDGE_FAILED') from None


def report_summary(report):
    report = report or {}
    return {key: report.get(key) for key in ('sourceUrl', 'kind', 'periodEnd', 'reportPdfSourceUrl', 'reportPdfKind',
            'sourceTextHash', 'reportOnly', 'noEquitiesConfirmed')} | {
            'holdingsCount': len(report.get('holdings') or []),
            'noEquitiesEvidence': proof_summary(report.get('noEquitiesEvidence'), report)}


def replay(sample, details, anchor, now, mixed):
    provider = ReplayProvider(sample, details, anchor, mixed)
    fund, errors = collect_fund({'code': sample['code'], 'companyId': anchor.get('companyId')}, china_today(now),
        provider=provider, target_period='2026-06-30', baseline_period='2026-03-31')
    result = build_fund(fund, now)
    return {'mode': 'retained-mixed-anchor-replay' if mixed else 'fresh-quarter-only',
            'independentInterimReparse': False, 'sourceHttpRequests': 0, 'priceRequests': provider.price_calls,
            'detailsReadOrder': provider.calls, 'collectorReports': [report_summary(r) for r in fund['reports']],
            'collectorErrorCodes': sorted(set(e['errorCode'] for e in errors)),
            'buildErrorCount': result['errorCount'], 'buildErrorCodes': result.get('errorCodes', []),
            'fundStatus': result['fund'].get('status'),
            'selectedReport': report_summary(result['fund'].get('selectedReport'))}


def run_diagnosis(provider, anchors, *, now=None, extract=extract_quarter):
    now = now or dt.datetime.now(dt.timezone.utc)
    results = []
    for sample in SAMPLES:
        item = {'code': sample['code'], 'sourceUrl': sample['sourceUrl'], 'periodEnd': sample['periodEnd']}
        try:
            details, extracted = extract(provider, sample, now)
            identity = normalize_portfolio_identity(details.get('portfolioIdentityEvidence'), document(sample), now=now)
            proof = proof_summary(details.get('noEquitiesEvidence'), document(sample))
            direct = replay(sample, details, anchors[sample['code']], now, False)
            mixed = replay(sample, details, anchors[sample['code']], now, True)
            drop = (proof['sourceMatchesDocument'] and direct['fundStatus'] == 'no-equities'
                    and direct['selectedReport']['sourceUrl'] == sample['sourceUrl']
                    and direct['selectedReport']['kind'] == sample['kind']
                    and not any(r['noEquitiesEvidence']['present'] for r in mixed['collectorReports']))
            retained = (proof['sourceMatchesDocument'] and mixed['fundStatus'] == 'no-equities'
                        and mixed['selectedReport']['sourceUrl'] == sample['sourceUrl']
                        and mixed['selectedReport']['kind'] == sample['kind'])
            item.update(status='diagnosed', classification='QUARTER_PROOF_DROPPED_BY_COLLECT' if drop else
                'QUARTER_PROOF_RETAINED_SEPARATELY' if retained else
                'QUARTER_PROOF_UNAVAILABLE' if not proof['present'] else 'REVIEW_RETAINED_PATH',
                quarterDetails={'noEquitiesEvidence': proof, 'parseMethod': details.get('parseMethod'),
                    'sourceTextHash': details.get('sourceTextHash'), 'pageStats': details.get('pageStats'),
                    'identityVerified': bool(identity), 'sourcePortfolioCode': (identity or {}).get('sourcePortfolioCode'),
                    'shareClassCodes': (identity or {}).get('shareClassCodes', []), **extracted},
                retainedAnchor={'sourceRunId': SOURCE_RUN, 'partSha256': SOURCE_PART_SHA256,
                    'quarterPublicationMetadata': quarter_publication_metadata(anchors[sample['code']], sample),
                    'originalEvidenceRetrievedAt': anchors[sample['code']]['selectedReport'].get('evidenceRetrievedAt'),
                    'independentInterimParseAvailable': False,
                    'selectedReport': report_summary(anchors[sample['code']]['selectedReport'])},
                quarterOnly=direct, mixedRetainedAnchor=mixed)
        except Exception as error:
            code = getattr(error, 'code', 'DIAGNOSTIC_FAILED')
            item.update(status='failed', errorCode=code if code in ERRORS else 'DIAGNOSTIC_FAILED')
        results.append(item)
    return {'schemaVersion': 1, 'observedAt': now.isoformat(), 'sourceRunId': SOURCE_RUN,
            'productionDataUpdated': False, 'provesFullCollection': False, 'independentInterimReparse': False,
            'marketRequests': 0, 'modelRequests': 0, 'sampleCount': len(results),
            'status': 'diagnosed' if all(r['status'] == 'diagnosed' for r in results) else 'failed', 'results': results}


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'ACTIONS_REQUIRED'}))
        return 2
    logging.disable(logging.CRITICAL)
    try:
        anchors = load_anchors(ROOT / INPUT)
        result = run_diagnosis(PublicProvider(timeout=18, attempts=1), anchors)
    except Exception as error:
        code = getattr(error, 'code', 'DIAGNOSTIC_FAILED')
        result = {'status': 'failed', 'errorCode': code if code in ERRORS else 'DIAGNOSTIC_FAILED',
                  'productionDataUpdated': False}
    encoded = (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode()
    if len(encoded) > MAX_OUTPUT or inspect_content(encoded):
        print(json.dumps({'status': 'failed', 'errorCode': 'DIAGNOSTIC_OUTPUT_REJECTED'}))
        return 1
    destination = ROOT / OUTPUT
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)
    print(json.dumps({'status': result['status'], 'sampleCount': result.get('sampleCount', 0)}))
    return 0 if result['status'] == 'diagnosed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
