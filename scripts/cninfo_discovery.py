#!/usr/bin/env python3
"""Bounded announcement metadata discovery, never a historical-universe claim.

Only the Actions transport makes requests. The parser/paginator accepts an
injected page fetcher for local synthetic tests. No PDF is fetched or executed.
"""
import argparse
from collections import Counter
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from publication_guard import inspect_content

ENDPOINT = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
ENTRY = 'https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?lastPage=index&url=disclosure%2Flist%2Fsearch'
SOURCE_SCRIPT = 'https://www.cninfo.com.cn/new/js/app/disclosure/notice/history-notice.js?v=20260710082532'
CHINA = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc
PAGE_SIZE = 100
MAX_PAGES_PER_SLICE = 20  # Deliberately below the site's UI limit of 100 pages.
MAX_REQUESTS = 200
MAX_ROWS = 40_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_SUMMARY_BYTES = 96_000
MAX_SHARD_BYTES = 2_000_000
MAX_ARTIFACT_BYTES = 64_000_000
MAX_SECONDS = 600
OUTPUT = Path('.cache/cninfo-discovery')


class DiscoveryError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def date_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'2026-\d{2}-\d{2}', value):
        raise DiscoveryError('INVALID_DATE')
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise DiscoveryError('INVALID_DATE') from None


def timestamp(value):
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 40:
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise DiscoveryError('INVALID_OBSERVATION_TIME') from None
    else:
        raise DiscoveryError('INVALID_OBSERVATION_TIME')
    if parsed.tzinfo is None:
        raise DiscoveryError('INVALID_OBSERVATION_TIME')
    return parsed.astimezone(UTC)


def stamp(value):
    return timestamp(value).isoformat().replace('+00:00', 'Z')


def integer(value, low=0, high=1_000_000):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def params_for(start, end, page, page_size=PAGE_SIZE):
    return {'pageNum': str(page), 'pageSize': str(page_size), 'column': 'fund',
            'tabName': 'fulltext', 'plate': '', 'stock': '', 'searchkey': '', 'secid': '',
            'category': 'category_jdbg_jjgg', 'trade': '',
            'seDate': f'{start.isoformat()}~{end.isoformat()}', 'sortName': '',
            'sortType': '', 'isHLtitle': 'true'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DiscoveryError('UNEXPECTED_REDIRECT')


class ActionsTransport:
    """Fixed public endpoint, bounded response/time; no credentials or retries."""
    def __init__(self, *, max_requests=MAX_REQUESTS, max_seconds=MAX_SECONDS):
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise DiscoveryError('ACTIONS_REQUIRED')
        if not integer(max_requests, 1, MAX_REQUESTS) or not integer(max_seconds, 1, MAX_SECONDS):
            raise DiscoveryError('INVALID_LIMIT')
        self.max_requests = max_requests
        self.deadline = time.monotonic() + max_seconds
        self.requests = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def __call__(self, params):
        if self.requests >= self.max_requests:
            raise DiscoveryError('REQUEST_LIMIT')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DiscoveryError('TIME_LIMIT')
        self.requests += 1
        req = urllib.request.Request(ENDPOINT, data=urllib.parse.urlencode(params).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json',
                     'Referer': ENTRY, 'User-Agent': 'RecordLab-PublicMetadataVerification/1.0'})
        try:
            with self.opener.open(req, timeout=min(15, remaining)) as response:
                if response.status != 200 or response.geturl() != ENDPOINT:
                    raise DiscoveryError('UNEXPECTED_HTTP_RESPONSE')
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                if content_type not in ('application/json', 'text/json'):
                    raise DiscoveryError('UNEXPECTED_CONTENT_TYPE')
                parts = []; size = 0
                while True:
                    if time.monotonic() > self.deadline:
                        raise DiscoveryError('TIME_LIMIT')
                    block = response.read(min(65_536, MAX_RESPONSE_BYTES + 1 - size))
                    if not block:
                        break
                    parts.append(block); size += len(block)
                    if size > MAX_RESPONSE_BYTES:
                        raise DiscoveryError('RESPONSE_TOO_LARGE')
                raw = b''.join(parts)
            payload = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except DiscoveryError:
            raise
        except urllib.error.HTTPError:
            raise DiscoveryError('HTTP_ERROR') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DiscoveryError('NETWORK_ERROR') from None
        except (ValueError, UnicodeError, RecursionError):
            raise DiscoveryError('INVALID_JSON') from None
        return {'payload': payload, 'contentHash': hashlib.sha256(raw).hexdigest(),
                'retrievedAt': stamp(dt.datetime.now(UTC)), 'responseBytes': len(raw)}


