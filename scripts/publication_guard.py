#!/usr/bin/env python3
"""Fail closed on unexpected public files or credential-shaped content."""
import argparse
from contextlib import contextmanager
import re
import subprocess
import time
from pathlib import Path

ROOT_FILES = {'README.md', 'package.json', 'package-lock.json', 'requirements.txt', '.gitignore'}
ROOT_DIRS = {'lib', 'web', 'scripts', 'tests', 'config', 'docs', '.github'}
IGNORE_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', 'raw', '.cache', '.local'}
EXTENSIONS = {'.md', '.json', '.js', '.css', '.html', '.py', '.txt', '.yml', '.yaml'}
MAX_CONTENT_BYTES = 25_000_000
PATTERNS = {
    'credential': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{35,}|sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)'),
    'credential_assignment': re.compile(r'''(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?key|password|authorization|cookie)\s*[=:]\s*["'](?:Bearer\s+)?[A-Za-z0-9_/.+=-]{16,}'''),
    'machine_path': re.compile(r'(?:/' + r'Users/[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\[^\\\s]+\\|/' + r'home/(?!runner(?:/|\b))[^/\s]+/)'),
    'url_credential': re.compile(r'(?i)(?:[?&](?:token|key|secret|password|signature|authorization)=)[^&\s"<>]{8,}|https?://[^/\s:@]+:[^/\s@]+@'),
    'personal_email': re.compile(r'\b[A-Za-z0-9._%+-]+@(?!(?:users\.noreply\.github\.com|example\.(?:com|test|invalid))\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
}
# These are necessary literal characters/prefixes of the original regexes,
# not replacement detectors. In particular, keep the Unicode IGNORECASE
# credential-assignment expression unfiltered.
REQUIRED_LITERALS = {
    'credential': ('ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_', 'sk-', 'AKIA', '-----BEGIN '),
    'machine_path': ('/Users/', '\\Users\\', '/home/'),
    'url_credential': ('?', '&', '@'),
    'personal_email': ('@',),
}


