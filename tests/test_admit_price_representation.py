"""Synthetic fixed-proof admission changes only its independent document."""
import base64
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_verify_price_source_census import fixture, sample_checkout, scan, census, LATER
import admit_price_representation as admission


@contextlib.contextmanager
def prepared():
    api, expected = fixture()
    with patch.object(census, 'PROOF', expected): proof = census.restore_proof(api, LATER)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'production'; commit = sample_checkout(root, proof, new_observation=True)
        value = scan(root, commit); evidence = Path(directory) / 'evidence'; evidence.mkdir()
        (evidence / 'census.json').write_text(json.dumps(value))
        (evidence / 'proof.json').write_text(json.dumps(proof))
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': census.restore.REPOSITORY,
               'GITHUB_WORKFLOW_REF': f'{census.restore.REPOSITORY}/{census.WORKFLOW}@refs/heads/main'}
        with patch.dict(admission.os.environ, env), patch.object(census, 'PROOF', expected), patch.object(census, 'DATA_COMMIT', commit):
            yield root, evidence, api, proof


class PriceRepresentationAdmissionTests(unittest.TestCase):
    def test_admission_and_idempotent_replay_preserve_every_source_byte(self):
        with prepared() as (root, evidence, api, proof):
            original = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / 'web/data').rglob('*') if p.is_file()}
            old_proof = copy.deepcopy(proof)
            result = admission.admit(evidence, root, api, LATER)
            self.assertEqual(result['status'], 'staged')
            target = root / admission.PUBLIC_PATH; first = target.read_bytes()
            document = json.loads(first)
            self.assertEqual(document['records'][0]['mathematicallyClassifiedObservationCount'], 2)
            self.assertEqual(document['records'][0]['uncoveredObservationCount'], 1)
            self.assertEqual(admission.admit(evidence, root, api, LATER)['status'], 'unchanged')
            self.assertEqual(target.read_bytes(), first)
            self.assertEqual(proof, old_proof)
            for name, raw in original.items(): self.assertEqual((root / name).read_bytes(), raw)
            paths = {p.relative_to(root).as_posix() for p in (root / 'web/data').rglob('*') if p.is_file()}
            self.assertEqual(paths - set(original), {admission.PUBLIC_PATH.as_posix()})

    def test_modified_proof_and_ineligible_census_cannot_write_public_data(self):
        for change in ('proof', 'incomplete'):
            with prepared() as (root, evidence, api, proof):
                file = evidence / ('proof.json' if change == 'proof' else 'census.json')
                value = json.loads(file.read_text())
                if change == 'proof': value['coveredObservations'][0]['fetchedAt'] = LATER.isoformat()
                else: value['complete'] = False
                file.write_text(json.dumps(value))
                with self.subTest(change=change), self.assertRaises(ValueError): admission.admit(evidence, root, api, LATER)
                self.assertFalse((root / admission.PUBLIC_PATH).exists())

    def test_remote_readback_requires_exact_persisted_content(self):
        with prepared() as (root, evidence, api, proof):
            admission.admit(evidence, root, api, LATER); raw = (root / admission.PUBLIC_PATH).read_bytes()
            class ReadbackAPI:
                def get(self, path):
                    self.path = path
                    return {'encoding': 'base64', 'content': base64.b64encode(raw).decode()}
            reader = ReadbackAPI()
            self.assertEqual(admission.verify_readback(root, reader)['status'], 'published-and-read-back')
            self.assertEqual(reader.path, 'contents/' + admission.PUBLIC_PATH.as_posix() + '?ref=main')
            raw = raw + b' '
            with self.assertRaisesRegex(ValueError, 'READBACK_MISMATCH'): admission.verify_readback(root, reader)

    def test_local_gate_precedes_any_artifact_or_quote_access(self):
        with patch.dict(admission.os.environ, {'GITHUB_ACTIONS': 'false'}), \
                patch.object(census, 'restore_proof', side_effect=AssertionError('no source request')):
            with self.assertRaisesRegex(ValueError, 'REQUIRES_ACTIONS'): admission.admit('unused')


if __name__ == '__main__': unittest.main()