def clean_text(value, limit):
    if not isinstance(value, str) or len(value) > limit * 2:
        raise DiscoveryError('INVALID_ANNOUNCEMENT_TEXT')
    # The official query highlights search matches with <em>, even though this
    # adapter deliberately sends no keyword. Other markup is not retained.
    value = html.unescape(re.sub(r'</?em>', '', value, flags=re.I)).strip()
    if not value or len(value) > limit or re.search(r'[<>\x00-\x1f\x7f]', value):
        raise DiscoveryError('INVALID_ANNOUNCEMENT_TEXT')
    return value


def report_title(title, quarter):
    compact = re.sub(r'\s+', '', title)
    numerals = {2: '[2二]', 3: '[3三]'}[quarter]
    return (bool(re.search(rf'2026年(?:度)?第?{numerals}季度报告', compact))
            and bool(re.search(r'基金|ETF|LOF|FOF', compact, re.I))
            and not re.search(r'提示|公告|招募|合同', compact))


def pdf_url(value, observed_date):
    if not isinstance(value, str) or len(value) > 250:
        raise DiscoveryError('INVALID_PDF_URL')
    if value.startswith('finalpage/'):
        value = 'https://static.cninfo.com.cn/' + value
    match = re.fullmatch(r'https://static\.cninfo\.com\.cn/finalpage/(2026-\d{2}-\d{2})/([0-9]{1,20})\.[Pp][Dd][Ff]', value)
    if not match:
        raise DiscoveryError('INVALID_PDF_URL')
    try:
        file_date = date_value(match[1])
    except DiscoveryError:
        raise DiscoveryError('INVALID_PDF_URL') from None
    if file_date > observed_date:
        raise DiscoveryError('FUTURE_PDF_DATE')
    return value


def parse_announcement(row, *, period, start, end, retrieved_at):
    if not isinstance(row, dict):
        raise DiscoveryError('INVALID_ANNOUNCEMENT')
    aid = row.get('announcementId')
    if not isinstance(aid, str) or not re.fullmatch(r'[0-9]{1,20}', aid):
        raise DiscoveryError('INVALID_ANNOUNCEMENT_ID')
    title = clean_text(row.get('announcementTitle'), 300)
    published = row.get('announcementTime')
    if not integer(published, 1_700_000_000_000, 1_900_000_000_000):
        raise DiscoveryError('INVALID_PUBLICATION_DATE')
    published_date = dt.datetime.fromtimestamp(published / 1000, UTC).astimezone(CHINA).date()
    observed_date = timestamp(retrieved_at).astimezone(CHINA).date()
    if not start <= published_date <= end or published_date > observed_date:
        raise DiscoveryError('PUBLICATION_OUTSIDE_WINDOW')
    if not report_title(title, int(period[-1])):
        return None
    raw_codes = row.get('secCode')
    if not isinstance(raw_codes, str) or len(raw_codes) > 2_000:
        raise DiscoveryError('INVALID_FUND_CODES')
    codes = [code.strip() for code in raw_codes.split(',')]
    if not codes or len(codes) > 100 or any(not re.fullmatch(r'[0-9]{6}', code) for code in codes):
        raise DiscoveryError('INVALID_FUND_CODES')
    if len(set(codes)) != len(codes):
        raise DiscoveryError('DUPLICATE_CODE_IN_ANNOUNCEMENT')
    label = clean_text(row.get('secName'), 200) if len(codes) == 1 else None
    url = pdf_url(row.get('adjunctUrl'), observed_date)
    if str(row.get('adjunctType', '')).upper() != 'PDF':
        raise DiscoveryError('INVALID_PDF_TYPE')
    return {'announcementId': aid, 'codes': codes, 'label': label, 'title': title,
            'publishedAt': published_date.isoformat(), 'sourceUrl': url,
            'targetPeriod': period, 'titleMatchesTargetPeriod': True,
            'correctionLabel': bool(re.search(r'更正|修订', title)), 'pdfVerified': False,
            'retrievedAt': stamp(retrieved_at)}


