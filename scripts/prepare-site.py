"""Publish bounded gzip partitions without mirroring multi-GB raw snapshots."""
import argparse
import gzip
import json
import os
from pathlib import Path
import re
import subprocess

from publication_guard import inspect_content, content_limit

COMPANY_PATH = re.compile(r'^\./data/companies/([A-Za-z0-9_-]{1,64})\.json$')
ANALYSIS_PATH = re.compile(r'^\./analysis/(details-(?:portfolios-)?\d+\.json)$')
COMPANY_FILE = re.compile(r'^[A-Za-z0-9_-]{1,64}\.json$')
ANALYSIS_FILE = re.compile(r'^details-(?:portfolios-)?\d+\.json$')


def ensure_analyses(source='web'):
    """Rebuild missing/stale site analysis after a source-only main update.

    The collection commit may precede a source update pulled during publication.
    Run this only on the hosted runner; normal preparation stays a pure pack step.
    """
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('ANALYSIS_REQUIRES_ACTIONS')
    root = Path(source)
    manifest = root / 'data/periods.json'
    if not manifest.exists(): return 0
    rebuilt = 0
    for period in json.loads(manifest.read_text())['periods']:
        quarter = period.get('id', '')
        if not re.fullmatch(r'2026Q[23]', quarter): raise ValueError('INVALID_ANALYSIS_PERIOD')
        folder = root / 'data/periods' / quarter
        latest = json.loads((folder / 'latest.json').read_text())
        destination = folder / 'analysis.json'
        previous = json.loads(destination.read_text()) if destination.exists() else {}
        if (previous.get('kind') == 'quarter-analysis' and previous.get('analysisVersion') == 8 and previous.get('priceReferenceVersion') == 1
                and previous.get('retrievedAt') == latest.get('retrievedAt')
                and 'priceUpdatedAt' in previous and previous.get('priceUpdatedAt') == latest.get('priceUpdatedAt')
                and previous.get('targetPeriodEnd') == latest.get('targetPeriodEnd')
                and previous.get('baselinePeriodEnd') == latest.get('baselinePeriodEnd')):
            continue
        subprocess.run(['node', 'scripts/analyze-data.js', '--data', str(folder), '--output', str(destination)], check=True)
        rebuilt += 1
    return rebuilt


def public_bytes(root, file):
    limit = content_limit('web/' + file.relative_to(root).as_posix())
    if file.stat().st_size > limit: raise ValueError('SITE_PUBLICATION_REJECTED')
    raw = file.read_bytes()
    if inspect_content(raw, limit): raise ValueError('SITE_PUBLICATION_REJECTED')
    return raw


