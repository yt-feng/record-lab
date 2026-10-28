"""Stored fact compatibility is distinct from original response rechecking."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_verify_price_source_census import fixture, sample_checkout, scan, census, LATER
from price_representation import classify_stored_facts
import price_consistency as consistency
import price_daily_evidence as daily


class PriceRepresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        api, cls.trusted = fixture()
        with patch.object(census, 'PROOF', cls.trusted): cls.proof = census.restore_proof(api, LATER)
        with tempfile.TemporaryDirectory() as directory:
            sample_checkout(directory, cls.proof)
            file = Path(directory) / 'web/data/periods/2026Q3/companies/co1.json'
            document = json.loads(file.read_text())
            for index in range(22):
                fund = copy.deepcopy(document['funds'][index % 2]); fund['code'] = f'{index + 3:06d}'
                fund['rows'][0]['priceRange']['fetchedAt'] = f'2026-10-26T12:{index:02d}:01+00:00'
                document['funds'].append(fund)
            file.write_text(json.dumps(document))
            subprocess.run(['git', '-C', directory, 'add', '.'], check=True, capture_output=True)
            subprocess.run(['git', '-C', directory, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                            'commit', '-qm', 'Additional synthetic time observations'], check=True, capture_output=True)
            commit = subprocess.check_output(['git', '-C', directory, 'rev-parse', 'HEAD'], text=True).strip()
            cls.value = scan(directory, commit)

    def classify(self, value=None, proof=None, trusted=None):
        return classify_stored_facts(value or self.value, proof or self.proof, trusted or self.trusted)

    def test_24_observations_share_two_facts_but_only_two_bind_original_rechecks(self):
        original = copy.deepcopy(self.value); proof = copy.deepcopy(self.proof)
        result = self.classify()
        self.assertTrue(result['eligible'])
        self.assertEqual(result['classification'], 'stored-fact-representation-compatible')
        self.assertEqual(result['factCount'], 2)
        self.assertEqual(result['mathematicallyClassifiedObservationCount'], 24)
        self.assertEqual(result['directlyBoundRecheckObservationCount'], 2)
        self.assertEqual(result['uncoveredObservationCount'], 22)
        self.assertEqual(result['responseCoverage'], 'partial')
        self.assertFalse(result['automaticResolution'])
        self.assertFalse(result['quotesModified'])
        self.assertEqual(self.value, original); self.assertEqual(self.proof, proof)

    def test_same_float32_bucket_is_not_directional_representation_compatibility(self):
        proof = copy.deepcopy(self.proof)
        evidence = proof['dailyEvidence']
        source = next(row for row in evidence['sources'] if row['provider'] == 'yahoo')
        old = source['series']['rows'][0]['close']
        source['series']['rows'][0]['close'] = old + 1e-10
        self.assertEqual(daily.f32(old), daily.f32(old + 1e-10))
        original = copy.deepcopy(evidence); original.pop('evidenceSha256')
        evidence['evidenceSha256'] = hashlib.sha256(daily.encoded(original)).hexdigest()
        trusted = {**self.trusted, 'evidenceSha256': evidence['evidenceSha256']}; proof['provenance'] = trusted
        result = self.classify(proof=proof, trusted=trusted)
        self.assertFalse(result['eligible'])
        self.assertIn('DAILY_REPRESENTATION_NOT_VERIFIED', result['reasonCodes'])

    def test_new_fact_other_key_or_incomplete_census_never_becomes_explained(self):
        for change in ('new-fact', 'key', 'digest', 'incomplete'):
            value = copy.deepcopy(self.value)
            if change == 'new-fact':
                extra = copy.deepcopy(value['facts'][0]); extra['factId'] = '0' * 64; value['facts'].append(extra)
            if change == 'key': value['key'] = value['key'].replace('000636', '000637')
            if change == 'digest': value['rawRegistry']['digest'] = '0' * 64
            if change == 'incomplete': value['complete'] = False
            with self.subTest(change=change): self.assertFalse(self.classify(value=value)['eligible'])

    def test_new_raw_url_with_same_normalized_fact_is_not_reused_as_original_source(self):
        value = copy.deepcopy(self.value)
        obs = value['sourceObservations'][0]
        obs['sourceUrl'] += '&period2=9999999999'
        result = self.classify(value=value)
        self.assertFalse(result['eligible'])
        self.assertIn('SOURCE_OBSERVATION_NOT_FACT_MATCHED', result['reasonCodes'])

    def test_proof_hash_daily_cell_and_complete_calendar_are_recomputed(self):
        for change in ('hash', 'cell', 'day', 'metadata'):
            proof = copy.deepcopy(self.proof)
            evidence = proof['dailyEvidence']
            if change == 'hash': evidence['evidenceSha256'] = '0' * 64
            if change == 'cell': evidence['sources'][0]['series']['rows'][0]['close'] += 0.01
            if change == 'day': evidence['sources'][0]['series']['rows'].pop()
            if change == 'metadata': evidence['sources'][0]['series']['metadata']['market'] = 7
            with self.subTest(change=change): self.assertFalse(self.classify(proof=proof)['eligible'])


if __name__ == '__main__': unittest.main()
