"""Publish one independent classification; never rewrite prices or ledgers."""
from __future__ import annotations
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import verify_price_source_census as census
from publication_guard import inspect_content

PUBLIC_PATH = Path('web/data/periods/2026Q3/price-representations.json')
MAX_PUBLIC_BYTES = 512_000


def guarded_read(path, maximum):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('REPRESENTATION_INPUT_INVALID')
    raw = path.read_bytes()
    if inspect_content(raw, max_bytes=maximum): raise ValueError('REPRESENTATION_INPUT_INVALID')
    return census.parse_json(raw)


def actions_gate():
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REPOSITORY') != census.restore.REPOSITORY
            or os.environ.get('GITHUB_WORKFLOW_REF') != f'{census.restore.REPOSITORY}/{census.WORKFLOW}@refs/heads/main'):
        raise ValueError('REPRESENTATION_REQUIRES_ACTIONS')


def admit(evidence_dir, root='.', api=None, now=None):
    actions_gate()
    directory = Path(evidence_dir)
    observed = now or dt.datetime.now(dt.timezone.utc)
    value = guarded_read(directory / 'census.json', census.MAX_CENSUS_BYTES)
    stored_proof = guarded_read(directory / 'proof.json', census.MAX_RESULT_BYTES)
    # Reuse and independently restore the exact existing proof. These requests
    # address GitHub artifacts only, never market-data endpoints or new rounds.
    proof = census.restore_proof(api, observed)
    if census.encoded(stored_proof) != census.encoded(proof):
        raise ValueError('REPRESENTATION_PROOF_MISMATCH')
    result = census.certificate(value, proof, observed)
    classification = result['representation']
    if classification['eligible'] is not True:
        raise ValueError('REPRESENTATION_NOT_ELIGIBLE')
    root = Path(root).resolve(); destination = root / PUBLIC_PATH
    if any(parent.is_symlink() for parent in [destination, *destination.parents] if parent != root.parent):
        raise ValueError('REPRESENTATION_OUTPUT_INVALID')
    document = {'schemaVersion': 1, 'kind': 'price-representations', 'quarter': '2026Q3',
        'targetPeriodEnd': '2026-09-30', 'publishedAt': observed.isoformat(),
        'purpose': 'Independent evidence classification; original prices, timestamps and raw conflicts are retained.',
        'records': [classification]}
    if destination.exists():
        old = guarded_read(destination, MAX_PUBLIC_BYTES)
        if (old.get('schemaVersion') != 1 or old.get('kind') != document['kind'] or old.get('quarter') != '2026Q3'
                or not isinstance(old.get('records'), list) or len(old['records']) > 8):
            raise ValueError('REPRESENTATION_EXISTING_INVALID')
        same = [record for record in old['records'] if record.get('key') == census.KEY]
        if same:
            if len(same) != 1 or census.encoded(same[0]) != census.encoded(classification):
                raise ValueError('REPRESENTATION_EXISTING_SCOPE_CHANGED')
            return {'status': 'unchanged', 'path': PUBLIC_PATH.as_posix(), 'marketDataRequests': 0}
        document['records'] = old['records'] + [classification]
    raw = census.encoded(document) + b'\n'
    if len(raw) > MAX_PUBLIC_BYTES or inspect_content(raw, max_bytes=MAX_PUBLIC_BYTES):
        raise ValueError('REPRESENTATION_PUBLICATION_REJECTED')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp'); temporary.write_bytes(raw); temporary.replace(destination)
    return {'status': 'staged', 'path': PUBLIC_PATH.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(),
            'mathematicallyClassifiedObservations': classification['mathematicallyClassifiedObservationCount'],
            'directlyBoundRecheckObservations': classification['directlyBoundRecheckObservationCount'],
            'marketDataRequests': 0, 'quotesModified': False, 'ledgerModified': False}


def verify_readback(root='.', api=None):
    actions_gate()
    path = Path(root) / PUBLIC_PATH
    guarded_read(path, MAX_PUBLIC_BYTES)
    remote = (api or census.restore.ActionsAPI()).get('contents/' + PUBLIC_PATH.as_posix() + '?ref=main')
    if not isinstance(remote, dict) or remote.get('encoding') != 'base64':
        raise ValueError('REPRESENTATION_READBACK_UNVERIFIED')
    try: raw = base64.b64decode(remote['content'], validate=False)
    except (KeyError, ValueError): raise ValueError('REPRESENTATION_READBACK_UNVERIFIED') from None
    if raw != path.read_bytes(): raise ValueError('REPRESENTATION_READBACK_MISMATCH')
    return {'status': 'published-and-read-back', 'path': PUBLIC_PATH.as_posix(),
            'sha256': hashlib.sha256(raw).hexdigest(), 'marketDataRequests': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument('--evidence-dir')
    parser.add_argument('--verify-readback', action='store_true'); args = parser.parse_args(argv)
    try:
        result = verify_readback() if args.verify_readback else admit(args.evidence_dir)
        print(json.dumps(result)); return 0
    except Exception:
        print(json.dumps({'status': 'failed', 'errorCode': 'REPRESENTATION_ADMISSION_FAILED'})); return 2


if __name__ == '__main__': raise SystemExit(main())
