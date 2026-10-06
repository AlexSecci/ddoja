"""Exercise real subscription workers against fictional data in an isolated database."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.store import Store
from app.workers import work_once

demo = ROOT / "data" / "demos" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
(demo / "config").mkdir(parents=True)
shutil.copyfile(ROOT / "config" / "workers.json", demo / "config" / "workers.json")
master = demo / "master.md"
master.write_text("# Test Candidate\nSoftware engineer. Built a C++ task scheduler used by an internal team.\n"
                  "No employment dates or quantitative metrics supplied.\n", encoding="utf-8")
profile = demo / "profile.md"
profile.write_text("Fictional test applicant. Seeks full-time US onsite/hybrid employment. Needs first H1B sponsorship. "
                   "Texas preferred. Do not invent experience, salary or start date.\n", encoding="utf-8")
store = Store(demo)
store.import_document("master_resume", master)
store.import_document("application_profile", profile)
store.learn("C++", "yes", "Built an internal task scheduler.", "fictional test fixture")
store.company("fictional", "Fictional test company", "https://example.test/careers", enabled=True)
record = store.discover("fictional", {"requisition_id": "DEMO-1", "url": "https://example.test/jobs/DEMO-1",
    "title": "Software Engineer, Task Scheduling", "location": "Austin, Texas", "country": "unknown",
    "employment": "unknown", "arrangement": "unknown",
    "jd": "Fictional job for testing. Full-time onsite software engineer in Austin, Texas, United States. Build a C++ task scheduler "
          "for internal teams. Requires hands-on C++ and task scheduling experience. First-time H1B sponsorship offered."})
reports = []
reports.append(work_once(store, "evaluation"))
if store.get_job(record["id"])["phase"] == "approved":
    reports.append(work_once(store, "materials"))
if store.get_job(record["id"])["run_status"] == "awaiting_material_review":
    reports.append(work_once(store, "review"))
summary = {"demo_root": str(demo), "fictional_data_only": True, "workers": reports,
           "final_phase": store.get_job(record["id"])["phase"], "final_status": store.get_job(record["id"])["run_status"]}
(demo / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps({"demo_root": str(demo), "fictional_data_only": True,
    "workers": [{"stage": item.get("run", {}).get("stage"), "status": item["status"],
                 "elapsed_seconds": item.get("run", {}).get("elapsed_seconds")} for item in reports],
    "final_phase": summary["final_phase"], "final_status": summary["final_status"]}, indent=2))
