from __future__ import annotations

from pathlib import Path
import hashlib
import json
import pytest

from fastapi.testclient import TestClient

from app.pipelines import AgentPipelineManager
from app.server import create_app
from app.store import Store, timestamp


class IsolatedAgentManager:
    """Route harness only: creates no CLI, model, browser, or submission work."""

    limits = {
        "discovery_per_company": 1,
        "evaluators_per_pipeline": 3,
        "submissions_per_pipeline": 3,
        "documents_per_parent": 1,
    }

    def __init__(self, store):
        self.store = store
        self.schedule_calls = 0

    def recover_interrupted(self):
        return None

    def shutdown(self):
        return None

    def view(self, pipeline_id):
        pipeline = self.store.get_pipeline(pipeline_id)
        workers = self.store.list_agent_workers(pipeline_id) if pipeline["mode"] == "agentic" else []
        return {"pipeline": pipeline, "workers": workers, "counts": pipeline["counts"],
                "limits": self.limits if pipeline["mode"] == "agentic" else {},
                "quota": self.store.get_agent_quota()}

    def pipeline_view(self, pipeline_id):
        return self.view(pipeline_id)

    def list_views(self):
        return [self.view(pipeline["id"]) for pipeline in self.store.list_pipelines()
                if pipeline.get("mode") == "agentic"]

    def start(self, pipeline_id):
        return self.view(pipeline_id)

    def stop(self, pipeline_id):
        self.store.request_pipeline_stop(pipeline_id)
        return self.view(pipeline_id)

    def resume(self, pipeline_id):
        return self.view(pipeline_id)

    def schedule(self):
        self.schedule_calls += 1

    def resume_quota(self):
        quota = self.store.set_agent_quota(False)
        return {"quota": quota, "workers": []}

    def resume_replacement(self, pipeline_id, worker_id, provenance, replacement_reason):
        raise AssertionError("Replacement route is not exercised by the no-op test manager.")


def _client(tmp_path, *, allow_http_loopback_test=False):
    manager = {}

    def factory(store):
        manager["instance"] = IsolatedAgentManager(store)
        return manager["instance"]

    app = create_app(tmp_path, agent_manager_factory=factory,
                     allow_http_loopback_test=allow_http_loopback_test)
    client = TestClient(app)
    token = client.get("/api/session").json()["token"]
    return app, client, {"X-Pipeline-Token": token}, manager


