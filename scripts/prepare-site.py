"""Publish bounded gzip partitions without mirroring multi-GB raw snapshots."""
import argparse
import gzip
import json
from pathlib import Path
import re

from publication_guard import inspect_content, content_limit

COMPANY_PATH = re.compile(r'^\./data/companies/([A-Za-z0-9_-]{1,64})\.json$')


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
        elif file.parent.name == 'companies' and file.suffix == '.json':
            raw = gzip.compress(raw, compresslevel=6, mtime=0)
            destination = destination.with_suffix('.json.gz')
        total += len(raw)
        if total > max_bytes: raise ValueError('SITE_SIZE_EXCEEDED')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        count += 1
    return {'status': 'site_ready', 'files': count, 'bytes': total, 'compression': 'gzip-company-partitions'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='web')
    parser.add_argument('--output', default='.cache/site')
    args = parser.parse_args()
    try: print(json.dumps(prepare(args.source, args.output)))
    except Exception:
        print(json.dumps({'status': 'failed', 'errorCode': 'SITE_PREPARATION_FAILED'}))
        raise SystemExit(1) from None
