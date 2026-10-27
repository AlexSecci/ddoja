"""Offline regressions for policy delivery and audited question withdrawal."""
from pathlib import Path
import json

import pytest

from app.agent_runtime import CodexAgentRuntime
from app.store import Conflict, Store


def _case(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fictional Policy", "https://policy.invalid/jobs")
    discovery = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(discovery["id"])
    launch = store.agent_launch_context(discovery["id"])
    for index in range(2):
        store.agent_action(discovery["id"], launch["capability"], "record_listing", {
            "requisition_id": str(index), "url": f"https://policy.invalid/{index}",
            "title": "C++ Systems Developer", "jd": "C++ systems role; networking depth desired.",
            "selected": True, "metadata": {"country": "US", "employment": "full_time", "arrangement": "hybrid"}})
    workers = [worker for worker in store.list_agent_workers(pipeline["id"]) if worker["role"] == "evaluator"]
    return store, pipeline, workers


def _question(store, worker, *, shared=False):
    assert store.reserve_agent_worker(worker["id"])
    launch = store.agent_launch_context(worker["id"])
    store.agent_runtime_event(worker["id"], {"type": "thread.started", "thread_id": "saved-" + worker["id"]})
    payload = {"question": "Legacy JD skills survey?"}
    if shared:
        payload.update(fact_key="networking.depth", fact_scope="applicant")
    result = store.agent_action(worker["id"], launch["capability"], "ask_question", payload)
    store.finish_agent_turn(worker["id"], {"disposition": "waiting"})
    return result["inbox_item_id"]


def test_withdrawal_keeps_history_without_fabricated_answers_or_starting_workers(tmp_path):
    store, pipeline, workers = _case(tmp_path)
    first = _question(store, workers[0], shared=True)
    second = _question(store, workers[1], shared=True)
    assert first == second
    result = store.reconsider_evaluator_questions([first], "User wants stretch decisions from saved evidence.")
    assert result["withdrawn"] == 1 and result["workers_started"] == 0
    assert set(result["affected_workers"]) == {worker["id"] for worker in workers}
    item = next(item for item in store.agent_inbox(pipeline["id"]) if item["id"] == first)
    assert item["state"] == "stale" and item["response"] == ""
    assert item["context"]["withdrawal"]["applicant_answer_supplied"] is False
    for worker in workers:
        current = store.get_agent_worker(worker["id"])
        assert current["state"] == "interrupted" and current["turn_active"] == 0
        assert current["session_id"] == "saved-" + worker["id"]
    with store.connect() as db:
        assert db.execute("SELECT state FROM agent_fact_questions").fetchone()[0] == "cancelled"
        assert not store._agent_profile_context(db)["answered_questions"]


def test_withdrawal_is_atomic_and_cannot_retire_an_approval_or_live_worker(tmp_path):
    store, pipeline, workers = _case(tmp_path)
    question = _question(store, workers[0])
    with store.connect(write=True) as db:
        worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (workers[1]["id"],)).fetchone()
        approval, _ = store._agent_insert_inbox(db, worker, "approval", action="upload", summary="Exact upload checkpoint")
    with pytest.raises(Conflict):
        store.reconsider_evaluator_questions([question, approval["id"]], "Policy changed")
    assert all(item["state"] == "pending" for item in store.agent_inbox(pipeline["id"]))
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET turn_active=1 WHERE id=?", (workers[0]["id"],))
    with pytest.raises(Conflict):
        store.reconsider_evaluator_questions([question], "Policy changed")


def test_new_policy_is_delivered_to_the_same_saved_session_after_withdrawal(tmp_path):
    store, pipeline, workers = _case(tmp_path)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "agent_core.md").write_text("Original core", encoding="utf-8")
    (prompts / "evaluation_agent.md").write_text("Original evaluator", encoding="utf-8")
    item_id = _question(store, workers[0])
    root = Path(__file__).resolve().parents[1]
    for name in ("agent_core.md", "evaluation_agent.md"):
        (prompts / name).write_text((root / "prompts" / name).read_text(encoding="utf-8"), encoding="utf-8")
    store.reconsider_evaluator_questions([item_id], "Current user hands-free policy")
    store.resume_agent_pipeline(pipeline["id"])
    assert store.reserve_agent_worker(workers[0]["id"])
    launch = store.agent_launch_context(workers[0]["id"])
    assert launch["task"]["task_context"]["instructions_changed"] is True
    assert launch["task"]["worker"]["session_id"] == "saved-" + workers[0]["id"]
    runtime = object.__new__(CodexAgentRuntime)
    runtime.root = tmp_path
    prompt = runtime._prompt(launch["task"], launch["task"]["worker"])
    assert "Hands-free application policy" in prompt
    assert "Do not ask technology checklists" in prompt
    assert "has no applicant answer" in prompt
