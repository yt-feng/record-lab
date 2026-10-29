"""Bounded, read-only CPU profiling of retained Q2 coverage and quality."""
from __future__ import annotations

import builtins
import contextlib
import copy
import cProfile
import datetime as dt
import hashlib
import io
import json
import math
import multiprocessing
import os
from pathlib import Path
import pstats
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.parse

import price_range_reference
import traversal
from publication_guard import inspect_content

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('.cache/stored-coverage-profile/summary.json')
PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
MAX_PAGES, MAX_FUNDS = 20, 64
MAX_FILE_BYTES, MAX_PAGE_BYTES = 25_000_000, 250_000_000
MAX_FUND_BYTES, MAX_SAMPLE_BYTES = 8_000_000, 64_000_000
MAX_OUTPUT_BYTES, CHILD_SECONDS, MAX_HOTSPOTS = 64_000, 180, 40
ERRORS = {'PROFILE_ACTIONS_REQUIRED', 'PROFILE_INPUT_INVALID', 'PROFILE_NO_SAMPLES',
          'PROFILE_TIMEOUT', 'PROFILE_FAILED', 'PROFILE_OUTPUT_INVALID',
          'PROFILE_OUTBOUND_BLOCKED', 'PROFILE_WRITE_BLOCKED'}
COUNTS = ('manifestPages', 'scannedPages', 'scannedPageBytes', 'scannedFundRecords',
          'duplicateFundRecords', 'oversizedFundRecords', 'sampledFunds', 'sampledBytes',
          'holdingCount', 'priceRowCount', 'referenceRowCount', 'referenceFactCount', 'witnessFactCount')

# Only these exact source/function pairs may leave cProfile. Caller maps,
# filenames, line numbers, argument values and raw exception text never leave.
FUNCTIONS = {
    'traversal': {'price_coverage', 'report_quality', '_report_quality_with_coverage', 'valid_price_coverage',
                  'valid_report_managers', 'valid_manager_rows', 'stock_key', 'date', 'instant', 'public_url',
                  'quarter_start', 'valid_baseline_exemption', 'no_equities_report_matches'},
    'price_range_reference': {'coverage_resolution', '_effective_reference', 'effective_reference',
        'effective_price_range', 'normalize_reference', 'build_reference', 'provider_for', 'preferred_fact',
        'key_for', 'valid_resolution', 'unresolved_conflict', 'resolution_for'},
    'price_consistency': {'canonical_digest', 'canonical', '_encoded', 'source_identity', '_date', '_instant',
                          '_identity', '_price', '_fact', 'conflict_for', 'normalize_price_conflict'},
    'price_coverage': {'expected_sessions', 'date_set_hash', 'valid_complete_price', 'valid_usable_complete_price'},
    'report_managers': {'normalize_manager_table_coverage', 'manager_key'},
    'strategy_sections': {'normalize_strategy_section_coverage'},
    'source_dates': {'china_date'},
    'copy': {'deepcopy', '_deepcopy_dict', '_deepcopy_list', '_deepcopy_tuple', '_deepcopy_atomic', '_keep_alive'},
    'json': {'dumps'}, 'json.encoder': {'encode', 'iterencode', '_make_iterencode'},
    'urllib.parse': {'urlparse', 'urlsplit', 'parse_qsl', 'unquote', 'urlencode', 'urlunparse', '_coerce_args'},
    're': {'fullmatch', 'search', '_compile'},
}
STANDARD_FILES = {copy.__file__: 'copy', json.__file__: 'json', json.encoder.__file__: 'json.encoder',
                  urllib.parse.__file__: 'urllib.parse', re.__file__: 're'}
PROFILE_FILES = {str(ROOT / 'scripts' / (name + '.py')): name for name in FUNCTIONS if name not in STANDARD_FILES.values()}
PROFILE_FILES.update({str(Path(file).resolve()): name for file, name in STANDARD_FILES.items()})


class ProfileError(ValueError):
    pass


