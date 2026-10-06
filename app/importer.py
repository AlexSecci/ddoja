"""Import canonical local records; preserve origins and submission evidence types."""
from __future__ import annotations

import csv
from pathlib import Path
import re

from .store import Store


def import_existing(store: Store, source: Path):
    report = {"documents_changed": 0, "technologies_changed": 0, "prior_submissions": 0,
              "individual_receipts_imported": 0, "unverified_legacy_submitted_ids": []}
    for document_id, filename in (("master_resume", "master-resume.md"),
                                  ("application_profile", "application-profile.md"),
                                  ("technology_history", "technology-history.md")):
        report["documents_changed"] += int(store.import_document(document_id, source / filename))
    history = (source / "technology-history.md").read_text(encoding="utf-8-sig")
    for line in history.splitlines():
        if not line.startswith("|"):
            continue
        fields = [field.strip() for field in line.strip().strip("|").split("|")]
        if len(fields) < 4 or fields[1].lower() not in {"yes", "no", "unknown"}:
            continue
        if len(fields) >= 6:
            name, answer, aliases, context, provenance, confirmed = fields[:6]
        else:
            name, answer, context, provenance = fields[:4]
            aliases, confirmed = "", "date recorded in source"
        report["technologies_changed"] += int(store.learn(name, answer.lower(), context,
            f"technology_history: {provenance}; confirmed {confirmed}", aliases.split(";")))

    sweep = source / "tesla-sweep"
    snapshot = sweep / "account-check-after-missing-tabs.md"
    content = snapshot.read_text(encoding="utf-8-sig")
    confirmed_jobs = dict((req, title.strip()) for title, req in
                          re.findall(r"^- generic: (.+)/(\d+)\s*$", content, re.M))
    reported_total = re.search(r"- generic: (\d+) Results", content)
    if not reported_total or len(confirmed_jobs) != int(reported_total[1]):
        raise ValueError("Saved employer-account total does not match parsed requisitions; reconciliation needs review.")
    with (sweep / "application-tracker.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = {row["requisition_id"]: row for row in csv.DictReader(stream)}
    store.company("tesla", "Tesla", "https://www.tesla.com/careers", enabled=False)
    for req, title in confirmed_jobs.items():
        row = rows.get(req, {})
        receipt = sweep / "applications" / req / "receipt.md"
        # This imported record is a submission ledger entry, not a fabricated JD.
        job = store.discover("tesla", {
            "requisition_id": req, "url": row.get("job_url") or f"https://www.tesla.com/careers/search/job/{req}",
            "title": title, "location": row.get("location") or "Unknown in imported account entry",
            "country": "unknown", "employment": "unknown", "arrangement": "unknown",
            "jd": "Legacy application ledger entry. Full description not imported in this reconciliation.",
            "source": str(snapshot),
        })
        store.import_submission(job["id"], snapshot, "employer_account", str(snapshot), attribution="unknown")
        report["prior_submissions"] += 1
        if receipt.exists():
            store.import_submission(job["id"], receipt, "receipt", str(receipt), attribution="unknown")
            report["individual_receipts_imported"] += 1
    report["unverified_legacy_submitted_ids"] = sorted(req for req, row in rows.items()
        if row["status"] == "submitted" and req not in confirmed_jobs)
    return report
