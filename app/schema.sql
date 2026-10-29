PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta VALUES ('schema_version', '1');
INSERT OR IGNORE INTO meta VALUES ('profile_version', '0');
INSERT OR IGNORE INTO meta VALUES ('paused', 'false');

CREATE TABLE IF NOT EXISTS companies(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, careers_url TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, last_scan_at TEXT,
 scan_note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_documents(
 id TEXT PRIMARY KEY, source_path TEXT NOT NULL, sha256 TEXT NOT NULL,
 content TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_versions(
 document_id TEXT NOT NULL, sha256 TEXT NOT NULL, content TEXT NOT NULL,
 source_path TEXT NOT NULL, imported_at TEXT NOT NULL, PRIMARY KEY(document_id,sha256)
);
CREATE TABLE IF NOT EXISTS technologies(
 key TEXT PRIMARY KEY, name TEXT NOT NULL, answer TEXT NOT NULL
 CHECK(answer IN ('yes','no','unknown')), context TEXT NOT NULL,
 source TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS technology_aliases(
 alias TEXT PRIMARY KEY, technology_key TEXT NOT NULL REFERENCES technologies(key)
);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 external_key TEXT NOT NULL, requisition_id TEXT, url TEXT NOT NULL, title TEXT NOT NULL,
 location TEXT NOT NULL, country TEXT NOT NULL, employment TEXT NOT NULL,
 arrangement TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'new',
 run_status TEXT NOT NULL DEFAULT 'idle', jd_version INTEGER NOT NULL DEFAULT 1,
 reason TEXT NOT NULL DEFAULT '', active_stage TEXT, lease_owner TEXT, lease_token TEXT,
 lease_until REAL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 UNIQUE(company_id, external_key)
);
CREATE TABLE IF NOT EXISTS job_versions(
 job_id TEXT NOT NULL REFERENCES jobs(id), version INTEGER NOT NULL,
 jd TEXT NOT NULL, sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
 source TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(job_id,version)
);
CREATE TABLE IF NOT EXISTS evaluations(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), jd_version INTEGER NOT NULL,
 profile_version INTEGER NOT NULL, decision TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packages(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), jd_version INTEGER NOT NULL,
 profile_version INTEGER NOT NULL, manifest TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions(
 id TEXT PRIMARY KEY, key TEXT NOT NULL, technology_key TEXT,
 prompt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', answer TEXT,
 created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_question ON questions(key) WHERE status='pending';
CREATE TABLE IF NOT EXISTS question_jobs(
 question_id TEXT NOT NULL REFERENCES questions(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 PRIMARY KEY(question_id,job_id)
);
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), kind TEXT NOT NULL,
 artifact_path TEXT NOT NULL, sha256 TEXT NOT NULL, source TEXT NOT NULL,
 attribution TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_evidence ON evidence(job_id,kind,sha256);
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), state TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), action TEXT NOT NULL,
 jd_version INTEGER NOT NULL, package_id TEXT, terms_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources(
 key TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL, lease_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT REFERENCES jobs(id), actor TEXT NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_sources(
 company_id TEXT PRIMARY KEY REFERENCES companies(id),
 adapter TEXT NOT NULL CHECK(adapter IN ('greenhouse','lever','lever_eu')),
 board_token TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans(
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 status TEXT NOT NULL, payload TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, lease_until REAL NOT NULL
);

-- Durable, company-scoped pipeline runs.  Membership is separate from jobs so a
-- listing may participate in more than one explicitly started run.
CREATE TABLE IF NOT EXISTS pipelines(
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 status TEXT NOT NULL, stage TEXT NOT NULL, filters TEXT NOT NULL,
 discovery_status TEXT NOT NULL DEFAULT 'pending', discovery_payload TEXT NOT NULL DEFAULT '{}',
 blockers TEXT NOT NULL DEFAULT '[]', stop_requested INTEGER NOT NULL DEFAULT 0,
 mode TEXT NOT NULL DEFAULT 'legacy', adopted_from_pipeline_id TEXT,
 agent_test_only INTEGER NOT NULL DEFAULT 0,
 dashboard_hidden INTEGER NOT NULL DEFAULT 0,
 evaluation_paused INTEGER NOT NULL DEFAULT 0, submission_paused INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pipelines_company_created ON pipelines(company_id,created_at);
CREATE TABLE IF NOT EXISTS pipeline_jobs(
 pipeline_id TEXT NOT NULL REFERENCES pipelines(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 discovered_at TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 1,
 excluded_reason TEXT NOT NULL DEFAULT '', manual_discard_reason TEXT NOT NULL DEFAULT '',
 manual_discard_provenance TEXT NOT NULL DEFAULT '', manual_discard_actor TEXT NOT NULL DEFAULT '',
 manual_discarded_at TEXT, agent_decision TEXT NOT NULL DEFAULT '', agent_reason TEXT NOT NULL DEFAULT '',
 agent_status TEXT NOT NULL DEFAULT '',
 fit_score INTEGER CHECK(fit_score BETWEEN 0 AND 100), fit_payload TEXT NOT NULL DEFAULT '{}',
 fit_jd_version INTEGER, fit_profile_version INTEGER,
 PRIMARY KEY(pipeline_id,job_id)
);
CREATE INDEX IF NOT EXISTS pipeline_jobs_job ON pipeline_jobs(job_id);

-- Agentic workflow state is additive and separate from the legacy offline
-- worker leases. Existing company pipelines remain mode='legacy'.
CREATE TABLE IF NOT EXISTS agent_workers(
 id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL REFERENCES pipelines(id),
 job_id TEXT REFERENCES jobs(id), parent_id TEXT REFERENCES agent_workers(id),
 role TEXT NOT NULL CHECK(role IN ('discovery','evaluator','submission','document')),
 state TEXT NOT NULL, session_id TEXT, resume_mode TEXT NOT NULL DEFAULT 'new_session',
 checkpoint TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '{}',
 result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', provider_error TEXT NOT NULL DEFAULT '',
 jd_version INTEGER, profile_version INTEGER NOT NULL DEFAULT 0,
 capability_sha256 TEXT NOT NULL DEFAULT '', capability_expires_at REAL,
 launch_generation INTEGER NOT NULL DEFAULT 0, turn_active INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_workers_pipeline_state ON agent_workers(pipeline_id,state,role);
CREATE INDEX IF NOT EXISTS agent_workers_job ON agent_workers(job_id,role,state);
CREATE INDEX IF NOT EXISTS agent_workers_parent ON agent_workers(parent_id);

CREATE TABLE IF NOT EXISTS agent_runtime_events(
 id TEXT PRIMARY KEY, worker_id TEXT NOT NULL REFERENCES agent_workers(id),
 session_id TEXT, event_type TEXT NOT NULL, tool_name TEXT NOT NULL DEFAULT '',
 success INTEGER, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_events_worker_session ON agent_runtime_events(worker_id,session_id,event_type,tool_name,success);

-- Host-linked listing captures reference actual successful browser observations
-- from the same worker/session. They are transport provenance, not model IDs.
CREATE TABLE IF NOT EXISTS agent_listing_captures(
 capture_ref TEXT NOT NULL UNIQUE, pipeline_id TEXT NOT NULL REFERENCES pipelines(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 worker_id TEXT NOT NULL REFERENCES agent_workers(id), event_id TEXT NOT NULL REFERENCES agent_runtime_events(id),
 session_id TEXT NOT NULL, jd_version INTEGER NOT NULL DEFAULT 1,
 description_sha256 TEXT NOT NULL DEFAULT '', observation TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
 PRIMARY KEY(pipeline_id,job_id,event_id)
);
CREATE INDEX IF NOT EXISTS agent_listing_captures_job ON agent_listing_captures(job_id,created_at);

-- Agent-authored evaluation cache entries are reusable only for the exact
-- JD/profile/filter/prompt/model snapshot; ask decisions are never cached.
CREATE TABLE IF NOT EXISTS agent_evaluation_cache(
 cache_key TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL REFERENCES pipelines(id),
 job_id TEXT NOT NULL REFERENCES jobs(id), jd_version INTEGER NOT NULL,
 profile_version INTEGER NOT NULL, policy_sha256 TEXT NOT NULL, model TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('approve','discard')), payload TEXT NOT NULL,
 source_worker_id TEXT NOT NULL REFERENCES agent_workers(id), created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_evaluation_cache_job ON agent_evaluation_cache(job_id,jd_version,profile_version);

-- Host-tracked workflow plans are exact-origin and exact-structure scoped.
-- Plan contents remain agent-authored; current DOM validation stays with agent.
CREATE TABLE IF NOT EXISTS agent_workflow_plans(
 id TEXT PRIMARY KEY, origin TEXT NOT NULL, role TEXT NOT NULL, structure_fingerprint TEXT NOT NULL,
 plan TEXT NOT NULL, observation TEXT NOT NULL DEFAULT '{}', event_id TEXT NOT NULL REFERENCES agent_runtime_events(id),
 worker_id TEXT NOT NULL REFERENCES agent_workers(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(origin,role,structure_fingerprint)
);

-- Explicit shared applicant/job fact questions. Keys are agent-chosen, exact
-- identifiers, never generated through fuzzy semantic matching.
CREATE TABLE IF NOT EXISTS agent_fact_questions(
 id TEXT PRIMARY KEY, fact_scope TEXT NOT NULL CHECK(fact_scope IN ('applicant','job')),
 scope_id TEXT NOT NULL, fact_key TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','answered','cancelled')),
 inbox_item_id TEXT NOT NULL REFERENCES agent_inbox(id), created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_fact_question_pending ON agent_fact_questions(fact_scope,scope_id,fact_key)
 WHERE state='pending';
CREATE TABLE IF NOT EXISTS agent_fact_subscribers(
 fact_id TEXT NOT NULL REFERENCES agent_fact_questions(id), worker_id TEXT NOT NULL REFERENCES agent_workers(id),
 pipeline_id TEXT NOT NULL REFERENCES pipelines(id), job_id TEXT REFERENCES jobs(id), created_at TEXT NOT NULL,
 PRIMARY KEY(fact_id,worker_id)
);

-- Authentication is company-scoped; connector failures are global. The host
-- only changes these controls from explicit agent/user actions and evidence.
CREATE TABLE IF NOT EXISTS agent_browser_controls(
 scope_key TEXT PRIMARY KEY, control_type TEXT NOT NULL CHECK(control_type IN ('company_auth','connector_circuit')),
 state TEXT NOT NULL CHECK(state IN ('open','closed')), reason TEXT NOT NULL DEFAULT '',
 provenance TEXT NOT NULL DEFAULT '', worker_id TEXT REFERENCES agent_workers(id),
 event_ids TEXT NOT NULL DEFAULT '[]', opened_at TEXT, closed_at TEXT, updated_at TEXT NOT NULL
);

-- Render cache uses exact Markdown and renderer/style hashes. Copies are
-- independently path/hash validated into each requested package.
CREATE TABLE IF NOT EXISTS agent_render_cache(
 cache_key TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL, renderer_sha256 TEXT NOT NULL,
 pdf_path TEXT NOT NULL, pdf_sha256 TEXT NOT NULL, previews TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_cache_observations(
 id INTEGER PRIMARY KEY AUTOINCREMENT, pipeline_id TEXT NOT NULL REFERENCES pipelines(id),
 worker_id TEXT REFERENCES agent_workers(id), cache_kind TEXT NOT NULL CHECK(cache_kind IN ('evaluation','render')),
 hit INTEGER NOT NULL CHECK(hit IN (0,1)), cache_key TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_resume_bases(
 id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL UNIQUE REFERENCES agent_artifacts(id),
 family TEXT NOT NULL, summary TEXT NOT NULL, profile_version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_cache_observations_pipeline ON agent_cache_observations(pipeline_id,cache_kind,hit);

-- Normalized usage rows are idempotent per logical CLI session turn. Raw
-- counters are retained beside deltas so cumulative session counters can be
-- resumed safely after the Python manager restarts.
CREATE TABLE IF NOT EXISTS agent_usage(
 worker_id TEXT NOT NULL REFERENCES agent_workers(id), session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
 counter_kind TEXT NOT NULL CHECK(counter_kind IN ('cumulative','turn')),
 input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER, reasoning_tokens INTEGER,
 delta_input_tokens INTEGER, delta_output_tokens INTEGER, delta_cached_input_tokens INTEGER, delta_reasoning_tokens INTEGER,
 tool_call_count INTEGER, elapsed_seconds REAL, counter_source TEXT,
 created_at TEXT NOT NULL, execution TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(worker_id,session_id,turn_id)
);
CREATE INDEX IF NOT EXISTS agent_usage_pipeline_worker ON agent_usage(worker_id,created_at);

CREATE TABLE IF NOT EXISTS agent_execution_runs(
 worker_id TEXT NOT NULL REFERENCES agent_workers(id), generation INTEGER NOT NULL,
 execution TEXT NOT NULL, previous_session_id TEXT, started_at TEXT NOT NULL,
 finished_at TEXT, state TEXT, PRIMARY KEY(worker_id,generation)
);

CREATE TABLE IF NOT EXISTS agent_submission_results(
 id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL REFERENCES pipelines(id),
 worker_id TEXT NOT NULL REFERENCES agent_workers(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', observations TEXT NOT NULL DEFAULT '{}',
 event_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_submission_job ON agent_submission_results(job_id,created_at);

CREATE TABLE IF NOT EXISTS agent_inbox(
 id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL REFERENCES pipelines(id),
 worker_id TEXT NOT NULL REFERENCES agent_workers(id), job_id TEXT REFERENCES jobs(id),
 kind TEXT NOT NULL CHECK(kind IN ('question','approval','first_application_review')),
 dedupe_key TEXT NOT NULL DEFAULT '', question TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '{}',
 response_hint TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'pending', response TEXT NOT NULL DEFAULT '', provenance TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS agent_inbox_pipeline_state ON agent_inbox(pipeline_id,state,created_at);
CREATE INDEX IF NOT EXISTS agent_inbox_worker ON agent_inbox(worker_id,state);
CREATE UNIQUE INDEX IF NOT EXISTS agent_inbox_dedupe ON agent_inbox(worker_id,dedupe_key)
 WHERE dedupe_key!='' AND state='pending';

CREATE TABLE IF NOT EXISTS agent_artifacts(
 id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL REFERENCES pipelines(id),
 worker_id TEXT NOT NULL REFERENCES agent_workers(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 kind TEXT NOT NULL CHECK(kind IN ('resume','cover_letter')),
 version INTEGER NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 filename TEXT NOT NULL, media_type TEXT NOT NULL, source_notes TEXT NOT NULL DEFAULT '{}',
 jd_version INTEGER NOT NULL, profile_version INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL, ready_at TEXT,
 pdf_path TEXT NOT NULL DEFAULT '', pdf_sha256 TEXT NOT NULL DEFAULT '', renderer_sha256 TEXT NOT NULL DEFAULT '',
 preview_paths TEXT NOT NULL DEFAULT '[]',
 UNIQUE(worker_id,kind,version)
);
CREATE INDEX IF NOT EXISTS agent_artifacts_job_snapshot ON agent_artifacts(job_id,jd_version,profile_version,status);

CREATE TABLE IF NOT EXISTS agent_legacy_question_links(
 pipeline_id TEXT NOT NULL REFERENCES pipelines(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 legacy_question_id TEXT NOT NULL REFERENCES questions(id), status TEXT NOT NULL,
 actor TEXT NOT NULL, provenance TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(pipeline_id,job_id,legacy_question_id)
);

CREATE TABLE IF NOT EXISTS agent_adoptions(
 source_pipeline_id TEXT NOT NULL REFERENCES pipelines(id), source_job_id TEXT NOT NULL REFERENCES jobs(id),
 pipeline_id TEXT NOT NULL REFERENCES pipelines(id), provenance TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(source_pipeline_id,source_job_id)
);
INSERT OR IGNORE INTO meta(key,value) VALUES('agent_quota_latch','{}');

-- No row means a historical submission has no recorded recruiter response;
-- API reads present that as unknown without changing imported legacy data.
CREATE TABLE IF NOT EXISTS application_outcomes(
 job_id TEXT PRIMARY KEY REFERENCES jobs(id),
 status TEXT NOT NULL CHECK(status IN ('pending','rejected','accepted','unknown')),
 provenance TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- Submission state is bound to the exact reviewed package and applicant snapshot.
CREATE TABLE IF NOT EXISTS submission_contexts(
 job_id TEXT NOT NULL REFERENCES jobs(id), pipeline_id TEXT NOT NULL REFERENCES pipelines(id),
 session_id TEXT, tab_id TEXT, checkpoint TEXT NOT NULL, package_id TEXT NOT NULL,
 jd_version INTEGER NOT NULL, profile_version INTEGER NOT NULL, package_sha256 TEXT NOT NULL,
 context TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
 PRIMARY KEY(job_id,pipeline_id)
);
CREATE TABLE IF NOT EXISTS submission_approvals(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
 pipeline_id TEXT NOT NULL REFERENCES pipelines(id), action TEXT NOT NULL,
 jd_version INTEGER NOT NULL, profile_version INTEGER NOT NULL, package_id TEXT NOT NULL,
 terms_hash TEXT NOT NULL, terms_text TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','consumed')),
 provenance TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS submission_approvals_pending ON submission_approvals(pipeline_id,status);
CREATE INDEX IF NOT EXISTS submission_approvals_snapshot ON submission_approvals(job_id,pipeline_id,action,jd_version,profile_version,package_id,terms_hash,created_at);
CREATE TABLE IF NOT EXISTS submission_questions(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
 pipeline_id TEXT NOT NULL REFERENCES pipelines(id), prompt TEXT NOT NULL,
 prompt_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','answered')),
 answer TEXT NOT NULL DEFAULT '', provenance TEXT NOT NULL DEFAULT '',
 actor TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, resolved_at TEXT,
 UNIQUE(job_id,pipeline_id,prompt_hash)
);
CREATE INDEX IF NOT EXISTS submission_questions_pending ON submission_questions(pipeline_id,status);

-- Public listing facts that could not be resolved from the feed/description are
-- verified against an exact quoted excerpt before the job is reevaluated.
CREATE TABLE IF NOT EXISTS listing_fact_questions(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
 pipeline_id TEXT NOT NULL REFERENCES pipelines(id), field TEXT NOT NULL,
 jd_version INTEGER NOT NULL, prompt TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','answered')),
 answer_value TEXT NOT NULL DEFAULT '', answer_evidence TEXT NOT NULL DEFAULT '',
 provenance TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, resolved_at TEXT,
 UNIQUE(job_id,pipeline_id,jd_version,field)
);
CREATE INDEX IF NOT EXISTS listing_fact_questions_pending ON listing_fact_questions(pipeline_id,status);

UPDATE meta SET value='7' WHERE key='schema_version';