def fail(code):
    raise ProfileError(code)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read_input(file):
    before = file.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
        fail('PROFILE_INPUT_INVALID')
    descriptor = os.open(file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_FILE_BYTES
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            fail('PROFILE_INPUT_INVALID')
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        fail('PROFILE_INPUT_INVALID')
    return json.loads(raw), len(raw), hashlib.sha256(raw).hexdigest()


def fund_counts(fund):
    holdings, rows = fund['selectedReport'].get('holdings', []), fund.get('rows', [])
    if not isinstance(holdings, list) or not isinstance(rows, list) or len(holdings) > 10000 or len(rows) > 10000:
        fail('PROFILE_INPUT_INVALID')
    result = dict(holdingCount=len(holdings), priceRowCount=len(rows), referenceRowCount=0,
                  referenceFactCount=0, witnessFactCount=0)
    for row in rows:
        price = row.get('priceRange') if isinstance(row, dict) else None
        if not isinstance(price, dict): continue
        reference, witness = price.get('rangeReference'), price.get('globalPriceConflict')
        result['referenceRowCount'] += int(isinstance(reference, dict))
        for value, field in ((reference, 'referenceFactCount'), (witness, 'witnessFactCount')):
            facts = value.get('facts') if isinstance(value, dict) else None
            if isinstance(facts, list): result[field] += len(facts)
    return result


def select_samples(root, now):
    folder = Path(root) / 'web/data/periods/2026Q2'
    for parent in (folder, *list(folder.parents)[:3]):
        if parent.is_symlink(): fail('PROFILE_INPUT_INVALID')
    manifest, _, manifest_digest = read_input(folder / 'latest.json')
    pages = manifest.get('companyFiles') if isinstance(manifest, dict) else None
    if (not isinstance(pages, list) or len(pages) > 10000
            or any(manifest.get(key) != value for key, value in PHASE.items())):
        fail('PROFILE_INPUT_INVALID')
    paths = []
    for item in pages:
        value = item.get('path') if isinstance(item, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r'\./data/companies/[A-Za-z0-9_-]{1,64}\.json', value):
            fail('PROFILE_INPUT_INVALID')
        paths.append(value)
    if len(paths) != len(set(paths)) or (folder / 'companies').is_symlink(): fail('PROFILE_INPUT_INVALID')
    counts = dict.fromkeys(COUNTS, 0); counts['manifestPages'] = len(paths)
    candidates, seen, page_digests = [], set(), []
    for relative in paths[:MAX_PAGES]:
        file = folder / relative[len('./data/'):]
        if counts['scannedPageBytes'] + file.lstat().st_size > MAX_PAGE_BYTES: break
        page, size, page_digest = read_input(file)
        funds = page.get('funds') if isinstance(page, dict) else None
        if not isinstance(funds, list) or len(funds) > 1000: fail('PROFILE_INPUT_INVALID')
        counts['scannedPages'] += 1; counts['scannedPageBytes'] += size; page_digests.append(page_digest)
        for fund in funds:
            counts['scannedFundRecords'] += 1
            if (not isinstance(fund, dict) or not traversal.is_code(fund.get('code'))
                    or not isinstance(fund.get('selectedReport'), dict)
                    or fund['selectedReport'].get('periodEnd') != PHASE['targetPeriodEnd']): continue
            if fund['code'] in seen:
                counts['duplicateFundRecords'] += 1; continue
            seen.add(fund['code'])
            sample_counts = fund_counts(fund)
            raw = encoded(fund)
            if len(raw) > min(MAX_FUND_BYTES, MAX_SAMPLE_BYTES):
                counts['oversizedFundRecords'] += 1; continue
            score = (-sample_counts['referenceRowCount'], -sample_counts['referenceFactCount'],
                     -sample_counts['witnessFactCount'], -sample_counts['holdingCount'], counts['scannedFundRecords'])
            candidates.append((score, fund, sample_counts, len(raw), hashlib.sha256(raw).hexdigest()))
            candidates.sort(key=lambda item: item[0]); del candidates[MAX_FUNDS:]
            while sum(item[3] for item in candidates) > MAX_SAMPLE_BYTES: candidates.pop()
    selected, sample_digests = [], []
    for _, fund, sample_counts, size, sample_digest in candidates:
        if counts['sampledBytes'] + size > MAX_SAMPLE_BYTES: continue
        selected.append(fund); sample_digests.append(sample_digest)
        counts['sampledBytes'] += size; counts['sampledFunds'] += 1
        for key, value in sample_counts.items(): counts[key] += value
    if not selected: fail('PROFILE_NO_SAMPLES')
    identity = digest({'manifest': manifest_digest, 'pages': page_digests, 'samples': sample_digests,
                       'phase': PHASE, 'validationClock': now.isoformat()})
    return selected, counts, identity


@contextlib.contextmanager
def read_only_execution():
    """Local process guards; no network settings are inspected or changed."""
    changed = []
    def replace(owner, name, value):
        changed.append((owner, name, getattr(owner, name))); setattr(owner, name, value)
    def outbound(*_args, **_kwargs): fail('PROFILE_OUTBOUND_BLOCKED')
    def write_blocked(*_args, **_kwargs): fail('PROFILE_WRITE_BLOCKED')
    def guarded_open(original):
        def opened(file, mode='r', *args, **kwargs):
            if not isinstance(mode, str) or any(flag in mode for flag in 'wax+'):
                write_blocked()
            return original(file, mode, *args, **kwargs)
        return opened
    original_os_open = os.open
    def opened_fd(file, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND): write_blocked()
        return original_os_open(file, flags, *args, **kwargs)
    try:
        for name in ('connect', 'connect_ex'): replace(socket.socket, name, outbound)
        for name in ('create_connection', 'getaddrinfo'): replace(socket, name, outbound)
        replace(subprocess, 'Popen', outbound)
        for owner in (builtins, io): replace(owner, 'open', guarded_open(owner.open))
        replace(os, 'open', opened_fd)
        for name in ('rename', 'replace', 'unlink', 'remove', 'mkdir', 'rmdir'): replace(os, name, write_blocked)
        yield
    finally:
        for owner, name, original in reversed(changed): setattr(owner, name, original)


@contextlib.contextmanager
def fixed_reference_clock(now):
    # These original APIs have an optional clock; callers without that argument
    # use this one diagnostic clock, while explicit clocks pass through intact.
    originals = {name: getattr(price_range_reference, name) for name in ('effective_reference', 'effective_price_range')}
    def bound(original):
        def call(price, identity=None, now_override=None, **kwargs):
            supplied = kwargs.pop('now', now_override)
            if kwargs: raise TypeError('invalid clock arguments')
            return original(price, identity, now if supplied is None else supplied)
        return call
    try:
        for name, original in originals.items(): setattr(price_range_reference, name, bound(original))
        yield
    finally:
        for name, original in originals.items(): setattr(price_range_reference, name, original)


def summarize_profile(profile):
    measured = pstats.Stats(profile).stats
    aggregated = {}
    for (filename, _, function), (primitive, calls, self_cpu, cumulative_cpu, _) in measured.items():
        module = PROFILE_FILES.get(filename)
        if module is None or function not in FUNCTIONS[module]: continue
        row = aggregated.setdefault((module, function), {'module': module, 'function': function,
            'primitiveCalls': 0, 'calls': 0, 'selfCpuSeconds': 0.0, 'cumulativeCpuSeconds': 0.0})
        for key, value in (('primitiveCalls', primitive), ('calls', calls),
                           ('selfCpuSeconds', self_cpu), ('cumulativeCpuSeconds', cumulative_cpu)):
            row[key] += value
    rows = sorted(aggregated.values(), key=lambda row: (-row['cumulativeCpuSeconds'], row['module'], row['function']))[:MAX_HOTSPOTS]
    for row in rows:
        for key in ('selfCpuSeconds', 'cumulativeCpuSeconds'): row[key] = round(row[key], 6)
    return rows


def base_summary(now):
    return {'schemaVersion': 1, 'kind': 'stored-coverage-profile', 'quarter': '2026Q2', 'status': 'running',
        'errorCode': None, 'validationClock': now.isoformat(), 'inputDigest': None,
        'counts': dict.fromkeys(COUNTS, 0), 'stages': [], 'outboundRequests': 0, 'productionWrites': 0,
        'fullCollectionAndAnalysisComplete': False}


def profile_sample(root, now, checkpoint=lambda _value: None):
    result = base_summary(now)
    funds, counts, identity = select_samples(root, now)
    result.update(counts=counts, inputDigest=identity); checkpoint(result)
    with fixed_reference_clock(now):
        for stage in ('coverage', 'quality'):
            profile = cProfile.Profile(timer=time.process_time)
            nonempty, cpu_seconds, wall_seconds = 0, 0.0, 0.0
            result['stages'].append({'stage': stage, 'sampledFunds': len(funds), 'processedFunds': 0,
                'complete': False, 'nonemptyResults': 0, 'cpuSeconds': 0.0, 'wallSeconds': 0.0, 'hotspots': []})
            checkpoint(result)
            for index, fund in enumerate(funds, 1):
                started_cpu, started_wall = time.process_time(), time.monotonic()
                profile.enable()
                try:
                    measured = traversal.price_coverage(fund, now) if stage == 'coverage' else traversal.report_quality(fund, PHASE, now)
                    nonempty += int(measured is not None)
                finally:
                    profile.disable()
                    cpu_seconds += time.process_time() - started_cpu
                    wall_seconds += time.monotonic() - started_wall
                if index % 8 == 0 or index == len(funds):
                    result['stages'][-1] = {'stage': stage, 'sampledFunds': len(funds), 'processedFunds': index,
                        'complete': index == len(funds), 'nonemptyResults': nonempty,
                        'cpuSeconds': round(cpu_seconds, 6), 'wallSeconds': round(wall_seconds, 6),
                        'hotspots': summarize_profile(profile)}
                    checkpoint(result)
    result['status'] = 'complete'
    return result


def checked_output(value):
    expected = set(base_summary(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)))
    if (not isinstance(value, dict) or set(value) != expected or value['schemaVersion'] != 1
            or value['kind'] != 'stored-coverage-profile' or value['quarter'] != '2026Q2'
            or value['status'] not in {'running', 'complete', 'failed', 'timeout'}
            or value['errorCode'] not in ERRORS | {None} or traversal.instant(value['validationClock']) is None
            or value['inputDigest'] is not None and not re.fullmatch(r'[a-f0-9]{64}', str(value['inputDigest']))
            or value['outboundRequests'] != 0 or value['productionWrites'] != 0
            or value['fullCollectionAndAnalysisComplete'] is not False
            or not isinstance(value['counts'], dict) or set(value['counts']) != set(COUNTS)
            or any(type(number) is not int or not 0 <= number <= 1_000_000_000 for number in value['counts'].values())
            or value['counts']['scannedPages'] > MAX_PAGES or value['counts']['sampledFunds'] > MAX_FUNDS
            or value['counts']['scannedPageBytes'] > MAX_PAGE_BYTES or value['counts']['sampledBytes'] > MAX_SAMPLE_BYTES
            or not isinstance(value['stages'], list) or len(value['stages']) > 2): fail('PROFILE_OUTPUT_INVALID')
    seen = set()
    for stage in value['stages']:
        if (not isinstance(stage, dict) or set(stage) != {'stage', 'sampledFunds', 'processedFunds', 'complete', 'nonemptyResults', 'cpuSeconds', 'wallSeconds', 'hotspots'}
                or stage['stage'] not in {'coverage', 'quality'} or stage['stage'] in seen
                or any(type(stage[key]) is not int or not 0 <= stage[key] <= MAX_FUNDS for key in ('sampledFunds', 'processedFunds', 'nonemptyResults'))
                or stage['sampledFunds'] != value['counts']['sampledFunds']
                or not 0 <= stage['nonemptyResults'] <= stage['processedFunds'] <= stage['sampledFunds']
                or type(stage['complete']) is not bool or stage['complete'] != (stage['processedFunds'] == stage['sampledFunds'])
                or any(type(stage[key]) not in (int, float) or not math.isfinite(stage[key]) or not 0 <= stage[key] <= CHILD_SECONDS + 10
                       for key in ('cpuSeconds', 'wallSeconds'))
                or not isinstance(stage['hotspots'], list) or len(stage['hotspots']) > MAX_HOTSPOTS): fail('PROFILE_OUTPUT_INVALID')
        seen.add(stage['stage'])
        for row in stage['hotspots']:
            if (not isinstance(row, dict) or set(row) != {'module', 'function', 'primitiveCalls', 'calls', 'selfCpuSeconds', 'cumulativeCpuSeconds'}
                    or row['module'] not in FUNCTIONS or row['function'] not in FUNCTIONS[row['module']]
                    or any(type(row[key]) is not int or not 0 <= row[key] <= 1_000_000_000 for key in ('primitiveCalls', 'calls'))
                    or any(type(row[key]) not in (int, float) or not math.isfinite(row[key]) or not 0 <= row[key] <= CHILD_SECONDS + 10
                           for key in ('selfCpuSeconds', 'cumulativeCpuSeconds'))): fail('PROFILE_OUTPUT_INVALID')
    if value['status'] == 'complete' and (seen != {'coverage', 'quality'} or not all(stage['complete'] for stage in value['stages'])):
        fail('PROFILE_OUTPUT_INVALID')
    raw = encoded(value)
    if len(raw) > MAX_OUTPUT_BYTES or inspect_content(raw, MAX_OUTPUT_BYTES): fail('PROFILE_OUTPUT_INVALID')
    return raw


