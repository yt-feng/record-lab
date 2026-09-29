#!/usr/bin/env python3
"""Fail closed on unexpected public files or credential-shaped content."""
import argparse
import re
import subprocess
from pathlib import Path

ROOT_FILES = {'README.md', 'package.json', 'package-lock.json', 'requirements.txt', '.gitignore'}
ROOT_DIRS = {'lib', 'web', 'scripts', 'tests', 'config', 'docs', '.github'}
IGNORE_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', 'raw', '.cache', '.local'}
EXTENSIONS = {'.md', '.json', '.js', '.css', '.html', '.py', '.txt', '.yml', '.yaml'}
PATTERNS = {
    'credential': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{35,}|sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)'),
    'credential_assignment': re.compile(r'''(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?key|password|authorization|cookie)\s*[=:]\s*["'](?:Bearer\s+)?[A-Za-z0-9_/.+=-]{16,}'''),
    'machine_path': re.compile(r'(?:/' + r'Users/[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\[^\\\s]+\\|/' + r'home/(?!runner(?:/|\b))[^/\s]+/)'),
    'url_credential': re.compile(r'(?i)(?:[?&](?:token|key|secret|password|signature|authorization)=)[^&\s"<>]{8,}|https?://[^/\s:@]+:[^/\s@]+@'),
    'personal_email': re.compile(r'\b[A-Za-z0-9._%+-]+@(?!(?:users\.noreply\.github\.com|example\.(?:com|test|invalid))\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
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
    if len(data) > 25_000_000 or b'\0' in data:
        return ['unsupported_content']
    try:
        value = data.decode('utf-8')
    except UnicodeDecodeError:
        return ['unsupported_encoding']
    return [category for category, pattern in PATTERNS.items() if pattern.search(value)]


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


def scan(root, history=False):
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
        for commit in git(root, 'rev-list', '--all').decode().splitlines():
            findings.extend(inspect_content(git(root, 'show', '-s', '--format=%B%n%an <%ae>%n%cn <%ce>', commit)))
            for entry in git(root, 'ls-tree', '-rz', commit).split(b'\0'):
                if not entry:
                    continue
                metadata, name = entry.split(b'\t', 1)
                mode, kind, oid = metadata.split()
                if mode in (b'120000', b'160000') or not allowed_path(name.decode()):
                    findings.append('unexpected_history_file')
                elif kind == b'blob':
                    findings.extend(inspect_content(git(root, 'cat-file', 'blob', oid.decode())))
    return count, findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--history', action='store_true')
    args = parser.parse_args()
    try:
        count, findings = scan(Path(args.root).resolve(), args.history)
    except Exception:
        print('publication_check_failed: scan_error')
        return 1
    if findings:
        print('publication_check_failed: ' + ','.join(sorted(set(findings))))
        return 1
    print(f'publication_check_passed: files={count}')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