def _create_pipeline(client, headers, *, company="Example Robotics", url="https://careers.example.org/jobs"):
    response = client.post("/api/pipelines", headers=headers, json={
        "company_name": company,
        "careers_url": url,
        "title_contains": "Systems Engineer",
        "location_contains": "Seattle",
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_efficiency_and_runtime_routes_require_the_local_session(tmp_path):
    app, client, headers, _ = _client(tmp_path)
    pipeline = app.state.store.create_agent_pipeline("Synthetic metrics", "https://metrics.invalid/jobs")
    for path in ("/api/efficiency", f"/api/pipelines/{pipeline['id']}/runtime-status"):
        assert client.get(path).status_code == 403
        assert client.get(path, headers=headers).status_code == 200
    report = client.get("/api/efficiency", headers=headers).json()
    assert report["totals"]["input_tokens"] is None


def test_local_theme_assets_load_without_expanding_static_file_scope(tmp_path):
    _, client, _, _ = _client(tmp_path)
    for asset in ("theme.css", "space-grotesk.ttf", "dm-sans.ttf", "orbit-mark.svg"):
        response = client.get("/static/" + asset)
        assert response.status_code == 200, asset
        assert len(response.content) > 100
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/space-grotesk.ttf").headers["content-type"].startswith("font/")
    assert client.get("/static/schema.sql").status_code == 404
    assert client.get("/static/config.json").status_code == 404


def _seed_listing(store, company_id, *, title="Test Engineer"):
    return store.discover(company_id, {
        "url": "https://jobs.example.org/req/fixture-1",
        "title": title,
        "jd": "Fictional local UI contract listing. No employer contact is made.",
        "location": "Seattle, WA",
        "country": "US",
        "employment": "full_time",
        "arrangement": "hybrid",
    })


def _seed_inbox(store, pipeline_id, job_id, kind, **fields):
    with store.connect(write=True) as db:
        worker = store._agent_insert_worker(db, pipeline_id, "evaluator", job_id=job_id)
        row = db.execute("SELECT * FROM agent_workers WHERE id=?", (worker,)).fetchone()
        item, _ = store._agent_insert_inbox(db, row, kind, **fields)
        return item["id"], worker


def test_agentic_release_and_arbitrary_https_company_start_are_visible_without_feed_setup(tmp_path):
    _, client, headers, manager = _client(tmp_path)
    with client:
        health = client.get("/api/health").json()
        assert health["pipeline_release"] == "agentic_pipeline_v1"
        assert health["agent_limits"] == IsolatedAgentManager.limits

        view = _create_pipeline(client, headers, company="Example Robotics",
                                url="https://careers.example.org/open-roles")
        pipeline = view["pipeline"]
        assert pipeline["mode"] == "agentic"
        assert pipeline["company_name"] == "Example Robotics"
        assert pipeline["careers_url"] == "https://careers.example.org/open-roles"
        assert {key: value for key, value in pipeline["filters"].items()
                if key != "career_scope"} == {
            "title_contains": "Systems Engineer", "location_contains": "Seattle",
            "country": "US", "employment": "full_time", "arrangement": ["onsite", "hybrid"],
        }
        assert pipeline["filters"]["career_scope"] == Store._agent_career_scope()
        assert pipeline["status"] == "queued"
        assert len(view["workers"]) == 1
        assert view["workers"][0]["role"] == "discovery"
        assert manager["instance"].schedule_calls == 0

        listed = client.get("/api/pipelines", headers=headers).json()
        assert any(row["pipeline"]["id"] == pipeline["id"] for row in listed)
        workers = client.get(f"/api/pipelines/{pipeline['id']}/workers", headers=headers).json()
        assert workers["limits"] == IsolatedAgentManager.limits
        assert workers["workers"][0]["session_id"] is None
        assert client.get(f"/api/pipelines/{pipeline['id']}/inbox", headers=headers).json()["items"] == []

        not_https = client.post("/api/pipelines", headers=headers, json={
            "company_name": "Bad URL Fixture", "careers_url": "http://careers.example.org/jobs",
        })
        assert not_https.status_code in {400, 422}


def test_loopback_http_careers_site_is_only_enabled_by_isolated_app_factory_opt_in(tmp_path):
    _, production_client, production_headers, _ = _client(tmp_path / "production")
    with production_client:
        production_health = production_client.get("/api/health").json()
        assert production_health["fixture_test_mode"] == {"allow_loopback_http_careers_url": False}
        denied = production_client.post("/api/pipelines", headers=production_headers, json={
            "company_name": "Fictional Local Board",
            "careers_url": "http://127.0.0.1:8765/__fictional/board",
        })
        assert denied.status_code in {400, 422}
        request_flag = production_client.post("/api/pipelines", headers=production_headers, json={
            "company_name": "Fictional Local Board",
            "careers_url": "http://127.0.0.1:8765/__fictional/board",
            "allow_http_loopback_test": True,
            "test_only": True,
        })
        assert request_flag.status_code == 422

    _, fixture_client, fixture_headers, _ = _client(
        tmp_path / "fixture", allow_http_loopback_test=True)
    with fixture_client:
        fixture_health = fixture_client.get("/api/health").json()
        assert fixture_health["fixture_test_mode"] == {"allow_loopback_http_careers_url": True}
        allowed = fixture_client.post("/api/pipelines", headers=fixture_headers, json={
            "company_name": "Fictional Local Board",
            "careers_url": "http://127.0.0.1:8765/__fictional/board",
        })
        assert allowed.status_code == 200, allowed.text
        pipeline = allowed.json()["pipeline"]
        assert pipeline["agent_test_only"] == 1
        assert pipeline["careers_url"] == "http://127.0.0.1:8765/__fictional/board"


def test_ui_allows_only_https_or_the_exact_fixture_board_url(tmp_path):
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    helper = js.split("function isAllowedCareersUrl(url) {", 1)[1].split("\n}", 1)[0]
    assert "url.protocol === 'https:'" in helper
    assert "serviceHealth?.fixture_test_mode?.allow_loopback_http_careers_url === true" in helper
    assert "url.href === 'http://127.0.0.1:8765/__fictional/board'" in helper
    assert "url.protocol === 'http:'" not in helper


def test_scoped_question_and_action_decision_routes_store_exact_user_choice(tmp_path):
    _, client, headers, manager = _client(tmp_path)
    store = Store(tmp_path)
    with client:
        view = _create_pipeline(client, headers)
        pipeline_id = view["pipeline"]["id"]
        company_id = view["pipeline"]["company_id"]
        seeded_job = _seed_listing(store, company_id)
        job_id = seeded_job["id"]
        with store.connect(write=True) as db:
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,agent_status) "
                       "VALUES(?,?,?,1,'selected')", (pipeline_id, job_id, timestamp()))
        question_id, question_worker = _seed_inbox(
            store, pipeline_id, job_id, "question", question="Which work location do you prefer?",
            response_hint="Describe the location that works for you.", context={"reason": "Missing preference."},
        )
        question_response = client.post(
            f"/api/pipelines/{pipeline_id}/inbox/{question_id}/answer", headers=headers,
            json={"response": "Seattle area", "provenance": "User provided this location preference."},
        )
        assert question_response.status_code == 200, question_response.text
        assert question_response.json()["item"]["state"] == "answered"
        assert question_response.json()["item"]["provenance"] == "User provided this location preference."
        assert store.get_agent_worker(question_worker)["state"] == "queued"

        approval_id, approval_worker = _seed_inbox(
            store, pipeline_id, job_id, "first_application_review",
            action="first_application_review", summary="Review the first application before final send.",
            context={"observations": ["Synthetic UI test only."],
                     "jd_version": store.get_job(job_id)["jd_version"],
                     "profile_version": store.profile_version()},
        )
        denied = client.post(
            f"/api/pipelines/{pipeline_id}/inbox/{approval_id}/decision", headers=headers,
            json={"decision": "maybe", "provenance": "Synthetic choice."},
        )
        assert denied.status_code == 422
        decided = client.post(
            f"/api/pipelines/{pipeline_id}/inbox/{approval_id}/decision", headers=headers,
            json={"decision": "reject", "provenance": "User declined the synthetic first-review fixture."},
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["item"]["state"] == "rejected"
        assert store.get_agent_worker(approval_worker)["state"] == "queued"
        assert manager["instance"].schedule_calls == 2


@pytest.mark.parametrize("note", [None, "", "   ", "Applicant supplied this extra context."])
def test_inbox_answer_needs_no_separate_source_but_retains_an_optional_note(tmp_path, note):
    _, client, headers, _ = _client(tmp_path)
    store = Store(tmp_path)
    with client:
        view = _create_pipeline(client, headers)
        pipeline_id = view["pipeline"]["id"]
        job = _seed_listing(store, view["pipeline"]["company_id"])
        question_id, worker_id = _seed_inbox(store, pipeline_id, job["id"], "question", question="A required factual field?")
        route = f"/api/pipelines/{pipeline_id}/inbox/{question_id}/answer"
        body = {"response": "The applicant's exact answer"}
        if note is not None:
            body["provenance"] = note
        assert client.post(route, json=body).status_code == 403
        assert client.post(route, headers=headers, json={"response": "   "}).status_code == 422
        answered = client.post(route, headers=headers, json=body)
        assert answered.status_code == 200, answered.text
        item = answered.json()["item"]
        assert json.loads(item["response"]) == "The applicant's exact answer"
        assert item["provenance"] == (note.strip() if note and note.strip() else "Direct answer from the applicant in the dashboard.")
        assert store.get_agent_worker(worker_id)["state"] == "queued"


def test_approval_note_is_optional_but_an_explicit_scoped_decision_is_still_required(tmp_path):
    _, client, headers, _ = _client(tmp_path)
    store = Store(tmp_path)
    with client:
        view = _create_pipeline(client, headers)
        pipeline_id = view["pipeline"]["id"]
        job = _seed_listing(store, view["pipeline"]["company_id"])
        with store.connect(write=True) as db:
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,agent_status) "
                       "VALUES(?,?,?,1,'selected')", (pipeline_id, job["id"], timestamp()))
        item_id, _ = _seed_inbox(store, pipeline_id, job["id"], "first_application_review", action="first_application_review",
                                context={"jd_version": store.get_job(job["id"])["jd_version"],
                                         "profile_version": store.profile_version()})
        route = f"/api/pipelines/{pipeline_id}/inbox/{item_id}/decision"
        assert client.post(route, headers=headers, json={}).status_code == 422
        assert client.post(route, headers=headers, json={"decision": "maybe"}).status_code == 422
        answered = client.post(route, headers=headers, json={"decision": "reject"})
        assert answered.status_code == 200, answered.text
        assert answered.json()["item"]["state"] == "rejected"
        assert answered.json()["item"]["provenance"] == "User explicitly selected reject for this checkpoint in the dashboard."


