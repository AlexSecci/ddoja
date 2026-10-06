"""Verify that an invented metric is rejected, using only fictional applicant data."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.store import Store
from app.workers import work_once

if __name__ == "__main__":
    demo = ROOT / "data" / "demos" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-negative")
    (demo / "config").mkdir(parents=True)
    shutil.copyfile(ROOT / "config" / "workers.json", demo / "config" / "workers.json")
    source = demo / "source.md"
    source.write_text("# Test Candidate\nBuilt a C++ scheduler. No performance measurements or percentages were supplied.\n", encoding="utf-8")
    store = Store(demo)
    store.import_document("master_resume", source)
    store.company("fictional", "Fictional company", "https://example.test/careers", True)
    job = store.discover("fictional", {"requisition_id": "NEGATIVE", "url": "https://example.test/NEGATIVE",
        "title": "C++ Engineer", "jd": "Full-time onsite C++ engineer in Austin, Texas, USA.", "country": "US",
        "employment": "full_time", "arrangement": "onsite", "location": "Austin, Texas"})
    store.evaluate(store.claim("evaluation", "fixture"), {"decision": "approve", "reason": "Fictional quality test."})
    store.store_package(store.claim("materials", "fixture"), {
        "resume_markdown": "# Test Candidate\n\n## Project\n- Built a C++ scheduler.\n- Reduced latency by 99%.\n",
        "cover_letter_markdown": "Dear team,\n\nI built a C++ scheduler.\n\nTest Candidate\n",
        "sources": [{"document_id": "master_resume", "excerpt": "Built a C++ scheduler."}], "answers": []})
    result = work_once(store, "review")
    final_job = store.get_job(job["id"])
    summary = {"fictional_data_only": True, "demo_root": str(demo), "result": result,
               "final_phase": final_job["phase"], "final_status": final_job["run_status"]}
    (demo / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    review = result["result"]["review"]
    rejected = review["decision"] == "fail" and not result["result"]["upload_ready"]
    print(json.dumps({"demo_root": str(demo), "fabricated_metric_rejected": rejected,
                     "issues": review["issues"], "final_status": final_job["run_status"]}, indent=2))
    if not rejected:
        raise RuntimeError("Negative factual review failed; keep live submission disabled.")