def _worker(send, root, now_text):
    sys.dont_write_bytecode = True
    now = traversal.utc_now(now_text); latest = base_summary(now)
    def checkpoint(value):
        nonlocal latest
        latest = value
        send.send_bytes(checked_output(value))
    try:
        with read_only_execution():
            checkpoint(profile_sample(root, now, checkpoint))
    except Exception as error:
        latest.update(status='failed', errorCode=str(error) if isinstance(error, ProfileError) and str(error) in ERRORS else 'PROFILE_FAILED')
        try: checkpoint(latest)
        except Exception: pass
    finally:
        send.close()


def run(root, *, now=None, timeout_seconds=CHILD_SECONDS, worker_target=_worker):
    current = traversal.utc_now(now); result = base_summary(current)
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= CHILD_SECONDS: fail('PROFILE_INPUT_INVALID')
    context = multiprocessing.get_context('spawn'); receive, send = context.Pipe(duplex=False)
    child = context.Process(target=worker_target, args=(send, str(root), current.isoformat()), daemon=True)
    started = False
    try:
        child.start(); started = True; send.close()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if receive.poll(min(0.2, max(0, deadline - time.monotonic()))):
                try: value = json.loads(receive.recv_bytes(MAX_OUTPUT_BYTES))
                except EOFError: break
                checked_output(value); result = value
                if result['status'] != 'running': return result
            elif not child.is_alive(): break
        result.update(status='timeout' if child.is_alive() else 'failed',
                      errorCode='PROFILE_TIMEOUT' if child.is_alive() else 'PROFILE_FAILED')
    except Exception:
        result.update(status='failed', errorCode='PROFILE_OUTPUT_INVALID')
    finally:
        receive.close(); send.close()
        if started:
            if child.is_alive(): child.terminate()
            child.join(timeout=2)
            if child.is_alive(): child.kill(); child.join(timeout=2)
    return result


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'PROFILE_ACTIONS_REQUIRED'})); return 1
    try:
        result = run(ROOT)
        raw = checked_output(result)
        output = ROOT / OUTPUT
        if (ROOT / '.cache').is_symlink() or output.parent.is_symlink() or output.is_symlink(): fail('PROFILE_OUTPUT_INVALID')
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.tmp')
        with temporary.open('xb') as stream: stream.write(raw)
        temporary.replace(output)
        print(raw.decode())
        return 0 if result['status'] == 'complete' else 1
    except Exception:
        print(json.dumps({'status': 'failed', 'errorCode': 'PROFILE_OUTPUT_INVALID'})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
