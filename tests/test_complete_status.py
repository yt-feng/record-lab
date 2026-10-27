"""Recovery checks use tiny synthetic batch files only."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

spec = importlib.util.spec_from_file_location("complete_status", Path(__file__).parents[1] / "scripts/complete-status.py")
complete_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(complete_status)

NOW = "2026-09-29T12:00:00Z"
CONFIG = {"targetPeriodEnd": "2026-06-30", "baselinePeriodEnd": "2026-03-31",
          "funds": [{"code": "000001", "companyCode": "co1"}],
          "traversal": {"plannedAt": "2026-09-29T10:00:00Z"}}


class CompleteStatusTests(unittest.TestCase):
    def plan(self, root, count=1):
        plan, parts = root / "plan", root / "parts"
        plan.mkdir()
        (plan / "matrix.json").write_text(json.dumps([{"id": str(index), "config": f"batch-{index}.json"} for index in range(count)]))
        for index in range(count):
            (plan / f"batch-{index}.json").write_text(json.dumps(CONFIG))
            (parts / f"part-{index}").mkdir(parents=True)
        return plan, parts

    def test_missing_record_is_bounded_failure_and_replay_preserves_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, parts = root / "plan", root / "parts"
            plan.mkdir()
            (plan / "matrix.json").write_text(json.dumps([{"id": "0", "config": "batch-0.json"}]))
            (plan / "batch-0.json").write_text(json.dumps(CONFIG))
            first = complete_status.reconcile(plan, parts, NOW)
            status = parts / "part-0/status.json"
            original = status.read_bytes()
            self.assertEqual(first["repairedCount"], 1)
            self.assertEqual(json.loads(original)["funds"][0]["errorCode"], "BATCH_FAILED")
            second = complete_status.reconcile(plan, parts, "2026-09-29T12:30:00Z")
            self.assertEqual(second["preservedCount"], 1)
            self.assertEqual(status.read_bytes(), original)

    def test_wrong_schema_phase_codes_time_and_malformed_json_are_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, parts = root / "plan", root / "parts"
            plan.mkdir()
            destination = parts / "part-0"
            destination.mkdir(parents=True)
            (plan / "matrix.json").write_text(json.dumps([{"id": "0", "config": "batch-0.json"}]))
            (plan / "batch-0.json").write_text(json.dumps(CONFIG))
            valid = complete_status.build_status(CONFIG, None, NOW)
            invalid = ["{", {**valid, "schemaVersion": 2}, {**valid, "phase": {}},
                       {**valid, "funds": []}, {**valid, "attemptedAt": "2026-09-29T09:00:00Z"},
                       {**valid, "attemptedAt": "2026-09-30T12:00:00Z"}]
            for value in invalid:
                (destination / "status.json").write_text(value if isinstance(value, str) else json.dumps(value))
                result = complete_status.reconcile(plan, parts, NOW)
                self.assertEqual(result["repairedCount"], 1)
                self.assertTrue(complete_status.valid_status(json.loads((destination / "status.json").read_text()), CONFIG, NOW))

    def test_existing_invalid_snapshot_creates_no_product_failure_and_blocks_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, parts = self.plan(Path(temporary))
            root = parts / "part-0"
            (root / "part.json").write_text("{private malformed fixture")
            with self.assertRaises(complete_status.BatchStatusInputError) as caught:
                complete_status.reconcile(plan, parts, NOW)
            self.assertEqual(caught.exception.summary, {"status": "partial", "batchCount": 1, "repairedCount": 0,
                                                        "preservedCount": 0, "inputErrorCount": 1,
                                                        "inputErrors": [{"batchId": "0", "errorCode": "SNAPSHOT_INVALID_JSON"}],
                                                        "missingPartCount": 0, "missingPartIds": []})
            saved = json.loads((root / "status.json").read_text())
            self.assertEqual(saved["kind"], "batch-status-input-error")
            self.assertEqual(saved["errorCode"], "SNAPSHOT_INVALID_JSON")
            self.assertNotIn("funds", saved)
            self.assertNotIn("attemptedAt", saved)
            self.assertNotIn("private", json.dumps(saved))

    def test_diagnostic_survives_missing_rejected_part_and_replay_keeps_original_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, parts = self.plan(Path(temporary))
            root = parts / "part-0"
            (root / "part.json").write_text("{")
            with self.assertRaises(complete_status.BatchStatusInputError):
                complete_status.reconcile(plan, parts, NOW)
            before = (root / "status.json").read_bytes()
            (root / "part.json").unlink()
            with self.assertRaises(complete_status.BatchStatusInputError):
                complete_status.reconcile(plan, parts, "2026-09-29T12:30:00Z")
            self.assertEqual((root / "status.json").read_bytes(), before)

    def test_error_in_one_part_preserves_other_original_valid_attempt_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, parts = self.plan(Path(temporary), 2)
            original = (json.dumps(complete_status.build_status(CONFIG, None, NOW), indent=2) + "\n").encode()
            (parts / "part-0/status.json").write_bytes(original)
            (parts / "part-1/part.json").write_text("null")
            with self.assertRaises(complete_status.BatchStatusInputError) as caught:
                complete_status.reconcile(plan, parts, "2026-09-29T12:30:00Z")
            self.assertEqual(caught.exception.summary["preservedCount"], 1)
            self.assertEqual(caught.exception.summary["inputErrorCount"], 1)
            self.assertEqual(caught.exception.summary["repairedCount"], 0)
            self.assertEqual((parts / "part-0/status.json").read_bytes(), original)

    def test_oversize_part_uses_strict_snapshot_reader_in_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, parts = self.plan(Path(temporary))
            (parts / "part-0/part.json").write_bytes(b"x" * 129)
            module = complete_status.read_status_snapshot.__globals__
            with patch.dict(module, MAX_STATUS_SNAPSHOT_BYTES=128):
                with self.assertRaises(complete_status.BatchStatusInputError):
                    complete_status.reconcile(plan, parts, NOW)
            saved = json.loads((parts / "part-0/status.json").read_text())
            self.assertEqual(saved["errorCode"], "SNAPSHOT_TOO_LARGE")
            self.assertEqual(saved["snapshotInput"]["byteLength"], 129)

    def test_invalid_diagnostic_is_not_replaced_with_a_product_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, parts = self.plan(Path(temporary))
            root = parts / "part-0"
            (root / "part.json").write_text("{")
            with self.assertRaises(complete_status.BatchStatusInputError):
                complete_status.reconcile(plan, parts, NOW)
            valid = json.loads((root / "status.json").read_text())
            (root / "part.json").unlink()
            invalid = [{**valid, "phase": {}}, {**valid, "schemaVersion": True}, {**valid, "errorCode": "private detail"},
                       {**valid, "attemptedAt": NOW}, {**valid, "diagnosedAt": "2026-10-01T12:00:00Z"},
                       {**valid, "snapshotInput": {**valid["snapshotInput"], "byteLength": True}}]
            for value in invalid:
                before = json.dumps(value).encode()
                (root / "status.json").write_bytes(before)
                with self.assertRaisesRegex(ValueError, "^INVALID_STATUS_INPUT_DIAGNOSTIC$"):
                    complete_status.reconcile(plan, parts, NOW)
                self.assertEqual((root / "status.json").read_bytes(), before)

    def test_ordinary_valid_snapshot_recovery_and_missing_behavior_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, parts = self.plan(Path(temporary), 2)
            document = {"schemaVersion": 1, "sourceMode": "actions", "funds": [],
                        "batchResults": [{"code": "000001", "status": "deferred-budget", "attempted": False,
                                          "errorCode": "BATCH_DEADLINE_REACHED"}]}
            (parts / "part-0/part.json").write_text(json.dumps(document))
            summary = complete_status.reconcile(plan, parts, NOW)
            self.assertEqual(summary["inputErrorCount"], 0)
            self.assertEqual(summary["repairedCount"], 2)
            self.assertEqual(summary["missingPartCount"], 1)
            self.assertEqual(summary["missingPartIds"], ["1"])
            deferred = json.loads((parts / "part-0/status.json").read_text())["funds"][0]
            self.assertEqual(deferred["status"], "deferred-budget")
            self.assertFalse(deferred["attempted"])
            self.assertEqual(json.loads((parts / "part-1/status.json").read_text())["funds"][0]["errorCode"], "BATCH_FAILED")


if __name__ == "__main__":
    unittest.main()
