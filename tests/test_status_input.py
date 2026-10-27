"""Tiny batch-artifact fixtures; no network or large snapshots."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_traversal as support

traversal = support.traversal
ROOT = Path(__file__).resolve().parents[1]
NOW = support.NOW
CONFIG = {**support.CONFIG, "funds": [{"code": "000001", "companyCode": "co1"}],
          "traversal": {"plannedAt": "2026-09-29T09:00:00Z"}}


def snapshot():
    return {"schemaVersion": 1, "sourceMode": "actions", "funds": [],
            "targetPeriodEnd": "2026-06-30", "baselinePeriodEnd": "2026-03-31",
            "retrievedAt": "2026-09-29T10:00:00.123456+00:00", "errors": [],
            "batchResults": [{"code": "000001", "status": "failed", "errorCode": "NO_REPORTS"}]}


class StatusInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "part.json"
        self.config = self.root / "batch.json"
        self.output = self.root / "status.json"
        self.config.write_text(json.dumps(CONFIG), encoding="utf-8")

    def read(self):
        return traversal.read_status_snapshot(self.input, CONFIG)

    def write(self, document):
        self.input.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def assert_bad(self, code):
        with self.assertRaises(traversal.StatusSnapshotError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, code)
        diagnostic = traversal.status_input_diagnostic(CONFIG, caught.exception, NOW)
        self.assertEqual(diagnostic["kind"], "batch-status-input-error")
        self.assertEqual(diagnostic["diagnosedAt"], "2026-09-29T12:00:00+00:00")
        self.assertNotIn("attemptedAt", diagnostic)
        self.assertNotIn("funds", diagnostic)
        self.assertNotIn("Count", json.dumps(diagnostic))
        self.assertNotIn(str(self.root), json.dumps(diagnostic))
        self.assertFalse(traversal.valid_status(diagnostic, CONFIG, NOW))
        return diagnostic

    def test_only_true_absence_retains_missing_batch_contract(self):
        self.assertIsNone(self.read())
        result = traversal.build_status(CONFIG, self.read(), NOW)
        self.assertEqual(result["funds"][0]["errorCode"], "BATCH_FAILED")
        self.input.write_text("null", encoding="utf-8")
        self.assert_bad("SNAPSHOT_INVALID_SCHEMA")

    def test_oversize_is_rejected_without_opening_or_reading_body(self):
        self.input.write_bytes(b"x" * 129)
        with patch.object(traversal, "MAX_STATUS_SNAPSHOT_BYTES", 128), patch.object(traversal.os, "open", side_effect=AssertionError("must not open")):
            diagnostic = self.assert_bad("SNAPSHOT_TOO_LARGE")
        self.assertEqual(diagnostic["snapshotInput"], {"state": "present-unreadable", "byteLength": 129, "maxBytes": 128})

    def test_exact_byte_cap_accepts_utf8_and_newline(self):
        document = snapshot()
        document["label"] = "季度合成数据"
        payload = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
        self.input.write_bytes(payload)
        with patch.object(traversal, "MAX_STATUS_SNAPSHOT_BYTES", len(payload)):
            self.assertEqual(self.read(), document)
        with patch.object(traversal, "MAX_STATUS_SNAPSHOT_BYTES", len(payload) - 1):
            self.assert_bad("SNAPSHOT_TOO_LARGE")

    def test_malformed_utf8_json_duplicate_keys_and_nonfinite_are_not_absence(self):
        fixtures = [b"\xff", b"", b"{", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}']
        for payload in fixtures:
            with self.subTest(payload=payload):
                self.input.write_bytes(payload)
                self.assert_bad("SNAPSHOT_INVALID_JSON")

    def test_bad_envelope_and_array_types_are_batch_input_errors(self):
        fixtures = [[], None, {}, {**snapshot(), "schemaVersion": True}, {**snapshot(), "schemaVersion": 2},
                    {**snapshot(), "sourceMode": "fixture"}, {**snapshot(), "funds": None}]
        for key in ("funds", "batchResults", "errors", "pricePatches", "inceptionPatches", "reportLocatorReceipts",
                    "strategyClaimsTasks", "strategyClaimsReceipts", "strategyClaimsPatches"):
            fixtures.extend([{**snapshot(), key: {}}, {**snapshot(), key: [None]}])
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.write(fixture)
                self.assert_bad("SNAPSHOT_INVALID_SCHEMA")

    def test_explicit_wrong_phase_is_batch_error_but_legacy_omission_remains_compatible(self):
        for key, value in (("targetPeriodEnd", "2026-09-30"), ("baselinePeriodEnd", "2025-12-31"), ("targetPeriodEnd", None)):
            self.write({**snapshot(), key: value})
            self.assert_bad("SNAPSHOT_PHASE_MISMATCH")
        document = snapshot()
        document.pop("targetPeriodEnd")
        document.pop("baselinePeriodEnd")
        self.write(document)
        self.assertEqual(self.read(), document)

    def test_symlink_dangling_symlink_and_directory_are_not_missing_files(self):
        target = self.root / "valid.json"
        target.write_text(json.dumps(snapshot()), encoding="utf-8")
        for destination in (target, self.root / "absent.json"):
            self.input.symlink_to(destination)
            self.assert_bad("SNAPSHOT_READ_FAILED")
            self.input.unlink()
        self.input.mkdir()
        self.assert_bad("SNAPSHOT_READ_FAILED")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires local FIFO support")
    def test_nonregular_file_is_rejected_without_blocking(self):
        os.mkfifo(self.input)
        self.assert_bad("SNAPSHOT_READ_FAILED")

    def test_disappearing_or_unreadable_existing_file_is_not_missing(self):
        self.write(snapshot())
        for error in (PermissionError("private path detail"), FileNotFoundError("changed after stat")):
            with patch.object(traversal.os, "open", side_effect=error):
                self.assert_bad("SNAPSHOT_READ_FAILED")

    def test_valid_product_failure_is_preserved_as_product_failure(self):
        self.write(snapshot())
        result = traversal.build_status(CONFIG, self.read(), NOW)
        self.assertEqual(result["funds"][0]["errorCode"], "NO_REPORTS")
        self.assertTrue(traversal.valid_status(result, CONFIG, NOW))

    def test_valid_fresh_proof_keeps_original_observation_time(self):
        document = snapshot()
        observed = document["retrievedAt"]
        document["batchResults"][0].update(status="complete", errorCode=None)
        document["funds"] = [{"code": "000001", "status": "ok", "dataStatus": "fresh", "retrievedAt": observed,
                              "selectedReport": {**support.evidence(), "holdings": [{"stockCode": "600001"}]},
                              "rows": [{"stockCode": "600001"}]}]
        self.write(document)
        loaded = self.read()
        self.assertEqual(loaded, document)
        result = traversal.build_status(CONFIG, loaded, NOW)
        self.assertEqual(result["funds"][0]["status"], "complete")
        self.assertEqual(result["funds"][0]["reportEvidence"]["observedAt"], observed)

    def test_metadata_only_receipt_arrays_and_deferred_input_are_not_rejected(self):
        document = snapshot()
        for key in ("pricePatches", "reportLocatorReceipts", "strategyClaimsReceipts"):
            document[key] = [{"code": "000001", "attemptedAt": "2026-09-29T09:30:00.123456+00:00"}]
        document["batchResults"][0] = {"code": "000001", "status": "deferred-budget", "attempted": False,
                                       "errorCode": "BATCH_DEADLINE_REACHED"}
        self.write(document)
        loaded = self.read()
        self.assertEqual(loaded, document)
        result = traversal.build_status(CONFIG, loaded, NOW)
        self.assertEqual(result["funds"][0]["status"], "deferred-budget")
        self.assertFalse(result["funds"][0]["attempted"])

    def test_diagnostic_cannot_consume_budget_even_with_injected_event_fields(self):
        self.input.write_bytes(b"{")
        diagnostic = self.assert_bad("SNAPSHOT_INVALID_JSON")
        baseline = traversal.merge_progress(support.catalog(), None, CONFIG, [], NOW)
        after = traversal.merge_progress(support.catalog(), None, CONFIG, [diagnostic], NOW)
        self.assertEqual(after, baseline)
        injected = {**support.event(status="failed"), **diagnostic}
        self.assertFalse(traversal.valid_status(injected, CONFIG, NOW))
        self.assertEqual(traversal.merge_progress(support.catalog(), None, CONFIG, [injected], NOW), baseline)

    def test_main_replaces_stale_status_with_diagnostic_and_never_prints_recorded(self):
        self.input.write_bytes(b"{")
        self.output.write_text(json.dumps(support.event(status="failed")), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(traversal.StatusSnapshotError):
            traversal.main(["status", "--config", str(self.config), "--snapshot", str(self.input), "--output", str(self.output)])
        self.assertEqual(output.getvalue(), "")
        diagnostic = json.loads(self.output.read_text())
        self.assertEqual(diagnostic["errorCode"], "SNAPSHOT_INVALID_JSON")
        self.assertNotIn("funds", diagnostic)
        self.assertNotIn("attemptedAt", diagnostic)

    def test_actual_cli_exits_nonzero_and_emits_only_fixed_error(self):
        self.input.write_text("private malformed content", encoding="utf-8")
        completed = subprocess.run([sys.executable, str(ROOT / "scripts/traversal.py"), "status", "--config", str(self.config),
                                    "--snapshot", str(self.input), "--output", str(self.output)], capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(json.loads(completed.stdout), {"status": "failed", "errorCode": "SNAPSHOT_INVALID_JSON"})
        self.assertEqual(completed.stderr, "")
        self.assertNotIn("private", self.output.read_text())


if __name__ == "__main__":
    unittest.main()
