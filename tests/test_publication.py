import importlib.util
import unittest
from pathlib import Path
spec = importlib.util.spec_from_file_location('guard', Path(__file__).parents[1] / 'scripts/publication_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

class PublicationTests(unittest.TestCase):
    def test_explicit_path_allowlist(self):
        self.assertTrue(guard.allowed_path('web/data/latest.json'))
        for item in ['.env', 'web/.env.json', 'raw/data.json', 'web/../../keys.txt', 'docs/private.key', 'web/file.bin']:
            self.assertFalse(guard.allowed_path(item), item)

    def test_identity_and_tokens(self):
        self.assertIn('credential', guard.inspect_content(('gh' + 'p_' + 'z' * 32).encode()))
        self.assertIn('machine_path', guard.inspect_content(('/Us' + 'ers/person/project/').encode()))
        self.assertIn('personal_email', guard.inspect_content(('person' + '@' + 'private-domain.test').encode()))
        self.assertEqual(guard.inspect_content(b'Automation <automation@users.noreply.github.com>'), [])

    def test_sensitive_url_and_binary(self):
        self.assertIn('url_credential', guard.inspect_content(('https://example.com?to' + 'ken=' + 'z' * 30).encode()))
        self.assertEqual(guard.inspect_content(b'abc\0def'), ['unsupported_content'])
        self.assertEqual(guard.inspect_content('基金报告：https://example.com/report.pdf'.encode()), [])

if __name__ == '__main__':
    unittest.main()