def allowed_path(name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        return False
    if any(part in IGNORE_DIRS for part in p.parts):
        return False
    if p.name.startswith('.env') or p.name.endswith(('.log', '.pem', '.key')):
        return False
    if len(p.parts) == 1:
        return name in ROOT_FILES
    return p.parts[0] in ROOT_DIRS and p.suffix in EXTENSIONS


def inspect_content(data):
    if len(data) > MAX_CONTENT_BYTES or b'\0' in data:
        return ['unsupported_content']
    try:
        value = data.decode('utf-8')
    except UnicodeDecodeError:
        return ['unsupported_encoding']
    return [category for category, pattern in PATTERNS.items()
            if (category not in REQUIRED_LITERALS or any(marker in value for marker in REQUIRED_LITERALS[category]))
            and pattern.search(value)]


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


@contextmanager
def history_blobs(root):
    """Read immutable objects through one process, with a bounded per-blob buffer."""
    process = subprocess.Popen(['git', '-C', str(root), 'cat-file', '--batch'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)

    def read(oid):
        process.stdin.write(oid+b'\n'); process.stdin.flush()
        header = process.stdout.readline().split()
        if len(header) != 3 or header[0] != oid or header[1] != b'blob':
            raise RuntimeError('Invalid history blob response')
        size = int(header[2])
        if size < 0:
            raise RuntimeError('Invalid history blob size')
        if size <= MAX_CONTENT_BYTES:
            data = process.stdout.read(size)
            if len(data) != size:
                raise RuntimeError('Truncated history blob')
        else:
            # Oversized content still fails publication; drain it without
            # allocating an unbounded buffer or corrupting the next response.
            data = None
            remaining = size
            while remaining:
                chunk = process.stdout.read(min(remaining, 1024*1024))
                if not chunk:
                    raise RuntimeError('Truncated history blob')
                remaining -= len(chunk)
        if process.stdout.read(1) != b'\n':
            raise RuntimeError('Invalid history blob delimiter')
        read.bytes_read += size
        return data

    read.bytes_read = 0
    try:
        yield read
        process.stdin.close()
        if process.wait(timeout=30) != 0:
            raise RuntimeError('History blob reader failed')
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdin.close(); process.stdout.close()


def scan_history(root, stats=None, progress=None):
    findings = []
    seen_blobs = set()
    started = time.monotonic()
    last_progress = started
    commit_count = entry_count = 0
    with history_blobs(root) as read_blob:
        for commit in git(root, 'rev-list', '--all').decode().splitlines():
            commit_count += 1
            findings.extend(inspect_content(git(root, 'show', '-s', '--format=%B%n%an <%ae>%n%cn <%ce>', commit)))
            for entry in git(root, 'ls-tree', '-rz', commit).split(b'\0'):
                if not entry:
                    continue
                entry_count += 1
                metadata, name = entry.split(b'\t', 1)
                mode, kind, oid = metadata.split()
                # Every path/mode is checked in every reachable tree, even if
                # the same bytes were previously allowed at another path.
                if mode in (b'120000', b'160000') or not allowed_path(name.decode()):
                    findings.append('unexpected_history_file')
                elif kind == b'blob' and oid not in seen_blobs:
                    seen_blobs.add(oid)
                    raw = read_blob(oid)
                    findings.extend(['unsupported_content'] if raw is None else inspect_content(raw))
                    now = time.monotonic()
                    if progress is not None and now-last_progress >= 30:
                        progress(f'history_scan_progress: commits={commit_count} tree_entries={entry_count} '
                                 f'unique_blobs={len(seen_blobs)} bytes={read_blob.bytes_read} '
                                 f'elapsed_seconds={round(now-started, 3)}', flush=True)
                        last_progress = now
    if stats is not None:
        stats.update(commits=commit_count, tree_entries=entry_count, unique_blobs=len(seen_blobs),
                     bytes=read_blob.bytes_read, elapsed_seconds=round(time.monotonic()-started, 3))
    return findings


def scan(root, history=False, history_stats=None, history_progress=None):
    findings = []
    count = 0
    for file in root.rglob('*'):
        relative = file.relative_to(root)
        if any(part in IGNORE_DIRS for part in relative.parts):
            continue
        if file.is_symlink():
            findings.append('symlink'); continue
        if not file.is_file():
            continue
        count += 1
        if not allowed_path(relative.as_posix()):
            findings.append('unexpected_file'); continue
        findings.extend(inspect_content(file.read_bytes()))
    if (root / '.git').exists():
        for name in git(root, 'ls-files', '-z').decode().split('\0'):
            if name and not allowed_path(name):
                findings.append('unexpected_tracked_file')
    if history:
        findings.extend(scan_history(root, history_stats, history_progress))
    return count, findings


def scan_plan(root):
    """Plan artifacts are public too, even in an ignored staging directory."""
    findings, count = [], 0
    if root.is_symlink() or not root.is_dir():
        return 0, ['invalid_plan_directory']
    for file in root.rglob('*'):
        if file.is_symlink() or not file.is_file() or file.parent != root:
            findings.append('unexpected_plan_file'); continue
        if not re.fullmatch(r'(?:batch-\d+|matrix|meta)\.json', file.name):
            findings.append('unexpected_plan_file'); continue
        count += 1
        findings.extend(inspect_content(file.read_bytes()))
    if not (root / 'matrix.json').is_file() or not (root / 'meta.json').is_file():
        findings.append('missing_plan_metadata')
    return count, findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--plan-dir')
    args = parser.parse_args()
    history_stats = {}
    try:
        count, findings = scan(Path(args.root).resolve(), args.history, history_stats, print)
        if args.plan_dir:
            extra_count, extra_findings = scan_plan(Path(args.plan_dir))
            count += extra_count
            findings.extend(extra_findings)
    except Exception:
        print('publication_check_failed: scan_error')
        return 1
    if args.history:
        print('history_scan_complete: ' + ' '.join(f'{key}={value}' for key, value in history_stats.items()))
    if findings:
        print('publication_check_failed: ' + ','.join(sorted(set(findings))))
        return 1
    print(f'publication_check_passed: files={count}')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