def test_campaign_hiding_is_persistent_reversible_and_never_hides_active_work(tmp_path):
    _, client, headers, _ = _client(tmp_path)
    with client:
        view = _create_pipeline(client, headers)
        pipeline_id = view["pipeline"]["id"]
        store = Store(tmp_path)
        route = f"/api/pipelines/{pipeline_id}/visibility"
        assert client.post(route, json={"hidden": True}).status_code == 403
        assert client.post(route, headers=headers, json={"hidden": True}).status_code == 409
        with store.connect(write=True) as db:
            db.execute("UPDATE pipelines SET status='completed' WHERE id=?", (pipeline_id,))
        hidden = client.post(route, headers=headers, json={"hidden": True})
        assert hidden.status_code == 200, hidden.text
        assert Store(tmp_path).get_pipeline(pipeline_id)["dashboard_hidden"] is True
        assert client.post(route, headers=headers, json={"hidden": False}).status_code == 200
        assert store.get_pipeline(pipeline_id)["dashboard_hidden"] is False
        with store.connect(write=True) as db:
            db.execute("UPDATE pipelines SET status='stopped',stop_requested=1 WHERE id=?", (pipeline_id,))
        assert client.post(route, headers=headers, json={"hidden": True}).status_code == 200
        store.resume_agent_pipeline(pipeline_id)
        assert store.get_pipeline(pipeline_id)["dashboard_hidden"] is False