def window_for(period, as_of, today, end=None, include_q2_late=False):
    if period not in ('2026Q2', '2026Q3'):
        raise DiscoveryError('UNSUPPORTED_PERIOD')
    effective_as_of = min(date_value(as_of), today)
    start = dt.date(2026, 7 if period == '2026Q2' else 10, 1)
    default_end = dt.date(2026, 7, 31) if period == '2026Q2' and not include_q2_late else effective_as_of
    last = min(effective_as_of, date_value(end) if end is not None else default_end)
    if last < start:
        raise DiscoveryError('WINDOW_NOT_STARTED')
    return start, last, effective_as_of


def discover_period(period, *, as_of, fetch_page, observed_at, window_end=None,
                    include_q2_late=False, page_size=PAGE_SIZE,
                    max_pages=MAX_PAGES_PER_SLICE, max_requests=MAX_REQUESTS):
    """Enumerate one explicit announcement window; retain valid partial evidence.

    `windowEnumerationComplete` describes this source/filter/window only. It is
    never a statement that all funds or all target-quarter reports were found.
    """
    observed = timestamp(observed_at)
    if not integer(page_size, 1, PAGE_SIZE) or not integer(max_pages, 1, MAX_PAGES_PER_SLICE) or not integer(max_requests, 1, MAX_REQUESTS):
        raise DiscoveryError('INVALID_LIMIT')
    start, end, effective_as_of = window_for(period, as_of, observed.astimezone(CHINA).date(), window_end, include_q2_late)
    queue = []; cursor = start
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=6), end)
        queue.append((cursor, stop, None)); cursor = stop + dt.timedelta(days=1)
    pages = []; slices = []; announcements = {}; observed_ids = set(); probe_ids = set(); conflicts = set()
    rejects = Counter(); probe_rejects = Counter(); filtered = 0; duplicates = 0; requests = 0; raw_rows = 0
    fatal = None

    def fetch(first, last, number):
        nonlocal requests
        if requests >= max_requests:
            raise DiscoveryError('REQUEST_LIMIT')
        requests += 1
        receipt = fetch_page(params_for(first, last, number, page_size))
        if not isinstance(receipt, dict):
            raise DiscoveryError('INVALID_PAGE_RECEIPT')
        at = timestamp(receipt.get('retrievedAt'))
        digest = receipt.get('contentHash')
        size = receipt.get('responseBytes')
        if at < observed or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or not integer(size, 1, MAX_RESPONSE_BYTES):
            raise DiscoveryError('INVALID_PAGE_RECEIPT')
        payload = receipt.get('payload')
        if not isinstance(payload, dict) or not integer(payload.get('totalRecordNum')) or not integer(payload.get('totalAnnouncement')):
            raise DiscoveryError('INVALID_PAGE_SCHEMA')
        rows = payload.get('announcements')
        if rows is None and payload['totalRecordNum'] == 0:
            rows = []
        if not isinstance(rows, list) or len(rows) > page_size or not isinstance(payload.get('hasMore'), bool):
            raise DiscoveryError('INVALID_PAGE_SCHEMA')
        audit = {'start': first.isoformat(), 'end': last.isoformat(), 'page': number,
                 'rowCount': len(rows), 'totalRecordNum': payload['totalRecordNum'],
                 'totalAnnouncement': payload['totalAnnouncement'], 'hasMore': payload['hasMore'],
                 'contentHash': digest, 'retrievedAt': stamp(at), 'responseBytes': size}
        pages.append(audit)
        return rows, payload, audit

    def retain(row, first, last, number, audit, descriptor, *, probe=False):
        nonlocal filtered
        aid = row.get('announcementId') if isinstance(row, dict) else None
        if probe and isinstance(aid, str) and re.fullmatch(r'[0-9]{1,20}', aid):
            probe_ids.add(aid)
        try:
            record = parse_announcement(row, period=period, start=first, end=last, retrieved_at=audit['retrievedAt'])
            if record is None:
                if not probe: filtered += 1
                return
            record['sourcePage'] = {'start': first.isoformat(), 'end': last.isoformat(),
                                    'page': number, 'contentHash': audit['contentHash'], 'splitProbe': probe}
            if aid in announcements:
                keys = ('codes', 'title', 'publishedAt', 'sourceUrl', 'label')
                if any(announcements[aid][key] != record[key] for key in keys):
                    conflicts.add(aid); descriptor['errors'].append('CONFLICTING_ANNOUNCEMENT_ID')
                elif not probe and announcements[aid]['sourcePage']['splitProbe']:
                    announcements[aid] = record
            else:
                announcements[aid] = record
        except DiscoveryError as error:
            if probe:
                probe_rejects[error.code] += 1
            else:
                rejects[error.code] += 1; descriptor['errors'].append('REJECTED_ROW')

    while queue:
        first, last, parent = queue.pop(0)
        descriptor = {'start': first.isoformat(), 'end': last.isoformat(), 'status': 'partial',
                      'sliceId': f'{first.isoformat()}~{last.isoformat()}', 'parentSlice': parent,
                      'expectedRows': None, 'observedRows': 0, 'uniqueRows': 0, 'errors': []}
        slices.append(descriptor)
        try:
            rows, payload, audit = fetch(first, last, 1)
            total = payload['totalRecordNum']; descriptor['expectedRows'] = total
            if total > page_size * max_pages:
                if first < last:
                    # A split decision is still a real successful observation.
                    # Keep valid candidates even if all child requests fail;
                    # probe rows do not satisfy any leaf pagination proof.
                    for row in rows: retain(row, first, last, 1, audit, descriptor, probe=True)
                    middle = first + (last - first) // 2
                    queue[0:0] = [(first, middle, descriptor['sliceId']),
                                  (middle + dt.timedelta(days=1), last, descriptor['sliceId'])]
                    descriptor['status'] = 'split'
                    continue
                descriptor['errors'].append('DAILY_PAGE_LIMIT')
            count = min(max(1, math.ceil(total / page_size)), max_pages)
            local_ids = set(); expected_announcements = payload['totalAnnouncement']
            for number in range(1, count + 1):
                if number > 1:
                    rows, payload, audit = fetch(first, last, number)
                if payload['totalRecordNum'] != total or payload['totalAnnouncement'] != expected_announcements:
                    descriptor['errors'].append('TOTAL_CHANGED')
                expected_length = min(page_size, max(0, total - (number - 1) * page_size))
                if len(rows) != expected_length:
                    descriptor['errors'].append('PAGE_ROW_COUNT_MISMATCH')
                if payload['hasMore'] != (number * page_size < total):
                    descriptor['errors'].append('HAS_MORE_MISMATCH')
                for row in rows:
                    raw_rows += 1; descriptor['observedRows'] += 1
                    if raw_rows > MAX_ROWS:
                        raise DiscoveryError('ROW_LIMIT')
                    aid = row.get('announcementId') if isinstance(row, dict) else None
                    valid_id = isinstance(aid, str) and re.fullmatch(r'[0-9]{1,20}', aid)
                    if valid_id:
                        if aid in local_ids or aid in observed_ids:
                            duplicates += 1; descriptor['errors'].append('DUPLICATE_ANNOUNCEMENT_ID')
                        local_ids.add(aid); observed_ids.add(aid)
                    retain(row, first, last, number, audit, descriptor)
                descriptor['uniqueRows'] = len(local_ids)
            if descriptor['uniqueRows'] != total:
                descriptor['errors'].append('UNIQUE_ROW_COUNT_MISMATCH')
            descriptor['errors'] = sorted(set(descriptor['errors']))
            descriptor['status'] = 'complete' if not descriptor['errors'] else 'partial'
        except DiscoveryError as error:
            descriptor['errors'] = sorted(set(descriptor['errors'] + [error.code]))
            if error.code in ('REQUEST_LIMIT', 'TIME_LIMIT', 'ROW_LIMIT'):
                fatal = error.code
                slices.extend({'start': a.isoformat(), 'end': b.isoformat(), 'status': 'unattempted',
                               'sliceId': f'{a.isoformat()}~{b.isoformat()}', 'parentSlice': parent_id,
                               'expectedRows': None, 'observedRows': 0, 'uniqueRows': 0,
                               'errors': [error.code]} for a, b, parent_id in queue)
                queue.clear()
        except Exception:
            descriptor['errors'] = sorted(set(descriptor['errors'] + ['PAGE_FETCH_FAILED']))
    # A moving source can return a smaller result after a split. Checking only
    # the sampled probe IDs would miss unseen withdrawn rows: every split node
    # must conserve its declared count across its two disjoint child windows.
    for descriptor in reversed(slices):
        if descriptor['status'] != 'split': continue
        children = [item for item in slices if item['parentSlice'] == descriptor['sliceId']]
        counts_known = len(children) == 2 and all(integer(item['expectedRows']) for item in children)
        descriptor['childDeclaredRows'] = sum(item['expectedRows'] for item in children) if counts_known else None
        if counts_known and descriptor['childDeclaredRows'] != descriptor['expectedRows']:
            descriptor['errors'].append('TOTAL_CHANGED_AFTER_SPLIT')
        if len(children) != 2 or any(item['status'] not in ('complete', 'split') for item in children):
            descriptor['errors'].append('INCOMPLETE_CHILD_SLICES')
        if descriptor['errors']:
            descriptor['errors'] = sorted(set(descriptor['errors']))
            descriptor['status'] = 'partial'
    records = [record for aid, record in sorted(announcements.items()) if aid not in conflicts]
    candidates = {}
    for record in records:
        for code in record['codes']:
            candidate = candidates.setdefault(code, {'code': code, 'label': None, 'announcementIds': [],
                'recordUnit': 'announcement-associated-code', 'periodExistenceVerified': False,
                'pdfVerified': False, 'historicalUniverseVerified': False})
            candidate['announcementIds'].append(record['announcementId'])
            if len(record['codes']) == 1 and candidate['label'] is None:
                candidate['label'] = record['label']
    unresolved_probes = probe_ids - observed_ids
    complete = not conflicts and not unresolved_probes and all(item['status'] in ('complete', 'split') for item in slices)
    return {'schemaVersion': 1, 'targetPeriod': period, 'asOf': effective_as_of.isoformat(),
            'observedAt': stamp(observed), 'sourceUrl': ENDPOINT, 'entryUrl': ENTRY,
            'sourceScriptUrl': SOURCE_SCRIPT, 'discoveryWindow': {'start': start.isoformat(), 'end': end.isoformat(),
                'basis': 'announcement-publication-date', 'scope': 'supplementary-source-window'},
            'query': {'column': 'fund', 'tabName': 'fulltext', 'category': 'category_jdbg_jjgg', 'searchkey': '', 'pageSize': page_size},
            'status': 'window-enumerated' if complete else 'partial', 'windowEnumerationComplete': complete,
            'allQuarterReportsVerified': False, 'historicalUniverseVerified': False, 'productionCatalogModified': False,
            'counts': {'requestCount': requests, 'receivedRows': sum(page['rowCount'] for page in pages),
                'enumeratedRows': raw_rows, 'uniqueAnnouncementIds': len(observed_ids | probe_ids),
                'enumeratedUniqueAnnouncementIds': len(observed_ids), 'unconfirmedProbeAnnouncementIds': len(unresolved_probes),
                'targetReports': len(records), 'candidateCodes': len(candidates), 'filteredNonTargetRows': filtered,
                'rejectedRows': sum(rejects.values()), 'duplicateRows': duplicates, 'conflictingAnnouncementIds': len(conflicts)},
            'rejectionReasons': dict(sorted(rejects.items())), 'probeRejectionReasons': dict(sorted(probe_rejects.items())),
            'limitReached': fatal,
            'slices': slices, 'pages': pages, 'announcements': records,
            'candidates': [candidates[code] for code in sorted(candidates)],
            'limitations': ['仅枚举指定来源、筛选条件和公告日期窗口，不证明全行业报告或历史产品目录完整。',
                '证券代码来自公告列表关联关系，PDF身份、份额类别与季末存续尚未核验。',
                '标题不符目标季度的公告不纳入候选；未下载PDF；未请求公告关联代码展开接口。']}


