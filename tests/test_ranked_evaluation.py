import json

import pytest

from app.fit_scoring import assessment
from app.store import Store
from test_efficiency_controls import seed, start


def scored(score, blocked=False):
    return {"score": score, "eligibility": "blocked" if blocked else "eligible",
            "confidence": "high", "reason": "Supported fixture assessment",
            "requirements": [{"requirement": "C++ systems work", "importance": "core",
                              "match": "met" if score >= 75 else "transferable", "evidence": "Confirmed fixture source"}],
            "hard_blockers": ([{"requirement": "Mandatory citizenship", "job_evidence": "Citizenship explicitly mandatory",
                                "applicant_evidence": "Confirmed incompatible citizenship"}] if blocked else [])}


def evaluate(store, worker, payload):
    launch = start(store, worker)
    result = store.agent_action(worker["id"], launch["capability"], "finish_evaluation", payload)
    store.finish_agent_turn(worker["id"], {"disposition": "completed"})
    return result


def evaluators(store, pipeline):
    return [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "evaluator"]


def submissions(store, pipeline):
    return [w for w in store.list_agent_workers(pipeline["id"]) if w["role"] == "submission"]


def test_assessment_only_publishes_and_dispatcher_orders_entire_backlog(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 4)
    for worker, score in zip(evaluators(store, pipeline), [30, 100, 1, 84]):
        result = evaluate(store, worker, scored(score))
        assert result["score"] == score and result["submission_worker_id"] is None
    assert submissions(store, pipeline) == []
    assert store.agent_pipeline_counts(pipeline["id"])["scored_backlog"] == 4
    assert len(store.prepare_ranked_submissions()) == 4
    assert store.prepare_ranked_submissions() == []
    assert [j["fit_score"] for j in store.pipeline_jobs(pipeline["id"])["jobs"]] == [100, 84, 30, 1]
    order = [store.get_agent_worker(wid) for wid in store.list_queued_agent_workers()]
    assert [w["context"]["fit_score"] for w in order if w["role"] == "submission"] == [100, 84, 30, 1]
    parents = [w for w in order if w["role"] == "submission"]
    assert all(store.reserve_agent_worker(w["id"]) for w in parents[:3])
    assert not store.reserve_agent_worker(parents[3]["id"])


