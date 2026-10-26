"""Exercise the runner checkout pattern against actual shared JS dependencies."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


class SparseBatchTests(unittest.TestCase):
    def test_batch_includes_shared_validators_without_bulk_public_data(self):
        source = Path(__file__).resolve().parents[1]
        workflow = (source / '.github/workflows/traverse.yml').read_text()
        self.assertIn('sparse-checkout-cone-mode: false', workflow)
        match = re.search(r'          sparse-checkout: \|\n((?:            [^\n]+\n)+)', workflow)
        self.assertIsNotNone(match)
        patterns = '\n'.join(line.strip() for line in match[1].splitlines()) + '\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ('scripts', 'lib', 'config'):
                shutil.copytree(source / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'web/data').mkdir(parents=True)
            (root / 'web/data/large.json').write_text('{}')
            for name in ('lifecycle.js', 'zero-stock.js', 'nonstock-assets.js', 'strategy-sections.js'):
                shutil.copy2(source / 'web' / name, root / 'web' / name)
            (root / 'requirements.txt').write_text('')
            def git(*args, **kwargs):
                return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True, **kwargs)
            git('init', '-q')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture')
            git('sparse-checkout', 'set', '--no-cone', '--stdin', input=patterns)
            self.assertFalse((root / 'web/data/large.json').exists())
            self.assertTrue((root / 'requirements.txt').exists())
            subprocess.run(['node', '-e', "require('./scripts/build-data'); require('./scripts/analyze-data');"],
                           cwd=root, check=True, capture_output=True)
            subprocess.run(['python3', '-c', "import sys;sys.path.insert(0,'scripts');import collect_prices,price_patches"],
                           cwd=root, check=True, capture_output=True)


if __name__ == '__main__': unittest.main()
