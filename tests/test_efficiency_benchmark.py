import hashlib
import json
import sqlite3

from tools.efficiency_benchmark import audit_database, completed_tool, size_stats, text_blocks


def test_completed_tool_normalizes_raw_and_wrapped_without_failed_partial_results():
    item = {"id": "same", "result": {"content": [{"type": "text", "text": "abc"},
                                                  {"type": "image", "data": "never count binary"}]}}
    for payload in ({"type": "item.completed", "item": item},
                    {"type": "tool_result", "success": True, "transport_event": {"item": item}}):
        assert text_blocks(completed_tool(payload)[1]) == ["abc"]
    assert completed_tool({"type": "item.started", "item": item}) is None
    assert completed_tool({"type": "tool_result", "success": False, "transport_event": {"item": item}}) is None
    item["result"]["isError"] = True
    assert completed_tool({"type": "item.completed", "item": item}) is None


def test_empty_measurement_is_unavailable_not_invented_latency_or_token_count():
    assert size_stats([]) == {"count": 0, "total_characters": 0,
                              "median_characters": None, "max_characters": None}


def test_historical_audit_deduplicates_transport_copies_but_not_resumed_item_ids(tmp_path):
    path = tmp_path / "history.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE agent_workers(id TEXT,role TEXT);
      CREATE TABLE agent_runtime_events(worker_id TEXT,session_id TEXT,tool_name TEXT,payload TEXT,created_at TEXT);
      CREATE TABLE source_documents(id TEXT,content TEXT,sha256 TEXT);
      CREATE TABLE job_versions(job_id TEXT,version INTEGER,sha256 TEXT);
      INSERT INTO agent_workers VALUES('w','evaluator');
    """)
    item = {"id": "item_0", "server": "agent_bridge", "tool": "get_context",
            "result": {"content": [{"type": "text", "text": '{"job":{"jd":"full description"}}'}]}}
    events = [
        {"type": "turn.started"},
        {"type": "item.completed", "item": item},
        {"type": "tool_result", "success": True, "transport_event": {"item": item}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 1}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": item},
        {"type": "turn.completed", "usage": {"input_tokens": 15, "output_tokens": 2}},
    ]
    for event in events:
        db.execute("INSERT INTO agent_runtime_events VALUES('w','same-thread','',?,'same-second')", (json.dumps(event),))
    db.commit()
    db.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = audit_database(path)
    assert report["context_payloads"]["count"] == 2
    assert report["usage_observations"]["monotone_multi_turn_sessions"] == 1
    assert report["usage_observations"]["totals"] is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
