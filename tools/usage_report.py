"""Export observed task usage without starting workers or changing pipeline state."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.timing import summarize_timings

TOKENS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_tokens")
TIMES = ("worker_elapsed_seconds", "startup_seconds", "browser_tool_seconds", "other_tool_seconds",
         "all_tool_seconds", "model_and_transport_seconds")
ROLES = ("discovery", "evaluator", "document", "submission")


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def token_totals(rows):
    result = {"usage_invocations": len(rows)}
    for key in TOKENS:
        values = [r[key] for r in rows if r.get(key) is not None]
        result[key] = sum(values) if values else None
        result["missing_" + key] = len(rows) - len(values)
    return result


def export_report(pipeline_id, output):
    now = datetime.now(timezone.utc)
    with sqlite3.connect((ROOT / "data/pipeline.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        pipeline = dict(db.execute("SELECT p.id,c.name company,p.status,p.created_at FROM pipelines p "
                                   "JOIN companies c ON c.id=p.company_id WHERE p.id=?", (pipeline_id,)).fetchone())
        workers = [dict(r) for r in db.execute(
            "SELECT w.id worker_id,w.role,w.state,w.job_id,w.parent_id,w.profile_version,w.created_at,w.started_at,"
            "w.completed_at,w.updated_at,j.requisition_id,j.title FROM agent_workers w "
            "LEFT JOIN jobs j ON j.id=w.job_id WHERE w.pipeline_id=? ORDER BY w.created_at,w.id", (pipeline_id,))]
        raw_usage = [dict(r) for r in db.execute(
            "SELECT u.* FROM agent_usage u JOIN agent_workers w ON w.id=u.worker_id "
            "WHERE w.pipeline_id=? ORDER BY u.rowid", (pipeline_id,))]
        events = [dict(r) for r in db.execute(
            "SELECT e.worker_id,e.session_id,e.event_type,e.payload,e.created_at FROM agent_runtime_events e "
            "JOIN agent_workers w ON w.id=e.worker_id WHERE w.pipeline_id=? "
            "AND e.event_type IN ('usage','timing_summary') ORDER BY e.rowid", (pipeline_id,))]
        jobs = [dict(r) for r in db.execute(
            "SELECT j.id job_id,j.requisition_id,j.title,pj.agent_decision,pj.agent_status "
            "FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id WHERE pj.pipeline_id=?", (pipeline_id,))]
        artifacts = [dict(r) for r in db.execute(
            "SELECT worker_id,kind,status,count(*) n FROM agent_artifacts WHERE pipeline_id=? "
            "GROUP BY worker_id,kind,status", (pipeline_id,))]
        campaigns = [dict(r) for r in db.execute(
            "SELECT p.id pipeline_id,c.name company,p.status,count(u.worker_id) usage_invocations,"
            "sum(u.delta_input_tokens) input_tokens,sum(u.delta_cached_input_tokens) cached_input_tokens,"
            "sum(u.delta_input_tokens-u.delta_cached_input_tokens) uncached_input_tokens,"
            "sum(u.delta_output_tokens) output_tokens FROM pipelines p JOIN companies c ON c.id=p.company_id "
            "LEFT JOIN agent_workers w ON w.pipeline_id=p.id LEFT JOIN agent_usage u ON u.worker_id=w.id "
            "GROUP BY p.id ORDER BY p.created_at")]

    worker_index = {w["worker_id"]: w for w in workers}
    usage = []
    for row in raw_usage:
        w = worker_index[row["worker_id"]]
        item = {key: row[key] for key in ("worker_id", "session_id", "turn_id", "counter_source", "created_at")}
        item.update({key: w[key] for key in ("role", "state", "job_id", "requisition_id", "title")})
        for key in TOKENS:
            if key != "uncached_input_tokens":
                item[key] = row["delta_" + key]
        item["uncached_input_tokens"] = (item["input_tokens"] - item["cached_input_tokens"]
            if item["input_tokens"] is not None and item["cached_input_tokens"] is not None else None)
        item["usage_reported_execution_seconds"] = row["elapsed_seconds"]
        item["tool_call_count"] = row["tool_call_count"]
        usage.append(item)

    # The usage event precedes its timing summary within the same invocation.
    # CLI turn IDs also equal the timing run ID. App-server IDs are different.
    by_turn = {(u["worker_id"], u["session_id"], u["turn_id"]): u for u in usage}
    pending_usage = {}
    timings = {}
    for event in events:
        payload = json.loads(event["payload"])
        scope = (event["worker_id"], event["session_id"])
        if event["event_type"] == "usage":
            pending_usage[scope] = payload.get("turn_id")
            continue
        timing = payload.get("timing", {})
        if summarize_timings([timing])["measured_turns"] != 1:
            continue
        key = (*scope, timing["run_id"])
        if key not in by_turn:
            key = (*scope, pending_usage.get(scope))
        matched = by_turn.get(key)
        # Avoid attaching a previous invocation's counters to a quota failure.
        if matched and not matched.get("timing_run_id"):
            matched["timing_run_id"] = timing["run_id"]
            matched.update({name: timing[name] for name in TIMES})
            matched["timing_match"] = ("run_id" if key[-1] == timing["run_id"] else "ordered_same_session_events")
        timings[timing["run_id"]] = {"worker_id": event["worker_id"], "session_id": event["session_id"], **timing}
        pending_usage.pop(scope, None)

    per_worker = []
    for w in workers:
        own_usage = [u for u in usage if u["worker_id"] == w["worker_id"]]
        own_timing = [t for t in timings.values() if t["worker_id"] == w["worker_id"]]
        measured = summarize_timings(own_timing)
        item = {**w, **token_totals(own_usage), "timed_invocations": measured["measured_turns"]}
        item.update({name: measured[name] for name in TIMES})
        item["browser_calls"] = measured["browser_calls"]
        item["invocations_with_usage_and_timing"] = sum(bool(u.get("timing_run_id")) for u in own_usage)
        for a in artifacts:
            if a["worker_id"] == w["worker_id"]:
                item[a["kind"] + "_" + a["status"] + "_versions"] = a["n"]
        per_worker.append(item)

    per_job = []
    for j in jobs:
        own = [w for w in per_worker if w["job_id"] == j["job_id"]]
        item = {**j, "worker_attempts_created": len(own),
                "worker_attempts_with_usage": sum(w["usage_invocations"] > 0 for w in own)}
        for key in (*TOKENS, *TIMES, "usage_invocations", "timed_invocations"):
            values = [w[key] for w in own if w.get(key) is not None]
            item[key] = sum(values) if values else None
        per_job.append(item)

    by_role = []
    for role in ROLES:
        own = [u for u in usage if u["role"] == role]
        tasks = [w for w in per_worker if w["role"] == role]
        measured = summarize_timings(t for t in timings.values() if worker_index[t["worker_id"]]["role"] == role)
        row = {"role": role, **token_totals(own), "workers_created": len(tasks),
               "workers_with_usage": sum(w["usage_invocations"] > 0 for w in tasks),
               "workers_with_timing": sum(w["timed_invocations"] > 0 for w in tasks),
               "unique_jobs_with_usage": len({u["job_id"] for u in own if u["job_id"]}),
               "completed_or_discarded_workers": sum(w["state"] in {"completed", "discarded"} for w in tasks),
               "timed_invocations": measured["measured_turns"],
               **{key: measured[key] for key in TIMES}}
        for key in TOKENS:
            row["mean_" + key + "_per_worker_with_usage"] = (row[key] / row["workers_with_usage"]
                if row[key] is not None and row["workers_with_usage"] else None)
        row["mean_worker_elapsed_seconds_per_timed_worker"] = (row["worker_elapsed_seconds"] / row["workers_with_timing"]
            if row["workers_with_timing"] else None)
        observed_tasks = [w for w in tasks if w["usage_invocations"] > 0]
        row["mean_worker_elapsed_seconds_per_worker_with_usage"] = (sum(w["worker_elapsed_seconds"] or 0 for w in observed_tasks)
            / len(observed_tasks) if observed_tasks else None)
        by_role.append(row)

    timing = summarize_timings(timings.values())
    totals = token_totals(usage)
    assert all(u["input_tokens"] == u["cached_input_tokens"] + u["uncached_input_tokens"] for u in usage
               if u["input_tokens"] is not None and u["cached_input_tokens"] is not None)
    for key in TOKENS:
        assert sum(r[key] or 0 for r in by_role) == (totals[key] or 0)
    counts = {"discovered_jobs": len(jobs), "currently_evaluated_jobs": sum(j["agent_decision"] in {"approve", "discard"} for j in jobs),
              "confirmed_submitted_jobs": sum(j["agent_status"] == "submitted" for j in jobs),
              "ready_resume_versions": sum(a["n"] for a in artifacts if a["kind"] == "resume" and a["status"] == "ready"),
              "ready_cover_letter_versions": sum(a["n"] for a in artifacts if a["kind"] == "cover_letter" and a["status"] == "ready")}
    notes = [
        "Input includes cached input; uncached input is input minus cached. Reasoning tokens are a subset of output, not an extra charge.",
        "Tokens are actual stored per-invocation deltas, not summed cumulative snapshots. A turn can include many model requests and repeated context.",
        "These are pipeline worker counters only: supervisor/chat usage, older uninstrumented work and requests without emitted counters are excluded.",
        "Time is measured active worker execution including tools/startup, summed across invocations; concurrent workers overlap in wall time.",
        "Inbox, queue, quota pauses and time between invocations are excluded from active execution time.",
        "Model/transport is time outside measured tools/startup; it includes inference, network, provider queueing and orchestration. Pure inference is unavailable.",
        "Retries, profile refreshes, superseded attempts and unfinished tasks with recorded usage are included. Empty CSV values mean unavailable, not zero.",
        "Discovery works on many jobs; exact per-listing token attribution is unavailable. Per-job exports exclude shared discovery cost.",
        "Document workers may write both a resume and cover letter; exact per-file token attribution is unavailable.",
        "Subscription quota percentages/costs cannot be calculated from these raw token counts; no pricing weights are assumed.",
        "Other campaign totals cover recorded data only; their historical instrumentation coverage differs. No pipeline state was changed.",
    ]
    averages = []
    for r in by_role:
        count = counts["discovered_jobs"] if r["role"] == "discovery" else r["workers_with_usage"]
        averages.append({"role": r["role"], "denominator": count,
                         "unit": "discovered listing" if r["role"] == "discovery" else "worker attempt with token counters",
                         **{key: r[key] / count if count and r[key] is not None else None for key in TOKENS},
                         "worker_elapsed_seconds": (r["worker_elapsed_seconds"] / count if r["role"] == "discovery" and count
                             else r["mean_worker_elapsed_seconds_per_worker_with_usage"])})
    report = {"generated_at_utc": now.isoformat(), "pipeline": pipeline, "scope": "Entire saved campaign, including all recorded attempts",
              "counts": counts, "totals": totals, "timing": timing, "by_role": by_role,
              "averages": averages,
              "timing_invocations_without_counters": timing["measured_turns"] - sum(bool(u.get("timing_run_id")) for u in usage),
              "notes": notes}
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(output / "by-role.csv", by_role)
    write_csv(output / "averages.csv", averages)
    write_csv(output / "per-worker.csv", per_worker)
    write_csv(output / "per-invocation.csv", usage)
    write_csv(output / "per-job.csv", per_job)
    write_csv(output / "all-campaigns-recorded-tokens.csv", campaigns)
    timing_rows, call_rows = [], []
    for t in timings.values():
        w = worker_index[t["worker_id"]]
        head = {"worker_id": t["worker_id"], "run_id": t["run_id"], "role": w["role"], "requisition_id": w["requisition_id"]}
        timing_rows.append({**head, "started_at_utc": t["started_at_utc"], **{k: t[k] for k in TIMES}, "incomplete_calls": t["incomplete_calls"]})
        for call in t.get("calls", []):
            call_rows.append({**head, **call})
    write_csv(output / "per-run-timing.csv", timing_rows)
    write_csv(output / "per-tool-call.csv", call_rows)

    lines = [f"# {pipeline['company']} campaign usage", "", f"Generated {now.isoformat()}. Campaign remains {pipeline['status']}.", "",
             "| Stage | Cached input | Uncached input | Output | Measured worker time |", "|---|---:|---:|---:|---:|"]
    for r in by_role:
        lines.append(f"| {r['role']} | {r['cached_input_tokens']:,} | {r['uncached_input_tokens']:,} | {r['output_tokens']:,} | {r['worker_elapsed_seconds']/60:.2f} min |")
    lines += ["", "## Averages", "", "Discovery is divided by discovered listings. Other stages are divided by worker attempts with counters; resumed invocations are grouped into their worker. These include unfinished attempts.", "",
              "| Stage | Denominator | Total input / unit | Cached input / unit | Uncached input / unit | Output / unit | Active seconds / unit |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for r in averages:
        values = [f"{r[k]:,.1f}" if r[k] is not None else "unavailable" for k in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "worker_elapsed_seconds")]
        lines.append(f"| {r['role']} | {r['denominator']} | {' | '.join(values)} |")
    lines += ["", "## Measurement notes", "", *("- " + n for n in notes), "", "## Individual measured tasks", "",
              "| Stage | Requisition | State | Cached input | Uncached input | Output | Active minutes | Worker ID |",
              "|---|---|---|---:|---:|---:|---:|---|"]
    for r in sorted(per_worker, key=lambda r: r["input_tokens"] or 0, reverse=True):
        if not r["usage_invocations"] and not r["timed_invocations"]:
            continue
        values = [f"{r[k]:,}" if r[k] is not None else "unavailable" for k in ("cached_input_tokens", "uncached_input_tokens", "output_tokens")]
        minutes = f"{r['worker_elapsed_seconds']/60:.2f}" if r["worker_elapsed_seconds"] is not None else "unavailable"
        lines.append(f"| {r['role']} | {r['requisition_id'] or 'shared discovery'} | {r['state']} | {' | '.join(values)} | {minutes} | {r['worker_id']} |")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **report}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    pid = args.pipeline_id or json.loads((ROOT / "data/experiments/current.json").read_text())["pipeline_id"]
    folder = args.output or ROOT / "data/reports" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-usage")
    export_report(pid, folder)
