"""Reuse content verdicts only from API-verified, successful main workflow runs.

Artifacts are hints until their immutable archive digest, run provenance, source
fingerprint and Git ancestry all pass. No caller-supplied verdict file is accepted.
Any cache miss or invalid proof falls back to a complete history content scan.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import selectors
import subprocess
import time
import zipfile

REPOSITORY = 'yt-feng/record-lab'
WORKFLOWS = ('.github/workflows/check.yml', '.github/workflows/traverse.yml', '.github/workflows/update.yml')
SOURCES = ('scripts/publication_guard.py', 'scripts/publication_cache.py', 'requirements.txt', *WORKFLOWS)
EVENTS = {'push', 'schedule', 'workflow_dispatch'}
MAX_LOOKUP_SECONDS = 20
MAX_METADATA = 1_000_000
MAX_ARCHIVE = 8_000_000
MAX_MANIFEST = 16_000_000
MAX_OIDS = 250_000
OID = re.compile(r'[0-9a-f]{40}')
FIELDS = {'schema', 'repository', 'run_id', 'run_attempt', 'workflow_path', 'run_head_sha',
          'checkout_sha', 'fingerprint', 'blob_oids'}


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


def fingerprint(root, revision=None):
    sources = {name: hashlib.sha256(git(root, 'show', f'{revision}:{name}') if revision
                                   else (root/name).read_bytes()).hexdigest() for name in SOURCES}
    value = {'sources': sources, 'python': [platform.python_implementation(), platform.python_version()]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ancestor(root, older, newer):
    return subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', older, newer],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def artifact_name(digest, run_id, attempt):
    return f'publication-history-v1-{digest}-{run_id}-{attempt}'


def positive_integer(value):
    return type(value) is int and value > 0


def read_bounded_command(command, max_bytes, deadline):
    """Bound both bytes and wall time; kill/reap a hanging or oversized producer."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks, size = [], 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Optional proof lookup exceeded its budget')
                if not selector.select(timeout=remaining):
                    raise TimeoutError('Optional proof lookup exceeded its budget')
                chunk = os.read(process.stdout.fileno(), min(65536, max_bytes-size+1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('Optional proof response exceeded its byte limit')
                chunks.append(chunk)
        if process.wait(timeout=max(0.001, deadline-time.monotonic())):
            raise RuntimeError('Optional proof API request failed')
        return b''.join(chunks)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


class GitHubAPI:
    def __init__(self):
        self.deadline = time.monotonic()+MAX_LOOKUP_SECONDS

    def get(self, suffix, binary=False):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Optional proof lookup exceeded its budget')
        # Canonical repository/API endpoint, no archive URL supplied by a caller.
        data = read_bounded_command(['gh', 'api', '--hostname', 'github.com',
                                     f'repos/{REPOSITORY}/{suffix}'],
                                    MAX_ARCHIVE if binary else MAX_METADATA, self.deadline)
        return data if binary else json.loads(data)


def validate_proof(root, artifact, run, archive, digest, current_head):
    """Pure verification shared by the API loader and offline regression tests."""
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('event') not in EVENTS or run.get('head_branch') != 'main'
            or run.get('path') not in WORKFLOWS):
        raise ValueError('Untrusted workflow run')
    repository, head_repository = run.get('repository', {}), run.get('head_repository', {})
    if (repository.get('full_name') != REPOSITORY or head_repository.get('full_name') != REPOSITORY
            or not positive_integer(repository.get('id')) or repository['id'] != head_repository.get('id')):
        raise ValueError('Untrusted repository')
    identity = artifact.get('workflow_run', {})
    if (not positive_integer(artifact.get('id')) or not positive_integer(identity.get('id'))
            or not positive_integer(identity.get('repository_id'))
            or not positive_integer(identity.get('head_repository_id'))
            or identity.get('id') != run.get('id') or identity.get('repository_id') != repository['id']
            or identity.get('head_repository_id') != repository['id'] or identity.get('head_branch') != 'main'
            or identity.get('head_sha') != run.get('head_sha') or artifact.get('expired') is not False):
        raise ValueError('Artifact identity mismatch')
    if (not positive_integer(run.get('id')) or not positive_integer(run.get('run_attempt'))
            or artifact.get('name') != artifact_name(digest, run['id'], run['run_attempt'])):
        raise ValueError('Artifact name mismatch')
    size = artifact.get('size_in_bytes')
    if not positive_integer(size) or size > MAX_ARCHIVE or len(archive) != size:
        raise ValueError('Artifact size invalid')
    expected = artifact.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', expected) or expected != 'sha256:'+hashlib.sha256(archive).hexdigest():
        raise ValueError('Archive digest mismatch')
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        if (len(entries) != 1 or entries[0].filename != 'publication-history.json'
                or entries[0].is_dir() or entries[0].file_size > MAX_MANIFEST
                or entries[0].external_attr >> 16 & 0o170000 == 0o120000):
            raise ValueError('Invalid manifest archive')
        proof = json.loads(zipped.read(entries[0]))
    if (not isinstance(proof, dict) or set(proof) != FIELDS or type(proof['schema']) is not int or proof['schema'] != 1
            or proof['repository'] != REPOSITORY or proof['fingerprint'] != digest
            or not positive_integer(proof['run_id']) or not positive_integer(proof['run_attempt'])
            or proof['run_id'] != run['id'] or proof['run_attempt'] != run['run_attempt']
            or proof['workflow_path'] != run['path'] or proof['run_head_sha'] != run['head_sha']):
        raise ValueError('Manifest identity mismatch')
    checkout, run_head = proof['checkout_sha'], proof['run_head_sha']
    if (not isinstance(checkout, str) or not OID.fullmatch(checkout)
            or not isinstance(run_head, str) or not OID.fullmatch(run_head)
            or not ancestor(root, run_head, checkout) or not ancestor(root, checkout, current_head)):
        raise ValueError('Manifest ancestry mismatch')
    # Bind the actual checkout AND workflow definition executed at the API run
    # head. An advancing main checkout cannot claim a different workflow's proof.
    if fingerprint(root, checkout) != digest or fingerprint(root, run_head) != digest:
        raise ValueError('Historical source fingerprint mismatch')
    oids = proof['blob_oids']
    if (not isinstance(oids, list) or len(oids) > MAX_OIDS
            or any(not isinstance(oid, str) or not OID.fullmatch(oid) for oid in oids)
            or oids != sorted(set(oids))):
        raise ValueError('Invalid blob inventory')
    return frozenset(oid.encode() for oid in oids)


def load_verified(root, env=None, api=None):
    env = os.environ if env is None else env
    if env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPOSITORY or not env.get('GH_TOKEN'):
        return frozenset()
    try:
        digest = fingerprint(root)
        current_head = git(root, 'rev-parse', 'HEAD').decode().strip()
        # Dirty scanner/workflow files are never allowed to reuse historical verdicts.
        if fingerprint(root, current_head) != digest:
            return frozenset()
        api = api or GitHubAPI()
        listing = api.get('actions/artifacts?per_page=100')
        candidates = [artifact for artifact in listing['artifacts']
                      if artifact.get('name', '').startswith('publication-history-v1-'+digest+'-')
                      and artifact.get('expired') is False
                      and positive_integer(artifact.get('size_in_bytes'))
                      and 0 < artifact['size_in_bytes'] <= MAX_ARCHIVE]
        for artifact in candidates[:5]:
            try:
                run_id = artifact['workflow_run']['id']
                artifact_id = artifact['id']
                if not positive_integer(run_id) or not positive_integer(artifact_id):
                    continue
                run = api.get(f'actions/runs/{run_id}')
                # Reject untrusted runs before downloading an archive.
                if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
                        or run.get('event') not in EVENTS or run.get('head_branch') != 'main'
                        or run.get('path') not in WORKFLOWS):
                    continue
                archive = api.get(f'actions/artifacts/{artifact_id}/zip', binary=True)
                return validate_proof(root, artifact, run, archive, digest, current_head)
            except (ValueError, KeyError, TypeError, zipfile.BadZipFile):
                continue
    except Exception:
        pass
    return frozenset()


