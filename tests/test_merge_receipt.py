"""Small cross-runtime fixtures for oversized transient receipt pagination."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from merge_receipt import read_merge_receipt
import merge_receipt
import traversal

NODE = r"""
const f=require('./tests/price-reference-fixture');
const {finalObservation}=require('./scripts/merge-parts');
const {writeMergeReceipt}=require('./lib/merge-receipt');
const x=f.fixture();
const fund=f.snapshot('000001','eastmoney').funds[0]; fund.companyId='co1';
fund.rows[0].priceRange={...fund.rows[0].priceRange,rangeReference:x.reference,globalPriceConflict:x.witness};
fund.sourcePortfolioCode='000001';fund.shareClassCodes=['000001','000002'];fund.portfolioSourceUrl='https://example.org/identity.pdf';
const observation=finalObservation(fund);
const receipt={schemaVersion:1,phase:f.phase,generatedAt:f.now,requestedCodes:['000001','000002'],
  funds:['000001','000002'].map(code=>({code,...observation})),
  pricePatches:[{code:'000001',accepted:false}],strategyClaims:[],extraMetadata:{originalAt:f.fetchedAt}};
writeMergeReceipt(process.argv[1],receipt,{legacyLimit:1000,pageLimit:8000});
process.stdout.write(JSON.stringify(receipt));
"""


class MergeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'merge-receipt.json'
        result = subprocess.run(['node', '-e', NODE, str(self.path)], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        self.original = json.loads(result.stdout)
        self.manifest = json.loads(self.path.read_text())
        self.original_pages = {row['path']: (self.path.parent / row['path']).read_bytes()
                               for row in self.manifest['fundPages']}

    def read(self):
        return read_merge_receipt(self.path, read_json=traversal.read_json, error_type=traversal.TraversalError)

    def reset(self):
        self.path.write_text(json.dumps(self.manifest))
        for name, raw in self.original_pages.items():
            (self.path.parent / name).write_bytes(raw)

    def rewrite_page(self, index, mutate):
        manifest = json.loads(self.path.read_text())
        row = manifest['fundPages'][index]
        old = self.path.parent / row['path']
        value = json.loads(old.read_bytes()); mutate(value)
        raw = json.dumps(value, separators=(',', ':')).encode()
        digest = hashlib.sha256(raw).hexdigest()
        name = f'merge-receipt-pages/{index:04d}-{digest}.json'
        (self.path.parent / name).write_bytes(raw)
        manifest['totalPageBytes'] += len(raw) - row['byteLength']
        row.update(path=name, sha256=digest, byteLength=len(raw))
        self.path.write_text(json.dumps(manifest))

    def test_decode_and_reconciliation_equal_the_exact_legacy_receipt(self):
        self.assertGreater(len(self.manifest['fundPages']), 1)
        actual = self.read()
        self.assertEqual(actual, self.original)
        config = {**self.original['phase'], 'funds': [{'code': row['code'], 'companyCode': 'co1'}
                                                    for row in actual['funds']]}
        status = {'schemaVersion': 1, 'phase': self.original['phase'], 'attemptedAt': self.original['generatedAt'],
                  'funds': [{'code': row['code'], 'companyCode': 'co1', 'status': 'complete'} for row in actual['funds']]}
        legacy = traversal.reconcile_observations([copy.deepcopy(status)], self.original, config, self.original['generatedAt'])
        paged = traversal.reconcile_observations([copy.deepcopy(status)], actual, config, self.original['generatedAt'])
        self.assertEqual(paged, legacy)
        self.assertEqual(len(paged[0]['portfolioAliases']), 1)
        for row in paged[0]['funds']:
            self.assertEqual(row['priceCoverage']['priceComplete'], 1)
            self.assertEqual(row['priceCoverage']['referenceResolutions'], self.original['funds'][0]['priceCoverage']['referenceResolutions'])
            self.assertEqual(row['reportEvidence']['observedAt'], '2026-10-28T10:00:00.000001Z')
        self.path.write_text(json.dumps(self.original))
        self.assertEqual(self.read(), self.original)

    def test_bad_manifest_metadata_is_rejected_before_pages_are_opened(self):
        mutations = [lambda x: x.update(fundCount=x['fundCount'] + 1),
                     lambda x: x.update(totalPageBytes=merge_receipt.MAX_TOTAL_BYTES + 1),
                     lambda x: x['fundPages'][0].update(byteLength=merge_receipt.MAX_PAGE_BYTES + 1),
                     lambda x: x['fundPages'][0].update(path='../outside.json'),
                     lambda x: x['fundPages'].reverse(),
                     lambda x: x['fundPages'].append(copy.deepcopy(x['fundPages'][0])),
                     lambda x: x['fundPages'].pop(),
                     lambda x: x['receipt'].update(requestedCodes=['000001', '000001']),
                     lambda x: x.update(fundCount=True)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = copy.deepcopy(self.manifest); mutate(value); self.path.write_text(json.dumps(value))
                with patch.object(merge_receipt.os, 'open') as opened:
                    # The existing manifest reader opens the manifest normally.
                    with self.assertRaises(traversal.TraversalError):
                        read_merge_receipt(self.path, read_json=lambda _: value, error_type=traversal.TraversalError)
                    opened.assert_not_called()

    def test_missing_tampered_and_symlinked_pages_are_rejected(self):
        target = self.path.parent / self.manifest['fundPages'][0]['path']
        for kind in ('missing', 'truncated', 'tampered', 'symlink'):
            with self.subTest(kind=kind):
                self.reset()
                raw = target.read_bytes()
                if kind == 'missing': target.unlink()
                elif kind == 'truncated': target.write_bytes(raw[:-1])
                elif kind == 'tampered': target.write_bytes(raw.replace(b'600001', b'600002', 1))
                else:
                    target.unlink(); target.symlink_to(self.path)
                with self.assertRaises(traversal.TraversalError): self.read()
                if target.is_symlink(): target.unlink()

    def test_hash_valid_but_wrong_phase_clock_codes_and_rows_are_rejected(self):
        mutations = [lambda x: x['phase'].update(targetPeriodEnd='2026-09-30'),
                     lambda x: x.update(generatedAt='2026-10-28T00:00:00Z'),
                     lambda x: x['funds'][0].update(code='999999'),
                     lambda x: x.update(funds=[]),
                     lambda x: x['funds'].append(copy.deepcopy(x['funds'][0]))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.reset(); self.rewrite_page(0, mutate)
                with self.assertRaises(traversal.TraversalError): self.read()

    def test_page_directory_symlink_is_rejected(self):
        root = self.path.parent / 'merge-receipt-pages'
        moved = self.path.parent / 'moved'
        root.rename(moved); root.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(traversal.TraversalError): self.read()


if __name__ == '__main__':
    unittest.main()
