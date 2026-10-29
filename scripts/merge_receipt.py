"""Decode bounded transient receipt pages before existing evidence validation."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_PAGE_BYTES = 8_000_000
MAX_PAGES = 64
MAX_TOTAL_BYTES = MAX_PAGE_BYTES * MAX_PAGES
MAX_FUNDS = 4096


def read_merge_receipt(path, *, read_json, error_type=ValueError):
    """Legacy objects are unchanged; pages reconstruct that exact legacy shape.

    The caller supplies its existing bounded manifest reader and fixed error
    class, avoiding a dependency on traversal or admission runtime modules.
    """
    def fail():
        raise error_type('INVALID_PROGRESS') from None

    def integer(value, minimum, maximum):
        return type(value) is int and minimum <= value <= maximum

    def code(value):
        return isinstance(value, str) and re.fullmatch(r'[0-9]{6}', value) is not None

    path = Path(path)
    manifest = read_json(path)
    if not isinstance(manifest, dict) or manifest.get('schemaVersion') != 2:
        return manifest
    if (set(manifest) != {'schemaVersion', 'storageFormat', 'receipt', 'fundCount', 'totalPageBytes', 'fundPages'}
            or manifest['storageFormat'] != 'fund-pages-v1'):
        fail()
    metadata, pages = manifest['receipt'], manifest['fundPages']
    if (not isinstance(metadata, dict) or type(metadata.get('schemaVersion')) is not int
            or metadata['schemaVersion'] != 1 or 'funds' in metadata
            or not isinstance(metadata.get('phase'), dict) or not isinstance(metadata.get('generatedAt'), str)
            or not isinstance(metadata.get('requestedCodes'), list)
            or not integer(len(metadata['requestedCodes']), 1, MAX_FUNDS)
            or not all(code(item) for item in metadata['requestedCodes'])
            or len(set(metadata['requestedCodes'])) != len(metadata['requestedCodes'])
            or not isinstance(pages, list) or not integer(len(pages), 1, MAX_PAGES)
            or not integer(manifest['fundCount'], 1, min(MAX_FUNDS, len(metadata['requestedCodes'])))
            or not integer(manifest['totalPageBytes'], 1, MAX_TOTAL_BYTES)):
        fail()
    count, size, previous_code = 0, 0, ''
    for index, page in enumerate(pages):
        if (not isinstance(page, dict) or set(page) != {'path', 'sha256', 'byteLength', 'fundCount', 'firstCode', 'lastCode'}
                or not isinstance(page.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', page['sha256'])
                or page['path'] != f"merge-receipt-pages/{index:04d}-{page['sha256']}.json"
                or not integer(page['byteLength'], 1, MAX_PAGE_BYTES)
                or not integer(page['fundCount'], 1, MAX_FUNDS)
                or not code(page['firstCode']) or not code(page['lastCode'])
                or not previous_code < page['firstCode'] <= page['lastCode']):
            fail()
        count += page['fundCount']; size += page['byteLength']; previous_code = page['lastCode']
    if count != manifest['fundCount'] or size != manifest['totalPageBytes']:
        fail()
    root_fd = page_fd = None
    funds, requested = [], set(metadata['requestedCodes'])
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
        root_fd = os.open(path.parent, flags)
        page_fd = os.open('merge-receipt-pages', flags, dir_fd=root_fd)
        for page in pages:
            filename = page['path'].split('/')[1]
            before = os.stat(filename, dir_fd=page_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_size != page['byteLength']:
                fail()
            descriptor = os.open(filename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                 | getattr(os, 'O_NONBLOCK', 0), dir_fd=page_fd)
            with os.fdopen(descriptor, 'rb') as handle:
                opened = os.fstat(handle.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_size != page['byteLength']
                        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                    fail()
                raw = handle.read(MAX_PAGE_BYTES + 1)
                after = os.fstat(handle.fileno())
                if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                    fail()
            if len(raw) != page['byteLength'] or hashlib.sha256(raw).hexdigest() != page['sha256']:
                fail()
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'kind', 'phase', 'generatedAt', 'funds'}
                    or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                    or value['kind'] != 'merge-receipt-fund-page'
                    or value['phase'] != metadata['phase'] or value['generatedAt'] != metadata['generatedAt']
                    or not isinstance(value['funds'], list) or len(value['funds']) != page['fundCount']):
                fail()
            codes = [item.get('code') if isinstance(item, dict) else None for item in value['funds']]
            if (not all(code(item) and item in requested for item in codes)
                    or codes != sorted(set(codes)) or codes[0] != page['firstCode'] or codes[-1] != page['lastCode']):
                fail()
            funds.extend(value['funds'])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        fail()
    finally:
        if page_fd is not None: os.close(page_fd)
        if root_fd is not None: os.close(root_fd)
    return {**metadata, 'funds': funds}
