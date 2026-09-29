"""Reconcile missing or invalid attempt records without replaying valid events."""
import json
from pathlib import Path

try:
    from scripts.traversal import atomic_json, build_status, read_json, valid_status
except ModuleNotFoundError:
    from traversal import atomic_json, build_status, read_json, valid_status


def reconcile(plan_root=".cache/plan", parts_root=".cache/parts", now=None):
    plan = Path(plan_root)
    parts = Path(parts_root)
    matrix = read_json(plan / "matrix.json")
    if not isinstance(matrix, list) or len(matrix) > 32:
        raise ValueError("INVALID_PLAN")
    repaired = 0
    preserved = 0
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
        if valid_status(saved, config, now):
            preserved += 1
            continue
        try:
            snapshot = read_json(root / "part.json", optional=True)
        except Exception:
            snapshot = None
        atomic_json(root / "status.json", build_status(config, snapshot, now))
        repaired += 1
    return {"status": "reconciled", "batchCount": len(matrix), "repairedCount": repaired, "preservedCount": preserved}


if __name__ == "__main__":
    try:
        print(json.dumps(reconcile()))
    except Exception:
        print(json.dumps({"status": "failed", "errorCode": "BATCH_ATTEMPT_RECONCILIATION_FAILED"}))
        raise SystemExit(1) from None