def test_quota_resume_and_explicit_legacy_adoption_are_scoped(tmp_path):
    _, client, headers, _ = _client(tmp_path)
    store = Store(tmp_path)
    with client:
        store.set_agent_quota(True, provider_error="synthetic quota fixture", reason="Synthetic test latch.")
        quota = client.get("/api/agent-quota", headers=headers).json()
        assert quota["paused"] is True
        assert quota["provider_error"] == "synthetic quota fixture"
        resumed = client.post("/api/agent-quota/resume", headers=headers, json={})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["quota"]["paused"] is False

        store.company("legacy-fixture", "Legacy Fixture Co", "https://legacy.example.org/careers", enabled=True)
        legacy = store.create_pipeline("legacy-fixture")
        job_id = _seed_listing(store, "legacy-fixture")["id"]
        with store.connect(write=True) as db:
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected) VALUES(?,?,?,1)",
                       (legacy["id"], job_id, timestamp()))
        jobs = client.get(f"/api/pipelines/{legacy['id']}/jobs", headers=headers).json()["jobs"]
        assert jobs[0]["adoption"]["eligible"] is True
        adopted = client.post(
            f"/api/pipelines/{legacy['id']}/jobs/{job_id}/adopt", headers=headers,
            json={"provenance": "User explicitly requested a fresh agent evaluation for this saved job."},
        )
        assert adopted.status_code == 200, adopted.text
        new_pipeline = adopted.json()["pipeline"]
        assert new_pipeline["mode"] == "agentic"
        assert new_pipeline["id"] != legacy["id"]
        assert new_pipeline["adopted_from_pipeline_id"] == legacy["id"]
        assert store.get_pipeline(legacy["id"])["mode"] == "legacy"


