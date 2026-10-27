from __future__ import annotations

import threading
import time
import uuid

from app.pipelines import AgentPipelineManager
from app.store import Store, timestamp


def _wait_for(predicate, *, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for the isolated agent manager transition.")


class _RuntimeBase:
    def __init__(self, store):
        self.store = store

    def action(self, task, name, payload):
        return self.store.agent_action(task["worker"]["id"], task["capability"], name, payload)

    @staticmethod
    def session_event(on_event, worker, session_id):
        on_event({"type": "thread.resumed" if worker.get("session_id") else "thread.started",
                  "thread_id": session_id})

    @staticmethod
    def tool_event(on_event, name, session_id):
        on_event({"type": "tool_result", "tool_name": "agent_bridge." + name,
                  "success": True, "session_id": session_id})

    def record_listing(self, task, on_event, index, title=None, session_id=None):
        company_id = self.store.get_pipeline(task["worker"]["pipeline_id"])["company_id"]
        result = self.action(task, "record_listing", {
            "requisition_id": f"fixture-{index}",
            "url": f"https://fixture.invalid/jobs/{company_id}/{index}",
            "title": title or f"Fixture Engineer {index}",
            "jd": "Synthetic job description for an isolated manager concurrency test.",
            "metadata": {"location": "Chicago, IL", "country": "US",
                         "employment": "full_time", "arrangement": "hybrid"},
            "selected": True,
            "selection_reason": "Selected by the synthetic test discovery agent.",
            "observation": {"test_only": True},
        })
        self.tool_event(on_event, "record_listing", session_id or task["worker"].get("session_id") or "fixture-discovery")
        return result


class _CapacityRuntime(_RuntimeBase):
    """Barrier runtime: Store and AgentPipelineManager stay real; external work is synthetic."""

    def __init__(self, store):
        super().__init__(store)
        self.discovery_release = threading.Event()
        self.evaluation_release = threading.Event()
        self.submission_release = threading.Event()
        self.lock = threading.Lock()
        self.started: dict[str, set[str]] = {"discovery": set(), "evaluator": set(), "submission": set()}
        self.active_discovery = 0
        self.max_active_discovery = 0

    def mark_started(self, role, worker_id):
        with self.lock:
            self.started[role].add(worker_id)

    def count_started(self, role):
        with self.lock:
            return len(self.started[role])

    def run(self, launch, on_event, should_stop):
        task = launch["task"]
        task["capability"] = launch["capability"]
        worker = task["worker"]
        role, worker_id = worker["role"], worker["id"]
        session_id = worker.get("session_id") or f"capacity-{role}-{worker_id}"
        self.session_event(on_event, worker, session_id)
        self.mark_started(role, worker_id)

        if role == "discovery":
            with self.lock:
                self.active_discovery += 1
                self.max_active_discovery = max(self.max_active_discovery, self.active_discovery)
            try:
                # The primary pipeline publishes enough selected rows to fill
                # its evaluator and submission caps while discovery remains live.
                if worker["pipeline_id"] == self.primary_pipeline_id:
                    for index in range(8):
                        self.record_listing(task, on_event, index, session_id=session_id)
                    assert self.discovery_release.wait(8)
                if should_stop():
                    return {"disposition": "interrupted", "session_id": session_id}
                self.action(task, "finish_discovery", {
                    "status": "complete", "detail": "Synthetic coverage boundary reached.",
                    "coverage": {"fixture_only": True},
                })
                self.tool_event(on_event, "finish_discovery", session_id)
                return {"disposition": "completed", "session_id": session_id,
                        "checkpoint": "fixture_discovery_complete"}
            finally:
                with self.lock:
                    self.active_discovery -= 1

        if role == "evaluator":
            assert self.evaluation_release.wait(8)
            if should_stop():
                return {"disposition": "interrupted", "session_id": session_id}
            self.action(task, "finish_evaluation", {
                "decision": "approve", "reason": "Synthetic fixture approval only.",
                "observations": {"test_only": True},
            })
            self.tool_event(on_event, "finish_evaluation", session_id)
            return {"disposition": "completed", "session_id": session_id, "checkpoint": "fixture_evaluation_complete"}

        if role == "submission":
            assert self.submission_release.wait(8)
            if should_stop():
                return {"disposition": "interrupted", "session_id": session_id}
            # No browser or submission occurs; return a bounded wait only after
            # the manager-cap assertions have observed the active slot count.
            return {"disposition": "waiting", "session_id": session_id,
                    "checkpoint": "fixture_submission_not_started"}

        raise AssertionError(f"Unexpected worker role: {role}")


def _insert_overlapping_pipeline(store, company_id):
    """Seed an otherwise API-forbidden overlap to exercise the manager's company claim."""
    pipeline_id, now = uuid.uuid4().hex, timestamp()
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO pipelines(id,company_id,status,stage,filters,discovery_status,discovery_payload,"
            "blockers,stop_requested,mode,agent_test_only,created_at,updated_at) "
            "VALUES(?,?, 'queued','discovery','{}','pending','{}','[]',0,'agentic',0,?,?)",
            (pipeline_id, company_id, now, now),
        )
        store._agent_insert_worker(db, pipeline_id, "discovery")
    return store.get_pipeline(pipeline_id)


def test_manager_enforces_company_discovery_and_three_eval_submission_slots(tmp_path):
    # This fixture deliberately holds eight submissions to isolate concurrency
    # from the separately tested default six-application backpressure buffer.
    (tmp_path / "config").mkdir()
    (tmp_path / "config/workers.json").write_text('{"efficiency":{"max_approved_backlog":100}}')
    store = Store(tmp_path)
    primary = store.create_agent_pipeline("Shared Fixture Company", "https://fixture.invalid/careers")
    overlapping = _insert_overlapping_pipeline(store, primary["company_id"])
    runtime = _CapacityRuntime(store)
    runtime.primary_pipeline_id = primary["id"]
    manager = AgentPipelineManager(store, runtime_factory=lambda _store: runtime)
    try:
        manager.start(primary["id"])
        manager.start(overlapping["id"])

        primary_discovery = next(worker for worker in store.list_agent_workers(primary["id"])
                                 if worker["role"] == "discovery")
        overlapping_discovery = next(worker for worker in store.list_agent_workers(overlapping["id"])
                                      if worker["role"] == "discovery")
        _wait_for(lambda: primary_discovery["id"] in runtime.started["discovery"])
        assert store.get_agent_worker(primary_discovery["id"])["state"] == "running"
        assert store.get_agent_worker(overlapping_discovery["id"])["state"] != "running"
        assert runtime.max_active_discovery == 1

        _wait_for(lambda: runtime.count_started("evaluator") == 3)
        _wait_for(lambda: store.get_pipeline(primary["id"])["counts"]["discovered"] == 8)
        primary_workers = store.list_agent_workers(primary["id"])
        evaluators = [worker for worker in primary_workers if worker["role"] == "evaluator"]
        assert len(evaluators) == 8
        assert sum(worker["state"] == "running" for worker in evaluators) == 3
        assert sum(worker["state"] == "queued" for worker in evaluators) == 5
        assert runtime.max_active_discovery == 1

        runtime.evaluation_release.set()
        _wait_for(lambda: runtime.count_started("submission") == 3)
        _wait_for(lambda: sum(worker["role"] == "evaluator" and worker["state"] == "completed"
                              for worker in store.list_agent_workers(primary["id"])) == 8)
        _wait_for(lambda: sum(worker["role"] == "submission"
                              for worker in store.list_agent_workers(primary["id"])) == 8)
        primary_workers = store.list_agent_workers(primary["id"])
        submissions = [worker for worker in primary_workers if worker["role"] == "submission"]
        assert len(submissions) == 8
        assert sum(worker["state"] == "running" for worker in submissions) == 3
        assert sum(worker["state"] == "queued" for worker in submissions) == 5
        assert runtime.max_active_discovery == 1
        assert store.get_agent_worker(overlapping_discovery["id"])["state"] != "running"
    finally:
        runtime.discovery_release.set()
        runtime.evaluation_release.set()
        runtime.submission_release.set()
        manager.shutdown()


class _QuestionRuntime(_RuntimeBase):
    def __init__(self, store):
        super().__init__(store)
        self.question_started = threading.Event()
        self.other_job_checkpoint = threading.Event()
        self.resumed_session = threading.Event()
        self.saved_session = None
        self.resumed_session_id = None
        self.turns: dict[str, int] = {}
        self.lock = threading.Lock()

    def run(self, launch, on_event, should_stop):
        task = launch["task"]
        task["capability"] = launch["capability"]
        worker = task["worker"]
        role, worker_id = worker["role"], worker["id"]
        session_id = worker.get("session_id") or f"job-session-{worker.get('job_id') or worker_id}"
        self.session_event(on_event, worker, session_id)

        if role == "discovery":
            self.record_listing(task, on_event, 1, "Questionable Fixture Engineer", session_id)
            self.record_listing(task, on_event, 2, "Independent Fixture Engineer", session_id)
            self.action(task, "finish_discovery", {
                "status": "complete", "detail": "Both synthetic listings were discovered.",
                "coverage": {"fixture_only": True},
            })
            self.tool_event(on_event, "finish_discovery", session_id)
            return {"disposition": "completed", "session_id": session_id}

        if role == "evaluator":
            job = self.store.get_job(worker["job_id"])
            with self.lock:
                turn = self.turns.get(worker_id, 0) + 1
                self.turns[worker_id] = turn
            if job["title"] == "Questionable Fixture Engineer" and turn == 1:
                self.saved_session = session_id
                self.action(task, "ask_question", {
                    "question": "Synthetic question: is the optional fixture fact confirmed?",
                    "dedupe_key": "fixture-question",
                    "context": {"test_only": True},
                    "response_hint": "A synthetic yes/no detail.",
                })
                self.tool_event(on_event, "ask_question", session_id)
                self.question_started.set()
                return {"disposition": "waiting", "session_id": session_id,
                        "checkpoint": "fixture_question"}
            if job["title"] == "Questionable Fixture Engineer":
                self.resumed_session_id = session_id
                self.resumed_session.set()
            self.action(task, "finish_evaluation", {
                "decision": "approve" if job["title"] == "Independent Fixture Engineer" else "discard",
                "reason": "Synthetic fixture evaluation completed.",
                "observations": {"test_only": True},
            })
            self.tool_event(on_event, "finish_evaluation", session_id)
            return {"disposition": "completed", "session_id": session_id}

        if role == "submission":
            self.action(task, "request_approval", {
                "action": "first_application_review",
                "summary": "Synthetic job-local review checkpoint; no application action is possible.",
                "observation": {"test_only": True},
            })
            self.tool_event(on_event, "request_approval", session_id)
            self.other_job_checkpoint.set()
            return {"disposition": "waiting", "session_id": session_id,
                    "checkpoint": "fixture_other_job_review"}

        raise AssertionError(f"Unexpected worker role: {role}")


def test_question_wait_is_job_local_and_answer_resumes_same_provider_session(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Question Company", "https://fixture.invalid/careers")
    runtime = _QuestionRuntime(store)
    manager = AgentPipelineManager(store, runtime_factory=lambda _store: runtime)
    try:
        manager.start(pipeline["id"])
        assert runtime.question_started.wait(5)
        assert runtime.other_job_checkpoint.wait(5), (
            [(worker["role"], worker["state"], worker["job_id"], worker["error"])
             for worker in store.list_agent_workers(pipeline["id"])],
            [(item["kind"], item["state"], item["job_id"], item.get("action"))
             for item in store.agent_inbox(pipeline["id"])]
        )

        view = manager.pipeline_view(pipeline["id"])
        questionable = next(job for job in store.pipeline_jobs(pipeline["id"])["jobs"]
                            if job["title"] == "Questionable Fixture Engineer")
        independent = next(job for job in store.pipeline_jobs(pipeline["id"])["jobs"]
                           if job["title"] == "Independent Fixture Engineer")
        workers = store.list_agent_workers(pipeline["id"])
        question_worker = next(worker for worker in workers
                               if worker["role"] == "evaluator" and worker["job_id"] == questionable["id"])
        other_submission = next(worker for worker in workers
                                if worker["role"] == "submission" and worker["job_id"] == independent["id"])
        assert question_worker["state"] == "waiting_question"
        assert other_submission["state"] == "waiting_approval"
        assert view["pipeline"]["status"] in {"running", "awaiting_user"}
        assert any(item["job_id"] == questionable["id"] and item["kind"] == "question"
                   for item in store.agent_inbox(pipeline["id"]))

        question = next(item for item in store.agent_inbox(pipeline["id"])
                        if item["job_id"] == questionable["id"] and item["kind"] == "question")
        store.answer_agent_inbox(question["id"],
                                 "Synthetic fixture answer supplied for this isolated test.",
                                 "Explicit synthetic test provenance.")
        manager.schedule()
        assert runtime.resumed_session.wait(5)
        assert runtime.resumed_session_id == runtime.saved_session
        _wait_for(lambda: store.get_agent_worker(question_worker["id"])["state"] == "discarded")
        # The unrelated approval remains pending and independent of the answered question.
        review = next(item for item in store.agent_inbox(pipeline["id"])
                      if item["kind"] == "first_application_review")
        assert review["state"] == "pending"
    finally:
        manager.stop(pipeline["id"])
        manager.shutdown()


def test_agent_question_answer_keeps_neutral_authorship_and_exact_provenance(tmp_path):
    store = Store(tmp_path)
    pipeline = store.create_agent_pipeline("Fixture Observation Provenance", "https://fixture.invalid/careers")
    discovery = store.list_agent_workers(pipeline["id"])[0]
    assert store.reserve_agent_worker(discovery["id"])
    discovery_launch = store.agent_launch_context(discovery["id"])
    listing = store.agent_action(discovery["id"], discovery_launch["capability"], "record_listing", {
        "requisition_id": "fixture-provenance-1",
        "url": "https://fixture.invalid/jobs/fixture-provenance-1",
        "title": "Fixture Observation Provenance Engineer",
        "jd": "Synthetic complete listing for the isolated provenance test.",
        "metadata": {"location": "Seattle, WA", "country": "US", "employment": "full_time",
                     "arrangement": "hybrid"},
        "selected": True,
        "selection_reason": "Selected by the isolated fixture.",
    })
    store.agent_action(discovery["id"], discovery_launch["capability"], "finish_discovery", {
        "status": "complete", "detail": "Synthetic discovery complete.", "coverage": {"test_only": True},
    })

    evaluator = next(worker for worker in store.list_agent_workers(pipeline["id"])
                     if worker["role"] == "evaluator" and worker["job_id"] == listing["job_id"])
    assert store.reserve_agent_worker(evaluator["id"])
    launch = store.agent_launch_context(evaluator["id"])
    question = store.agent_action(evaluator["id"], launch["capability"], "ask_question", {
        "question": "What source was observed for this applicant's referral?",
        "fact_key": "fixture_referral_observation", "fact_scope": "applicant",
        "context": {"scope": "fictional applicant fact for provenance test"},
    })
    response = "Other / company careers page"
    provenance = "Dashboard operator relayed the discovery agent's observation; not a direct applicant statement."
    store.answer_agent_inbox(question["inbox_item_id"], response, provenance)

    with store.connect() as db:
        context = store._agent_profile_context(db)
        answer = next(item for item in context["answered_questions"]
                      if item["id"] == question["inbox_item_id"])
        worker = db.execute("SELECT * FROM agent_workers WHERE id=?", (evaluator["id"],)).fetchone()
        exact = store._agent_retrieve_sources(db, worker, {"source_ids": [answer["ref"]]})["sources"][0]["record"]
        event = db.execute("SELECT actor,payload FROM events WHERE kind='agent.inbox_answered' "
                           "AND job_id=? ORDER BY id DESC LIMIT 1", (listing["job_id"],)).fetchone()

    assert answer["source"] == "answered_agent_question"
    assert store._agent_decode(exact["response"], exact["response"]) == response
    assert exact["provenance"] == provenance
    assert exact["job_id"] == listing["job_id"]
    assert event["actor"] == "dashboard_operator"
    assert provenance in event["payload"]


class _QuotaRuntime(_RuntimeBase):
    def __init__(self, store):
        super().__init__(store)
        self.quota_seen = threading.Event()
        self.resume_seen = threading.Event()
        self.saved_session = None
        self.resume_session = None
        self.resume_checkpoint = None
        self.evaluator_turns = {}

    def run(self, launch, on_event, should_stop):
        task = launch["task"]
        task["capability"] = launch["capability"]
        worker = task["worker"]
        role, worker_id = worker["role"], worker["id"]
        session_id = worker.get("session_id") or f"quota-session-{worker_id}"
        self.session_event(on_event, worker, session_id)
        if role == "discovery":
            self.record_listing(task, on_event, 1, "Quota Fixture Engineer", session_id)
            self.action(task, "finish_discovery", {
                "status": "complete", "detail": "Synthetic discovery complete.",
                "coverage": {"fixture_only": True},
            })
            self.tool_event(on_event, "finish_discovery", session_id)
            return {"disposition": "completed", "session_id": session_id}
        if role != "evaluator":
            raise AssertionError(f"Unexpected worker role: {role}")

        turn = self.evaluator_turns.get(worker_id, 0) + 1
        self.evaluator_turns[worker_id] = turn
        if turn == 1:
            self.saved_session = session_id
            self.action(task, "checkpoint", {"checkpoint": "fixture_before_provider_limit",
                                               "context": {"test_only_checkpoint": True}})
            on_event({"type": "quota_error", "message": "Synthetic provider usage limit.",
                      "checkpoint": "fixture_provider_limit"})
            self.quota_seen.set()
            return {"disposition": "quota_paused", "provider_error": "Synthetic provider usage limit.",
                    "session_id": session_id, "checkpoint": "fixture_provider_limit"}

        self.resume_session = session_id
        self.resume_checkpoint = worker.get("checkpoint")
        self.resume_seen.set()
        self.action(task, "finish_evaluation", {
            "decision": "discard", "reason": "Synthetic evaluator resumed after quota reset.",
            "observations": {"test_only": True},
        })
        self.tool_event(on_event, "finish_evaluation", session_id)
        return {"disposition": "completed", "session_id": session_id}


def test_quota_resume_preserves_session_and_checkpoint_without_restarting_stopped_pipeline(tmp_path):
    store = Store(tmp_path)
    active = store.create_agent_pipeline("Quota Fixture Active", "https://quota.fixture.invalid/careers")
    stopped = store.create_agent_pipeline("Quota Fixture Stopped", "https://stopped.fixture.invalid/careers")
    runtime = _QuotaRuntime(store)
    manager = AgentPipelineManager(store, runtime_factory=lambda _store: runtime)
    try:
        manager.stop(stopped["id"])
        assert store.get_pipeline(stopped["id"])["status"] == "stopped"
        manager.start(active["id"])
        assert runtime.quota_seen.wait(5)
        _wait_for(lambda: store.get_agent_worker(
            next(worker["id"] for worker in store.list_agent_workers(active["id"])
                 if worker["role"] == "evaluator"))["state"] == "quota_paused")

        evaluator = next(worker for worker in store.list_agent_workers(active["id"])
                         if worker["role"] == "evaluator")
        assert evaluator["session_id"] == runtime.saved_session
        assert evaluator["checkpoint"] == "fixture_provider_limit"
        assert store.get_agent_quota()["paused"] is True
        # The quota latch is persisted just before the runtime future returns.
        # Wait for that turn to release its executor slot before manually
        # resuming, so this test verifies the resumed checkpoint, not a timing
        # race between the test thread and Future's done callback.
        def runtime_turn_released():
            future = manager._futures.get(evaluator["id"])
            return future is None or future.done()

        _wait_for(runtime_turn_released)

        resumed = manager.resume_quota()
        assert resumed["quota"]["paused"] is False
        assert runtime.resume_seen.wait(5)
        assert runtime.resume_session == runtime.saved_session
        assert runtime.resume_checkpoint == "fixture_provider_limit"
        _wait_for(lambda: store.get_agent_worker(evaluator["id"])["state"] == "discarded")
        _wait_for(lambda: store.get_pipeline(active["id"])["status"] == "completed")
        assert store.get_pipeline(stopped["id"])["status"] == "stopped"
        assert store.get_pipeline(stopped["id"])["stop_requested"] is True
        assert all(worker["state"] == "stopped" for worker in store.list_agent_workers(stopped["id"]))
    finally:
        manager.shutdown()