def current_partitions(root, files):
    """Validate every current manifest before excluding historical partitions.

    Paths are relative to each manifest's own directory. This includes both
    the legacy data root and each quarter; a root manifest is not superseded
    merely because a periods manifest also exists.
    """
    available = set(files)
    managed, referenced, manifests = {}, set(), {}

    def require_part(file, value, pattern, directory, invalid, missing):
        match = pattern.fullmatch(value) if isinstance(value, str) else None
        if not match: raise ValueError(invalid)
        name = match[1] + '.json' if directory == 'companies' else match[1]
        part = file.parent / directory / name
        if part not in available: raise ValueError(missing)
        referenced.add(part)
        return value + '.gz'

    for file in files:
        # A partition can itself have a generic name. Only its enclosing
        # current manifest owns the directory's reference inventory.
        if file.parent.name in {'companies', 'analysis'} or file.name not in {'latest.json', 'analysis.json'}:
            continue
        body = json.loads(public_bytes(root, file))
        if not isinstance(body, dict): raise ValueError('INVALID_SITE_MANIFEST')
        if file.name == 'latest.json':
            managed[file.parent / 'companies'] = COMPANY_FILE
            items = body.get('companyFiles', [])
            if not isinstance(items, list): raise ValueError('INVALID_COMPANY_PATH')
            seen = set()
            for item in items:
                value = item.get('path') if isinstance(item, dict) else None
                if not isinstance(value, str) or value in seen: raise ValueError('INVALID_COMPANY_PATH')
                seen.add(value)
                item['path'] = require_part(file, value, COMPANY_PATH, 'companies',
                    'INVALID_COMPANY_PATH', 'MISSING_COMPANY_PART')
        else:
            managed[file.parent / 'analysis'] = ANALYSIS_FILE
            stocks, portfolios = body.get('stocks', []), body.get('portfolioFiles', [])
            if not isinstance(stocks, list) or not isinstance(portfolios, list): raise ValueError('INVALID_ANALYSIS_PATH')
            for stock in stocks:
                details = stock.get('details', []) if isinstance(stock, dict) else None
                if (not isinstance(details, list) or any(not isinstance(value, str) for value in details)
                        or len(set(details)) != len(details)):
                    raise ValueError('INVALID_ANALYSIS_PATH')
                # Several stocks legitimately share one bounded detail file.
                stock['details'] = [require_part(file, value, ANALYSIS_PATH, 'analysis',
                    'INVALID_ANALYSIS_PATH', 'MISSING_ANALYSIS_PART') for value in details]
            seen = set()
            for item in portfolios:
                value = item.get('path') if isinstance(item, dict) else None
                if not isinstance(value, str) or value in seen: raise ValueError('INVALID_ANALYSIS_PATH')
                seen.add(value)
                item['path'] = require_part(file, value, ANALYSIS_PATH, 'analysis',
                    'INVALID_ANALYSIS_PATH', 'MISSING_ANALYSIS_PART')
        manifests[file] = (json.dumps(body, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    historical = {file for file in files if file.parent in managed
                  and managed[file.parent].fullmatch(file.name) and file not in referenced}
    return manifests, historical, referenced


def prepare(source='web', output='.cache/site', max_bytes=900_000_000):
    root, target = Path(source), Path(output)
    if root.is_symlink(): raise ValueError('SITE_SYMLINK')
    if not root.is_dir(): raise ValueError('SITE_SOURCE_MISSING')
    if target.exists() or target.is_symlink(): raise ValueError('SITE_OUTPUT_EXISTS')
    if target.resolve().is_relative_to(root.resolve()): raise ValueError('SITE_OUTPUT_INSIDE_SOURCE')
    files = []
    for file in sorted(root.rglob('*')):
        if file.is_symlink(): raise ValueError('SITE_SYMLINK')
        if not file.is_file(): continue
        if file.suffix not in {'.json', '.js', '.css', '.html'}: raise ValueError('SITE_FILE_TYPE')
        files.append(file)
    manifests, historical, referenced = current_partitions(root, files)
    skipped_bytes = sum(file.stat().st_size for file in historical)
    target.mkdir(parents=True)
    count, total = 0, 0
    for file in files:
        if file in historical: continue
        relative = file.relative_to(root)
        raw = manifests[file] if file in manifests else public_bytes(root, file)
        destination = target / relative
        if file not in manifests and file.parent.name in {'companies', 'analysis'} and file.suffix == '.json':
            raw = gzip.compress(raw, compresslevel=6, mtime=0)
            destination = destination.with_suffix('.json.gz')
        total += len(raw)
        if total > max_bytes: raise ValueError('SITE_SIZE_EXCEEDED')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        count += 1
    return {'status': 'site_ready', 'files': count, 'bytes': total, 'compression': 'gzip-data-partitions',
            'selectedPartitionFiles': len(referenced), 'skippedHistoricalPartitionFiles': len(historical),
            'skippedSourceBytes': skipped_bytes}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='web')
    parser.add_argument('--output', default='.cache/site')
    parser.add_argument('--analyses-only', action='store_true',
                        help='Update stale analyses before persistence without creating a site package.')
    args = parser.parse_args(argv)
    try:
        rebuilt = ensure_analyses(args.source)
        result = {'status': 'analyses_ready', 'rebuiltPeriods': rebuilt} if args.analyses_only else prepare(args.source, args.output)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({'status': 'failed', 'errorCode': 'SITE_PREPARATION_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