def test_profile_refresh_route_is_authenticated_job_scoped_and_records_provenance(tmp_path):
    app, client, headers, manager = _client(tmp_path)
    store = Store(tmp_path)
    with client:
        view = _create_pipeline(client, headers, company="Profile Refresh Fixture",
                                url="https://careers.profile-refresh.invalid/jobs")
        pipeline_id = view["pipeline"]["id"]
        job_id = _seed_listing(store, view["pipeline"]["company_id"])["id"]
        with store.connect(write=True) as db:
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,agent_status) "
                       "VALUES(?,?,?,1,'selected')", (pipeline_id, job_id, timestamp()))
        saved_job = client.get(f"/api/pipelines/{pipeline_id}/jobs", headers=headers).json()["jobs"][0]
        assert saved_job["id"] == job_id
        assert saved_job["profile_refresh"]["eligible"] is True
        calls = []

        def refresh_job_profile(scoped_pipeline_id, scoped_job_id, provenance):
            calls.append((scoped_pipeline_id, scoped_job_id, provenance))
            return {
                **manager["instance"].view(scoped_pipeline_id),
                "profile_refresh": {
                    "pipeline_id": scoped_pipeline_id,
                    "job_id": scoped_job_id,
                    "status": "queued",
                    "already_current": False,
                },
            }

        manager["instance"].refresh_job_profile = refresh_job_profile
        route = f"/api/pipelines/{pipeline_id}/jobs/{job_id}/refresh-profile"
        body = {"provenance": "User corrected the canonical MSc completion date; reevaluate this unsent job."}

        unauthorized = client.post(route, json=body)
        assert unauthorized.status_code == 403
        blank = client.post(route, headers=headers, json={"provenance": "  "})
        assert blank.status_code == 422
        refreshed = client.post(route, headers=headers, json=body)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["profile_refresh"] == {
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "status": "queued",
            "already_current": False,
        }
        assert calls == [(pipeline_id, job_id, body["provenance"])]

        legacy_company_id = "profile-refresh-legacy"
        store.company(legacy_company_id, "Legacy Profile Fixture", "https://legacy.profile-refresh.invalid/jobs",
                      enabled=True)
        legacy = store.create_pipeline(legacy_company_id)
        legacy_job_id = _seed_listing(store, legacy_company_id)["id"]
        with store.connect(write=True) as db:
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected) VALUES(?,?,?,1)",
                       (legacy["id"], legacy_job_id, timestamp()))
        legacy_response = client.post(
            f"/api/pipelines/{legacy['id']}/jobs/{legacy_job_id}/refresh-profile",
            headers=headers, json=body,
        )
        assert legacy_response.status_code == 409
        assert calls == [(pipeline_id, job_id, body["provenance"])]


def test_profile_refresh_route_queues_one_scoped_evaluator_with_real_manager(tmp_path):
    class WaitingRuntime:
        def run(self, task, on_event, should_stop):
            worker = task["worker"]
            return {
                "disposition": "waiting",
                "session_id": worker.get("session_id") or f"ui-refresh-fixture-{worker['id']}",
                "checkpoint": "UI_PROFILE_REFRESH_TEST_ONLY; no external work.",
            }

    created_manager = {}

    def factory(store):
        manager = AgentPipelineManager(
            store, runtime_factory=lambda _store: WaitingRuntime(), max_workers=2, document_workers=1)
        created_manager["instance"] = manager
        return manager

    app = create_app(tmp_path, agent_manager_factory=factory)
    client = TestClient(app)
    store = Store(tmp_path)
    with client:
        headers = {"X-Pipeline-Token": client.get("/api/session").json()["token"]}
        pipeline = store.create_agent_pipeline(
            "UI Profile Refresh TEST ONLY", "https://profile-refresh.fixture.invalid/careers",
            {"country": "US", "employment": "full_time", "arrangement": ["onsite", "hybrid"],
             "title_contains": "Synthetic Engineer", "location_contains": ""},
        )
        job = store.discover(pipeline["company_id"], {
            "url": "https://profile-refresh.fixture.invalid/jobs/synthetic-engineer",
            "title": "Synthetic Engineer",
            "jd": "Synthetic isolated route test listing only.",
            "location": "Chicago, IL", "country": "US", "employment": "full_time",
            "arrangement": "hybrid",
        })
        with store.connect(write=True) as db:
            db.execute("INSERT INTO pipeline_jobs(pipeline_id,job_id,discovered_at,selected,agent_status) "
                       "VALUES(?,?,?,1,'selected')",
                       (pipeline["id"], job["id"], timestamp()))

        assert store.agent_profile_refresh_eligibility(pipeline["id"], job["id"])["eligible"] is True
        response = client.post(
            f"/api/pipelines/{pipeline['id']}/jobs/{job['id']}/refresh-profile",
            headers=headers,
            json={"provenance": "Synthetic test only: profile correction imported before one-job re-evaluation."},
        )
        assert response.status_code == 200, response.text
        refresh = response.json()["profile_refresh"]
        assert refresh["created"] is True
        assert refresh["job_id"] == job["id"]
        assert refresh["profile_version"] == store.profile_version()
        workers = store.list_agent_workers(pipeline["id"])
        evaluators = [worker for worker in workers if worker["role"] == "evaluator"]
        assert len(evaluators) == 1
        assert evaluators[0]["id"] == refresh["evaluator_worker_id"]
        assert evaluators[0]["job_id"] == job["id"]
        assert not any(worker["role"] == "submission" for worker in workers)
        assert store.get_pipeline(pipeline["id"])["counts"]["submitted"] == 0
    assert created_manager["instance"] is not None


