"""Reconcile missing or invalid attempt records without replaying valid events."""
import json
from pathlib import Path

try:
    from scripts.traversal import (atomic_json, build_status, read_json, valid_status, read_status_snapshot,
                                   StatusSnapshotError, status_input_diagnostic, valid_status_input_diagnostic, source_repair_mode)
except ModuleNotFoundError:
    from traversal import (atomic_json, build_status, read_json, valid_status, read_status_snapshot,
                           StatusSnapshotError, status_input_diagnostic, valid_status_input_diagnostic, source_repair_mode)


class BatchStatusInputError(Exception):
    def __init__(self, summary):
        self.summary = summary
        super().__init__("BATCH_STATUS_INPUT_ERROR")


def reconcile(plan_root=".cache/plan", parts_root=".cache/parts", now=None):
    plan = Path(plan_root)
    parts = Path(parts_root)
    matrix = read_json(plan / "matrix.json")
    if not isinstance(matrix, list) or len(matrix) > 64:
        raise ValueError("INVALID_PLAN")
    if len(matrix) > 32:
        # Verify every enlarged-plan lane before creating any status artifact.
        for item in matrix:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                    or not item['id'].isdigit() or item.get('config') != f"batch-{item['id']}.json"
                    or not source_repair_mode(read_json(plan / item['config']))):
                raise ValueError("INVALID_PLAN")
    repaired = 0
    preserved = 0
    input_errors = 0
    missing_parts = []
    input_error_rows = []
    seen = set()
    for item in matrix:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].isdigit():
            raise ValueError("INVALID_PLAN")
        if item["id"] in seen or item.get("config") != f"batch-{item['id']}.json":
            raise ValueError("INVALID_PLAN")
        seen.add(item["id"])
        config = read_json(plan / item["config"])
        root = parts / ("part-" + item["id"])
        try:
            saved = read_json(root / "status.json", optional=True)
        except Exception:
            saved = None
        if isinstance(saved, dict) and saved.get("kind") == "batch-status-input-error":
            if not valid_status_input_diagnostic(saved, config, now):
                raise ValueError("INVALID_STATUS_INPUT_DIAGNOSTIC")
            # The diagnostic is not an attempt. Preserve its original bytes and
            # time even when the rejected part was intentionally not uploaded.
            input_errors += 1
            input_error_rows.append({"batchId": item["id"], "errorCode": saved["errorCode"]})
            continue
        try:
            snapshot = read_status_snapshot(root / "part.json", config)
        except StatusSnapshotError as error:
            atomic_json(root / "status.json", status_input_diagnostic(config, error, now))
            input_errors += 1
            input_error_rows.append({"batchId": item["id"], "errorCode": error.code})
            continue
        if snapshot is None:
            missing_parts.append(item["id"])
        if valid_status(saved, config, now):
            preserved += 1
            continue
        atomic_json(root / "status.json", build_status(config, snapshot, now))
        repaired += 1
    summary = {"status": "partial" if input_errors else "reconciled", "batchCount": len(matrix),
               "repairedCount": repaired, "preservedCount": preserved, "inputErrorCount": input_errors,
               "inputErrors": input_error_rows, "missingPartCount": len(missing_parts), "missingPartIds": missing_parts}
    if input_errors:
        # campaign.publish calls this function directly and ignores its return
        # value. Raising prevents a bad input being reported as a successful
        # publish while leaving every valid attempt and part intact for repair.
        raise BatchStatusInputError(summary)
    return summary


if __name__ == "__main__":
    try:
        print(json.dumps(reconcile()))
    except BatchStatusInputError as error:
        print(json.dumps({**error.summary, "errorCode": "BATCH_STATUS_INPUT_ERROR"}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"status": "failed", "errorCode": "BATCH_ATTEMPT_RECONCILIATION_FAILED"}))
        raise SystemExit(1) from None
