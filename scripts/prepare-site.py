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
        if (previous.get('kind') == 'quarter-analysis' and previous.get('analysisVersion') == 4
                and previous.get('retrievedAt') == latest.get('retrievedAt')
                and 'priceUpdatedAt' in previous and previous.get('priceUpdatedAt') == latest.get('priceUpdatedAt')
                and previous.get('targetPeriodEnd') == latest.get('targetPeriodEnd')
                and previous.get('baselinePeriodEnd') == latest.get('baselinePeriodEnd')):
            continue
        subprocess.run(['node', 'scripts/analyze-data.js', '--data', str(folder), '--output', str(destination)], check=True)
        rebuilt += 1
    return rebuilt


def prepare(source='web', output='.cache/site', max_bytes=900_000_000):
    root, target = Path(source), Path(output)
    if target.exists(): raise ValueError('SITE_OUTPUT_EXISTS')
    target.mkdir(parents=True)
    count, total = 0, 0
    for file in sorted(root.rglob('*')):
        if file.is_symlink(): raise ValueError('SITE_SYMLINK')
        if not file.is_file(): continue
        relative = file.relative_to(root)
        if file.suffix not in {'.json', '.js', '.css', '.html'}: raise ValueError('SITE_FILE_TYPE')
        raw = file.read_bytes()
        if inspect_content(raw, content_limit('web/' + relative.as_posix())): raise ValueError('SITE_PUBLICATION_REJECTED')
        destination = target / relative
        if file.name == 'latest.json':
            body = json.loads(raw)
            for item in body.get('companyFiles', []):
                match = COMPANY_PATH.fullmatch(item.get('path', ''))
                if not match: raise ValueError('INVALID_COMPANY_PATH')
                if not (file.parent / 'companies' / (match[1] + '.json')).is_file():
                    raise ValueError('MISSING_COMPANY_PART')
                item['path'] += '.gz'
            raw = (json.dumps(body, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        elif file.name == 'analysis.json':
            body = json.loads(raw)
            def compressed_analysis_path(value):
                match = ANALYSIS_PATH.fullmatch(value)
                if not match: raise ValueError('INVALID_ANALYSIS_PATH')
                if not (file.parent / 'analysis' / match[1]).is_file():
                    raise ValueError('MISSING_ANALYSIS_PART')
                return value + '.gz'
            for stock in body.get('stocks', []):
                stock['details'] = [compressed_analysis_path(value) for value in stock.get('details', [])]
            for part in body.get('portfolioFiles', []):
                part['path'] = compressed_analysis_path(part['path'])
            raw = (json.dumps(body, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        elif file.parent.name in {'companies', 'analysis'} and file.suffix == '.json':
            raw = gzip.compress(raw, compresslevel=6, mtime=0)
            destination = destination.with_suffix('.json.gz')
        total += len(raw)
        if total > max_bytes: raise ValueError('SITE_SIZE_EXCEEDED')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        count += 1
    return {'status': 'site_ready', 'files': count, 'bytes': total, 'compression': 'gzip-data-partitions'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='web')
    parser.add_argument('--output', default='.cache/site')
    args = parser.parse_args()
    try:
        ensure_analyses(args.source)
        print(json.dumps(prepare(args.source, args.output)))
    except Exception:
        print(json.dumps({'status': 'failed', 'errorCode': 'SITE_PREPARATION_FAILED'}))
        raise SystemExit(1) from None
