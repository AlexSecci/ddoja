import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.store import Store
from app.workers import work_batch

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--max-tasks", type=int, default=20)
    args = parser.parse_args()
    store = Store(args.root)
    result = work_batch(store, args.max_tasks)
    (store.root / "data" / "last-batch-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "stop_reason": result.get("stop_reason", result.get("reason")),
        "tasks_completed": result["tasks_completed"], "jobs": [{"id": job["id"], "phase": job["phase"], "progress": job["run_status"]} for job in store.listing("jobs")]}, indent=2))
