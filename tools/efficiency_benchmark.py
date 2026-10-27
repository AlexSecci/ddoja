"""Read-only historical payload audit; no inference or employer/browser access.

Character counts measure transport volume, not tokens or subscription credits.
Use immutable saved inputs for comparisons, never infer semantic quality from size.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def completed_tool(payload):
    """Normalize old CLI raw events and host-normalized tool results once."""
    transport = payload.get("transport_event", payload)
    item = transport.get("item") if isinstance(transport, dict) else None
    if not isinstance(item, dict):
        return None
    if payload.get("type") not in {"item.completed", "tool_result"}:
        return None
    result = item.get("result")
    if not isinstance(result, dict) or item.get("error") or result.get("isError"):
        return None
    if item.get("status") in {"failed", "in_progress"} or payload.get("success") is False:
        return None
    return item, result


def text_blocks(result):
    return [block["text"] for block in result.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)]


def size_stats(values):
    return {"count": len(values), "total_characters": sum(values),
            "median_characters": median(values) if values else None,
            "max_characters": max(values) if values else None}


def audit_database(path):
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    context_sizes, browser_sizes, calls = [], [], Counter()
    latest_context, seen, turn_ordinals = {}, set(), Counter()
    usage_sessions = defaultdict(list)
    try:
        rows = db.execute("SELECT e.*,w.role FROM agent_runtime_events e "
                          "JOIN agent_workers w ON w.id=e.worker_id ORDER BY e.created_at,e.rowid")
        for row in rows:
            payload = json.loads(row["payload"])
            session_key = (row["worker_id"], row["session_id"])
            if payload.get("type") == "turn.started":
                turn_ordinals[session_key] += 1
            completed = completed_tool(payload)
            if completed:
                item, result = completed
                name = row["tool_name"] or ".".join(filter(None, (item.get("server"), item.get("tool"))))
                # The same transport result can be saved both raw and normalized.
                # Exec restarts item_0 at each resumed turn; IDs are not session-global.
                identity = (*session_key, turn_ordinals[session_key], item.get("id"), name)
                if item.get("id") and identity in seen:
                    continue
                seen.add(identity)
                texts = text_blocks(result)
                count = sum(map(len, texts))
                calls[name] += 1
                if name == "agent_bridge.get_context":
                    context_sizes.append(count)
                    for text in texts:
                        try:
                            latest_context[row["role"]] = json.loads(text)
                        except (ValueError, TypeError):
                            pass
                if name == "cua_repl.js":
                    browser_sizes.append(count)
            if payload.get("type") == "turn.completed" and isinstance(payload.get("usage"), dict):
                usage_sessions[(row["worker_id"], row["session_id"])].append(payload["usage"])
        sources = [{"id": row["id"], "characters": len(row["content"]), "sha256": row["sha256"]}
                   for row in db.execute("SELECT id,content,sha256 FROM source_documents ORDER BY id")]
        input_digest = hashlib.sha256()
        job_count = 0
        for row in db.execute("SELECT job_id,version,sha256 FROM job_versions ORDER BY job_id,version"):
            input_digest.update(json.dumps(list(row), separators=(",", ":")).encode())
            job_count += 1
        multiple, monotone = 0, 0
        for snapshots in usage_sessions.values():
            if len(snapshots) > 1:
                multiple += 1
                if all(all(a.get(k, 0) <= b.get(k, 0) for k in ("input_tokens", "output_tokens"))
                       for a, b in zip(snapshots, snapshots[1:])):
                    monotone += 1
        return {
            "method": "read_only_historical_audit_no_inference",
            "database": str(path),
            "limitations": ["Character counts are not tokens or subscription allowance.",
                            "Historical runs include setup, debugging and recovery, not a clean latency benchmark.",
                            "Usage snapshots are not summed without verified counter semantics."],
            "context_payloads": size_stats(context_sizes), "browser_payloads": size_stats(browser_sizes),
            "completed_tool_calls": dict(sorted(calls.items())), "sources": sources,
            "latest_context_sections": {role: {key: len(json.dumps(value, ensure_ascii=False))
                                               for key, value in context.items()}
                                        for role, context in latest_context.items()},
            "saved_jd_versions": job_count, "saved_jd_manifest_sha256": input_digest.hexdigest(),
            "usage_observations": {"sessions": len(usage_sessions), "multi_turn_sessions": multiple,
                                   "monotone_multi_turn_sessions": monotone, "totals": None},
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/pipeline.sqlite")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_database(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.output.resolve()),
                      "contexts": report["context_payloads"], "browser": report["browser_payloads"]}))


if __name__ == "__main__":
    main()
