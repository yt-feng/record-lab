"""Recovery checks use tiny synthetic batch files only."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("complete_status", Path(__file__).parents[1] / "scripts/complete-status.py")
complete_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(complete_status)

NOW = "2026-09-29T12:00:00Z"
CONFIG = {"targetPeriodEnd": "2026-06-30", "baselinePeriodEnd": "2026-03-31",
          "funds": [{"code": "000001", "companyCode": "co1"}],
          "traversal": {"plannedAt": "2026-09-29T10:00:00Z"}}


class CompleteStatusTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