def test_zero_never_submits_and_low_positive_eligible_score_is_retained(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    a, b = evaluators(store, pipeline)
    evaluate(store, a, scored(0, blocked=True))
    evaluate(store, b, scored(1))
    assert len(store.prepare_ranked_submissions()) == 1
    assert submissions(store, pipeline)[0]["job_id"] == b["job_id"]
    assert store.pipeline_jobs(pipeline["id"])["jobs"][0]["fit_current"]


@pytest.mark.parametrize("change", [
    {"score": True}, {"score": 1.5}, {"score": -1}, {"score": 101}, {"score": "90"},
    {"eligibility": "maybe"}, {"confidence": "guess"}, {"requirements": []},
    {"eligibility": "blocked"}, {"hard_blockers": [{"requirement": "Unsupported"}]},
])
def test_invalid_assessments_are_rejected_without_inventing_a_score(change):
    with pytest.raises(ValueError):
        assessment({**scored(85), **change})


def test_new_stronger_score_precedes_queued_work_but_does_not_preempt_started_task(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 3)
    first, second, third = evaluators(store, pipeline)
    evaluate(store, first, scored(35))
    store.prepare_ranked_submissions()
    active = submissions(store, pipeline)[0]
    assert store.reserve_agent_worker(active["id"])
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='queued',turn_active=0 WHERE id=?", (active["id"],))
    evaluate(store, second, scored(55))
    evaluate(store, third, scored(98))
    store.prepare_ranked_submissions()
    queued = [store.get_agent_worker(i) for i in store.list_queued_agent_workers()]
    parents = [w for w in queued if w["role"] == "submission"]
    assert parents[0]["id"] == active["id"]
    assert [w["context"]["fit_score"] for w in parents[1:]] == [98, 55]


def test_stages_pause_independently_and_never_resume_stopped_campaign(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    a, b = evaluators(store, pipeline)
    store.set_stage_dispatch(pipeline["id"], "submission", True)
    evaluate(store, a, scored(90))
    assert store.prepare_ranked_submissions() == []
    assert store.reserve_agent_worker(b["id"])
    store.set_stage_dispatch(pipeline["id"], "evaluation", True)
    store.set_stage_dispatch(pipeline["id"], "submission", False)
    assert len(store.prepare_ranked_submissions()) == 1
    parent = submissions(store, pipeline)[0]
    assert store.reserve_agent_worker(parent["id"])
    store.set_stage_dispatch(pipeline["id"], "submission", True)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='queued',turn_active=0 WHERE id=?", (parent["id"],))
    assert store.reserve_agent_worker(parent["id"])
    store.request_pipeline_stop(pipeline["id"])
    store.set_stage_dispatch(pipeline["id"], "submission", False)
    assert store.get_pipeline(pipeline["id"])["stop_requested"]
    assert store.prepare_ranked_submissions() == []


def test_paused_evaluation_leaves_submission_backlog_available(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    a, b = evaluators(store, pipeline)
    evaluate(store, a, scored(80))
    store.set_stage_dispatch(pipeline["id"], "evaluation", True)
    assert not store.reserve_agent_worker(b["id"])
    store.prepare_ranked_submissions()
    assert store.reserve_agent_worker(submissions(store, pipeline)[0]["id"])


def test_stale_assessment_cannot_dispatch_or_display_as_current(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    evaluate(store, evaluators(store, pipeline)[0], scored(100))
    with store.connect(write=True) as db:
        db.execute("UPDATE meta SET value='1' WHERE key='profile_version'")
    assert store.prepare_ranked_submissions() == []
    job = store.pipeline_jobs(pipeline["id"])["jobs"][0]
    assert job["fit_score"] is None and not job["fit_current"]


def test_uncertain_prior_send_never_becomes_an_automatic_retry(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    worker = evaluators(store, pipeline)[0]
    evaluate(store, worker, scored(100))
    with store.connect(write=True) as db:
        wid = store._agent_insert_worker(db, pipeline["id"], "submission", job_id=worker["job_id"],
                                        context={"final_action_may_have_been_sent": True})
        db.execute("UPDATE agent_workers SET state='uncertain' WHERE id=?", (wid,))
    assert store.prepare_ranked_submissions() == []
    assert len(submissions(store, pipeline)) == 1


def test_quota_latch_prevents_dispatch_without_discarding_assessment(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    evaluate(store, evaluators(store, pipeline)[0], scored(100))
    store.set_agent_quota(True, reason="Fixture quota")
    assert store.prepare_ranked_submissions() == []
    assert store.pipeline_jobs(pipeline["id"])["jobs"][0]["fit_score"] == 100


def test_legacy_binary_assessment_is_not_assigned_a_fake_score(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    evaluate(store, evaluators(store, pipeline)[0], {"decision": "approve", "reason": "Saved legacy contract"})
    job = store.pipeline_jobs(pipeline["id"])["jobs"][0]
    assert job["fit_score"] is None and job["fit_assessment"] is None
    assert store.agent_pipeline_counts(pipeline["id"])["scored"] == 0


def test_current_bridge_requires_score_but_old_pinned_contract_remains_resumable():
    from tools.agent_mcp import AgentMCPBridge
    bridge = AgentMCPBridge("fixture", "capability", "evaluator", lambda *args: {"received": args[-1]})
    with pytest.raises(ValueError, match="requires a 0–100"):
        bridge.call("finish_evaluation", {"decision": "approve", "reason": "Old binary call"})
    assert bridge.call("finish_evaluation", scored(90))["received"]["score"] == 90
    bridge._catalog = [{"name": "finish_evaluation", "inputSchema": {"required": ["decision", "reason"]}}]
    assert bridge.call("finish_evaluation", {"decision": "approve", "reason": "Saved old contract"})["received"]["decision"] == "approve"


def test_queued_scored_worker_cannot_start_after_profile_changes(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    evaluate(store, evaluators(store, pipeline)[0], scored(100))
    store.prepare_ranked_submissions()
    parent = submissions(store, pipeline)[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE meta SET value='1' WHERE key='profile_version'")
    assert not store.reserve_agent_worker(parent["id"])


def test_scored_cache_restores_score_without_launching_a_submission(tmp_path):
    store, pipeline, d, launch, listings = seed(tmp_path, 1)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/evaluation_agent.md").write_text("Fixture scored policy")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/workers.json").write_text("{}")
    worker = evaluators(store, pipeline)[0]
    evaluate(store, worker, scored(88))
    store.request_pipeline_stop(pipeline["id"])
    store.finish_agent_turn(d["id"], {"disposition": "interrupted"})
    second = store.create_agent_pipeline("Synthetic Efficiency", "https://fictional.invalid/careers")
    discovery = store.list_agent_workers(second["id"])[0]
    turn = start(store, discovery)
    result = store.agent_action(discovery["id"], turn["capability"], "record_listing", listings[0][1])
    assert store.get_agent_worker(result["evaluator_worker_id"])["result"]["cached"]
    assert submissions(store, second) == []
    job = store.pipeline_jobs(second["id"])["jobs"][0]
    assert job["fit_current"] and job["fit_score"] == 88
    assert len(store.prepare_ranked_submissions()) == 1


def test_stage_control_and_score_evidence_are_available_through_authenticated_api(tmp_path):
    from test_ui_contract import _client
    app, client, headers, manager = _client(tmp_path)
    store = app.state.store
    pipeline = store.create_agent_pipeline("Fixture Scoring", "https://fictional.invalid/careers")
    discovery = store.list_agent_workers(pipeline["id"])[0]
    turn = start(store, discovery)
    store.agent_action(discovery["id"], turn["capability"], "record_listing", {
        "requisition_id": "score-1", "url": "https://fictional.invalid/job/score-1", "title": "Fictional C++ role",
        "jd": "Complete fictional JD", "selected": True,
        "metadata": {"country": "US", "employment": "full_time", "arrangement": "hybrid", "location": "Austin"}})
    evaluate(store, evaluators(store, pipeline)[0], scored(91))
    url = "/api/pipelines/" + pipeline["id"]
    jobs = client.get(url + "/jobs", headers=headers).json()["jobs"]
    assert jobs[0]["fit_score"] == 91 and jobs[0]["fit_assessment"]["requirements"]
    assert client.post(url + "/dispatch", json={"stage": "submission", "paused": True}).status_code == 403
    response = client.post(url + "/dispatch", headers=headers, json={"stage": "submission", "paused": True})
    assert response.status_code == 200 and response.json()["pipeline"]["submission_paused"]
    assert client.post(url + "/dispatch", headers=headers, json={"stage": "submission", "paused": "yes"}).status_code == 422


def test_migration_preserves_legacy_workers_and_keeps_old_assessments_unscored(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    evaluate(store, evaluators(store, pipeline)[0], {"decision": "approve", "reason": "Historical binary assessment"})
    before = store.list_agent_workers(pipeline["id"])
    with store.connect(write=True) as db:
        db.execute("DROP INDEX pipeline_jobs_fit")
        for column in ["fit_score", "fit_payload", "fit_jd_version", "fit_profile_version"]:
            db.execute("ALTER TABLE pipeline_jobs DROP COLUMN " + column)
        for column in ["discovery_paused", "evaluation_paused", "submission_paused"]:
            db.execute("ALTER TABLE pipelines DROP COLUMN " + column)
    reopened = Store(tmp_path)
    assert reopened.list_agent_workers(pipeline["id"]) == before
    assert reopened.pipeline_jobs(pipeline["id"])["jobs"][0]["fit_score"] is None
    assert not reopened.get_pipeline(pipeline["id"])["evaluation_paused"]


def test_dispatch_continues_from_saved_scored_backlog_after_dashboard_restart(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 2)
    for worker, score in zip(evaluators(store, pipeline), [71, 97]):
        evaluate(store, worker, scored(score))
    reopened = Store(tmp_path)
    assert len(reopened.prepare_ranked_submissions()) == 2
    assert Store(tmp_path).prepare_ranked_submissions() == []
    parents = [reopened.get_agent_worker(i) for i in reopened.list_queued_agent_workers()]
    assert [w["context"]["fit_score"] for w in parents if w["role"] == "submission"] == [97, 71]


def test_unblocked_zero_is_not_an_application_candidate(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    result = evaluate(store, evaluators(store, pipeline)[0], scored(0))
    assert result["decision"] == "discard"
    assert store.prepare_ranked_submissions() == []


def test_legacy_rescoring_preserves_history_and_queues_once_without_resuming(tmp_path):
    store, pipeline, d, _, _ = seed(tmp_path, 1)
    worker = evaluators(store, pipeline)[0]
    evaluate(store, worker, {"decision": "approve", "reason": "Old assessment"})
    parent = submissions(store, pipeline)[0]
    old = store.get_agent_worker(worker["id"])
    store.request_pipeline_stop(pipeline["id"])
    store.finish_agent_turn(d["id"], {"disposition": "interrupted"})
    result = store.queue_legacy_rescoring(pipeline["id"])
    assert result["count"] == 1
    assert store.get_agent_worker(worker["id"]) == old
    assert store.get_agent_worker(parent["id"])["state"] == "superseded"
    assert store.get_pipeline(pipeline["id"])["stop_requested"]
    assert store.queue_legacy_rescoring(pipeline["id"])["count"] == 0


def test_legacy_rescoring_excludes_receipts_closed_and_possible_sends(tmp_path):
    store, pipeline, _, _, _ = seed(tmp_path, 3)
    workers = evaluators(store, pipeline)
    for worker in workers:
        evaluate(store, worker, {"decision": "approve", "reason": "Historical fit"})
    parents = submissions(store, pipeline)
    with store.connect(write=True) as db:
        db.execute("UPDATE jobs SET phase='submitted' WHERE id=?", (parents[0]["job_id"],))
        db.execute("INSERT INTO agent_submission_results(id,pipeline_id,worker_id,job_id,state,detail,observations,event_ids,created_at) "
                   "VALUES('closed',?,?,?,'discarded','Closed listing','{}','[]','fixture')",
                   (pipeline["id"],parents[1]["id"],parents[1]["job_id"]))
        db.execute("UPDATE agent_workers SET context=? WHERE id=?",
                   (json.dumps({"final_action_may_have_been_sent": True}),parents[2]["id"]))
    assert store.queue_legacy_rescoring(pipeline["id"])["count"] == 0


def test_evaluation_only_batch_ends_after_scores_without_submitting(tmp_path):
    from app.trial_limits import stop_reason
    store, pipeline, _, _, _ = seed(tmp_path, 1)
    worker = evaluators(store, pipeline)[0]
    evaluate(store, worker, scored(68))
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET turn_active=0 WHERE role='discovery'")
        policy = {"pipeline_id":pipeline["id"],"job_ids":[worker["job_id"]],"execution_watermark":0,
                  "allowed_roles":["evaluator"],"max_invocations":None,"deadline_unix":None}
        assert "evaluation-only" in stop_reason(db,policy)
    assert submissions(store,pipeline) == []


def test_discovery_switch_blocks_its_next_turn_without_pausing_evaluation(tmp_path):
    store, pipeline, discovery, _, _ = seed(tmp_path, 1)
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET state='queued',turn_active=0 WHERE id=?", (discovery["id"],))
    store.set_stage_dispatch(pipeline["id"], "discovery", True)
    assert store.get_pipeline(pipeline["id"])["discovery_paused"]
    assert not store.reserve_agent_worker(discovery["id"])
    assert store.reserve_agent_worker(evaluators(store,pipeline)[0]["id"])
    store.set_stage_dispatch(pipeline["id"], "discovery", False)
    assert store.reserve_agent_worker(discovery["id"])


def test_discovery_switch_is_authenticated_and_does_not_resume_stopped_campaign(tmp_path):
    from test_ui_contract import _client
    app,client,headers,_ = _client(tmp_path)
    store=app.state.store;pipeline=store.create_agent_pipeline("Fixture Controls","https://fictional.invalid/careers")
    store.request_pipeline_stop(pipeline["id"])
    response=client.post('/api/pipelines/'+pipeline['id']+'/dispatch',headers=headers,json={'stage':'discovery','paused':True})
    assert response.status_code==200 and response.json()['pipeline']['discovery_paused']
    assert store.get_pipeline(pipeline['id'])['stop_requested']


def test_application_batch_finishes_when_closed_job_has_durable_discard(tmp_path):
    from app.trial_limits import stop_reason
    store,pipeline,discovery,_,_=seed(tmp_path,1)
    worker=evaluators(store,pipeline)[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_workers SET turn_active=0 WHERE role='discovery'")
        db.execute("UPDATE pipeline_jobs SET agent_status='discarded' WHERE pipeline_id=?",(pipeline['id'],))
        policy={'pipeline_id':pipeline['id'],'job_ids':[worker['job_id']],'execution_watermark':0,'max_invocations':None,'deadline_unix':None,'allowed_roles':['submission','document']}
        assert 'terminal results' in stop_reason(db,policy)


def ranked_batch_fixture(tmp_path):
    store,pipeline,discovery,turn,_=seed(tmp_path,3)
    for worker,score in zip(evaluators(store,pipeline),[40,95,75]):
        evaluate(store,worker,scored(score))
    store.agent_action(discovery['id'],turn['capability'],'finish_discovery',{'status':'complete','detail':'Fixture enumeration complete'})
    store.finish_agent_turn(discovery['id'],{'disposition':'completed'})
    return store,pipeline


def test_ranked_submission_batch_selects_top_jobs_without_granting_actions(tmp_path):
    store,pipeline=ranked_batch_fixture(tmp_path)
    policy=store.configure_ranked_submission_batch(pipeline['id'],2)
    assert [j['fit_score'] for j in policy['selected_jobs']]==[95,75]
    assert policy['allowed_roles']==['submission','document']
    view=store.get_pipeline(pipeline['id'])
    assert view['discovery_paused'] and view['evaluation_paused'] and not view['submission_paused']
    assert store.agent_inbox(pipeline['id'])==[]
    assert len(store.prepare_ranked_submissions())==2
    assert all('final_action_authorized_at' not in w['context'] for w in submissions(store,pipeline))


def test_ranked_submission_batch_cannot_retry_possible_send(tmp_path):
    store,pipeline=ranked_batch_fixture(tmp_path)
    top=next(j for j in store.pipeline_jobs(pipeline['id'])['jobs'] if j['fit_score']==95)
    with store.connect(write=True) as db:
        wid=store._agent_insert_worker(db,pipeline['id'],'submission',job_id=top['id'],context={'final_action_may_have_been_sent':True})
        db.execute("UPDATE agent_workers SET state='superseded' WHERE id=?",(wid,))
    policy=store.configure_ranked_submission_batch(pipeline['id'],2)
    assert [j['fit_score'] for j in policy['selected_jobs']]==[75,40]


def test_ranked_submission_batch_does_not_clear_quota(tmp_path):
    from app.store import Conflict
    store,pipeline=ranked_batch_fixture(tmp_path)
    store.set_agent_quota(True,reason='Fixture pause')
    with pytest.raises(Conflict,match='quota'):
        store.configure_ranked_submission_batch(pipeline['id'],2)
    assert store.get_agent_quota()['paused']


def test_ranked_submission_batch_rejects_more_jobs_than_available(tmp_path):
    from app.store import Conflict
    store,pipeline=ranked_batch_fixture(tmp_path)
    with pytest.raises(Conflict,match='Only 3'):
        store.configure_ranked_submission_batch(pipeline['id'],4)


def test_closed_auth_gate_wakes_saved_waiter_without_resetting_application(tmp_path):
    from test_efficiency_controls import cua
    store,pipeline,_,_,_=seed(tmp_path,2)
    for worker in evaluators(store,pipeline):
        evaluate(store,worker,{'decision':'approve','reason':'Fixture'})
    a,b=submissions(store,pipeline)
    x=start(store,a);y=start(store,b);cua(store,a)
    store.agent_action(a['id'],x['capability'],'browser_control',{'operation':'acquire_auth'})
    store.agent_action(b['id'],y['capability'],'browser_control',{'operation':'acquire_auth'})
    store.finish_agent_turn(b['id'],{'disposition':'waiting'})
    before=store.get_agent_worker(b['id'])
    with store.connect(write=True) as db:
        db.execute("UPDATE agent_browser_controls SET state='closed' WHERE control_type='company_auth'")
        store._agent_wake_orphaned_auth_waiters(db)
    after=store.get_agent_worker(b['id'])
    assert after['state']=='queued' and after['session_id']==before['session_id']
    assert 'browser_wait_scope' not in after['context']
    assert 'final_action_authorized_at' not in after['context']


def test_closed_auth_gate_does_not_wake_worker_with_pending_human_action(tmp_path):
    from test_efficiency_controls import cua
    store,pipeline,_,_,_=seed(tmp_path,2)
    for worker in evaluators(store,pipeline):
        evaluate(store,worker,{'decision':'approve','reason':'Fixture'})
    a,b=submissions(store,pipeline)
    x=start(store,a);y=start(store,b);cua(store,a)
    store.agent_action(a['id'],x['capability'],'browser_control',{'operation':'acquire_auth'})
    store.agent_action(b['id'],y['capability'],'browser_control',{'operation':'acquire_auth'})
    store.finish_agent_turn(b['id'],{'disposition':'waiting'})
    with store.connect(write=True) as db:
        w=db.execute('SELECT * FROM agent_workers WHERE id=?',(b['id'],)).fetchone()
        store._agent_insert_inbox(db,w,'approval',action='fixture_review',summary='Human action needed',context={})
        db.execute("UPDATE agent_browser_controls SET state='closed' WHERE control_type='company_auth'")
        store._agent_wake_orphaned_auth_waiters(db)
    assert store.get_agent_worker(b['id'])['state']=='waiting_approval'