def write_proof(root, blob_oids, env=None):
    """Called only after every publication check has passed, never for PR runs."""
    env = os.environ if env is None else env
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != 'refs/heads/main' or env.get('GITHUB_EVENT_NAME') not in EVENTS
            or not env.get('PUBLICATION_CACHE_OUTPUT') or not env.get('GITHUB_OUTPUT')):
        return False
    try:
        prefix = REPOSITORY+'/'
        workflow_ref = env['GITHUB_WORKFLOW_REF']
        if not workflow_ref.startswith(prefix) or not workflow_ref.endswith('@refs/heads/main'):
            return False
        workflow = workflow_ref[len(prefix):].split('@')[0]
        if workflow not in WORKFLOWS:
            return False
        checkout = git(root, 'rev-parse', 'HEAD').decode().strip()
        run_head = env['GITHUB_SHA']
        digest = fingerprint(root)
        if (not OID.fullmatch(run_head) or not ancestor(root, run_head, checkout)
                or fingerprint(root, checkout) != digest or fingerprint(root, run_head) != digest):
            return False
        run_id, attempt = int(env['GITHUB_RUN_ID']), int(env['GITHUB_RUN_ATTEMPT'])
        oids = sorted(oid.decode() for oid in blob_oids)
        if run_id <= 0 or attempt <= 0 or len(oids) > MAX_OIDS or any(not OID.fullmatch(oid) for oid in oids):
            return False
        proof = dict(schema=1, repository=REPOSITORY, run_id=run_id, run_attempt=attempt,
                     workflow_path=workflow, run_head_sha=run_head, checkout_sha=checkout,
                     fingerprint=digest, blob_oids=oids)
        output = Path(env['PUBLICATION_CACHE_OUTPUT'])
        output.write_text(json.dumps(proof, sort_keys=True, separators=(',', ':'))+'\n')
        with Path(env['GITHUB_OUTPUT']).open('a') as handle:
            handle.write('cache_artifact_name='+artifact_name(digest, run_id, attempt)+'\n')
        return True
    except Exception:
        return False
