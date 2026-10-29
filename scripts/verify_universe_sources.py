"""Actions-only source diagnostics; never change or certify the fund corpus.

The cited AMAC tables are aggregate benchmarks, not product directories. Even
matching totals cannot establish equal scope or complete product enumeration.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

import historical_universe as history
import verify_universe as verify
import universe_source_diff as source_diff
from publication_guard import inspect_content

OUTPUT = Path('.cache/universe-source-verification/result.json')
MAX_CORE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
SOURCE_WINDOWS = 2
RETRYABLE_SOURCE_ERRORS = {'NETWORK_ERROR', 'TIMEOUT'}
CORE_NS = 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties'
AGGREGATE_OBSERVED_ON = '2026-10-29'
AGGREGATES = (
    {'asOf': '2026-06-30', 'publishedAt': '2026-07-23', 'fundProducts': 14353,
     'fundManagers': 165,
     'sourceUrl': 'https://www.amac.org.cn/sjtj/tjbg/gmjj/202607/P020260723642575271747.pdf'},
    {'asOf': '2026-08-31', 'publishedAt': '2026-09-18', 'fundProducts': 14585,
     'fundManagers': 165,
     'sourceUrl': 'https://www.amac.org.cn/sjtj/tjbg/gmjj/202609/P020260918624509873296.pdf'},
    {'asOf': '2026-09-30', 'publishedAt': '2026-10-28', 'fundProducts': 14760,
     'fundManagers': 165,
     'sourceUrl': 'https://www.amac.org.cn/sjtj/tjbg/gmjj/202610/P020261028612999638714.pdf'},
)


def explicit_period_dates(text):
    """Only explicit as-of labels; creation and inception dates are unrelated."""
    dates = set()
    pattern = r'(?:截至|截止|as\s+of)\s*(\d{4})[年/.-]?(\d{2})[月/.-]?(\d{2})(?:日)?(?!\d)'
    for match in re.finditer(pattern, text, re.I):
        try:
            dates.add(dt.date(*(int(value) for value in match.groups())).isoformat())
        except ValueError:
            continue
    return sorted(dates)


def core_properties(data):
    """Retain dates and counts only; never publish author or raw property text."""
    book = verify.Workbook(data)
    result = {'present': False, 'createdAt': None, 'modifiedAt': None,
              'invalidTimestampProperties': 0, 'authorPropertyCount': 0,
              'explicitPeriodDates': [], 'timestampsArePeriodEvidence': False}
    try:
        entry = book.entries.get('docProps/core.xml')
        if entry is None:
            return result
        if entry.file_size > MAX_CORE_BYTES:
            raise verify.VerificationError('XML_LIMIT')
        with book.archive.open(entry) as stream:
            raw = stream.read(MAX_CORE_BYTES + 1)
        if (len(raw) > MAX_CORE_BYTES or len(raw) != entry.file_size or b'\0' in raw
                or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', raw, re.I)):
            raise verify.VerificationError('XML_REJECTED')
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise verify.VerificationError('XML_REJECTED') from None
        if root.tag != '{' + CORE_NS + '}coreProperties':
            raise verify.VerificationError('UNSUPPORTED_SCHEMA')
        if len(list(root.iter())) > 64 or any(list(child) for child in root):
            raise verify.VerificationError('XML_LIMIT')
        result['present'] = True
        seen, periods = set(), set()
        for child in root:
            name = verify.local(child.tag)
            if name in seen:
                raise verify.VerificationError('UNSUPPORTED_SCHEMA')
            seen.add(name)
            value = child.text or ''
            if name in {'creator', 'lastModifiedBy'}:
                result['authorPropertyCount'] += 1
            elif name in {'title', 'subject', 'description'}:
                periods.update(explicit_period_dates(value))
            elif name in {'created', 'modified'}:
                try:
                    stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError
                    result['createdAt' if name == 'created' else 'modifiedAt'] = stamp.astimezone(dt.timezone.utc).isoformat()
                except ValueError:
                    result['invalidTimestampProperties'] += 1
        result['explicitPeriodDates'] = sorted(periods)
        return result
    finally:
        book.archive.close()


def compare_aggregate(parsed_count, expected_count):
    benchmark = next(row for row in AGGREGATES if row['asOf'] == history.SOURCE_AS_OF)
    return {'asOf': benchmark['asOf'], 'aggregateProductCount': benchmark['fundProducts'],
            'pinnedExpectedProductRows': expected_count, 'parsedProductRows': parsed_count,
            'aggregateMinusPinnedExpected': benchmark['fundProducts'] - expected_count,
            'aggregateMinusParsed': None if parsed_count is None else benchmark['fundProducts'] - parsed_count,
            'countsMatch': parsed_count == benchmark['fundProducts'] if parsed_count is not None else None,
            'sameScopeVerified': False, 'productSetReconciled': False,
            'status': 'unresolved', 'aggregateSourceUrl': benchmark['sourceUrl']}


def run_diagnostic(fetch=verify.download_official, now=None, comparison_fetch=None, catalog_root='web/data',
                   checkpoint=None, sleep=time.sleep):
    observed = history.observation_time(now or dt.datetime.now(dt.timezone.utc))
    result = {'schemaVersion': 1, 'status': 'failed', 'diagnosticComplete': False,
              'errorCode': None, 'retrievedAt': observed, 'sourceUrl': history.SOURCE_URL,
              'sourceAsOf': history.SOURCE_AS_OF, 'sourceAsOfBasis': 'official-download-filename',
              'expectedSourceSha256': history.SOURCE_HASH,
              'historicalUniverseVerified': False, 'catalogCompletenessVerified': False,
              'productionCatalogModified': False,
              'aggregateBenchmarks': [{**row, 'observedOn': AGGREGATE_OBSERVED_ON,
                  'verificationMethod': 'published-table-manually-verified-on-observed-date',
                  'fetchedByThisRun': False, 'scope': 'aggregate-only-not-product-list',
                  'excludesFundsReportedForLiquidation': True} for row in AGGREGATES],
              'comparison': compare_aggregate(None, history.EXPECTED_PRODUCTS), 'sourceRequests': []}
    def save():
        if checkpoint is not None: checkpoint(result)

    def source_request(url, operation):
        # Each underlying official downloader already enforces a 60-second
        # deadline and three attempts. One fresh bounded window recovers an
        # exhausted transient window; deterministic rejections are not retried.
        record = {'sourceUrl': url, 'status': 'pending', 'attempts': []}
        result['sourceRequests'].append(record)
        for number in range(1, SOURCE_WINDOWS + 1):
            attempt = {'window': number, 'status': 'started',
                       'startedAt': history.observation_time(dt.datetime.now(dt.timezone.utc))}
            record['status'] = 'running'; record['attempts'].append(attempt); save()
            try:
                body = operation()
            except Exception as error:
                code = getattr(error, 'code', None)
                code = code if code in verify.ERRORS else 'UNIVERSE_SOURCE_DIAGNOSTIC_FAILED'
                attempt.update(status='failed', errorCode=code)
                record.update(status='failed', errorCode=code); save()
                if code not in RETRYABLE_SOURCE_ERRORS or number == SOURCE_WINDOWS:
                    raise verify.VerificationError(code) from None
                sleep(number * 2)
            else:
                attempt.update(status='observed')
                record.update(status='observed', errorCode=None, downloadedBytes=len(body),
                              sourceSha256=hashlib.sha256(body).hexdigest())
                save()
                return body
        raise verify.VerificationError('TIMEOUT')

    data = None
    try:
        data = source_request(history.SOURCE_URL, fetch)
        digest = hashlib.sha256(data).hexdigest()
        result.update(sourceSha256=digest, downloadedBytes=len(data),
                      sourcePinMatches=digest == history.SOURCE_HASH)
        inspected = verify.inspect_workbook(data)
        result['workbook'] = {'sheetCount': inspected['sheetCount'],
            'parsedCellCount': inspected['parsedCellCount'],
            'sheets': [{key: sheet[key] for key in ('rowCount', 'nonemptyRowCount', 'maxRowNumber', 'cellCount')}
                       for sheet in inspected['sheets']],
            'coreProperties': core_properties(data)}
        header_dates = sorted({date for sheet in inspected['sheets'] for row in sheet['firstFiveRows']
            if row['row'] <= 2 for cell in row['cells'] for date in explicit_period_dates(cell['value'])})
        core_dates = result['workbook']['coreProperties']['explicitPeriodDates']
        dates = set(header_dates + core_dates)
        contradictory = bool(dates - {history.SOURCE_AS_OF})
        header_truncated = any(row['omittedCellCount'] or any(cell['truncated'] for cell in row['cells'])
                               for sheet in inspected['sheets'] for row in sheet['firstFiveRows'] if row['row'] <= 2)
        result['periodEvidence'] = {'headerDates': header_dates, 'coreExplicitDates': core_dates,
            'headerInspectionRowsPerSheet': 2, 'headerInspectionTruncated': header_truncated,
            'status': 'contradictory' if contradictory else 'explicit-date-found' if dates else 'filename-only',
            'sameScopeVerified': False}
        reasons = ['AGGREGATE_SCOPE_NOT_RECONCILED', 'PRODUCT_SET_NOT_RECONCILED']
        if header_truncated:
            reasons.append('HEADER_DATE_INSPECTION_TRUNCATED')
        if not dates:
            reasons.append('WORKBOOK_PERIOD_NOT_CORROBORATED')
        if contradictory:
            reasons.append('WORKBOOK_PERIOD_CONTRADICTORY')
        if result['sourcePinMatches']:
            parsed = history.parse_official_index(data, retrieved_at=observed)
            stats = parsed['diagnostics']
            result['pinnedParser'] = {'parseStatus': parsed['parseStatus'], **{key: stats[key] for key in
                ('observedRows', 'headerRows', 'expectedProductRows', 'observedProductRows',
                 'parsedRows', 'rejectedRows', 'missingPhysicalRows', 'duplicateCodeCount',
                 'serialSequenceComplete', 'productRowsComplete', 'unresolvedInceptionRows')}}
            result['comparison'] = compare_aggregate(stats['parsedRows'], history.EXPECTED_PRODUCTS)
            if parsed['parseStatus'] != 'complete':
                reasons.append('PINNED_PARSE_INCOMPLETE')
            if result['comparison']['countsMatch'] is False:
                reasons.append('PRODUCT_COUNT_DISCREPANCY')
        else:
            result['pinnedParser'] = {'parseStatus': 'not-run', 'errorCode': 'SOURCE_HASH_MISMATCH'}
            reasons.append('SOURCE_HASH_MISMATCH')
        result.update(status='partial', diagnosticComplete=True, unresolvedReasons=reasons)
    except (verify.VerificationError, history.HistoricalUniverseError) as error:
        result['errorCode'] = error.code
    except Exception:
        result['errorCode'] = 'UNIVERSE_SOURCE_DIAGNOSTIC_FAILED'
    # Save the completed June inspection before another source can time out.
    # A June transport failure also must not prevent an independent August or
    # current landing-page observation from surviving this diagnostic run.
    save()
    if comparison_fetch is not None:
        try:
            comparison = source_diff.compare_sources(data, dates=explicit_period_dates,
                core_properties=core_properties,
                fetch=lambda url: source_request(url, lambda: comparison_fetch(url)), catalog_root=catalog_root)
            result['twoPeriodComparison'] = comparison
            successful_source = any(source.get('parseStatus') in {'complete', 'partial'} for source in comparison['sources'])
            if successful_source or comparison['landingPage']['fetchStatus'] == 'observed':
                result.update(status='partial', diagnosticComplete=True)
            failed = [row for row in result['sourceRequests'] if row['status'] == 'failed']
            if failed:
                reasons = result.setdefault('unresolvedReasons', [])
                reasons.extend(reason for reason in ('SOURCE_REQUESTS_INCOMPLETE', 'PRODUCT_SET_NOT_RECONCILED') if reason not in reasons)
        except Exception as error:
            result['comparisonErrorCode'] = getattr(error, 'code', 'UNIVERSE_SOURCE_DIAGNOSTIC_FAILED')
        save()
    return result


def encode_result(result):
    data = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    error = 'SUMMARY_TOO_LARGE' if len(data) > MAX_OUTPUT_BYTES else 'PUBLICATION_REJECTED' if inspect_content(data) else None
    if error:
        result = {'schemaVersion': 1, 'status': 'failed', 'diagnosticComplete': False,
                  'errorCode': error, 'historicalUniverseVerified': False,
                  'catalogCompletenessVerified': False, 'productionCatalogModified': False}
        data = json.dumps(result, separators=(',', ':')).encode('utf-8')
    return result, data


def save_result(result):
    guarded, data = encode_result(result)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_bytes(data)
    temporary.replace(OUTPUT)
    return guarded


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'ACTIONS_REQUIRED'}))
        return 2
    result = save_result(run_diagnostic(comparison_fetch=source_diff.download_source, checkpoint=save_result))
    print(json.dumps({key: result.get(key) for key in ('status', 'diagnosticComplete', 'errorCode')}))
    # Successful diagnostics can document an unresolved discrepancy. They never
    # indicate that a catalog or a quarter has passed completeness acceptance.
    return 0 if result['diagnosticComplete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