def test_agent_artifact_route_checks_workspace_scope_and_hash(tmp_path):
    _, client, headers, _ = _client(tmp_path)
    store = Store(tmp_path)
    with client:
        view = _create_pipeline(client, headers)
        pipeline = view["pipeline"]
        job_id = _seed_listing(store, pipeline["company_id"])["id"]
        worker_id = store.list_agent_workers(pipeline["id"])[0]["id"]
        target = Path(tmp_path) / "artifacts" / "agents" / "fixture-resume.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"synthetic reviewed artifact")
        checksum = hashlib.sha256(target.read_bytes()).hexdigest()
        with store.connect(write=True) as db:
            db.execute(
                "INSERT INTO agent_artifacts(id,pipeline_id,worker_id,job_id,kind,version,path,sha256,filename,"
                "media_type,source_notes,jd_version,profile_version,status,created_at,ready_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',?,?)",
                ("fixture-artifact", pipeline["id"], worker_id, job_id, "resume", 1,
                 str(target.relative_to(tmp_path)).replace("\\", "/"), checksum, "fixture-resume.pdf",
                 "application/pdf", "{}", 1, store.profile_version(), timestamp(), timestamp()),
            )
        downloaded = client.get("/api/agent-artifacts/fixture-artifact", headers=headers)
        assert downloaded.status_code == 200
        assert downloaded.content == b"synthetic reviewed artifact"
        target.write_bytes(b"changed after review")
        mismatch = client.get("/api/agent-artifacts/fixture-artifact", headers=headers)
        assert mismatch.status_code == 409


def test_recruiter_outcome_controls_match_the_manual_provenance_route(tmp_path):
    _, client, headers, _ = _client(tmp_path)
    store = Store(tmp_path)
    root = Path(__file__).resolve().parents[1]
    html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    with client:
        view = _create_pipeline(client, headers)
        job_id = _seed_listing(store, view["pipeline"]["company_id"])["id"]
        with store.connect(write=True) as db:
            db.execute("UPDATE jobs SET phase='submitted' WHERE id=?", (job_id,))
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?)",
                       ("fixture-receipt", job_id, "application_submission", "fixture://synthetic",
                        "c" * 64, "isolated UI test", "synthetic only", timestamp()))
        response = client.post(
            f"/api/jobs/{job_id}/application-status", headers=headers,
            json={"status": "rejected", "provenance": "Source: synthetic test message\nNote: fixture-only outcome"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["application_status"] == "rejected"
        detail = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        assert detail["application_outcome"]["status"] == "rejected"
        assert "synthetic test message" in detail["application_outcome"]["provenance"]
    for outcome in ("pending", "rejected", "accepted", "unknown"):
        assert 'value="' + outcome + '"' in html
    assert "application-status" in js
    assert "status: $('outcome-value').value" in js


def test_agentic_markup_covers_filters_workers_inbox_quota_and_nonfalse_completion():
    root = Path(__file__).resolve().parents[1]
    html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert all(label in html for label in ("United States", "Full-time", "On-site or hybrid"))
    assert 'name="company_name"' in html and 'name="careers_url"' in html
    assert "pipeline_release === 'agentic_pipeline_v1'" in js
    assert "company_name: companyName" in js and "careers_url: careersUrl" in js
    assert "async function loadPipelineWorkers(id)" in js
    assert "if (currentView === 'pipelines' && expandedWorkers.has(p.id))" in js
    assert "if (!workerRecords.has(p.id)) loadPipelineWorkers(p.id)" in js
    assert "'/inbox/' + segment(item.id) + '/answer'" in js
    assert "JSON.stringify({ decision, provenance:" in js
    assert "profileRefresh.eligible === true" in js
    assert "refresh-profile" in js and "Re-evaluate with updated profile" in js
    assert "item.is_current === false" in js and "item.stale_reason" in js
    assert "approve.disabled = true; reject.disabled = true;" in js
    assert "agent-quota/resume" in js
    assert "resume-replacement" in js
    assert "p.status === 'completed' && discovery.status === 'complete'" in js
    assert "Selected listings with a terminal disposition" in js
    assert "Submitted with evidence" in js
    assert "Application fields observed by the agent" in js
    assert "Evidence/source" in js
    assert "Download exact reviewed PDF" in js
    assert "Prepared cover-letter document text" in js
    assert "Agent-reported worker-owned browser tab" in js
    assert "Discovering" not in html or "Discovery" in html


def test_job_record_rationale_is_labeled_as_an_evaluation_time_snapshot():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Fit assessment" in js
    assert "Evaluation-time rationale only; this is not a current submission authorization." in js
