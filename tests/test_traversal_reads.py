"""Small failure fixtures for safe, actionable production read diagnostics."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import traversal
from publication_guard import content_limit, inspect_content


class TraversalReadTests(unittest.TestCase):
    def test_json_read_failures_distinguish_input_without_echoing_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cases = [('merge-receipt.json', None, 'merge-receipt', 'missing'),
                     ('status.json', b'{private text', 'batch-status', 'invalid-json'),
                     ('latest.json', b'\xff', 'manifest', 'invalid-json')]
            for name, payload, kind, reason in cases:
                with self.subTest(name=name):
                    file = root / name
                    if payload is not None:
                        file.write_bytes(payload)
                    with self.assertRaises(traversal.JsonReadError) as raised:
                        traversal.read_json(file)
                    error = raised.exception
                    self.assertEqual(error.code, 'READ_FAILED')
                    self.assertEqual(error.diagnostic, {'inputKind': kind, 'reason': reason,
                        'byteLength': len(payload) if payload is not None else None, 'maxBytes': 25_000_000})
                    encoded = json.dumps(error.diagnostic).encode()
                    self.assertEqual(inspect_content(encoded), [])
                    self.assertNotIn(folder.encode(), encoded)
                    self.assertNotIn(b'private text', encoded)

    def test_bounds_match_publication_and_oversize_is_rejected_before_read(self):
        for name in ('progress.json', 'latest.json', 'merge-receipt.json'):
            limit = content_limit('web/data/periods/2026Q3/' + name)
            fake_stat = os.stat_result((stat.S_IFREG | 0o600, 1, 1, 1, 0, 0, limit + 1, 0, 0, 0))
            with self.subTest(name=name), patch.object(Path, 'lstat', return_value=fake_stat), patch.object(os, 'open') as opened:
                with self.assertRaises(traversal.JsonReadError) as raised:
                    traversal.read_json(name)
                self.assertEqual(raised.exception.diagnostic['reason'], 'too-large')
                self.assertEqual(raised.exception.diagnostic['maxBytes'], limit)
                self.assertEqual(raised.exception.diagnostic['byteLength'], limit + 1)
                opened.assert_not_called()

    def test_company_category_symlink_and_io_errors_remain_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'companies').mkdir()
            target = root / 'companies' / 'unpublished-name.json'
            target.write_text('{}')
            with patch.object(os, 'open', side_effect=PermissionError('private detail')):
                with self.assertRaises(traversal.JsonReadError) as raised:
                    traversal.read_json(target)
                self.assertEqual(raised.exception.diagnostic['inputKind'], 'company-page')
                self.assertEqual(raised.exception.diagnostic['reason'], 'io-error')
            link = root / 'catalog.json'
            link.symlink_to(target)
            with self.assertRaises(traversal.JsonReadError) as raised:
                traversal.read_json(link)
            self.assertEqual(raised.exception.diagnostic['reason'], 'not-regular')
            self.assertEqual(traversal.read_json(target), {})
            self.assertIsNone(traversal.read_json(root / 'absent.json', optional=True))

    def test_cli_reports_diagnostic_without_traceback_or_arbitrary_path(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, str(Path(traversal.__file__).resolve()),
                                     'merge', '--config', str(Path(folder) / 'private-input.json')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, '')
            value = json.loads(result.stdout)
            self.assertEqual(value['errorCode'], 'READ_FAILED')
            self.assertEqual(value['readDiagnostic']['inputKind'], 'other')
            self.assertEqual(value['readDiagnostic']['reason'], 'missing')
            self.assertNotIn(folder, result.stdout)


if __name__ == '__main__':
    unittest.main()