def artifacts_for(results):
    summaries = [{key: value for key, value in result.items() if key not in ('announcements', 'candidates')} for result in results]
    common = {'schemaVersion': 1, 'historicalUniverseVerified': False, 'allQuarterReportsVerified': False,
              'productionCatalogModified': False}
    encoded = {}; manifests = []

    def encode(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()

    def publish(name, value, limit):
        data = encode(value)
        if len(data) > limit:
            raise DiscoveryError('ARTIFACT_TOO_LARGE')
        if inspect_content(data):
            raise DiscoveryError('PUBLICATION_REJECTED')
        encoded[name] = data
        if sum(len(part) for part in encoded.values()) > MAX_ARTIFACT_BYTES:
            raise DiscoveryError('ARTIFACT_TOTAL_LIMIT')
        return {'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    for result in results:
        manifest = {'targetPeriod': result['targetPeriod'], 'discoveryWindow': result['discoveryWindow'],
                    'windowEnumerationComplete': result['windowEnumerationComplete'], 'sourceUrl': ENDPOINT,
                    'announcementCount': len(result['announcements']), 'candidateCodeCount': len(result['candidates']), 'files': []}
        for kind, rows in (('announcements', result['announcements']), ('codes', result['candidates'])):
            header = common | {'targetPeriod': result['targetPeriod'], 'sourceUrl': ENDPOINT, 'kind': kind, 'rows': []}
            chunk = []; size = len(encode(header)); index = 0
            def flush():
                nonlocal index, chunk, size
                if not chunk: return
                index += 1
                if index > 100: raise DiscoveryError('SHARD_LIMIT')
                name = f'{kind}-{result["targetPeriod"]}-{index:04d}.json'
                descriptor = publish(name, header | {'rows': chunk}, MAX_SHARD_BYTES)
                manifest['files'].append(descriptor | {'kind': kind, 'rowCount': len(chunk)})
                chunk = []; size = len(encode(header))
            for row in rows:
                row_size = len(encode(row)) + 1
                if size + row_size > MAX_SHARD_BYTES: flush()
                if size + row_size > MAX_SHARD_BYTES: raise DiscoveryError('ROW_TOO_LARGE')
                chunk.append(row); size += row_size
            flush()
        manifests.append(manifest)
    publish('candidates.json', common | {'artifactKind': 'candidate-manifest', 'periods': manifests}, MAX_SUMMARY_BYTES)
    publish('summary.json', common | {'periods': summaries}, MAX_SUMMARY_BYTES)
    return encoded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', default='2026-10-26')
    parser.add_argument('--include-q2-late', action='store_true')
    args = parser.parse_args(argv)
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'ACTIONS_REQUIRED'}))
        return 2
    try:
        transport = ActionsTransport(); results = []
        for period in ('2026Q2', '2026Q3'):
            results.append(discover_period(period, as_of=args.as_of, fetch_page=transport,
                observed_at=dt.datetime.now(UTC), include_q2_late=args.include_q2_late))
        encoded = artifacts_for(results)
        complete = all(result['windowEnumerationComplete'] for result in results)
        status = 'window-enumerated' if complete else 'partial'
    except DiscoveryError as error:
        encoded = {'summary.json': json.dumps({'schemaVersion': 1, 'status': 'failed', 'errorCode': error.code,
            'sourceUrl': ENDPOINT, 'historicalUniverseVerified': False, 'allQuarterReportsVerified': False,
            'productionCatalogModified': False}, separators=(',', ':')).encode()}
        complete = False; status = 'failed'
    except Exception:
        encoded = {'summary.json': b'{"status":"failed","errorCode":"DISCOVERY_FAILED","historicalUniverseVerified":false,"allQuarterReportsVerified":false,"productionCatalogModified":false}'}
        complete = False; status = 'failed'
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # No previous owned artifact may survive an unsuccessful verification.
    owned = re.compile(r'(?:summary|candidates|(?:announcements|codes)-2026Q[23]-[0-9]{4})\.json')
    for path in OUTPUT.glob('*.json'):
        if owned.fullmatch(path.name) and path.name not in encoded: path.unlink()
    # Shards precede their manifest; the final summary is the last write.
    for name in encoded:
        path = OUTPUT / name
        temporary = path.with_suffix('.tmp'); temporary.write_bytes(encoded[name]); temporary.replace(path)
    print(json.dumps({'status': status, 'historicalUniverseVerified': False, 'productionCatalogModified': False}))
    return 0 if complete else 1


if __name__ == '__main__':
    raise SystemExit(main())
